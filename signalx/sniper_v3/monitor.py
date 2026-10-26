"""Background monitor for user-assigned SNIPER V3 symbols.

The monitor evaluates the watchlist on closed 5m boundaries and writes the
latest snapshots to `data/sniper/latest.json`. It is observation-only and never
places orders or writes legacy journal SIGNAL rows.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx

from signalx.feeds import FeedError
from signalx.sniper_v3.data import (
    fetch_and_evaluate_detailed,
    funding_percentile,
    oi_crowding,
)
from signalx.sniper_v3.evallog import EvalLog
from signalx.sniper_v3.primitives import SniperError
from signalx.validation import ValidationError

from .assignments import AssignmentStore

BAR_MS = 300_000
SETTLE_MS = 8_000


def _now_ms() -> int:
    return int(time.time() * 1000)


def _eval_row(symbol: str, row: dict, diagnostics: object) -> dict:
    """Flatten one monitor row into an append-only evaluation record."""
    diag = row.get("diagnostics") or {}
    metrics = diag.get("metrics") or {}
    decision = row.get("decision") or {}
    context = row.get("context") or {}
    checks = [
        {"key": c.get("key"), "state": c.get("state"), "detail": c.get("detail")}
        for c in (diag.get("checks") or [])
    ]
    out: dict = {
        "symbol": symbol,
        "as_of_ms": row.get("as_of_ms"),
        "evaluated_ms": row.get("evaluated_ms"),
        "status": row.get("status"),
        "failed_gate": diag.get("failed_gate"),
        "reason": diag.get("reason"),
        "direction": diag.get("direction"),
        "last_close": row.get("last_close"),
        "checks": checks,
        "confidence": metrics.get("confidence"),
        "expected_value_r": metrics.get("expected_value_r"),
        "setup_score": metrics.get("setup_score"),
        "rr": metrics.get("rr"),
        "entry": decision.get("entry"),
        "stop": decision.get("stop"),
        "tp1": decision.get("tp1"),
        "tp2": decision.get("tp2"),
        "funding_rate": context.get("funding_rate"),
        "funding_pctile": context.get("funding_pctile"),
        "oi_crowding": context.get("oi_crowding"),
    }
    for key, value in metrics.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out.setdefault(key, value)
    return out


class SniperMonitor:
    def __init__(
        self,
        client: httpx.AsyncClient,
        store: AssignmentStore,
        results_path: Path,
        *,
        interval_s: float = 300.0,
        workers: int = 3,
    ) -> None:
        self.client = client
        self.store = store
        self.results_path = Path(results_path)
        self.interval_s = max(30.0, interval_s)
        self.workers = max(1, min(workers, 8))
        self.eval_log = EvalLog(self.results_path.parent / "evals")
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._results: dict[str, dict] = {}
        self._heartbeat_ms: int | None = None
        self._evaluating = False
        self._last_error: str | None = None

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="sniper-v3-monitor")
        return self._task

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.evaluate_all()
            except Exception as exc:  # keep the monitor alive; expose the error
                self._last_error = f"{exc.__class__.__name__}: {exc}"
            if self._stop.is_set():
                break
            await self._sleep()

    async def _sleep(self) -> None:
        now = _now_ms()
        next_close = (now // BAR_MS + 1) * BAR_MS + SETTLE_MS
        timeout = max(self.interval_s, (next_close - now) / 1000.0)
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self._wake.clear()

    async def evaluate_all(self) -> None:
        if self._evaluating:
            return
        self._evaluating = True
        try:
            symbols = self.store.symbols()
            sem = asyncio.Semaphore(self.workers)

            async def one(symbol: str) -> tuple[str, dict]:
                async with sem:
                    try:
                        inputs, decision, diagnostics = await fetch_and_evaluate_detailed(
                            self.client, symbol
                        )
                    except (FeedError, SniperError, ValidationError) as exc:
                        return symbol, {
                            "symbol": symbol,
                            "status": "error",
                            "evaluated_ms": _now_ms(),
                            "error": str(exc),
                        }
                    except Exception as exc:  # one bad symbol must not kill the sweep
                        return symbol, {
                            "symbol": symbol,
                            "status": "error",
                            "evaluated_ms": _now_ms(),
                            "error": f"{exc.__class__.__name__}: {exc}",
                        }
                    last = inputs.klines_5m[-1] if inputs.klines_5m else None
                    row = {
                        "symbol": symbol,
                        "status": "setup" if decision is not None else "no_setup",
                        "evaluated_ms": _now_ms(),
                        "as_of_ms": last.close_time if last is not None else None,
                        "last_close": last.close if last is not None else None,
                        "decision": decision.to_dict() if decision is not None else None,
                        "diagnostics": diagnostics.to_dict(),
                        "context": {
                            "funding_rate": inputs.funding_rate,
                            "funding_pctile": funding_percentile(inputs),
                            "oi_now": inputs.oi_now,
                            "oi_crowding": oi_crowding(inputs),
                        },
                    }
                    try:
                        self.eval_log.record(
                            _eval_row(symbol, row, diagnostics)
                        )
                        self.eval_log.record_outcomes(symbol, inputs.klines_5m)
                    except OSError:
                        # A full disk must not stop the observation loop.
                        pass
                    return symbol, row

            rows = await asyncio.gather(*(one(symbol) for symbol in symbols))
            self._results = {symbol: row for symbol, row in rows}
            self._heartbeat_ms = _now_ms()
            self._last_error = None
            self._write()
        finally:
            self._evaluating = False

    def _write(self) -> None:
        self.results_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_ms": self._heartbeat_ms,
            "interval_s": self.interval_s,
            "results": self._results,
            "last_error": self._last_error,
        }
        tmp = self.results_path.with_name(self.results_path.name + ".tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        tmp.replace(self.results_path)

    def snapshot(self) -> dict:
        return {
            "evaluating": self._evaluating,
            "heartbeat_ms": self._heartbeat_ms,
            "symbols": self.store.symbols(),
            "results": {k: dict(v) for k, v in self._results.items()},
            "last_error": self._last_error,
            "funnel": self.eval_log.funnel(),
        }

    def forget(self, symbol: str) -> None:
        sym = symbol.upper()
        if sym in self._results:
            self._results.pop(sym, None)
            try:
                self._write()
            except OSError:
                pass

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
