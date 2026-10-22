#!/usr/bin/env python3
"""Paper companion: consume sentinel SIGNAL rows and manage simulated shorts.

No exchange credentials, no order endpoints, no live execution. Start the
sentinel first, then this process. It fills at the latest trade price with
adverse slippage and manages stop/TP1/TP2/time exits.

Examples:
  .venv/bin/python scripts/companion.py --once
  .venv/bin/python scripts/companion.py --interval 15
  .venv/bin/python scripts/companion.py --paper-dir data/paper
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signalx.companion import Companion, CompanionConfig, load_or_create_broker  # noqa: E402
from signalx.feeds import FeedError, fetch_price, make_client  # noqa: E402
from signalx.journal import SignalJournal  # noqa: E402
from signalx.paper import PaperConfig, PaperError  # noqa: E402


def _ts(ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ms / 1000))


def _print_events(events: list[dict]) -> None:
    for ev in events:
        action = ev.get("action", "?")
        sym = ev.get("symbol", "")
        reason = ev.get("reason", "")
        extra = ""
        if action == "OPEN":
            extra = f" entry={ev.get('entry'):.8g} qty={ev.get('qty'):.8g} risk=${ev.get('risk', 0):.2f}"
        elif action == "EXIT":
            extra = f" {ev.get('reason')} qty={ev.get('qty'):.8g} net=${ev.get('net', 0):+.2f}"
        print(f"[{_ts(int(time.time() * 1000))}] PAPER {action} {sym} {reason}{extra}".rstrip())


async def run(args: argparse.Namespace) -> int:
    paper_dir = Path(args.paper_dir)
    paper_dir.mkdir(parents=True, exist_ok=True)
    cfg = CompanionConfig(
        journal_dir=Path(args.journal),
        paper_dir=paper_dir,
        halt_path=Path(args.halt).expanduser(),
        market=args.market,
        interval_s=args.interval,
        paper_config=PaperConfig(starting_equity=args.starting_equity, risk_pct=args.risk_pct),
    )
    journal = SignalJournal(cfg.journal_dir)
    now = int(time.time() * 1000)
    broker = load_or_create_broker(cfg, now_ms=now)
    print(
        f"paper companion: mode=paper market={cfg.market} "
        f"equity=${broker.equity:,.2f} open={len(broker.positions)} "
        f"halted={broker.halted or broker.manual_halt}"
    )

    async with make_client() as client:
        async def price_fn(symbol: str) -> float:
            return await fetch_price(client, cfg.market, symbol)

        comp = Companion(broker, journal, cfg, price_fn)
        while True:
            now = int(time.time() * 1000)
            try:
                events = await comp.step(now)
                comp.save(now)
            except (FeedError, PaperError, OSError, ValueError) as exc:
                print(f"[{_ts(now)}] companion error: {type(exc).__name__}: {exc}", file=sys.stderr)
                if args.once:
                    return 2
            else:
                if events:
                    _print_events(events)
                print(
                    f"[{_ts(now)}] paper sweep: equity=${comp.broker.equity:,.2f} "
                    f"open={len(comp.broker.positions)} "
                    f"halted={comp.broker.halted or comp.broker.manual_halt}"
                )
            if args.once:
                return 0
            await asyncio.sleep(args.interval)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="SIGNAL-X paper companion (no live orders)")
    ap.add_argument("--journal", default=str(ROOT / "data" / "journal"))
    ap.add_argument("--paper-dir", default=str(ROOT / "data" / "paper"))
    ap.add_argument(
        "--halt",
        default=str(Path.home() / ".config" / "signal-x" / "TRADING_HALT"),
        help="kill-switch file; create it to block new paper entries",
    )
    ap.add_argument("--market", choices=("futures", "spot"), default="futures")
    ap.add_argument("--interval", type=float, default=15.0, help="seconds between sweeps")
    ap.add_argument("--starting-equity", type=float, default=10_000.0)
    ap.add_argument("--risk-pct", type=float, default=0.0025, help="fraction of equity per trade (0.0025 = 0.25%%)")
    ap.add_argument("--once", action="store_true", help="one sweep, then exit")
    args = ap.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nstopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
