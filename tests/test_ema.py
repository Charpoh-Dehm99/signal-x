"""Triple-EMA engine tests: seeding, states, cross timing, cascade chains."""

from __future__ import annotations

import pytest

from signalx.ema import (
    EmaCross,
    TfInfo,
    cascade_pattern,
    cross_events,
    ema,
    tf_crosses,
    triple_ema,
    triple_state,
)

MIN_MS = 60_000
T0 = 1_600_000_000_000
STEP = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


def times(n: int, step: int = MIN_MS) -> list[int]:
    return [T0 + i * step for i in range(n)]


class TestEma:
    def test_seeds_with_sma_of_first_period(self):
        assert ema([1.0, 2.0, 3.0, 4.0, 5.0], 3) == [None, None, 2.0, 3.0, 4.0]

    def test_recursion_matches_alpha(self):
        # period 8 -> alpha 2/9; seed at idx 7, first update consumes close 9.0
        out = ema([float(i) for i in range(1, 10)], 8)
        assert out[:7] == [None] * 7
        assert out[7] == pytest.approx(4.5)          # SMA(1..8)
        assert out[8] == pytest.approx(2 / 9 * 9 + 7 / 9 * 4.5)

    def test_constant_series_is_constant(self):
        out = ema([7.0] * 30, 21)
        assert out[20] == pytest.approx(7.0) and out[-1] == pytest.approx(7.0)

    def test_short_series_all_none(self):
        assert ema([1.0, 2.0], 5) == [None, None]

    def test_bad_period_raises(self):
        with pytest.raises(ValueError):
            ema([1.0], 0)


class TestTripleState:
    def test_stacks(self):
        assert triple_state(3.0, 2.0, 1.0) == "up"
        assert triple_state(1.0, 2.0, 3.0) == "down"

    def test_neutral_and_warmup(self):
        assert triple_state(2.0, 3.0, 1.0) == "neutral"
        assert triple_state(1.0, 1.0, 1.0) == "neutral"
        assert triple_state(None, 2.0, 3.0) is None


class TestCrossEvents:
    def test_fires_on_direction_flips_only(self):
        states = [None, "neutral", "up", "up", "neutral", "down", "down", "up"]
        crosses = cross_events(times(8), states)
        assert [(c.index, c.direction, c.prev_state) for c in crosses] == [
            (5, "down", "up"),
            (7, "up", "down"),
        ]
        assert crosses[0].time == T0 + 5 * MIN_MS

    def test_neutral_between_same_states_does_not_refire(self):
        states = ["down", "neutral", "down"]
        assert cross_events(times(3), states) == []

    def test_first_directional_state_is_not_a_cross(self):
        assert cross_events(times(3), [None, "up", "up"]) == []

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            cross_events(times(3), ["up", "up"])


class TestTripleEma:
    def test_decline_then_rally_produces_one_up_cross(self):
        closes = [10.0, 9.0, 8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0,
                  2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 18.0, 20.0]
        res = triple_ema(times(len(closes)), closes, periods=(2, 3, 4))
        assert len(res.fast) == len(closes)
        assert res.states[3] == "down"          # seeded, stacked bearish
        assert res.states[-1] == "up"
        assert len(res.crosses) == 1
        cross = res.crosses[0]
        assert cross.direction == "up" and cross.prev_state == "down"
        assert cross.index >= 10
        assert res.states[cross.index] == "up"
        assert res.state == "up"

    def test_state_property_skips_warmup_nones(self):
        res = triple_ema(times(2), [1.0, 2.0], periods=(2, 3, 4))
        assert res.state is None

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            triple_ema(times(3), [1.0, 2.0])

    def test_periods_must_ascend(self):
        with pytest.raises(ValueError):
            triple_ema(times(5), [1.0] * 5, periods=(3, 2, 1))


class TestTfCrosses:
    def test_row_carries_last_emas_and_state(self):
        closes = [float(40 - i) for i in range(30)]
        info = tf_crosses("5m", STEP["5m"], times(30, STEP["5m"]), closes)
        assert info.tf == "5m" and info.step_ms == STEP["5m"] and info.closed == 30
        assert info.state == "down"
        assert all(v is not None for v in info.emas)
        assert isinstance(info.crosses, tuple)

    def test_empty_window_is_warmup(self):
        info = tf_crosses("4h", STEP["4h"], [], [])
        assert info.state is None and info.closed == 0
        assert info.emas == (None, None, None) and info.crosses == ()


def mk_info(tf: str, state: str | None, cross_ups=(), cross_downs=()) -> TfInfo:
    prev = {"up": "down", "down": "up"}
    crosses = (
        tuple(EmaCross(0, t, "up", prev["up"]) for t in cross_ups)
        + tuple(EmaCross(0, t, "down", prev["down"]) for t in cross_downs)
    )
    crosses = tuple(sorted(crosses, key=lambda c: c.time))
    return TfInfo(tf=tf, step_ms=STEP[tf], state=state, emas=(1.0, 2.0, 3.0),
                  crosses=crosses, closed=200)


class TestCascadePattern:
    def test_chain_across_all_four_timeframes(self):
        base = T0
        # bar opens of the crossing bars; the 5m and 15m bars both open at T0
        opens = [
            base,
            base,
            base + 2 * STEP["1h"],
            base + 2 * STEP["1h"] + STEP["4h"],
        ]
        infos = [
            mk_info("5m", "up", [opens[0]]),
            mk_info("15m", "up", [opens[1]]),
            mk_info("1h", "up", [opens[2]]),
            mk_info("4h", "up", [opens[3]]),
        ]
        now = opens[3] + STEP["4h"] + 10
        pat = cascade_pattern(infos, now_ms=now)
        assert pat["direction"] == "up"
        assert pat["chain"] == ["5m", "15m", "1h", "4h"]
        assert pat["intervals_ms"] == [
            STEP["15m"] - STEP["5m"],                      # 15m close one bar after 5m close
            (opens[2] + STEP["1h"]) - (opens[1] + STEP["15m"]),  # 1h close 2.75 h later
            (opens[3] + STEP["4h"]) - (opens[2] + STEP["1h"]),   # 4h close two bars later
        ]
        assert pat["full_stack"] == "up"

    def test_chain_breaks_at_missing_cross(self):
        infos = [
            mk_info("5m", "up", [T0]),
            mk_info("15m", "up"),                   # state up but no cross
        ]
        pat = cascade_pattern(infos, now_ms=T0 + 20 * STEP["5m"])
        assert pat["direction"] == "up" and pat["chain"] == ["5m"]

    def test_lag_violation_stops_chain(self):
        infos = [
            mk_info("5m", "up", [T0]),
            mk_info("15m", "up", [T0 + STEP["5m"] + 5 * STEP["15m"]]),  # 5 bars late
        ]
        pat = cascade_pattern(infos, now_ms=T0 + 30 * STEP["5m"])
        assert pat["chain"] == ["5m"]
        assert pat["intervals_ms"] == []

    def test_stale_base_cross_does_not_chain(self):
        infos = [mk_info("5m", "up", [T0])]
        pat = cascade_pattern(infos, now_ms=T0 + 400 * STEP["5m"])
        assert pat["direction"] is None and pat["chain"] == []
        assert pat["full_stack"] == "up"            # alignment still reported

    def test_longer_down_chain_wins(self):
        infos = [
            mk_info("5m", "down", cross_ups=[T0], cross_downs=[T0 + STEP["5m"]]),
            mk_info("15m", "down", cross_ups=[T0], cross_downs=[T0 + STEP["5m"] + STEP["15m"]]),
            mk_info("1h", "neutral", cross_ups=[T0]),
        ]
        now = T0 + STEP["5m"] + STEP["15m"] + 2 * STEP["1h"]
        pat = cascade_pattern(infos, now_ms=now)
        assert pat["direction"] == "down"
        assert pat["chain"] == ["5m", "15m"]
        assert pat["full_stack"] is None

    def test_full_stack_requires_single_directional_state(self):
        infos = [mk_info("5m", "up"), mk_info("15m", "neutral")]
        pat = cascade_pattern(infos, now_ms=T0)
        assert pat["full_stack"] is None

    def test_no_data_no_pattern(self):
        pat = cascade_pattern([mk_info("5m", None)], now_ms=T0)
        assert pat == {"direction": None, "chain": [], "intervals_ms": [], "full_stack": None}
