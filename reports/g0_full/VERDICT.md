# SIGNAL-X — G0 full-universe rerun: VERDICT

**Date:** 2026-10-17 · **Scope:** all 625 USDT-M perps with pump events (incl. delisted), 2024-10 .. 2026-09
**Data:** 5,785 screened pump events (1h); per-event 1m windows from the tight funnel plan; every funding
dump month for every symbol (11,943 tasks). 6,043/6,043 1m downloads, 0 errors, 625/625 parquet built.
**Run:** `scripts/run_funnel_study.py` (same plateau/simulation/conditioning code paths as the pilot).

## Verdict: KILL

Pre-registered criterion (blueprint §14): *conditioned expectancy ≤ 0 after 2× costs → stop the project.*

| thesis cohort (all three conditioners) | n | T1 hit [95% CI] | median net | mean net | **mean net @2× costs** |
|---|---|---|---|---|---|
| funding ≥ P90 (30d) ∧ trigger ≤ 24h ∧ pump top decile | 15 | 46.7% [24.8, 69.9] | −2.34% | −0.86% | **−1.06% → KILL** |

## The kill does not rest on n=15

Every conditioning subset is ≤ 0 after 2× costs, including the large ones:

| subset | n | median net | mean net 2×-cost |
|---|---|---|---|
| all triggered (primary config) | 3,021 | −0.32% | −0.30% |
| funding data available | 2,998 | −0.31% | −0.29% |
| funding ≥ P90 (trailing 30d) | 1,201 | −0.73% | −0.30% |
| trigger ≤ 24h after pump | 553 | −1.56% | −0.45% |
| pump size top decile | 314 | −1.33% | −0.65% |

Direction of failure matters: **funding-extreme adds exactly zero** (same −0.30% as baseline), and the
two other "exhaustion" markers make expectancy *worse*. The exhaustion-markers thesis is not merely
unsupported — the available evidence points the other way (the more exhausted the pump, the worse the
short side performs through this structure).

## Why the strategy bleeds (mechanism read)

- Entry is days late: median bars-to-trigger 829×5m ≈ **2.9 days** after the pump event. The raw
  post-pump drift (below) is spent by the time the chain fires.
- Geometry mismatch: T1 (measured move) hits ~50% — a coin flip before costs — while T2 (pre-event low,
  the fat-tail target) hits before stop only **5.9%** (n=2,754). The reversion tail is real but this
  structure almost never captures it.
- Raw drift is real (all 5,785 events, no costs): mean +24h **−0.66%**, median −4.42%, 64% negative;
  +168h median −8.96%, 67% negative. An index of pumps would have had short-side drift — the
  plateau+BOS entry converts a real drift into a minus-after-costs trade.
- Path grid (primary, raw): P(−3% before +3%) = 49% — symmetric; costs decide, and they decide against.

## Disposition (per project rules)

- **Do not proceed** to Phase 2 promotion (calibration/backtest-as-gate), G1/G2/G3, or any execution wiring.
- Observation tooling built alongside (sentinel state-machine wiring, signal journal, recorder, WS
  streamer w/ liquidation cascades) remains repository instrumentation, explicitly labeled
  "G0 killed — observation only". No live signal claim is supported by this dataset.
- Any future hypothesis needs a **new** pre-registered gate and an out-of-sample design; this dataset is
  now in-sample (thresholds were derived from the pilot, and this rerun read them back).

## Caveats

- Intersection cohort n=15 (CI spans 25–70% T1 hit) — cited for completeness, not as the load-bearing evidence.
- Events cluster in pump regimes → CIs overstate independence; 1h screen resolution misses sub-hour pumps.
- Screen/conditioning thresholds derived from pilot data (in-sample). The honest reading is: the rerun
  could not demonstrate conditioned edge, and the larger subsets are consistently negative.

## Artifacts

- `reports/g0_full/summary.md` — tables: baseline drift, 3 configs, path grid, cohorts, conditioning.
- `reports/g0_full/{events_screen.csv, events.csv, symbol_months.json}` — screen events, per-trigger rows, window plan.
- `logs/study_g0_full.log` — run log. Data: `data/parquet/{klines,klines_1h,funding}`.
