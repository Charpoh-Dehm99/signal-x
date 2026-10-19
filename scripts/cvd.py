#!/usr/bin/env python3
"""CVD recorder: stream aggTrades for the watchlist and store per-minute
taker buy/sell notional buckets (observation only).

G0 killed the tested signal space — this is instrumentation for future
pre-registered hypotheses (flow/CVD research), never a signal claim.

Usage (from repo root):
  .venv/bin/python scripts/cvd.py --top 10
  .venv/bin/python scripts/cvd.py --symbols AGTUSDT,ICNTUSDT
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

import websockets  # noqa: E402
from websockets.exceptions import WebSocketException  # noqa: E402

from signalx.cvd import (  # noqa: E402
    CvdError,
    CvdStore,
    MinuteBuckets,
    parse_agg_message,
)
from signalx.feeds import FeedError, fetch_gainers, make_client  # noqa: E402
from signalx.stream import WS_BASE, backoff_delays  # noqa: E402
from signalx.validation import ValidationError, validate_symbol  # noqa: E402

MIN_FLUSH_S = 10.0


def _ts() -> str:
    return time.strftime("%H:%M:%S", time.gmtime())


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--top", type=int, default=10,
                    help="top gainers recorded [0..500] (default 10; aggTrade is high-rate)")
    ap.add_argument("--symbols", default="", help="extra symbols, comma separated")
    ap.add_argument("--flush", type=float, default=60.0,
                    help=f"seconds between parquet flushes (>= {MIN_FLUSH_S:.0f})")
    ap.add_argument("--max-reconnects", type=int, default=0,
                    help="give up after N reconnects; 0 = retry forever (default)")
    ap.add_argument("--data-root", default=str(ROOT / "data" / "live" / "cvd"),
                    help="per-symbol parquet root")
    return ap.parse_args(argv)


async def gather_symbols(args, extra: list[str]) -> list[str]:
    symbols: list[str] = []
    if args.top:
        try:
            async with make_client() as client:
                gainers = await fetch_gainers(client, top=args.top)
            symbols = [g["symbol"] for g in gainers]
        except FeedError as exc:
            print(f"[{_ts()}] gainers ERROR: {exc}")
    for s in extra:
        if s not in symbols:
            symbols.append(s)
    clean: list[str] = []
    for s in symbols:
        try:
            validate_symbol(s)
        except ValidationError:
            print(f"[{_ts()}] skipping non-conforming symbol {s!r}")
            continue
        clean.append(s)
    return clean


def flush(store: CvdStore, buffers: dict[str, list[dict]], symbols: list[str]) -> tuple[int, int]:
    total_new = 0
    active = 0
    for sym in symbols:
        rows = buffers.get(sym)
        if not rows:
            continue
        total_new += store.upsert(sym, rows)
        active += 1
        buffers[sym] = []
    return total_new, active


async def record(args, symbols: list[str]) -> int:
    store = CvdStore(args.data_root)
    print(f"data root: {args.data_root} ({len(list(store.root.glob('*.parquet')))} files)")
    # @trade, not @aggTrade: this environment's aggTrade stream is silent
    # (verified 2026-10-17); trade frames carry the same T/p/q/m fields and
    # sum to identical taker notional.
    streams = [f"{s.lower()}@trade" for s in symbols]
    url = f"{WS_BASE}/stream?streams={'/'.join(streams)}"
    delays = backoff_delays(args.max_reconnects or 8)
    attempt = 0
    buffers: dict[str, list[dict]] = {}
    buckets: dict[str, MinuteBuckets] = {s: MinuteBuckets(s) for s in symbols}
    flush_s = max(args.flush, MIN_FLUSH_S)
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
                print(f"[{_ts()}] connected: {len(symbols)} symbols (trade)")
                attempt = 0
                last_flush = time.monotonic()
                async for raw in ws:
                    now = time.monotonic()
                    if now - last_flush >= flush_s:
                        new, active = flush(store, buffers, symbols)
                        print(f"[{_ts()}] flush: {new} new rows, {active}/{len(symbols)} symbols")
                        last_flush = now
                    try:
                        msg = parse_agg_message(json.loads(raw))
                    except (json.JSONDecodeError, CvdError) as exc:
                        print(f"[{_ts()}] WARNING: bad frame: {exc}")
                        continue
                    if msg is not None:
                        sym, trade = msg
                        try:
                            closed = buckets[sym].add(trade)
                        except (CvdError, KeyError) as exc:
                            print(f"[{_ts()}] WARNING: {exc}")
                            continue
                        if closed is not None:
                            buffers.setdefault(sym, []).append(closed)
        except (WebSocketException, OSError) as exc:
            new, _ = flush(store, buffers, symbols)
            if new:
                print(f"[{_ts()}] flush: {new} rows before reconnect")
            if args.max_reconnects > 0 and attempt >= len(delays):
                print(f"[{_ts()}] giving up after {attempt} reconnects: {exc}")
                return 1
            delay = delays[min(attempt, len(delays) - 1)]
            attempt += 1
            print(
                f"[{_ts()}] disconnected ({exc.__class__.__name__}: {exc}); "
                f"reconnecting in {delay:.0f}s"
            )
            await asyncio.sleep(delay)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if not (0 <= args.top <= 500):
            raise ValidationError("--top must be in [0..500]")
        if args.flush < MIN_FLUSH_S:
            raise ValidationError(f"--flush must be >= {MIN_FLUSH_S:.0f}s")
        if args.max_reconnects < 0:
            raise ValidationError("--max-reconnects must be >= 0 (0 = retry forever)")
        extra = [validate_symbol(s.strip().upper()) for s in args.symbols.split(",") if s.strip()]
        if not args.top and not extra:
            raise ValidationError("need --top > 0 or --symbols")
    except ValidationError as exc:
        print(f"ERROR: {exc}")
        return 2

    try:
        symbols = asyncio.run(gather_symbols(args, extra))
        if not symbols:
            print("ERROR: no symbols to record")
            return 1
        print(f"recording: {', '.join(symbols)}")
        return asyncio.run(record(args, symbols))
    except KeyboardInterrupt:
        print("\nstopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
