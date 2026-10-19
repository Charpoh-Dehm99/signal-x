"""Resistance zone construction: recency/volume-weighted pivot clustering."""

from __future__ import annotations

import math

from .models import Pivot, Zone


def cluster_zones(
    pivots: list[Pivot],
    *,
    atr_value: float,
    now_index: int,
    decay_per_bar: float = 0.005,
    merge_dist_atr: float = 1.0,
    min_touches: int = 2,
) -> list[Zone]:
    """Cluster pivot prices into zones.

    weight_i = exp(-decay * age_bars) * volume_i
    Zones merge while within merge_dist_atr * ATR; strength = sum of weights.
    """
    if atr_value is None or atr_value <= 0:
        raise ValueError("atr_value must be positive")
    if now_index < 0:
        raise ValueError("now_index must be >= 0")
    if merge_dist_atr <= 0:
        raise ValueError("merge_dist_atr must be positive")
    if min_touches < 1:
        raise ValueError("min_touches must be >= 1")

    items: list[tuple[float, float]] = []
    for p in pivots:
        age = max(now_index - p.index, 0)
        weight = math.exp(-decay_per_bar * age) * max(p.volume, 0.0)
        items.append((p.price, weight))
    if not items:
        return []

    items.sort(key=lambda t: t[0])
    merge_dist = merge_dist_atr * atr_value

    clusters: list[list[tuple[float, float]]] = [[items[0]]]
    for price, weight in items[1:]:
        current = clusters[-1]
        mean = sum(x[0] for x in current) / len(current)
        if abs(price - mean) <= merge_dist:
            current.append((price, weight))
        else:
            clusters.append([(price, weight)])

    zones: list[Zone] = []
    for cluster in clusters:
        if len(cluster) < min_touches:
            continue
        lo = min(x[0] for x in cluster)
        hi = max(x[0] for x in cluster)
        strength = sum(x[1] for x in cluster)
        zones.append(Zone(lo, hi, strength, len(cluster)))

    zones.sort(key=lambda z: z.strength, reverse=True)
    return zones


def nearest_resistance_zone(zones: list[Zone], price: float) -> Zone | None:
    """Lowest zone whose top is at or above price (the overhead resistance)."""
    above = [z for z in zones if z.high >= price]
    if not above:
        return None
    return min(above, key=lambda z: z.low)


def distance_atr(price: float, level: float, atr_value: float) -> float:
    if atr_value <= 0:
        raise ValueError("atr_value must be positive")
    return abs(level - price) / atr_value


def round_number_level(price: float, step: float) -> float:
    if step <= 0:
        raise ValueError("step must be positive")
    return round(price / step) * step


def round_step_for_price(price: float) -> float:
    """Step of ~1% of price magnitude: 100 -> 1, 0.775 -> 0.001."""
    if price <= 0:
        raise ValueError("price must be positive")
    exp = math.floor(math.log10(price)) - 2
    return 10.0 ** exp
