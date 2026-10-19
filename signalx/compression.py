"""Plateau / compression detection: box tightness relative to ATR."""

from __future__ import annotations

from .models import Kline, Zone


def compression_metrics(
    klines: list[Kline],
    window: int,
    atr_value: float,
    *,
    max_height_atr: float = 2.0,
    min_containment: float = 0.6,
) -> dict:
    """Measure how compressed the last `window` bars are.

    height_atr   : box height in ATR units (lower = tighter)
    containment  : share of closes inside the central 90% of the box
    score        : 0.6 * tightness + 0.4 * containment (0..1)
    qualifies    : height_atr <= max_height_atr AND containment >= min_containment
    """
    if window < 3:
        raise ValueError("window must be >= 3")
    if len(klines) < window:
        raise ValueError("not enough bars for window")
    if atr_value is None or atr_value <= 0:
        raise ValueError("atr_value must be positive")

    chunk = klines[-window:]
    hi = max(k.high for k in chunk)
    lo = min(k.low for k in chunk)
    height = hi - lo
    height_atr = height / atr_value

    band_lo = lo + 0.05 * height
    band_hi = hi - 0.05 * height
    inside = sum(1 for k in chunk if band_lo <= k.close <= band_hi)
    containment = inside / len(chunk)

    height_score = max(0.0, 1.0 - min(height_atr / max_height_atr, 1.0))
    score = 0.6 * height_score + 0.4 * containment

    return {
        "height": height,
        "height_atr": height_atr,
        "containment": containment,
        "score": score,
        "qualifies": bool(height_atr <= max_height_atr and containment >= min_containment),
        "box": Zone(lo, hi, 0.0, len(chunk)),
    }


def momentum_decaying(roc_values: list[float | None], lookback: int = 3) -> bool:
    """True when recent ROC values are strictly decreasing (deceleration)."""
    if lookback < 2:
        raise ValueError("lookback must be >= 2")
    tail = [v for v in roc_values[-(lookback + 1):] if v is not None]
    if len(tail) < lookback + 1:
        return False
    return all(tail[i] > tail[i + 1] for i in range(len(tail) - 1))
