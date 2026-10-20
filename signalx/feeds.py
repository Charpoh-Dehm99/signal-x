"""Async REST feed layer shared by the CLI watcher and the web API.

httpx.AsyncClient based. Same security posture as signalx.scanner and the CLI
watcher: host + path whitelists, params as dicts only, response size cap,
strict numeric parsing, fail loud (FeedError). Never swallows errors.
"""

from __future__ import annotations

import httpx

from .live import live_snapshot
from .validation import ValidationError, _strict_float, parse_kline_row, validate_interval

SPOT_BASE = "https://api.binance.com"
FUTURES_BASE = "https://fapi.binance.com"
BASES = (SPOT_BASE, FUTURES_BASE)
ALLOWED_PATH_PREFIXES = ("/api/v3/", "/fapi/v1/", "/futures/data/")

MAX_RESPONSE_BYTES = 2_000_000
TIMEOUT_S = 15.0
RETRIES = 2

TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
}
OI_PERIODS = {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}
KLINES_LIMIT = 500
FUNDING_HISTORY = 90
OI_HISTORY = 288
SPARK_CLOSES = 120


class FeedError(RuntimeError):
    """Transport/validation failure in the async feed layer. Never swallowed."""


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S), follow_redirects=False)


async def _get_json(client: httpx.AsyncClient, base: str, path: str, params: dict) -> object:
    if base not in BASES:
        raise FeedError(f"base not whitelisted: {base!r}")
    if not isinstance(path, str) or not any(path.startswith(p) for p in ALLOWED_PATH_PREFIXES):
        raise FeedError(f"path not whitelisted: {path!r}")
    if not isinstance(params, dict):
        raise FeedError("params must be a dict")

    last_exc: Exception | None = None
    for _ in range(RETRIES + 1):
        try:
            resp = await client.get(base + path, params=params)
        except httpx.HTTPError as exc:
            last_exc = FeedError(f"request failed: {exc.__class__.__name__}")
            continue
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise FeedError(f"response exceeds {MAX_RESPONSE_BYTES} bytes for {path}")
        if resp.status_code != 200:
            raise FeedError(f"HTTP {resp.status_code} for {path}")
        try:
            return resp.json()
        except ValueError as exc:
            raise FeedError(f"non-JSON response for {path}") from exc
    raise FeedError(f"request failed after retries: {last_exc}")


def _num(value: object, name: str) -> float:
    try:
        return _strict_float(value, name)
    except ValidationError as exc:
        raise FeedError(str(exc)) from exc


def _step(interval: object) -> tuple[str, int]:
    try:
        iv = validate_interval(interval)
    except ValidationError as exc:
        raise FeedError(str(exc)) from exc
    if iv not in TF_MS:
        raise FeedError(f"interval not supported by feeds: {iv!r}")
    return iv, TF_MS[iv]


def drop_incomplete(klines, interval, now_ms):
    """Drop the trailing in-progress candle (partial aggregates)."""
    step = TF_MS[interval]
    if klines and klines[-1].open_time + step > now_ms:
        return klines[:-1]
    return klines


def _base_prefix(market: str) -> tuple[str, str]:
    if market == "spot":
        return SPOT_BASE, "/api/v3"
    if market == "futures":
        return FUTURES_BASE, "/fapi/v1"
    raise FeedError(f"unknown market: {market!r}")


async def fetch_klines(
    client: httpx.AsyncClient,
    market: str,
    symbol: str,
    interval: str,
    now_ms: int,
    *,
    limit: int = KLINES_LIMIT,
    min_closed: int = 50,
    start_ms: int | None = None,
    end_ms: int | None = None,
    include_incomplete: bool = False,
):
    """Closed klines by default. include_incomplete=True keeps the trailing
    in-progress bar (charting only — never feed it to signal decisions)."""
    iv, _ = _step(interval)
    base, prefix = _base_prefix(market)
    params: dict = {"symbol": symbol, "interval": iv, "limit": limit}
    for name, value in (("startTime", start_ms), ("endTime", end_ms)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise FeedError(f"{name} must be a positive int")
        params[name] = value
    rows = await _get_json(client, base, f"{prefix}/klines", params)
    if not isinstance(rows, list) or not rows:
        raise FeedError("empty/malformed klines payload")
    try:
        klines = [parse_kline_row(r, now_ms=now_ms) for r in rows]
    except ValidationError as exc:
        raise FeedError(str(exc)) from exc
    if not include_incomplete:
        klines = drop_incomplete(klines, iv, now_ms)
    if min_closed and len(klines) < min_closed:
        raise FeedError("too few closed klines")
    return klines


async def fetch_ticker(client: httpx.AsyncClient, market: str, symbol: str) -> float:
    base, prefix = _base_prefix(market)
    payload = await _get_json(client, base, f"{prefix}/ticker/24hr", {"symbol": symbol})
    if not isinstance(payload, dict):
        raise FeedError("malformed ticker payload")
    return _num(payload.get("priceChangePercent"), "priceChangePercent")


async def fetch_gainers(
    client: httpx.AsyncClient,
    *,
    top: int = 50,
    min_quote_volume: float = 1_000_000.0,
    with_universe: bool = False,
) -> list[dict] | tuple[list[dict], int]:
    """Top USDT-M futures 24h gainers (bulk ticker; perp universe only).

    With ``with_universe=True`` returns ``(top, universe)`` where *universe*
    is the number of symbols passing the liquidity filter before truncation.
    """
    if not (1 <= top <= 500):
        raise FeedError("top must be in [1, 500]")
    payload = await _get_json(client, FUTURES_BASE, "/fapi/v1/ticker/24hr", {})
    if not isinstance(payload, list) or not payload:
        raise FeedError("malformed bulk ticker payload")
    out = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        sym = item.get("symbol")
        if not isinstance(sym, str) or not sym.endswith("USDT") or "_" in sym:
            continue
        change = item.get("priceChangePercent")
        qv = item.get("quoteVolume")
        try:
            change_f = _strict_float(change, "priceChangePercent")
            qv_f = _strict_float(qv, "quoteVolume")
            price_f = _strict_float(item.get("lastPrice"), "lastPrice")
        except ValidationError:
            continue  # a single malformed row must not poison the list
        if qv_f < min_quote_volume:
            continue
        out.append({
            "symbol": sym,
            "change_pct": change_f,
            "quote_volume": qv_f,
            "last_price": price_f,
        })
    out.sort(key=lambda d: d["change_pct"], reverse=True)
    if with_universe:
        return out[:top], len(out)
    return out[:top]


async def fetch_funding(client: httpx.AsyncClient, symbol: str) -> tuple[float, list[float]]:
    payload = await _get_json(client, FUTURES_BASE, "/fapi/v1/premiumIndex", {"symbol": symbol})
    if not isinstance(payload, dict):
        raise FeedError("malformed premiumIndex payload")
    now = _num(payload.get("lastFundingRate"), "lastFundingRate")
    rows = await _get_json(client, FUTURES_BASE, "/fapi/v1/fundingRate",
                           {"symbol": symbol, "limit": FUNDING_HISTORY})
    if not isinstance(rows, list) or not rows:
        raise FeedError("malformed fundingRate payload")
    hist = [_num(r.get("fundingRate"), "fundingRate") for r in rows]
    return now, hist


async def fetch_open_interest(
    client: httpx.AsyncClient, symbol: str, period: str, *, with_times: bool = False
) -> tuple:
    """Current OI + history. With `with_times`, also returns each sample's ms
    timestamp so callers can align OI to price bars of any timeframe."""
    payload = await _get_json(client, FUTURES_BASE, "/fapi/v1/openInterest", {"symbol": symbol})
    if not isinstance(payload, dict):
        raise FeedError("malformed openInterest payload")
    oi_now = _num(payload.get("openInterest"), "openInterest")
    rows = await _get_json(client, FUTURES_BASE, "/futures/data/openInterestHist",
                           {"symbol": symbol, "period": period, "limit": OI_HISTORY})
    if not isinstance(rows, list) or len(rows) < 2:
        raise FeedError("malformed openInterestHist payload")
    hist = [_num(r.get("sumOpenInterest"), "sumOpenInterest") for r in rows]
    if not with_times:
        return oi_now, hist
    times: list[int] = []
    for r in rows:
        ts = r.get("timestamp")
        if isinstance(ts, bool) or not isinstance(ts, int) or ts <= 0:
            raise FeedError("malformed openInterestHist timestamp")
        times.append(ts)
    return oi_now, hist, times


async def detect_market(client: httpx.AsyncClient, symbol: str) -> str:
    try:
        await fetch_ticker(client, "futures", symbol)
        return "futures"
    except FeedError:
        await fetch_ticker(client, "spot", symbol)
        return "spot"


async def poll_snapshot(
    client: httpx.AsyncClient,
    symbol: str,
    market: str,
    tf: str,
    now_ms: int,
    *,
    spark_closes: int = SPARK_CLOSES,
) -> dict:
    """One full observation snapshot (same shape as signalx.live.live_snapshot).

    Adds `closes` + `closes_time` (recent tf closes for charting) and `market`.
    """
    klines = await fetch_klines(client, market, symbol, tf, now_ms)
    hourly = await fetch_klines(client, market, symbol, "1h", now_ms)
    change = await fetch_ticker(client, market, symbol)

    funding_now = funding_hist = oi_now = oi_hist = oi_times = None
    if market == "futures":
        funding_now, funding_hist = await fetch_funding(client, symbol)
        oi_now, oi_hist, oi_times = await fetch_open_interest(
            client, symbol, tf if tf in OI_PERIODS else "5m", with_times=True
        )

    snap = live_snapshot(
        symbol=symbol,
        klines5m=klines,
        hourly_qv=[k.quote_volume for k in hourly],
        change_24h_pct=change,
        funding_now=funding_now,
        funding_history=funding_hist,
        oi_now=oi_now,
        oi_history=oi_hist,
        oi_times=oi_times,
    )
    snap["bar_time"] = klines[-1].open_time
    tail = klines[-spark_closes:]
    snap["market"] = market
    snap["tf"] = tf
    snap["closes"] = [k.close for k in tail]
    snap["closes_time"] = [k.open_time for k in tail]
    return snap
