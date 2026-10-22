"""Persistent assignment list for the SNIPER V3 observation engine.

Assignments are user intent only: they tell the monitor which symbols to
evaluate on closed 5m bars. They never create orders or journal SIGNAL rows.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SYMBOL_RE = re.compile(r"^[A-Z0-9]{4,20}$")
MAX_ASSIGNMENTS = 10


class AssignmentError(RuntimeError):
    """Invalid SNIPER assignment."""


class AssignmentFullError(AssignmentError):
    """SNIPER watchlist capacity reached."""


@dataclass(frozen=True)
class Assignment:
    symbol: str
    assigned_ms: int

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "assigned_ms": self.assigned_ms}


def normalize_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise AssignmentError("symbol must be a string")
    sym = symbol.strip().upper()
    if not SYMBOL_RE.fullmatch(sym):
        raise AssignmentError("symbol must match ^[A-Z0-9]{4,20}$")
    return sym


class AssignmentStore:
    """Atomic JSON-backed list; safe for the FastAPI event loop and scripts."""

    def __init__(self, path: Path, *, max_assignments: int = MAX_ASSIGNMENTS) -> None:
        self.path = Path(path)
        self.max_assignments = max_assignments
        self._lock = threading.RLock()

    def _read(self) -> list[Assignment]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = payload.get("assignments") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        out: list[Assignment] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = row.get("symbol")
            ts = row.get("assigned_ms")
            if not isinstance(sym, str):
                continue
            try:
                sym = normalize_symbol(sym)
            except AssignmentError:
                continue
            if sym in seen:
                continue
            seen.add(sym)
            out.append(Assignment(sym, ts if isinstance(ts, int) else int(time.time() * 1000)))
        return out[: self.max_assignments]

    def _write(self, rows: list[Assignment]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_ms": int(time.time() * 1000),
            "assignments": [row.to_dict() for row in rows],
        }
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def assignments(self) -> list[Assignment]:
        with self._lock:
            return self._read()

    def symbols(self) -> list[str]:
        return [row.symbol for row in self.assignments()]

    def add(self, symbol: str) -> tuple[Assignment, bool]:
        sym = normalize_symbol(symbol)
        with self._lock:
            rows = self._read()
            for row in rows:
                if row.symbol == sym:
                    return row, False
            if len(rows) >= self.max_assignments:
                raise AssignmentFullError(
                    f"SNIPER watchlist is full ({self.max_assignments} symbols)"
                )
            row = Assignment(sym, int(time.time() * 1000))
            rows.append(row)
            self._write(rows)
            return row, True

    def remove(self, symbol: str) -> bool:
        sym = normalize_symbol(symbol)
        with self._lock:
            rows = self._read()
            kept = [row for row in rows if row.symbol != sym]
            if len(kept) == len(rows):
                return False
            self._write(kept)
            return True
