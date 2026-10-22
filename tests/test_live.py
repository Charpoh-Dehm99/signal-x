"""Tests for signalx.live: volume z-score, snapshot composition, state/events."""

from __future__ import annotations

import pytest

from signalx.book import DepthSnapshot
from signalx.live import LiveError, live_snapshot, volume_z
from signalx.models import AggTrade, Pivot
from tests.helpers import MIN_MS, START_MS, mk_kline, series_from_closes

MS5 = 300_000


def bars(closes, *, highs=None, lows=None):
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs is not None else c * 1.001
        l = lows[i] if lows is not None else c * 0.999
        out.append(mk_kline(START_MS + i * MS5, c, h, l, c, v=1000.0))
    return out


def flat(n, price=100.0, half=0.1):
    return bars([price] * n, highs=[price + half] * n, lows=[price - half] * n)


def resistance_series(last_close=100.0, n=90):
    closes = [100.0] * n
    closes[-1] = last_close
    highs = [101.0] * n
    lows = [99.0] * n
    highs[20] = 110.0
    highs[50] = 110.0
    lows[30] = 95.0
    return bars(closes, highs=highs, lows=lows)


# ---------------------------------------------------------------- volume_z


class TestVolumeZ:
    def test_empty_raises(self):
        with pytest.raises(LiveError):
            volume_z([])

    def test_not_enough_history_is_none(self):
        assert volume_z([100.0] * 10) is None

    def test_flat_history_zero_variance_is_none(self):
        assert volume_z([100.0] * 130) is None

    def test_spike_scores_high(self):
        hist = [100.0 + (i % 5) for i in range(120)]
        vals = hist + [200.0]
        mean = sum(hist) / len(hist)
        std = (sum((v - mean) ** 2 for v in hist) / len(hist)) ** 0.5
        z = volume_z(vals)
        assert z == pytest.approx((200.0 - mean) / std)
        assert z > 3.0


# ---------------------------------------------------------------- snapshot


class TestSnapshot:
    def test_requires_enough_bars(self):
        with pytest.raises(LiveError):
            live_snapshot(symbol="TESTUSDT", klines5m=flat(49))

    def test_pump_state_with_volume_spike(self):
        closes = [100.0 + i * 0.5 for i in range(90)]
        qv = [100.0 + (i % 7) for i in range(129)] + [500.0]
        snap = live_snapshot(
            symbol="TESTUSDT", klines5m=bars(closes), hourly_qv=qv, change_24h_pct=22.0,
        )
        assert snap["is_pump"] is True
        assert snap["pump_failed"] is False
        assert snap["vol_z"] > 3.0
        assert snap["state"] == "PUMP"

    def test_bullish_divergence_event_and_flag(self, monkeypatch):
        import signalx.live as live

        fresh = (
            Pivot(80, 99.0, "L", 82, 0.0),
            Pivot(86, 98.0, "L", 88, 0.0),
            Pivot(81, 30.0, "L", 83, 0.0),
            Pivot(85, 35.0, "L", 87, 0.0),
        )
        monkeypatch.setattr(live, "find_bearish_divergences", lambda *a, **k: [])
        monkeypatch.setattr(live, "find_bullish_divergences", lambda *a, **k: [fresh])
        snap = live_snapshot(symbol="TESTUSDT", klines5m=flat(90))
        assert snap["has_bullish_div"] is True
        assert snap["bullish_div_age"] == 1
        assert "BULLISH_DIVERGENCE" in snap["events"]
        assert "BEARISH_DIVERGENCE" not in snap["events"]

    def test_stale_divergence_is_not_an_event(self, monkeypatch):
        import signalx.live as live

        stale = (
            Pivot(20, 99.0, "L", 22, 0.0),
            Pivot(26, 98.0, "L", 28, 0.0),
            Pivot(21, 30.0, "L", 23, 0.0),
            Pivot(25, 35.0, "L", 27, 0.0),
        )
        monkeypatch.setattr(live, "find_bearish_divergences", lambda *a, **k: [])
        monkeypatch.setattr(live, "find_bullish_divergences", lambda *a, **k: [stale])
        snap = live_snapshot(symbol="TESTUSDT", klines5m=flat(90))
        assert snap["has_bullish_div"] is False
        assert snap["bullish_div_age"] == 61
        assert "BULLISH_DIVERGENCE" not in snap["events"]

    def test_contradictory_divergences_keep_fresher_side(self, monkeypatch):
        import signalx.live as live

        bear = (
            Pivot(70, 101.0, "H", 72, 0.0),
            Pivot(86, 102.0, "H", 88, 0.0),
            Pivot(71, 70.0, "H", 73, 0.0),
            Pivot(85, 65.0, "H", 87, 0.0),
        )
        bull = (
            Pivot(60, 99.0, "L", 62, 0.0),
            Pivot(80, 98.0, "L", 82, 0.0),
            Pivot(61, 30.0, "L", 63, 0.0),
            Pivot(79, 35.0, "L", 81, 0.0),
        )
        monkeypatch.setattr(live, "find_bearish_divergences", lambda *a, **k: [bear])
        monkeypatch.setattr(live, "find_bullish_divergences", lambda *a, **k: [bull])
        snap = live_snapshot(symbol="TESTUSDT", klines5m=flat(90))
        assert snap["has_bearish_div"] is True
        assert snap["has_bullish_div"] is False
        assert "BEARISH_DIVERGENCE" in snap["events"]
        assert "BULLISH_DIVERGENCE" not in snap["events"]

    def test_no_divergence_on_flat_series(self):
        snap = live_snapshot(symbol="TESTUSDT", klines5m=flat(90))
        assert snap["has_bearish_div"] is False
        assert snap["has_bullish_div"] is False
        assert "BULLISH_DIVERGENCE" not in snap["events"]

    def test_flat_series_plateau(self):
        snap = live_snapshot(symbol="TESTUSDT", klines5m=flat(90))
        assert snap["compression"]["qualifies"] is True
        assert snap["state"] == "PLATEAU"
        assert "PLATEAU_ACTIVE" in snap["events"]
        assert snap["resistance"] is None

    def test_resistance_zone_and_distance(self):
        snap = live_snapshot(symbol="TESTUSDT", klines5m=resistance_series())
        r = snap["resistance"]
        assert r is not None
        assert r["low"] == pytest.approx(110.0)
        assert r["touches"] == 2
        # baseline ATR ~2.0; Wilder smoothing keeps a little decay from the spikes
        assert snap["atr"] == pytest.approx(2.0, rel=0.05)
        assert r["dist_atr"] == pytest.approx(10.0 / snap["atr"])
        assert r["dist_pct"] == pytest.approx(0.10)
        assert "NEAR_RESISTANCE" not in snap["events"]
        assert snap["bos_down"] is False
        assert snap["bos_up"] is False
        assert snap["swing_low"] == pytest.approx(95.0)

    def test_bos_down_on_close_below_swing_low(self):
        snap = live_snapshot(symbol="TESTUSDT", klines5m=resistance_series(last_close=94.0))
        assert snap["swing_low"] == pytest.approx(95.0)
        assert snap["bos_down"] is True
        assert snap["bos_up"] is False
        assert snap["pump_failed"] is True
        assert "BOS_DOWN" in snap["events"]
        assert "PUMP_FAILED" in snap["events"]

    def test_bos_up_on_close_above_swing_high(self):
        n = 90
        closes = [100.0] * n
        highs = [101.0] * n
        lows = [99.0] * n
        highs[50] = 110.0
        closes[-1] = 111.0
        highs[-1] = 111.5
        snap = live_snapshot(symbol="TESTUSDT", klines5m=bars(closes, highs=highs, lows=lows))
        assert snap["swing_high"] == pytest.approx(110.0)
        assert snap["bos_up"] is True
        assert snap["bos_down"] is False
        assert "BOS_UP" in snap["events"]

    def test_bos_down_event_is_not_sticky(self):
        klines = resistance_series(last_close=94.0)
        prev = klines[-2]
        klines[-2] = mk_kline(prev.open_time, 100.0, 100.2, 93.8, 94.0, v=prev.volume)
        snap = live_snapshot(symbol="TESTUSDT", klines5m=klines)
        assert snap["swing_low"] == pytest.approx(95.0)
        assert snap["bos_down"] is False
        assert "BOS_DOWN" not in snap["events"]
        # The structural state persists until price recovers, even though the
        # one-bar BOS event has passed.
        assert snap["pump_failed"] is True
        assert "PUMP_FAILED" in snap["events"]

    def test_bos_up_event_is_not_sticky(self):
        n = 90
        closes = [100.0] * n
        highs = [101.0] * n
        lows = [99.0] * n
        highs[50] = 110.0
        closes[-2] = closes[-1] = 111.0
        highs[-2] = highs[-1] = 111.5
        snap = live_snapshot(symbol="TESTUSDT", klines5m=bars(closes, highs=highs, lows=lows))
        assert snap["swing_high"] == pytest.approx(110.0)
        assert snap["bos_up"] is False
        assert "BOS_UP" not in snap["events"]

    def test_squeeze_unknown_without_futures_inputs(self):
        snap = live_snapshot(symbol="TESTUSDT", klines5m=flat(90))
        assert snap["squeeze_active"] is None
        assert snap["funding_pctile"] is None
        assert snap["oi_regime"] is None
        assert "SQUEEZE_GUARD" not in snap["events"]

    def test_squeeze_blocks_when_hot(self):
        n = 90
        k = bars([100.0] * n, highs=[100.0] * n, lows=[99.8] * n)  # close == high
        funding_hist = [0.0001] * 30 + [0.0020]
        oi_hist = [100.0 * (1.01 ** i) for i in range(12)]
        snap = live_snapshot(
            symbol="TESTUSDT",
            klines5m=k,
            funding_now=0.003,
            funding_history=funding_hist,
            oi_now=oi_hist[-1] * 1.02,
            oi_history=oi_hist,
        )
        assert snap["funding_pctile"] > 90.0
        assert snap["oi_regime"] == "ABSORPTION"
        assert snap["squeeze_active"] is True
        assert snap["state"] == "SQUEEZE_RISK"
        assert "SQUEEZE_GUARD" in snap["events"]

    def test_oi_delta_aligns_to_first_price_bar_by_timestamp(self, monkeypatch):
        import signalx.live as live

        captured = {}

        def fake_regime(delta_oi, delta_price, at_highs):
            captured.update(delta_oi=delta_oi, delta_price=delta_price, at_highs=at_highs)
            return "TREND_FUEL"

        monkeypatch.setattr(live, "oi_price_regime", fake_regime)
        k = series_from_closes([100.0 + i for i in range(300)], spread=0.0)
        oi_times = [START_MS + MIN_MS + i * 5 * MIN_MS for i in range(60)]
        oi_hist = [1000.0 + i for i in range(60)]
        live_snapshot(
            symbol="TESTUSDT",
            klines5m=k,
            funding_now=0.0001,
            funding_history=[0.0001] * 30,
            oi_now=2000.0,
            oi_history=oi_hist,
            oi_times=oi_times,
        )
        # Reference bar is bar 1 (101.0), not 300 - 60 = bar 240 (340.0).
        assert captured["delta_price"] == pytest.approx(399.0 / 101.0 - 1.0)
        assert captured["delta_oi"] == pytest.approx(2000.0 / 1000.0 - 1.0)
        assert captured["at_highs"] is True

    def test_momentum_decay_event(self):
        steps = [max(2.0 - i * 0.02, 0.05) for i in range(89)]
        closes = [100.0]
        for s in steps:
            closes.append(closes[-1] + s)
        snap = live_snapshot(symbol="TESTUSDT", klines5m=bars(closes))
        assert snap["momentum_decay"] is True
        assert snap["momentum_decay_bias"] == "bear"
        assert "MOMENTUM_DECAY" in snap["events"]

    def test_momentum_decay_supports_long_when_selloff_fades(self):
        steps = [-10.0 * (0.9 ** i) for i in range(89)]
        closes = [1000.0]
        for s in steps:
            closes.append(closes[-1] + s)
        snap = live_snapshot(symbol="TESTUSDT", klines5m=bars(closes))
        assert snap["momentum_decay"] is True
        assert snap["momentum_decay_bias"] == "bull"
        assert "MOMENTUM_DECAY" in snap["events"]

    def test_flow_from_klines_is_always_present(self):
        snap = live_snapshot(symbol="TESTUSDT", klines5m=flat(90))
        assert snap["flow"]["source"] == "klines"
        assert snap["flow"]["cvd_ratio"] == pytest.approx(0.0)
        assert snap["flow"]["large_print_count"] is None

    def test_flow_microstructure_enrichment(self):
        depth = DepthSnapshot(
            "TESTUSDT", 1, ((100.0, 5.0),), ((101.0, 1.0),)
        )
        snap = live_snapshot(
            symbol="TESTUSDT",
            klines5m=flat(90),
            agg_trades=[AggTrade(1, 100.0, 300.0, False)],
            depth_snapshot=depth,
        )
        assert snap["flow"]["source"] == "klines+trades+book"
        assert snap["flow"]["large_print_count"] == 1
        assert snap["flow"]["book_imbalance"] is not None


# --------------------------------------------------- swing_high / vol_ratio


class TestSnapshotStructureExtras:
    def test_swing_high_exposed(self):
        snap = live_snapshot(symbol="TESTUSDT", klines5m=resistance_series())
        assert snap["swing_high"] == pytest.approx(110.0)

    def test_swing_high_none_without_pivots(self):
        snap = live_snapshot(symbol="TESTUSDT", klines5m=flat(90))
        assert snap["swing_high"] is None

    def test_vol_ratio_spike(self):
        k = flat(90)
        k[-1] = mk_kline(START_MS + 89 * MS5, 100.0, 100.1, 99.9, 100.0, v=3000.0)
        snap = live_snapshot(symbol="TESTUSDT", klines5m=k)
        assert snap["vol_ratio"] == pytest.approx(3.0)

    def test_vol_ratio_zero_base_is_none(self):
        k = bars([100.0] * 90)
        k = [mk_kline(b.open_time, b.open, b.high, b.low, b.close, v=0.0) for b in k]
        snap = live_snapshot(symbol="TESTUSDT", klines5m=k)
        assert snap["vol_ratio"] is None
