"""Tests for signalx.book: frame parsing, band features, store roundtrip."""

from __future__ import annotations

import pytest

from signalx.book import (
    BookError,
    BookStore,
    BookTicker,
    DepthSnapshot,
    band_depth,
    depth_features,
    merge_book_rows,
    parse_book_message,
    parse_depth_rest,
)


def depth_frame(symbol="AGTUSDT", ts=1_000, bids=None, asks=None):
    return {
        "stream": f"{symbol.lower()}@depth20@1000ms",
        "data": {
            "e": "depthUpdate",
            "E": ts,
            "T": ts,
            "s": symbol,
            "b": bids if bids is not None else [["100", "2"], ["98", "1"]],
            "a": asks if asks is not None else [["101", "1"], ["104", "1"]],
        },
    }


def ticker_frame(symbol="AGTUSDT", ts=1_000, bid="100", bidq="3", ask="101", askq="4"):
    return {
        "stream": f"{symbol.lower()}@bookTicker",
        "data": {
            "e": "bookTicker",
            "E": ts,
            "T": ts,
            "s": symbol,
            "b": bid,
            "B": bidq,
            "a": ask,
            "A": askq,
        },
    }


def make_snap(ts=1_000):
    return DepthSnapshot(
        symbol="AGTUSDT",
        ts_ms=ts,
        bids=((100.0, 2.0), (98.0, 1.0)),
        asks=((101.0, 1.0), (104.0, 1.0)),
    )


class TestParsing:
    def test_depth_parsed(self):
        ev = parse_book_message(depth_frame())
        assert isinstance(ev, DepthSnapshot)
        assert ev.symbol == "AGTUSDT"
        assert ev.ts_ms == 1_000
        assert ev.bids == ((100.0, 2.0), (98.0, 1.0))

    def test_ticker_parsed(self):
        ev = parse_book_message(ticker_frame())
        assert isinstance(ev, BookTicker)
        assert ev.bid == 100.0
        assert ev.ask == 101.0
        assert ev.bid_qty == 3.0
        assert ev.ask_qty == 4.0

    def test_ack_returns_none(self):
        assert parse_book_message({"result": None, "id": 1}) is None

    def test_unknown_event_returns_none(self):
        assert parse_book_message({"stream": "x", "data": {"e": "aggTrade"}}) is None

    def test_fail_loud_cases(self):
        with pytest.raises(BookError):
            parse_book_message(["not a dict"])
        with pytest.raises(BookError):
            parse_book_message({"nope": 1})
        with pytest.raises(BookError):
            parse_book_message(depth_frame(bids=[]))
        with pytest.raises(BookError):
            parse_book_message(depth_frame(bids=[["abc", "1"]]))
        with pytest.raises(BookError):
            parse_book_message(depth_frame(bids=[["100"]]))
        with pytest.raises(BookError):
            parse_book_message(depth_frame(bids=[["100", "-1"]]))
        with pytest.raises(BookError):
            parse_book_message(ticker_frame(bid="105", ask="101"))

    def test_crossed_depth_raises(self):
        with pytest.raises(BookError):
            parse_book_message(depth_frame(bids=[["101", "1"]], asks=[["101", "1"]]))

    def test_rest_depth_parsed_with_timestamp(self):
        snap = parse_depth_rest(
            {"T": 1234, "bids": [["100", "2"]], "asks": [["101", "1"]]},
            symbol="AGTUSDT",
            now_ms=9_999,
        )
        assert snap.symbol == "AGTUSDT"
        assert snap.ts_ms == 1234
        assert snap.bids == ((100.0, 2.0),)

    def test_rest_depth_uses_now_when_no_timestamp(self):
        snap = parse_depth_rest(
            {"bids": [["100", "2"]], "asks": [["101", "1"]]},
            symbol="AGTUSDT",
            now_ms=9_999,
        )
        assert snap.ts_ms == 9_999

    def test_rest_depth_fails_loud(self):
        with pytest.raises(BookError):
            parse_depth_rest([], symbol="AGTUSDT", now_ms=9_999)
        with pytest.raises(BookError):
            parse_depth_rest({"bids": [], "asks": []}, symbol="AGTUSDT", now_ms=9_999)


class TestBands:
    def test_band_depth_math(self):
        snap = make_snap()
        bid_n, ask_n, imbal = band_depth(snap, 2.0)
        # mid 100.5; low 98.49 -> bid 100 only; high 102.51 -> ask 101 only
        assert bid_n == 200.0
        assert ask_n == 101.0
        assert imbal == pytest.approx((200.0 - 101.0) / 301.0)

        bid_n, ask_n, imbal = band_depth(snap, 10.0)
        assert bid_n == pytest.approx(298.0)
        assert ask_n == pytest.approx(205.0)
        assert imbal == pytest.approx((298.0 - 205.0) / 503.0)

    def test_band_pct_invalid(self):
        with pytest.raises(BookError):
            band_depth(make_snap(), 0)
        with pytest.raises(BookError):
            band_depth(make_snap(), True)

    def test_features_row(self):
        ticker = BookTicker("AGTUSDT", 100.0, 3.0, 101.0, 4.0, 999)
        row = depth_features(make_snap(), ticker=ticker)
        assert row["ts_ms"] == 1_000
        assert row["mid"] == pytest.approx(100.5)
        assert row["spread_abs"] == pytest.approx(1.0)
        assert row["spread_bps"] == pytest.approx(1.0 / 100.5 * 10_000.0)
        assert row["bid0"] == 100.0
        assert row["ask0"] == 101.0
        assert row["bid0_qty"] == 2.0
        assert row["ask0_qty"] == 1.0
        # 0.5% band: low 99.9975 -> bid 100 only; high 101.0025 -> ask 101 only
        assert row["d05_bid"] == 200.0
        assert row["d05_ask"] == 101.0
        assert row["imbal05"] == pytest.approx((200.0 - 101.0) / 301.0)
        assert row["tb_bid"] == 100.0
        assert row["tb_ask_qty"] == 4.0

    def test_features_without_ticker_nulls(self):
        row = depth_features(make_snap())
        assert row["tb_bid"] is None
        assert row["tb_ask"] is None
        assert row["tb_bid_qty"] is None
        assert row["tb_ask_qty"] is None

    def test_features_foreign_ticker_nulls(self):
        foreign = BookTicker("OTHERUSDT", 1.0, 1.0, 2.0, 1.0, 999)
        row = depth_features(make_snap(), ticker=foreign)
        assert row["tb_bid"] is None

    def test_features_bad_args(self):
        with pytest.raises(BookError):
            depth_features({"not": "a snapshot"})
        with pytest.raises(BookError):
            depth_features(make_snap(), ticker="nope")


class TestStore:
    def _row(self, ts=1_000):
        return depth_features(make_snap(ts))

    def test_upsert_merge_dedupe(self, tmp_path):
        store = BookStore(tmp_path)
        assert store.upsert("AGTUSDT", [self._row(2_000)]) == 1
        updated = self._row(2_000)
        updated["mid"] = 999.0  # new wins
        assert store.upsert("AGTUSDT", [updated, self._row(3_000)]) == 1
        rows = store.load("AGTUSDT")
        assert [r["ts_ms"] for r in rows] == [2_000, 3_000]
        assert rows[0]["mid"] == 999.0
        assert store.last_ts_ms("AGTUSDT") == 3_000

    def test_empty_store(self, tmp_path):
        store = BookStore(tmp_path)
        assert store.load("AGTUSDT") == []
        assert store.last_ts_ms("AGTUSDT") is None

    def test_bad_symbol_raises(self, tmp_path):
        with pytest.raises(BookError):
            BookStore(tmp_path).upsert("bad symbol!", [self._row()])

    def test_unknown_column_fails_loud(self, tmp_path):
        bad = self._row()
        bad["bogus"] = 1.0
        with pytest.raises(BookError):
            BookStore(tmp_path).upsert("AGTUSDT", [bad])

    def test_missing_ts_fails_loud(self):
        bad = self._row()
        bad["ts_ms"] = None
        with pytest.raises(BookError):
            merge_book_rows([], [bad])

    def test_non_dict_row_fails_loud(self):
        with pytest.raises(BookError):
            merge_book_rows([], ["not a row"])
