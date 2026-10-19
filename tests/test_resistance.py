"""Resistance zone clustering tests."""

from __future__ import annotations

import pytest

from signalx.models import Pivot
from signalx.resistance import (
    cluster_zones,
    distance_atr,
    nearest_resistance_zone,
    round_number_level,
    round_step_for_price,
)

PIVOTS = [
    Pivot(10, 100.0, "H", 12, 10.0),
    Pivot(20, 100.5, "H", 22, 10.0),
    Pivot(30, 130.0, "H", 32, 10.0),
]


def test_cluster_merges_nearby_and_drops_singletons():
    zones = cluster_zones(PIVOTS, atr_value=1.0, now_index=40)
    assert len(zones) == 1
    z = zones[0]
    assert z.low == 100.0 and z.high == 100.5
    assert z.touches == 2
    assert z.strength > 0


def test_cluster_min_touches_one_keeps_all():
    zones = cluster_zones(PIVOTS, atr_value=1.0, now_index=40, min_touches=1)
    assert len(zones) == 2


def test_recency_decay_reduces_strength():
    fresh = cluster_zones(PIVOTS, atr_value=1.0, now_index=40, decay_per_bar=0.0)
    decayed = cluster_zones(PIVOTS, atr_value=1.0, now_index=40, decay_per_bar=0.1)
    assert decayed[0].strength < fresh[0].strength


def test_zone_parameters_validated():
    with pytest.raises(ValueError):
        cluster_zones(PIVOTS, atr_value=0, now_index=40)
    with pytest.raises(ValueError):
        cluster_zones(PIVOTS, atr_value=1.0, now_index=-1)
    with pytest.raises(ValueError):
        cluster_zones(PIVOTS, atr_value=1.0, now_index=40, min_touches=0)
    with pytest.raises(ValueError):
        cluster_zones(PIVOTS, atr_value=1.0, now_index=40, merge_dist_atr=0)


def test_empty_pivots_returns_empty():
    assert cluster_zones([], atr_value=1.0, now_index=0) == []


def test_nearest_resistance_zone():
    # Default min_touches=2: only the 100-100.5 cluster becomes a zone.
    zones = cluster_zones(PIVOTS, atr_value=1.0, now_index=40)
    z = nearest_resistance_zone(zones, 99.0)
    assert z is not None and z.low == 100.0
    assert nearest_resistance_zone(zones, 101.0) is None

    # With min_touches=1 the 130 singleton is a zone and is the nearest above 101.
    wider = cluster_zones(PIVOTS, atr_value=1.0, now_index=40, min_touches=1)
    z2 = nearest_resistance_zone(wider, 101.0)
    assert z2 is not None and z2.low == 130.0


def test_distance_atr():
    assert distance_atr(100.0, 102.0, 2.0) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        distance_atr(100.0, 102.0, 0.0)


def test_round_numbers():
    assert round_number_level(104.7, 10) == 100.0
    assert round_step_for_price(100.0) == pytest.approx(1.0)
    assert round_step_for_price(0.775) == pytest.approx(0.001)
    with pytest.raises(ValueError):
        round_number_level(1.0, 0)
    with pytest.raises(ValueError):
        round_step_for_price(0)
