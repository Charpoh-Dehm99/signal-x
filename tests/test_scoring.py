"""Confluence scorer tests."""

from __future__ import annotations

import pytest

from signalx.scoring import score, sigmoid


def test_sigmoid_bounds_and_center():
    assert sigmoid(0.0) == pytest.approx(0.5)
    assert sigmoid(50.0) == pytest.approx(1.0)
    assert sigmoid(-50.0) == pytest.approx(0.0)


def test_empty_features_is_low_probability():
    p, contribs = score({})
    assert p < 0.5
    assert len(contribs) == 3
    assert all(abs(c[1]) == 0.0 for c in contribs)


def test_more_bearish_features_raise_probability():
    base = {"compression_score": 0.8}
    weak, _ = score(base)
    strong, _ = score({**base, "bos_confirmed": True, "has_bearish_div": True})
    assert strong > weak


def test_bool_features_handled_as_1_or_0():
    p_true, _ = score({"has_bearish_div": True})
    p_one, _ = score({"has_bearish_div": 1.0})
    assert p_true == pytest.approx(p_one)


def test_none_feature_contributes_zero():
    p_none, _ = score({"has_bearish_div": None})
    p_absent, _ = score({})
    assert p_none == pytest.approx(p_absent)


def test_top_contributions_sorted_by_abs():
    _, contribs = score({"bos_confirmed": True, "compression_score": 0.9})
    magnitudes = [abs(c[1]) for c in contribs]
    assert magnitudes == sorted(magnitudes, reverse=True)


def test_non_numeric_feature_raises_loudly():
    with pytest.raises(TypeError):
        score({"bos_confirmed": "yes"})
    with pytest.raises(TypeError):
        score({"bos_confirmed": ["x"]})


def test_features_must_be_dict():
    with pytest.raises(TypeError):
        score([("bos_confirmed", 1)])
