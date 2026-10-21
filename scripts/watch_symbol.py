#!/usr/bin/env python3
"""Live watcher: poll one symbol, report pump/plateau/resistance/squeeze state.

Observation tool only: the Phase 1 G0 event study did NOT validate a short edge
(see reports/phase1/G0_VERDICT.md). Events are observations, not trade signals.

Usage (from repo root):
  .venv/bin/python scripts/watch_symbol.py AMPUSDT --once
  .venv/bin/python scripts/watch_symbol.py AMPUSDT --interval 60 --exit-on-resistance
  .venv/bin/python scripts/watch_symbol.py AMPUSDT --market futures --alert

Uses the async feed layer (signalx.feeds) shared with the web API.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import asyncio  # noqa: E402

from signalx.alerts import load_alert_env, send_alert  # noqa: E402
from signalx.feeds import (  # noqa: E402
    FeedError,
    TF_MS,
    detect_market,
    make_client,
    poll_snapshot,
)
from signalx.live import LiveError  # noqa: E402
from signalx.validation import ValidationError, validate_interval, validate_symbol  # noqa: E402

MIN_POLL_INTERVAL_S = 10.0


def _fmt(x, spec="{:.6g}"):
    return "n/a" if x is None else spec.format(x)


def render(snap, market, tf):
    ts = time.strftime("%H:%M:%S", time.gmtime())
    chg = _fmt(snap["change_24h_pct"], "{:+.1f}%")
    head = (
        f"[{ts}] {snap['symbol']} {_fmt(snap['price'])} ({chg} 24h)"
        f" tf={tf} market={market} state={snap['state']}"
    )
    cm = snap["compression"]
    cm_txt = (
        f"h/atr={cm['height_atr']:.1f} contain={cm['containment']:.2f} q={cm['qualifies']}"
        if cm else "n/a"
    )
    line2 = (
        f"  pump={snap['is_pump']} vol_z={_fmt(snap['vol_z'], '{:.1f}')}"
        f" | plateau {cm_txt}"
        f" | RSI {_fmt(snap['rsi'], '{:.1f}')}"
        f" div={snap['has_bearish_div']} decay={snap['momentum_decay']}"
    )
    r = snap["resistance"]
    r_txt = (
        f"{r['low']:.8g}-{r['high']:.8g} ({r['dist_pct'] * 100:+.1f}%,"
        f" {r['dist_atr']:.1f} ATR, {r['touches']}T)"
        if r else "none"
    )
    line3 = (
        f"  resistance {r_txt} | swing_low {_fmt(snap['swing_low'])}"
        f" swing_high {_fmt(snap['swing_high'])} | BOS_down={snap['bos_down']}"
        f" BOS_up={snap['bos_up']} | zones={snap['n_zones']}"
    )
    if snap["funding_now"] is None:
        fund_txt = "n/a (spot)"
    else:
        fund_txt = f"{snap['funding_now']:.5g} pctile={_fmt(snap['funding_pctile'], '{:.0f}')}"
    line4 = (
        f"  funding {fund_txt} | oi_regime={snap['oi_regime'] or 'n/a'}"
        f" | squeeze={snap['squeeze_active']}"
    )
    flow = snap.get("flow") or {}
    line5 = (
        f"  flow cvd={_fmt(flow.get('cvd_ratio'), '{:+.3f}')}"
        f" taker={_fmt(flow.get('taker_buy_ratio'), '{:.3f}')}"
        f" large_n={flow.get('large_print_count') if flow.get('large_print_count') is not None else 'n/a'}"
        f" large_net={_fmt(flow.get('large_print_net_usd'), '{:+,.0f}')}"
        f" book={_fmt(flow.get('book_imbalance'), '{:+.3f}')}"
        f" bias={flow.get('fat_money_bias') or 'n/a'}"
        f" absorb={flow.get('absorption') or 'n/a'}"
    )
    events = ", ".join(snap["events"]) if snap["events"] else "-"
    return "\n".join([head, line2, line3, line4, line5, f"  EVENTS: {events}"])


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("symbol")
    ap.add_argument("--market", choices=("auto", "spot", "futures"), default="auto")
    ap.add_argument("--tf", default="5m", help="kline timeframe (default 5m)")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between polls (>= 10)")
    ap.add_argument("--polls", type=int, default=0, help="0 = poll forever")
    ap.add_argument("--once", action="store_true", help="single snapshot, then exit")
    ap.add_argument("--exit-on-resistance", action="store_true")
    ap.add_argument("--flow", action="store_true",
                    help="also fetch agg trades + depth for flow features")
    ap.add_argument("--alert", action="store_true",
                    help="Telegram alert on new events (needs TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")
    return ap.parse_args(argv)


async def watch(args, symbol: str, interval: str, max_polls: int, creds: dict | None) -> int:
    market = args.market
    prev_events: set[str] = set()
    polls = 0
    async with make_client() as client:
        while True:
            polls += 1
            now_ms = int(time.time() * 1000)
            try:
                if market == "auto":
                    market = await detect_market(client, symbol)
                    note = " (no USDT-M perp; spot only)" if market == "spot" else ""
                    print(f"market auto-detected: {market}{note}")
                snap = await poll_snapshot(
                    client, symbol, market, interval, now_ms,
                    include_microstructure=args.flow,
                )
            except (FeedError, ValidationError, LiveError) as exc:
                print(f"ERROR: {exc}")
                if max_polls == 1:
                    return 1
                await asyncio.sleep(max(args.interval, MIN_POLL_INTERVAL_S))
                continue

            print(render(snap, market, interval))
            new_events = set(snap["events"]) - prev_events
            prev_events = set(snap["events"])
            if creds and new_events:
                text = f"{symbol} {snap['state']} new: {', '.join(sorted(new_events))}\n" + render(
                    snap, market, interval
                )
                try:
                    send_alert(text, creds["TELEGRAM_BOT_TOKEN"], creds["TELEGRAM_CHAT_ID"])
                    print("  alert sent")
                except RuntimeError as exc:
                    print(f"  WARNING: alert failed: {exc}")

            if args.exit_on_resistance and "RESISTANCE_TOUCHED" in snap["events"]:
                print(f"EXIT: {symbol} touched resistance {snap['resistance']['low']:.8g}")
                return 0
            if max_polls and polls >= max_polls:
                return 0
            await asyncio.sleep(max(args.interval, MIN_POLL_INTERVAL_S))


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        symbol = validate_symbol(args.symbol.upper())
        interval = validate_interval(args.tf)
        if interval not in TF_MS:
            raise ValidationError(f"timeframe not supported by watcher: {interval}")
        if args.interval < MIN_POLL_INTERVAL_S:
            raise ValidationError(f"--interval must be >= {MIN_POLL_INTERVAL_S:.0f}s")
        if args.polls < 0:
            raise ValidationError("--polls must be >= 0")
        max_polls = 1 if args.once else args.polls
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
        return asyncio.run(watch(args, symbol, interval, max_polls, creds))
    except KeyboardInterrupt:
        print("\nstopped")
        return 130


if __name__ == "__main__":
    sys.exit(main())
