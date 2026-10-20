#!/usr/bin/env python3
"""Universe sentinel: sweep top gainers, run the per-symbol state machine,
journal transitions/signals, optionally alert on Telegram.

Observation only: the Phase 1 G0 event study did NOT validate a short edge
(reports/phase1/G0_VERDICT.md). Sentinel output is an audited observation,
never an executed order.

Usage (from repo root):
  .venv/bin/python scripts/sentinel.py --once
  .venv/bin/python scripts/sentinel.py --top 40 --interval 60
  .venv/bin/python scripts/sentinel.py --symbols AGTUSDT,AUSDT --alert
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signalx.alerts import format_signal, load_alert_env, send_alert  # noqa: E402
from signalx.feeds import (  # noqa: E402
    FeedError,
    TF_MS,
    fetch_gainers,
    make_client,
    poll_snapshot,
)
from signalx.journal import SignalJournal  # noqa: E402
from signalx.live import LiveError  # noqa: E402
from signalx.sentinel import (  # noqa: E402
    SentinelError,
    SymbolTracker,
    journal_rows,
    latest_journal_states,
    record_to_signal,
    restore_state,
)
from signalx.validation import ValidationError, validate_interval, validate_symbol  # noqa: E402

MIN_INTERVAL_S = 30.0
EVICT_AFTER_MS = 6 * 3600 * 1000
QUIET_STATES = ("IDLE",)


def _ts() -> str:
    return time.strftime("%H:%M:%S", time.gmtime())


def _write_heartbeat(path: Path, payload: dict) -> None:
    """Atomically publish the sweep heartbeat for the dashboard."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--top", type=int, default=40, help="top gainers tracked [1..500] (default 40)")
    ap.add_argument("--symbols", default="", help="extra symbols to track, comma separated")
    ap.add_argument("--tf", default="5m", help="kline timeframe (default 5m)")
    ap.add_argument("--interval", type=float, default=60.0,
                    help=f"seconds between sweeps (>= {MIN_INTERVAL_S:.0f})")
    ap.add_argument("--workers", type=int, default=6, help="concurrent symbol polls [1..32]")
    ap.add_argument("--min-volume", type=float, default=1_000_000.0,
                    help="min 24h quote volume for gainers")
    ap.add_argument("--journal", default=str(ROOT / "data" / "journal"),
                    help="journal directory (day-partitioned parquet)")
    ap.add_argument("--heartbeat", default=str(ROOT / "data" / "sentinel_heartbeat.json"),
                    help="sweep heartbeat JSON updated every loop (dashboard freshness)")
    ap.add_argument("--pump-threshold", type=float, default=0.6,
                    help="state machine pump_score threshold (0, 1]")
    ap.add_argument("--once", action="store_true", help="single sweep, then exit")
    ap.add_argument("--alert", action="store_true",
                    help="Telegram alert on TRIGGERED (needs TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")
    return ap.parse_args(argv)


async def _one(client, symbol: str, tf: str, now_ms: int, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            return await poll_snapshot(client, symbol, "futures", tf, now_ms)
        except (FeedError, ValidationError, LiveError) as exc:
            return {"symbol": symbol, "error": str(exc)}


async def sweep(
    client,
    symbols: list[str],
    args,
    tf: str,
    trackers: dict,
    journal: SignalJournal,
    creds: dict | None,
    state_hints: dict,
    bar_ms: int,
) -> tuple[int, int]:
    now_ms = int(time.time() * 1000)
    sem = asyncio.Semaphore(args.workers)
    snaps = await asyncio.gather(*(_one(client, s, tf, now_ms, sem) for s in symbols))

    rows: list[dict] = []
    active = 0
    for snap in snaps:
        sym = snap["symbol"]
        if "error" in snap:
            print(f"[{_ts()}] {sym}: ERROR {snap['error']}")
            continue
        tracker = trackers.get(sym)
        if tracker is None:
            tracker = SymbolTracker(
                sym, pump_threshold=args.pump_threshold, bar_ms=bar_ms
            )
            hint = state_hints.get(sym)
            if hint is not None and hint["state"] != "IDLE":
                restore_state(
                    tracker, hint["state"],
                    ts_ms=hint["ts_ms"], bar_ms=bar_ms, now_ms=now_ms,
                )
                print(f"[{_ts()}] {sym}: rehydrated {hint['state']} from journal")
            trackers[sym] = tracker
        try:
            result = tracker.update(snap, now_ms=now_ms)
        except SentinelError as exc:
            print(f"[{_ts()}] {sym}: ERROR {exc}")
            continue
        new_rows = journal_rows(result)
        rows.extend(new_rows)
        for row in new_rows:
            state_hints[row["symbol"]] = {"state": row["state"], "ts_ms": row["ts_ms"]}
        if result["state"] not in QUIET_STATES:
            active += 1
        if result["transitioned"]:
            chg = snap.get("change_24h_pct")
            chg_txt = f"{chg:+.1f}%" if isinstance(chg, (int, float)) else "n/a"
            print(f"[{_ts()}] {sym} {result['previous']} -> {result['state']} ({chg_txt})")
        sig = result["signal"]
        if sig is not None:
            plan = result["plan"]
            print(
                f"[{_ts()}] *** SIGNAL {sym} SHORT entry {sig.entry:.8g} stop {sig.stop:.8g} "
                f"tp1 {sig.tp1:.8g} tp2 {sig.tp2:.8g} rr1 {plan['rr1']:.2f} score {sig.score:.2f}"
            )
            print(f"          why: {'; '.join(sig.reasons) if sig.reasons else '-'}")
            if creds:
                try:
                    send_alert(
                        format_signal(record_to_signal(sig)),
                        creds["TELEGRAM_BOT_TOKEN"],
                        creds["TELEGRAM_CHAT_ID"],
                    )
                    print(f"[{_ts()}] alert sent for {sym}")
                except RuntimeError as exc:
                    print(f"[{_ts()}] WARNING: alert failed: {exc}")
    if rows:
        journal.append(rows)
    return len(rows), active


async def run(args, extra_symbols: list[str], creds: dict | None) -> int:
    journal = SignalJournal(args.journal)
    heartbeat_path = Path(args.heartbeat)
    trackers: dict[str, SymbolTracker] = {}
    print(f"journal: {args.journal} ({journal.count()} rows)")
    print(f"heartbeat: {heartbeat_path}")
    state_hints = latest_journal_states(journal.read_all())
    bar_ms = TF_MS[args.tf]
    non_idle = {s: h for s, h in state_hints.items() if h["state"] != "IDLE"}
    if non_idle:
        summary = ", ".join(f"{s}={h['state']}" for s, h in sorted(non_idle.items()))
        print(f"rehydration hints: {len(non_idle)} non-IDLE ({summary})")
    warned: set[str] = set()
    async with make_client() as client:
        while True:
            now_ms = int(time.time() * 1000)
            symbols: list[str] = []
            try:
                gainers = await fetch_gainers(
                    client, top=args.top, min_quote_volume=args.min_volume
                )
                symbols = [g["symbol"] for g in gainers]
            except FeedError as exc:
                print(f"[{_ts()}] gainers ERROR: {exc}")
            for s in extra_symbols:
                if s not in symbols:
                    symbols.append(s)
            clean: list[str] = []
            for s in symbols:
                try:
                    validate_symbol(s)
                except ValidationError as exc:
                    if s not in warned:
                        print(f"[{_ts()}] skipping non-conforming symbol {s!r}: {exc}")
                        warned.add(s)
                    continue
                clean.append(s)
            symbols = clean
            rows = 0
            active = 0
            if not symbols:
                print(f"[{_ts()}] no symbols this sweep")
            else:
                rows, active = await sweep(
                    client, symbols, args, args.tf, trackers, journal, creds,
                    state_hints, bar_ms,
                )
                for sym in list(trackers):
                    t = trackers[sym]
                    if sym not in symbols and t.last_seen_ms and now_ms - t.last_seen_ms > EVICT_AFTER_MS:
                        del trackers[sym]
                print(
                    f"[{_ts()}] sweep: {len(symbols)} symbols, {active} active, "
                    f"{rows} journal rows (journal total {journal.count()})"
                )
            _write_heartbeat(heartbeat_path, {
                "ts_ms": int(time.time() * 1000),
                "tf": args.tf,
                "interval_s": args.interval,
                "symbols": len(symbols),
                "active": active,
                "rows": rows,
                "journal_rows": journal.count(),
            })
            if args.once:
                return 0
            await asyncio.sleep(max(args.interval, MIN_INTERVAL_S))


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if not (1 <= args.top <= 500):
            raise ValidationError("--top must be in [1..500]")
        if not (1 <= args.workers <= 32):
            raise ValidationError("--workers must be in [1..32]")
        if args.interval < MIN_INTERVAL_S:
            raise ValidationError(f"--interval must be >= {MIN_INTERVAL_S:.0f}s")
        if not 0.0 < args.pump_threshold <= 1.0:
            raise ValidationError("--pump-threshold must be in (0, 1]")
        tf = validate_interval(args.tf)
        if tf not in TF_MS:
            raise ValidationError(f"timeframe not supported by sentinel: {tf}")
        extra = [validate_symbol(s.strip().upper()) for s in args.symbols.split(",") if s.strip()]
    except ValidationError as exc:
        print(f"ERROR: {exc}")
        return 2

    creds = None
    if args.alert:
        try:
            creds = load_alert_env()
        except RuntimeError as exc:
            print(f"WARNING: alerts disabled: {exc}")

    try:
        return asyncio.run(run(args, extra, creds))
    except KeyboardInterrupt:
        print("\nstopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
