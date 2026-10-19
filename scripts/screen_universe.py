#!/usr/bin/env python3
"""Full-universe screening funnel driver for the bounded G0 rerun.

Subcommands:
  plan-1h   data/universe.json -> plan CSV (kind,symbol,period) for 1h klines.
  screen    screen every downloaded 1h parquet -> canonical pump events CSV
            + symbol->event-months map + 1m/funding download plan CSV.

The 1m plan covers exactly the months each event's study window [-48h, +14d]
touches (see signalx.funnel.build_tight_plan). Events in the last screen month
(2026-09) get daily 1m dumps for the following month up to the newest
published day, probed via BTCUSDT because monthly dumps only exist for
finished months. Funding-rate dumps are tiny, so the whole funding history
for every symbol is planned (plan_funding_all.csv), avoiding any lookback
edge cases in the funding-percentile conditioning.

All outputs are written atomically (tmp + rename). Any partial failure exits
nonzero without writing (a partial screen would silently bias the study).

Usage:
  .venv/bin/python scripts/screen_universe.py plan-1h
  .venv/bin/python scripts/screen_universe.py screen --data-root data --out reports/g0_full
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from signalx.datafeed import (  # noqa: E402
    DataFeedError,
    download,
    dump_stem,
    dump_url,
    raw_dir,
    read_parquet,
)
from signalx.funnel import build_tight_plan, plan_from_universe, screen_symbol  # noqa: E402

# Screen window: pumps detected on 1h bars; 2024-09 is lookback context only.
SCREEN_START = "2024-10"
SCREEN_END = "2026-09"
CONTEXT_MONTH = "2024-09"
PROBE_SYMBOL = "BTCUSDT"      # daily-tail probe reference (published for all)
PROBE_DAYS = 20


def _ms(month: str, *, end: bool = False) -> int:
    y, m = (int(x) for x in month.split("-"))
    if end:
        last = calendar.monthrange(y, m)[1]
        dt = datetime.datetime(y, m, last, 23, tzinfo=datetime.timezone.utc)
    else:
        dt = datetime.datetime(y, m, 1, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _write_plan(path: Path, tasks: list[tuple[str, str, str]]) -> None:
    lines = ["kind,symbol,period"] + [",".join(t) for t in tasks]
    _atomic_write(path, "\n".join(lines) + "\n")


# --------------------------------------------------------------- plan-1h


def cmd_plan_1h(args: argparse.Namespace) -> int:
    if not args.universe.exists():
        print(f"FATAL: universe file not found: {args.universe}", file=sys.stderr)
        return 1
    doc = json.loads(args.universe.read_text())
    tasks = plan_from_universe(doc, "1h")
    _write_plan(args.out, tasks)
    symbols = {s for _k, s, _p in tasks}
    months = sorted({p for _k, _s, p in tasks})
    print(f"[plan-1h] {len(tasks)} tasks ({len(symbols)} symbols x {len(months)} months)")
    print(f"[plan-1h] months: {months[0]} .. {months[-1]}")
    print(f"[plan-1h] wrote {args.out}")
    return 0


# ----------------------------------------------------------------- screen


def probe_tail_through(data_root: Path, *, symbol: str = PROBE_SYMBOL, days: int = PROBE_DAYS) -> str:
    """Newest published daily 1m dump day (YYYY-MM-DD) via download probe."""
    import requests

    sess = requests.Session()
    today = datetime.datetime.now(datetime.timezone.utc).date()
    for back in range(1, days + 1):
        day = today - datetime.timedelta(days=back)
        period = day.isoformat()
        stem = dump_stem("klines_1m_daily", symbol, period)
        dest = raw_dir(data_root, "klines_1m_daily", symbol) / f"{stem}.zip"
        if dest.exists():
            return period
        if download(dump_url("klines_1m_daily", symbol, period), dest, session=sess):
            return period
    raise DataFeedError(f"no daily dump published in the last {days} days for {symbol}")


def cmd_screen(args: argparse.Namespace) -> int:
    parquet_dir = args.data_root / "parquet" / "klines_1h"
    paths = sorted(parquet_dir.glob("*.parquet"))
    if not paths:
        print(f"FATAL: no 1h parquet under {parquet_dir} (run the 1h download first)", file=sys.stderr)
        return 1

    start_ms = _ms(SCREEN_START)
    end_ms = _ms(SCREEN_END, end=True)
    print(f"[screen] {len(paths)} symbols, window {SCREEN_START} .. {SCREEN_END}", flush=True)

    frames: list[pd.DataFrame] = []
    for i, path in enumerate(paths, start=1):
        sym = path.stem
        df = read_parquet(path)
        if not df["open_time"].is_monotonic_increasing:
            print(f"FATAL: {sym}: open_time not sorted", file=sys.stderr)
            return 1
        ev = screen_symbol(sym, df, start_ms=start_ms, end_ms=end_ms)
        if len(ev):
            qv24 = df["quote_volume"].rolling(24, min_periods=24).sum().to_numpy()
            ev = ev.assign(quote_volume_24h=qv24[ev["idx"].to_numpy()])
            frames.append(ev)
        if i % 100 == 0 or i == len(paths):
            n = sum(len(f) for f in frames)
            print(f"[screen] {i}/{len(paths)} symbols, {n} events so far", flush=True)

    events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["idx", "open_time", "close", "return_24h", "vol_z", "quote_volume_24h", "symbol", "month"]
    )
    events = events.sort_values(["symbol", "open_time"]).reset_index(drop=True)

    args.out.mkdir(parents=True, exist_ok=True)
    _atomic_write(args.out / "events_screen.csv", events.to_csv(index=False))

    symbol_months = {
        sym: sorted(set(grp["month"])) for sym, grp in events.groupby("symbol")
    }
    _atomic_write(args.out / "symbol_months.json", json.dumps(symbol_months, indent=1) + "\n")

    tail_through: str | None = None
    if any("2026-09" in months for months in symbol_months.values()):
        tail_through = probe_tail_through(args.data_root)
        print(f"[screen] right-edge events present; daily tail through {tail_through}", flush=True)

    events_ms = {
        sym: [int(x) for x in grp["open_time"]] for sym, grp in events.groupby("symbol")
    }
    tasks = build_tight_plan(events_ms, lo=CONTEXT_MONTH, hi=SCREEN_END, tail_through=tail_through)
    _write_plan(args.out / "plan_1m.csv", tasks)

    if args.universe.exists():
        doc = json.loads(args.universe.read_text())
        funding = plan_from_universe(doc, "funding")
        _write_plan(args.out / "plan_funding_all.csv", funding)
        print(f"[screen] funding plan (all months, all symbols): {len(funding)} tasks -> plan_funding_all.csv")
    else:
        print(f"[screen] WARNING: {args.universe} missing; no funding plan written", file=sys.stderr)

    by_kind: dict[str, int] = {}
    for k, _s, _p in tasks:
        by_kind[k] = by_kind.get(k, 0) + 1
    print(f"[screen] events={len(events)} across {len(symbol_months)} symbols")
    if len(events):
        q = events["return_24h"].quantile(0.9)
        print(
            f"[screen] pump size: median {100 * events['return_24h'].median():.0f}%, "
            f"P90 {100 * q:.0f}%, max {100 * events['return_24h'].max():.0f}%"
        )
    print(f"[screen] plan tasks: {len(tasks)} ({by_kind})")
    print(f"[screen] wrote {args.out / 'events_screen.csv'}, {args.out / 'symbol_months.json'}, {args.out / 'plan_1m.csv'}")
    return 0


# ------------------------------------------------------------------- main


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    plan = sub.add_parser("plan-1h", help="write the 1h universe download plan")
    plan.add_argument("--universe", type=Path, default=ROOT / "data" / "universe.json")
    plan.add_argument("--out", type=Path, default=ROOT / "reports" / "g0_full" / "plan_1h.csv")
    plan.set_defaults(fn=cmd_plan_1h)

    scr = sub.add_parser("screen", help="screen 1h parquet -> events + 1m plan")
    scr.add_argument("--data-root", type=Path, default=ROOT / "data")
    scr.add_argument("--out", type=Path, default=ROOT / "reports" / "g0_full")
    scr.add_argument("--universe", type=Path, default=ROOT / "data" / "universe.json")
    scr.set_defaults(fn=cmd_screen)
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
