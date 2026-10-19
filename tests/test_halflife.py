"""Ornstein-Uhlenbeck half-life estimation tests."""

from __future__ import annotations

import math
import random

import pytest

from signalx.halflife import ou_halflife


def _ar1_series(phi: float, n: int = 1000, seed: int = 7) -> list[float]:
    rng = random.Random(seed)  # nosec B311 - deterministic test data, not security
    x = [0.0]
    for _ in range(n):
        x.append(phi * x[-1] + rng.gauss(0.0, 0.1))
    return x


def test_ou_halflife_recovers_known_phi():
    series = _ar1_series(0.9)
    result = ou_halflife(series)
    assert result["mean_reverting"] is True
    assert result["phi"] == pytest.approx(0.9, abs=0.03)
    expected = math.log(2) / (-math.log(0.9))  # ~6.58
    assert result["halflife"] == pytest.approx(expected, rel=0.25)


def test_ou_halflife_random_walk_not_mean_reverting():
    # A random walk is not mean-reverting. The AR(1) estimate of phi is
    # negatively biased in finite samples (Kendall), so phi ~ 0.998 here and
    # the fitted half-life must be absurdly long (>100 bars), i.e. the tool
    # must NOT mistake a random walk for a tradeable reversion.
    rng = random.Random(3)  # nosec B311 - deterministic test data, not security
    x = [0.0]
    for _ in range(4000):
        x.append(x[-1] + rng.gauss(0.0, 0.1))
    result = ou_halflife(x)
    assert (not result["mean_reverting"]) or result["halflife"] > 100


def test_ou_halflife_input_validation():
    with pytest.raises(ValueError):
        ou_halflife([1.0] * 10)                    # too short
    with pytest.raises(ValueError):
        ou_halflife([1.0] * 60)                    # zero variance
    with pytest.raises(ValueError):
        ou_halflife([None] * 60 + [1.0] * 60)      # Nones not allowed
