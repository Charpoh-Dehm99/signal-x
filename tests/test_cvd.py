"""Tests for signalx.cvd — aggTrade parsing + minute buckets + store."""

from __future__ import annotations

import pytest

from signalx.cvd import (
    CvdError,
    CvdStore,
    MinuteBuckets,
    merge_cvd_rows,
    parse_agg_message,
)
from signalx.models import AggTrade


def msg(ts: int, price: float = 10.0, qty: float = 2.0, maker: bool = False, sym: str = "AGTUSDT"):
    return {
        "stream": f"{sym.lower()}@aggTrade",
        "data": {"e": "aggTrade", "s": sym, "p": str(price), "q": str(qty), "m": maker, "T": ts},
    }


def trade(ts: int, price: float = 10.0, qty: float = 2.0, maker: bool = False) -> AggTrade:
    return AggTrade(ts, price, qty, maker)


# --- parsing ---------------------------------------------------------------


def test_parse_combined_frame_ok():
    sym, t = parse_agg_message(msg(60_000, 10.0, 2.0, maker=False))
    assert sym == "AGTUSDT"
    assert t == AggTrade(60_000, 10.0, 2.0, False)


def test_parse_trade_event_ok():
    frame = msg(60_000, 10.0, 2.0, maker=True)
    frame["data"]["e"] = "trade"  # this environment delivers trade, not aggTrade
    sym, t = parse_agg_message(frame)
    assert sym == "AGTUSDT"
    assert t == AggTrade(60_000, 10.0, 2.0, True)


def test_parse_zero_value_record_is_skipped():
    frame = msg(60_000)
    frame["data"].update({"p": "0", "q": "0", "X": "NA", "st": 1})
    assert parse_agg_message(frame) is None


def test_parse_negative_price_still_raises():
    frame = msg(60_000)
    frame["data"]["p"] = "-1"
    with pytest.raises(CvdError):
        parse_agg_message(frame)


def test_parse_ack_is_none():
    assert parse_agg_message({"result": None, "id": 1}) is None


def test_parse_non_dict_raises():
    with pytest.raises(CvdError):
        parse_agg_message("nope")


def test_parse_missing_data_raises():
    with pytest.raises(CvdError):
        parse_agg_message({"foo": 1})


def test_parse_other_event_is_none():
    assert parse_agg_message({"data": {"e": "depthUpdate", "s": "AGTUSDT"}}) is None


def test_parse_bad_trade_fields_raises():
    bad = msg(60_000)
    del bad["data"]["p"]
    with pytest.raises(CvdError):
        parse_agg_message(bad)


def test_parse_bad_symbol_raises():
    with pytest.raises(CvdError):
        parse_agg_message(msg(60_000, sym="龙虾USDT"))


# --- buckets ----------------------------------------------------------------


def test_bucket_accumulates_and_emits_on_rollover():
    b = MinuteBuckets("AGTUSDT")
    assert b.add(trade(60_000 + 10_000, 10.0, 2.0, maker=False)) is None   # buy 20
    assert b.add(trade(60_000 + 50_000, 10.0, 1.0, maker=True)) is None    # sell 10
    closed = b.add(trade(120_000 + 5_000, 10.0, 3.0, maker=False))         # new minute
    assert closed == {
        "ts_min": 60_000,
        "buy_notional": 20.0,
        "sell_notional": 10.0,
        "delta": 10.0,
        "trades": 2,
    }


def test_bucket_same_minute_no_emit():
    b = MinuteBuckets("AGTUSDT")
    b.add(trade(60_000 + 1_000))
    assert b.add(trade(60_000 + 2_000)) is None


def test_bucket_out_of_order_raises():
    b = MinuteBuckets("AGTUSDT")
    b.add(trade(120_000))
    with pytest.raises(CvdError):
        b.add(trade(60_000))


def test_bucket_direction_signs():
    b = MinuteBuckets("AGTUSDT")
    b.add(trade(60_000, 10.0, 1.0, maker=True))   # taker sell
    closed = b.add(trade(120_000, 10.0, 1.0, maker=False))  # taker buy -> closes prev
    assert closed["delta"] == pytest.approx(-10.0)


def test_bucket_rejects_non_aggtrade():
    b = MinuteBuckets("AGTUSDT")
    with pytest.raises(CvdError):
        b.add({"ts": 1})  # type: ignore[arg-type]


def test_bucket_rejects_bad_symbol():
    with pytest.raises(CvdError):
        MinuteBuckets("bad symbol")


def test_bucket_rejects_nonpositive_price():
    b = MinuteBuckets("AGTUSDT")
    with pytest.raises(CvdError):
        b.add(AggTrade(60_000, 0.0, 1.0, False))


# --- store ------------------------------------------------------------------


def row(ts_min: int, buy: float = 10.0, sell: float = 5.0, trades: int = 3) -> dict:
    return {
        "ts_min": ts_min,
        "buy_notional": buy,
        "sell_notional": sell,
        "delta": buy - sell,
        "trades": trades,
    }


def test_store_roundtrip_and_sorted(tmp_path):
    store = CvdStore(tmp_path)
    assert store.upsert("AGTUSDT", [row(120_000), row(60_000)]) == 2
    rows = store.load("AGTUSDT")
    assert [r["ts_min"] for r in rows] == [60_000, 120_000]
    assert store.last_ts_min("AGTUSDT") == 120_000


def test_store_dedupe_new_wins(tmp_path):
    store = CvdStore(tmp_path)
    store.upsert("AGTUSDT", [row(60_000, buy=10.0)])
    assert store.upsert("AGTUSDT", [row(60_000, buy=99.0)]) == 0
    assert store.load("AGTUSDT")[0]["buy_notional"] == 99.0


def test_store_rejects_unknown_columns(tmp_path):
    store = CvdStore(tmp_path)
    with pytest.raises(CvdError):
        store.upsert("AGTUSDT", [dict(row(60_000), bogus=1.0)])


def test_store_missing_field_raises(tmp_path):
    store = CvdStore(tmp_path)
    bad = row(60_000)
    del bad["delta"]
    with pytest.raises(CvdError):
        store.upsert("AGTUSDT", [bad])


def test_merge_rows_empty():
    assert merge_cvd_rows([], []) == []
