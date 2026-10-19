"""Structure labeling, CHoCH, BOS, and classified break tests."""

from __future__ import annotations

import pytest

from signalx.models import LabeledPivot, Pivot
from signalx.structure import (
    detect_bos_down,
    detect_break_events,
    detect_choch,
    detect_choch_events,
    label_structure,
)
from tests.helpers import series_from_closes

STRUCTURE = [
    Pivot(1, 1.0, "H", 3),
    Pivot(2, 0.5, "L", 4),
    Pivot(5, 2.0, "H", 7),   # HH
    Pivot(6, 1.2, "L", 8),   # HL
    Pivot(9, 1.8, "H", 11),  # LH
]

CHOCH_SEQ = STRUCTURE + [
    Pivot(12, 0.9, "L", 14),  # LL
    Pivot(15, 1.1, "L", 17),  # HL -> bullish flip
]


def test_label_structure():
    labeled = label_structure(STRUCTURE)
    assert [lp.label for lp in labeled] == ["H", "L", "HH", "HL", "LH"]


def test_detect_choch_bearish():
    labeled = label_structure(STRUCTURE)
    choch = detect_choch(labeled)
    assert choch is not None
    assert choch.label == "LH"
    assert choch.pivot.price == 1.8


def test_detect_choch_none_without_hh():
    labeled = label_structure([Pivot(1, 1.0, "H", 3), Pivot(2, 0.5, "L", 4)])
    assert detect_choch(labeled) is None


def test_detect_choch_events_tracks_both_directions():
    events = detect_choch_events(label_structure(CHOCH_SEQ))
    assert [(d, lp.pivot.price) for d, lp in events] == [("down", 1.8), ("up", 1.1)]


def test_detect_choch_events_none_without_extreme_runs():
    labeled = label_structure([Pivot(1, 1.0, "H", 3), Pivot(2, 0.5, "L", 4)])
    assert detect_choch_events(labeled) == []


def test_detect_choch_events_first_event_matches_detect_choch():
    labeled = label_structure(STRUCTURE)
    events = detect_choch_events(labeled)
    assert events[0][0] == "down"
    assert events[0][1] is detect_choch(labeled)


def test_bos_down_detects_close_below_swing_low():
    closes = [1.0] * 12
    closes[10] = 1.1  # below the latest confirmed swing low of 1.2
    klines = series_from_closes(closes, spread=0.02)

    result = detect_bos_down(klines, STRUCTURE, index=10)
    assert result is not None
    level, swing = result
    assert level == 1.2
    assert swing.index == 6


def test_bos_down_none_when_close_holds():
    closes = [1.0] * 12
    closes[10] = 1.3  # above swing low
    klines = series_from_closes(closes, spread=0.02)
    assert detect_bos_down(klines, STRUCTURE, index=10) is None


def test_bos_down_uses_only_confirmed_pivots():
    closes = [1.0] * 12
    closes[7] = 0.4  # below the *only then-confirmed* swing low (0.5, confirmed bar 4)
    klines = series_from_closes(closes, spread=0.02)

    result = detect_bos_down(klines, STRUCTURE, index=7)
    assert result is not None
    level, swing = result
    assert level == 0.5
    assert swing.index == 2


def test_bos_down_index_validation():
    klines = series_from_closes([1.0] * 5)
    with pytest.raises(ValueError):
        detect_bos_down(klines, STRUCTURE, index=-1)
    with pytest.raises(ValueError):
        detect_bos_down(klines, STRUCTURE, index=99)


def test_bos_down_without_low_pivots():
    klines = series_from_closes([1.0] * 5)
    assert detect_bos_down(klines, [Pivot(1, 2.0, "H", 3)], index=4) is None


# Rally breaking 1.20 (bar 12), breakdown through 0.90 (bar 16), then a lower
# swing low 0.75 confirmed at bar 18 is broken at bar 19.
BREAK_CLOSES = (
    [1.00] * 6 + [1.00, 1.02, 1.05, 1.08, 1.12, 1.18, 1.25]
    + [1.10, 1.05, 1.00, 0.85, 0.80, 0.80, 0.70, 0.75]
)
BREAK_PIVOTS = [Pivot(6, 1.20, "H", 9), Pivot(2, 0.90, "L", 5), Pivot(15, 0.75, "L", 18)]
BREAK_LABELED = [
    LabeledPivot(Pivot(2, 0.80, "L", 5), "L"),
    LabeledPivot(Pivot(6, 1.20, "H", 9), "HH"),
]


class TestDetectBreakEvents:
    def test_classifies_bos_then_choch_then_bos(self):
        klines = series_from_closes(BREAK_CLOSES, spread=0.02)
        breaks = detect_break_events(klines, BREAK_PIVOTS, BREAK_LABELED)
        assert [(b.kind, b.direction, b.break_index, b.level) for b in breaks] == [
            ("bos", "up", 12, 1.20),     # with the HH bias -> continuation
            ("choch", "down", 16, 0.90),  # against the bias -> flip
            ("bos", "down", 19, 0.75),    # with the new bias -> continuation
        ]
        assert [b.swing_index for b in breaks] == [6, 2, 15]

    def test_defaults_to_bos_when_bias_unknown(self):
        klines = series_from_closes(BREAK_CLOSES, spread=0.02)
        pivots = [Pivot(2, 0.90, "L", 5), Pivot(6, 1.20, "H", 9)]
        breaks = detect_break_events(klines, pivots, [])
        assert [(b.kind, b.direction) for b in breaks] == [("bos", "up"), ("choch", "down")]

    def test_label_bias_flags_first_break_as_choch(self):
        klines = series_from_closes(BREAK_CLOSES, spread=0.02)
        labeled = [LabeledPivot(Pivot(9, 1.18, "H", 11), "LH")]  # bearish bias before bar 12
        breaks = detect_break_events(klines, [Pivot(6, 1.20, "H", 9)], labeled)
        assert len(breaks) == 1
        assert breaks[0].kind == "choch" and breaks[0].direction == "up"

    def test_max_events_keeps_classified_tail(self):
        klines = series_from_closes(BREAK_CLOSES, spread=0.02)
        breaks = detect_break_events(klines, BREAK_PIVOTS, BREAK_LABELED, max_events=1)
        assert len(breaks) == 1
        # the tail is BOS only because the cropped CHoCH still flipped the bias
        assert breaks[0].kind == "bos" and breaks[0].direction == "down"
        assert breaks[0].break_index == 19
