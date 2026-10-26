"""Causal feature primitives for SNIPER V3.

Everything here consumes closed candles. Pivots are requested with
`last_index=len(klines)-1`, so a pivot is visible only after its confirmation
bar. No function reaches beyond the supplied list.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from signalx.divergence import (
    find_bearish_divergences,
    find_bullish_divergences,
    rsi_high_pivots,
    rsi_low_pivots,
)
from signalx.indicators import atr, rsi, stddev
from signalx.models import Kline
from signalx.pivots import fractal_pivots, last_swing
from signalx.structure import detect_choch_events, label_structure

from .models import DivergenceView, StructureView, SweepView, TriggerView, ZoneView


class SniperError(RuntimeError):
    """Malformed input or insufficient data for a SNIPER computation."""


def _last_defined(values: list[float | None]) -> float | None:
    return next((v for v in reversed(values) if v is not None), None)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def require_atr(klines: list[Kline], n: int = 14) -> float:
    if len(klines) < n + 2:
        raise SniperError(f"need >= {n + 2} closed bars for ATR")
    value = _last_defined(atr(klines, n))
    if value is None or value <= 0:
        raise SniperError("ATR undefined or zero")
    return float(value)


def structure_view(
    klines: list[Kline],
    *,
    pivot_k: int = 2,
    label_window: int = 6,
) -> StructureView:
    """Directional structure from confirmed 1H/15m pivots.

    Bias is deliberately conservative: at least two directional labels are
    needed, and the strength is the net directional label share.
    """
    if len(klines) < (2 * pivot_k) + 3:
        raise SniperError("not enough bars for structure")
    close = klines[-1].close
    pivots = fractal_pivots(klines, pivot_k, last_index=len(klines) - 1)
    labeled = label_structure(pivots)
    directional = [lp.label for lp in labeled if lp.label in ("HH", "HL", "LH", "LL")]
    recent = directional[-label_window:]
    up = sum(1 for label in recent if label in ("HH", "HL"))
    down = sum(1 for label in recent if label in ("LH", "LL"))

    if up + down < 2:
        bias = "neutral"
        strength = 0.0
    elif up > down:
        bias = "long"
        strength = (up - down) / max(2, up + down)
    elif down > up:
        bias = "short"
        strength = (down - up) / max(2, up + down)
    else:
        bias = "neutral"
        strength = 0.0

    high = last_swing(pivots, "H", len(klines) - 1)
    low = last_swing(pivots, "L", len(klines) - 1)
    bos: str | None = None
    if high is not None and close > high.price:
        bos = "up"
    elif low is not None and close < low.price:
        bos = "down"

    choch: str | None = None
    events = detect_choch_events(labeled)
    if events:
        direction, lp = events[-1]
        if len(klines) - 1 - lp.pivot.confirmed_index <= 6:
            choch = direction

    return StructureView(
        bias=bias,  # type: ignore[arg-type]
        strength=strength,
        last_high=high.price if high is not None else None,
        last_low=low.price if low is not None else None,
        close=close,
        bos=bos,  # type: ignore[arg-type]
        choch=choch,  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class _ZonePoint:
    price: float
    tf: str
    ts: int
    volume: float


def _quote_volume(klines: list[Kline]) -> float:
    median = sorted(max(k.quote_volume, 0.0) for k in klines)[len(klines) // 2]
    return median if median > 0 else 1.0


def build_zones(
    klines_1h: list[Kline],
    klines_15m: list[Kline],
    *,
    atr_value: float,
    now_ms: int,
    pivot_k: int = 2,
    merge_width_atr: float = 0.35,
    half_life_hours: float = 12.0,
    min_touches: int = 2,
) -> list[ZoneView]:
    """Merge confirmed 1H/15m pivots into bounded-score price zones."""
    if atr_value <= 0:
        raise SniperError("zone ATR must be positive")
    if merge_width_atr <= 0:
        raise SniperError("merge_width_atr must be positive")

    points: list[_ZonePoint] = []
    medians: list[float] = []
    for tf, klines in (("1h", klines_1h), ("15m", klines_15m)):
        if len(klines) < (2 * pivot_k) + 3:
            continue
        medians.append(_quote_volume(klines))
        for p in fractal_pivots(klines, pivot_k, last_index=len(klines) - 1):
            points.append(
                _ZonePoint(
                    price=p.price,
                    tf=tf,
                    ts=klines[p.index].close_time,
                    volume=max(klines[p.index].quote_volume, 0.0),
                )
            )
    if not points:
        return []

    base_volume = max(medians) if medians else 1.0
    merge_dist = merge_width_atr * atr_value
    points.sort(key=lambda p: p.price)

    clusters: list[list[_ZonePoint]] = [[points[0]]]
    for point in points[1:]:
        current = clusters[-1]
        mean = sum(p.price for p in current) / len(current)
        if abs(point.price - mean) <= merge_dist:
            current.append(point)
        else:
            clusters.append([point])

    zones: list[ZoneView] = []
    for cluster in clusters:
        touches = len(cluster)
        has_1h = any(p.tf == "1h" for p in cluster)
        if touches < min_touches and not has_1h:
            continue
        low = min(p.price for p in cluster)
        high = max(p.price for p in cluster)
        last_ts = max(p.ts for p in cluster)
        age_h = max(0.0, (now_ms - last_ts) / 3_600_000.0)
        recency = math.exp(-age_h / max(half_life_hours, 0.1))
        touch_score = min(1.0, math.log1p(touches) / math.log1p(5))
        tf_weight = 1.0 if has_1h else 0.6
        volume = sum(p.volume for p in cluster)
        volume_scale = max(base_volume * 5.0, 1.0)
        volume_score = _clamp(math.log1p(volume) / math.log1p(volume_scale))
        score = (
            0.45 * touch_score
            + 0.30 * recency
            + 0.15 * tf_weight
            + 0.10 * volume_score
        )
        zones.append(
            ZoneView(
                low=low,
                high=high,
                tf="1h" if has_1h else "15m",
                touches=touches,
                score=_clamp(score),
                last_ts=last_ts,
                volume=volume,
            )
        )
    zones.sort(key=lambda z: z.score, reverse=True)
    return zones


def zone_distance_atr(price: float, zone: ZoneView, atr_value: float) -> float:
    if atr_value <= 0:
        raise SniperError("zone distance ATR must be positive")
    if zone.low <= price <= zone.high:
        return 0.0
    return min(abs(price - zone.low), abs(price - zone.high)) / atr_value


def nearest_zone(
    zones: list[ZoneView],
    price: float,
    direction: str,
    *,
    atr_value: float,
    max_distance_atr: float = 1.5,
) -> ZoneView | None:
    """Closest support (long) or resistance (short) within the distance cap."""
    if direction not in ("long", "short"):
        raise SniperError("direction must be long or short")
    candidates: list[tuple[float, ZoneView]] = []
    for zone in zones:
        if direction == "long":
            if zone.low > price:
                continue
            distance = max(0.0, price - zone.high)
        else:
            if zone.high < price:
                continue
            distance = max(0.0, zone.low - price)
        if distance / atr_value <= max_distance_atr:
            candidates.append((distance, zone))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], -item[1].score))
    return candidates[0][1]


def next_target_zone(
    zones: list[ZoneView],
    price: float,
    direction: str,
) -> ZoneView | None:
    """Nearest opposing zone beyond price, ignoring zones containing price."""
    if direction == "long":
        above = [z for z in zones if z.low > price]
        return min(above, key=lambda z: z.low) if above else None
    below = [z for z in zones if z.high < price]
    return max(below, key=lambda z: z.high) if below else None


def zone_broken(
    zone: ZoneView,
    klines_15m: list[Kline],
    direction: str,
    *,
    atr_value: float,
    buffer_atr: float = 0.25,
) -> bool:
    """True when a closed 15m bar has closed decisively through the zone."""
    if not klines_15m:
        return True
    close = klines_15m[-1].close
    if direction == "long":
        return close < zone.low - buffer_atr * atr_value
    return close > zone.high + buffer_atr * atr_value


def detect_sweep(
    klines: list[Kline],
    zone: ZoneView,
    direction: str,
    *,
    atr_value: float,
    lookback: int = 3,
    min_quality: float = 0.2,
) -> SweepView | None:
    """Liquidity sweep through a zone followed by a close back inside/above it."""
    if atr_value <= 0:
        raise SniperError("sweep ATR must be positive")
    if lookback < 1:
        raise SniperError("lookback must be positive")
    if len(klines) < 22:
        return None
    start = max(1, len(klines) - lookback)
    best: SweepView | None = None
    for i in range(start, len(klines)):
        bar = klines[i]
        base = [k.volume for k in klines[i - 20:i]]
        mean_volume = sum(base) / len(base) if base else 0.0
        vol_ratio = bar.volume / mean_volume if mean_volume > 0 else 1.0
        vol_score = _clamp((vol_ratio - 1.0) / 2.0)
        if direction == "long":
            if not (bar.low < zone.low and bar.close >= zone.low):
                continue
            wick = (zone.low - bar.low) / atr_value
            reclaim = (bar.close - zone.low) / atr_value
            extreme = bar.low
            level = zone.low
        else:
            if not (bar.high > zone.high and bar.close <= zone.high):
                continue
            wick = (bar.high - zone.high) / atr_value
            reclaim = (zone.high - bar.close) / atr_value
            extreme = bar.high
            level = zone.high
        quality = _clamp(
            0.55 * _clamp(wick)
            + 0.25 * _clamp(reclaim / 0.75)
            + 0.20 * vol_score
        )
        if quality < min_quality:
            continue
        candidate = SweepView(
            direction=direction,  # type: ignore[arg-type]
            level=level,
            extreme=extreme,
            close=bar.close,
            index=i,
            quality=quality,
            wick_atr=wick,
            reclaim_atr=reclaim,
        )
        if best is None or candidate.quality >= best.quality:
            best = candidate
    return best


def detect_divergence(
    klines: list[Kline],
    direction: str,
    *,
    atr_value: float,
    rsi_n: int = 14,
    pivot_k: int = 2,
    ttl_bars: int = 12,
    max_bars: int = 60,
) -> DivergenceView | None:
    """Most recent regular divergence in the requested direction."""
    if len(klines) < max(rsi_n + 5, 30):
        return None
    if atr_value <= 0:
        raise SniperError("divergence ATR must be positive")
    closes = [k.close for k in klines]
    rsi_values = rsi(closes, rsi_n)
    pivots = fractal_pivots(klines, pivot_k, last_index=len(klines) - 1)
    highs = [p for p in pivots if p.kind == "H"]
    lows = [p for p in pivots if p.kind == "L"]
    if direction == "long":
        pairs = find_bullish_divergences(
            lows, rsi_low_pivots(rsi_values, pivot_k), max_bars=max_bars
        )
    else:
        pairs = find_bearish_divergences(
            highs, rsi_high_pivots(rsi_values, pivot_k), max_bars=max_bars
        )
    if not pairs:
        return None
    price_a, price_b, rsi_a, rsi_b = pairs[-1]
    confirmed = max(price_b.confirmed_index, rsi_b.confirmed_index)
    if len(klines) - 1 - confirmed > ttl_bars:
        return None
    price_delta = abs(price_b.price - price_a.price) / atr_value
    osc_delta = abs(rsi_b.price - rsi_a.price) / 10.0
    strength = _clamp(price_delta * osc_delta)
    if strength <= 0:
        return None
    return DivergenceView(
        direction=direction,  # type: ignore[arg-type]
        price_a=price_a.price,
        price_b=price_b.price,
        osc_a=rsi_a.price,
        osc_b=rsi_b.price,
        confirmed_index=confirmed,
        strength=strength,
    )


def trigger_view(
    klines: list[Kline],
    direction: str,
    *,
    atr_value: float,
    pivot_k: int = 2,
    volume_window: int = 20,
    lookback: int = 1,
    max_chase_atr: float | None = None,
) -> TriggerView:
    """Closed 5m trigger features (micro BOS + displacement + participation).

    `lookback` makes the break persist for N closed bars instead of forcing it
    to land on the exact bar being evaluated. This is the setup TTL described in
    docs/SNIPER_V3.md ("the trigger must occur while a setup is active"); with
    `lookback=1` the behaviour is the original single-bar check.

    `max_chase_atr` rejects a stale break once price has run more than that many
    ATR5 away from the break bar's close, so a persisting trigger cannot become
    an excuse to chase.
    """
    if atr_value <= 0:
        raise SniperError("trigger ATR must be positive")
    if lookback < 1:
        raise SniperError("trigger lookback must be positive")
    if len(klines) < volume_window + 3:
        raise SniperError("not enough 5m bars for trigger")

    last = len(klines) - 1
    # Never look further back than the data allows.
    window = max(1, min(lookback, last - max(13, volume_window) + 1))
    idx = last
    view = _trigger_at(
        klines, last, direction, atr_value, pivot_k, volume_window, bars_since=0
    )
    if not view.micro_bos and window > 1:
        for i in range(last - 1, last - window, -1):
            candidate = _trigger_at(
                klines,
                i,
                direction,
                atr_value,
                pivot_k,
                volume_window,
                bars_since=last - i,
            )
            if candidate.micro_bos:
                idx, view = i, candidate
                break

    if view.micro_bos and max_chase_atr is not None:
        drift = abs(klines[last].close - klines[idx].close) / atr_value
        if drift > max_chase_atr:
            # The break is real but stale in price terms: report the current bar
            # as untriggered rather than recommending a chase.
            view = _trigger_at(
                klines, last, direction, atr_value, pivot_k, volume_window, bars_since=0
            )
    return view


def _trigger_at(
    klines: list[Kline],
    index: int,
    direction: str,
    atr_value: float,
    pivot_k: int,
    volume_window: int,
    *,
    bars_since: int,
) -> TriggerView:
    """Trigger features as they were visible on the closed bar at `index`."""
    bar = klines[index]
    previous_close = klines[index - 1].close
    pivots = fractal_pivots(klines, pivot_k, last_index=index)
    last_high = last_swing(pivots, "H", index)
    last_low = last_swing(pivots, "L", index)
    recent_high = max(k.high for k in klines[index - 12:index])
    recent_low = min(k.low for k in klines[index - 12:index])
    if direction == "long":
        level = max(recent_high, last_high.price if last_high is not None else recent_high)
        micro_bos = bar.close > level and previous_close <= level
        displacement = max(0.0, (bar.close - bar.open) / atr_value)
    else:
        level = min(recent_low, last_low.price if last_low is not None else recent_low)
        micro_bos = bar.close < level and previous_close >= level
        displacement = max(0.0, (bar.open - bar.close) / atr_value)

    base = [k.volume for k in klines[index - volume_window:index]]
    mean_volume = sum(base) / len(base) if base else 0.0
    volume_ratio = bar.volume / mean_volume if mean_volume > 0 else 1.0

    delta_series: list[float] = []
    for k in klines[index - volume_window:index + 1]:
        if k.quote_volume <= 0:
            delta_series = []
            break
        delta_series.append(2.0 * k.taker_buy_quote - k.quote_volume)
    delta_z: float | None = None
    if len(delta_series) >= 3:
        history = delta_series[:-1]
        sigma = stddev(history)
        if sigma > 0:
            delta_z = delta_series[-1] / sigma

    delta_score = _clamp(abs(delta_z) / 2.0) if delta_z is not None else 0.0
    score = (
        0.40 * _clamp(displacement / 1.5)
        + 0.30 * _clamp((volume_ratio - 1.0) / 2.0)
        + 0.30 * delta_score
    )
    return TriggerView(
        direction=direction,  # type: ignore[arg-type]
        close=bar.close,
        micro_bos=micro_bos,
        displacement_atr=displacement,
        volume_ratio=volume_ratio,
        delta_z=delta_z,
        score=_clamp(score),
        confirmed_index=index,
        bars_since=bars_since,
    )
