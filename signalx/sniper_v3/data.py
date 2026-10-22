"""Binance futures adapter for the SNIPER V3 engine.

REST only for the first slice. The sentinel's existing 5m stream is untouched;
this module builds a fresh 1H/15m/5m snapshot per evaluation.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from signalx.feeds import FeedError, fetch_funding, fetch_klines, fetch_open_interest
from signalx.indicators import pct_rank
from signalx.models import Kline

from .engine import SniperConfig, SniperEngine
from .models import SniperDecision, SniperDiagnostics


@dataclass(frozen=True)
class SniperInputs:
    symbol: str
    klines_1h: list[Kline]
    klines_15m: list[Kline]
    klines_5m: list[Kline]
    funding_rate: float | None = None
    funding_history: list[float] | None = None
    oi_now: float | None = None
    oi_history: list[float] | None = None
    oi_times: list[int] | None = None


async def _optional(coro):
    try:
        return await coro
    except FeedError:
        return None


async def fetch_sniper_inputs(
    client: httpx.AsyncClient,
    symbol: str,
    *,
    now_ms: int | None = None,
    limit: int = 500,
) -> SniperInputs:
    """Fetch closed 1H/15m/5m futures candles + optional derivatives context."""
    sym = symbol.upper()
    now = int(time.time() * 1000) if now_ms is None else now_ms
    k1h, k15, k5 = await asyncio.gather(
        fetch_klines(client, "futures", sym, "1h", now, limit=limit, min_closed=50),
        fetch_klines(client, "futures", sym, "15m", now, limit=limit, min_closed=50),
        fetch_klines(client, "futures", sym, "5m", now, limit=limit, min_closed=50),
    )
    funding = await _optional(fetch_funding(client, sym))
    oi = await _optional(fetch_open_interest(client, sym, "15m", with_times=True))
    if funding is None:
        funding_rate, funding_history = None, None
    else:
        funding_rate, funding_history = funding
    if oi is None:
        oi_now, oi_history, oi_times = None, None, None
    else:
        oi_now, oi_history, oi_times = oi
    return SniperInputs(
        symbol=sym,
        klines_1h=k1h,
        klines_15m=k15,
        klines_5m=k5,
        funding_rate=funding_rate,
        funding_history=funding_history,
        oi_now=oi_now,
        oi_history=oi_history,
        oi_times=oi_times,
    )


def funding_percentile(inputs: SniperInputs) -> float | None:
    if inputs.funding_rate is None or not inputs.funding_history:
        return None
    return pct_rank(inputs.funding_history, inputs.funding_rate)


def oi_crowding(inputs: SniperInputs) -> float | None:
    """A bounded crowding prior, not a validated OI signal.

    Penalizes a 24h OI expansion that coincides with an already-extended 24h
    price move. Returns 0..1, or None when data is unavailable.
    """
    oi = inputs.oi_history
    one_h = inputs.klines_1h
    if not oi or len(oi) < 48 or len(one_h) < 30:
        return None
    lookback = min(len(oi) - 1, 96)
    base = oi[-lookback - 1]
    if base <= 0:
        return None
    oi_change = oi[-1] / base - 1.0
    price_ref = one_h[-25].close
    if price_ref <= 0:
        return None
    price_change = one_h[-1].close / price_ref - 1.0
    oi_component = max(0.0, min(1.0, (abs(oi_change) - 0.15) / 0.35))
    price_component = max(0.0, min(1.0, abs(price_change) / 0.10))
    return min(1.0, oi_component * price_component)


def evaluate_inputs_detailed(
    inputs: SniperInputs,
    *,
    config: SniperConfig | None = None,
) -> tuple[SniperDecision | None, SniperDiagnostics]:
    engine = SniperEngine(inputs.symbol, config=config)
    return engine.evaluate_detailed(
        inputs.klines_1h,
        inputs.klines_15m,
        inputs.klines_5m,
        funding_pctile=funding_percentile(inputs),
        oi_crowding=oi_crowding(inputs),
    )


def evaluate_inputs(
    inputs: SniperInputs,
    *,
    config: SniperConfig | None = None,
) -> SniperDecision | None:
    decision, _ = evaluate_inputs_detailed(inputs, config=config)
    return decision


async def fetch_and_evaluate_detailed(
    client: httpx.AsyncClient,
    symbol: str,
    *,
    config: SniperConfig | None = None,
    now_ms: int | None = None,
) -> tuple[SniperInputs, SniperDecision | None, SniperDiagnostics]:
    inputs = await fetch_sniper_inputs(client, symbol, now_ms=now_ms)
    decision, diagnostics = evaluate_inputs_detailed(inputs, config=config)
    return inputs, decision, diagnostics


async def fetch_and_evaluate(
    client: httpx.AsyncClient,
    symbol: str,
    *,
    config: SniperConfig | None = None,
    now_ms: int | None = None,
) -> tuple[SniperInputs, SniperDecision | None]:
    inputs, decision, _ = await fetch_and_evaluate_detailed(
        client, symbol, config=config, now_ms=now_ms
    )
    return inputs, decision
