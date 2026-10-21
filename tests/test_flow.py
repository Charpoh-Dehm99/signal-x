"""Tests for signalx.flow: CVD, large prints, book pressure, bias composition."""

from __future__ import annotations

import pytest

from signalx.book import DepthSnapshot
from signalx.flow import (
    FlowError,
    cvd_quote_from_klines,
    flow_features,
    flow_from_depth,
    flow_from_klines,
    flow_from_trades,
)
from signalx.models import AggTrade, Kline

START = 1_600_000_000_000
STEP = 300_000


def kline(i, *, open_=100.0, close=100.0, qv=100.0, tbq=60.0):
    return Kline(
        open_time=START + i * STEP,
        open=open_,
        high=max(open_, close),
        low=min(open_, close),
        close=close,
        volume=1.0,
        close_time=START + i * STEP + STEP - 1,
        quote_volume=qv,
        trades=1,
        taker_buy_base=0.0,
        taker_buy_quote=tbq,
    )


def flat_klines(n=12, **kw):
    return [kline(i, **kw) for i in range(n)]


class TestKlineFlow:
    def test_flat_buying_pressure(self):
        out = flow_from_klines(flat_klines())
        assert out["cvd_net_quote"] == pytest.approx(240.0)
        assert out["cvd_ratio"] == pytest.approx(0.2)
        assert out["taker_buy_ratio"] == pytest.approx(0.6)
        assert out["large_bar_ratio"] == pytest.approx(1.0)
        assert out["cvd_price_divergence"] is None

    def test_bearish_cvd_divergence(self):
        ks = flat_klines(tbq=40.0)
        ks[-1] = kline(11, open_=100.0, close=102.0, qv=100.0, tbq=40.0)
        out = flow_from_klines(ks)
        assert out["price_change_pct"] == pytest.approx(0.02)
        assert out["cvd_ratio"] == pytest.approx(-0.2)
        assert out["cvd_price_divergence"] == "bearish"

    def test_bullish_cvd_divergence(self):
        ks = flat_klines(tbq=60.0)
        ks[-1] = kline(11, open_=100.0, close=98.0, qv=100.0, tbq=60.0)
        out = flow_from_klines(ks)
        assert out["price_change_pct"] == pytest.approx(-0.02)
        assert out["cvd_ratio"] == pytest.approx(0.2)
        assert out["cvd_price_divergence"] == "bullish"

    def test_cvd_series_is_cumulative(self):
        ks = [kline(0, qv=100.0, tbq=60.0), kline(1, qv=100.0, tbq=40.0)]
        assert cvd_quote_from_klines(ks) == [20.0, 0.0]

    def test_zero_volume_yields_none_ratios(self):
        out = flow_from_klines(flat_klines(qv=0.0, tbq=0.0))
        assert out["cvd_net_quote"] is None
        assert out["cvd_ratio"] is None
        assert out["taker_buy_ratio"] is None
        assert out["large_bar_ratio"] is None

    def test_short_series_and_bad_window_raise(self):
        with pytest.raises(FlowError):
            flow_from_klines(flat_klines(3), window=4)
        with pytest.raises(FlowError):
            flow_from_klines(flat_klines(), window=True)
        with pytest.raises(FlowError):
            cvd_quote_from_klines([])


class TestTradeFlow:
    def test_large_print_split_and_largest(self):
        trades = [
            AggTrade(1, 10.0, 3000.0, False),
            AggTrade(2, 10.0, 1000.0, True),
            AggTrade(3, 10.0, 2500.0, True),
        ]
        out = flow_from_trades(trades)
        assert out["trade_count"] == 3
        assert out["buy_notional"] == pytest.approx(30_000.0)
        assert out["sell_notional"] == pytest.approx(35_000.0)
        assert out["large_print_count"] == 2
        assert out["large_print_buy_usd"] == pytest.approx(30_000.0)
        assert out["large_print_sell_usd"] == pytest.approx(25_000.0)
        assert out["large_print_net_usd"] == pytest.approx(5_000.0)
        assert out["large_print_net_ratio"] == pytest.approx(5_000.0 / 55_000.0)
        assert out["large_print_net_share"] == pytest.approx(5_000.0 / 65_000.0)
        assert out["largest_print_usd"] == pytest.approx(30_000.0)
        assert out["largest_print_side"] == "buy"

    def test_empty_trades(self):
        out = flow_from_trades([])
        assert out["trade_count"] == 0
        assert out["large_print_count"] == 0
        assert out["large_print_net_ratio"] is None
        assert out["largest_print_side"] is None

    def test_bad_inputs_raise(self):
        with pytest.raises(FlowError):
            flow_from_trades(["not a trade"])
        with pytest.raises(FlowError):
            flow_from_trades([], min_notional=0)
        with pytest.raises(FlowError):
            flow_from_trades([AggTrade(1, 10.0, 0.0, False)])


class TestDepthFlow:
    def test_bid_heavy_book(self):
        snap = DepthSnapshot(
            "AAAUSDT",
            1,
            ((100.0, 2.0), (98.0, 1.0)),
            ((101.0, 1.0), (104.0, 1.0)),
        )
        out = flow_from_depth(snap)
        assert out["book_bid_usd"] == pytest.approx(200.0)
        assert out["book_ask_usd"] == pytest.approx(101.0)
        assert out["book_imbalance"] == pytest.approx((200.0 - 101.0) / 301.0)
        assert out["book_bias"] == "bid"
        assert out["book_spread_bps"] == pytest.approx(1.0 / 100.5 * 10_000.0)

    def test_ask_heavy_book(self):
        snap = DepthSnapshot(
            "AAAUSDT",
            1,
            ((100.0, 1.0),),
            ((101.0, 3.0),),
        )
        out = flow_from_depth(snap)
        assert out["book_bias"] == "ask"
        assert out["book_imbalance"] < 0

    def test_bad_inputs_raise(self):
        with pytest.raises(FlowError):
            flow_from_depth({"not": "a snapshot"})
        with pytest.raises(FlowError):
            flow_from_depth(
                DepthSnapshot("AAAUSDT", 1, ((100.0, 1.0),), ((101.0, 1.0),)),
                band_pct=0,
            )


class TestComposition:
    def test_full_flow_bias_bull(self):
        snap = DepthSnapshot(
            "AAAUSDT",
            1,
            ((100.0, 5.0),),
            ((101.0, 1.0),),
        )
        out = flow_features(
            flat_klines(),
            agg_trades=[AggTrade(1, 10.0, 3000.0, False)],
            depth=snap,
        )
        assert out["source"] == "klines+trades+book"
        assert out["fat_money_bias"] == "bull"
        assert out["absorption"] == "accumulation"

    def test_flow_bias_bear(self):
        ks = flat_klines(tbq=40.0)
        out = flow_features(
            ks,
            agg_trades=[AggTrade(1, 10.0, 3000.0, True)],
            depth=DepthSnapshot("AAAUSDT", 1, ((100.0, 1.0),), ((101.0, 5.0),)),
        )
        assert out["fat_money_bias"] == "bear"
        assert out["absorption"] == "distribution"

    def test_mixed_flow(self):
        ks = flat_klines(tbq=40.0)
        out = flow_features(
            ks,
            agg_trades=[AggTrade(1, 10.0, 3000.0, False)],
            depth=DepthSnapshot("AAAUSDT", 1, ((100.0, 1.0),), ((101.0, 5.0),)),
        )
        assert out["fat_money_bias"] == "mixed"

    def test_single_flow_vote_is_not_directional(self):
        out = flow_features(
            flat_klines(),
            depth=DepthSnapshot("AAAUSDT", 1, ((100.0, 1.0),), ((101.0, 1.0),)),
        )
        assert out["fat_money_bias"] == "mixed"

    def test_noisy_large_prints_do_not_dominate_bias(self):
        trades = [
            AggTrade(1, 100.0, 250.0, False),
            AggTrade(2, 100.0, 300.0, True),
        ]
        trades += [AggTrade(3 + i, 100.0, 1.0, i % 2 == 0) for i in range(1000)]
        out = flow_features(
            flat_klines(),
            agg_trades=trades,
            depth=DepthSnapshot("AAAUSDT", 1, ((100.0, 1.0),), ((101.0, 1.0),)),
        )
        assert out["large_print_net_ratio"] < -0.05
        assert abs(out["large_print_net_share"]) < 0.05
        assert out["fat_money_bias"] == "mixed"
