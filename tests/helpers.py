"""Shared test helpers (not collected by pytest: no test_ prefix)."""

from __future__ import annotations

from signalx.models import Kline

START_MS = 1_600_000_000_000
MIN_MS = 60_000


def mk_kline(open_time, o, h, l, c, v=1000.0, tbb=None, trades=10, qv=None):
    if tbb is None:
        tbb = v / 2
    if qv is None:
        qv = v * c
    return Kline(
        open_time=open_time,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=v,
        close_time=open_time + MIN_MS - 1,
        quote_volume=qv,
        trades=trades,
        taker_buy_base=tbb,
        taker_buy_quote=tbb * c,
    )


def series_from_closes(closes, spread=0.5, volume=1000.0):
    """Build valid klines from a close series (o=prev close, +/- spread)."""
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        h = max(o, c) + spread
        l = min(o, c) - spread
        out.append(mk_kline(START_MS + i * MIN_MS, o, h, l, c, v=volume))
        prev = c
    return out


def bars_from_hl(highs, lows):
    """Build valid klines with o == c == midpoint. Lengths must match."""
    if len(highs) != len(lows):
        raise ValueError("length mismatch")
    out = []
    for i, (h, l) in enumerate(zip(highs, lows)):
        mid = (h + l) / 2.0
        out.append(mk_kline(START_MS + i * MIN_MS, mid, h, l, mid))
    return out
