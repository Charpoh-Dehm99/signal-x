"""Divergence detection: price extremes vs oscillator (RSI) extremes.

Bearish: price HH while RSI prints LH. Bullish: price LL while RSI prints HL.
Pivot pairing is deterministic and monotonic: consecutive price pivots are
paired with the nearest-in-time RSI pivots within +/- max_lag bars; RSI pivots
are never reused out of order.
"""

from __future__ import annotations

from .models import Pivot
from .pivots import fractal_pivots_values


def rsi_high_pivots(rsi_values: list[float | None], k: int = 2) -> list[Pivot]:
    """Extract RSI swing highs as Pivot objects (volume unused)."""
    out: list[Pivot] = []
    for idx, val, kind in fractal_pivots_values(rsi_values, k):
        if kind == "H":
            out.append(Pivot(idx, val, "H", idx + k, 0.0))
    return out


def rsi_low_pivots(rsi_values: list[float | None], k: int = 2) -> list[Pivot]:
    """Extract RSI swing lows as Pivot objects (volume unused)."""
    out: list[Pivot] = []
    for idx, val, kind in fractal_pivots_values(rsi_values, k):
        if kind == "L":
            out.append(Pivot(idx, val, "L", idx + k, 0.0))
    return out


def _nearest(candidates: list[Pivot], target_idx: int, max_lag: int) -> Pivot | None:
    best: Pivot | None = None
    best_d: int | None = None
    for p in candidates:
        d = abs(p.index - target_idx)
        if d <= max_lag and (best_d is None or d < best_d):
            best, best_d = p, d
    return best


def find_bearish_divergences(
    price_highs: list[Pivot],
    rsi_highs: list[Pivot],
    *,
    min_bars: int = 5,
    max_bars: int = 60,
    max_lag: int = 3,
) -> list[tuple[Pivot, Pivot, Pivot, Pivot]]:
    """Regular bearish divergences.

    Returns (price_p1, price_p2, rsi_p1, rsi_p2) where price made a higher
    high and RSI made a lower high within the lookback window.
    """
    if min_bars < 1 or max_bars < min_bars or max_lag < 0:
        raise ValueError("invalid window parameters")

    out: list[tuple[Pivot, Pivot, Pivot, Pivot]] = []
    for p1, p2 in zip(price_highs, price_highs[1:]):
        gap = p2.index - p1.index
        if gap < min_bars or gap > max_bars:
            continue
        if p2.price <= p1.price:
            continue
        r1 = _nearest(rsi_highs, p1.index, max_lag)
        r2 = _nearest(rsi_highs, p2.index, max_lag)
        if r1 is None or r2 is None:
            continue
        if r2.index > r1.index and r2.price < r1.price:
            out.append((p1, p2, r1, r2))
    return out


def find_bullish_divergences(
    price_lows: list[Pivot],
    rsi_lows: list[Pivot],
    *,
    min_bars: int = 5,
    max_bars: int = 60,
    max_lag: int = 3,
) -> list[tuple[Pivot, Pivot, Pivot, Pivot]]:
    """Regular bullish divergences.

    Returns (price_p1, price_p2, rsi_p1, rsi_p2) where price made a lower low
    and RSI made a higher low within the lookback window.
    """
    if min_bars < 1 or max_bars < min_bars or max_lag < 0:
        raise ValueError("invalid window parameters")

    out: list[tuple[Pivot, Pivot, Pivot, Pivot]] = []
    for p1, p2 in zip(price_lows, price_lows[1:]):
        gap = p2.index - p1.index
        if gap < min_bars or gap > max_bars:
            continue
        if p2.price >= p1.price:
            continue
        r1 = _nearest(rsi_lows, p1.index, max_lag)
        r2 = _nearest(rsi_lows, p2.index, max_lag)
        if r1 is None or r2 is None:
            continue
        if r2.index > r1.index and r2.price > r1.price:
            out.append((p1, p2, r1, r2))
    return out
