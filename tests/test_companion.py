"""Tests for signalx.companion: journal tail -> paper entries/exits."""

from __future__ import annotations

import asyncio

import pytest

from signalx.companion import Companion, CompanionConfig
from signalx.paper import PaperBroker, PaperConfig

NOW = 1_700_000_000_000


class FakeJournal:
    def __init__(self, rows):
        self.rows = rows

    def read_all(self):
        return self.rows


def sig(**over):
    base = {
        "symbol": "AAAUSDT",
        "ts_ms": NOW + 100,
        "event": "SIGNAL",
        "entry": 100.0,
        "stop": 105.0,
        "tp1": 95.0,
        "tp2": 90.0,
        "score": 0.7,
    }
    base.update(over)
    return base


def make_companion(rows, prices, tmp_path):
    cfg = CompanionConfig(
        journal_dir=tmp_path,
        paper_dir=tmp_path,
        halt_path=tmp_path / "HALT",
        paper_config=PaperConfig(taker_fee_bps=0.0, slippage_bps=0.0),
    )
    broker = PaperBroker(cfg.paper_config, now_ms=NOW)
    broker.last_signal_ts = NOW

    async def price_fn(symbol):
        return prices[symbol]

    return Companion(broker, FakeJournal(rows), cfg, price_fn), broker


def test_step_opens_from_new_signal(tmp_path):
    comp, broker = make_companion([sig()], {"AAAUSDT": 100.0}, tmp_path)
    events = asyncio.run(comp.step(NOW + 200))
    assert events[0]["action"] == "OPEN"
    assert "AAAUSDT" in broker.positions


def test_step_does_not_replay_old_signals(tmp_path):
    comp, broker = make_companion([sig(ts_ms=NOW - 1)], {"AAAUSDT": 100.0}, tmp_path)
    events = asyncio.run(comp.step(NOW + 200))
    assert events == []
    assert not broker.positions


def test_step_exits_on_stop(tmp_path):
    rows = [sig()]
    comp, broker = make_companion(rows, {"AAAUSDT": 100.0}, tmp_path)
    asyncio.run(comp.step(NOW + 200))
    comp.price_fn = lambda symbol: _value(106.0)
    comp.journal = FakeJournal([])
    events = asyncio.run(comp.step(NOW + 201))
    assert events[0]["action"] == "EXIT"
    assert events[0]["reason"] == "STOP"
    assert not broker.positions


def test_step_quote_error_is_reported(tmp_path):
    async def boom(symbol):
        raise RuntimeError("feed down")

    cfg = CompanionConfig(
        journal_dir=tmp_path, paper_dir=tmp_path, halt_path=tmp_path / "HALT",
    )
    broker = PaperBroker(cfg.paper_config, now_ms=NOW)
    broker.last_signal_ts = NOW
    comp = Companion(broker, FakeJournal([sig()]), cfg, boom)
    events = asyncio.run(comp.step(NOW + 200))
    assert events[0]["action"] == "QUOTE_ERROR"
    assert broker.last_signal_ts == NOW + 100


def test_manual_halt_blocks_open_but_not_management(tmp_path):
    comp, broker = make_companion([sig()], {"AAAUSDT": 100.0}, tmp_path)
    (tmp_path / "HALT").write_text("stop\\n", encoding="utf-8")
    events = asyncio.run(comp.step(NOW + 200))
    assert events[0]["action"] == "SKIP"
    assert broker.manual_halt is True
    assert not broker.positions


async def _value(v):
    return v
