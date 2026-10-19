"""Swing detection with explicit confirmation indices (anti-lookahead).

Rules:
- A pivot at bar i is only usable from bar i+k onwards (fractal) or from the
  bar where the reversal threshold was crossed (zigzag).
- `pivots_as_of(pivots, index)` is the ONLY sanctioned way to consume pivots
  when simulating a decision at a point in time.
"""

from __future__ import annotations

from .indicators import atr as _atr
from .models import Kline, Pivot


def fractal_pivots(
    klines: list[Kline],
    k: int = 2,
    *,
    last_index: int | None = None,
) -> list[Pivot]:
    """Fractal swing highs/lows.

    last_index: pretend 'now' is this bar; pivots needing bars beyond it are
    not detectably confirmed yet and are excluded. (Tests pin this behavior.)
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    n = len(klines)
    if n == 0:
        return []
    if last_index is None:
        last_index = n - 1
    if last_index >= n or last_index < 0:
        raise ValueError("last_index out of range")

    out: list[Pivot] = []
    for i in range(k, last_index - k + 1):
        hi = klines[i].high
        lo = klines[i].low
        left = klines[i - k:i]
        right = klines[i + 1:i + k + 1]
        left_h = [b.high for b in left]
        right_h = [b.high for b in right]
        left_l = [b.low for b in left]
        right_l = [b.low for b in right]
        if hi > max(left_h) and hi > max(right_h):
            out.append(Pivot(i, hi, "H", i + k, klines[i].volume))
        elif lo < min(left_l) and lo < min(right_l):
            out.append(Pivot(i, lo, "L", i + k, klines[i].volume))
    return out


def fractal_pivots_values(values: list[float | None], k: int = 2) -> list[tuple[int, float, str]]:
    """Fractal pivots over a raw series (e.g., RSI). Returns (index, value, kind)."""
    if k < 1:
        raise ValueError("k must be >= 1")
    n = len(values)
    out: list[tuple[int, float, str]] = []
    for i in range(k, n - k):
        v = values[i]
        if v is None:
            continue
        window = values[i - k:i] + values[i + 1:i + k + 1]
        if any(x is None for x in window):
            continue
        left = values[i - k:i]
        right = values[i + 1:i + k + 1]
        if not left or not right:
            continue  # defensive: cannot happen with valid k, but never assert-guard logic
        if all(v > x for x in left + right):
            out.append((i, v, "H"))
        elif all(v < x for x in left + right):
            out.append((i, v, "L"))
    return out


def zigzag_pivots(
    klines: list[Kline],
    k_zz: float = 0.6,
    atr_values: list[float | None] | None = None,
    atr_n: int = 14,
) -> list[Pivot]:
    """ATR-adaptive ZigZag. Pivot confirmed_index = bar where reversal crossed."""
    if not klines:
        return []
    if atr_values is None:
        atr_values = _atr(klines, atr_n)

    trend = 1
    hh_price = klines[0].high
    hh_idx = 0
    ll_price = klines[0].low
    ll_idx = 0
    out: list[Pivot] = []

    for i, kl in enumerate(klines):
        if i == 0:
            continue
        a = atr_values[i] if i < len(atr_values) else None
        if a is None or kl.close <= 0:
            continue
        theta = max(k_zz * a / kl.close, 0.0005)
        if trend > 0:
            if kl.high >= hh_price:
                hh_price, hh_idx = kl.high, i
            elif kl.low < hh_price * (1.0 - theta):
                out.append(Pivot(hh_idx, hh_price, "H", i, klines[hh_idx].volume))
                trend = -1
                ll_price, ll_idx = kl.low, i
        else:
            if kl.low <= ll_price:
                ll_price, ll_idx = kl.low, i
            elif kl.high > ll_price * (1.0 + theta):
                out.append(Pivot(ll_idx, ll_price, "L", i, klines[ll_idx].volume))
                trend = 1
                hh_price, hh_idx = kl.high, i
    return out


def pivots_as_of(pivots: list[Pivot], index: int) -> list[Pivot]:
    """Only pivots that were *already confirmed* at bar `index`."""
    if index < 0:
        raise ValueError("index must be >= 0")
    return [p for p in pivots if p.confirmed_index <= index]


def last_swing(pivots: list[Pivot], kind: str, index: int | None = None) -> Pivot | None:
    if kind not in ("H", "L"):
        raise ValueError("kind must be 'H' or 'L'")
    candidates = pivots if index is None else pivots_as_of(pivots, index)
    same = [p for p in candidates if p.kind == kind]
    if not same:
        return None
    return max(same, key=lambda p: (p.confirmed_index, p.index))
