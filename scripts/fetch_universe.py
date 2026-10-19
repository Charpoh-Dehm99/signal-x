#!/usr/bin/env python3
"""Build the full-universe manifest for the G0 rerun.

Steps:
  1. S3 dump listing (paginated) -> every symbol dir ever published (includes
     delisted symbols that exchangeInfo has forgotten -> survivorship fix).
  2. fapi exchangeInfo -> classify currently known contracts; TradFi perps
     (XAU/TSLA/... — no crypto pump dynamics) are excluded with a reason.
  3. Per-symbol dump availability for klines 1h / klines 1m / fundingRate
     months in the study period (parallel, one listing per series).
  4. Write data/universe.json + data/universe_symbols.txt atomically.

Any listing failure aborts the write and exits nonzero (a partial universe
would silently bias the event study).

Usage:
  .venv/bin/python scripts/fetch_universe.py [--workers 12] [--out data/universe.json]
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signalx.universe import (  # noqa: E402
    UniverseError,
    build_universe,
    fetch_available_months,
    fetch_exchange_info,
    fetch_server_universe,
)

# Study window: screening runs 2024-10-01 .. 2026-09-30; 2024-09 is context
# (pump detection needs 7d of prior hourly volume + 24h return lookback).
CONTEXT_MONTH = "2024-09"
SCREEN_START = "2024-10"
SCREEN_END = "2026-09"

_tls = threading.local()


def _session():
    import requests

    if not hasattr(_tls, "sess"):
        _tls.sess = requests.Session()
    return _tls.sess


def months_in_range(months: list[str]) -> list[str]:
    return [m for m in months if CONTEXT_MONTH <= m <= SCREEN_END]


def symbol_jobs(symbol: str, dump_dir: str) -> dict:
    sess = _session()
    return {
        "months_1h": months_in_range(fetch_available_months(dump_dir, interval="1h", session=sess)),
        "months_1m": months_in_range(fetch_available_months(dump_dir, interval="1m", session=sess)),
        "months_funding": months_in_range(fetch_available_months(dump_dir, interval=None, session=sess)),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "universe.json")
    args = ap.parse_args(argv)

    sess = _session()

    print("[universe] fetching S3 symbol listing ...", flush=True)
    raw_dirs = fetch_server_universe(session=sess)
    print(f"[universe] {len(raw_dirs)} symbol dump dirs on the server", flush=True)

    print("[universe] fetching exchangeInfo ...", flush=True)
    info_map, skipped_symbols = fetch_exchange_info(session=sess)
    print(f"[universe] exchangeInfo: {len(info_map)} symbols", flush=True)
    if skipped_symbols:
        print(f"[universe] skipped non-whitelist tickers (out of scope): {skipped_symbols}", flush=True)

    universe, excluded = build_universe(raw_dirs, info_map)
    ex_by_reason: dict[str, list[str]] = {}
    for raw, reason in excluded:
        ex_by_reason.setdefault(reason, []).append(raw)
    for reason, raws in sorted(ex_by_reason.items()):
        sample = ", ".join(raws[:6]) + (" ..." if len(raws) > 6 else "")
        print(f"[universe] excluded {reason}: {len(raws)} ({sample})", flush=True)
    print(f"[universe] crypto-perp universe: {len(universe)} symbols", flush=True)

    print(f"[universe] listing available months for {len(universe)} symbols (3 series each) ...", flush=True)
    errors: list[str] = []
    done = 0
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(symbol_jobs, sym, meta["dump_dir"]): sym for sym, meta in universe.items()}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                results[sym] = fut.result()
            except Exception as exc:  # noqa: BLE001 - collected and reported loudly below
                errors.append(f"{sym}: {exc}")
            done += 1
            if done % 100 == 0 or done == len(universe):
                print(f"[universe] {done}/{len(universe)} symbols ({len(errors)} errors)", flush=True)

    if errors:
        print(f"[universe] {len(errors)} ERRORS (first 10):", flush=True)
        for e in errors[:10]:
            print(f"  ! {e}", flush=True)
        print("[universe] aborting without writing a partial manifest", flush=True)
        return 2

    doc = {
        "generated_at_ms": int(time.time() * 1000),
        "period": {"context_month": CONTEXT_MONTH, "screen_start": SCREEN_START, "screen_end": SCREEN_END},
        "counts": {"server_dirs": len(raw_dirs), "universe": len(universe), "excluded": len(excluded)},
        "exchange_info_skipped": sorted(skipped_symbols),
        "excluded": {reason: sorted(raws) for reason, raws in sorted(ex_by_reason.items())},
        "symbols": {
            sym: {
                "dump_dir": universe[sym]["dump_dir"],
                "exchange_status": info_map.get(sym, {}).get("status"),
                **results[sym],
            }
            for sym in sorted(universe)
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_name(args.out.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=False) + "\n")
    tmp.replace(args.out)
    symbols_txt = args.out.parent / "universe_symbols.txt"
    symbols_txt.write_text("\n".join(sorted(universe)) + "\n")

    no_data = [s for s, m in results.items() if not (m["months_1h"] or m["months_1m"])]
    with_events_window = [s for s, m in results.items() if m["months_1m"]]
    print(f"[universe] wrote {args.out}")
    print(f"[universe] {len(with_events_window)} symbols have 1m dumps in window; {len(no_data)} have none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
