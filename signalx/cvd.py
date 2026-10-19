"""AggTrade -> per-minute CVD bucket core (observation only).

Per-symbol minute buckets of taker buy/sell notional from the aggTrade
stream. Only CLOSED buckets are emitted (up to 1 min latency) — a partial
minute is never persisted, which makes parquet upserts dedupe-safe on
``ts_min`` even across daemon restarts (a minute spanning a restart is
partial; that is accepted and documented).

Rows are recomputable CVD primitives: ``delta = buy_notional - sell_notional``;
readers cumulate for CVD. A missing ``ts_min`` row means zero trades that
minute (zero-flow minutes are not persisted).

G0 KILL stands — this is instrumentation for future pre-registered
hypotheses, never a signal claim. Fail-loud: malformed frames raise CvdError.
"""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .models import AggTrade
from .validation import (
    ValidationError,
    _strict_float,
    parse_agg_trade,
    taker_direction,
    validate_symbol,
)

CVD_FIELDS: tuple[tuple[str, str], ...] = (
    ("ts_min", "int64"),
    ("buy_notional", "float64"),
    ("sell_notional", "float64"),
    ("delta", "float64"),
    ("trades", "int64"),
)

MIN_MS = 60_000


class CvdError(RuntimeError):
    """Malformed CVD input. Never swallow this."""


def _sym(value: object) -> str:
    if not isinstance(value, str):
        raise CvdError("symbol must be str")
    try:
        return validate_symbol(value)
    except ValidationError as exc:
        raise CvdError(str(exc)) from exc


def _pos_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CvdError(f"{name} must be a positive int")
    return value


def _f(value: object, name: str) -> float:
    try:
        return _strict_float(value, name)
    except ValidationError as exc:
        raise CvdError(str(exc)) from exc


def parse_agg_message(payload: object) -> tuple[str, AggTrade] | None:
    """Parse one combined-stream frame. Returns (symbol, AggTrade) or None.

    Accepts both ``aggTrade`` and ``trade`` events — they carry the same
    T/p/q/m fields and sum to identical taker notional (aggTrade is just
    trade bundling). The ``trade`` stream is used because this environment's
    aggTrade stream delivers no frames (verified 2026-10-17).

    None covers subscription acks and other routed events.
    """
    if not isinstance(payload, dict):
        raise CvdError("frame must be a dict")
    data = payload.get("data")
    if data is None:
        if "result" in payload:  # subscribe ack
            return None
        raise CvdError("frame missing 'data'")
    if not isinstance(data, dict):
        raise CvdError("frame 'data' must be a dict")
    if data.get("e") not in ("aggTrade", "trade"):
        return None
    # Binance emits occasional zero p/q administrative records (st=1, X="NA")
    # on some symbols; those are not trades — skip silently. Anything else
    # malformed stays fail-loud through the strict parser below.
    try:
        if float(data.get("p")) == 0.0 or float(data.get("q")) == 0.0:
            return None
    except (TypeError, ValueError):
        pass  # let parse_agg_trade raise the proper error
    sym = _sym(data.get("s"))
    try:
        trade = parse_agg_trade(data)
    except ValidationError as exc:
        raise CvdError(str(exc)) from exc
    return sym, trade


class MinuteBuckets:
    """Rolling per-symbol accumulator emitting CLOSED minute rows."""

    def __init__(self, symbol: str) -> None:
        self.symbol = _sym(symbol)
        self.ts_min: int | None = None
        self._buy = 0.0
        self._sell = 0.0
        self._trades = 0

    def add(self, trade: AggTrade) -> dict | None:
        """Accumulate one trade; returns the closed previous minute row.

        Returns None while the current minute is still open.
        """
        if not isinstance(trade, AggTrade):
            raise CvdError("add expects an AggTrade")
        ts = _pos_int(trade.timestamp, "trade.timestamp")
        price = _f(trade.price, "trade.price")
        qty = _f(trade.qty, "trade.qty")
        if price <= 0 or qty <= 0:
            raise CvdError("trade price/qty must be positive")
        if not isinstance(trade.is_buyer_maker, bool):
            raise CvdError("trade.is_buyer_maker must be bool")

        bucket = ts // MIN_MS * MIN_MS
        closed: dict | None = None
        if self.ts_min is None:
            self.ts_min = bucket
        elif bucket < self.ts_min:
            raise CvdError(f"out-of-order trade: {ts} < open bucket {self.ts_min}")
        elif bucket > self.ts_min:
            closed = self._close()
            self.ts_min = bucket

        notional = price * qty
        if taker_direction(trade.is_buyer_maker) > 0:
            self._buy += notional
        else:
            self._sell += notional
        self._trades += 1
        return closed

    def _close(self) -> dict:
        if self.ts_min is None:
            raise CvdError("_close called with no open bucket")
        return {
            "ts_min": self.ts_min,
            "buy_notional": self._buy,
            "sell_notional": self._sell,
            "delta": self._buy - self._sell,
            "trades": self._trades,
        }


def _schema() -> pa.Schema:
    types = {"int64": pa.int64(), "float64": pa.float64()}
    return pa.schema([(name, types[kind]) for name, kind in CVD_FIELDS])


def _normalize(row: object) -> dict:
    if not isinstance(row, dict):
        raise CvdError("cvd row must be a dict")
    fields = {name for name, _ in CVD_FIELDS}
    unknown = set(row) - fields
    if unknown:
        raise CvdError(f"unknown cvd columns: {sorted(unknown)}")
    out: dict = {}
    for name, kind in CVD_FIELDS:
        value = row.get(name)
        if value is None:
            raise CvdError(f"{name} is required")
        if kind == "int64":
            out[name] = _pos_int(value, name)
        else:
            out[name] = _f(value, name)
    return out


def merge_cvd_rows(existing: list[dict], new: list[dict]) -> list[dict]:
    """Dedup rows by ts_min (new wins), sorted ascending."""
    merged: dict[int, dict] = {}
    for rows in (existing, new):
        for row in rows:
            norm = _normalize(row)
            merged[norm["ts_min"]] = norm
    return [merged[k] for k in sorted(merged)]


class CvdStore:
    """Per-symbol Parquet CVD store with atomic upserts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, symbol: str) -> Path:
        sym = _sym(symbol)
        return self.root / f"{sym}.parquet"

    def load(self, symbol: str) -> list[dict]:
        path = self.path_for(symbol)
        if not path.exists():
            return []
        return pq.read_table(path).to_pylist()

    def last_ts_min(self, symbol: str) -> int | None:
        rows = self.load(symbol)
        return max((row["ts_min"] for row in rows), default=None)

    def upsert(self, symbol: str, rows: list[dict]) -> int:
        """Merge rows and write atomically. Returns count of NEW ts_min keys."""
        if not isinstance(rows, list):
            raise CvdError("rows must be a list")
        path = self.path_for(symbol)
        existing = self.load(symbol)
        merged = merge_cvd_rows(existing, rows)
        table = pa.Table.from_pylist(merged, schema=_schema())
        tmp = path.with_name(path.name + ".tmp")
        pq.write_table(table, tmp)
        os.replace(tmp, path)
        return len(merged) - len(existing)
