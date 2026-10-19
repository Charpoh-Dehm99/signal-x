# SIGNAL-X — G0 Verdict: Phase 1 Pilot (2026-10-16)

**Status: NOT PASSED** under this specification.
**Decision: do not proceed to Phase 3 (live alerts) on this signal as-is.**

## What was tested

- Universe: 20 liquid/mid-cap USDT perps (pilot sample, see `summary.md`).
- Period: 2024-10 .. 2026-09 (1m futures dumps, 18 × 1,051,200 rows, zero gaps).
- Event: 24h return >= 20% AND hourly volume-z >= 3 (7d), 48h cooldown. n = **124 events**.
- Chain: plateau (box <= K×ATR(5m), containment >= 0.5, dwell >= 2h) -> BOS close < confirmed
  swing low -> volume >= 1.5× mean20 -> short next open.
- Exits: stop = swing high + 0.5 ATR (median 2.0% away), T1 = measured move, T2 = pre-event low,
  24h time stop. Costs: 10 bps/side fees+slip round trip + realized funding. Stop-first on ties.

## Result 1 — No unconditional reversion

| horizon | mean | median | share<0 |
|---|---|---|---|
| +24h | +2.44% | -0.03% | 51% |
| +168h | +3.02% | -2.52% | 53% |

Median forward returns are ~zero. The positive mean is a **right tail of continuations**
(max +160% at +24h). Pumped liquid perps do not reliably dump within days.

## Result 2 — Conditional chain is a coin flip with negative expectancy

Primary config (4.5×ATR box): 70/124 triggered.

| metric | value |
|---|---|
| T1 hit before stop | 45.7% [34.6, 57.3] (Wilson 95%) |
| avg net win (T1) / avg net loss (stop) | +2.35% / -2.31% |
| **expectancy (mean net, incl. funding -0.10%)** | **-0.14% per trade** |
| median bars from pump to trigger | 916 (5m bars) = 3.2 days |
| T2 (pre-event low) hit before stop | 0.0% (n=68) — unreachable inside 24h |

Raw path grid P(-X% before +Y%) at X=Y: 50% (-2/+2), 51% (-3/+3), 36% (-5/+5).
After ~0.2% costs, the 50/50 grid is a guaranteed slow bleed.

## Result 3 — The chain fires late and late is generic

Early triggers (<=24h after pump): 20% T1 hit, -0.72% mean net (n=10).
Late triggers (>24h): 50% T1 hit, -0.04% mean net (n=60).

Median 3.2 days after the pump, the "plateau -> BOS" largely captures **generic consolidation
breakdowns**, not post-blow-off exhaustion. The thesis was about exhaustion; this implementation
did not isolate it.

## Result 4 — Blueprint parameter was unsatisfiable (corrected)

`box_height <= 2 x ATR(5m)` over 36 bars: 0/124 events could satisfy it (median best box
= ~3.0×ATR; 2h+ quiet streaks need ~4.5×). The original rule conflated a 3h box with a 5m ATR.
Configs were recalibrated to 3.0/4.5/5.0× empirically; strict config still triggers 0/124.

## Caveats (why this is not the final word)

- n=124 events / 70 triggers; events cluster in time (market-wide pump regimes) -> effective n much lower.
- Pilot universe has survivorship and selection bias; parameter choice saw this data (no walk-forward yet).
- No regime conditioning (BTC trend), no funding/OI squeeze filters in the trigger (thesis-relevant).

## Recommended next move (one bounded iteration)

1. **Full-universe rerun**: all USDT-M perps incl. delisted/settled symbols (1000s of events).
2. Condition the trigger on the thesis's actual exhaustion markers:
   funding percentile >= 90 (short-crowding guard inverted: block when funding already negative),
   blow-off timing (trigger window <= 24h), prior 7d pump percentile (top decile).
3. Kill criterion: if conditioned expectancy is still <= 0 after 2x costs on full universe, **stop the project**.
