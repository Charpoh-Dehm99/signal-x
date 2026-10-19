"""Live recorder core: gap math, backfill planning, atomic per-symbol store.

Pure helpers + a small Parquet store used by scripts/recorder.py. Single
writer per symbol file assumed. Fail loud: malformed inputs raise
RecorderError.

Store layout: <root>/<SYMBOL>.parquet with the Kline column set, deduped on
open_time, sorted ascending.
"""

from __future__ import annotations

import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .models import Kline
from .validation import ValidationError, validate_symbol

KLINE_FIELDS: tuple[tuple[str, str], ...] = (
    ("open_time", "int64"),
    ("open", "float64"),
    ("high", "float64"),
    ("low", "float64"),
    ("close", "float64"),
    ("volume", "float64"),
    ("close_time", "int64"),
    ("quote_volume", "float64"),
    ("trades", "int64"),
    ("taker_buy_base", "float64"),
    ("taker_buy_quote", "float64"),
)


class RecorderError(RuntimeError):
    """Malformed recorder input. Never swallow this."""


def _pos_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RecorderError(f"{name} must be a positive int")
    return value


def gap_bars(last_stored_ms: int, first_fetched_ms: int, step_ms: int) -> int:
    """Missing bar count between the last stored open time and the first fetched."""
    _pos_int(last_stored_ms, "last_stored_ms")
    _pos_int(first_fetched_ms, "first_fetched_ms")
    _pos_int(step_ms, "step_ms")
    missing = (first_fetched_ms - last_stored_ms - step_ms) // step_ms
    return max(0, missing)


def plan_backfill(
    last_stored_ms: int | None, now_ms: int, step_ms: int, *, max_bars: int
) -> dict | None:
    """Bounded backfill window from after the last stored bar up to now.

    Returns {"start_ms", "end_ms", "limit"} or None when nothing is missing.
    When more than max_bars are missing, keeps the most recent max_bars.
    """
    _pos_int(now_ms, "now_ms")
    _pos_int(step_ms, "step_ms")
    _pos_int(max_bars, "max_bars")
    if last_stored_ms is None:
        start = now_ms - max_bars * step_ms
        bars = max_bars
    else:
        _pos_int(last_stored_ms, "last_stored_ms")
        start = last_stored_ms + step_ms
        bars = (now_ms - start) // step_ms
    if bars <= 0:
        return None
    if bars > max_bars:
        start = now_ms - max_bars * step_ms
        bars = max_bars
    return {"start_ms": start, "end_ms": now_ms, "limit": bars}


def merge_kline_rows(existing: list[dict], new: list[dict]) -> list[dict]:
    """Dedup rows by open_time (new wins), sorted ascending."""
    merged: dict[int, dict] = {}
    for rows in (existing, new):
        for row in rows:
            if not isinstance(row, dict) or "open_time" not in row:
                raise RecorderError("kline row must be a dict with open_time")
            _pos_int(row["open_time"], "open_time")
            merged[row["open_time"]] = row
    return [merged[k] for k in sorted(merged)]


def kline_to_row(k: Kline) -> dict:
    if not isinstance(k, Kline):
        raise RecorderError("kline_to_row expects a Kline")
    return {name: getattr(k, name) for name, _ in KLINE_FIELDS}


def is_stale(last_open_ms: int | None, now_ms: int, step_ms: int, *, factor: int = 3) -> bool:
    """True when no new bar has been stored for more than factor steps."""
    _pos_int(now_ms, "now_ms")
    _pos_int(step_ms, "step_ms")
    if last_open_ms is None:
        return True
    _pos_int(last_open_ms, "last_open_ms")
    return now_ms - last_open_ms > factor * step_ms


def _kline_schema() -> pa.Schema:
    types = {"int64": pa.int64(), "float64": pa.float64()}
    return pa.schema([(name, types[kind]) for name, kind in KLINE_FIELDS])


class KlineStore:
    """Per-symbol Parquet kline store with atomic upserts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, symbol: str) -> Path:
        try:
            sym = validate_symbol(symbol)
        except ValidationError as exc:
            raise RecorderError(str(exc)) from exc
        return self.root / f"{sym}.parquet"

    def load(self, symbol: str) -> list[dict]:
        path = self.path_for(symbol)
        if not path.exists():
            return []
        return pq.read_table(path).to_pylist()

    def last_open_ms(self, symbol: str) -> int | None:
        rows = self.load(symbol)
        return max((row["open_time"] for row in rows), default=None)

    def upsert(self, symbol: str, rows: list[dict]) -> int:
        """Merge rows and write atomically. Returns count of NEW open_times."""
        if not isinstance(rows, list):
            raise RecorderError("rows must be a list")
        path = self.path_for(symbol)
        existing = self.load(symbol)
        merged = merge_kline_rows(existing, rows)
        table = pa.Table.from_pylist(merged, schema=_kline_schema())
        tmp = path.with_name(path.name + ".tmp")
        pq.write_table(table, tmp)
        os.replace(tmp, path)
        return len(merged) - len(existing)
