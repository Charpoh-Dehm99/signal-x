# SNIPER V3 — Mastermind multi-timeframe engine

Status: design + first implementation slice (2026-10-22). This is an observation
engine until an event study validates it. It does not place orders and it must
not be wired into the existing short sentinel's signal path.

## Thesis

Most retail multi-timeframe systems are three indicators stacked together. SNIPER V3 instead uses a
strict causal funnel:

1. **1H — permission.** Is there a directional structure regime with enough strength to trade?
2. **15m — location.** Is price at a meaningful zone, and has it swept/reclaimed that zone?
3. **5m — timing.** Has the 5m market actually displaced in the intended direction with participation?

A lower timeframe can veto but cannot create a trade. No 5m "perfect candle" is allowed to override
1H/15m context. Funding, open interest and taker flow are context/risk modifiers, not standalone
entry triggers.

## Causal rules

Every decision is made on **closed candles only**, using the bar's `close_time`.

- 1H and 15m state is updated only when those candles close.
- A 5m evaluation at time `t` may use 1H/15m candles whose `close_time <= t`.
- Pivots are confirmed only after `k` bars on each side; a pivot at index `i` is not visible at `i`.
- Zones, divergences, sweeps and BOS are tagged with the bar index at which they became visible.
- SETUP and TRIGGER expire after a fixed number of 5m bars; old levels cannot remain "fresh" forever.
- Entry is the **next 5m open** in backtests, not the close that produced the decision.
- Stop-first is assumed when stop and target are both touched in one candle.

## Feature definitions

Notation: `ATR_n` is Wilder ATR; `VWAP` and volume are quote-volume based where available.

### 1H structure bias

```text
pivots   = confirmed fractal pivots (k=2) on closed 1H candles
labels   = HH / HL / LH / LL for the last 6 confirmed pivots
up       = count(HH, HL)
down     = count(LH, LL)
bias     = "long" if up > down, "short" if down > up, else "neutral"
strength = abs(up - down) / max(1, up + down)
```

Hard gate: `bias != neutral` and `strength >= 0.30`. Neutral means no trade, even if 15m/5m look good.
The last confirmed 1H swing high/low defines structural invalidation and is used as a context feature.

The same structure view is computed on 15m. It is a soft alignment feature (`1.0` same,
`0.5` neutral, `0.0` opposite) and a **strong opposite 15m structure** (`strength >= 0.75`)
vetoes the setup. This prevents a 1H long from buying into a firmly bearish 15m leg while
still allowing a normal pullback/sweep at support.

### Zone book

Zones are built from confirmed 1H and 15m pivots, then merged when their centers are within
`zone_width_atr * ATR15`. Each merged zone gets a bounded score:

```text
touch_score = min(1, log1p(touches) / log1p(5))
recency     = exp(-age_hours / 12)
tf_weight   = 1.00 for a 1H-origin zone, 0.60 for 15m-only
volume_score= clamp(log1p(sum_quote_volume) / log1p(20 * median_quote_volume), 0, 1)
score       = 0.45*touch_score + 0.30*recency + 0.15*tf_weight + 0.10*volume_score
```

- Zones with `score < 0.25` are ignored.
- Longs require support at or below price; shorts require resistance at or above price.
- Distance from price to the near edge must be `<= 1.5 * ATR15`.
- A zone broken by more than `0.25 * ATR15` on a closed 15m candle is marked broken and cannot arm a setup.
- The next unbroken opposing zone is the first target; if there is none, use a 2R target.

### 15m sweep / reclaim

For a long, within the last 3 closed 15m candles:

```text
pierce  = low < zone.low
reclaim = close >= zone.low
wick    = (zone.low - low) / ATR15
quality = clamp(0.55*wick + 0.25*reclaim_strength + 0.20*volume_ratio_score, 0, 1)
```

Short is symmetric. `reclaim_strength = (close - zone.low) / max(ATR15, 1e-9)` for longs.
A sweep needs `pierce`, `reclaim`, and `quality >= 0.20`. This is a liquidity sweep, not merely a wick
below a line.

### 15m divergence

Use RSI(14) price pivots and RSI pivots:

- Bullish: price makes a lower low, RSI makes a higher low.
- Bearish: price makes a higher high, RSI makes a lower high.
- `strength = clamp((abs(price_delta)/ATR15) * (abs(rsi_delta)/10), 0, 1)`.
- Only the most recent confirmed divergence is used; it expires after 12 15m bars.

Divergence is a **score component**, not a standalone entry. A low-quality sweep with no divergence still
needs a strong reclaim/trigger to pass.

### 5m trigger

On the latest closed 5m candle:

```text
micro_bos     = close > max(high[-13:-1]) and prior close <= that level  (long)
                close < min(low[-13:-1])  and prior close >= that level   (short)
                (a recent confirmed 5m swing is also included as a level)
displacement  = (close-open)/ATR5                      (long; sign-flipped for short)
vol_ratio     = volume / mean(previous 20 volumes)
delta_z       = (2*taker_buy_quote - quote_volume) / rolling_std(delta, 20)
trigger_score = 0.40*clamp(displacement/1.5)
              + 0.30*clamp((vol_ratio-1)/2)
              + 0.30*clamp(abs(delta_z)/2)
```

Hard gates: `micro_bos`, `vol_ratio >= 1.10`, `trigger_score >= 0.35`.
The trigger must occur while a setup is active (setup TTL: 12 5m bars).

### Setup score

```text
setup_score = 0.45*zone.score
            + 0.35*sweep.quality
            + 0.20*(divergence.strength if present else 0)
```

Hard gate: `setup_score >= 0.35`; if there is no divergence, require `sweep.quality >= 0.55`.

## Mastermind ranker

The ranker is deliberately small and auditable. It is logistic, not a deep net:

```text
x = [htf_strength, zone_score, sweep_quality, divergence_strength,
     trigger_score, mtf_alignment, rr, vol_ratio, delta_z,
     funding_penalty, oi_penalty]
z = b + sum(w_i * x_i)
confidence = sigmoid(z)
EV_R = confidence * RR - (1 - confidence) - cost_R
```

Default weights are **priors**, not fitted parameters. A fitted model is only accepted after
walk-forward out-of-sample testing and a placebo-label test. Requiring `confidence >= 0.50` and
`EV_R > 0` is a filter, not proof of edge.

## Risk plan

```text
risk_per_unit = entry - stop            (long)
stop          = sweep_extreme - 0.25*ATR5
target1       = next opposing zone, or 2R
target2       = 3R
RR            = (target1 - entry) / risk_per_unit
size          = risk_budget / risk_per_unit
```

Hard gates: `RR >= 1.5`, `risk_per_unit <= 3.0*ATR5`, `risk_per_unit > 0`.
No averaging down, no widening a stop, no re-entry on the same zone without a new sweep.

## Filters outside the candle model

These are risk filters; none is a standalone signal:

- **Funding:** penalize longs when funding is in the top decile and shorts when in the bottom decile.
- **Open interest:** penalize crowded continuation when OI is at an extreme and price is extended.
- **Liquidations:** historical public REST liquidation data is unavailable; only forward `forceOrder`
  capture can be used. Do not invent historical liquidation features.
- **Macro/news:** high-impact scheduled events are a manual blackout window until a calendar feed exists.
- **Data quality:** stale mark price, missing taker fields, or insufficient warmup means **no decision**.

## Architecture in SIGNAL-X

```text
Binance REST/WS
      |
      v
CandleStore (1h, 15m, 5m, closed-only)
      |
      v
TF feature views (structure, zones, sweep, divergence, trigger)
      |
      v
SNIPER V3 Mastermind engine -> decision
      |
      +--> journal (separate event type, never SIGNAL)
      +--> /api/sniper/{symbol}
      +--> dashboard observation panel
```

Reuse:

- `signalx.feeds` for REST/market data;
- `signalx.indicators`, `signalx.pivots`, `signalx.structure` for causal primitives;
- `signalx.models.Kline` as the bar type;
- `signalx.journal` only after the event schema is explicitly extended.

Do not reuse the existing short engine's `SIGNAL` path or its unvalidated threshold. SNIPER decisions are
observations with their own schema and must be distinguishable from the legacy short signal.

## Validation protocol

1. **Unit/causality tests:** truncating future bars must not change an earlier decision.
2. **Component tests:** structure, zone merge/score, sweep, divergence, trigger, stop/target.
3. **Synthetic scenario tests:** a hand-built 1H up / 15m sweep / 5m reclaim produces a long; the mirror
   produces a short; a 5m breakout against a 1H downtrend produces nothing.
4. **Historical event study:** 1H/15m/5m replay, next-open entry, 5+5 bps costs, stop-first, fixed horizon.
5. **Baselines:** random entries matched by direction/hour/volatility; simple 5m BOS; legacy SIGNAL.
6. **Robustness:** per-symbol, per-month, funding regimes, exclusion of top-1% movers, parameter
   perturbation. Report confidence intervals, not just means.
7. **Promotion rule:** only after positive out-of-sample expectancy, stable per-symbol/month behaviour,
   and a placebo test that fails to reproduce the edge.

## Implemented slice

- `signalx/sniper_v3/models.py` — separate decision/zone/sweep/trigger types.
- `signalx/sniper_v3/primitives.py` — causal structure, merged zones, sweep,
  divergence, 5m trigger.
- `signalx/sniper_v3/scoring.py` — logistic Mastermind ranker (prior weights;
  optional `fit` for later walk-forward work).
- `signalx/sniper_v3/engine.py` — 1H permission → 15m location → 5m timing,
  risk plan and filters.
- `signalx/sniper_v3/data.py` — futures 1H/15m/5m REST snapshot + funding/OI
  context.
- `signalx/sniper_v3/backtest.py` — next-open entry, stop-first, costs, replay
  harness.
- `signalx/sniper_v3/assignments.py` — atomic, persistent watchlist of up to
  10 symbols (`data/sniper/assignments.json`).
- `signalx/sniper_v3/monitor.py` — in-process observer that wakes on assignment
  and re-evaluates assigned symbols at the next closed 5m bar
  (`data/sniper/latest.json`).
- `scripts/sniper_v3.py` — on-demand/looped observation scan.
- `GET /api/sniper/{symbol}` — API observation payload, including
  `diagnostics` with the first failed gate and reached checks.
- `GET|POST /api/sniper/watchlist`, `DELETE /api/sniper/watchlist/{symbol}` —
  assign, inspect, and remove monitored symbols.
- Dashboard right-rail SNIPER V3 card — assign form, watchlist badges,
  gate-by-gate diagnostics, on-demand evaluation for the loaded symbol, and a
  `NOTIFY` toggle for new LONG/SHORT transitions; no journal writes. `NOTIFY`
  uses the existing bell
  panel and, when the browser grants permission, a desktop notification. The
  dashboard page must be open for the client-side alert; the server monitor
  itself continues while the API process runs.
- 34 focused tests added; the full suite is 865 tests.

The first slice is intentionally uncalibrated. It can say "no setup"; it cannot
claim an edge.

## Known limitations

- The current repository has no historical 15m/5m parquet corpus; live API calls or a new download are
  required for a real backtest.
- Binance funding/OI history is available but not yet joined into a point-in-time feature store.
- The default ranker weights are priors and must not be presented as calibrated.
- This first slice is an observation engine, not an execution system.
