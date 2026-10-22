"""Tests for signalx.paper: sizing, exits, halt rules, persistence."""

from __future__ import annotations

import json

import pytest

from signalx.paper import (
    PaperBroker,
    PaperConfig,
    PaperError,
    load_paper_state,
    save_paper_state,
)

NOW = 1_700_000_000_000


def signal(**over):
    base = {
        "symbol": "AAAUSDT",
        "ts_ms": NOW - 1000,
        "entry": 100.0,
        "stop": 105.0,
        "tp1": 95.0,
        "tp2": 90.0,
        "score": 0.7,
    }
    base.update(over)
    return base


def zero_cost(**over):
    opts = {"taker_fee_bps": 0.0, "slippage_bps": 0.0}
    opts.update(over)
    return PaperConfig(**opts)


class TestConfig:
    def test_defaults_are_paper_safe(self):
        cfg = PaperConfig()
        assert cfg.starting_equity == 10_000.0
        assert cfg.risk_pct == 0.0025
        assert cfg.max_leverage == 1.0

    @pytest.mark.parametrize("kwargs", [
        {"starting_equity": 0},
        {"risk_pct": 0},
        {"risk_pct": 0.06},
        {"max_leverage": 0.5},
        {"taker_fee_bps": 500},
        {"daily_loss_limit_pct": 0},
        {"max_hold_ms": 0},
        {"tp1_fraction": 1.5},
        {"min_notional": 0},
        {"max_entry_premium_pct": -1},
    ])
    def test_bad_config_raises(self, kwargs):
        with pytest.raises(PaperError):
            PaperConfig(**kwargs)


class TestEntries:
    def test_sizes_risk_and_opens_short(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        r = b.open_from_signal(signal(), 100.0, NOW)
        assert r["opened"] is True
        assert r["qty"] == pytest.approx(5.0)          # $25 risk / $5 per unit
        assert r["notional"] == pytest.approx(500.0)   # 1x cap not binding
        assert "AAAUSDT" in b.positions

    def test_slippage_worsens_entry(self):
        b = PaperBroker(zero_cost(slippage_bps=5.0), now_ms=NOW)
        r = b.open_from_signal(signal(), 100.0, NOW)
        assert r["opened"] is True
        assert r["entry"] == pytest.approx(99.95)
        assert r["qty"] == pytest.approx(25.0 / 5.05)

    def test_skips_when_target_already_reached(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        r = b.open_from_signal(signal(), 94.0, NOW)
        assert r["opened"] is False
        assert "target" in r["reason"]

    def test_skips_when_stop_already_breached(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        r = b.open_from_signal(signal(), 106.0, NOW)
        assert r["opened"] is False
        assert "stop" in r["reason"]

    def test_skips_on_entry_premium(self):
        b = PaperBroker(zero_cost(max_entry_premium_pct=0.5), now_ms=NOW)
        r = b.open_from_signal(signal(), 100.6, NOW)
        assert r["opened"] is False
        assert "premium" in r["reason"]

    def test_skips_duplicate_symbol(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        assert b.open_from_signal(signal(), 100.0, NOW)["opened"] is True
        r = b.open_from_signal(signal(ts_ms=NOW - 500), 99.0, NOW)
        assert r["opened"] is False
        assert "already open" in r["reason"]

    def test_skips_non_short_side(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        r = b.open_from_signal(signal(side="LONG"), 100.0, NOW)
        assert r["opened"] is False
        assert "SHORT" in r["reason"]

    def test_last_signal_ts_advances_even_on_skip(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        b.open_from_signal(signal(ts_ms=NOW - 5000), 94.0, NOW)
        assert b.last_signal_ts == NOW - 5000


class TestExits:
    def test_stop_exit_records_loss(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        b.open_from_signal(signal(), 100.0, NOW)
        fills = b.mark_prices({"AAAUSDT": 106.0}, NOW + 1)
        assert len(fills) == 1
        assert fills[0]["reason"] == "STOP"
        assert fills[0]["net"] == pytest.approx(-30.0)
        assert b.equity == pytest.approx(9970.0)

    def test_tp1_partial_moves_stop_to_breakeven(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        b.open_from_signal(signal(), 100.0, NOW)
        fills = b.mark_prices({"AAAUSDT": 94.0}, NOW + 1)
        assert fills[0]["reason"] == "TP1"
        assert fills[0]["net"] == pytest.approx(15.0)   # (100-94) * 2.5
        pos = b.positions["AAAUSDT"]
        assert pos.remaining_qty == pytest.approx(2.5)
        assert pos.stop == pytest.approx(100.0)

    def test_tp2_closes_remainder(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        b.open_from_signal(signal(), 100.0, NOW)
        b.mark_prices({"AAAUSDT": 94.0}, NOW + 1)
        fills = b.mark_prices({"AAAUSDT": 89.0}, NOW + 2)
        assert fills[0]["reason"] == "TP2"
        assert fills[0]["net"] == pytest.approx(27.5)   # (100-89) * 2.5
        assert "AAAUSDT" not in b.positions
        assert b.realized_pnl == pytest.approx(42.5)

    def test_time_stop_closes(self):
        b = PaperBroker(zero_cost(max_hold_ms=1000), now_ms=NOW)
        b.open_from_signal(signal(), 100.0, NOW)
        fills = b.mark_prices({"AAAUSDT": 100.0}, NOW + 1001)
        assert fills[0]["reason"] == "TIME"
        assert "AAAUSDT" not in b.positions

    def test_stop_wins_if_stop_and_target_same_tick(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        b.open_from_signal(signal(), 100.0, NOW)
        fills = b.mark_prices({"AAAUSDT": 106.0}, NOW + 1)
        assert fills[0]["reason"] == "STOP"

    def test_fees_are_deducted(self):
        b = PaperBroker(PaperConfig(taker_fee_bps=10.0, slippage_bps=0.0), now_ms=NOW)
        b.open_from_signal(signal(), 100.0, NOW)
        fills = b.mark_prices({"AAAUSDT": 106.0}, NOW + 1)
        # fees = (100 + 106) * 5 * 0.001 = 1.03
        assert fills[0]["gross"] == pytest.approx(-30.0)
        assert fills[0]["fees"] == pytest.approx(1.03)
        assert fills[0]["net"] == pytest.approx(-31.03)


class TestHalts:
    def test_daily_loss_blocks_new_entries(self):
        b = PaperBroker(zero_cost(daily_loss_limit_pct=0.2), now_ms=NOW)
        b.open_from_signal(signal(), 100.0, NOW)
        b.mark_prices({"AAAUSDT": 106.0}, NOW + 1)     # -$30 > 0.2% of $10k
        assert b.halted is True
        r = b.open_from_signal(signal(symbol="BBBUSDT"), 100.0, NOW + 2)
        assert r["opened"] is False
        assert "daily loss" in r["reason"]

    def test_new_day_resets_daily_halt(self):
        b = PaperBroker(zero_cost(daily_loss_limit_pct=0.2), now_ms=NOW)
        b.open_from_signal(signal(), 100.0, NOW)
        b.mark_prices({"AAAUSDT": 106.0}, NOW + 1)
        assert b.halted is True
        b.roll_day(NOW + 86_400_000)
        assert b.halted is False
        assert b.realized_today == 0.0

    def test_manual_halt_blocks_entries(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        b.set_manual_halt(True, "test halt")
        r = b.open_from_signal(signal(), 100.0, NOW)
        assert r["opened"] is False
        assert "halt" in r["reason"]


class TestPersistence:
    def test_round_trip_state(self, tmp_path):
        path = tmp_path / "state.json"
        b = PaperBroker(zero_cost(), now_ms=NOW)
        b.open_from_signal(signal(), 100.0, NOW)
        save_paper_state(b, path, NOW + 1)
        loaded = load_paper_state(path, now_ms=NOW + 2)
        assert loaded is not None
        assert loaded.equity == pytest.approx(b.equity)
        assert "AAAUSDT" in loaded.positions
        assert loaded.positions["AAAUSDT"].remaining_qty == pytest.approx(5.0)

    def test_load_missing_is_none(self, tmp_path):
        assert load_paper_state(tmp_path / "missing.json", now_ms=NOW) is None

    def test_corrupt_state_raises(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"equity": "oops"}), encoding="utf-8")
        with pytest.raises(PaperError):
            load_paper_state(path, now_ms=NOW)

    def test_snapshot_shape(self):
        b = PaperBroker(zero_cost(), now_ms=NOW)
        b.open_from_signal(signal(), 100.0, NOW)
        snap = b.snapshot(NOW + 10)
        assert snap["mode"] == "paper"
        assert snap["equity"] == pytest.approx(10_000.0)
        assert len(snap["positions"]) == 1
        assert snap["risk_used"] == pytest.approx(25.0)
