"""Book-feature recorder core: depth/bookTicker parsing + per-symbol store.

Observation-only microstructure capture for the watchlist. G0 killed the
tested signal space; this is instrumentation for cost/impact research, not a
signal claim. Pure, testable pieces only; the async driver lives in
scripts/book.py. Fail-loud: malformed frames raise BookError.

Features per depth snapshot: mid, spread, per-band quote depth (lower bound
from the top-20 levels) and imbalance, plus the latest bookTicker BBO.
Storage: <root>/<SYMBOL>.parquet, deduped on ts_ms, sorted ascending.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .validation import ValidationError, _strict_float, validate_symbol

BANDS: tuple[tuple[float, str], ...] = ((0.5, "05"), (1.0, "1"), (2.0, "2"))

BOOK_FIELDS: tuple[tuple[str, str], ...] = (
    ("ts_ms", "int64"),
    ("mid", "float64"),
    ("spread_abs", "float64"),
    ("spread_bps", "float64"),
    ("bid0", "float64"),
    ("ask0", "float64"),
    ("bid0_qty", "float64"),
    ("ask0_qty", "float64"),
) + tuple(
    (f"d{key}_bid", "float64") for _, key in BANDS
) + tuple(
    (f"d{key}_ask", "float64") for _, key in BANDS
) + tuple(
    (f"imbal{key}", "float64") for _, key in BANDS
) + (
    ("tb_bid", "float64"),
    ("tb_ask", "float64"),
    ("tb_bid_qty", "float64"),
    ("tb_ask_qty", "float64"),
)


class BookError(RuntimeError):
    """Malformed book input. Never swallow this."""


@dataclass(frozen=True)
class BookTicker:
    symbol: str
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float
    ts_ms: int


@dataclass(frozen=True)
class DepthSnapshot:
    symbol: str
    ts_ms: int
    bids: tuple[tuple[float, float], ...]
    asks: tuple[tuple[float, float], ...]


def _f(value: object, name: str) -> float:
    try:
        return _strict_float(value, name)
    except ValidationError as exc:
        raise BookError(str(exc)) from exc


def _pos_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BookError(f"{name} must be a positive int")
    return value


def _sym(value: object) -> str:
    if not isinstance(value, str):
        raise BookError("symbol must be str")
    try:
        return validate_symbol(value)
    except ValidationError as exc:
        raise BookError(str(exc)) from exc


def _levels(value: object, name: str) -> tuple[tuple[float, float], ...]:
    if not isinstance(value, list) or not value:
        raise BookError(f"{name} must be a non-empty list")
    out: list[tuple[float, float]] = []
    for i, level in enumerate(value):
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise BookError(f"{name}[{i}] must be a [price, qty] pair")
        price = _f(level[0], f"{name}[{i}].price")
        qty = _f(level[1], f"{name}[{i}].qty")
        if price <= 0 or qty < 0:
            raise BookError(f"{name}[{i}] needs price > 0 and qty >= 0")
        out.append((price, qty))
    return tuple(out)


def _ts(data: dict) -> int:
    for key in ("T", "E"):
        value = data.get(key)
        if value is not None:
            return _pos_int(value, key)
    raise BookError("frame missing T/E timestamp")


def parse_book_ticker_payload(data: dict) -> BookTicker:
    """Parse a bookTicker event body (best bid/ask with quantities)."""
    bid = _f(data.get("b"), "b")
    ask = _f(data.get("a"), "a")
    if bid <= 0 or ask <= 0 or bid >= ask:
        raise BookError(f"invalid bookTicker quotes: bid={bid} ask={ask}")
    return BookTicker(
        symbol=_sym(data.get("s")),
        bid=bid,
        bid_qty=_f(data.get("B"), "B"),
        ask=ask,
        ask_qty=_f(data.get("A"), "A"),
        ts_ms=_ts(data),
    )


def parse_depth_payload(data: dict) -> DepthSnapshot:
    """Parse a partial-book depth event body (top-N levels)."""
    bids = _levels(data.get("b"), "b")
    asks = _levels(data.get("a"), "a")
    if max(p for p, _ in bids) >= min(p for p, _ in asks):
        raise BookError("crossed book: best bid >= best ask")
    return DepthSnapshot(
        symbol=_sym(data.get("s")), ts_ms=_ts(data), bids=bids, asks=asks
    )


def parse_book_message(payload: object):
    """Parse one combined-stream frame. Returns BookTicker, DepthSnapshot or None.

    None covers subscription acks and frames without a routed event.
    """
    if not isinstance(payload, dict):
        raise BookError("frame must be a dict")
    data = payload.get("data")
    if data is None:
        if "result" in payload:  # subscribe ack
            return None
        raise BookError("frame missing 'data'")
    if not isinstance(data, dict):
        raise BookError("frame 'data' must be a dict")
    event = data.get("e")
    if event == "bookTicker":
        return parse_book_ticker_payload(data)
    if event == "depthUpdate":
        return parse_depth_payload(data)
    return None


def band_depth(
    snapshot: DepthSnapshot, pct: float
) -> tuple[float, float, float | None]:
    """Quote notional within +-pct% of mid on each side (top-N lower bound).

    Returns (bid_notional, ask_notional, imbalance). Imbalance is None when
    both sides are empty.
    """
    if isinstance(pct, bool) or not isinstance(pct, (int, float)) or pct <= 0:
        raise BookError("band pct must be a positive number")
    best_bid = max(p for p, _ in snapshot.bids)
    best_ask = min(p for p, _ in snapshot.asks)
    mid = (best_bid + best_ask) / 2.0
    low = mid * (1.0 - pct / 100.0)
    high = mid * (1.0 + pct / 100.0)
    bid_n = sum(p * q for p, q in snapshot.bids if p >= low)
    ask_n = sum(p * q for p, q in snapshot.asks if p <= high)
    total = bid_n + ask_n
    imbal = (bid_n - ask_n) / total if total > 0 else None
    return bid_n, ask_n, imbal


def depth_features(snapshot: DepthSnapshot, ticker: BookTicker | None = None) -> dict:
    """Flatten one depth snapshot into a fixed-schema feature row."""
    if not isinstance(snapshot, DepthSnapshot):
        raise BookError("depth_features expects a DepthSnapshot")
    if ticker is not None and not isinstance(ticker, BookTicker):
        raise BookError("ticker must be a BookTicker or None")
    bids, asks = snapshot.bids, snapshot.asks
    best_bid, bid0_qty = max(bids, key=lambda lv: lv[0])
    best_ask, ask0_qty = min(asks, key=lambda lv: lv[0])
    mid = (best_bid + best_ask) / 2.0
    spread = best_ask - best_bid

    row: dict = {
        "ts_ms": snapshot.ts_ms,
        "mid": mid,
        "spread_abs": spread,
        "spread_bps": spread / mid * 10_000.0,
        "bid0": best_bid,
        "ask0": best_ask,
        "bid0_qty": bid0_qty,
        "ask0_qty": ask0_qty,
    }
    for pct, key in BANDS:
        bid_n, ask_n, imbal = band_depth(snapshot, pct)
        row[f"d{key}_bid"] = bid_n
        row[f"d{key}_ask"] = ask_n
        row[f"imbal{key}"] = imbal
    if ticker is not None and ticker.symbol == snapshot.symbol:
        row["tb_bid"] = ticker.bid
        row["tb_ask"] = ticker.ask
        row["tb_bid_qty"] = ticker.bid_qty
        row["tb_ask_qty"] = ticker.ask_qty
    else:
        row["tb_bid"] = row["tb_ask"] = row["tb_bid_qty"] = row["tb_ask_qty"] = None
    return row


def _schema() -> pa.Schema:
    types = {"int64": pa.int64(), "float64": pa.float64()}
    return pa.schema([(name, types[kind]) for name, kind in BOOK_FIELDS])


def _normalize(row: object) -> dict:
    if not isinstance(row, dict):
        raise BookError("book row must be a dict")
    fields = {name for name, _ in BOOK_FIELDS}
    unknown = set(row) - fields
    if unknown:
        raise BookError(f"unknown book columns: {sorted(unknown)}")
    out: dict = {}
    for name, kind in BOOK_FIELDS:
        value = row.get(name)
        if value is None:
            if kind == "int64":
                raise BookError(f"{name} is required")
            out[name] = None
            continue
        if kind == "int64":
            out[name] = _pos_int(value, name)
        else:
            out[name] = _f(value, name)
    return out


def merge_book_rows(existing: list[dict], new: list[dict]) -> list[dict]:
    """Dedup rows by ts_ms (new wins), sorted ascending."""
    merged: dict[int, dict] = {}
    for rows in (existing, new):
        for row in rows:
            norm = _normalize(row)
            merged[norm["ts_ms"]] = norm
    return [merged[k] for k in sorted(merged)]


class BookStore:
    """Per-symbol Parquet book-feature store with atomic upserts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, symbol: str) -> Path:
        try:
            sym = validate_symbol(symbol)
        except ValidationError as exc:
            raise BookError(str(exc)) from exc
        return self.root / f"{sym}.parquet"

    def load(self, symbol: str) -> list[dict]:
        path = self.path_for(symbol)
        if not path.exists():
            return []
        return pq.read_table(path).to_pylist()

    def last_ts_ms(self, symbol: str) -> int | None:
        rows = self.load(symbol)
        return max((row["ts_ms"] for row in rows), default=None)

    def upsert(self, symbol: str, rows: list[dict]) -> int:
        """Merge rows and write atomically. Returns count of NEW ts_ms keys."""
        if not isinstance(rows, list):
            raise BookError("rows must be a list")
        path = self.path_for(symbol)
        existing = self.load(symbol)
        merged = merge_book_rows(existing, rows)
        table = pa.Table.from_pylist(merged, schema=_schema())
        tmp = path.with_name(path.name + ".tmp")
        pq.write_table(table, tmp)
        os.replace(tmp, path)
        return len(merged) - len(existing)
