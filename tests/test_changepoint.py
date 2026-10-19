"""CUSUM change-point tests."""

from __future__ import annotations

import pytest

from signalx.changepoint import cusum


def test_cusum_detects_upward_step():
    series = [0.0] * 30 + [5.0] * 30
    result = cusum(series, k=0.2, h=5.0)
    assert result["alarm_index"] is not None
    assert 30 <= result["alarm_index"] <= 40
    assert result["alarms"]


def test_cusum_flat_series_no_alarm():
    result = cusum([1.0] * 50, k=0.5, h=5.0)
    assert result["alarm_index"] is None
    assert result["alarms"] == []


def test_cusum_input_validation():
    with pytest.raises(ValueError):
        cusum([1.0] * 5)              # too short
    with pytest.raises(ValueError):
        cusum([1.0] * 30, k=-1)
    with pytest.raises(ValueError):
        cusum([1.0] * 30, h=0)
