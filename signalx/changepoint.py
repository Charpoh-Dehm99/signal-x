"""Change-point detection (online CUSUM) for plateau/regime shift alarms."""

from __future__ import annotations

import statistics


def cusum(
    values: list[float],
    *,
    k: float = 0.5,
    h: float = 5.0,
    min_len: int = 20,
) -> dict:
    """One-sided CUSUM for upward step detection on an internally
    z-normalized series.

    S_t = max(0, S_{t-1} + z_t - k); alarm when S_t > h.
    Returns alarm indices and the first alarm.
    """
    if len(values) < min_len:
        raise ValueError("series too short for CUSUM")
    if k < 0:
        raise ValueError("k must be >= 0")
    if h <= 0:
        raise ValueError("h must be > 0")

    mu = statistics.mean(values)
    sd = statistics.pstdev(values) or 1.0
    s = 0.0
    alarms: list[int] = []
    for i, v in enumerate(values):
        z = (v - mu) / sd
        s = max(0.0, s + z - k)
        if s > h:
            alarms.append(i)
            s = 0.0
    return {
        "alarm_index": alarms[0] if alarms else None,
        "alarms": alarms,
        "final_s": s,
    }
