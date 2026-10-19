"""Pump-screening funnel: 1h screen -> canonical events -> 1m download plan.

Downloading 1m klines for the full universe over 24 months would be ~100 GB.
The hourly series is ~60x smaller and matches the blueprint's pump-detection
resolution exactly (24h return + 7d volume z-score). Strategy: screen on 1h
for every symbol, then download 1m klines + funding only for event months +/- 1
(the event-study window is [-48h, +14d]).

Right-edge handling: monthly dumps for the current, unfinished month publish
only after it ends, so events near the study end (September 2026) get their
post-event days from daily 1m dumps instead.

Pure functions; fail loud (FunnelError).
"""

from __future__ import annotations

import calendar
import datetime
import re

import pandas as pd

from .eventstudy import pump_events

MONTH_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])\Z")
DAY_RE = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\Z")


class FunnelError(RuntimeError):
    """Any funnel/plan failure that must be surfaced, never swallowed."""


# ------------------------------------------------------------ month math


def month_of(ms: int) -> str:
    """UTC year-month string ("YYYY-MM") for a millisecond timestamp."""
    if isinstance(ms, bool) or not isinstance(ms, int) or ms < 0:
        raise FunnelError(f"month_of: bad timestamp {ms!r}")
    dt = datetime.datetime.fromtimestamp(ms / 1000.0, tz=datetime.timezone.utc)
    return f"{dt.year:04d}-{dt.month:02d}"


def _parse_month(month: object, name: str = "month") -> tuple[int, int]:
    if not isinstance(month, str) or not MONTH_RE.match(month):
        raise FunnelError(f"{name} must be YYYY-MM, got {month!r}")
    y, m = month.split("-")
    return int(y), int(m)


def prev_month(month: str) -> str:
    y, m = _parse_month(month)
    m -= 1
    if m == 0:
        y, m = y - 1, 12
    if y < 1:
        raise FunnelError(f"prev_month underflow for {month!r}")
    return f"{y:04d}-{m:02d}"


def next_month(month: str) -> str:
    y, m = _parse_month(month)
    m += 1
    if m == 13:
        y, m = y + 1, 1
    return f"{y:04d}-{m:02d}"


def context_months(event_month: str, *, lo: str, hi: str) -> list[str]:
    """{M-1, M, M+1} clipped to [lo, hi] — the 1m context window per event."""
    _parse_month(lo, "lo")
    _parse_month(hi, "hi")
    if lo > hi:
        raise FunnelError(f"context_months: lo {lo} > hi {hi}")
    months = {prev_month(event_month), event_month, next_month(event_month)}
    return sorted(m for m in months if lo <= m <= hi)


def tail_daily_days(after_month: str, through_day: str) -> list[str]:
    """Every day of `after_month` from the 1st through `through_day` inclusive."""
    y, m = _parse_month(after_month, "after_month")
    if not isinstance(through_day, str) or not DAY_RE.match(through_day):
        raise FunnelError(f"through_day must be YYYY-MM-DD, got {through_day!r}")
    end = datetime.date.fromisoformat(through_day)
    if (end.year, end.month) != (y, m):
        raise FunnelError(f"through_day {through_day} is not in {after_month}")
    return [f"{after_month}-{d:02d}" for d in range(1, end.day + 1)]


# --------------------------------------------------------------- screening


def screen_symbol(symbol: str, hourly: pd.DataFrame, *, start_ms: int, end_ms: int) -> pd.DataFrame:
    """Pump events for one symbol's hourly series within [start_ms, end_ms].

    `hourly` must carry the standard kline columns (open_time, close,
    quote_volume, ...) and be sorted ascending; it may start before start_ms
    (lookback context). Returns the pump_events frame + symbol/month columns.
    """
    if not isinstance(symbol, str) or not symbol:
        raise FunnelError("screen_symbol: bad symbol")
    if start_ms > end_ms:
        raise FunnelError("screen_symbol: start_ms > end_ms")
    events = pump_events(hourly)
    keep = events["open_time"].between(start_ms, end_ms) if len(events) else pd.Series(dtype=bool)
    events = events.loc[keep].reset_index(drop=True) if len(events) else events
    events = events.assign(symbol=symbol, month=events["open_time"].map(month_of) if len(events) else None)
    return events


# ------------------------------------------------------------------ plan


SERIES_KINDS = {"1h": "klines_1h", "1m": "klines", "funding": "funding"}
_SERIES_MONTHS_KEY = {"1h": "months_1h", "1m": "months_1m", "funding": "months_funding"}


def plan_from_universe(universe_doc: dict, series: str) -> list[tuple[str, str, str]]:
    """Every available month per symbol for one series, as plan rows.

    `series` is "1h", "1m" or "funding" (see signalx.universe month listings).
    """
    if series not in SERIES_KINDS:
        raise FunnelError(f"unknown series {series!r} (known: {', '.join(SERIES_KINDS)})")
    if not isinstance(universe_doc, dict) or not isinstance(universe_doc.get("symbols"), dict):
        raise FunnelError("universe document missing 'symbols' map")
    kind = SERIES_KINDS[series]
    months_key = _SERIES_MONTHS_KEY[series]
    tasks: set[tuple[str, str, str]] = set()
    for symbol, meta in universe_doc["symbols"].items():
        if not isinstance(meta, dict) or not isinstance(meta.get(months_key), list):
            raise FunnelError(f"universe {symbol}: missing {months_key}")
        for month in meta[months_key]:
            tasks.add((kind, symbol, _validate_plan_period(month)))
    return sorted(tasks)


def _validate_plan_period(month: object) -> str:
    _parse_month(month, "universe month")
    return month


def window_months(start_ms: int, end_ms: int, *, lo: str, hi: str) -> list[str]:
    """Calendar months intersecting [start_ms, end_ms], clipped to [lo, hi]."""
    if isinstance(start_ms, bool) or not isinstance(start_ms, int) or start_ms < 0:
        raise FunnelError(f"window_months: bad start_ms {start_ms!r}")
    if isinstance(end_ms, bool) or not isinstance(end_ms, int) or end_ms < 0:
        raise FunnelError(f"window_months: bad end_ms {end_ms!r}")
    if start_ms > end_ms:
        raise FunnelError("window_months: start_ms > end_ms")
    _parse_month(lo, "lo")
    _parse_month(hi, "hi")
    if lo > hi:
        raise FunnelError(f"window_months: lo {lo} > hi {hi}")
    first, last = month_of(start_ms), month_of(end_ms)
    months: list[str] = []
    m = first
    while m <= last:
        if lo <= m <= hi:
            months.append(m)
        m = next_month(m)
    return months


def build_tight_plan(
    events_ms: dict[str, list[int]],
    *,
    lo: str,
    hi: str,
    tail_through: str | None = None,
    lead_hours: int = 48,
    trail_days: int = 14,
) -> list[tuple[str, str, str]]:
    """Minimal 1m download plan: only the months each event window touches.

    The event-study window is [event - lead_hours, event + trail_days]. For
    every event in `events_ms` the plan adds the monthly 1m dumps of exactly
    the months that window intersects (clipped to [lo, hi]). Events whose
    window spills past `hi` additionally get daily 1m dumps for the following
    month, from day 1 through min(window end, tail_through) — monthly dumps do
    not exist for unfinished months.

    This is a strict refinement of build_plan's {M-1, M, M+1}: mid-month
    events touch one month instead of three, which keeps the full-universe
    1m footprint finite.
    """
    _parse_month(lo, "lo")
    _parse_month(hi, "hi")
    if lo > hi:
        raise FunnelError(f"build_tight_plan: lo {lo} > hi {hi}")
    if isinstance(lead_hours, bool) or not isinstance(lead_hours, int) or lead_hours < 1:
        raise FunnelError(f"build_tight_plan: bad lead_hours {lead_hours!r}")
    if isinstance(trail_days, bool) or not isinstance(trail_days, int) or trail_days < 1:
        raise FunnelError(f"build_tight_plan: bad trail_days {trail_days!r}")
    if tail_through is not None and not DAY_RE.match(tail_through):
        raise FunnelError(f"build_tight_plan: bad tail_through {tail_through!r}")

    tail_y, tail_m, tail_d = (
        (int(x) for x in tail_through.split("-")) if tail_through is not None else (0, 0, 0)
    )
    tasks: set[tuple[str, str, str]] = set()
    for symbol in sorted(events_ms):
        stamps = events_ms[symbol]
        if not isinstance(stamps, list):
            raise FunnelError(f"build_tight_plan: {symbol} events must be a list")
        for ms in sorted(stamps):
            lo_ms = ms - lead_hours * 3_600_000
            hi_ms = ms + trail_days * 86_400_000
            for m in window_months(lo_ms, hi_ms, lo=lo, hi=hi):
                tasks.add(("klines", symbol, m))
            end_month = month_of(hi_ms)
            nm = next_month(hi)
            if end_month >= nm and tail_through is not None:
                if (tail_y, tail_m) < (int(nm[:4]), int(nm[5:7])):
                    raise FunnelError(f"build_tight_plan: tail_through {tail_through} precedes {nm}")
                y, m = _parse_month(nm)
                last_day = calendar.monthrange(y, m)[1]
                if (tail_y, tail_m) == (y, m):
                    last_day = min(last_day, tail_d)
                end_day = datetime.datetime.fromtimestamp(hi_ms / 1000.0, tz=datetime.timezone.utc).day
                through = min(end_day, last_day)
                for day in tail_daily_days(nm, f"{nm}-{through:02d}"):
                    tasks.add(("klines_1m_daily", symbol, day))
    return sorted(tasks)


def build_plan(
    symbol_months: dict[str, set[str]],
    *,
    lo: str,
    hi: str,
    tail_through: str | None = None,
) -> list[tuple[str, str, str]]:
    """Download plan rows (kind, symbol, period) for the event months.

    For every (symbol, event month M):
      - 1m klines + funding for context months {M-1, M, M+1} clipped to [lo, hi].
      - if M is the last monthly month (hi), daily 1m klines for the following
        month through `tail_through` (monthly dumps do not exist yet for it).
    """
    _parse_month(lo, "lo")
    _parse_month(hi, "hi")
    if lo > hi:
        raise FunnelError(f"build_plan: lo {lo} > hi {hi}")
    if tail_through is not None and not DAY_RE.match(tail_through):
        raise FunnelError(f"build_plan: bad tail_through {tail_through!r}")

    tasks: set[tuple[str, str, str]] = set()
    for symbol in sorted(symbol_months):
        months = symbol_months[symbol]
        if not isinstance(months, set):
            raise FunnelError(f"build_plan: {symbol} months must be a set")
        for m in sorted(months):
            _parse_month(m, f"{symbol} month")
            if not (lo <= m <= hi):
                raise FunnelError(f"build_plan: event month {m} outside [{lo}, {hi}] for {symbol}")
            for pm in context_months(m, lo=lo, hi=hi):
                tasks.add(("klines", symbol, pm))
                tasks.add(("funding", symbol, pm))
            nm = next_month(m)
            if nm > hi and tail_through is not None:
                for day in tail_daily_days(nm, tail_through):
                    tasks.add(("klines_1m_daily", symbol, day))
    return sorted(tasks)
