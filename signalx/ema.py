"""EMA 8/13/21 triple-EMA engine. Pure functions, no I/O.

Semantics match the classic charting definition (same as the reference alert
script): each EMA is seeded with the SMA of the first `period` closes,
alpha = 2 / (period + 1). State is UP while EMA8 > EMA13 > EMA21, DOWN while
EMA8 < EMA13 < EMA21, NEUTRAL otherwise. A cross is the state flipping between
UP and DOWN; a neutral excursion in between neither cancels the next cross nor
double-fires it.

The cascade helpers time the same cross across timeframes (5m -> 15m -> 1h ->
4h) and detect propagation: a chain where each slower timeframe flips the same
way within `CROSS_LAG_BARS` of its own bars after the previous one.

Observation tool only: crosses are alerts to look, not trade signals (G0
killed 2026-10-17).
"""

from __future__ import annotations

from dataclasses import dataclass

FAST = 8
MID = 13
SLOW = 21
CASCADE_TFS = ("5m", "15m", "1h", "4h")
CROSS_LAG_BARS = 3   # per slower-TF bar a chained cross may lag the previous one
MAX_BASE_AGE_BARS = 288  # the base-TF cross must be at most this old to chain


def ema(values: list[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first `period` values (None before)."""
    if period < 1:
        raise ValueError("period must be >= 1")
    out: list[float | None] = []
    alpha = 2.0 / (period + 1)
    prev: float | None = None
    for i, v in enumerate(values):
        if prev is None:
            if i < period - 1:
                out.append(None)
                continue
            prev = sum(values[:period]) / period
        elif i >= period:
            prev = alpha * v + (1.0 - alpha) * prev
        out.append(prev)
    return out


def triple_state(v8: float | None, v13: float | None, v21: float | None) -> str | None:
    """'up' (all stacked bullish), 'down', 'neutral', or None while warming up."""
    if v8 is None or v13 is None or v21 is None:
        return None
    if v8 > v13 > v21:
        return "up"
    if v8 < v13 < v21:
        return "down"
    return "neutral"


@dataclass(frozen=True)
class EmaCross:
    index: int        # bar index (closed bars) where the state changed
    time: int         # open_time of that bar (ms)
    direction: str    # 'up' | 'down'
    prev_state: str   # directional state before the change ('up' | 'down')


def cross_events(open_times: list[int], states: list[str | None]) -> list[EmaCross]:
    """State flips between 'up' and 'down', in bar order.

    Neutral reads in between do not fire or cancel anything: the event lands on
    the first bar whose directional state differs from the previous one.
    """
    if len(open_times) != len(states):
        raise ValueError("open_times/states length mismatch")
    out: list[EmaCross] = []
    last: str | None = None
    for i, s in enumerate(states):
        if s not in ("up", "down"):
            continue
        if last is not None and s != last:
            out.append(EmaCross(i, open_times[i], s, last))
        last = s
    return out


@dataclass(frozen=True)
class TripleEmaResult:
    fast: list[float | None]
    mid: list[float | None]
    slow: list[float | None]
    states: list[str | None]
    crosses: list[EmaCross]

    @property
    def state(self) -> str | None:
        return next((s for s in reversed(self.states) if s is not None), None)


def triple_ema(
    open_times: list[int],
    closes: list[float],
    periods: tuple[int, int, int] = (FAST, MID, SLOW),
) -> TripleEmaResult:
    """EMA stack over closes; every array is index-aligned with the input."""
    if len(open_times) != len(closes):
        raise ValueError("open_times/closes length mismatch")
    p8, p13, p21 = periods
    if not p8 < p13 < p21:
        raise ValueError("periods must be strictly ascending")
    fast = ema(closes, p8)
    mid = ema(closes, p13)
    slow = ema(closes, p21)
    states = [triple_state(a, b, c) for a, b, c in zip(fast, mid, slow)]
    return TripleEmaResult(fast, mid, slow, states, cross_events(open_times, states))


@dataclass(frozen=True)
class TfInfo:
    tf: str
    step_ms: int
    state: str | None                          # None while warming up
    emas: tuple[float | None, float | None, float | None]  # last fast/mid/slow
    crosses: tuple[EmaCross, ...]              # oldest first
    closed: int                                # closed bars analyzed


def tf_crosses(tf: str, step_ms: int, open_times: list[int], closes: list[float]) -> TfInfo:
    """Analyze one timeframe's closed bars into a TfInfo row."""
    res = triple_ema(open_times, closes)
    last = lambda arr: arr[-1] if arr else None  # noqa: E731
    return TfInfo(
        tf=tf,
        step_ms=step_ms,
        state=res.state,
        emas=(last(res.fast), last(res.mid), last(res.slow)),
        crosses=tuple(res.crosses),
        closed=len(closes),
    )


def last_cross_in(crosses: tuple[EmaCross, ...], direction: str) -> EmaCross | None:
    return next((c for c in reversed(crosses) if c.direction == direction), None)


def cascade_pattern(
    infos: list[TfInfo],
    *,
    now_ms: int,
    lag_bars: int = CROSS_LAG_BARS,
    max_base_age_bars: int = MAX_BASE_AGE_BARS,
) -> dict:
    """Time the cross propagation across timeframes (lightest TF first).

    A chain starts at the smallest timeframe whose latest cross in direction d
    is recent (<= max_base_age_bars of its own bars) and whose state is still
    d; each next timeframe joins while its own cross in d lands within
    lag_bars * (its step) after the previous chain member. The longest chain
    across both directions wins (ties keep 'down').

    Returns the winning chain, the measured intervals between members, and the
    full-stack alignment (all analyzed timeframes in one directional state).
    """
    ordered = sorted(infos, key=lambda i: i.step_ms)
    best_dir: str | None = None
    best_chain: list[str] = []
    best_intervals: list[int] = []
    for d in ("down", "up"):
        chain: list[str] = []
        intervals: list[int] = []
        prev_close_ms = 0
        for info in ordered:
            cross = last_cross_in(info.crosses, d)
            if cross is None or info.state != d:
                if chain:
                    break
                continue
            close_ms = cross.time + info.step_ms
            if not chain:
                age = (now_ms - close_ms) / info.step_ms
                if 0 <= age <= max_base_age_bars:
                    chain.append(info.tf)
                    prev_close_ms = close_ms
                continue
            gap = close_ms - prev_close_ms
            if 0 <= gap <= lag_bars * info.step_ms:
                chain.append(info.tf)
                intervals.append(gap)
                prev_close_ms = close_ms
            else:
                break
        if len(chain) > len(best_chain):
            best_dir, best_chain, best_intervals = d, chain, intervals

    states = {i.state for i in ordered}
    single = states.pop() if len(states) == 1 else None
    full_stack = single if single in ("up", "down") else None
    return {
        "direction": best_dir,
        "chain": best_chain,
        "intervals_ms": best_intervals,
        "full_stack": full_stack,
    }
