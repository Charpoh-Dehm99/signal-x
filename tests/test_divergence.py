"""Divergence detection tests (bearish + bullish)."""

from __future__ import annotations

import pytest

from signalx.divergence import (
    find_bearish_divergences,
    find_bullish_divergences,
    rsi_high_pivots,
    rsi_low_pivots,
)
from signalx.models import Pivot

PRICE_HH = [
    Pivot(10, 100.0, "H", 12),
    Pivot(30, 105.0, "H", 32),
    Pivot(50, 110.0, "H", 52),
]


def test_bearish_divergence_detected():
    rsi_highs = [
        Pivot(10, 70.0, "H", 12),
        Pivot(30, 65.0, "H", 32),   # LH while price HH => divergence
        Pivot(50, 72.0, "H", 52),   # higher again => no second divergence
    ]
    found = find_bearish_divergences(PRICE_HH, rsi_highs)
    assert len(found) == 1
    p1, p2, r1, r2 = found[0]
    assert (p1.index, p2.index) == (10, 30)
    assert r2.price < r1.price


def test_no_divergence_when_rsi_confirms():
    rsi_highs = [
        Pivot(10, 70.0, "H", 12),
        Pivot(30, 75.0, "H", 32),
        Pivot(50, 80.0, "H", 52),
    ]
    assert find_bearish_divergences(PRICE_HH, rsi_highs) == []


def test_no_divergence_when_price_not_higher_high():
    price = [Pivot(10, 105.0, "H", 12), Pivot(30, 100.0, "H", 32)]
    rsi_highs = [Pivot(10, 70.0, "H", 12), Pivot(30, 65.0, "H", 32)]
    assert find_bearish_divergences(price, rsi_highs) == []


def test_no_divergence_when_gap_too_small():
    price = [Pivot(10, 100.0, "H", 12), Pivot(13, 105.0, "H", 15)]  # 3 bars < min 5
    rsi_highs = [Pivot(10, 70.0, "H", 12), Pivot(13, 65.0, "H", 15)]
    assert find_bearish_divergences(price, rsi_highs) == []


def test_no_divergence_when_gap_too_large():
    price = [Pivot(10, 100.0, "H", 12), Pivot(200, 105.0, "H", 202)]  # 190 bars > max 60
    rsi_highs = [Pivot(10, 70.0, "H", 12), Pivot(200, 65.0, "H", 202)]
    assert find_bearish_divergences(price, rsi_highs) == []


def test_rsi_pivots_requires_lag_within_max():
    price = [Pivot(10, 100.0, "H", 12), Pivot(40, 105.0, "H", 42)]
    rsi_highs = [Pivot(20, 60.0, "H", 22)]  # 10 bars away, lag max 3 => unmatched
    assert find_bearish_divergences(price, rsi_highs) == []


def test_invalid_window_parameters_raise():
    with pytest.raises(ValueError):
        find_bearish_divergences(PRICE_HH, [], min_bars=0)
    with pytest.raises(ValueError):
        find_bearish_divergences(PRICE_HH, [], min_bars=10, max_bars=5)
    with pytest.raises(ValueError):
        find_bearish_divergences(PRICE_HH, [], max_lag=-1)


def test_rsi_high_pivots_from_series():
    values = [50.0, 55.0, 70.0, 55.0, 50.0, 60.0, 40.0]
    pivots = rsi_high_pivots(values, k=1)
    assert any(p.index == 2 for p in pivots)


# ---------------------------------------------------------------- bullish

PRICE_LL = [
    Pivot(10, 110.0, "L", 12),
    Pivot(30, 105.0, "L", 32),
    Pivot(50, 100.0, "L", 52),
]


def test_bullish_divergence_detected():
    rsi_lows = [
        Pivot(10, 30.0, "L", 12),
        Pivot(30, 35.0, "L", 32),   # HL while price LL => divergence
        Pivot(50, 28.0, "L", 52),   # lower again => no second divergence
    ]
    found = find_bullish_divergences(PRICE_LL, rsi_lows)
    assert len(found) == 1
    p1, p2, r1, r2 = found[0]
    assert (p1.index, p2.index) == (10, 30)
    assert r2.price > r1.price


def test_bullish_none_when_rsi_confirms():
    rsi_lows = [
        Pivot(10, 30.0, "L", 12),
        Pivot(30, 25.0, "L", 32),
        Pivot(50, 20.0, "L", 52),
    ]
    assert find_bullish_divergences(PRICE_LL, rsi_lows) == []


def test_bullish_none_when_price_not_lower_low():
    price = [Pivot(10, 100.0, "L", 12), Pivot(30, 105.0, "L", 32)]
    rsi_lows = [Pivot(10, 30.0, "L", 12), Pivot(30, 35.0, "L", 32)]
    assert find_bullish_divergences(price, rsi_lows) == []


def test_bullish_none_when_gap_out_of_window():
    price = [Pivot(10, 110.0, "L", 12), Pivot(13, 105.0, "L", 15)]
    rsi_lows = [Pivot(10, 30.0, "L", 12), Pivot(13, 35.0, "L", 15)]
    assert find_bullish_divergences(price, rsi_lows) == []


def test_bullish_invalid_window_parameters_raise():
    with pytest.raises(ValueError):
        find_bullish_divergences(PRICE_LL, [], min_bars=0)
    with pytest.raises(ValueError):
        find_bullish_divergences(PRICE_LL, [], min_bars=10, max_bars=5)
    with pytest.raises(ValueError):
        find_bullish_divergences(PRICE_LL, [], max_lag=-1)


def test_rsi_low_pivots_from_series():
    values = [50.0, 45.0, 30.0, 45.0, 50.0, 40.0, 60.0]
    pivots = rsi_low_pivots(values, k=1)
    assert any(p.index == 2 for p in pivots)
