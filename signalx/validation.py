"""Strict, fail-loud validation for all exchange-derived data.

Security scope:
- Symbol whitelisting (blocks URL/path/JSON injection into downstream requests).
- Interval whitelisting (never interpolate caller strings into requests).
- Numeric poisoning (NaN/Inf, bool confusion, oversized strings) rejected.
- OHLC invariants and volume sanity enforced.
- Timestamp sanity (no future bars, monotonic close_time).

Policy: every malformed input raises ValidationError. Nothing is silently
coerced or dropped here. Callers that *filter* (e.g., ranking candidates)
must record a reason for every exclusion.
"""

from __future__ import annotations

import math
import re

from .models import AggTrade, Kline


class ValidationError(ValueError):
    """Raised on any invalid input. Never swallow this."""


# NOTE: \Z (not $) — Python's '$' matches before a trailing newline, which
# would let "BTCUSDT\n" through. Caught by tests/test_validation.py.
SYMBOL_RE = re.compile(r"^[A-Z0-9]{4,20}\Z")

INTERVAL_WHITELIST = frozenset({
    "1s", "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
})

MAX_STR = 64           # any longer attacker-controlled string is rejected
NUM_MAX_ABS = 1e15     # sanity bound on prices/volumes
MAX_CLOCK_SKEW_MS = 60_000


def validate_symbol(symbol: object) -> str:
    if not isinstance(symbol, str):
        raise ValidationError(f"symbol must be str, got {type(symbol).__name__}")
    if len(symbol) > MAX_STR:
        raise ValidationError("symbol too long")
    if not SYMBOL_RE.match(symbol):
        raise ValidationError(f"symbol rejected by whitelist: {symbol!r}")
    return symbol


def validate_interval(interval: object) -> str:
    if not isinstance(interval, str) or interval not in INTERVAL_WHITELIST:
        raise ValidationError(f"interval not whitelisted: {interval!r}")
    return interval


def _strict_float(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise ValidationError(f"{name}: bool not allowed")
    if isinstance(value, (int, float)):
        f = float(value)
    elif isinstance(value, str):
        if len(value) > MAX_STR:
            raise ValidationError(f"{name}: string too long")
        try:
            f = float(value)
        except ValueError as exc:
            raise ValidationError(f"{name}: not numeric: {value!r}") from exc
    else:
        raise ValidationError(f"{name}: unsupported type {type(value).__name__}")
    if math.isnan(f) or math.isinf(f):
        raise ValidationError(f"{name}: NaN/Inf rejected")
    if abs(f) > NUM_MAX_ABS:
        raise ValidationError(f"{name}: out of sane bounds")
    return f


def _strict_int(value: object, name: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{name}: bool not allowed")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if len(value) > MAX_STR:
            raise ValidationError(f"{name}: string too long")
        try:
            return int(value)
        except ValueError as exc:
            raise ValidationError(f"{name}: not an integer: {value!r}") from exc
    raise ValidationError(f"{name}: invalid integer type/value")


def parse_kline_row(row: object, *, now_ms: int | None = None) -> Kline:
    """Parse one Binance kline array (>= 11 fields) with full validation."""
    if not isinstance(row, (list, tuple)) or len(row) < 11:
        raise ValidationError("kline row: expected list/tuple with >= 11 fields")

    open_time = _strict_int(row[0], "open_time")
    o = _strict_float(row[1], "open")
    h = _strict_float(row[2], "high")
    low = _strict_float(row[3], "low")
    c = _strict_float(row[4], "close")
    v = _strict_float(row[5], "volume")
    close_time = _strict_int(row[6], "close_time")
    qv = _strict_float(row[7], "quote_volume")
    trades = _strict_int(row[8], "trades")
    tbb = _strict_float(row[9], "taker_buy_base")
    tbq = _strict_float(row[10], "taker_buy_quote")

    if open_time <= 0 or close_time <= open_time:
        raise ValidationError("timestamps invalid")
    if now_ms is not None:
        now_ms_int = _strict_int(now_ms, "now_ms")
        if open_time > now_ms_int + MAX_CLOCK_SKEW_MS:
            raise ValidationError("open_time in the future")
    if h < max(o, c) or low > min(o, c) or low > h:
        raise ValidationError("OHLC invariants violated")
    if o <= 0 or h <= 0 or low <= 0 or c <= 0:
        raise ValidationError("non-positive price")
    if v < 0 or qv < 0 or trades < 0 or tbb < 0 or tbq < 0:
        raise ValidationError("negative volume/trades rejected")
    if v > 0 and tbb > v:
        raise ValidationError("taker_buy_base exceeds total volume")

    return Kline(open_time, o, h, low, c, v, close_time, qv, trades, tbb, tbq)


def parse_agg_trade(payload: object) -> AggTrade:
    if not isinstance(payload, dict):
        raise ValidationError("aggTrade payload must be a dict")
    try:
        ts = _strict_int(payload["T"], "T")
        price = _strict_float(payload["p"], "p")
        qty = _strict_float(payload["q"], "q")
        maker = payload["m"]
    except KeyError as exc:
        raise ValidationError(f"aggTrade missing field: {exc}") from exc
    if not isinstance(maker, bool):
        raise ValidationError("aggTrade 'm' must be boolean")
    if price <= 0 or qty <= 0:
        raise ValidationError("aggTrade non-positive price/qty")
    return AggTrade(ts, price, qty, maker)


def taker_direction(is_buyer_maker: bool) -> int:
    """Aggressor side: if the buyer was the maker, the taker was a SELLER."""
    return -1 if is_buyer_maker else 1
