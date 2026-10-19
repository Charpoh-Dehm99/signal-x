"""Tests for signalx.costmodel — band-aggregate fill/round-trip cost model."""

from __future__ import annotations

import pytest

from signalx.costmodel import (
    CostModelError,
    DEFAULT_BAND_EDGES_BPS,
    est_fill_cost_bps,
    round_trip_cost_bps,
)

DEPTHS = (100_000.0, 300_000.0, 600_000.0)  # d05, d1, d2


def test_bad_notional_raises():
    with pytest.raises(CostModelError):
        est_fill_cost_bps(0, 1.0, DEPTHS)
    with pytest.raises(CostModelError):
        est_fill_cost_bps(-5, 1.0, DEPTHS)
    with pytest.raises(CostModelError):
        est_fill_cost_bps(True, 1.0, DEPTHS)


def test_small_order_near_half_spread():
    cost = est_fill_cost_bps(1_000.0, 1.0, DEPTHS)
    assert 1.0 < cost < 1.6  # just inside the first band


def test_exact_first_band_edge():
    # N == d05 -> edge at 50 bps, VWAP = (hs + 50) / 2
    cost = est_fill_cost_bps(100_000.0, 1.0, DEPTHS)
    assert cost == pytest.approx((1.0 + 50.0) / 2.0)


def test_walk_into_second_band():
    # 100k consumes band 1; 50k into band 2 (capacity d1-d05 = 200k over 50..100 bps)
    cost = est_fill_cost_bps(150_000.0, 1.0, DEPTHS)
    acc = (1.0 + 50.0) / 2.0 * 100_000.0 + (50.0 + 62.5) / 2.0 * 50_000.0
    assert cost == pytest.approx(acc / 150_000.0)


def test_beyond_deepest_band_is_none():
    assert est_fill_cost_bps(600_001.0, 1.0, DEPTHS) is None


def test_monotone_in_notional():
    costs = [est_fill_cost_bps(n, 2.0, DEPTHS) for n in (1_000, 50_000, 100_000, 250_000, 600_000)]
    assert all(a < b for a, b in zip(costs, costs[1:]))


def test_depths_must_be_non_decreasing():
    with pytest.raises(CostModelError):
        est_fill_cost_bps(1_000.0, 1.0, (100_000.0, 50_000.0, 600_000.0))


def test_length_mismatch_raises():
    with pytest.raises(CostModelError):
        est_fill_cost_bps(1_000.0, 1.0, (100_000.0, 200_000.0))


def test_half_spread_above_first_edge_raises():
    with pytest.raises(CostModelError):
        est_fill_cost_bps(1_000.0, 60.0, DEPTHS, DEFAULT_BAND_EDGES_BPS)


def test_zero_half_spread_ok():
    cost = est_fill_cost_bps(1_000.0, 0.0, DEPTHS)
    assert cost is not None and 0.0 < cost < 1.0


def test_round_trip_symmetric():
    rt = round_trip_cost_bps(10_000.0, 5.0, 1.0, 1.0, DEPTHS, DEPTHS)
    side = est_fill_cost_bps(10_000.0, 1.0, DEPTHS)
    assert rt == pytest.approx(2.0 * 5.0 + 2.0 * side)


def test_round_trip_none_when_side_exhausted():
    thin = (1_000.0, 2_000.0, 3_000.0)
    assert round_trip_cost_bps(10_000.0, 5.0, 1.0, 1.0, DEPTHS, thin) is None
