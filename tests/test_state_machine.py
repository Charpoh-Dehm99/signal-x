"""State machine tests: happy path, fail-closed squeeze guard, timeouts."""

from __future__ import annotations

import pytest

from signalx.state_machine import State, StateMachine


def test_happy_path_to_triggered():
    sm = StateMachine()
    assert sm.step({"pump_score": 0.8}) == State.PUMP
    assert sm.step({"compression_qualifies": True, "momentum_decaying": True}) == State.PLATEAU
    assert sm.step({"near_resistance": True, "has_bearish_div": True}) == State.ARMED
    assert sm.step({
        "bos_confirmed": True,
        "breakout_volume_ok": True,
        "squeeze_guard": False,
    }) == State.TRIGGERED
    assert sm.step({}) == State.IN_TRADE
    assert sm.step({"trade_closed": True}) == State.CLOSED
    assert len(sm.history) == 6


def test_single_transition_per_step():
    sm = StateMachine()
    everything = {
        "pump_score": 0.9,
        "compression_qualifies": True,
        "momentum_decaying": True,
        "near_resistance": True,
        "has_bearish_div": True,
        "bos_confirmed": True,
        "breakout_volume_ok": True,
        "squeeze_guard": False,
    }
    # Even with every feature set, one step only advances one state.
    assert sm.step(everything) == State.PUMP


def _armed():
    sm = StateMachine()
    sm.step({"pump_score": 0.8})
    sm.step({"compression_qualifies": True, "momentum_decaying": True})
    sm.step({"near_resistance": True, "has_bearish_div": True})
    assert sm.state == State.ARMED
    return sm


def test_squeeze_guard_blocks_trigger():
    sm = _armed()
    state = sm.step({
        "bos_confirmed": True,
        "breakout_volume_ok": True,
        "squeeze_guard": True,
    })
    assert state == State.ARMED


def test_missing_squeeze_guard_fails_closed():
    sm = _armed()
    state = sm.step({"bos_confirmed": True, "breakout_volume_ok": True})
    assert state == State.ARMED


def test_none_squeeze_guard_fails_closed():
    sm = _armed()
    state = sm.step({
        "bos_confirmed": True,
        "breakout_volume_ok": True,
        "squeeze_guard": None,
    })
    assert state == State.ARMED


def test_invalidation_resets_to_idle():
    sm = _armed()
    assert sm.step({"invalidation": True}) == State.IDLE

    sm2 = StateMachine()
    sm2.step({"pump_score": 0.8})
    sm2.step({"compression_qualifies": True, "momentum_decaying": True})
    assert sm2.step({"invalidation": True}) == State.IDLE


def test_plateau_timeout_returns_to_idle():
    sm = StateMachine(plateau_timeout_bars=3)
    sm.step({"pump_score": 0.8})
    assert sm.state == State.PUMP
    for _ in range(5):
        sm.step({})
    assert sm.state == State.IDLE


def test_arms_on_funding_extreme_alone():
    sm = StateMachine()
    sm.step({"pump_score": 0.8})
    sm.step({"compression_qualifies": True, "momentum_decaying": True})
    assert sm.step({"near_resistance": True, "funding_extreme": True}) == State.ARMED


def test_features_must_be_dict():
    sm = StateMachine()
    with pytest.raises(TypeError):
        sm.step("pump_score=0.9")


def test_invalid_timeouts_rejected():
    with pytest.raises(ValueError):
        StateMachine(plateau_timeout_bars=0)
    with pytest.raises(ValueError):
        StateMachine(armed_timeout_bars=0)


def test_history_records_transitions():
    sm = StateMachine()
    sm.step({"pump_score": 0.8})
    sm.step({"pump_score": 0.8})  # no transition
    assert sm.history == [(State.IDLE, State.PUMP)]
    assert sm.bars_in_state == 1
