"""Day-partitioned Parquet signal journal for sentinel observations.

Single-writer assumption (one sentinel process). Each append rewrites the
affected day file atomically (tmp + os.replace). Rows are normalized against a
fixed schema; unknown keys raise JournalError (fail loud, no silent drops).
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

COLUMNS: tuple[tuple[str, str], ...] = (
    ("ts_ms", "int64"),
    ("symbol", "string"),
    ("event", "string"),
    ("state", "string"),
    ("prev_state", "string"),
    ("price", "float64"),
    ("score", "float64"),
    ("entry", "float64"),
    ("stop", "float64"),
    ("tp1", "float64"),
    ("tp2", "float64"),
    ("reasons", "string"),
    ("features_json", "string"),
)

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class JournalError(RuntimeError):
    """Malformed journal input. Never swallow this."""


def _arrow_type(kind: str) -> pa.DataType:
    if kind == "int64":
        return pa.int64()
    if kind == "float64":
        return pa.float64()
    return pa.string()


def _schema() -> pa.Schema:
    return pa.schema([(name, _arrow_type(kind)) for name, kind in COLUMNS])


def day_str(ts_ms: int) -> str:
    """UTC day partition key for a millisecond timestamp."""
    if isinstance(ts_ms, bool) or not isinstance(ts_ms, int) or ts_ms <= 0:
        raise JournalError("ts_ms must be a positive int")
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _normalize(row: object) -> dict:
    if not isinstance(row, dict):
        raise JournalError("journal row must be a dict")
    unknown = set(row) - {name for name, _ in COLUMNS}
    if unknown:
        raise JournalError(f"unknown journal columns: {sorted(unknown)}")
    out: dict = {}
    for name, kind in COLUMNS:
        value = row.get(name)
        if value is None:
            out[name] = None
            continue
        if kind == "int64":
            if isinstance(value, bool) or not isinstance(value, int):
                raise JournalError(f"{name} must be int, got {type(value).__name__}")
        elif kind == "float64":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise JournalError(f"{name} must be a number, got {type(value).__name__}")
            value = float(value)
        else:
            if not isinstance(value, str):
                raise JournalError(f"{name} must be str, got {type(value).__name__}")
        out[name] = value
    if out["ts_ms"] is None:
        raise JournalError("ts_ms is required")
    return out


class SignalJournal:
    """Append-only, day-partitioned Parquet journal rooted at ``root``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def day_file(self, day: str) -> Path:
        if not isinstance(day, str) or not _DAY_RE.match(day):
            raise JournalError(f"bad day partition: {day!r}")
        return self.root / f"{day}.parquet"

    def append(self, rows: list[dict]) -> int:
        """Normalize + append rows, grouped into day partitions. Returns count."""
        if not isinstance(rows, list):
            raise JournalError("rows must be a list")
        normalized = [_normalize(r) for r in rows]
        by_day: dict[str, list[dict]] = {}
        for row in normalized:
            by_day.setdefault(day_str(row["ts_ms"]), []).append(row)
        for day in sorted(by_day):
            path = self.day_file(day)
            tables = [pq.read_table(path)] if path.exists() else []
            tables.append(pa.Table.from_pylist(by_day[day], schema=_schema()))
            table = pa.concat_tables(tables) if len(tables) > 1 else tables[0]
            tmp = path.with_name(path.name + ".tmp")
            pq.write_table(table, tmp)
            os.replace(tmp, path)
        return len(normalized)

    def read_all(self) -> list[dict]:
        """All rows across partitions, partition order = chronological."""
        out: list[dict] = []
        for path in sorted(self.root.glob("*.parquet")):
            out.extend(pq.read_table(path).to_pylist())
        return out

    def count(self) -> int:
        total = 0
        for path in sorted(self.root.glob("*.parquet")):
            total += pq.read_metadata(path).num_rows
        return total
