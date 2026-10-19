#!/usr/bin/env python3
"""Full-universe G0 rerun: canonical screen events -> conditioned outcomes.

Unlike run_event_study.py (which re-detects pumps from whole-history 1m
parquet), this runner takes the canonical event list produced by the 1h screen
(scripts/screen_universe.py) and analyses each event from its 1m window
[event - 48h, event + 14d] as downloaded by the funnel plan. Forward returns
are time-addressed (the per-symbol parquet contains disjoint windows, so
positional indexing would be wrong).

Everything downstream reuses the pilot machinery: same plateau configs,
same short simulation (fees/slippage/funding, stop-first), same conditioning
(funding p90 / trigger <= 24h / top-decile pump) and the same report tables.

Kill criterion (G0 memo): conditioned expectancy <= 0 after 2x costs on the
full universe -> stop the project.

Usage:
  .venv/bin/python scripts/run_funnel_study.py --events reports/g0_full/events_screen.csv --out reports/g0_full
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from run_event_study import (  # noqa: E402
    CONFIGS,
    FEE_BPS,
    GRID,
    HORIZONS,
    MAX_WAIT_BARS,
    SLIP_BPS,
    TIME_STOP_BARS,
    pct,
    run_sim,
    table_baseline,
    table_cohorts,
    table_conditioned,
    table_grid,
    table_strategy,
)
from signalx.conditioning import condition_events  # noqa: E402
from signalx.datafeed import read_parquet  # noqa: E402
from signalx.eventstudy import (  # noqa: E402
    EventStudyError,
    find_trigger,
    resample_ohlcv,
    simulate_short,
)

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
LEAD_HOURS = 48
TRAIL_DAYS = 14


def analyse_symbol(symbol: str, data_root: str, events: list[dict]) -> tuple[list[dict], str | None]:
    """All study rows for one symbol's canonical events (+ missing-data note)."""
    kpath = Path(data_root) / "parquet" / "klines" / f"{symbol}.parquet"
    if not kpath.exists():
        return [], f"{symbol}: no 1m parquet"
    df = read_parquet(kpath)
    if df.empty or not df["open_time"].is_monotonic_increasing:
        raise EventStudyError(f"{symbol}: empty or unsorted 1m parquet")
    ms = df["open_time"].to_numpy(dtype=np.int64)

    fpath = Path(data_root) / "parquet" / "funding" / f"{symbol}.parquet"
    funding = read_parquet(fpath) if fpath.exists() else None

    rows: list[dict] = []
    for ev in events:
        e_ms = int(ev["open_time"])
        row: dict = {
            "symbol": symbol,
            "event_ms": e_ms,
            "return_24h": float(ev["return_24h"]),
            "vol_z": float(ev["vol_z"]),
            "quote_volume_24h": float(ev.get("quote_volume_24h", float("nan"))),
        }
        a = int(np.searchsorted(ms, e_ms - LEAD_HOURS * HOUR_MS, side="left"))
        b = int(np.searchsorted(ms, e_ms + TRAIL_DAYS * DAY_MS, side="right"))
        window = df.iloc[a:b]

        # Baseline forward returns: time-addressed 1h closes.
        for h in HORIZONS:
            row[f"+{h}h"] = None
        if len(window):
            hourly = resample_ohlcv(window, "1h")
            h_ms = hourly["open_time"].to_numpy(dtype=np.int64)
            h_close = hourly["close"].to_numpy(dtype=float)
            ib = int(np.searchsorted(h_ms, e_ms, side="left"))
            if ib < len(h_ms) and h_ms[ib] == e_ms:
                base = h_close[ib]
                for h in HORIZONS:
                    t = e_ms + h * HOUR_MS
                    j = int(np.searchsorted(h_ms, t, side="left"))
                    if j < len(h_ms) and h_ms[j] == t:
                        row[f"+{h}h"] = float(h_close[j]) / base - 1.0

        no_trig = {f"trig_{name}": False for name in CONFIGS}
        if len(window) < 1_000:
            rows.append(row | no_trig)
            continue
        bars = resample_ohlcv(window, "5min")
        e_idx = int(np.searchsorted(bars["open_time"].to_numpy(), e_ms, side="left"))
        if e_idx >= len(bars) - 3:
            rows.append(row | no_trig)
            continue

        # T2 = pre-event consolidation low (24h before event)
        p0 = int(np.searchsorted(ms, e_ms - 24 * HOUR_MS, "left"))
        p1 = int(np.searchsorted(ms, e_ms, "right"))
        pre = df.iloc[p0:p1]
        pre_low = float(pre["low"].min()) if len(pre) else None

        for name, cfg in CONFIGS.items():
            trig = find_trigger(bars, e_idx, max_wait_bars=MAX_WAIT_BARS, **cfg)
            row[f"trig_{name}"] = trig is not None
            if trig is None:
                continue
            entry_idx = trig["entry_idx"]
            entry_ms = int(bars["open_time"].iloc[entry_idx])
            pref = f"{name}_"
            row.update(
                {
                    pref + "bars_since_event": trig["bars_since_event"],
                    pref + "entry": trig["entry"],
                    pref + "stop": trig["stop"],
                    pref + "target": trig["target"],
                    pref + "stop_dist_pct": (trig["stop"] - trig["entry"]) / trig["entry"],
                    pref + "rr_t1": (trig["entry"] - trig["target"]) / (trig["stop"] - trig["entry"]),
                    pref + "vol_ratio": trig["vol_ratio"],
                    pref + "stop_dist_atr": trig["stop_dist_atr"],
                }
            )
            sim_t1 = run_sim(bars, trig, trig["target"], funding, entry_idx, entry_ms)
            row.update(
                {
                    pref + "sim_outcome": sim_t1["outcome"],
                    pref + "gross": sim_t1["gross_return"],
                    pref + "net_funding": sim_t1["net_with_funding"],
                    pref + "mfe": sim_t1["mfe"],
                    pref + "mae": sim_t1["mae"],
                    pref + "bars_held": sim_t1["bars_held"],
                }
            )
            if pre_low is not None and pre_low < trig["entry"]:
                sim_t2 = run_sim(bars, trig, pre_low, funding, entry_idx, entry_ms)
                row.update(
                    {
                        pref + "t2": pre_low,
                        pref + "t2_outcome": sim_t2["outcome"],
                        pref + "t2_net_funding": sim_t2["net_with_funding"],
                    }
                )
            if name == "primary":
                for x in GRID:
                    for y in GRID:
                        sg = simulate_short(
                            bars, entry_idx,
                            trig["entry"] * (1 + y / 100.0),
                            trig["entry"] * (1 - x / 100.0),
                            TIME_STOP_BARS, fee_bps=0.0, slip_bps=0.0,
                        )
                        row[f"g_{x:g}_{y:g}"] = 1 if sg["outcome"] == "target" else 0
        rows.append(row)
    return rows, None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--events", type=Path, default=ROOT / "reports" / "g0_full" / "events_screen.csv")
    ap.add_argument("--data-root", type=Path, default=ROOT / "data")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "g0_full")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--symbols", default="", help="optional comma list (debug/smoke runs)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.events.exists():
        print(f"FATAL: events file not found: {args.events}", file=sys.stderr)
        return 1
    events = pd.read_csv(args.events)
    for col in ("symbol", "open_time", "return_24h", "vol_z"):
        if col not in events.columns:
            print(f"FATAL: events file missing column {col!r}", file=sys.stderr)
            return 1
    if args.symbols:
        keep = {s.strip() for s in args.symbols.split(",") if s.strip()}
        events = events[events["symbol"].isin(keep)]
    if events.empty:
        print("FATAL: no events to analyse", file=sys.stderr)
        return 1

    by_symbol = {
        sym: grp.to_dict("records") for sym, grp in events.groupby("symbol", sort=True)
    }
    print(f"[study] {len(events)} events across {len(by_symbol)} symbols, workers={args.workers}", flush=True)

    rows: list[dict] = []
    missing: list[str] = []
    done = 0
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(analyse_symbol, sym, str(args.data_root), recs): sym
            for sym, recs in by_symbol.items()
        }
        for fut in as_completed(futures):
            sym = futures[fut]
            sym_rows, note = fut.result()
            rows.extend(sym_rows)
            if note:
                missing.append(note)
            done += 1
            if done % 50 == 0 or done == len(by_symbol):
                print(f"[study] {done}/{len(by_symbol)} symbols, {len(rows)} events analysed", flush=True)

    if missing:
        print(f"[study] {len(missing)} symbols without data (first 10): {missing[:10]}", flush=True)
    df = pd.DataFrame(rows)
    if df.empty:
        print("FATAL: no rows produced", file=sys.stderr)
        return 1
    df = df.sort_values(["symbol", "event_ms"]).reset_index(drop=True)

    def funding_loader(sym: str):
        path = args.data_root / "parquet" / "funding" / f"{sym}.parquet"
        return read_parquet(path) if path.exists() else None

    df, cond = condition_events(df, funding_fn=funding_loader)
    args.out.mkdir(parents=True, exist_ok=True)
    df.to_csv(args.out / "events.csv", index=False)

    triggered = int(df["trig_primary"].sum())
    c = cond.get("conditioned", {})
    verdict = "INCONCLUSIVE (n=0)"
    if c.get("n", 0):
        verdict = "KILL: conditioned expectancy <= 0 after 2x costs" if (c["mean_net_2x"] or 0) <= 0 else "CANDIDATE: conditioned expectancy > 0 after 2x costs"

    md = [
        "# SIGNAL-X - G0 full-universe rerun (1h screen -> 1m event windows)",
        "",
        f"Universe: {len(by_symbol)} symbols with pump events (full USDT-M perp dump universe incl. delisted).",
        "Period: 2024-10 .. 2026-09 (1h screen), 1m windows [event-48h, event+14d].",
        "Pump: 24h return >= 20% AND volume-z >= 3 (7d window), cooldown 48h (screen resolution 1h).",
        f"Execution sim: 5m bars, entry next open, fees {FEE_BPS:.0f}bps/side + slip {SLIP_BPS:.0f}bps/side, "
        "realized funding, stop-first on ambiguous bars, 24h time stop.",
        "",
        f"Events: {len(df)}; primary-config triggers: {triggered}.",
        "",
        table_baseline(df),
        "",
        table_strategy(df, "strict"),
        "",
        table_strategy(df, "primary"),
        "",
        table_strategy(df, "relaxed"),
        "",
        table_grid(df),
        "",
        table_cohorts(df),
        "",
        table_conditioned(cond) if cond else "## Conditioned cohort\n\n_no events_",
        "",
        "## Kill criterion",
        "",
        f"Conditioned cohort n={c.get('n', 0)}; mean net after 2x costs = "
        f"{pct(c.get('mean_net_2x')) if c.get('n', 0) else 'n/a'}.",
        f"**Verdict: {verdict}**",
        "",
        "## Method notes",
        "- Events are canonical screen outputs (1h dumps); per-event 1m windows come from the tight funnel plan",
        "  (only the months each window touches; right-edge events use daily dumps).",
        "- Funding percentile from plan_funding_all (every funding dump month for every symbol).",
        "- Same plateau/simulation/conditioning code paths as the pilot (run_event_study.py).",
        "",
        "## Caveats",
        "- Screen is on 1h bars; a pump could in theory be missed between hour boundaries (matches blueprint resolution).",
        "- Events cluster in time (market-wide pump regimes) -> CIs overstate independence.",
        "- Conditioning thresholds were derived from the pilot sample (in-sample); this rerun is the honest sample.",
    ]
    if missing:
        md += ["", f"Missing-data symbols ({len(missing)}): {', '.join(s.split(':')[0] for s in missing)}"]
    (args.out / "summary.md").write_text("\n".join(md) + "\n")

    print(f"[done] events={len(df)} triggers={triggered} funded_known={cond['funding_known']['n']} conditioned={c.get('n', 0)}")
    print(f"[done] conditioned mean net 2x-cost={pct(c.get('mean_net_2x')) if c.get('n', 0) else 'n/a'} -> {verdict}")
    print(f"[done] report: {args.out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
