"""Tests for signalx.stream: frame parsing, cascade detection, backoff."""

from __future__ import annotations

import pytest

from signalx.stream import (
    KlineEvent,
    LiquidationCascadeDetector,
    LiquidationOrder,
    StreamError,
    backoff_delays,
    parse_liquidation_payload,
    parse_kline_payload,
    parse_stream_message,
)

TS = 1_700_000_000_000


def kline_frame(closed=False, over=None):
    k = {
        "t": TS, "T": TS + 59_999, "s": "TESTUSDT", "i": "1m",
        "o": "100.0", "c": "101.0", "h": "101.5", "l": "99.5",
        "v": "123.4", "q": "12400.5", "n": 42, "x": closed,
        "V": "60.0", "Q": "6000.0",
    }
    if over:
        k.update(over)
    return {"e": "kline", "k": k}


def force_frame(side="SELL", price="100.0", qty="5.0", ts=TS):
    return {"e": "forceOrder", "o": {"s": "TESTUSDT", "S": side, "p": price, "q": qty, "T": ts}}


def liq(side="LONG", ts=TS, notional=100.0):
    return LiquidationOrder(
        symbol="TESTUSDT", liquidated_side=side, price=10.0, qty=notional / 10.0,
        ts_ms=ts, notional=notional,
    )


class TestParseKline:
    def test_valid(self):
        ev = parse_kline_payload(kline_frame(closed=True))
        assert isinstance(ev, KlineEvent)
        assert ev.symbol == "TESTUSDT"
        assert ev.closed is True
        assert ev.kline.close == pytest.approx(101.0)
        assert ev.kline.trades == 42
        assert ev.kline.taker_buy_quote == pytest.approx(6000.0)

    def test_missing_k_raises(self):
        with pytest.raises(StreamError):
            parse_kline_payload({"e": "kline"})

    def test_bad_number_raises(self):
        with pytest.raises(StreamError):
            parse_kline_payload(kline_frame(over={"c": "oops"}))

    def test_bad_symbol_raises(self):
        with pytest.raises(StreamError):
            parse_kline_payload(kline_frame(over={"s": "bad symbol!"}))


class TestParseLiquidation:
    def test_sell_is_long_liquidation(self):
        order = parse_liquidation_payload(force_frame(side="SELL", price="100.0", qty="5.0"))
        assert order.liquidated_side == "LONG"
        assert order.notional == pytest.approx(500.0)

    def test_buy_is_short_liquidation(self):
        order = parse_liquidation_payload(force_frame(side="BUY"))
        assert order.liquidated_side == "SHORT"

    def test_bad_side_raises(self):
        with pytest.raises(StreamError):
            parse_liquidation_payload(force_frame(side="HODL"))


class TestParseMessage:
    def test_wrapped_kline(self):
        frame = {"stream": "testusdt@kline_1m", "data": kline_frame()}
        ev = parse_stream_message(frame)
        assert isinstance(ev, KlineEvent)

    def test_wrapped_force_order(self):
        frame = {"stream": "!forceOrder@arr", "data": force_frame()}
        ev = parse_stream_message(frame)
        assert isinstance(ev, LiquidationOrder)

    def test_subscribe_ack_is_none(self):
        assert parse_stream_message({"result": None, "id": 1}) is None

    def test_unknown_event_is_none(self):
        assert parse_stream_message({"stream": "x", "data": {"e": "aggTrade"}}) is None

    def test_missing_data_raises(self):
        with pytest.raises(StreamError):
            parse_stream_message({"stream": "x"})

    def test_non_dict_raises(self):
        with pytest.raises(StreamError):
            parse_stream_message("ping")


class TestCascadeDetector:
    def test_below_threshold_is_none(self):
        d = LiquidationCascadeDetector(window_ms=60_000, min_count=5)
        for i in range(4):
            assert d.add(liq(ts=TS + i)) is None

    def test_fifth_fires(self):
        d = LiquidationCascadeDetector(window_ms=60_000, min_count=5)
        out = None
        for i in range(5):
            out = d.add(liq(ts=TS + i, notional=10.0))
        assert out is not None
        assert out["count"] == 5
        assert out["side"] == "LONG"
        assert out["notional"] == pytest.approx(50.0)

    def test_sides_are_separate(self):
        d = LiquidationCascadeDetector(window_ms=60_000, min_count=3)
        d.add(liq(side="LONG", ts=TS))
        d.add(liq(side="LONG", ts=TS + 1))
        assert d.add(liq(side="SHORT", ts=TS + 2)) is None
        assert d.add(liq(side="LONG", ts=TS + 3)) is not None

    def test_old_events_evicted(self):
        d = LiquidationCascadeDetector(window_ms=1_000, min_count=3)
        for i in range(2):
            assert d.add(liq(ts=TS + i)) is None
        # first two are now outside the window
        assert d.add(liq(ts=TS + 5000)) is None

    def test_cooldown_suppresses_refire(self):
        d = LiquidationCascadeDetector(window_ms=60_000, min_count=3, cooldown_ms=30_000)
        assert d.add(liq(ts=TS)) is None
        assert d.add(liq(ts=TS + 1)) is None
        assert d.add(liq(ts=TS + 2)) is not None
        assert d.add(liq(ts=TS + 3)) is None  # within cooldown
        assert d.add(liq(ts=TS + 30_002)) is not None  # cooldown elapsed

    def test_bad_order_raises(self):
        d = LiquidationCascadeDetector()
        with pytest.raises(StreamError):
            d.add({"symbol": "X"})

    def test_bad_config_raises(self):
        with pytest.raises(StreamError):
            LiquidationCascadeDetector(window_ms=0)
        with pytest.raises(StreamError):
            LiquidationCascadeDetector(min_count=True)


class TestBackoff:
    def test_sequence_and_cap(self):
        assert backoff_delays(4) == [1.0, 2.0, 4.0, 8.0]
        assert backoff_delays(10)[-1] == 60.0

    def test_bad_args(self):
        with pytest.raises(StreamError):
            backoff_delays(0)
        with pytest.raises(StreamError):
            backoff_delays(3, base_s=-1)
