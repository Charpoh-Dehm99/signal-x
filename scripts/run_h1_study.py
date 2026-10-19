#!/usr/bin/env python3
"""H1-coarse study runner — pre-registered, single run.

Reads the 1m kline corpus, computes quarter-hour boundary flow-imbalance
deciles and forward-return spreads, evaluates the pre-registered gate, and
writes reports/h1/{deciles,halves,symbols}.csv + summary.md + VERDICT.md.

Pre-registration: reports/h1/PREREGISTRATION.md (sha256 recorded at run time).
This is the *registered* single run; do not re-run to tune. Any deviation is
recorded in VERDICT.md, never patched into the prereg.

Observation only — no execution, no live signal (G0 KILL stands).

Usage:
  .venv/bin/python scripts/run_h1_study.py [--limit N] [--out-dir DIR]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
import time
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data/parquet/klines"
PREREG = ROOT / "reports/h1/PREREGISTRATION.md"
DEFAULT_OUT = ROOT / "reports/h1"

BAR_MS = 60_000
BOUNDARY_MS = 900_000
MIN_QV = 100_000.0
HORIZONS = (("r4", 240), ("r8", 480), ("r12", 720))
MAX_OFF = max(off for _, off in HORIZONS)
MIN_SYM_EVENTS = 50
MIN_F60 = 0.99  # uniform 60s spacing check per symbol

GATE = {
    "net_spread_8h": 0.0040,        # 2 legs x 0.20%/leg stressed basis
    "half_min_spread_8h": 0.0020,
    "breadth_frac": 0.60,
    "monotonicity_corr": 0.60,
    "min_events_per_extreme": 500,
}

OUT_COLS = ("imbal", "r4", "r8", "r12", "half", "sym")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--limit", type=int, default=0,
                    help="only process the first N symbols (mechanics smoke; 0 = all)")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    return ap.parse_args(argv)


def symbol_events(path: Path, sym_id: int):
    """Per-symbol event arrays following the prereg, or (None, stats)."""
    stats: dict = {"symbol": path.stem, "rows": 0, "skip": None}
    tbl = pq.read_table(
        path, columns=["open_time", "close", "quote_volume", "taker_buy_quote"]
    )
    ot = tbl.column("open_time").to_numpy().astype(np.int64, copy=False)
    close = tbl.column("close").to_numpy().astype(np.float64, copy=False)
    qv = tbl.column("quote_volume").to_numpy().astype(np.float64, copy=False)
    tbq = tbl.column("taker_buy_quote").to_numpy().astype(np.float64, copy=False)
    if len(ot) != len(close) or len(ot) != len(qv) or len(ot) != len(tbq):
        raise ValueError(f"{path.name}: ragged columns")
    stats["rows"] = len(ot)
    order = np.argsort(ot, kind="stable")
    ot, close, qv, tbq = ot[order], close[order], qv[order], tbq[order]

    d = np.diff(ot)
    d = d[d > 0]
    f60 = float((d == BAR_MS).mean()) if len(d) else 0.0
    if f60 < MIN_F60:
        stats["skip"] = f"spacing f60={f60:.3f}"
        return None, stats
    n = len(ot)
    if n <= MAX_OFF:
        stats["skip"] = f"only {n} rows"
        return None, stats

    bmask = ot % BOUNDARY_MS == 0
    stats["n_boundary"] = int(bmask.sum())
    idx = np.flatnonzero(bmask)
    ok = (qv[idx] >= MIN_QV) & (tbq[idx] >= 0.0) & (tbq[idx] <= qv[idx]) & (close[idx] > 0.0)
    idx = idx[ok]
    stats["n_after_qv"] = int(len(idx))
    idx = idx[idx + MAX_OFF < n]

    keep = np.ones(len(idx), dtype=bool)
    for _, off in HORIZONS:
        keep &= ot[idx + off] - ot[idx] == off * BAR_MS
    idx = idx[keep]
    stats["n_kept"] = int(len(idx))
    if len(idx) == 0:
        return None, stats

    imbal = ((2.0 * tbq[idx] - qv[idx]) / qv[idx]).astype(np.float32)
    rets = {
        name: (close[idx + off] / close[idx] - 1.0).astype(np.float32)
        for name, off in HORIZONS
    }
    t = ot[idx]
    half = (t > np.median(t)).astype(np.uint8)
    sym = np.full(len(idx), sym_id, dtype=np.uint16)
    stats["first_ms"], stats["last_ms"] = int(t.min()), int(t.max())
    return (imbal, rets["r4"], rets["r8"], rets["r12"], half, sym), stats


def _means(vals: np.ndarray, grp: np.ndarray, ngroups: int):
    sums = np.bincount(grp, weights=vals.astype(np.float64), minlength=ngroups)
    cnts = np.bincount(grp, minlength=ngroups)
    means = sums / np.maximum(cnts, 1)
    return means, cnts


def main(argv=None) -> int:
    args = parse_args(argv)
    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(DATA_DIR.glob("*.parquet"))
    if args.limit:
        files = files[: args.limit]
    if not files:
        print(f"ERROR: no parquet files under {DATA_DIR}")
        return 2

    t0 = time.time()
    imbal_l, r4_l, r8_l, r12_l, half_l, sym_l = [], [], [], [], [], []
    names: list[str] = []
    skipped: list[tuple[str, str]] = []
    n_boundary_tot = n_qv_tot = n_kept_tot = rows_tot = 0
    first_ms = None
    last_ms = None

    for i, path in enumerate(files, 1):
        arrs, st = symbol_events(path, len(names))
        rows_tot += st["rows"]
        n_boundary_tot += st.get("n_boundary", 0)
        n_qv_tot += st.get("n_after_qv", 0)
        if st.get("skip"):
            skipped.append((st["symbol"], st["skip"]))
        if arrs is not None:
            names.append(st["symbol"])
            imbal_l.append(arrs[0])
            r4_l.append(arrs[1])
            r8_l.append(arrs[2])
            r12_l.append(arrs[3])
            half_l.append(arrs[4])
            sym_l.append(arrs[5])
            n_kept_tot += len(arrs[0])
            first_ms = st["first_ms"] if first_ms is None else min(first_ms, st["first_ms"])
            last_ms = st["last_ms"] if last_ms is None else max(last_ms, st["last_ms"])
        if i % 50 == 0 or i == len(files):
            print(f"… {i}/{len(files)} symbols, events kept {n_kept_tot:,}", flush=True)

    if not imbal_l:
        print("ERROR: no events after filters")
        return 2

    imbal = np.concatenate(imbal_l)
    r4 = np.concatenate(r4_l)
    r8 = np.concatenate(r8_l)
    r12 = np.concatenate(r12_l)
    half = np.concatenate(half_l)
    sym = np.concatenate(sym_l)
    n_events = len(imbal)
    print(f"events: {n_events:,} from {len(names)} symbols "
          f"({len(skipped)} skipped), kept/candidates {n_kept_tot:,}/{n_qv_tot:,}", flush=True)

    # --- deciles (global edges, searchsorted right => 0..9) ---
    edges = np.quantile(imbal, np.arange(1, 10) / 10.0)
    dec = np.searchsorted(edges, imbal, side="right").astype(np.uint8)

    m4, cnts = _means(r4, dec, 10)
    m8, _ = _means(r8, dec, 10)
    m12, _ = _means(r12, dec, 10)
    s8 = float(m8[9] - m8[0])
    s4 = float(m4[9] - m4[0])
    s12 = float(m12[9] - m12[0])

    # --- halves (split at each symbol's own median event time) ---
    hm4, hcnts = _means(r4, (half.astype(np.int64) * 10 + dec).astype(np.int64), 20)
    hm8, _ = _means(r8, (half.astype(np.int64) * 10 + dec).astype(np.int64), 20)
    hm12, _ = _means(r12, (half.astype(np.int64) * 10 + dec).astype(np.int64), 20)
    h_spread8 = [float(hm8[h * 10 + 9] - hm8[h * 10 + 0]) for h in (0, 1)]

    # --- breadth (global edges, per-symbol counts/means) ---
    n_syms = len(names)
    sm8, scnts = _means(r8, (sym.astype(np.int64) * 10 + dec).astype(np.int64), n_syms * 10)
    n0 = scnts[0::10].astype(np.int64)
    n9 = scnts[9::10].astype(np.int64)
    spread_sym = sm8[9::10] - sm8[0::10]
    qual = (n0 >= MIN_SYM_EVENTS) & (n9 >= MIN_SYM_EVENTS)
    s8_sign = np.sign(s8)
    same_sign = np.sign(spread_sym[qual]) == s8_sign
    breadth_frac = float(same_sign.mean()) if qual.any() else float("nan")

    # --- monotonicity ---
    with np.errstate(invalid="ignore"):
        corr = float(np.corrcoef(np.arange(10), m8)[0, 1]) if (cnts > 0).all() else float("nan")

    # --- gate (preregistered, exact) ---
    gate_results = [
        ("Net spread S(8h) >= 0.40%", f"{s8:+.4%}", s8 >= GATE["net_spread_8h"],
         f">= {GATE['net_spread_8h']:.2%}"),
        ("Split-half same sign, min |S_half(8h)| >= 0.20%",
         f"{h_spread8[0]:+.4%} / {h_spread8[1]:+.4%}",
         (s8_sign != 0 and np.sign(h_spread8[0]) == s8_sign
          and np.sign(h_spread8[1]) == s8_sign
          and min(abs(h_spread8[0]), abs(h_spread8[1])) >= GATE["half_min_spread_8h"]),
         "both same sign as S(8h)"),
        ("Breadth >= 60% same-sign symbols",
         f"{breadth_frac:.1%} ({int(qual.sum())} qualifying)",
         (breadth_frac >= GATE["breadth_frac"]) if np.isfinite(breadth_frac) else False,
         ">= 60%"),
        ("Monotonicity corr(decile, mean r8) >= 0.60",
         f"{corr:.3f}" if np.isfinite(corr) else "n/a",
         (corr >= GATE["monotonicity_corr"]) if np.isfinite(corr) else False, ">= 0.60"),
        ("n(D0), n(D9) >= 500",
         f"{int(cnts[0]):,} / {int(cnts[9]):,}",
         int(cnts[0]) >= GATE["min_events_per_extreme"] and int(cnts[9]) >= GATE["min_events_per_extreme"],
         ">= 500 each"),
    ]
    passed = all(g[2] for g in gate_results)
    verdict = "PASS" if passed else "KILL"

    prereg_hash = hashlib.sha256(PREREG.read_bytes()).hexdigest()
    utc = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    dur = time.time() - t0

    # --- write CSV outputs ---
    with open(out_dir / "deciles.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["decile", "n", "mean_r4", "mean_r8", "mean_r12"])
        for d in range(10):
            w.writerow([d, int(cnts[d]), f"{m4[d]:.6f}", f"{m8[d]:.6f}", f"{m12[d]:.6f}"])
    with open(out_dir / "halves.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["half", "n_d0", "n_d9", "spread_r4", "spread_r8", "spread_r12"])
        for h in (0, 1):
            w.writerow([h, int(hcnts[h * 10 + 0]), int(hcnts[h * 10 + 9]),
                        f"{hm4[h * 10 + 9] - hm4[h * 10 + 0]:.6f}",
                        f"{hm8[h * 10 + 9] - hm8[h * 10 + 0]:.6f}",
                        f"{hm12[h * 10 + 9] - hm12[h * 10 + 0]:.6f}"])
    order = np.argsort(-np.abs(spread_sym))
    with open(out_dir / "symbols.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "n_d0", "n_d9", "spread_r8"])
        for j in order:
            w.writerow([names[j], int(n0[j]), int(n9[j]), f"{spread_sym[j]:.6f}"])

    # --- summary.md ---
    mins = (lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))) if first_ms else (lambda ms: "n/a")
    lines = [
        "# H1-coarse — run summary",
        "",
        f"- Run: {utc} ({dur:.1f}s), prereg sha256 `{prereg_hash}`",
        f"- Corpus: {len(files)} files, {rows_tot:,} rows; symbols included {len(names)}, skipped {len(skipped)}",
        f"- Candidate boundary bars (qv ok): {n_qv_tot:,}; events kept: {n_events:,}",
        f"- Event window: {mins(first_ms)} .. {mins(last_ms)}" if first_ms else "- Event window: n/a",
        f"- Gate basis: 2 legs x 0.20%/leg = 0.40% (G0 stressed cost convention)",
        "",
        "## Deciles (r8 = primary)",
        "| decile | n | mean r4 | mean r8 | mean r12 |",
        "|---:|---:|---:|---:|---:|",
    ]
    for d in range(10):
        lines.append(f"| {d} | {int(cnts[d]):,} | {m4[d]:+.3%} | {m8[d]:+.3%} | {m12[d]:+.3%} |")
    lines += [
        "",
        f"Primary spread S(8h) = **{s8:+.4%}** (secondary: S(4h) {s4:+.4%}, S(12h) {s12:+.4%})",
        ""
    ]
    lines += [
        "## Halves",
        "| half | spread r4 | spread r8 | spread r12 |",
        "|---:|---:|---:|---:|",
        f"| 0 (earlier) | {hm4[9]-hm4[0]:+.4%} | {hm8[9]-hm8[0]:+.4%} | {hm12[9]-hm12[0]:+.4%} |",
        f"| 1 (later) | {hm4[19]-hm4[10]:+.4%} | {hm8[19]-hm8[10]:+.4%} | {hm12[19]-hm12[10]:+.4%} |",
        "",
        "## Gate",
        "| criterion | observed | required | result |",
        "|---|---|---|:---:|",
    ]
    for name, obs, ok, req in gate_results:
        lines.append(f"| {name} | {obs} | {req} | {'PASS' if ok else 'FAIL'} |")
    lines += ["", f"**Verdict: {verdict}** (single preregistered run; do not re-run)", ""]
    if skipped:
        lines += ["## Skipped symbols", "", "| symbol | reason |", "|---|---|"]
        lines += [f"| {s} | {r} |" for s, r in skipped[:50]]
        if len(skipped) > 50:
            lines.append(f"| … | {len(skipped)-50} more |")
    (out_dir / "summary.md").write_text("\n".join(lines))

    # --- VERDICT.md ---
    failing = [g[0] for g in gate_results if not g[2]]
    if passed:
        interp = (
            f"Screening PASS (in-sample). Primary S(8h) = {s8:+.4%} clears the 0.40% "
            "cost basis with all stability checks. This is NOT a tradeable edge: "
            "the corpus is the same in-sample window as G0. Next step would be a "
            "fresh-OOS confirmation under a new pre-registration (future recorded "
            "data or purchased 1s data); no backtester, no sizing, no execution "
            "until then."
        )
    else:
        interp = (
            f"Screening KILL. Primary S(8h) = {s8:+.4%} vs required >= +0.40%. "
            f"Failing criteria: {'; '.join(failing)}. The quarter-hour imbalance "
            "question is closed for this corpus unless a separately pre-registered "
            "1s refinement (H1b) is funded and run — the 1m proxy does not carry a "
            "spread large enough to survive the two-leg stressed cost basis."
        )
    verdict_lines = [
        f"# H1-coarse verdict: **{verdict}**",
        "",
        f"- Date: {utc}; single registered run, {n_events:,} events, {len(names)} symbols",
        f"- Prereg sha256: `{prereg_hash}`",
        f"- Primary S(8h) = {s8:+.4%} | halves {h_spread8[0]:+.4%} / {h_spread8[1]:+.4%} | "
        f"breadth {breadth_frac:.1%} | corr {corr:.3f}",
        "",
        interp,
        "",
        "Secondary (non-gating): S(4h) = " + f"{s4:+.4%}, S(12h) = {s12:+.4%}.",
        "",
        "Observation only — no execution (G0 KILL stands). Do not re-run this",
        "study to tune thresholds; deviations go here, not in the prereg.",
        "",
    ]
    (out_dir / "VERDICT.md").write_text("\n".join(verdict_lines))

    print("\n=== GATE ===")
    for name, obs, ok, req in gate_results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {obs} (req {req})")
    print(f"VERDICT: {verdict}  S(8h)={s8:+.4%}  halves={h_spread8[0]:+.4%}/{h_spread8[1]:+.4%} "
          f" breadth={breadth_frac:.1%} corr={corr:.3f}")
    print(f"wrote {out_dir}/{{deciles,halves,symbols}}.csv summary.md VERDICT.md ({dur:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
