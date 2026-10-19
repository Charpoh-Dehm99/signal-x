"""Tests for signalx.recorder: gap math, backfill planning, atomic store."""

from __future__ import annotations

import pytest

from signalx.models import Kline
from signalx.recorder import (
    KLINE_FIELDS,
    KlineStore,
    RecorderError,
    gap_bars,
    is_stale,
    kline_to_row,
    merge_kline_rows,
    plan_backfill,
)

STEP = 60_000
NOW = 1_700_000_100_000


def krow(open_time, close=100.0):
    return {
        "open_time": open_time,
        "open": close,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "volume": 10.0,
        "close_time": open_time + STEP - 1,
        "quote_volume": close * 10.0,
        "trades": 5,
        "taker_buy_base": 5.0,
        "taker_buy_quote": close * 5.0,
    }


class TestGapBars:
    def test_contiguous_is_zero(self):
        assert gap_bars(NOW, NOW + STEP, STEP) == 0

    def test_four_missing(self):
        assert gap_bars(NOW, NOW + 5 * STEP, STEP) == 4

    def test_overlap_clamps_to_zero(self):
        assert gap_bars(NOW, NOW, STEP) == 0

    def test_bad_input_raises(self):
        with pytest.raises(RecorderError):
            gap_bars(0, NOW, STEP)
        with pytest.raises(RecorderError):
            gap_bars(NOW, True, STEP)
        with pytest.raises(RecorderError):
            gap_bars(NOW, NOW, -1)


class TestPlanBackfill:
    def test_initial_fetch_capped(self):
        plan = plan_backfill(None, NOW, STEP, max_bars=100)
        assert plan == {"start_ms": NOW - 100 * STEP, "end_ms": NOW, "limit": 100}

    def test_small_gap(self):
        last = NOW - 5 * STEP
        plan = plan_backfill(last, NOW, STEP, max_bars=100)
        assert plan["start_ms"] == last + STEP
        assert plan["limit"] == 4

    def test_nothing_to_fetch(self):
        assert plan_backfill(NOW - STEP, NOW, STEP, max_bars=100) is None
        assert plan_backfill(NOW, NOW, STEP, max_bars=100) is None

    def test_long_gap_keeps_recent(self):
        plan = plan_backfill(NOW - 10_000 * STEP, NOW, STEP, max_bars=500)
        assert plan["start_ms"] == NOW - 500 * STEP
        assert plan["limit"] == 500

    def test_bad_args(self):
        with pytest.raises(RecorderError):
            plan_backfill(None, NOW, STEP, max_bars=0)
        with pytest.raises(RecorderError):
            plan_backfill(True, NOW, STEP, max_bars=10)


class TestMerge:
    def test_dedup_new_wins_and_sorted(self):
        a = krow(NOW, 100.0)
        b = krow(NOW + STEP, 101.0)
        b_new = krow(NOW + STEP, 999.0)
        merged = merge_kline_rows([a, b], [b_new])
        assert [r["open_time"] for r in merged] == [NOW, NOW + STEP]
        assert merged[1]["close"] == 999.0

    def test_empty_inputs(self):
        assert merge_kline_rows([], []) == []

    def test_bad_row_raises(self):
        with pytest.raises(RecorderError):
            merge_kline_rows([], [{"no_time": 1}])
        with pytest.raises(RecorderError):
            merge_kline_rows([], [krow(-5)])


class TestKlineToRow:
    def test_field_mapping(self):
        k = Kline(
            open_time=NOW, open=1.0, high=1.2, low=0.9, close=1.1, volume=10.0,
            close_time=NOW + STEP - 1, quote_volume=11.0, trades=7,
            taker_buy_base=4.0, taker_buy_quote=4.4,
        )
        row = kline_to_row(k)
        assert set(row) == {name for name, _ in KLINE_FIELDS}
        assert row["close"] == 1.1
        assert row["taker_buy_quote"] == 4.4

    def test_wrong_type_raises(self):
        with pytest.raises(RecorderError):
            kline_to_row({"open_time": NOW})


class TestStore:
    @pytest.fixture()
    def store(self, tmp_path):
        return KlineStore(tmp_path / "live")

    def test_initial_upsert_counts_new(self, store):
        added = store.upsert("TESTUSDT", [krow(NOW), krow(NOW + STEP)])
        assert added == 2
        assert store.last_open_ms("TESTUSDT") == NOW + STEP

    def test_identical_upsert_adds_zero(self, store):
        rows = [krow(NOW)]
        store.upsert("TESTUSDT", rows)
        assert store.upsert("TESTUSDT", rows) == 0

    def test_overlap_new_wins(self, store):
        store.upsert("TESTUSDT", [krow(NOW, 100.0), krow(NOW + STEP, 101.0)])
        added = store.upsert("TESTUSDT", [krow(NOW + STEP, 999.0), krow(NOW + 2 * STEP, 102.0)])
        assert added == 1
        rows = store.load("TESTUSDT")
        assert rows[1]["close"] == 999.0
        assert len(rows) == 3

    def test_missing_symbol_is_empty(self, store):
        assert store.load("NOPEUSDT") == []
        assert store.last_open_ms("NOPEUSDT") is None

    def test_invalid_symbol_raises(self, store):
        with pytest.raises(RecorderError):
            store.path_for("bad symbol!")

    def test_no_tmp_left(self, store):
        store.upsert("TESTUSDT", [krow(NOW)])
        assert list(store.root.glob("*.tmp")) == []


class TestIsStale:
    def test_none_is_stale(self):
        assert is_stale(None, NOW, STEP) is True

    def test_fresh_not_stale(self):
        assert is_stale(NOW - STEP, NOW, STEP) is False

    def test_old_is_stale(self):
        assert is_stale(NOW - 4 * STEP, NOW, STEP) is True

    def test_bad_input_raises(self):
        with pytest.raises(RecorderError):
            is_stale(NOW, "now", STEP)
