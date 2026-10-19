# SIGNAL-X - Phase 1 event study (G0 pilot)

Universe: 20 symbols: 1000FLOKIUSDT, 1000PEPEUSDT, ADAUSDT, ATOMUSDT, AVAXUSDT, DOGEUSDT, ENAUSDT, GALAUSDT, INJUSDT, JUPUSDT, LINKUSDT, NEARUSDT, OPUSDT, ORDIUSDT, SEIUSDT, SOLUSDT, SUIUSDT, TIAUSDT, WIFUSDT, XRPUSDT
Period: 2024-10 .. 2026-09 (monthly futures dumps)
Pump: 24h return >= 20% AND volume-z >= 3 (7d window), cooldown 48h.
Execution sim: 5m bars, entry next open, fees 5bps/side + slip 5bps/side, realized funding, stop-first on ambiguous bars, 24h time stop.
Note: PEPE/FLOKI trade as 1000PEPE/1000FLOKI on futures.

### Baseline forward returns (all pump events)

| horizon | n | mean | median | share<0 |
|---|---|---|---|---|
| +1h | 124 | +0.30% | -0.29% | 54% |
| +4h | 124 | +0.91% | +0.33% | 46% |
| +12h | 124 | +1.60% | -0.04% | 50% |
| +24h | 124 | +2.44% | -0.03% | 51% |
| +48h | 124 | +2.53% | -1.26% | 56% |
| +168h | 124 | +3.02% | -2.52% | 53% |

### Strategy - config `strict` (3xATR box, dwell>=24)

Events: 124 total, **0 triggered** (0%).

| metric | value |
|---|---|
| T1 hit before stop | 0.0% [0.0, 100.0] (Wilson 95%) |

### Strategy - config `primary` (4.5xATR box, dwell>=24)

Events: 124 total, **70 triggered** (56%).

| metric | value |
|---|---|
| T1 hit before stop | 45.7% [34.6, 57.3] (Wilson 95%) |
| median net (fees+slip+funding) | -1.16% |
| mean net | -0.14% |
| mean MFE / MAE | +1.84% / +1.98% |
| median bars to trigger (5m) | 916 |
| median stop distance | 2.0% |
| T2 (pre-event low) hit before stop | 0.0% [0.0, 5.3] (n=68) |

### Strategy - config `relaxed` (5xATR box, dwell>=24)

Events: 124 total, **120 triggered** (97%).

| metric | value |
|---|---|
| T1 hit before stop | 43.3% [34.8, 52.3] (Wilson 95%) |
| median net (fees+slip+funding) | -1.58% |
| mean net | -0.30% |
| mean MFE / MAE | +1.88% / +2.07% |
| median bars to trigger (5m) | 508 |
| median stop distance | 2.3% |
| T2 (pre-event low) hit before stop | 1.7% [0.5, 6.0] (n=117) |

### Path grid P(-X% before +Y%), primary config, raw (no costs)

| X\Y | +2% | +3% | +5% |
|---|---|---|---|
| -2% | 50% (n=70) | 60% (n=70) | 71% (n=70) |
| -3% | 41% (n=70) | 51% (n=70) | 60% (n=70) |
| -5% | 24% (n=70) | 34% (n=70) | 36% (n=70) |

### Cohorts by 24h pump size (primary, triggered)

| cohort | n | T1 hit | median net |
|---|---|---|---|
| 20-30% | 68 | 46% | -1.16% |
| 30-50% | 2 | 50% | +0.16% |

## Conditioned cohort (thesis exhaustion markers)

Funding percentile is measured at trigger entry (trailing 30d, min 60 ticks; unknown => excluded). Trigger window = 288 x 5m bars. Pump decile over the sample. 2x-cost column stresses fees+slip twice (-0.20% extra).

| subset | n | T1 hit [95% CI] | median net | mean net | mean net 2x-cost |
|---|---|---|---|---|---|
| all triggered | 70 | 45.7% [34.6, 57.3] | -1.16% | -0.14% | -0.34% |
| funding data available | 70 | 45.7% [34.6, 57.3] | -1.16% | -0.14% | -0.34% |
| funding >= P90 (trailing 30d) | 32 | 40.6% [25.5, 57.7] | -1.55% | -0.37% | -0.57% |
| trigger <= 24h after pump | 10 | 20.0% [5.7, 51.0] | -1.75% | -0.72% | -0.92% |
| pump size top decile | 5 | 40.0% [11.8, 76.9] | -1.85% | -0.53% | -0.73% |
| ALL THREE (thesis cohort) | 1 | 100.0% [20.7, 100.0] | +2.18% | +2.18% | +1.98% |

## Method notes
- Plateau gate recalibrated: blueprint's box-height <= 2 x ATR(5m) over 36 bars is
  empirically unsatisfiable (median best box = ~3.0 x ATR; 1h+ quiet streaks require ~4.5x).
  Configs strict/primary/relaxed = 3.0/4.5/5.0 x ATR(5m), containment >= 0.5, dwell >= 24 bars (2h).
- Pivots: fractal k=3, only confirmed pivots used (anti-lookahead).
- Simulated amounts are fractions of entry notional; no leverage/size modeling here.

## Caveats
- Pilot universe = liquid/mid-cap perps sampled 2026-10 (survivorship bias; full G0 needs all symbols incl. delisted).
- Events cluster in time (market-wide pump regimes) -> CIs overstate independence.
- Parameter choice was informed by this same data (selection risk); a walk-forward rerun is required before G1.
- Conditioning filters were derived from this same sample (in-sample); the full-universe rerun is the honest test.
