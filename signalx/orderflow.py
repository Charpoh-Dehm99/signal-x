"""Order-flow / leverage analytics: CVD, OI matrix, funding, squeeze guard.

Fail-closed policy: when inputs are unknown (None), the squeeze guard
returns True (block trading) rather than allowing a possibly-suicidal short.
"""

from __future__ import annotations

from .models import AggTrade, Kline
from .validation import ValidationError, taker_direction


def cvd_from_klines(klines: list[Kline]) -> list[float]:
    """Cumulative volume delta using taker-buy split embedded in klines."""
    total = 0.0
    out: list[float] = []
    for k in klines:
        sell_vol = k.volume - k.taker_buy_base
        delta = k.taker_buy_base - sell_vol
        total += delta
        out.append(total)
    return out


def cvd_from_agg_trades(trades: list[AggTrade]) -> list[float]:
    """CVD from agg trades; aggressor side derived from is_buyer_maker."""
    total = 0.0
    out: list[float] = []
    for t in trades:
        total += taker_direction(t.is_buyer_maker) * t.qty
        out.append(total)
    return out


def oi_price_regime(
    delta_oi_pct: float | None,
    delta_price_pct: float | None,
    at_highs: bool,
    *,
    flat_pct: float = 0.05,
) -> str:
    """Classify the OI/price matrix.

    TREND_FUEL : OI up + price up          -> do not short yet
    ABSORPTION : OI up + price flat at highs -> late longs trapped (armed)
    FLUSH      : OI down + price down      -> long liquidation cascade
    NEUTRAL    : anything else
    """
    if delta_oi_pct is None or delta_price_pct is None:
        raise ValidationError("missing OI/price deltas")
    if flat_pct < 0:
        raise ValueError("flat_pct must be >= 0")

    if delta_oi_pct > 0:
        if delta_price_pct > flat_pct:
            return "TREND_FUEL"
        if abs(delta_price_pct) <= flat_pct and at_highs:
            return "ABSORPTION"
        return "NEUTRAL"
    if delta_oi_pct < 0 and delta_price_pct < -flat_pct:
        return "FLUSH"
    return "NEUTRAL"


def squeeze_guard(
    oi_rising: bool | None,
    funding_pctile: float | None,
    price_at_new_highs: bool | None,
    *,
    funding_threshold: float = 90.0,
) -> bool:
    """True = BLOCK shorts (default when any input is unknown: fail closed)."""
    if oi_rising is None or funding_pctile is None or price_at_new_highs is None:
        return True
    return bool(oi_rising and funding_pctile > funding_threshold and price_at_new_highs)
