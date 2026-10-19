# H1-coarse verdict: **KILL**

- Date: 2026-10-17 18:00 UTC; single registered run, 1,761,874 events, 625 symbols
- Prereg sha256: `5961d6df2d8dbdd26345f3740ff57cbf116ec1879baee52bcb7a3d745fd96ad1`
- Primary S(8h) = +0.0369% | halves +0.0559% / +0.0188% | breadth 51.5% | corr 0.245

Screening KILL. Primary S(8h) = +0.0369% vs required >= +0.40%. Failing criteria: Net spread S(8h) >= 0.40%; Split-half same sign, min |S_half(8h)| >= 0.20%; Breadth >= 60% same-sign symbols; Monotonicity corr(decile, mean r8) >= 0.60. The quarter-hour imbalance question is closed for this corpus unless a separately pre-registered 1s refinement (H1b) is funded and run — the 1m proxy does not carry a spread large enough to survive the two-leg stressed cost basis.

Secondary (non-gating): S(4h) = -0.0044%, S(12h) = +0.0356%.

Observation only — no execution (G0 KILL stands). Do not re-run this
study to tune thresholds; deviations go here, not in the prereg.

## Context notes (post-run, non-deviations)

1. The `qv >= 100k USDT` floor was registered when the bars were believed to
   be 5m; on verified 1m bars it selects only liquid minutes (kept
   1.76M/1.77M candidates ≈ 99.8%, but from the high-volume segment of
   roughly half the symbols). So the screen was run on a *more tradeable*
   universe than a mid-cap-wide test — the negative result holds a fortiori
   for less liquid names, not less so.
2. Decile means show no monotone gradient (local max at D7–D8, corr 0.245);
   the positive S(8h) of +0.037% gross is ~5 bps inside a single leg's
   stressed cost, let alone two. Shape is consistent with noise, not a
   diluted-but-real effect.
3. Process: a 5-symbol mechanics smoke (to /tmp, not registered results) was
   run after freeze and before the full run; it also KILLed in the same
   direction. The full run above is the single registered run.

