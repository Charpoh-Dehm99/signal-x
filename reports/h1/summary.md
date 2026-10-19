# H1-coarse — run summary

- Run: 2026-10-17 18:00 UTC (30.6s), prereg sha256 `5961d6df2d8dbdd26345f3740ff57cbf116ec1879baee52bcb7a3d745fd96ad1`
- Corpus: 625 files, 237,090,883 rows; symbols included 625, skipped 0
- Candidate boundary bars (qv ok): 1,765,715; events kept: 1,761,874
- Event window: 2024-09-04 .. 2026-10-14
- Gate basis: 2 legs x 0.20%/leg = 0.40% (G0 stressed cost convention)

## Deciles (r8 = primary)
| decile | n | mean r4 | mean r8 | mean r12 |
|---:|---:|---:|---:|---:|
| 0 | 176,188 | +0.016% | -0.019% | -0.015% |
| 1 | 176,187 | +0.034% | +0.041% | +0.021% |
| 2 | 176,187 | -0.002% | +0.028% | +0.048% |
| 3 | 176,188 | -0.001% | -0.008% | -0.023% |
| 4 | 176,187 | -0.014% | +0.002% | -0.023% |
| 5 | 176,187 | -0.044% | -0.027% | -0.073% |
| 6 | 176,188 | -0.032% | -0.019% | +0.011% |
| 7 | 176,187 | +0.026% | +0.061% | +0.075% |
| 8 | 176,187 | +0.020% | +0.036% | +0.062% |
| 9 | 176,188 | +0.011% | +0.018% | +0.021% |

Primary spread S(8h) = **+0.0369%** (secondary: S(4h) -0.0044%, S(12h) +0.0356%)

## Halves
| half | spread r4 | spread r8 | spread r12 |
|---:|---:|---:|---:|
| 0 (earlier) | +0.0040% | +0.0559% | +0.0717% |
| 1 (later) | -0.0126% | +0.0188% | +0.0015% |

## Gate
| criterion | observed | required | result |
|---|---|---|:---:|
| Net spread S(8h) >= 0.40% | +0.0369% | >= 0.40% | FAIL |
| Split-half same sign, min |S_half(8h)| >= 0.20% | +0.0559% / +0.0188% | both same sign as S(8h) | FAIL |
| Breadth >= 60% same-sign symbols | 51.5% (303 qualifying) | >= 60% | FAIL |
| Monotonicity corr(decile, mean r8) >= 0.60 | 0.245 | >= 0.60 | FAIL |
| n(D0), n(D9) >= 500 | 176,188 / 176,188 | >= 500 each | PASS |

**Verdict: KILL** (single preregistered run; do not re-run)
