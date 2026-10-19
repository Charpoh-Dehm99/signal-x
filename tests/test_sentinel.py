"""Tests for signalx.sentinel: pump score, feature bridge, plan, tracker chain."""

from __future__ import annotations

import pytest

from signalx.models import Side
from signalx.sentinel import (
    SentinelError,
    SymbolTracker,
    features_from_snapshot,
    journal_rows,
    latest_journal_states,
    pump_score,
    record_to_signal,
    rehydrate_trackers,
    restore_state,
    trigger_plan,
)
from signalx.state_machine import State
from signalx.validation import ValidationError


def snap(**over):
    base = {
        "symbol": "TESTUSDT",
        "price": 100.0,
        "atr": 1.0,
        "change_24h_pct": 20.0,
        "vol_z": 3.0,
        "compression": None,
        "events": [],
        "swing_high": None,
        "swing_low": None,
        "bos_down": False,
        "has_bearish_div": False,
        "momentum_decay": False,
        "funding_pctile": None,
        "squeeze_active": None,
        "vol_ratio": 1.0,
    }
    base.update(over)
    return base


# ----------------------------------------------------------------- pump_score


class TestPumpScore:
    def test_both_missing_is_zero(self):
        assert pump_score(None, None) == 0.0

    def test_change_at_floor_is_zero(self):
        assert pump_score(15.0, None) == 0.0

    def test_saturates_at_one(self):
        assert pump_score(60.0, 10.0) == pytest.approx(1.0)
        assert pump_score(100.0, 50.0) == pytest.approx(1.0)

    def test_blend_midpoint(self):
        # change halfway (37.5) -> 0.3, vol_z halfway (5) -> 0.2
        assert pump_score(37.5, 5.0) == pytest.approx(0.5)

    def test_monotonic_in_change(self):
        assert pump_score(30.0, 3.0) < pump_score(45.0, 3.0)

    def test_non_numeric_raises(self):
        with pytest.raises(SentinelError):
            pump_score("hot", 3.0)


# ------------------------------------------------------------------- features


class TestFeatures:
    def test_non_dict_raises(self):
        with pytest.raises(SentinelError):
            features_from_snapshot(["not", "a", "dict"])

    def test_missing_key_raises(self):
        s = snap()
        del s["atr"]
        with pytest.raises(SentinelError):
            features_from_snapshot(s)

    def test_nonpositive_price_raises(self):
        with pytest.raises(SentinelError):
            features_from_snapshot(snap(price=0.0))

    def test_volume_gate_fails_closed_when_unknown(self):
        f = features_from_snapshot(snap(vol_ratio=None))
        assert f["breakout_volume_ok"] is False

    def test_volume_gate(self):
        assert features_from_snapshot(snap(vol_ratio=1.5))["breakout_volume_ok"] is True
        assert features_from_snapshot(snap(vol_ratio=1.49))["breakout_volume_ok"] is False

    def test_invalidation_above_swing_high_buffer(self):
        assert features_from_snapshot(snap(price=101.6, swing_high=101.0))["invalidation"] is True
        assert features_from_snapshot(snap(price=101.4, swing_high=101.0))["invalidation"] is False

    def test_squeeze_guard_none_passthrough(self):
        assert features_from_snapshot(snap())["squeeze_guard"] is None

    def test_funding_extreme_boundary(self):
        assert features_from_snapshot(snap(funding_pctile=90.0))["funding_extreme"] is True
        assert features_from_snapshot(snap(funding_pctile=89.9))["funding_extreme"] is False

    def test_near_resistance_from_events(self):
        assert features_from_snapshot(snap(events=["NEAR_RESISTANCE"]))["near_resistance"] is True
        assert features_from_snapshot(snap(events=["BOS_DOWN"]))["near_resistance"] is False


# ------------------------------------------------------------- trigger_plan


class TestTriggerPlan:
    def test_swing_high_stop_and_measured_move(self):
        p = trigger_plan(snap(swing_high=101.0, compression={"qualifies": True, "height": 2.0}))
        assert p["stop"] == pytest.approx(101.5)
        assert p["risk"] == pytest.approx(1.5)
        assert p["tp1"] == pytest.approx(98.0)
        assert p["rr1"] == pytest.approx(4.0 / 3.0)
        assert p["tp2"] == pytest.approx(95.5)
        assert p["rr2"] == pytest.approx(3.0)
        assert p["stop_source"] == "swing_high+buffer"
        assert p["tp1_source"] == "measured_move"

    def test_fallbacks_without_structure_or_box(self):
        p = trigger_plan(snap())
        assert p["stop"] == pytest.approx(102.0)
        assert p["tp1"] == pytest.approx(96.0)
        assert p["tp2"] == pytest.approx(94.0)
        assert p["stop_source"] == "fallback_2atr"
        assert p["tp1_source"] == "fallback_2r"

    def test_stop_below_entry_raises(self):
        with pytest.raises(SentinelError):
            trigger_plan(snap(swing_high=95.0))

    def test_result_fails_loud_on_missing_keys(self):
        with pytest.raises(SentinelError):
            trigger_plan({"price": 100.0, "atr": 1.0})


# ------------------------------------------------------------ tracker chain


def step_to_armed(t, now=1_000):
    r1 = t.update(snap(change_24h_pct=60.0, vol_z=6.0), now_ms=now)
    assert r1["state"] == "PUMP_DETECTED"
    r2 = t.update(
        snap(
            change_24h_pct=60.0,
            vol_z=6.0,
            compression={"qualifies": True, "height": 2.0},
            momentum_decay=True,
        ),
        now_ms=now + 1,
    )
    assert r2["state"] == "PLATEAU_BUILDING"
    r3 = t.update(
        snap(change_24h_pct=60.0, vol_z=6.0, events=["RESISTANCE_TOUCHED"], has_bearish_div=True),
        now_ms=now + 2,
    )
    assert r3["state"] == "ARMED"
    return r3


TRIGGER_SNAP = dict(
    change_24h_pct=60.0,
    vol_z=6.0,
    events=["RESISTANCE_TOUCHED"],
    has_bearish_div=True,
    bos_down=True,
    vol_ratio=2.0,
    swing_high=101.0,
    squeeze_active=False,
    compression={"qualifies": True, "height": 2.0},
)


class TestSymbolTracker:
    def test_invalid_symbol_raises(self):
        with pytest.raises(ValidationError):
            SymbolTracker("bad symbol!")

    def test_malformed_snapshot_raises(self):
        with pytest.raises(SentinelError):
            SymbolTracker("TESTUSDT").update({"price": 1.0}, now_ms=1)

    def test_bad_now_ms_raises(self):
        t = SymbolTracker("TESTUSDT")
        with pytest.raises(SentinelError):
            t.update(snap(), now_ms=0)
        with pytest.raises(SentinelError):
            t.update(snap(), now_ms=True)

    def test_chain_arms_but_unknown_guard_blocks_trigger(self):
        t = SymbolTracker("TESTUSDT")
        step_to_armed(t)
        # squeeze_active defaults to None -> fail closed -> stays ARMED
        r = t.update(snap(**{k: v for k, v in TRIGGER_SNAP.items() if k != "squeeze_active"}), now_ms=2_000)
        assert r["state"] == "ARMED"
        assert r["signal"] is None

    def test_chain_arms_but_squeeze_blocks_trigger(self):
        t = SymbolTracker("TESTUSDT")
        step_to_armed(t)
        r = t.update(snap(**{**TRIGGER_SNAP, "squeeze_active": True}), now_ms=2_000)
        assert r["state"] == "ARMED"
        assert r["signal"] is None

    def test_full_chain_triggers_with_plan(self):
        t = SymbolTracker("TESTUSDT")
        step_to_armed(t)
        r = t.update(snap(**TRIGGER_SNAP), now_ms=2_000)
        assert r["state"] == "TRIGGERED"
        assert r["transitioned"] is True
        sig = r["signal"]
        assert sig is not None
        assert sig.symbol == "TESTUSDT"
        assert sig.entry == pytest.approx(100.0)
        assert sig.stop == pytest.approx(101.5)
        assert sig.tp1 == pytest.approx(98.0)
        assert sig.score == pytest.approx(0.84)
        assert "RESISTANCE_TOUCHED" in sig.reasons
        assert "BEARISH_DIVERGENCE" not in sig.reasons  # not in this snapshot's events
        assert any(rr.startswith("BREAKOUT_VOL_x2.0") for rr in sig.reasons)
        assert t.signals_emitted == 1
        # one more step: TRIGGERED -> IN_TRADE, no second signal
        r2 = t.update(snap(**TRIGGER_SNAP), now_ms=2_001)
        assert r2["state"] == "IN_TRADE"
        assert r2["signal"] is None

    def test_invalidation_from_armed_goes_idle(self):
        t = SymbolTracker("TESTUSDT")
        step_to_armed(t)
        r = t.update(snap(price=102.5, swing_high=101.0), now_ms=2_002)
        assert r["state"] == "IDLE"

    def test_journal_rows_empty_for_non_transition(self):
        t = SymbolTracker("TESTUSDT")
        t.update(snap(change_24h_pct=5.0, vol_z=1.0), now_ms=1)
        r = t.update(snap(change_24h_pct=5.0, vol_z=1.0), now_ms=2)
        assert r["transitioned"] is False
        assert journal_rows(r) == []

    def test_journal_rows_transition_and_signal(self):
        t = SymbolTracker("TESTUSDT")
        step_to_armed(t)
        r = t.update(snap(**TRIGGER_SNAP), now_ms=2_000)
        rows = journal_rows(r)
        assert len(rows) == 2
        kinds = {row["event"] for row in rows}
        assert kinds == {"TRANSITION", "SIGNAL"}
        sig_row = next(row for row in rows if row["event"] == "SIGNAL")
        assert sig_row["state"] == "TRIGGERED"
        assert sig_row["prev_state"] == "ARMED"
        assert sig_row["entry"] == pytest.approx(100.0)
        assert sig_row["stop"] == pytest.approx(101.5)
        assert sig_row["reasons"] is not None
        tr_row = next(row for row in rows if row["event"] == "TRANSITION")
        assert tr_row["entry"] is None
        assert tr_row["features_json"].startswith("{")

    def test_record_to_signal_adapter(self):
        t = SymbolTracker("TESTUSDT")
        step_to_armed(t)
        r = t.update(snap(**TRIGGER_SNAP), now_ms=2_000)
        signal = record_to_signal(r["signal"])
        assert signal.side is Side.SHORT
        assert signal.symbol == "TESTUSDT"
        assert signal.score == pytest.approx(0.84)


# ---------------------------------------------------------------- rehydration

BAR_MS = 300_000  # 5m


class TestRehydration:
    def test_latest_journal_states_last_row_wins(self):
        rows = [
            {"symbol": "AAAUSDT", "state": "ARMED", "ts_ms": 1_000},
            {"symbol": "BBBUSDT", "state": "PUMP_DETECTED", "ts_ms": 1_500},
            {"symbol": "AAAUSDT", "state": "TRIGGERED", "ts_ms": 2_000},
        ]
        latest = latest_journal_states(rows)
        assert latest["AAAUSDT"] == {"state": "TRIGGERED", "ts_ms": 2_000}
        assert latest["BBBUSDT"] == {"state": "PUMP_DETECTED", "ts_ms": 1_500}

    def test_latest_journal_states_fails_loud(self):
        with pytest.raises(SentinelError):
            latest_journal_states([{"symbol": "AAAUSDT", "state": "MOON", "ts_ms": 1}])
        with pytest.raises(SentinelError):
            latest_journal_states(["not a dict"])
        with pytest.raises(SentinelError):
            latest_journal_states([{"symbol": None, "state": "ARMED", "ts_ms": 1}])

    def test_restore_state_seeds_elapsed_bars(self):
        t = SymbolTracker("TESTUSDT")
        restore_state(t, "ARMED", ts_ms=1_000_000, bar_ms=BAR_MS, now_ms=1_000_000 + 24 * BAR_MS)
        assert t.state == State.ARMED
        assert t.sm.bars_in_state == 24

    def test_restore_state_without_timing_leaves_bar_count_zero(self):
        t = SymbolTracker("TESTUSDT")
        restore_state(t, "PUMP_DETECTED")
        assert t.state == State.PUMP
        assert t.sm.bars_in_state == 0

    def test_restore_state_idle_is_noop_and_unknown_raises(self):
        t = SymbolTracker("TESTUSDT")
        restore_state(t, "IDLE", ts_ms=1_000, bar_ms=BAR_MS, now_ms=9_000_000)
        assert t.state == State.IDLE
        with pytest.raises(SentinelError):
            restore_state(t, "MOON")

    def test_rehydrate_restores_matching_symbols_only(self):
        rows = [
            {"symbol": "AAAUSDT", "state": "ARMED", "ts_ms": 1_000},
            {"symbol": "CCCUSDT", "state": "TRIGGERED", "ts_ms": 1_000},
        ]
        trackers = rehydrate_trackers(rows, ["AAAUSDT", "BBBUSDT"], bar_ms=BAR_MS, now_ms=1_000)
        assert set(trackers) == {"AAAUSDT", "BBBUSDT"}
        assert trackers["AAAUSDT"].state == State.ARMED
        assert trackers["BBBUSDT"].state == State.IDLE

    def test_rehydrated_armed_tracker_triggers_normally(self):
        rows = [{"symbol": "TESTUSDT", "state": "ARMED", "ts_ms": 1}]
        trackers = rehydrate_trackers(rows, ["TESTUSDT"])
        r = trackers["TESTUSDT"].update(snap(**TRIGGER_SNAP), now_ms=2_000)
        assert r["state"] == "TRIGGERED"
        assert r["signal"] is not None

    def test_rehydrated_triggered_tracker_never_reemits(self):
        rows = [{"symbol": "TESTUSDT", "state": "TRIGGERED", "ts_ms": 1}]
        trackers = rehydrate_trackers(rows, ["TESTUSDT"])
        r = trackers["TESTUSDT"].update(snap(**TRIGGER_SNAP), now_ms=2_000)
        assert r["state"] == "IN_TRADE"
        assert r["signal"] is None
