"""Append-only evaluation journal for SNIPER V3.

`monitor.py` used to overwrite a single snapshot every five minutes, so a setup
that fired at 10:00 was gone by 10:05 and "the engine never fires" could not be
verified either way. This module keeps every evaluation forever, so the gate
funnel and the setup rate are measurable.

Two append-only JSONL files per directory:

- ``evals.jsonl``    one row per evaluation, including every diagnostic metric;
- ``outcomes.jsonl`` forward returns for rows old enough to score, keyed by
  ``(symbol, as_of_ms)`` and written at most once.

Nothing here is a signal or an order. It is measurement.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable

BAR_MS = 300_000


def _flatten(metrics: dict[str, Any]) -> dict[str, Any]:
    """Keep the row flat JSON so parquet/pandas joins stay trivial."""
    out: dict[str, Any] = {}
    for key, value in metrics.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
    return out


class EvalLog:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.evals_path = self.directory / "evals.jsonl"
        self.outcomes_path = self.directory / "outcomes.jsonl"
        self._scored: set[tuple[str, int]] = set()
        self._loaded = False
        self._pending: dict[str, list[dict[str, Any]]] | None = None
        self._funnel_cache: tuple[int, dict[str, Any]] | None = None

    # -- writes ---------------------------------------------------------
    def record(self, row: dict[str, Any]) -> None:
        """Append one evaluation. Never rewrites existing rows."""
        if not row.get("symbol") or row.get("as_of_ms") is None:
            return
        line = json.dumps(row, separators=(",", ":"), sort_keys=True)
        with self.evals_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self._pending is not None:
            self._pending.setdefault(str(row["symbol"]), []).append(row)

    def _load_scored(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.outcomes_path.exists():
            return
        for row in _iter_jsonl(self.outcomes_path):
            key = (str(row.get("symbol")), int(row.get("as_of_ms", 0)))
            self._scored.add(key)

    def record_outcomes(
        self,
        symbol: str,
        klines_5m: list[Any],
        *,
        horizons: tuple[int, int] = (3, 12),
    ) -> int:
        """Backfill forward returns for this symbol's unscored evaluations.

        `klines_5m` is the same closed snapshot the evaluation used, so the
        returns are computed from bars that are already in the past. Rows whose
        horizon has not elapsed yet are simply skipped and retried next sweep.
        """
        if not klines_5m:
            return 0
        self._load_scored()
        by_time = {k.close_time: i for i, k in enumerate(klines_5m)}
        short, long_ = horizons
        written = 0
        for row in self._pending_for(symbol):
            as_of = int(row["as_of_ms"])
            key = (symbol, as_of)
            if key in self._scored:
                continue
            idx = by_time.get(as_of)
            if idx is None or idx + long_ >= len(klines_5m):
                continue
            base = klines_5m[idx].close
            if base <= 0:
                continue
            direction = row.get("direction")
            sign = 1.0 if direction == "long" else -1.0 if direction == "short" else 0.0
            payload: dict[str, Any] = {
                "symbol": symbol,
                "as_of_ms": as_of,
                "direction": direction,
                "status": row.get("status"),
                "failed_gate": row.get("failed_gate"),
                "scored_ms": int(time.time() * 1000),
            }
            for bars in (short, long_):
                fwd_bps = (klines_5m[idx + bars].close / base - 1.0) * 10_000.0
                payload[f"fwd_bps_{bars}"] = fwd_bps
                payload[f"dir_fwd_bps_{bars}"] = fwd_bps * sign
            with self.outcomes_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n")
            self._scored.add(key)
            written += 1
        return written

    def _pending_for(self, symbol: str) -> list[dict[str, Any]]:
        """Rows still awaiting an outcome, scanned once and kept in memory."""
        if self._pending is None:
            self._pending = {}
            for row in _iter_jsonl(self.evals_path):
                key = row.get("symbol")
                if key:
                    self._pending.setdefault(str(key), []).append(row)
        return self._pending.get(symbol, [])

    # -- reads ----------------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        return list(_iter_jsonl(self.evals_path))

    def outcomes(self) -> list[dict[str, Any]]:
        return list(_iter_jsonl(self.outcomes_path))

    def funnel(self) -> dict[str, Any]:
        """Gate histogram: which gate killed each evaluation, and how often."""
        size = self.evals_path.stat().st_size if self.evals_path.exists() else 0
        if self._funnel_cache and self._funnel_cache[0] == size:
            return self._funnel_cache[1]
        rows = self.rows()
        gates: dict[str, int] = {}
        for row in rows:
            key = str(row.get("failed_gate") or "setup")
            gates[key] = gates.get(key, 0) + 1
        total = len(rows)
        reach: dict[str, int] = {}
        for row in rows:
            for check in row.get("checks") or ():
                if check.get("state") == "pass":
                    reach[check.get("key", "?")] = reach.get(check.get("key", "?"), 0) + 1
        setups = gates.pop("setup", 0)
        out = {
            "n": total,
            "setups": setups,
            "setup_rate": (setups / total) if total else None,
            "failed_gate": dict(sorted(gates.items(), key=lambda kv: -kv[1])),
            "reached_gate": reach,
        }
        self._funnel_cache = (size, out)
        return out


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
