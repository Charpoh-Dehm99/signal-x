"""Binance data-dump ingestion: URL building, checksum-verified downloads,
zip-safe extraction, and CSV -> DataFrame / Parquet normalization.

Security posture (fail loud, never silent):
- URLs and paths are built only from validated symbol / month / kind values;
  callers never supply URL fragments or filesystem names.
- Every downloaded artifact is SHA256-verified against the published
  .CHECKSUM file (strict by default).
- Zip extraction rejects absolute paths, '..', separators, hidden names,
  unexpected extensions, oversized members, and too many members
  (zip-slip / zip-bomb guards).
- CSV parsing raises DataFeedError on schema drift, bad numerics, or OHLC
  invariant violations instead of dropping rows silently.
"""

from __future__ import annotations

import datetime
import hashlib
import re
import shutil
import zipfile
from pathlib import Path

import pandas as pd

from .validation import ValidationError, validate_symbol

DATA_HOST = "https://data.binance.vision"

# kind -> (url path template, filename stem template, storage group, period unit)
# The storage group determines the raw/ + parquet/ directory: monthly and daily
# dumps of the same logical series share one group, so the parquet build
# concatenates and de-duplicates them (the 1m series may mix monthly history
# with daily files for the current, still-incomplete month).
KINDS: dict[str, tuple[str, str, str, str]] = {
    "klines": (
        "data/futures/um/monthly/klines/{symbol}/1m",
        "{symbol}-1m-{period}",
        "klines",
        "month",
    ),
    "klines_1m_daily": (
        "data/futures/um/daily/klines/{symbol}/1m",
        "{symbol}-1m-{period}",
        "klines",
        "day",
    ),
    "klines_1h": (
        "data/futures/um/monthly/klines/{symbol}/1h",
        "{symbol}-1h-{period}",
        "klines_1h",
        "month",
    ),
    "funding": (
        "data/futures/um/monthly/fundingRate/{symbol}",
        "{symbol}-fundingRate-{period}",
        "funding",
        "month",
    ),
    # NOTE: UM funding dumps exist only monthly (verified 2026-10: the daily
    # fundingRate prefix is empty), so the current-month tail has no funding.
}

MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])\Z")
DAY_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\Z")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")

MAX_ZIP_MEMBERS = 16
MAX_MEMBER_BYTES = 1 << 30   # 1 GiB uncompressed per member
MAX_TOTAL_BYTES = 4 << 30    # 4 GiB uncompressed per archive

KLINE_COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore",
]
FUNDING_COLUMNS = ["funding_time", "interval_hours", "rate"]

_US_PER_MS_THRESHOLD = 10**14  # dumps switched some fields to microseconds in 2025


class DataFeedError(RuntimeError):
    """Any ingestion failure that must be surfaced, never swallowed."""


# --------------------------------------------------------------------- URLs

def validate_month(month: object) -> str:
    if not isinstance(month, str) or not MONTH_RE.match(month):
        raise ValidationError(f"month must be YYYY-MM, got {month!r}")
    return month


def validate_day(day: object) -> str:
    if not isinstance(day, str) or not DAY_RE.match(day):
        raise ValidationError(f"day must be YYYY-MM-DD, got {day!r}")
    try:
        datetime.date.fromisoformat(day)
    except ValueError as exc:
        raise ValidationError(f"invalid calendar day: {day!r}") from exc
    return day


def storage_group(kind: object) -> str:
    if kind not in KINDS:
        raise ValidationError(f"unknown dump kind: {kind!r}")
    return KINDS[kind][2]


def validate_period(kind: object, period: object) -> str:
    if kind not in KINDS:
        raise ValidationError(f"unknown dump kind: {kind!r}")
    unit = KINDS[kind][3]
    return validate_month(period) if unit == "month" else validate_day(period)


def dump_stem(kind: str, symbol: object, period: object) -> str:
    if kind not in KINDS:
        raise ValidationError(f"unknown dump kind: {kind!r}")
    sym = validate_symbol(symbol)
    per = validate_period(kind, period)
    return KINDS[kind][1].format(symbol=sym, period=per)


def dump_url(kind: str, symbol: object, period: object) -> str:
    if kind not in KINDS:
        raise ValidationError(f"unknown dump kind: {kind!r}")
    sym = validate_symbol(symbol)
    per = validate_period(kind, period)
    base, template, _group, _unit = KINDS[kind]
    path = base.format(symbol=sym)
    return f"{DATA_HOST}/{path}/{template.format(symbol=sym, period=per)}.zip"


def raw_dir(data_root: Path, kind: object, symbol: object) -> Path:
    """Raw dump directory for a kind's storage group + symbol."""
    sym = validate_symbol(symbol)
    return data_root / "raw" / storage_group(kind) / sym


def parquet_path(data_root: Path, kind: object, symbol: object) -> Path:
    """Parquet output path for a kind's storage group + symbol."""
    sym = validate_symbol(symbol)
    return data_root / "parquet" / storage_group(kind) / f"{sym}.parquet"


def read_plan(plan_file: Path) -> list[tuple[str, str, str]]:
    """Read a `kind,symbol,period` CSV plan into a validated, de-duplicated task list.

    Fails loud (DataFeedError) on a missing file, wrong header, bad row, or an
    unknown kind / invalid symbol / invalid period.
    """
    import csv

    if not isinstance(plan_file, Path) or not plan_file.exists():
        raise DataFeedError(f"plan file not found: {plan_file!r}")
    tasks: set[tuple[str, str, str]] = set()
    with open(plan_file, newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames != ["kind", "symbol", "period"]:
            raise DataFeedError(f"plan header must be kind,symbol,period (got {reader.fieldnames})")
        for lineno, row in enumerate(reader, start=2):
            try:
                kind = row["kind"]
                if kind not in KINDS:
                    raise ValidationError(f"unknown kind {kind!r}")
                symbol = validate_symbol(row["symbol"])
                period = validate_period(kind, row["period"])
            except (KeyError, TypeError, ValidationError) as exc:
                raise DataFeedError(f"plan line {lineno}: {exc}") from exc
            tasks.add((kind, symbol, period))
    return sorted(tasks)


# ---------------------------------------------------------------- checksums

def parse_checksum(text: str) -> tuple[str, str]:
    """Parse a '.CHECKSUM' file body: '<sha256hex>  <filename>'."""
    if not isinstance(text, str):
        raise DataFeedError("checksum content is not text")
    parts = text.strip().split()
    if len(parts) != 2 or not _SHA256_RE.match(parts[0]):
        raise DataFeedError(f"malformed checksum file: {text[:80]!r}")
    return parts[0].lower(), parts[1]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path: Path, checksum_text: str, *, expected_name: str | None = None) -> None:
    digest, name = parse_checksum(checksum_text)
    if expected_name is not None and name != expected_name:
        raise DataFeedError(f"checksum names {name!r}, expected {expected_name!r}")
    actual = sha256_file(path)
    if actual != digest:
        raise DataFeedError(f"SHA256 mismatch for {path.name}: expected {digest}, got {actual}")


# ---------------------------------------------------------------- download

def download(
    url: str,
    dest: Path,
    *,
    session=None,
    timeout: float = 60.0,
    retries: int = 2,
) -> bool:
    """Stream `url` to `dest` atomically. Returns False when the file is absent (404).

    Raises DataFeedError for any other failure after retries are exhausted.
    """
    if not url.startswith(DATA_HOST + "/"):
        raise DataFeedError(f"refusing non-whitelisted host: {url!r}")
    import requests  # local import keeps module import cheap for tests

    sess = session if session is not None else requests
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")

    last_exc: Exception | None = None
    for _ in range(retries + 1):
        try:
            resp = sess.get(url, stream=True, timeout=timeout)
            try:
                if resp.status_code == 404:
                    return False
                if resp.status_code != 200:
                    raise DataFeedError(f"HTTP {resp.status_code} for {url}")
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1 << 20):
                        fh.write(chunk)
            finally:
                close = getattr(resp, "close", None)
                if callable(close):
                    close()
            tmp.replace(dest)
            return True
        except (OSError, DataFeedError, requests.RequestException) as exc:
            last_exc = exc
    raise DataFeedError(f"download failed for {url}: {last_exc}") from last_exc


# ---------------------------------------------------------------- extraction

def safe_extract_zip(
    zip_path: Path,
    dest_dir: Path,
    *,
    allowed_suffixes: tuple[str, ...] = (".csv",),
) -> list[Path]:
    """Extract a dump zip; reject anything that could escape or bomb dest_dir."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_MEMBERS:
            raise DataFeedError(f"{zip_path.name}: too many zip members ({len(infos)})")
        total = 0
        for info in infos:
            name = info.filename
            if (
                name != Path(name).name
                or "/" in name
                or "\\" in name
                or name.startswith(".")
                or "\x00" in name
            ):
                raise DataFeedError(f"{zip_path.name}: unsafe member name {name!r}")
            if not name.lower().endswith(allowed_suffixes):
                raise DataFeedError(f"{zip_path.name}: unexpected member extension {name!r}")
            if info.file_size > MAX_MEMBER_BYTES:
                raise DataFeedError(f"{zip_path.name}: member too large: {name!r}")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise DataFeedError(f"{zip_path.name}: archive exceeds size budget")
            target = dest_dir / name
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)
            out.append(target)
    return out


# --------------------------------------------------------------- CSV parsing

def _read_csv_autodetect(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, header=None, dtype=str)
    if len(df):
        first = str(df.iloc[0, 0]).strip().lower()
        if not _looks_numeric(first):
            df = df.iloc[1:].reset_index(drop=True)
    return df


def _looks_numeric(text: str) -> bool:
    try:
        float(text)
        return True
    except ValueError:
        return False


def _coerce_int(series: pd.Series, name: str, path: Path) -> pd.Series:
    out = pd.to_numeric(series, errors="coerce")
    if out.isna().any():
        raise DataFeedError(f"{path.name}: non-numeric {name} in {int(out.isna().sum())} rows")
    rounded = out.round()
    if not (out == rounded).all():
        raise DataFeedError(f"{path.name}: fractional values in integer column {name}")
    return rounded.astype("int64")


def _coerce_float(series: pd.Series, name: str, path: Path) -> pd.Series:
    out = pd.to_numeric(series, errors="coerce")
    if out.isna().any():
        raise DataFeedError(f"{path.name}: non-numeric {name} in {int(out.isna().sum())} rows")
    return out.astype("float64")


def read_kline_csv(path: Path) -> pd.DataFrame:
    """Normalize one monthly kline CSV. Fails loud on any schema/invariant drift."""
    df = _read_csv_autodetect(path)
    if df.empty:
        raise DataFeedError(f"empty kline file: {path.name}")
    if df.shape[1] < 11:
        raise DataFeedError(f"{path.name}: expected >= 11 columns, got {df.shape[1]}")
    df = df.iloc[:, : len(KLINE_COLUMNS)].copy()
    while df.shape[1] < len(KLINE_COLUMNS):
        df[df.shape[1]] = "0"
    df.columns = KLINE_COLUMNS

    for col in ("open_time", "close_time", "trades"):
        df[col] = _coerce_int(df[col], col, path)
    for col in ("open", "high", "low", "close", "volume", "quote_volume",
                "taker_buy_base", "taker_buy_quote"):
        df[col] = _coerce_float(df[col], col, path)

    if (df["open_time"] >= _US_PER_MS_THRESHOLD).any():
        df["open_time"] = df["open_time"] // 1000
        df["close_time"] = df["close_time"] // 1000

    bad = (
        (df["high"] < df["low"])
        | (df["high"] < df[["open", "close"]].max(axis=1))
        | (df["low"] > df[["open", "close"]].min(axis=1))
        | (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
        | (df[["volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]] < 0).any(axis=1)
        | (df["trades"] < 0)
    )
    if bool(bad.any()):
        raise DataFeedError(f"{path.name}: {int(bad.sum())} rows violate OHLC/volume invariants")

    df = (
        df.drop_duplicates(subset="open_time", keep="last")
        .sort_values("open_time")
        .reset_index(drop=True)
    )
    return df[
        ["open_time", "open", "high", "low", "close", "volume", "close_time",
         "quote_volume", "trades", "taker_buy_base", "taker_buy_quote"]
    ]


def read_funding_csv(path: Path) -> pd.DataFrame:
    """Normalize one monthly funding-rate CSV: funding_time(ms), interval_hours, rate."""
    df = _read_csv_autodetect(path)
    if df.empty:
        raise DataFeedError(f"empty funding file: {path.name}")
    if df.shape[1] < 3:
        raise DataFeedError(f"{path.name}: expected >= 3 columns, got {df.shape[1]}")
    df = df.iloc[:, :3].copy()
    df.columns = FUNDING_COLUMNS

    df["funding_time"] = _coerce_int(df["funding_time"], "funding_time", path)
    df["interval_hours"] = _coerce_float(df["interval_hours"], "interval_hours", path)
    df["rate"] = _coerce_float(df["rate"], "rate", path)

    if (df["funding_time"] >= _US_PER_MS_THRESHOLD).any():
        df["funding_time"] = df["funding_time"] // 1000

    if (df["interval_hours"] <= 0).any():
        raise DataFeedError(f"{path.name}: non-positive funding interval")
    if df["rate"].abs().max() > 0.05:
        raise DataFeedError(f"{path.name}: implausible funding rate magnitude")

    df = (
        df.drop_duplicates(subset="funding_time", keep="last")
        .sort_values("funding_time")
        .reset_index(drop=True)
    )
    return df


# ---------------------------------------------------------------- parquet

def write_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def read_parquet(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path)
