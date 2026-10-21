"""Live observation snapshot for one symbol: pump / plateau / resistance / squeeze.

This is a monitoring tool, not a validated signal: the Phase 1 G0 event study
found no short edge for the full chain (see reports/phase1/G0_VERDICT.md).
It answers: "is this coin still pumping, how compressed is it, how far is the
nearest resistance, and is the squeeze guard flashing?"

Fail-loud policy: malformed inputs raise LiveError. Futures-only inputs
(funding/OI) are optional; when absent the squeeze guard reports None
("unknown") instead of pretending the market is safe.
"""

from __future__ import annotations

from bisect import bisect_right

from .book import DepthSnapshot
from .compression import compression_metrics, momentum_decay_bias
from .divergence import (
    find_bearish_divergences,
    find_bullish_divergences,
    rsi_high_pivots,
    rsi_low_pivots,
)
from .flow import DEFAULT_FLOW_WINDOW, DEFAULT_LARGE_PRINT_USD, FlowError, flow_features
from .indicators import atr, pct_rank, roc, rsi
from .models import AggTrade, Kline
from .orderflow import oi_price_regime, squeeze_guard
from .pivots import fractal_pivots, last_swing
from .resistance import cluster_zones, distance_atr, nearest_resistance_zone


class LiveError(RuntimeError):
    """Malformed live snapshot input. Never swallow this."""


def _last_defined(seq: list[float | None]) -> float | None:
    return next((v for v in reversed(seq) if v is not None), None)


def _confirm_index(pivot: object) -> int:
    """Confirmation bar of a divergence pivot (Pivot or bare bar index)."""
    idx = getattr(pivot, "confirmed_index", None)
    if idx is None:
        idx = getattr(pivot, "index", None)
    if idx is None and isinstance(pivot, int) and not isinstance(pivot, bool):
        idx = pivot
    if isinstance(idx, bool) or not isinstance(idx, int):
        raise LiveError("divergence pivot missing a valid confirmation index")
    return idx


def _divergence_age(divs: list[tuple], n_bars: int) -> int | None:
    """Age in closed bars of the most recently confirmed divergence."""
    if not divs:
        return None
    confirmed = max(
        max(_confirm_index(d[1]), _confirm_index(d[3])) for d in divs
    )
    return max(0, n_bars - 1 - confirmed)


def volume_z(closed_qv: list[float], *, window: int = 168, min_periods: int = 120) -> float | None:
    """Z-score of the latest CLOSED hourly quote volume vs the prior `window` hours.

    Callers must pass closed bars only (drop the in-progress bar), otherwise a
    partial hour biases the z-score downward.
    """
    if not closed_qv:
        raise LiveError("volume_z: empty series")
    if len(closed_qv) < min_periods:
        return None
    x = float(closed_qv[-1])
    hist = [float(v) for v in closed_qv[-(window + 1):-1]]
    if len(hist) < 2:
        return None
    mean = sum(hist) / len(hist)
    var = sum((v - mean) ** 2 for v in hist) / len(hist)
    std = var ** 0.5
    if std <= 0:
        return None
    return (x - mean) / std


def live_snapshot(
    *,
    symbol: str,
    klines5m: list[Kline],
    hourly_qv: list[float] | None = None,
    change_24h_pct: float | None = None,
    funding_now: float | None = None,
    funding_history: list[float] | None = None,
    oi_now: float | None = None,
    oi_history: list[float] | None = None,
    oi_times: list[int] | None = None,
    atr_n: int = 14,
    pivot_k: int = 3,
    divergence_max_age_bars: int = 12,
    plateau_window: int = 36,
    max_height_atr: float = 4.5,
    min_containment: float = 0.5,
    pump_change_threshold: float = 15.0,
    pump_vol_z_threshold: float = 3.0,
    z_min_periods: int = 120,
    near_resistance_atr: float = 0.5,
    squeeze_funding_pctile: float = 90.0,
    agg_trades: list[AggTrade] | None = None,
    depth_snapshot: DepthSnapshot | None = None,
    flow_window: int = DEFAULT_FLOW_WINDOW,
    large_print_min_notional: float = DEFAULT_LARGE_PRINT_USD,
) -> dict:
    """Compose all engines into one observation snapshot (see module docstring)."""
    if len(klines5m) < 50:
        raise LiveError("live_snapshot: need >= 50 5m klines")

    closes = [k.close for k in klines5m]
    highs = [k.high for k in klines5m]
    price = closes[-1]

    atr_now = _last_defined(atr(klines5m, atr_n))
    if atr_now is None or atr_now <= 0:
        raise LiveError("live_snapshot: ATR undefined/zero (not enough bars?)")

    # --- pump state -------------------------------------------------------
    vol_z = volume_z(hourly_qv, min_periods=z_min_periods) if hourly_qv else None
    is_pump = bool(
        change_24h_pct is not None
        and change_24h_pct >= pump_change_threshold
        and vol_z is not None
        and vol_z >= pump_vol_z_threshold
    )

    # --- plateau / compression -------------------------------------------
    cm = None
    if len(klines5m) >= plateau_window:
        cm = compression_metrics(
            klines5m[-plateau_window:], plateau_window, atr_now,
            max_height_atr=max_height_atr, min_containment=min_containment,
        )

    # --- structure & resistance ------------------------------------------
    pivots = fractal_pivots(klines5m, pivot_k)
    zones = cluster_zones(pivots, atr_value=atr_now, now_index=len(klines5m) - 1)
    resist = nearest_resistance_zone(zones, price)
    resist_info = None
    if resist is not None:
        resist_info = {
            "low": resist.low,
            "high": resist.high,
            "strength": resist.strength,
            "touches": resist.touches,
            "dist_pct": (resist.low - price) / price,
            "dist_atr": distance_atr(price, resist.low, atr_now),
        }

    swing_low = last_swing(pivots, "L")
    swing_high = last_swing(pivots, "H")
    bos_down = bool(swing_low is not None and price < swing_low.price)
    bos_up = bool(swing_high is not None and price > swing_high.price)

    # volume ratio of the latest closed bar vs the prior 20 closed bars
    vol_ratio = None
    if len(klines5m) >= 21:
        base = [k.volume for k in klines5m[-21:-1]]
        mean_base = sum(base) / len(base)
        if mean_base > 0:
            vol_ratio = klines5m[-1].volume / mean_base

    try:
        flow = flow_features(
            klines5m,
            agg_trades=agg_trades,
            depth=depth_snapshot,
            window=flow_window,
            min_notional=large_print_min_notional,
        )
    except FlowError as exc:
        raise LiveError(str(exc)) from exc

    # --- momentum / divergence ---------------------------------------------
    rsi_vals = rsi(closes, 14)
    rsi_last = _last_defined(rsi_vals)
    price_highs = [p for p in pivots if p.kind == "H"]
    price_lows = [p for p in pivots if p.kind == "L"]
    divs = find_bearish_divergences(price_highs, rsi_high_pivots(rsi_vals, k=2))
    budivs = find_bullish_divergences(price_lows, rsi_low_pivots(rsi_vals, k=2))
    bear_age = _divergence_age(divs, len(klines5m))
    bull_age = _divergence_age(budivs, len(klines5m))
    has_div = bear_age is not None and bear_age <= divergence_max_age_bars
    has_budiv = bull_age is not None and bull_age <= divergence_max_age_bars
    if has_div and has_budiv:
        # Both cannot be current; keep the fresher signal (bearish wins ties).
        if bull_age < bear_age:
            has_div = False
        else:
            has_budiv = False
    decay_bias = momentum_decay_bias(roc(closes, 12), lookback=3)
    momentum_decay = decay_bias is not None

    # --- futures context (optional) ----------------------------------------
    funding_pctile = None
    if funding_now is not None and funding_history:
        funding_pctile = pct_rank(list(funding_history), funding_now)

    oi_regime = None
    squeeze_active: bool | None = None
    if funding_now is not None and funding_history and oi_now is not None and oi_history:
        hist = [v for v in oi_history if v is not None]
        # Align OI to the price window by timestamp: OI is often sampled at a
        # different period (e.g. 5m OI vs 1m bars), so counting bars back from
        # the end compares different lookbacks. Both deltas must start at the
        # same time.
        if oi_times and len(oi_times) == len(oi_history):
            open_times = [k.open_time for k in klines5m]
            start = bisect_right(oi_times, open_times[0]) - 1
            start = max(0, min(start, len(oi_times) - 1))
            ref_idx = max(0, min(bisect_right(open_times, oi_times[start]) - 1,
                                 len(klines5m) - 1))
            hist = [v for v in oi_history[start:] if v is not None]
        else:
            ref_idx = max(len(klines5m) - len(hist), 0)
        if len(hist) >= 2 and hist[0] > 0:
            delta_oi_pct = oi_now / hist[0] - 1.0
            ref_close = closes[ref_idx]
            delta_price_pct = price / ref_close - 1.0 if ref_close > 0 else 0.0
            at_highs = price >= 0.999 * max(highs[ref_idx:])
            oi_regime = oi_price_regime(delta_oi_pct, delta_price_pct, at_highs)
            squeeze_active = squeeze_guard(
                oi_rising=delta_oi_pct > 0,
                funding_pctile=funding_pctile,
                price_at_new_highs=at_highs,
                funding_threshold=squeeze_funding_pctile,
            )

    # --- events / state ------------------------------------------------------
    events: list[str] = []
    if resist_info is not None:
        if price >= resist_info["low"]:
            events.append("RESISTANCE_TOUCHED")
        elif resist_info["dist_atr"] <= near_resistance_atr:
            events.append("NEAR_RESISTANCE")
    if cm is not None and cm["qualifies"]:
        events.append("PLATEAU_ACTIVE")
    if bos_down:
        events.append("BOS_DOWN")
    elif bos_up:
        events.append("BOS_UP")
    if has_div:
        events.append("BEARISH_DIVERGENCE")
    if has_budiv:
        events.append("BULLISH_DIVERGENCE")
    if momentum_decay:
        events.append("MOMENTUM_DECAY")
    if squeeze_active:
        events.append("SQUEEZE_GUARD")
    if funding_pctile is not None and funding_pctile >= squeeze_funding_pctile:
        events.append("FUNDING_EXTREME")

    near_or_touched = "RESISTANCE_TOUCHED" in events or "NEAR_RESISTANCE" in events
    if squeeze_active:
        state = "SQUEEZE_RISK"
    elif near_or_touched and has_div:
        state = "ARMED"
    elif "PLATEAU_ACTIVE" in events:
        state = "PLATEAU"
    elif is_pump:
        state = "PUMP"
    else:
        state = "WATCH"

    return {
        "symbol": symbol,
        "price": price,
        "atr": atr_now,
        "change_24h_pct": change_24h_pct,
        "vol_z": vol_z,
        "is_pump": is_pump,
        "compression": cm,
        "n_zones": len(zones),
        "resistance": resist_info,
        "swing_low": swing_low.price if swing_low is not None else None,
        "swing_high": swing_high.price if swing_high is not None else None,
        "bos_down": bos_down,
        "bos_up": bos_up,
        "vol_ratio": vol_ratio,
        "rsi": rsi_last,
        "has_bearish_div": has_div,
        "has_bullish_div": has_budiv,
        "bearish_div_age": bear_age,
        "bullish_div_age": bull_age,
        "divergence_max_age_bars": divergence_max_age_bars,
        "momentum_decay": momentum_decay,
        "momentum_decay_bias": decay_bias,
        "funding_now": funding_now,
        "funding_pctile": funding_pctile,
        "oi_regime": oi_regime,
        "squeeze_active": squeeze_active,
        "flow": flow,
        "events": events,
        "state": state,
    }
