#!/usr/bin/env python3
"""SNIPER V3 observation runner (1H permission -> 15m location -> 5m timing).

Observation only: no orders, no journal SIGNAL rows. See docs/SNIPER_V3.md.
Usage (from repo root):
  .venv/bin/python scripts/sniper_v3.py --symbol BTCUSDT --once
  .venv/bin/python scripts/sniper_v3.py --top 10 --interval 60 --once
  .venv/bin/python scripts/sniper_v3.py --symbol ETHUSDT --loop --interval 60
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signalx.feeds import FeedError, fetch_gainers, make_client  # noqa: E402
from signalx.sniper_v3.data import (  # noqa: E402
    fetch_and_evaluate,
    funding_percentile,
    oi_crowding,
)
from signalx.sniper_v3.primitives import SniperError  # noqa: E402
from signalx.validation import ValidationError, validate_symbol  # noqa: E402

MIN_INTERVAL_S = 30.0


def _ts() -> str:
    return time.strftime("%H:%M:%S", time.gmtime())


def _symbols(raw: str) -> list[str]:
    out: list[str] = []
    for chunk in raw.split(","):
        chunk = chunk.strip().upper()
        if not chunk:
            continue
        try:
            out.append(validate_symbol(chunk))
        except ValidationError as exc:
            raise SystemExit(str(exc)) from exc
    return out


def _print_result(symbol: str, inputs, decision) -> None:
    fp = funding_percentile(inputs)
    oc = oi_crowding(inputs)
    ctx = f"funding_pct={fp:.0f}" if fp is not None else "funding_pct=n/a"
    ctx += f" oi_crowd={oc:.2f}" if oc is not None else " oi_crowd=n/a"
    if decision is None:
        print(f"[{_ts()}] {symbol}: no SNIPER setup ({ctx})")
        return
    d = decision
    print(
        f"[{_ts()}] {symbol}: {d.direction.upper()} entry={d.entry:.8g} "
        f"stop={d.stop:.8g} tp1={d.tp1:.8g} tp2={d.tp2:.8g} "
        f"rr={d.rr:.2f} conf={d.confidence:.2f} ev={d.expected_value_r:+.2f}R ({ctx})"
    )
    print(f"          why: {'; '.join(d.reasons)}")


async def scan(client, symbols: list[str], *, workers: int = 6) -> int:
    sem = asyncio.Semaphore(max(1, min(workers, 16)))

    async def one(sym: str):
        async with sem:
            try:
                return sym, await fetch_and_evaluate(client, sym)
            except (FeedError, SniperError, ValidationError) as exc:
                return sym, exc

    results = await asyncio.gather(*(one(s) for s in symbols))
    found = 0
    for sym, payload in results:
        if isinstance(payload, Exception):
            print(f"[{_ts()}] {sym}: ERROR {payload}")
            continue
        inputs, decision = payload
        _print_result(sym, inputs, decision)
        found += int(decision is not None)
    return found


async def main_async(args) -> int:
    client = make_client()
    try:
        symbols = _symbols(args.symbols)
        if not symbols:
            gainers = await fetch_gainers(
                client, top=max(1, min(args.top, 100)), min_quote_volume=args.min_volume
            )
            symbols = [g["symbol"] for g in gainers]
        if not symbols:
            print("no symbols to evaluate")
            return 2
        while True:
            await scan(client, symbols, workers=args.workers)
            if not args.loop:
                return 0
            # Align just after the next 5m close.
            now_ms = int(time.time() * 1000)
            sleep_s = (300_000 - (now_ms % 300_000)) / 1000.0 + 3.0
            await asyncio.sleep(max(args.interval, sleep_s))
    finally:
        await client.aclose()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="", help="single symbol")
    ap.add_argument("--symbols", default="", help="comma-separated symbols")
    ap.add_argument("--top", type=int, default=10, help="top gainers to scan [1..100]")
    ap.add_argument("--min-volume", type=float, default=5_000_000.0,
                    help="min 24h quote volume for gainer scan")
    ap.add_argument("--workers", type=int, default=4, help="concurrent symbol fetches [1..16]")
    ap.add_argument("--interval", type=float, default=60.0,
                    help=f"minimum loop sleep seconds (>= {MIN_INTERVAL_S:.0f})")
    ap.add_argument("--once", action="store_true", help="single scan, then exit")
    ap.add_argument("--loop", action="store_true", help="poll until interrupted")
    args = ap.parse_args(argv)
    if args.symbol:
        args.symbols = args.symbol if not args.symbols else f"{args.symbol},{args.symbols}"
    if args.interval < MIN_INTERVAL_S:
        args.interval = MIN_INTERVAL_S
    if not (1 <= args.top <= 100):
        ap.error("--top must be in [1, 100]")
    if not (1 <= args.workers <= 16):
        ap.error("--workers must be in [1, 16]")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(main_async(args))
    except (FeedError, SniperError, ValidationError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
