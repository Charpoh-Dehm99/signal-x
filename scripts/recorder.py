#!/usr/bin/env python3
"""Live kline recorder: continuous watchlist capture into per-symbol Parquet.

For each tracked symbol, fetch bars since the last stored bar (bounded window)
and upsert into <data-root>/<SYMBOL>.parquet. Symbols whose store lags more
than 3 bars are marked STALE. Funding/OI recording: next increment.

Data/observation tool only; no orders.

Usage (from repo root):
  .venv/bin/python scripts/recorder.py --once
  .venv/bin/python scripts/recorder.py --top 40 --interval 60
  .venv/bin/python scripts/recorder.py --symbols AGTUSDT,AUSDT
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signalx.feeds import (  # noqa: E402
    FeedError,
    TF_MS,
    fetch_gainers,
    fetch_klines,
    make_client,
)
from signalx.recorder import (  # noqa: E402
    KlineStore,
    RecorderError,
    is_stale,
    kline_to_row,
    plan_backfill,
)
from signalx.validation import ValidationError, validate_interval, validate_symbol  # noqa: E402

MIN_INTERVAL_S = 30.0
MAX_BARS_CAP = 1000


def _ts() -> str:
    return time.strftime("%H:%M:%S", time.gmtime())


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--top", type=int, default=40, help="top gainers tracked [1..500] (default 40)")
    ap.add_argument("--symbols", default="", help="extra symbols to record, comma separated")
    ap.add_argument("--tf", default="1m", help="kline timeframe to record (default 1m)")
    ap.add_argument("--interval", type=float, default=60.0,
                    help=f"seconds between cycles (>= {MIN_INTERVAL_S:.0f})")
    ap.add_argument("--workers", type=int, default=6, help="concurrent symbol fetches [1..32]")
    ap.add_argument("--max-bars", type=int, default=500,
                    help=f"max bars per symbol per cycle [1..{MAX_BARS_CAP}]")
    ap.add_argument("--data-root", default=str(ROOT / "data" / "live" / "klines"),
                    help="per-symbol parquet directory")
    ap.add_argument("--once", action="store_true", help="single cycle, then exit")
    return ap.parse_args(argv)


async def cycle(client, symbols: list[str], args, tf: str, store: KlineStore) -> None:
    now_ms = int(time.time() * 1000)
    step_ms = TF_MS[tf]
    sem = asyncio.Semaphore(args.workers)

    async def one(symbol: str):
        async with sem:
            try:
                last = store.last_open_ms(symbol)
                plan = plan_backfill(last, now_ms, step_ms, max_bars=args.max_bars)
                if plan is None:
                    return symbol, 0, last, None
                klines = await fetch_klines(
                    client, "futures", symbol, tf, now_ms,
                    limit=plan["limit"], min_closed=1,
                    start_ms=plan["start_ms"], end_ms=plan["end_ms"],
                )
                added = store.upsert(symbol, [kline_to_row(k) for k in klines])
                return symbol, added, store.last_open_ms(symbol), None
            except (FeedError, ValidationError, RecorderError) as exc:
                return symbol, -1, None, str(exc)

    results = await asyncio.gather(*(one(s) for s in symbols))
    fresh = 0
    for symbol, added, last, err in results:
        if err is not None:
            print(f"[{_ts()}] {symbol} ERROR: {err}")
            continue
        stale = is_stale(last, now_ms, step_ms)
        if stale:
            marker = " STALE"
        else:
            marker = ""
            fresh += 1
        print(f"[{_ts()}] {symbol} +{added} bars up_to={last}{marker}")
    print(f"[{_ts()}] cycle: {len(symbols)} symbols, {fresh} fresh, {len(symbols) - fresh} stale/errors")


async def run(args, extra_symbols: list[str]) -> int:
    store = KlineStore(args.data_root)
    print(f"data root: {args.data_root} ({len(list(store.root.glob('*.parquet')))} files)")
    async with make_client() as client:
        while True:
            symbols: list[str] = []
            try:
                gainers = await fetch_gainers(client, top=args.top)
                symbols = [g["symbol"] for g in gainers]
            except FeedError as exc:
                print(f"[{_ts()}] gainers ERROR: {exc}")
            for s in extra_symbols:
                if s not in symbols:
                    symbols.append(s)
            if symbols:
                await cycle(client, symbols, args, args.tf, store)
            else:
                print(f"[{_ts()}] no symbols this cycle")
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
        if not (1 <= args.max_bars <= MAX_BARS_CAP):
            raise ValidationError(f"--max-bars must be in [1..{MAX_BARS_CAP}]")
        if args.interval < MIN_INTERVAL_S:
            raise ValidationError(f"--interval must be >= {MIN_INTERVAL_S:.0f}s")
        tf = validate_interval(args.tf)
        if tf not in TF_MS:
            raise ValidationError(f"timeframe not supported by recorder: {tf}")
        extra = [validate_symbol(s.strip().upper()) for s in args.symbols.split(",") if s.strip()]
    except ValidationError as exc:
        print(f"ERROR: {exc}")
        return 2

    try:
        return asyncio.run(run(args, extra))
    except KeyboardInterrupt:
        print("\nstopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
