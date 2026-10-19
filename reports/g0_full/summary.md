# SIGNAL-X - G0 full-universe rerun (1h screen -> 1m event windows)

Universe: 625 symbols with pump events (full USDT-M perp dump universe incl. delisted).
Period: 2024-10 .. 2026-09 (1h screen), 1m windows [event-48h, event+14d].
Pump: 24h return >= 20% AND volume-z >= 3 (7d window), cooldown 48h (screen resolution 1h).
Execution sim: 5m bars, entry next open, fees 5bps/side + slip 5bps/side, realized funding, stop-first on ambiguous bars, 24h time stop.

Events: 5785; primary-config triggers: 3021.

### Baseline forward returns (all pump events)

| horizon | n | mean | median | share<0 |
|---|---|---|---|---|
| +1h | 5785 | -0.12% | -0.58% | 56% |
| +4h | 5785 | -0.26% | -1.32% | 58% |
| +12h | 5785 | -0.29% | -2.68% | 61% |
| +24h | 5785 | -0.66% | -4.42% | 64% |
| +48h | 5785 | -0.85% | -5.72% | 66% |
| +168h | 5785 | -0.03% | -8.96% | 67% |

### Strategy - config `strict` (3xATR box, dwell>=24)

Events: 5785 total, **17 triggered** (0%).

| metric | value |
|---|---|
| T1 hit before stop | 64.7% [41.3, 82.7] (Wilson 95%) |
| median net (fees+slip+funding) | +1.09% |
| mean net | +1.07% |
| mean MFE / MAE | +2.66% / +2.16% |
| median bars to trigger (5m) | 1107 |
| median stop distance | 4.3% |
| T2 (pre-event low) hit before stop | 6.7% [1.2, 29.8] (n=15) |

### Strategy - config `primary` (4.5xATR box, dwell>=24)

Events: 5785 total, **3021 triggered** (52%).

| metric | value |
|---|---|
| T1 hit before stop | 49.7% [47.9, 51.5] (Wilson 95%) |
| median net (fees+slip+funding) | -0.32% |
| mean net | -0.10% |
| mean MFE / MAE | +2.38% / +2.50% |
| median bars to trigger (5m) | 829 |
| median stop distance | 2.4% |
| T2 (pre-event low) hit before stop | 5.9% [5.1, 6.9] (n=2754) |

### Strategy - config `relaxed` (5xATR box, dwell>=24)

Events: 5785 total, **5137 triggered** (89%).

| metric | value |
|---|---|
| T1 hit before stop | 48.7% [47.4, 50.1] (Wilson 95%) |
| median net (fees+slip+funding) | -0.86% |
| mean net | -0.11% |
| mean MFE / MAE | +2.78% / +2.86% |
| median bars to trigger (5m) | 529 |
| median stop distance | 2.8% |
| T2 (pre-event low) hit before stop | 5.4% [4.8, 6.1] (n=4834) |

### Path grid P(-X% before +Y%), primary config, raw (no costs)

| X\Y | +2% | +3% | +5% |
|---|---|---|---|
| -2% | 50% (n=3021) | 60% (n=3021) | 71% (n=3021) |
| -3% | 39% (n=3021) | 49% (n=3021) | 60% (n=3021) |
| -5% | 26% (n=3021) | 34% (n=3021) | 43% (n=3021) |

### Cohorts by 24h pump size (primary, triggered)

| cohort | n | T1 hit | median net |
|---|---|---|---|
| 20-30% | 2278 | 50% | +0.65% |
| 30-50% | 586 | 48% | -1.10% |
| >50% | 157 | 49% | -0.31% |

## Conditioned cohort (thesis exhaustion markers)

Funding percentile is measured at trigger entry (trailing 30d, min 60 ticks; unknown => excluded). Trigger window = 288 x 5m bars. Pump decile over the sample. 2x-cost column stresses fees+slip twice (-0.20% extra).

| subset | n | T1 hit [95% CI] | median net | mean net | mean net 2x-cost |
|---|---|---|---|---|---|
| all triggered | 3021 | 49.7% [47.9, 51.5] | -0.32% | -0.10% | -0.30% |
| funding data available | 2998 | 49.7% [47.9, 51.5] | -0.31% | -0.09% | -0.29% |
| funding >= P90 (trailing 30d) | 1201 | 49.4% [46.6, 52.2] | -0.73% | -0.10% | -0.30% |
| trigger <= 24h after pump | 553 | 46.5% [42.4, 50.6] | -1.56% | -0.25% | -0.45% |
| pump size top decile | 314 | 46.5% [41.1, 52.0] | -1.33% | -0.45% | -0.65% |
| ALL THREE (thesis cohort) | 15 | 46.7% [24.8, 69.9] | -2.34% | -0.86% | -1.06% |

## Kill criterion

Conditioned cohort n=15; mean net after 2x costs = -1.06%.
**Verdict: KILL: conditioned expectancy <= 0 after 2x costs**

## Method notes
- Events are canonical screen outputs (1h dumps); per-event 1m windows come from the tight funnel plan
  (only the months each window touches; right-edge events use daily dumps).
- Funding percentile from plan_funding_all (every funding dump month for every symbol).
- Same plateau/simulation/conditioning code paths as the pilot (run_event_study.py).

## Caveats
- Screen is on 1h bars; a pump could in theory be missed between hour boundaries (matches blueprint resolution).
- Events cluster in time (market-wide pump regimes) -> CIs overstate independence.
- Conditioning thresholds were derived from the pilot sample (in-sample); this rerun is the honest sample.
