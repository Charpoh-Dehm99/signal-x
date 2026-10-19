"""Tests for signalx.eventstudy: resampling, pump detection, outcome sim, trigger chain."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from signalx.eventstudy import (
    EventStudyError,
    find_trigger,
    forward_returns,
    funding_cost,
    pump_events,
    resample_ohlcv,
    simulate_short,
    to_klines,
    wilson_ci,
)

BASE = 1_699_999_200_000  # 2023-11-14 22:00:00 UTC - aligned to hour AND 5min buckets


def mk1m(n: int, start_ms: int = BASE) -> pd.DataFrame:
    i = np.arange(n)
    close = 100.0 + i * 0.1
    return pd.DataFrame(
        {
            "open_time": start_ms + i * 60_000,
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": np.full(n, 10.0),
            "quote_volume": close * 10.0,
        }
    )


def mk_hourly(n: int) -> pd.DataFrame:
    i = np.arange(n)
    qv = 100.0 + (i % 7)
    return pd.DataFrame(
        {
            "open_time": BASE + i * 3_600_000,
            "open": np.full(n, 100.0),
            "high": np.full(n, 101.0),
            "low": np.full(n, 99.0),
            "close": np.full(n, 100.0),
            "volume": qv / 100.0,
            "quote_volume": qv,
        }
    )


def mk_bars(opens, highs, lows, closes) -> pd.DataFrame:
    n = len(opens)
    return pd.DataFrame(
        {
            "open_time": BASE + np.arange(n) * 300_000,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.ones(n),
            "quote_volume": np.full(n, 100.0),
        }
    )


# ---------------------------------------------------------------- resample


class TestResample:
    def test_hourly_aggregation(self):
        df = mk1m(120)
        h = resample_ohlcv(df, "1h")
        assert len(h) == 2
        assert h["open"].iloc[0] == pytest.approx(df["open"].iloc[0])
        assert h["close"].iloc[0] == pytest.approx(df["close"].iloc[59])
        assert h["high"].iloc[0] == pytest.approx(df["high"].iloc[:60].max())
        assert h["low"].iloc[1] == pytest.approx(df["low"].iloc[60:].min())
        assert h["volume"].iloc[1] == pytest.approx(df["volume"].iloc[60:].sum())
        assert h["open_time"].iloc[0] == BASE

    def test_5m_aggregation(self):
        df = mk1m(30)
        b = resample_ohlcv(df, "5min")
        assert len(b) == 6
        assert (b["open_time"].diff().dropna() == 300_000).all()

    def test_errors(self):
        with pytest.raises(EventStudyError, match="empty"):
            resample_ohlcv(mk1m(0), "1h")
        bad = mk1m(10).drop(columns=["quote_volume"])
        with pytest.raises(EventStudyError, match="missing"):
            resample_ohlcv(bad, "1h")
        unsorted = mk1m(10).iloc[::-1]
        with pytest.raises(EventStudyError, match="sorted"):
            resample_ohlcv(unsorted, "1h")


class TestToKlines:
    def test_step_inference(self):
        bars = resample_ohlcv(mk1m(30), "5min")
        kl = to_klines(bars)
        assert len(kl) == 6
        assert kl[1].close_time - kl[1].open_time == 300_000 - 1

    def test_empty_raises(self):
        with pytest.raises(EventStudyError, match="empty"):
            to_klines(mk1m(0))


# ------------------------------------------------------------ pump events


class TestPumpEvents:
    def test_detects_and_cooldowns(self):
        h = mk_hourly(220)
        h.loc[150, "close"] = 130.0  # +30% vs idx 126 (100)
        h.loc[150, "quote_volume"] = 5000.0
        h.loc[152, "close"] = 131.0
        h.loc[152, "quote_volume"] = 6000.0  # same event, within cooldown
        events = pump_events(h)
        assert len(events) == 1
        assert events["idx"].iloc[0] == 150
        assert events["return_24h"].iloc[0] == pytest.approx(0.30)

    def test_new_event_after_cooldown(self):
        h = mk_hourly(260)
        h.loc[150, "close"] = 130.0
        h.loc[150, "quote_volume"] = 5000.0
        h.loc[200, "close"] = 121.0  # +21% vs idx 176
        h.loc[200, "quote_volume"] = 8000.0
        events = pump_events(h)
        assert list(events["idx"]) == [150, 200]

    def test_no_events_and_schema(self):
        events = pump_events(mk_hourly(200), return_thr=0.5)
        assert len(events) == 0
        assert list(events.columns) == ["idx", "open_time", "close", "return_24h", "vol_z"]

    def test_missing_column_raises(self):
        with pytest.raises(EventStudyError, match="missing"):
            pump_events(mk_hourly(10).drop(columns=["quote_volume"]))


class TestForwardReturns:
    def test_values_and_beyond_end(self):
        h = mk_hourly(50)
        h["close"] = 100.0 + np.arange(50)
        r = forward_returns(h, 10, horizons=(1, 5))
        assert r["+1h"] == pytest.approx(111 / 110 - 1)
        assert r["+5h"] == pytest.approx(115 / 110 - 1)
        r2 = forward_returns(h, 48, horizons=(1, 4))
        assert r2["+1h"] == pytest.approx(149 / 148 - 1)
        assert r2["+4h"] is None

    def test_bad_idx(self):
        with pytest.raises(EventStudyError, match="range"):
            forward_returns(mk_hourly(10), 10)


# ---------------------------------------------------------- simulate short


class TestSimulateShort:
    def test_target_hit(self):
        bars = mk_bars([100, 100, 100, 100], [100, 101, 101, 101], [100, 94, 93, 93], [100, 100, 100, 100])
        r = simulate_short(bars, 0, stop=110.0, target=95.0, max_bars=10, fee_bps=0.0, slip_bps=0.0)
        assert r["outcome"] == "target"
        assert r["gross_return"] == pytest.approx(0.05)
        assert r["bars_held"] == 2
        assert r["mfe"] == pytest.approx(0.06)
        assert r["mae"] == pytest.approx(0.01)

    def test_stop_hit(self):
        bars = mk_bars([100, 100, 100, 100], [100, 101, 111, 100], [100, 99, 99, 98], [100, 100, 100, 100])
        r = simulate_short(bars, 0, stop=110.0, target=90.0, max_bars=10, fee_bps=0.0, slip_bps=0.0)
        assert r["outcome"] == "stop"
        assert r["gross_return"] == pytest.approx(-0.10)
        assert r["bars_held"] == 3
        assert r["mae"] == pytest.approx(0.11)

    def test_time_exit(self):
        bars = mk_bars([100, 100, 100, 100], [100.5, 100.5, 100.5, 100.5], [99.5, 99.5, 99.5, 99.5], [100, 100.5, 100.25, 100])
        r = simulate_short(bars, 0, stop=110.0, target=90.0, max_bars=2, fee_bps=0.0, slip_bps=0.0)
        assert r["outcome"] == "time"
        assert r["gross_return"] == pytest.approx(-0.005)
        assert r["bars_held"] == 2

    def test_same_bar_stop_first_pessimistic(self):
        bars = mk_bars([100, 100], [100, 111], [100, 94], [100, 100])
        r_stop = simulate_short(bars, 0, stop=110.0, target=95.0, max_bars=5, fee_bps=0.0, slip_bps=0.0, stop_first=True)
        r_tgt = simulate_short(bars, 0, stop=110.0, target=95.0, max_bars=5, fee_bps=0.0, slip_bps=0.0, stop_first=False)
        assert r_stop["outcome"] == "stop"
        assert r_tgt["outcome"] == "target"

    def test_costs_math_exact(self):
        bars = mk_bars([100], [111], [94], [100])
        r = simulate_short(bars, 0, stop=110.0, target=95.0, max_bars=1, fee_bps=5.0, slip_bps=10.0)
        assert r["entry_fill"] == pytest.approx(99.9)
        expected_gross = (99.9 - 110.0 * 1.001) / 99.9
        expected_net = expected_gross - 2 * 5 / 10_000
        assert r["outcome"] == "stop"
        assert r["gross_return"] == pytest.approx(expected_gross)
        assert r["net_return"] == pytest.approx(expected_net)

    def test_invalid_levels_raise(self):
        bars = mk_bars([100, 100], [101, 101], [99, 99], [100, 100])
        with pytest.raises(EventStudyError, match="stop"):
            simulate_short(bars, 0, stop=99.0, target=90.0, max_bars=5)
        with pytest.raises(EventStudyError, match="target"):
            simulate_short(bars, 0, stop=110.0, target=101.0, max_bars=5)
        with pytest.raises(EventStudyError, match="range"):
            simulate_short(bars, 5, stop=110.0, target=90.0, max_bars=5)


class TestFunding:
    def test_window_boundaries(self):
        f = pd.DataFrame({"funding_time": [50, 150, 250], "rate": [0.1, 0.2, 0.3]})
        assert funding_cost(100, 250, f) == pytest.approx(0.5)
        assert funding_cost(150, 250, f) == pytest.approx(0.3)
        assert funding_cost(100, 149, f) == pytest.approx(0.0)
        assert funding_cost(100, 250, None) == pytest.approx(0.0)
        assert funding_cost(100, 250, pd.DataFrame({"funding_time": [], "rate": []})) == 0.0

    def test_missing_col_raises(self):
        with pytest.raises(EventStudyError, match="missing"):
            funding_cost(0, 1, pd.DataFrame({"funding_time": [1]}))


class TestWilson:
    def test_sanity(self):
        assert wilson_ci(0, 0) == (0.0, 1.0)
        lo, hi = wilson_ci(0, 100)
        assert lo == 0.0 and 0.0 < hi < 0.05
        lo, hi = wilson_ci(100, 100)
        assert hi == pytest.approx(1.0)
        assert 0.95 < lo < 1.0
        lo, hi = wilson_ci(50, 100)
        assert 0.40 < lo < 0.50 < hi < 0.60

    def test_invalid_k(self):
        with pytest.raises(EventStudyError):
            wilson_ci(5, 3)


# ------------------------------------------------------------- find_trigger


def make_signal_bars(breakdown: bool = True, breakdown_volume: float = 10.0) -> pd.DataFrame:
    n = 130
    close = np.zeros(n)
    for i in range(30):
        close[i] = 100 + i * (50 / 29)  # rally 100 -> 150
    pattern = [150.6, 151.4, 150.8, 151.2, 150.7, 151.3]
    for i in range(30, 110):
        close[i] = pattern[(i - 30) % 6]
    close[85] = 150.5  # dip; low overridden below -> pivot L after confirmation
    if breakdown:
        close[110] = 148.0
    else:
        close[110] = pattern[(110 - 30) % 6]
    for i in range(111, n):
        close[i] = 148.0 + (i - 111) * 0.05

    open_ = np.roll(close, 1)
    open_[0] = 100.0
    high = close + 0.3
    low = close - 0.3
    low[85] = 150.15
    vol = np.ones(n)
    vol[110] = breakdown_volume
    return mk_bars(open_, high, low, [close[i] for i in range(n)]).assign(volume=vol)


class TestFindTrigger:
    def test_trigger_found_with_levels(self):
        bars = make_signal_bars()
        r = find_trigger(bars, 20, plateau_window=30, min_dwell=15, max_wait_bars=200)
        assert r is not None
        assert r["signal_idx"] == 110
        assert r["entry_idx"] == 111
        assert r["entry"] == pytest.approx(148.0)
        assert r["stop"] > r["entry"]
        assert r["target"] < r["entry"]
        assert r["vol_ratio"] >= 1.5
        assert r["stop_dist_atr"] > 0

    def test_no_breakdown_no_trigger(self):
        bars = make_signal_bars(breakdown=False)
        assert find_trigger(bars, 20, plateau_window=30, min_dwell=15, max_wait_bars=200) is None

    def test_low_volume_break_rejected(self):
        bars = make_signal_bars(breakdown=True, breakdown_volume=1.0)
        assert find_trigger(bars, 20, plateau_window=30, min_dwell=15, max_wait_bars=200) is None

    def test_bad_event_idx(self):
        bars = make_signal_bars()
        with pytest.raises(EventStudyError, match="range"):
            find_trigger(bars, 999)
