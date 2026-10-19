#!/usr/bin/env python3
"""Phase 1 event study: pump -> plateau -> BOS-down short outcomes, net of costs.

For every pump event (24h return >= 20%, volume-z >= 3) we measure:
  1. Baseline: forward returns at fixed horizons (unconditional reversion check).
  2. Conditional: plateau -> BOS trigger via signalx engines, then a short
     simulation with stop (swing high + 0.5 ATR), measured-move T1, pre-event-low
     T2, 24h time stop, fees + slippage + realized funding.

Plateau configs are swept because the blueprint's original "box <= 2 x ATR(5m)"
rule was empirically unsatisfiable (see reports/phase1/summary.md): the box gate
needs ~3.0-5.0 x ATR(5m) for real 2h+ consolidations. strict=3.0 documents the
original intent, primary=4.5, relaxed=5.0.

Outputs (reports/phase1/):
  events.csv          - every pump event with baseline + trigger + outcome fields
  summary.md          - aggregate tables with Wilson CIs and cohort splits

Usage:
  .venv/bin/python scripts/run_event_study.py --data-root data --out reports/phase1
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from signalx.conditioning import condition_events  # noqa: E402
from signalx.datafeed import read_parquet  # noqa: E402
from signalx.eventstudy import (  # noqa: E402
    EventStudyError,
    find_trigger,
    forward_returns,
    funding_cost,
    pump_events,
    resample_ohlcv,
    simulate_short,
    wilson_ci,
)

HORIZONS = (1, 4, 12, 24, 48, 168)
FEE_BPS = 5.0
SLIP_BPS = 5.0
MAX_WAIT_BARS = 2016      # 7 days on 5m bars
TIME_STOP_BARS = 288      # 24 hours on 5m bars
GRID = (2.0, 3.0, 5.0)    # X% down before Y% up

CONFIGS: dict[str, dict] = {
    "strict": {"max_height_atr": 3.0, "min_containment": 0.5, "min_dwell": 24},
    "primary": {"max_height_atr": 4.5, "min_containment": 0.5, "min_dwell": 24},
    "relaxed": {"max_height_atr": 5.0, "min_containment": 0.5, "min_dwell": 24},
}


def pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100.0 * x:+.2f}%"


def run_sim(bars, trig, target, funding, entry_idx, entry_ms) -> dict:
    sim = simulate_short(
        bars, entry_idx, trig["stop"], target, TIME_STOP_BARS,
        fee_bps=FEE_BPS, slip_bps=SLIP_BPS, stop_first=True,
    )
    exit_ms = int(bars["open_time"].iloc[sim["exit_idx"]]) + 300_000
    sim["funding"] = funding_cost(entry_ms, exit_ms, funding)
    sim["net_with_funding"] = sim["net_return"] + sim["funding"]
    return sim


def analyse_symbol(symbol: str, data_root: Path) -> list[dict]:
    kpath = data_root / "parquet" / "klines" / f"{symbol}.parquet"
    if not kpath.exists():
        print(f"[skip] {symbol}: no parquet", flush=True)
        return []
    df = read_parquet(kpath)
    hourly = resample_ohlcv(df, "1h")
    events = pump_events(hourly)
    fpath = data_root / "parquet" / "funding" / f"{symbol}.parquet"
    funding = read_parquet(fpath) if fpath.exists() else None
    print(f"[analyse] {symbol}: {len(df)} 1m bars, {len(events)} pump events", flush=True)

    rows: list[dict] = []
    ms1m = df["open_time"].to_numpy(dtype=np.int64)
    for ev in events.itertuples(index=False):
        row: dict = {
            "symbol": symbol,
            "event_ms": int(ev.open_time),
            "return_24h": float(ev.return_24h),
            "vol_z": float(ev.vol_z),
        }
        row.update(forward_returns(hourly, int(ev.idx), HORIZONS))

        lo_ms = int(ev.open_time) - 48 * 3_600_000
        hi_ms = int(ev.open_time) + 14 * 86_400_000
        a = int(np.searchsorted(ms1m, lo_ms, side="left"))
        b = int(np.searchsorted(ms1m, hi_ms, side="right"))
        window = df.iloc[a:b]
        if len(window) < 1_000:
            rows.append(row | {f"trig_{name}": False for name in CONFIGS})
            continue
        bars = resample_ohlcv(window, "5min")
        e_idx = int(np.searchsorted(bars["open_time"].to_numpy(), int(ev.open_time), side="left"))
        if e_idx >= len(bars) - 3:
            rows.append(row | {f"trig_{name}": False for name in CONFIGS})
            continue

        # T2 = pre-event consolidation low (24h before event)
        p0 = int(np.searchsorted(ms1m, int(ev.open_time) - 24 * 3_600_000, "left"))
        p1 = int(np.searchsorted(ms1m, int(ev.open_time), "right"))
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
    return rows


def table_baseline(df: pd.DataFrame) -> str:
    lines = ["### Baseline forward returns (all pump events)", "", "| horizon | n | mean | median | share<0 |", "|---|---|---|---|---|"]
    for h in HORIZONS:
        col = f"+{h}h"
        vals = df[col].dropna()
        if len(vals) == 0:
            continue
        lines.append(f"| {col} | {len(vals)} | {pct(vals.mean())} | {pct(vals.median())} | {100*(vals<0).mean():.0f}% |")
    return "\n".join(lines)


def table_strategy(df: pd.DataFrame, name: str) -> str:
    trig = df[df[f"trig_{name}"] & df.get(f"{name}_sim_outcome").notna()] if f"{name}_sim_outcome" in df.columns else df.iloc[0:0]
    n = len(trig)
    k = int((trig[f"{name}_sim_outcome"] == "target").sum()) if n else 0
    lo, hi = wilson_ci(k, n)
    lines = [
        f"### Strategy - config `{name}` ({CONFIGS[name]['max_height_atr']:g}xATR box, dwell>=24)",
        "",
        f"Events: {len(df)} total, **{n} triggered** ({100*n/max(len(df),1):.0f}%).",
        "",
        "| metric | value |",
        "|---|---|",
        f"| T1 hit before stop | {100*k/max(n,1):.1f}% [{100*lo:.1f}, {100*hi:.1f}] (Wilson 95%) |",
    ]
    if n:
        lines += [
            f"| median net (fees+slip+funding) | {pct(trig[f'{name}_net_funding'].median())} |",
            f"| mean net | {pct(trig[f'{name}_net_funding'].mean())} |",
            f"| mean MFE / MAE | {pct(trig[f'{name}_mfe'].mean())} / {pct(trig[f'{name}_mae'].mean())} |",
            f"| median bars to trigger (5m) | {trig[f'{name}_bars_since_event'].median():.0f} |",
            f"| median stop distance | {100*trig[f'{name}_stop_dist_pct'].median():.1f}% |",
        ]
    col2 = f"{name}_t2_net_funding"
    if col2 in df.columns:
        t2 = trig.dropna(subset=[col2])
        if len(t2):
            k2 = int((t2[f"{name}_t2_outcome"] == "target").sum())
            lo2, hi2 = wilson_ci(k2, len(t2))
            lines.append(f"| T2 (pre-event low) hit before stop | {100*k2/len(t2):.1f}% [{100*lo2:.1f}, {100*hi2:.1f}] (n={len(t2)}) |")
    return "\n".join(lines)


def table_grid(df: pd.DataFrame) -> str:
    trig = df[df["trig_primary"]] if "trig_primary" in df.columns else df.iloc[0:0]
    lines = ["### Path grid P(-X% before +Y%), primary config, raw (no costs)", "", "| X\\Y | " + " | ".join(f"+{y:g}%" for y in GRID) + " |", "|---|" + "---|" * len(GRID)]
    for x in GRID:
        cells = []
        for y in GRID:
            col = f"g_{x:g}_{y:g}"
            v = trig[col].dropna() if col in trig.columns else pd.Series(dtype=float)
            cells.append(f"{100*v.mean():.0f}% (n={len(v)})" if len(v) else "n/a")
        lines.append(f"| -{x:g}% | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def table_cohorts(df: pd.DataFrame) -> str:
    if "trig_primary" not in df.columns:
        return "_no triggered events_"
    trig = df[df["trig_primary"] & df["primary_sim_outcome"].notna()].copy()
    if trig.empty:
        return "_no triggered events_"
    bins = [0.0, 0.3, 0.5, np.inf]
    labels = ["20-30%", "30-50%", ">50%"]
    trig["cohort"] = pd.cut(trig["return_24h"], bins=bins, labels=labels, right=False)
    lines = ["### Cohorts by 24h pump size (primary, triggered)", "", "| cohort | n | T1 hit | median net |", "|---|---|---|---|"]
    for lab in labels:
        sub = trig[trig["cohort"] == lab]
        if len(sub) == 0:
            continue
        k = int((sub["primary_sim_outcome"] == "target").sum())
        lines.append(f"| {lab} | {len(sub)} | {100*k/len(sub):.0f}% | {pct(sub['primary_net_funding'].median())} |")
    return "\n".join(lines)


def table_conditioned(summary: dict) -> str:
    labels = [
        ("triggered", "all triggered"),
        ("funding_known", "funding data available"),
        ("funding_ge_p90", "funding >= P90 (trailing 30d)"),
        ("within_24h", "trigger <= 24h after pump"),
        ("top_decile", "pump size top decile"),
        ("conditioned", "ALL THREE (thesis cohort)"),
    ]
    lines = [
        "## Conditioned cohort (thesis exhaustion markers)",
        "",
        "Funding percentile is measured at trigger entry (trailing 30d, min 60 ticks; "
        "unknown => excluded). Trigger window = 288 x 5m bars. Pump decile over the sample. "
        "2x-cost column stresses fees+slip twice (-0.20% extra).",
        "",
        "| subset | n | T1 hit [95% CI] | median net | mean net | mean net 2x-cost |",
        "|---|---|---|---|---|---|",
    ]
    for key, label in labels:
        s = summary.get(key)
        if s is None:
            continue
        n = s["n"]
        if n == 0:
            lines.append(f"| {label} | 0 | - | - | - | - |")
            continue
        lo, hi = s["wilson"]
        lines.append(
            f"| {label} | {n} | {100 * s['t1_share']:.1f}% [{100 * lo:.1f}, {100 * hi:.1f}]"
            f" | {pct(s['median_net'])} | {pct(s['mean_net'])} | {pct(s['mean_net_2x'])} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-root", type=Path, default=ROOT / "data")
    ap.add_argument("--out", type=Path, default=ROOT / "reports" / "phase1")
    ap.add_argument("--symbols", default="", help="optional comma list; default = all parquet files")
    args = ap.parse_args(argv)

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = sorted(p.stem for p in (args.data_root / "parquet" / "klines").glob("*.parquet"))
    if not symbols:
        print("FATAL: no parquet data found", file=sys.stderr)
        return 1

    all_rows: list[dict] = []
    for sym in symbols:
        try:
            all_rows.extend(analyse_symbol(sym, args.data_root))
        except EventStudyError as exc:
            print(f"FATAL: {sym}: {exc}", file=sys.stderr)
            return 1

    df = pd.DataFrame(all_rows)
    args.out.mkdir(parents=True, exist_ok=True)

    if df.empty:
        print("WARNING: no events; skipping conditioning")
        cond: dict = {}
    else:
        def funding_loader(sym: str):
            path = args.data_root / "parquet" / "funding" / f"{sym}.parquet"
            return read_parquet(path) if path.exists() else None

        df, cond = condition_events(df, funding_fn=funding_loader)
    df.to_csv(args.out / "events.csv", index=False)

    md = [
        "# SIGNAL-X - Phase 1 event study (G0 pilot)",
        "",
        f"Universe: {len(symbols)} symbols: {', '.join(symbols)}",
        "Period: 2024-10 .. 2026-09 (monthly futures dumps)",
        "Pump: 24h return >= 20% AND volume-z >= 3 (7d window), cooldown 48h.",
        f"Execution sim: 5m bars, entry next open, fees {FEE_BPS:.0f}bps/side + slip {SLIP_BPS:.0f}bps/side, "
        "realized funding, stop-first on ambiguous bars, 24h time stop.",
        "Note: PEPE/FLOKI trade as 1000PEPE/1000FLOKI on futures.",
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
        "## Method notes",
        "- Plateau gate recalibrated: blueprint's box-height <= 2 x ATR(5m) over 36 bars is",
        "  empirically unsatisfiable (median best box = ~3.0 x ATR; 1h+ quiet streaks require ~4.5x).",
        "  Configs strict/primary/relaxed = 3.0/4.5/5.0 x ATR(5m), containment >= 0.5, dwell >= 24 bars (2h).",
        "- Pivots: fractal k=3, only confirmed pivots used (anti-lookahead).",
        "- Simulated amounts are fractions of entry notional; no leverage/size modeling here.",
        "",
        "## Caveats",
        "- Pilot universe = liquid/mid-cap perps sampled 2026-10 (survivorship bias; full G0 needs all symbols incl. delisted).",
        "- Events cluster in time (market-wide pump regimes) -> CIs overstate independence.",
        "- Parameter choice was informed by this same data (selection risk); a walk-forward rerun is required before G1.",
        "- Conditioning filters were derived from this same sample (in-sample); the full-universe rerun is the honest test.",
    ]
    (args.out / "summary.md").write_text("\n".join(md) + "\n")

    total_trig = {name: int(df[f"trig_{name}"].sum()) for name in CONFIGS if f"trig_{name}" in df.columns}
    print(f"[done] events={len(df)} triggers={total_trig}")
    if cond:
        c = cond.get("conditioned", {})
        print(
            f"[done] funded-known={cond['funding_known']['n']}/{cond['triggered']['n']}"
            f" conditioned={c.get('n', 0)}"
        )
    print(f"[done] report: {args.out / 'summary.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
