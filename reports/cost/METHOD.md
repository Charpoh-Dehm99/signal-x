# Cost/impact methodology — recorded books vs the G0 stress basis

Status: v1 (2026-10-17), day-one data. Observation only — G0 KILL stands;
this document calibrates cost realism for any future pre-registered study.

## Purpose

Both pre-registered gates (G0 pump-exhaustion, H1 quarter-hour imbalance)
charged costs using G0's stressed basis: fees+slip x2 = **0.20% per leg
round trip** (`signalx/conditioning.py` `extra_cost = 0.002`, 2-leg charge
0.40% in H1). This note checks that basis against measured books.

## Data

- `data/live/book/*.parquet` — bookTicker + depth20 snapshots at 500 ms,
  59 symbols, 1.22M rows at probe time; spans up to ~6.4 h (some files
  restarted mid-afternoon, 0.4 h).
- Recorded features: quoted spread (bps), top-of-book notional, quote
  notional within +/-0.5% / 1% / 2% of mid (top-20 lower bound), per-side.
- Probe output: `reports/cost/probe-2026-10-17.{csv,md}`.

## Model (first-order, deliberately simple)

`signalx/costmodel.py` — uniform-within-band fill walk: liquidity in each
recorded band is assumed uniform in price, fills walk outward from the best
quote; VWAP distance from mid = per-side cost; round trip =
2 x taker fee + buy-side fill + sell-side fill. Fee 5 bps/side (Binance
USDS-M taker, no discounts).

## Measured (medians across 59 symbols)

| metric | value |
|---|---|
| quoted spread | 3.15 bps |
| depth within 0.5% (ask) | $9,615 |
| depth within 1% (ask) | $10,098 |
| RT $2,000 | 23.3 bps |
| RT $5,000 | 32.5 bps |
| RT $10,000 | 36.0 bps |

Wide dispersion: mega-caps near-free (CLUSDT 1 bps spread, $2M in-band,
RT ~11 bps); the median watchlist symbol is thin — a $5k clip pays ~33 bps
round trip, of which 10 bps is fees alone.

## Calibration vs the stress basis

- H1 gate charged 2 legs x 20 bps = 40 bps. Measured single-leg RT (model,
  quiet conditions) = 23-36 bps -> a real 2-leg trade on comparable symbols
  would cost ~46-72 bps. **The gate was, if anything, generous — the H1
  KILL stands a fortiori.**
- For the median symbol, fees alone (10 bps RT) exceed H1's observed gross
  spread (+3.7 bps) by ~3x, before any impact or timing cost.

## Limitations (all conservative-leaning reads)

- Band aggregates, not price ladders: impact is model-based. Top-20 gives a
  lower bound on depth, which overstates modelled impact somewhat; the
  order-of-magnitude conclusions (fees > gross edge; stress ~= measured)
  are robust to that.
- Quiet-market sample. Pump/event-time books are worse (wider, thinner) —
  event-day execution costs can only exceed these numbers.
- Single venue (Binance USDS-M), single quiet afternoon window.

## Upgrade path

1. Extend the book recorder to also persist 5-10 price levels for the
   watchlist (replaces the uniform-band model with a walked ladder).
2. For specific event windows, consider tick L2 purchase; not needed for
   gating, which the conservative basis already handles.
