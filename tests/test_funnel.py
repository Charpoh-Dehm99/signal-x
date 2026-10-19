"""Tests for signalx.funnel: month math, screening wrapper, download plan."""

from __future__ import annotations

import datetime

import pandas as pd
import pytest

from signalx.funnel import (
    FunnelError,
    build_plan,
    build_tight_plan,
    context_months,
    month_of,
    next_month,
    plan_from_universe,
    prev_month,
    screen_symbol,
    tail_daily_days,
    window_months,
)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
# 2024-10-01 00:00 UTC
START_MS = int(datetime.datetime(2024, 10, 1, tzinfo=datetime.timezone.utc).timestamp() * 1000)
END_MS = int(datetime.datetime(2026, 9, 30, 23, tzinfo=datetime.timezone.utc).timestamp() * 1000)


# --------------------------------------------------------------- month math


class TestMonthMath:
    def test_month_of(self):
        assert month_of(START_MS) == "2024-10"
        assert month_of(START_MS + 45 * DAY_MS) == "2024-11"

    def test_month_of_rejects_garbage(self):
        for bad in (-1, "2024-10", 1.5, True, None):
            with pytest.raises(FunnelError):
                month_of(bad)

    def test_prev_next(self):
        assert prev_month("2024-10") == "2024-09"
        assert prev_month("2024-01") == "2023-12"
        assert next_month("2024-12") == "2025-01"
        assert next_month("2026-09") == "2026-10"

    def test_prev_next_reject(self):
        for bad in ("2024-13", "24-01", "2024-1", "", None):
            with pytest.raises(FunnelError):
                prev_month(bad)
            with pytest.raises(FunnelError):
                next_month(bad)

    def test_context_months_clip(self):
        assert context_months("2024-10", lo="2024-09", hi="2026-09") == ["2024-09", "2024-10", "2024-11"]
        assert context_months("2024-10", lo="2024-10", hi="2026-09") == ["2024-10", "2024-11"]
        assert context_months("2026-09", lo="2024-09", hi="2026-09") == ["2026-08", "2026-09"]

    def test_context_months_bad_bounds(self):
        with pytest.raises(FunnelError):
            context_months("2024-10", lo="2025-01", hi="2024-12")

    def test_tail_daily_days(self):
        days = tail_daily_days("2026-10", "2026-10-03")
        assert days == ["2026-10-01", "2026-10-02", "2026-10-03"]

    def test_tail_daily_days_validation(self):
        with pytest.raises(FunnelError):
            tail_daily_days("2026-10", "2026-11-01")
        with pytest.raises(FunnelError):
            tail_daily_days("2026-10", "2026-10-32")
        with pytest.raises(FunnelError):
            tail_daily_days("2026-13", "2026-13-01")


# ---------------------------------------------------------------- screening


def hourly_frame(n=400, spike_idx=300, volume_base=1000.0):
    """Flat ~100 price with mild volume alternation, then a pump at spike_idx.

    At spike_idx: price +25% vs 24 bars prior and a 6x volume spike; the spike
    decays over the following bars so the cooldown keeps exactly one event.
    """
    # 200h of lookback before the window; the spike (idx 300) lands ~100h in.
    ts = [START_MS - 200 * HOUR_MS + i * HOUR_MS for i in range(n)]
    close, volume = [], []
    price = 100.0
    for i in range(n):
        vol = volume_base * (1.1 if i % 2 else 0.9)
        if i == spike_idx:
            price = 125.0
            vol = volume_base * 6.0
        elif spike_idx < i <= spike_idx + 3:
            vol = volume_base * 4.0
        elif i > spike_idx + 3:
            vol = volume_base * (1.1 if i % 2 else 0.9)
        close.append(price)
        volume.append(vol)
    return pd.DataFrame(
        {
            "open_time": ts,
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": volume,
            "quote_volume": [c * v for c, v in zip(close, volume)],
        }
    )


class TestScreenSymbol:
    def test_detects_pump(self):
        df = hourly_frame()
        out = screen_symbol("TESTUSDT", df, start_ms=START_MS, end_ms=END_MS)
        assert len(out) == 1
        ev = out.iloc[0]
        assert ev["symbol"] == "TESTUSDT"
        assert ev["return_24h"] >= 0.20
        assert ev["vol_z"] >= 3.0
        assert ev["month"] == month_of(int(ev["open_time"]))

    def test_window_filter_drops_outside_events(self):
        df = hourly_frame()
        pump_ms = int(df.loc[df["close"].idxmax(), "open_time"])
        # window entirely after the pump
        out = screen_symbol("TESTUSDT", df, start_ms=pump_ms + HOUR_MS, end_ms=END_MS)
        assert len(out) == 0
        assert list(out.columns) == ["idx", "open_time", "close", "return_24h", "vol_z", "symbol", "month"]

    def test_no_pump(self):
        df = hourly_frame(spike_idx=10**9)  # never spikes
        out = screen_symbol("TESTUSDT", df, start_ms=START_MS, end_ms=END_MS)
        assert len(out) == 0

    def test_bad_args(self):
        with pytest.raises(FunnelError):
            screen_symbol("", hourly_frame(), start_ms=START_MS, end_ms=END_MS)
        with pytest.raises(FunnelError):
            screen_symbol("TESTUSDT", hourly_frame(), start_ms=END_MS, end_ms=START_MS)


# ------------------------------------------------------------------ plan


class TestBuildPlan:
    def test_mid_window_event(self):
        plan = build_plan({"SOLUSDT": {"2025-03"}}, lo="2024-09", hi="2026-09", tail_through=None)
        assert plan == [
            ("funding", "SOLUSDT", "2025-02"),
            ("funding", "SOLUSDT", "2025-03"),
            ("funding", "SOLUSDT", "2025-04"),
            ("klines", "SOLUSDT", "2025-02"),
            ("klines", "SOLUSDT", "2025-03"),
            ("klines", "SOLUSDT", "2025-04"),
        ]

    def test_left_edge_clips_context(self):
        plan = build_plan({"SOLUSDT": {"2024-10"}}, lo="2024-09", hi="2026-09", tail_through=None)
        months = {p for k, _s, p in plan if k == "klines"}
        assert months == {"2024-09", "2024-10", "2024-11"}

    def test_right_edge_gets_daily_tail(self):
        plan = build_plan({"WIFUSDT": {"2026-09"}}, lo="2024-09", hi="2026-09", tail_through="2026-10-03")
        daily = sorted(p for k, _s, p in plan if k == "klines_1m_daily")
        monthly = {p for k, _s, p in plan if k == "klines"}
        funding = {p for k, _s, p in plan if k == "funding"}
        assert daily == ["2026-10-01", "2026-10-02", "2026-10-03"]
        assert monthly == {"2026-08", "2026-09"}       # no 2026-10 monthly
        assert funding == {"2026-08", "2026-09"}       # funding stays monthly-only

    def test_multiple_symbols_exact(self):
        plan = build_plan(
            {"SOLUSDT": {"2025-03"}, "WIFUSDT": {"2025-03", "2025-04"}},
            lo="2024-09",
            hi="2026-09",
            tail_through=None,
        )
        # SOL: 3 months x 2 kinds = 6; WIF: {02,03,04,05} x 2 = 8 -> total 14
        assert len(plan) == 14

    def test_event_month_outside_bounds_raises(self):
        with pytest.raises(FunnelError):
            build_plan({"SOLUSDT": {"2027-01"}}, lo="2024-09", hi="2026-09", tail_through=None)

    def test_bad_tail_through_raises(self):
        with pytest.raises(FunnelError):
            build_plan({"SOLUSDT": {"2026-09"}}, lo="2024-09", hi="2026-09", tail_through="bogus")


# --------------------------------------------------------- window_months


def ms_at(y: int, mo: int, d: int, h: int = 0) -> int:
    return int(datetime.datetime(y, mo, d, h, tzinfo=datetime.timezone.utc).timestamp() * 1000)


class TestWindowMonths:
    def test_single_month(self):
        assert window_months(ms_at(2025, 3, 5), ms_at(2025, 3, 19), lo="2024-09", hi="2026-09") == ["2025-03"]

    def test_crosses_two_months(self):
        assert window_months(ms_at(2025, 3, 25), ms_at(2025, 4, 8), lo="2024-09", hi="2026-09") == ["2025-03", "2025-04"]

    def test_clip_to_bounds(self):
        assert window_months(ms_at(2024, 9, 25), ms_at(2024, 10, 3), lo="2024-10", hi="2026-09") == ["2024-10"]
        assert window_months(ms_at(2026, 9, 26), ms_at(2026, 10, 12), lo="2024-09", hi="2026-09") == ["2026-09"]

    def test_fully_outside_bounds_empty(self):
        assert window_months(ms_at(2024, 7, 1), ms_at(2024, 7, 5), lo="2024-09", hi="2026-09") == []

    def test_bad_args(self):
        for bad in (True, 1.5, "x", None, -1):
            with pytest.raises(FunnelError):
                window_months(bad, ms_at(2025, 3, 5), lo="2024-09", hi="2026-09")
        with pytest.raises(FunnelError):
            window_months(ms_at(2025, 3, 9), ms_at(2025, 3, 5), lo="2024-09", hi="2026-09")
        with pytest.raises(FunnelError):
            window_months(ms_at(2025, 3, 5), ms_at(2025, 3, 9), lo="2026-01", hi="2025-12")


# --------------------------------------------------------- build_tight_plan


class TestBuildTightPlan:
    def test_mid_month_event_one_month(self):
        plan = build_tight_plan({"SOLUSDT": [ms_at(2025, 3, 15)]}, lo="2024-09", hi="2026-09")
        assert plan == [("klines", "SOLUSDT", "2025-03")]

    def test_early_month_event_touches_prev_month(self):
        plan = build_tight_plan({"SOLUSDT": [ms_at(2025, 3, 1)]}, lo="2024-09", hi="2026-09")
        assert plan == [("klines", "SOLUSDT", "2025-02"), ("klines", "SOLUSDT", "2025-03")]

    def test_late_month_event_touches_next_month(self):
        plan = build_tight_plan({"SOLUSDT": [ms_at(2025, 3, 25)]}, lo="2024-09", hi="2026-09")
        assert plan == [("klines", "SOLUSDT", "2025-03"), ("klines", "SOLUSDT", "2025-04")]

    def test_left_edge_clip(self):
        plan = build_tight_plan({"SOLUSDT": [ms_at(2024, 10, 1)]}, lo="2024-09", hi="2026-09")
        months = [p for k, _s, p in plan if k == "klines"]
        assert months == ["2024-09", "2024-10"]

    def test_right_edge_daily_tail_clamped(self):
        plan = build_tight_plan(
            {"WIFUSDT": [ms_at(2026, 9, 28)]},
            lo="2024-09", hi="2026-09", tail_through="2026-10-03",
        )
        daily = sorted(p for k, _s, p in plan if k == "klines_1m_daily")
        monthly = [p for k, _s, p in plan if k == "klines"]
        # window = Sep 26 .. Oct 12; monthly clipped at hi, daily through tail_through
        assert monthly == ["2026-09"]
        assert daily == ["2026-10-01", "2026-10-02", "2026-10-03"]

    def test_right_edge_daily_tail_limited_by_window(self):
        plan = build_tight_plan(
            {"WIFUSDT": [ms_at(2026, 9, 20, 12)]},
            lo="2024-09", hi="2026-09", tail_through="2026-10-16",
        )
        daily = sorted(p for k, _s, p in plan if k == "klines_1m_daily")
        # window ends Oct 4 12:00 -> days 1..4
        assert daily == ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"]

    def test_no_tail_through_no_daily(self):
        plan = build_tight_plan({"WIFUSDT": [ms_at(2026, 9, 28)]}, lo="2024-09", hi="2026-09")
        assert all(k == "klines" for k, _s, _p in plan)

    def test_dedupes_across_events(self):
        plan = build_tight_plan(
            {"SOLUSDT": [ms_at(2025, 3, 10), ms_at(2025, 3, 14)]}, lo="2024-09", hi="2026-09"
        )
        assert plan == [("klines", "SOLUSDT", "2025-03")]

    def test_bad_inputs(self):
        with pytest.raises(FunnelError):
            build_tight_plan({"X": [ms_at(2025, 3, 5)]}, lo="2024-09", hi="2026-09", lead_hours=0)
        with pytest.raises(FunnelError):
            build_tight_plan({"X": [ms_at(2025, 3, 5)]}, lo="2024-09", hi="2026-09", trail_days=True)
        with pytest.raises(FunnelError):
            build_tight_plan({"X": [ms_at(2025, 3, 5)]}, lo="2024-09", hi="2026-09", tail_through="bogus")
        with pytest.raises(FunnelError):
            build_tight_plan({"X": "not-a-list"}, lo="2024-09", hi="2026-09")
        with pytest.raises(FunnelError):
            build_tight_plan(
                {"X": [ms_at(2026, 9, 28)]}, lo="2024-09", hi="2026-09", tail_through="2026-09-30"
            )


# ------------------------------------------------------- plan_from_universe


class TestPlanFromUniverse:
    DOC = {
        "symbols": {
            "SOLUSDT": {"months_1h": ["2024-09", "2024-10"], "months_1m": ["2024-10"], "months_funding": ["2024-10"]},
            "AERGOUSDT": {"months_1h": ["2025-01"], "months_1m": [], "months_funding": ["2025-01"]},
        }
    }

    def test_1h(self):
        plan = plan_from_universe(self.DOC, "1h")
        assert plan == [
            ("klines_1h", "AERGOUSDT", "2025-01"),
            ("klines_1h", "SOLUSDT", "2024-09"),
            ("klines_1h", "SOLUSDT", "2024-10"),
        ]

    def test_funding(self):
        plan = plan_from_universe(self.DOC, "funding")
        assert plan == [("funding", "AERGOUSDT", "2025-01"), ("funding", "SOLUSDT", "2024-10")]

    def test_bad_series(self):
        with pytest.raises(FunnelError):
            plan_from_universe(self.DOC, "1d")

    def test_missing_symbols(self):
        with pytest.raises(FunnelError):
            plan_from_universe({}, "1h")

    def test_missing_months_key(self):
        with pytest.raises(FunnelError):
            plan_from_universe({"symbols": {"X": {}}}, "1h")

    def test_bad_month(self):
        with pytest.raises(FunnelError):
            plan_from_universe({"symbols": {"X": {"months_1h": ["2024-13"]}}}, "1h")
