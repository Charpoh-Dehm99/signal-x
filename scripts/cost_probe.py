#!/usr/bin/env python3
"""Cost/impact probe from recorded book features.

Reads data/live/book/*.parquet (bookTicker + depth20 band aggregates) and
writes reports/cost/probe-<date>.{csv,md} with per-symbol spread / depth /
estimated round-trip taker cost at reference notionals.

First-order model only (uniform-within-band fill walk, see
reports/cost/METHOD.md). Observation only — G0 KILL stands.

Usage:
  .venv/bin/python scripts/cost_probe.py [--fee-bps 5] [--notionals 2000,5000,10000]
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signalx.costmodel import round_trip_cost_bps  # noqa: E402

COLS = (
    "ts_ms", "spread_bps", "bid0", "bid0_qty", "ask0", "ask0_qty",
    "d05_bid", "d05_ask", "d1_bid", "d1_ask", "d2_bid", "d2_ask",
)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--book-root", type=Path, default=ROOT / "data/live/book")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "reports/cost")
    ap.add_argument("--fee-bps", type=float, default=5.0, help="taker fee per side in bps")
    ap.add_argument("--notionals", default="2000,5000,10000",
                    help="comma-separated USD notionals for round-trip estimates")
    ap.add_argument("--min-rows", type=int, default=1000,
                    help="skip symbol files with fewer rows")
    return ap.parse_args(argv)


def _med(vals: np.ndarray) -> float:
    if len(vals) == 0 or np.isnan(vals).all():
        return float("nan")
    return float(np.nanmedian(vals))


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.fee_bps < 0:
        print("ERROR: --fee-bps must be >= 0")
        return 2
    try:
        notionals = [float(x) for x in args.notionals.split(",") if x.strip()]
    except ValueError:
        print("ERROR: --notionals must be comma-separated numbers")
        return 2
    if not notionals or any(n <= 0 for n in notionals):
        print("ERROR: notionals must be positive")
        return 2

    files = sorted(args.book_root.glob("*.parquet"))
    if not files:
        print(f"ERROR: no book parquets under {args.book_root}")
        return 2

    rows_out: list[dict] = []
    skipped: list[str] = []
    total_rows = 0
    for path in files:
        tbl = pq.read_table(path, columns=list(COLS))
        n = tbl.num_rows
        total_rows += n
        if n < args.min_rows:
            skipped.append(f"{path.stem} ({n})")
            continue
        a = {c: tbl.column(c).to_numpy(zero_copy_only=False).astype(np.float64) for c in COLS}
        spread = _med(a["spread_bps"])
        hs = spread / 2.0 if np.isfinite(spread) else float("nan")
        span_h = (a["ts_ms"].max() - a["ts_ms"].min()) / 3.6e6
        rec = {
            "symbol": path.stem,
            "rows": n,
            "span_h": round(float(span_h), 2),
            "spread_bps": spread,
            "hs_bps": hs,
            "tob_bid_usd": _med(a["bid0"] * a["bid0_qty"]),
            "tob_ask_usd": _med(a["ask0"] * a["ask0_qty"]),
            "d05_bid_usd": _med(a["d05_bid"]),
            "d05_ask_usd": _med(a["d05_ask"]),
            "d1_bid_usd": _med(a["d1_bid"]),
            "d1_ask_usd": _med(a["d1_ask"]),
            "d2_bid_usd": _med(a["d2_bid"]),
            "d2_ask_usd": _med(a["d2_ask"]),
        }
        if np.isfinite(hs):
            for notional in notionals:
                dep_bid = [rec["d05_bid_usd"], rec["d1_bid_usd"], rec["d2_bid_usd"]]
                dep_ask = [rec["d05_ask_usd"], rec["d1_ask_usd"], rec["d2_ask_usd"]]
                try:
                    rt = round_trip_cost_bps(notional, args.fee_bps, hs, hs, dep_bid, dep_ask)
                except Exception:
                    rt = None
                rec[f"rt_{int(notional)}"] = rt
        rows_out.append(rec)

    if not rows_out:
        print("ERROR: no symbols passed --min-rows")
        return 2

    args.out_dir.mkdir(parents=True, exist_ok=True)
    date = time.strftime("%Y-%m-%d", time.gmtime())
    csv_path = args.out_dir / f"probe-{date}.csv"
    md_path = args.out_dir / f"probe-{date}.md"

    rt_keys = [f"rt_{int(n)}" for n in notionals]
    with open(csv_path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows_out[0].keys()))
        w.writeheader()
        for rec in rows_out:
            w.writerow({k: ("" if (isinstance(v, float) and not np.isfinite(v)) or v is None else v)
                        for k, v in rec.items()})

    def fmt_usd(v: float) -> str:
        return f"{v:,.0f}" if np.isfinite(v) else "n/a"

    def fmt_rt(v) -> str:
        return f"{v:.1f}" if v is not None and np.isfinite(v) else "n/a"

    lines = [
        f"# Cost probe — {date} (model-based, first-order)",
        "",
        "> Estimates from recorded band aggregates; assumptions and limitations in",
        "> `METHOD.md`. Observation only — no execution (G0 KILL stands).",
        "",
        f"- Sources: {len(rows_out)} symbols / {total_rows:,} rows; "
        f"fee {args.fee_bps:.2f} bps/side; skipped {len(skipped)} thin files",
        "- Model: uniform-within-band fill walk from best quote; "
        f"RT = 2x fee + buy fill + sell fill ({', '.join(f'${n:,.0f}' for n in notionals)})",
        "",
        "| symbol | rows | span h | spread bps | tob ask $ | d05 ask $ | d1 ask $ | "
        + " | ".join(f"RT ${n:,.0f} bps" for n in notionals) + " |",
        "|---|---:|---:|---:|---:|---:|---:|" + "---:|" * len(notionals),
    ]
    for rec in rows_out:
        lines.append(
            f"| {rec['symbol']} | {rec['rows']:,} | {rec['span_h']} | {rec['spread_bps']:.2f} | "
            f"{fmt_usd(rec['tob_ask_usd'])} | {fmt_usd(rec['d05_ask_usd'])} | {fmt_usd(rec['d1_ask_usd'])} | "
            + " | ".join(fmt_rt(rec.get(k)) for k in rt_keys) + " |"
        )

    agg = {}
    for key in ("spread_bps", "tob_ask_usd", "d05_ask_usd", "d1_ask_usd", "d2_ask_usd"):
        agg[key] = _med(np.array([r[key] for r in rows_out]))
    for k in rt_keys:
        vals = [r[k] for r in rows_out if r.get(k) is not None]
        agg[k] = float(np.median(vals)) if vals else float("nan")
    lines += [
        "",
        f"**Aggregate (median across {len(rows_out)} symbols):** spread {agg['spread_bps']:.2f} bps; "
        f"d05 ask {fmt_usd(agg['d05_ask_usd'])}; d1 ask {fmt_usd(agg['d1_ask_usd'])}; "
        + "; ".join(f"RT ${n:,.0f} = {fmt_rt(agg[f'rt_{int(n)}'])} bps" for n in notionals),
        "",
    ]
    if skipped:
        lines += [f"Skipped thin files: {', '.join(skipped)}", ""]
    md_path.write_text("\n".join(lines))

    print("\n".join(lines))
    print(f"wrote {csv_path} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
