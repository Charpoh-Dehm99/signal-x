"""Backtest simulation tests: fill, stop-first, gaps, summary."""

from __future__ import annotations

from signalx.models import Kline
from signalx.sniper_v3.backtest import TradeResult, simulate_trade, summarize
from signalx.sniper_v3.models import SniperDecision


def _decision(direction: str = "long") -> SniperDecision:
    long = direction == "long"
    return SniperDecision(
        symbol="TESTUSDT",
        as_of_ms=1_000,
        direction=direction,
        entry=100.0,
        stop=99.0 if long else 101.0,
        tp1=102.0 if long else 98.0,
        tp2=104.0 if long else 96.0,
        rr=2.0,
        confidence=0.6,
        expected_value_r=0.2,
        setup_score=0.6,
        trigger_score=0.6,
        zone_score=0.6,
        sweep_quality=0.6,
        divergence_strength=0.0,
        htf_strength=0.6,
        funding_penalty=0.0,
        oi_penalty=0.0,
        reasons=(),
        features={},
    )


def _bar(i: int, o: float, h: float, l: float, c: float) -> Kline:
    ot = 1_000 + i * 300_000
    return Kline(ot, o, h, l, c, 100.0, ot + 300_000 - 1, 10_000.0, 10, 50.0, 5_000.0)


def test_long_target_hit():
    future = [_bar(0, 100.0, 102.5, 99.5, 102.0)]
    result = simulate_trade(_decision("long"), future)
    assert result is not None
    assert result.outcome == "tp1"
    assert result.r_multiple > 0
    assert result.entry_ts == future[0].open_time
    assert result.bars_held == 1


def test_long_stop_hit():
    future = [_bar(0, 100.0, 100.5, 98.9, 99.1)]
    result = simulate_trade(_decision("long"), future)
    assert result is not None
    assert result.outcome == "stop"
    assert result.r_multiple < 0


def test_stop_first_when_bar_spans_both():
    future = [_bar(0, 100.0, 103.0, 98.9, 101.0)]
    result = simulate_trade(_decision("long"), future)
    assert result is not None
    assert result.outcome == "stop"


def test_gap_through_stop_uses_open():
    future = [_bar(0, 98.5, 99.0, 98.0, 98.5)]
    result = simulate_trade(_decision("long"), future)
    assert result is not None
    assert result.outcome == "stop"
    assert result.exit < 99.0


def test_short_target_and_stop():
    target = simulate_trade(
        _decision("short"), [_bar(0, 100.0, 100.5, 97.5, 98.0)]
    )
    assert target is not None and target.outcome == "tp1"
    stopped = simulate_trade(
        _decision("short"), [_bar(0, 100.0, 101.1, 99.8, 101.0)]
    )
    assert stopped is not None and stopped.outcome == "stop"


def test_timeout_and_summary():
    future = [_bar(i, 100.0, 100.2, 99.8, 100.0) for i in range(5)]
    result = simulate_trade(_decision("long"), future)
    assert result is not None
    assert result.outcome == "timeout"
    summary = summarize([result])
    assert summary["n"] == 1
    assert summary["outcomes"] == {"timeout": 1}
    assert summary["mean_r"] is not None
    assert summarize([])["n"] == 0
