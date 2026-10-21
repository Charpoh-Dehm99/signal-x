"""Compression / plateau scoring tests."""

from __future__ import annotations

import pytest

from signalx.compression import compression_metrics, momentum_decay_bias, momentum_decaying
from tests.helpers import series_from_closes


def test_tight_range_qualifies_high_score():
    closes = [100 + 0.05 * ((-1) ** i) for i in range(30)]
    klines = series_from_closes(closes, spread=0.05)
    m = compression_metrics(klines, window=30, atr_value=1.0)
    assert m["qualifies"] is True
    assert m["height_atr"] < 1.0
    assert m["containment"] == pytest.approx(1.0)
    assert m["score"] > 0.8


def test_wide_range_disqualifies_low_score():
    closes = [100 + i for i in range(30)]
    klines = series_from_closes(closes, spread=0.05)
    m = compression_metrics(klines, window=30, atr_value=1.0)
    assert m["qualifies"] is False
    assert m["height_atr"] > 2.0
    assert m["score"] < 0.5


def test_compression_input_validation():
    klines = series_from_closes([100.0] * 10)
    with pytest.raises(ValueError):
        compression_metrics(klines, window=2, atr_value=1.0)
    with pytest.raises(ValueError):
        compression_metrics(klines, window=30, atr_value=1.0)  # not enough bars
    with pytest.raises(ValueError):
        compression_metrics(klines, window=5, atr_value=0.0)


def test_momentum_decay_bias_fading_rally_is_bearish():
    assert momentum_decay_bias([None, 0.05, 0.04, 0.03, 0.02]) == "bear"


def test_momentum_decay_bias_fading_selloff_is_bullish():
    assert momentum_decay_bias([None, -0.05, -0.04, -0.03, -0.02]) == "bull"


def test_momentum_decay_bias_none_on_acceleration():
    assert momentum_decay_bias([0.02, 0.03, 0.04]) is None
    assert momentum_decay_bias([-0.02, -0.03, -0.04]) is None


def test_momentum_decay_bias_insufficient_data():
    assert momentum_decay_bias([None, 0.05]) is None
    with pytest.raises(ValueError):
        momentum_decay_bias([0.1, 0.2], lookback=1)


def test_momentum_decaying_true_on_deceleration():
    assert momentum_decaying([None, 0.05, 0.04, 0.03, 0.02]) is True


def test_momentum_decaying_false_on_acceleration():
    assert momentum_decaying([0.02, 0.03, 0.04]) is False


def test_momentum_decaying_insufficient_data():
    assert momentum_decaying([None, 0.05]) is False
    with pytest.raises(ValueError):
        momentum_decaying([0.1, 0.2], lookback=1)
