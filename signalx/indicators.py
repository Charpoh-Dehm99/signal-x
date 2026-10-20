"""Technical indicators. Pure functions, no state, no I/O.

All functions accept plain lists to stay testable and replayable; the live
pipeline feeds them from ring buffers on closed bars only.
"""

from __future__ import annotations

import math

from .models import Kline


def sma(values: list[float], n: int) -> list[float | None]:
    if n <= 0:
        raise ValueError("n must be positive")
    out: list[float | None] = []
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        out.append(total / n if i >= n - 1 else None)
    return out


def wilder_rma(values: list[float], n: int) -> list[float | None]:
    """Wilder's smoothing (alpha = 1/n), seeded with an SMA."""
    if n <= 0:
        raise ValueError("n must be positive")
    out: list[float | None] = []
    prev = 0.0
    for i, v in enumerate(values):
        if i < n - 1:
            out.append(None)
            continue
        if i == n - 1:
            prev = sum(values[:n]) / n
        else:
            prev = (prev * (n - 1) + v) / n
        out.append(prev)
    return out


def rsi(closes: list[float], n: int = 14) -> list[float | None]:
    """Wilder RSI. Returns None until enough data.

    Canonical warmup: the first value is published at close index `n` (after
    `n` real deltas), matching TradingView/TA-Lib. The dummy delta at index 0
    is not part of the seed.
    """
    if n <= 0:
        raise ValueError("n must be positive")
    if len(closes) < n + 1:
        return [None] * len(closes)

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))

    # deltas[i] belongs to close index i+1, so prepend one None to realign.
    avg_gain = [None] + wilder_rma(gains, n)
    avg_loss = [None] + wilder_rma(losses, n)

    out: list[float | None] = []
    for g, loss in zip(avg_gain, avg_loss):
        if g is None or loss is None:
            out.append(None)
        elif loss == 0:
            out.append(100.0)
        else:
            rs = g / loss
            out.append(100.0 - 100.0 / (1.0 + rs))
    return out


def true_range(klines: list[Kline]) -> list[float]:
    out: list[float] = []
    for i, k in enumerate(klines):
        if i == 0:
            out.append(k.high - k.low)
            continue
        prev_close = klines[i - 1].close
        out.append(max(k.high - k.low, abs(k.high - prev_close), abs(k.low - prev_close)))
    return out


def atr(klines: list[Kline], n: int = 14) -> list[float | None]:
    """Wilder ATR. The first bar has no previous close, so the canonical
    seed uses TR[1..n] and the first value is published at bar index `n`."""
    tr = true_range(klines)
    return [None] + wilder_rma(tr[1:], n)


def roc(values: list[float], n: int) -> list[float | None]:
    if n <= 0:
        raise ValueError("n must be positive")
    out: list[float | None] = [None] * len(values)
    for i in range(n, len(values)):
        prev = values[i - n]
        if prev != 0:
            out[i] = (values[i] - prev) / prev
    return out


def avwap(klines: list[Kline], start: int = 0) -> list[tuple[float, float]]:
    """Anchored VWAP from `start`, returns (vwap, sigma) per bar from start."""
    if start < 0 or start >= len(klines):
        raise ValueError("start out of range")
    cum_pv = 0.0
    cum_v = 0.0
    cum_p2v = 0.0
    out: list[tuple[float, float]] = []
    for k in klines[start:]:
        typ = (k.high + k.low + k.close) / 3.0
        cum_pv += typ * k.volume
        cum_v += k.volume
        cum_p2v += typ * typ * k.volume
        if cum_v > 0:
            vwap = cum_pv / cum_v
            var = max(cum_p2v / cum_v - vwap * vwap, 0.0)
            out.append((vwap, math.sqrt(var)))
        else:
            out.append((typ, 0.0))
    return out


def pct_rank(values: list[float | None], x: float) -> float | None:
    """Percentile rank of x among non-None values (0..100)."""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return 100.0 * sum(1 for v in clean if v <= x) / len(clean)


def stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    var = sum((v - mean) ** 2 for v in values) / len(values)
    return math.sqrt(var)
