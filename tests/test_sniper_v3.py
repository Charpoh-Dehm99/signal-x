"""SNIPER V3 primitive and engine tests, including a hand-built MTF scenario."""

from __future__ import annotations

import pytest

from signalx.models import Kline
from signalx.sniper_v3 import MastermindScorer, SniperEngine
from signalx.sniper_v3.primitives import (
    build_zones,
    detect_sweep,
    require_atr,
    structure_view,
    trigger_view,
)

START_MS = 1_600_000_000_000


def _series(
    closes: list[float],
    interval_ms: int,
    *,
    spread: float = 0.02,
    volume: float = 1_000.0,
) -> list[Kline]:
    out: list[Kline] = []
    for i, close in enumerate(closes):
        open_time = START_MS + i * interval_ms
        out.append(
            Kline(
                open_time=open_time,
                open=close,
                high=close + spread,
                low=close - spread,
                close=close,
                volume=volume,
                close_time=open_time + interval_ms - 1,
                quote_volume=volume * close,
                trades=10,
                taker_buy_base=volume / 2.0,
                taker_buy_quote=volume * close / 2.0,
            )
        )
    return out


def _mirror(klines: list[Kline], level: float = 300.0) -> list[Kline]:
    return [
        Kline(
            open_time=k.open_time,
            open=level - k.open,
            high=level - k.low,
            low=level - k.high,
            close=level - k.close,
            volume=k.volume,
            close_time=k.close_time,
            quote_volume=k.quote_volume,
            trades=k.trades,
            taker_buy_base=k.taker_buy_base,
            taker_buy_quote=k.taker_buy_quote,
        )
        for k in klines
    ]


def _scenario() -> tuple[list[Kline], list[Kline], list[Kline]]:
    """1H uptrend + 15m sweep/reclaim + 5m BOS with volume."""
    one_hour_closes: list[float] = []
    price = 100.0
    for i in range(100):
        price += 0.5 if i % 7 < 5 else -0.3
        one_hour_closes.append(price)
    klines_1h = _series(one_hour_closes, 3_600_000)

    fifteen_closes: list[float] = []
    price = 126.7
    for i in range(400):
        price += 0.06 if i % 10 < 6 else -0.05
        fifteen_closes.append(price)
    klines_15m = _series(fifteen_closes, 900_000)

    five_closes: list[float] = []
    price = 126.4
    for i in range(1_200):
        price += -0.01 if i % 9 < 5 else 0.008
        five_closes.append(price)
    klines_5m = _series(five_closes, 300_000)

    # The test scenario must end at the same wall-clock instant on all TFs.
    now_ms = klines_1h[-1].close_time
    assert klines_15m[-1].close_time == now_ms
    assert klines_5m[-1].close_time == now_ms

    # Make the 15m support sweep explicit and the 5m breakout explicit.
    fifteen_base = now_ms - 3 * 900_000
    klines_15m[-3:] = [
        Kline(
            fifteen_base, 127.0, 127.05, 126.9, 127.0,
            2_000, fifteen_base + 900_000 - 1, 254_000, 10, 1_000, 127_000,
        ),
        Kline(
            fifteen_base + 900_000, 127.0, 127.1, 126.95, 127.05,
            1_800, fifteen_base + 2 * 900_000 - 1, 228_690, 10, 900, 114_345,
        ),
        Kline(
            fifteen_base + 2 * 900_000, 127.1, 127.65, 126.9, 127.6,
            2_500, now_ms, 319_000, 10, 1_250, 159_500,
        ),
    ]
    five_base = now_ms - 2 * 300_000
    klines_5m[-2:] = [
        Kline(
            five_base, 127.0, 127.1, 126.9, 127.0,
            1_000, five_base + 300_000 - 1, 127_000, 10, 500, 63_500,
        ),
        Kline(
            five_base + 300_000, 127.2, 127.65, 127.1, 127.6,
            4_000, now_ms, 510_400, 10, 2_300, 293_480,
        ),
    ]
    return klines_1h, klines_15m, klines_5m


@pytest.fixture(scope="module")
def scenario() -> tuple[list[Kline], list[Kline], list[Kline]]:
    return _scenario()


def test_structure_view_long_and_short(scenario):
    one_h, _, _ = scenario
    long_view = structure_view(one_h)
    short_view = structure_view(_mirror(one_h))
    assert long_view.bias == "long"
    assert long_view.strength == pytest.approx(1.0)
    assert short_view.bias == "short"
    assert short_view.strength == pytest.approx(1.0)


def test_zones_merge_and_bounded_scores(scenario):
    one_h, fifteen, _ = scenario
    now_ms = one_h[-1].close_time
    atr_15m = require_atr(fifteen)
    zones = build_zones(one_h, fifteen, atr_value=atr_15m, now_ms=now_ms)
    assert zones
    assert all(0.0 <= z.score <= 1.0 for z in zones)
    assert any(z.touches >= 2 for z in zones)
    assert all(z.low <= z.high for z in zones)
    # 1H zones should be represented in the merged book.
    assert any(z.tf == "1h" for z in zones)


def test_sweep_and_trigger_are_directional(scenario):
    one_h, fifteen, five = scenario
    now_ms = one_h[-1].close_time
    atr_15m = require_atr(fifteen)
    zones = build_zones(one_h, fifteen, atr_value=atr_15m, now_ms=now_ms)
    support = [z for z in zones if z.low <= 127.6 <= z.high]
    assert support
    zone = max(support, key=lambda z: z.score)
    sweep = detect_sweep(fifteen, zone, "long", atr_value=atr_15m)
    assert sweep is not None
    assert sweep.direction == "long"
    assert sweep.extreme < zone.low
    assert sweep.close >= zone.low

    trigger = trigger_view(five, "long", atr_value=require_atr(five))
    assert trigger.micro_bos is True
    assert trigger.volume_ratio > 1.1
    assert trigger.score >= 0.35


def test_scorer_orders_strong_features_above_weak():
    scorer = MastermindScorer()
    strong = scorer.features(
        htf_strength=1.0,
        zone_score=0.9,
        sweep_quality=0.9,
        divergence_strength=0.8,
        trigger_score=0.9,
        rr=3.0,
        volume_ratio=3.0,
        delta_z=1.5,
    )
    weak = scorer.features(
        htf_strength=0.1,
        zone_score=0.1,
        sweep_quality=0.1,
        divergence_strength=0.0,
        trigger_score=0.1,
        rr=1.0,
        volume_ratio=1.0,
        delta_z=None,
    )
    strong_conf, strong_ev = scorer.score(strong, 3.0)
    weak_conf, weak_ev = scorer.score(weak, 1.0)
    assert strong_conf > weak_conf
    assert strong_ev > weak_ev
    assert 0.0 <= scorer.confidence(strong) <= 1.0


def test_engine_long_scenario(scenario):
    one_h, fifteen, five = scenario
    decision = SniperEngine("TESTUSDT").evaluate(one_h, fifteen, five)
    assert decision is not None
    assert decision.direction == "long"
    assert decision.symbol == "TESTUSDT"
    assert decision.entry == pytest.approx(127.6)
    assert decision.stop < decision.entry < decision.tp1 < decision.tp2
    assert decision.rr >= 1.5
    assert decision.confidence >= 0.5
    assert decision.expected_value_r > 0
    assert "1H_LONG" in decision.reasons


def test_engine_short_mirror(scenario):
    one_h, fifteen, five = scenario
    decision = SniperEngine("TESTUSDT").evaluate(
        _mirror(one_h), _mirror(fifteen), _mirror(five)
    )
    assert decision is not None
    assert decision.direction == "short"
    assert decision.stop > decision.entry > decision.tp1 > decision.tp2
    assert decision.rr >= 1.5


def test_engine_rejects_5m_breakout_against_1h_bias(scenario):
    one_h, fifteen, five = scenario
    bad = list(five)
    last = bad[-1]
    bad[-1] = Kline(
        last.open_time,
        last.open,
        last.high,
        last.low,
        last.close - 1.0,
        last.volume,
        last.close_time,
        last.quote_volume,
        last.trades,
        last.taker_buy_base,
        last.taker_buy_quote,
    )
    assert SniperEngine("TESTUSDT").evaluate(one_h, fifteen, bad) is None


def test_engine_as_of_is_causal(scenario):
    one_h, fifteen, five = scenario
    engine = SniperEngine("TESTUSDT")
    now_ms = five[-1].close_time
    original = engine.evaluate(one_h, fifteen, five)

    future_1h = list(one_h) + [
        Kline(
            one_h[-1].open_time + 3_600_000,
            1.0, 1_000.0, 0.1, 1_000.0,
            1.0, one_h[-1].close_time + 3_600_000,
            1.0, 1, 0.5, 0.5,
        )
    ]
    future_15m = list(fifteen) + [
        Kline(
            fifteen[-1].open_time + 900_000,
            1.0, 1_000.0, 0.1, 1_000.0,
            1.0, fifteen[-1].close_time + 900_000,
            1.0, 1, 0.5, 0.5,
        )
    ]
    future_5m = list(five) + [
        Kline(
            five[-1].open_time + 300_000,
            1.0, 1_000.0, 0.1, 1_000.0,
            1.0, five[-1].close_time + 300_000,
            1.0, 1, 0.5, 0.5,
        )
    ]
    replay = engine.evaluate(future_1h, future_15m, future_5m, as_of_ms=now_ms)
    assert replay == original


def test_engine_diagnostics_explain_success(scenario):
    one_h, fifteen, five = scenario
    decision, diag = SniperEngine("TESTUSDT").evaluate_detailed(one_h, fifteen, five)
    assert decision is not None
    assert diag.status == "setup"
    assert diag.failed_gate is None
    assert diag.direction == "long"
    assert all(check.state == "pass" for check in diag.checks)
    assert diag.metrics["htf_bias"] == "long"
    assert diag.metrics["confidence"] == pytest.approx(decision.confidence)


def test_engine_diagnostics_report_first_failed_gate(scenario):
    one_h, fifteen, five = scenario
    bad = list(five)
    last = bad[-1]
    bad[-1] = Kline(
        last.open_time,
        last.open,
        last.high,
        last.low,
        last.close - 1.0,
        last.volume,
        last.close_time,
        last.quote_volume,
        last.trades,
        last.taker_buy_base,
        last.taker_buy_quote,
    )
    decision, diag = SniperEngine("TESTUSDT").evaluate_detailed(one_h, fifteen, bad)
    assert decision is None
    assert diag.status == "no_setup"
    assert diag.failed_gate is not None
    assert diag.reason
    assert any(check.state == "fail" for check in diag.checks)


def test_engine_diagnostics_reports_missing_bars():
    decision, diag = SniperEngine("TESTUSDT").evaluate_detailed([], [], [])
    assert decision is None
    assert diag.status == "no_setup"
    assert diag.failed_gate == "bars"
    assert "no closed 5m" in diag.reason
