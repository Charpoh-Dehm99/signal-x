"""Pivot detection tests, with anti-lookahead guarantees pinned."""

from __future__ import annotations

import pytest

from signalx.pivots import (
    fractal_pivots,
    fractal_pivots_values,
    last_swing,
    pivots_as_of,
    zigzag_pivots,
)
from tests.helpers import bars_from_hl, series_from_closes


def test_fractal_finds_single_peak():
    klines = bars_from_hl([1, 2, 3, 2, 1], [-1, -0.5, 0, -0.5, -1])
    pivots = fractal_pivots(klines, k=1)
    assert len(pivots) == 1
    assert pivots[0].kind == "H"
    assert pivots[0].index == 2
    assert pivots[0].confirmed_index == 3


def test_fractal_flat_top_does_not_double_count():
    klines = bars_from_hl([1, 2, 2, 2, 1], [0, 0.5, 0.5, 0.5, 0])
    assert fractal_pivots(klines, k=1) == []


def test_fractal_k2_peak():
    klines = bars_from_hl(
        [1, 2, 3, 4, 3, 2, 1],
        [0.5, 1.5, 2.5, 3.5, 2.5, 1.5, 0.5],
    )
    pivots = fractal_pivots(klines, k=2)
    assert len(pivots) == 1
    assert pivots[0].index == 3 and pivots[0].confirmed_index == 5


def test_fractal_no_lookahead_last_index():
    klines = bars_from_hl(
        [1, 2, 3, 4, 3, 2, 1],
        [0.5, 1.5, 2.5, 3.5, 2.5, 1.5, 0.5],
    )
    # At bar 4, the k=2 peak at bar 3 is NOT yet confirmable (needs bar 5).
    early = fractal_pivots(klines, k=2, last_index=4)
    assert early == []
    at_five = fractal_pivots(klines, k=2, last_index=5)
    assert len(at_five) == 1 and at_five[0].confirmed_index == 5


def test_fractal_every_pivot_respects_confirmation_index():
    klines = bars_from_hl(
        [1, 2, 3, 4, 3, 2, 1, 2, 3, 2, 1, 2],
        [0.5, 1.5, 2.5, 3.5, 2.5, 1.5, 0.5, 1.5, 2.5, 1.5, 0.5, 1.5],
    )
    last = 8
    pivots = fractal_pivots(klines, k=2, last_index=last)
    assert pivots, "expected at least one pivot"
    assert all(p.confirmed_index <= last for p in pivots)


def test_fractal_bad_k_and_index():
    klines = bars_from_hl([1, 2, 3], [0, 1, 0])
    with pytest.raises(ValueError):
        fractal_pivots(klines, k=0)
    with pytest.raises(ValueError):
        fractal_pivots(klines, k=1, last_index=99)


def test_fractal_values_skips_nones():
    values = [1.0, 2.0, None, 4.0, 2.0]
    out = fractal_pivots_values(values, k=1)
    assert all(v is not None for _, v, _ in out)


def test_zigzag_confirms_at_reversal_bar():
    closes = [10, 10.5, 11, 11.5, 12, 11.5, 11, 10.5, 10.6, 11, 11.5, 11.8]
    klines = series_from_closes(closes, spread=0.05)
    atr_values = [1.0] * len(klines)

    pivots = zigzag_pivots(klines, k_zz=1.0, atr_values=atr_values)
    assert len(pivots) == 2

    high = pivots[0]
    assert high.kind == "H"
    assert high.price == pytest.approx(12.05)
    assert high.confirmed_index >= high.index
    assert high.confirmed_index == 6  # bar where the reversal threshold was crossed

    low = pivots[1]
    assert low.kind == "L"
    assert low.price == pytest.approx(10.45)
    assert low.confirmed_index == 10


def test_pivots_as_of_filters_future():
    klines = bars_from_hl([1, 2, 3, 2, 1, 2, 3, 2, 1], [0, 1, 2, 1, 0, 1, 2, 1, 0])
    pivots = fractal_pivots(klines, k=2)
    subset = pivots_as_of(pivots, 4)
    assert all(p.confirmed_index <= 4 for p in subset)


def test_last_swing_uses_confirmation_time():
    klines = bars_from_hl([1, 2, 3, 2, 1], [-1, -0.5, 0, -0.5, -1])
    pivots = fractal_pivots(klines, k=1)  # H at index 2, confirmed 3
    assert last_swing(pivots, "H", index=2) is None      # not yet confirmable
    assert last_swing(pivots, "H", index=3) is not None
    with pytest.raises(ValueError):
        last_swing(pivots, "X")
