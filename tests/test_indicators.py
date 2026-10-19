"""Indicator correctness tests (math pinned, not just 'runs')."""

from __future__ import annotations

import pytest

from signalx.indicators import atr, avwap, pct_rank, roc, rsi, sma, stddev
from tests.helpers import series_from_closes


def test_sma_basic():
    assert sma([1, 2, 3, 4, 5], 2) == [None, 1.5, 2.5, 3.5, 4.5]


def test_sma_rejects_bad_n():
    with pytest.raises(ValueError):
        sma([1, 2, 3], 0)


def test_rsi_monotonic_up_is_100():
    closes = list(range(1, 40))
    values = rsi(closes, 14)
    assert values[12] is None
    assert values[13] == pytest.approx(100.0)
    assert values[-1] == pytest.approx(100.0)


def test_rsi_monotonic_down_is_0():
    closes = list(range(40, 1, -1))
    values = rsi(closes, 14)
    assert values[-1] == pytest.approx(0.0)


def test_rsi_too_short_returns_nones():
    assert rsi([1.0, 2.0, 3.0], 14) == [None, None, None]


def test_rsi_rejects_bad_period():
    with pytest.raises(ValueError):
        rsi([1, 2, 3], 0)


def test_atr_positive_on_volatile_series():
    klines = series_from_closes([10, 12, 9, 14, 8, 15, 7, 16, 9, 13, 11, 12, 10, 14, 12],
                                spread=0.5)
    values = atr(klines, 14)
    assert values[-1] is not None and values[-1] > 0


def test_avwap_flat_prices_is_price_sigma_zero():
    klines = series_from_closes([100.0] * 5, spread=0.5)
    points = avwap(klines, start=0)
    vwap, sigma = points[-1]
    assert vwap == pytest.approx(100.0)
    assert sigma == pytest.approx(0.0)


def test_avwap_start_out_of_range():
    klines = series_from_closes([1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        avwap(klines, start=3)


def test_roc_basic():
    assert roc([1.0, 2.0, 4.0, 8.0], 1) == [None, 1.0, 1.0, 1.0]


def test_pct_rank():
    assert pct_rank([1, 2, 3, 4, 5], 3) == pytest.approx(60.0)
    assert pct_rank([], 1) is None


def test_stddev_edge_cases():
    assert stddev([]) == 0.0
    assert stddev([5.0]) == 0.0
    assert stddev([0.0, 2.0]) == pytest.approx(1.0)
