# H1-coarse Pre-registration — quarter-hour boundary flow imbalance

Status: **FROZEN — written 2026-10-17, before any H1 outcome computation was run.**
Nothing below may be edited after the first study run; deviations must be
recorded in `VERDICT.md`, not patched into this file.

**Amendment v1 (2026-10-17, pre-first-run):** data-accuracy correction — the
corpus was verified to be 1-minute bars (uniform 60 s spacing), not 5-minute;
"5m" references and bar offsets corrected to 1m (240/480/720 bars). No H1
outcome had been computed when this amendment was written. Boundary
condition, feature, deciling, gate, and costs are unchanged. The runner
re-verifies 60 s spacing per symbol and skips anomalous files (counted in
`summary.md`).

## Motivation

Hansen & Kim (2026) report a quarter-hour effect: crypto-futures order
imbalance measured in short (≈10 s) windows around xx:00/15/30/45 predicts
returns over the following 4–12 hours. H1-coarse tests the diluted version of
that claim that our existing corpus can actually support: imbalance measured
on the **1-minute bar beginning at the quarter-hour boundary**, using
`taker_buy_base/quote` fields already present in the kline parquets. It is a
cheap screen, not a replication.

Honest prior: the 1m window is ~6× the paper's but still fine-grained; the
effect may be diluted below costs. A KILL is a perfectly acceptable outcome
and closes the question at near-zero cost. No promoted signal, no
execution — observation only (G0 KILL stands).

## Data (fixed)

- Source: `data/parquet/klines/<SYMBOL>.parquet`, 625 USDT-M perpetual
  symbols, **1m bars (verified uniform 60 s spacing)**, corpus as built for
  G0 on 2026-10-16.
- Same in-sample corpus as G0. Not fresh OOS — a pass is a screening result
  only; confirmation would require future recorded data or purchased 1s data
  under a new pre-registration (H1b), never this file.
- Columns used: `open_time`, `close`, `quote_volume`, `taker_buy_quote`.

## Event definition (fixed)

Include a bar as an event iff **all** hold:

1. `open_time % 900_000 == 0` (boundary bar: xx:00/15/30/45 in UTC ms).
2. `quote_volume >= 100_000` USDT (floor to exclude dead books).
3. `0 <= taker_buy_quote <= quote_volume` (sanity; drop otherwise).
4. `close > 0`.
5. For each horizon h, the bar at `index + offset(h)` exists **and**
   `open_time[index + offset(h)] - open_time[index] == offset(h) * 60_000`
   (index-based offsets valid only if the series is contiguous; gaps are
   dropped, not bridged).

No other filters. No per-symbol filtering by activity level beyond the
quote-volume floor.

## Feature (fixed)

`imbal = (2 * taker_buy_quote - quote_volume) / quote_volume` ∈ [-1, 1]
computed on the boundary bar itself.

## Outcomes (fixed)

`r_h = close(t + h) / close(t) - 1`, close-to-close from the boundary bar
close, gross (no costs). Horizons: 4h / 8h / 12h = offsets 240 / 480 / 720 bars.

## Test design (fixed, single run)

- Global deciles: pooled `imbal` over all events; edges = quantiles
  0.1..0.9; decile of an event = searchsorted(edges, imbal, side="right")
  → 0..9. Edges are computed on the same corpus (no per-symbol fitting).
- Primary statistic: `S(h) = mean(r_h | D9) - mean(r_h | D0)`,
  **primary horizon 8h**; 4h/12h are reported as secondary, non-gating.
- Split halves: within each symbol, split its events at its own median event
  time; halves pooled across symbols.
- Breadth: symbols with ≥ 50 events in both D0 and D9; per-symbol spread
  `S_sym(8h) = mean(D9) - mean(D0)` using the global edges.

## Gate (all five must hold on the single run; no iteration, no tuning)

1. **Net spread**: `S(8h) >= 0.0040` — two legs (long D9, short D0) charged
   at G0's stressed cost basis (fees+slip ×2 = 0.20% per leg round trip).
2. **Split-half stability**: both halves same sign as S(8h) and
   `min(|S_half(8h)|) >= 0.0020`.
3. **Breadth**: ≥ 60% of qualifying symbols same sign as S(8h).
4. **Monotonicity**: Pearson corr(decile index 0..9, mean r8 per decile)
   ≥ 0.60.
5. **Sample**: ≥ 500 events in D0 and ≥ 500 in D9.

Any failure → **H1-coarse KILL**; the question is closed unless/until a
separately pre-registered 1s refinement (H1b) is funded and run. A pass is
"Screening PASS", not a tradeable edge: no backtester, no sizing, no
execution until a fresh-OOS confirmation under a new pre-registration.

## Cost model note

0.40% total = 2 legs × 0.20%/leg, mirroring G0's 2×-cost stress
(`signalx/conditioning.py` `extra_cost = 0.002`). The book-based cost model
(in progress, `reports/cost/METHOD.md`) may later show worse slippage for
this universe; a future confirmation run must restate costs accordingly.

## Outputs

- `reports/h1/deciles.csv` — decile, n, mean r4/r8/r12
- `reports/h1/halves.csv` — per-half D0/D9 means and spreads
- `reports/h1/symbols.csv` — per-symbol D0/D9 counts and spreads
- `reports/h1/summary.md` — run stats + gate table
- `reports/h1/VERDICT.md` — PASS/KILL + prereg sha256 (recorded at run time)

Implementation: `scripts/run_h1_study.py` (single pass over the corpus,
index-based offsets with the contiguity guard above).
