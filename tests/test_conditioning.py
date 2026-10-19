"""Tests for signalx.conditioning: funding percentile, decile mask, conjunction stats."""

from __future__ import annotations

import pandas as pd
import pytest

from signalx.conditioning import (
    ConditioningError,
    condition_events,
    funding_percentile_at,
    top_decile_mask,
)

MS_PER_DAY = 86_400_000
STEP = 8 * 3_600_000


def fund(rates, start_ms=0):
    return pd.DataFrame(
        {
            "funding_time": [start_ms + i * STEP for i in range(len(rates))],
            "interval_hours": [8] * len(rates),
            "rate": rates,
        }
    )


HOT = fund([0.0001] * 89 + [0.0010])   # last tick is the max -> pctile 100
COLD = fund([0.0010] * 89 + [0.0001])  # last tick is the min -> pctile ~1


# ------------------------------------------------------- funding_percentile_at


class TestFundingPercentile:
    def test_none_and_empty(self):
        assert funding_percentile_at(None, 10**13) is None
        assert funding_percentile_at(fund([]), 10**13) is None

    def test_missing_columns_raise(self):
        with pytest.raises(ConditioningError):
            funding_percentile_at(pd.DataFrame({"funding_time": [1, 2]}), 10**13)

    def test_unsorted_raises(self):
        bad = fund([0.001, 0.002]).iloc[::-1]
        with pytest.raises(ConditioningError):
            funding_percentile_at(bad, 10**13)

    def test_too_few_ticks_returns_none(self):
        assert funding_percentile_at(fund([0.001] * 10), 10**13, min_ticks=60) is None

    def test_flat_history_last_is_100(self):
        assert funding_percentile_at(fund([0.0001] * 90), 89 * STEP) == pytest.approx(100.0)

    def test_hot_last_tick(self):
        at = 89 * STEP  # window (29.67d - 30d, 29.67d] contains all 90 ticks
        assert funding_percentile_at(HOT, at) == pytest.approx(100.0)

    def test_cold_last_tick(self):
        at = 89 * STEP
        assert funding_percentile_at(COLD, at) == pytest.approx(100.0 * 1 / 90)

    def test_old_tick_outside_lookback_excluded(self):
        rates = [0.01] + [0.0001] * 89
        at = 30 * MS_PER_DAY + STEP  # excludes the 0.01 tick at t=0, keeps ticks 1..89
        pct = funding_percentile_at(fund(rates), at)
        assert pct == pytest.approx(100.0)


# ------------------------------------------------------------ top_decile_mask


class TestTopDecile:
    def test_invalid_quantile_raises(self):
        with pytest.raises(ConditioningError):
            top_decile_mask(pd.Series([1.0, 2.0]), quantile=1.0)

    def test_not_a_series_raises(self):
        with pytest.raises(ConditioningError):
            top_decile_mask([1.0, 2.0])

    def test_empty_and_all_nan(self):
        assert top_decile_mask(pd.Series(dtype=float)).sum() == 0
        assert top_decile_mask(pd.Series([None, None])).sum() == 0

    def test_top_10_of_100(self):
        s = pd.Series(range(100), dtype=float)
        mask = top_decile_mask(s)
        assert mask.sum() == 10
        assert set(s[mask]) == set(range(90, 100))

    def test_nan_not_flagged(self):
        s = pd.Series([1.0, float("nan"), 100.0])
        mask = top_decile_mask(s, quantile=0.5)
        assert mask.tolist() == [False, False, True]


# ------------------------------------------------------------ condition_events


def mk_events():
    return pd.DataFrame(
        {
            "symbol": ["AAAAUSDT", "BBBBUSDT", "CCCCUSDT", "DDDDUSDT", "EEEEUSDT"],
            "event_ms": [31 * MS_PER_DAY] * 5,
            "return_24h": [4.0, 3.0, 4.0, 4.0, 4.0],
            "trig_primary": [True, True, True, True, False],
            "primary_bars_since_event": [100, 100, 100, 500, None],
            "primary_sim_outcome": ["target", "stop", "stop", "target", None],
            "primary_net_funding": [0.04, 0.01, -0.02, 0.005, None],
        }
    )


def funding_fn(sym):
    if sym in ("AAAAUSDT", "BBBBUSDT", "DDDDUSDT", "EEEEUSDT"):
        return HOT
    if sym == "CCCCUSDT":
        return COLD
    return None


class TestConditionEvents:
    def test_missing_required_column_raises(self):
        with pytest.raises(ConditioningError):
            condition_events(pd.DataFrame({"symbol": ["X"]}), funding_fn=funding_fn)

    def test_conjunction_and_columns(self):
        out, summary = condition_events(mk_events(), funding_fn=funding_fn)
        # decile threshold: quantile(0.9) of [3,4,4,4,4] = 4.0 -> B (3.0) excluded.
        # The mask is computed over ALL pump events, so E (untriggered) is flagged too.
        assert out["pump_top_decile"].tolist() == [True, False, True, True, True]
        # A passes all; C fails funding; D fails timing; B fails decile; E not triggered
        assert out["conditioned"].tolist() == [True, False, False, False, False]
        assert out["cond_funding"].tolist() == [True, True, False, True, False]

    def test_extra_cost_column(self):
        out, _ = condition_events(mk_events(), funding_fn=funding_fn)
        assert out["net_funding_2x"].iloc[0] == pytest.approx(0.04 - 0.002)

    def test_summary_subsets(self):
        _, summary = condition_events(mk_events(), funding_fn=funding_fn)
        assert summary["triggered"]["n"] == 4
        assert summary["funding_ge_p90"]["n"] == 3
        assert summary["within_24h"]["n"] == 3
        assert summary["top_decile"]["n"] == 3
        assert summary["conditioned"]["n"] == 1
        assert summary["conditioned"]["t1_hits"] == 1
        assert summary["conditioned"]["mean_net"] == pytest.approx(0.04)
        assert summary["conditioned"]["mean_net_2x"] == pytest.approx(0.038)
        lo, hi = summary["conditioned"]["wilson"]
        assert 0.0 < lo <= hi <= 1.0

    def test_unknown_funding_fails_closed(self):
        out, summary = condition_events(mk_events(), funding_fn=lambda sym: None)
        assert out["funding_known"].sum() == 0
        assert out["conditioned"].sum() == 0
        assert summary["conditioned"]["n"] == 0

    def test_triggered_without_bars_raises(self):
        ev = mk_events()
        ev.loc[0, "primary_bars_since_event"] = None
        with pytest.raises(ConditioningError):
            condition_events(ev, funding_fn=funding_fn)

    def test_empty_frame(self):
        empty = mk_events().iloc[0:0]
        out, summary = condition_events(empty, funding_fn=funding_fn)
        assert len(out) == 0
        assert summary["triggered"]["n"] == 0
