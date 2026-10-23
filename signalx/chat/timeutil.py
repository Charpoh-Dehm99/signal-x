"""Local system-time formatting for the chat packet and read-only tools.

Market timestamps are epoch milliseconds. The LLM is unreliable at doing
timezone arithmetic in its head, so we hand it preformatted local labels
alongside the raw values. The raw epoch stays available for provenance.

The browser sends its IANA timezone with each chat request; when it is absent
or unrecognised we fall back to the server's system timezone.
"""

from __future__ import annotations

import math
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def resolve_timezone(name: str | None) -> tzinfo | None:
    """Resolve an IANA timezone name, returning None on invalid input."""
    if not isinstance(name, str):
        return None
    value = name.strip()
    if not value or len(value) > 64:
        return None
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _valid_ms(ms: object) -> bool:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool):
        return False
    value = float(ms)
    return math.isfinite(value) and value > 0


def _local_dt(ms: object, tz: tzinfo | None) -> datetime | None:
    if not _valid_ms(ms):
        return None
    if tz is None:
        return datetime.fromtimestamp(float(ms) / 1000).astimezone()
    return datetime.fromtimestamp(float(ms) / 1000, tz=tz)


def local_iso(ms: object, *, tz: tzinfo | None = None, timespec: str = "seconds") -> str | None:
    """Return an ISO-8601 timestamp in the display timezone."""
    dt = _local_dt(ms, tz)
    return dt.isoformat(timespec=timespec) if dt is not None else None


def local_label(ms: object, *, tz: tzinfo | None = None) -> str | None:
    """Return a human-readable display timestamp, e.g. 2026-10-23 09:59:59 EDT."""
    dt = _local_dt(ms, tz)
    if dt is None:
        return None
    zone = dt.tzname() or _offset_label(dt)
    return dt.strftime("%Y-%m-%d %H:%M:%S") + " " + zone


def local_zone(tz: tzinfo | None = None) -> str:
    """Return the display timezone name and UTC offset, e.g. EDT (UTC-04:00)."""
    now = datetime.now(tz=tz) if tz is not None else datetime.now().astimezone()
    name = now.tzname()
    offset = _offset_label(now)
    return f"{name} ({offset})" if name else offset


def _offset_label(dt: datetime) -> str:
    offset = dt.utcoffset()
    if offset is None:
        return "local time"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    hours, rem = divmod(abs(total), 3600)
    minutes = rem // 60
    return f"UTC{sign}{hours:02d}:{minutes:02d}"
