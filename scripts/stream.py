#!/usr/bin/env python3
"""Live websocket tape: large-print trades for the watchlist (+ liquidations
and cascades, wired for venues that deliver them).

Stream availability note (verified 2026-10-17 on this environment's
fstream endpoint): only ``trade``, ``bookTicker`` and ``depth20`` frames are
delivered; ``aggTrade``, ``kline_1m``, ``markPrice``, ``miniTicker`` and
``forceOrder`` (incl. the ``!...@arr`` variants) are silent. The tape
therefore subscribes to ``@trade`` and prints trades >= --min-notional.
The liquidation/cascade path stays wired but dormant until the venue
delivers forceOrder frames.

Observation tool only; no orders.

Usage (from repo root):
  .venv/bin/python scripts/stream.py --top 10 --min-notional 25000
  .venv/bin/python scripts/stream.py --symbols AGTUSDT --min-notional 0
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

from signalx.cvd import CvdError, parse_agg_message  # noqa: E402
from signalx.feeds import FeedError, fetch_gainers, make_client  # noqa: E402
from signalx.stream import (  # noqa: E402
    FORCE_ORDER_STREAM,
    WS_BASE,
    LiquidationCascadeDetector,
    LiquidationOrder,
    StreamError,
    backoff_delays,
    parse_stream_message,
)
from signalx.validation import ValidationError, validate_symbol  # noqa: E402


def _ts() -> str:
    return time.strftime("%H:%M:%S", time.gmtime())


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--top", type=int, default=10, help="top gainers to stream [0..500] (default 10)")
    ap.add_argument("--symbols", default="", help="extra symbols, comma separated")
    ap.add_argument("--min-notional", type=float, default=25_000.0,
                    help="min print size in quote units (default 25000; 0 = print all)")
    ap.add_argument("--window", type=int, default=60_000, help="cascade window in ms (default 60000)")
    ap.add_argument("--min-count", type=int, default=5, help="liquidations to flag a cascade (default 5)")
    ap.add_argument("--max-reconnects", type=int, default=0,
                    help="give up after N reconnects; 0 = retry forever (default)")
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


async def stream(args, symbols: list[str]) -> int:
    streams = [f"{s.lower()}@trade" for s in symbols] + [FORCE_ORDER_STREAM]
    url = f"{WS_BASE}/stream?streams={'/'.join(streams)}"
    detector = LiquidationCascadeDetector(window_ms=args.window, min_count=args.min_count)
    delays = backoff_delays(args.max_reconnects or 8)
    attempt = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, close_timeout=5) as ws:
                print(f"[{_ts()}] connected: {len(symbols)} trade streams + liquidation stream")
                attempt = 0
                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError as exc:
                        print(f"[{_ts()}] WARNING: bad json: {exc}")
                        continue
                    try:
                        trade = parse_agg_message(payload)
                    except CvdError as exc:
                        print(f"[{_ts()}] WARNING: bad trade frame: {exc}")
                        trade = None
                    if trade is not None:
                        sym, t = trade
                        notional = t.price * t.qty
                        if notional >= args.min_notional:
                            side = "SELL" if t.is_buyer_maker else "BUY"
                            print(
                                f"[{_ts()}] PRINT {sym} {side} {t.qty:.6g} @ {t.price:.8g} "
                                f"(${notional:,.0f})"
                            )
                        continue
                    try:
                        ev = parse_stream_message(payload)
                    except StreamError as exc:
                        print(f"[{_ts()}] WARNING: bad frame: {exc}")
                        continue
                    if isinstance(ev, LiquidationOrder):
                        print(
                            f"[{_ts()}] LIQ {ev.symbol} {ev.liquidated_side} "
                            f"{ev.qty:.6g} @ {ev.price:.8g} (${ev.notional:,.0f})"
                        )
                        cascade = detector.add(ev)
                        if cascade is not None:
                            print(
                                f"[{_ts()}] *** CASCADE {cascade['symbol']} {cascade['side']} "
                                f"x{cascade['count']} (${cascade['notional']:,.0f} "
                                f"in {cascade['window_ms'] // 1000}s)"
                            )
        except (WebSocketException, OSError) as exc:
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
        if args.window <= 0 or args.min_count <= 0:
            raise ValidationError("--window and --min-count must be positive")
        if args.min_notional < 0:
            raise ValidationError("--min-notional must be >= 0")
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
            print("ERROR: no symbols to stream")
            return 1
        print(f"streaming: {', '.join(symbols)}")
        return asyncio.run(stream(args, symbols))
    except KeyboardInterrupt:
        print("\nstopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
