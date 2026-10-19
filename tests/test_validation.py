"""Security-critical validation tests.

Every case here asserts that malformed/hostile input FAILS LOUDLY
(ValidationError). If any of these pass silently, that is a security bug.
"""

from __future__ import annotations

import pytest

from signalx.validation import (
    ValidationError,
    parse_agg_trade,
    parse_kline_row,
    taker_direction,
    validate_interval,
    validate_symbol,
)

VALID_ROW = ["1000", "1.0", "1.5", "0.9", "1.2", "100", "1059", "120.0", "10", "60", "72.0"]


# ------------------------------------------------------------------ symbols
@pytest.mark.parametrize(
    "hostile",
    [
        "../../etc/passwd",
        "BTC/USDT",
        "BTC USDT",
        "BTCUSDT;rm -rf /",
        "BTCUSDT\n",
        "btcusdt",                 # lowercase is not a valid REST symbol
        "BTC\u0000USDT",
        "BTCUSDT?foo=bar",
        "ＡＢＣＤ",                 # unicode
        "",
        "A" * 30,                  # too long
        "AB1",                     # too short
    ],
)
def test_symbol_whitelist_rejects_hostile_input(hostile):
    with pytest.raises(ValidationError):
        validate_symbol(hostile)


@pytest.mark.parametrize("not_a_string", [None, 123, ["BTCUSDT"], {"s": "BTCUSDT"}, True])
def test_symbol_rejects_non_strings(not_a_string):
    with pytest.raises(ValidationError):
        validate_symbol(not_a_string)


@pytest.mark.parametrize("good", ["BTCUSDT", "ETHBTC", "1000PEPEUSDT", "1INCHUSDT"])
def test_symbol_accepts_valid(good):
    assert validate_symbol(good) == good


# ---------------------------------------------------------------- intervals
@pytest.mark.parametrize("hostile", ["60m", "'; DROP TABLE--", "1m\n", "../", "", None, 60])
def test_interval_rejects_non_whitelisted(hostile):
    with pytest.raises(ValidationError):
        validate_interval(hostile)


@pytest.mark.parametrize("good", ["1m", "5m", "1h", "4h", "1d", "1M"])
def test_interval_accepts_whitelisted(good):
    assert validate_interval(good) == good


# ------------------------------------------------------------------- klines
def test_valid_kline_parses():
    k = parse_kline_row(VALID_ROW)
    assert k.open == 1.0 and k.high == 1.5 and k.low == 0.9 and k.close == 1.2
    assert k.volume == 100.0 and k.taker_buy_base == 60.0


def test_kline_future_timestamp_rejected():
    future_row = list(VALID_ROW)
    future_row[0] = "1700000000000"
    future_row[6] = "1700000059999"
    with pytest.raises(ValidationError):
        parse_kline_row(future_row, now_ms=1_600_000_000_000)  # bar 100s in the future


def test_kline_timestamp_skew_within_tolerance_ok():
    k = parse_kline_row(VALID_ROW, now_ms=int(VALID_ROW[0]) + 30_000)
    assert k.open_time == 1000


@pytest.mark.parametrize(
    "mutation,field",
    [
        (lambda r: (r.pop(), r)[1], "missing fields"),
        (lambda r: r.__setitem__(1, "nan") or r, "NaN open"),
        (lambda r: r.__setitem__(4, "inf") or r, "Inf close"),
        (lambda r: r.__setitem__(2, "0.95") or r, "high below close"),
        (lambda r: r.__setitem__(3, "1.3") or r, "low above close"),
        (lambda r: r.__setitem__(5, "-5") or r, "negative volume"),
        (lambda r: r.__setitem__(9, "999") or r, "taker buy > volume"),
        (lambda r: r.__setitem__(1, "abc") or r, "non-numeric"),
        (lambda r: r.__setitem__(8, "1.5") or r, "float trades"),
        (lambda r: r.__setitem__(0, "0") or r, "zero open_time"),
        (lambda r: r.__setitem__(6, "999") or r, "close_time <= open_time"),
        (lambda r: r.__setitem__(1, "9" * 100) or r, "oversized string"),
        (lambda r: r.__setitem__(1, "1e500") or r, "float overflow to inf"),
        (lambda r: r.__setitem__(2, "1e16") or r, "absurd magnitude"),
    ],
)
def test_kline_malformed_rows_raise(mutation, field):
    row = list(VALID_ROW)
    mutation(row)
    with pytest.raises(ValidationError):
        parse_kline_row(row)


def test_kline_rejects_non_list_and_type_confusion():
    with pytest.raises(ValidationError):
        parse_kline_row("1000,1,2,3,4,5,6,7,8,9,10")
    with pytest.raises(ValidationError):
        parse_kline_row({"open": 1})


def test_kline_rejects_bool_fields():
    row = list(VALID_ROW)
    row[1] = True
    with pytest.raises(ValidationError):
        parse_kline_row(row)


# ---------------------------------------------------------------- aggTrades
def test_agg_trade_valid_and_direction():
    t = parse_agg_trade({"T": 1000, "p": "1.5", "q": "2", "m": True})
    assert t.is_buyer_maker is True and t.qty == 2.0
    assert taker_direction(True) == -1    # buyer is maker => taker sold
    assert taker_direction(False) == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"T": 1000, "p": "1.5", "q": "2"},              # missing m
        {"T": 1000, "p": "1.5", "q": "2", "m": "yes"},  # m not bool
        {"T": 1000, "p": "nan", "q": "2", "m": True},   # NaN price
        {"T": 1000, "p": "1.5", "q": "-2", "m": True},  # negative qty
        {"T": 1000, "p": "0", "q": "2", "m": True},     # zero price
        {"T": "x", "p": "1.5", "q": "2", "m": True},    # bad timestamp
    ],
)
def test_agg_trade_hostile_payloads_raise(payload):
    with pytest.raises(ValidationError):
        parse_agg_trade(payload)
