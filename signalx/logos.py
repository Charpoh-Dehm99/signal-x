"""Coin icon URLs from public sources: CoinGecko markets/search + Binance CDN.

Cosmetic layer only: the dashboard keeps letter-avatar fallbacks for symbols
that are missing here and for whenever an upstream is unreachable. Nothing in
this module feeds signal or observation decisions.
"""

from __future__ import annotations

import re

import httpx

ASCII_SYM_RE = re.compile(r"[A-Z0-9]{1,20}")

MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
SEARCH_URL = "https://api.coingecko.com/api/v3/search"
STATIC_LOGO_URL = "https://bin.bnbstatic.com/static/assets/logos/{}.png"
MAX_RESPONSE_BYTES = 3_000_000
RETRIES = 1  # transport errors only, same posture as signalx.feeds
PER_PAGE = 250
PAGES = 4  # top ~1000 by market cap
HEADERS = {"accept": "application/json", "user-agent": "signal-x-dashboard/1.0"}
_UNRANKED = 10**9


class LogoError(RuntimeError):
    """Upstream fetch/parse failure. Cosmetic layer only — callers degrade."""


async def _get_json(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    *,
    what: str,
) -> object:
    last_exc: LogoError | None = None
    for _ in range(RETRIES + 1):
        try:
            resp = await client.get(url, params=params, headers=HEADERS)
        except httpx.HTTPError as exc:
            last_exc = LogoError(f"request failed: {exc.__class__.__name__}")
            continue
        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise LogoError(f"response exceeds {MAX_RESPONSE_BYTES} bytes for {what}")
        if resp.status_code != 200:
            raise LogoError(f"HTTP {resp.status_code} for {what}")
        try:
            return resp.json()
        except ValueError as exc:
            raise LogoError(f"non-JSON response for {what}") from exc
    raise last_exc or LogoError("request failed")


async def _fetch_page(client: httpx.AsyncClient, page: int, per_page: int) -> list:
    data = await _get_json(
        client,
        MARKETS_URL,
        {
            "vs_currency": "usd",
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "sparkline": "false",
        },
        what=f"markets page {page}",
    )
    if not isinstance(data, list):
        raise LogoError(f"malformed markets payload for page {page}")
    return data


async def fetch_coin_logos(
    client: httpx.AsyncClient,
    *,
    pages: int = PAGES,
    per_page: int = PER_PAGE,
) -> dict[str, str]:
    """Upper-cased symbol -> icon URL, ranked by market cap (first wins)."""
    if not isinstance(pages, int) or pages < 1:
        raise LogoError("pages must be a positive int")
    if not isinstance(per_page, int) or not (1 <= per_page <= 250):
        raise LogoError("per_page must be in [1, 250]")

    out: dict[str, str] = {}
    for page in range(1, pages + 1):
        try:
            rows = await _fetch_page(client, page=page, per_page=per_page)
        except LogoError:
            if page == 1:
                raise
            break  # keep the partial map; icons are cosmetic
        for row in rows:
            if not isinstance(row, dict):
                continue
            sym = row.get("symbol")
            img = row.get("image")
            if not isinstance(sym, str) or not sym:
                continue
            if not isinstance(img, str) or not img.startswith("https://"):
                continue
            out.setdefault(sym.upper(), img)
        if len(rows) < per_page:
            break  # end of the ranked universe
    return out


async def search_coin_logo(client: httpx.AsyncClient, symbol: str) -> str | None:
    """Exact-ticker CoinGecko search; best-ranked match or None when absent."""
    data = await _get_json(client, SEARCH_URL, {"query": symbol.lower()}, what=f"search {symbol!r}")
    coins = data.get("coins") if isinstance(data, dict) else None
    if not isinstance(coins, list):
        raise LogoError(f"malformed search payload for {symbol!r}")
    matches: list[tuple[int, str]] = []
    for coin in coins:
        if not isinstance(coin, dict):
            continue
        sym = coin.get("symbol")
        if not isinstance(sym, str) or sym.upper() != symbol.upper():
            continue
        img = coin.get("large") or coin.get("small") or coin.get("thumb")
        if not isinstance(img, str) or not img.startswith("https://"):
            continue
        rank = coin.get("market_cap_rank")
        matches.append((rank if isinstance(rank, int) else _UNRANKED, img))
    if not matches:
        return None
    matches.sort(key=lambda m: m[0])
    return matches[0][1]


async def probe_static_logo(client: httpx.AsyncClient, symbol: str) -> str | None:
    """Binance CDN icon (present for most Binance-listed assets); None if absent."""
    url = STATIC_LOGO_URL.format(symbol.upper())
    last_exc: LogoError | None = None
    for _ in range(RETRIES + 1):
        try:
            resp = await client.get(url)
        except httpx.HTTPError as exc:
            last_exc = LogoError(f"request failed: {exc.__class__.__name__}")
            continue
        return url if resp.status_code == 200 else None
    raise last_exc or LogoError("request failed")


async def resolve_coin_logo(client: httpx.AsyncClient, symbol: str) -> str | None:
    """Best-effort icon for a base asset: CoinGecko search, then Binance CDN.

    None means *definitively* absent from both sources (safe to negatively
    cache); LogoError means a transient upstream problem (retry later).
    """
    sym = symbol.upper()
    if not ASCII_SYM_RE.fullmatch(sym):
        raise LogoError(f"invalid symbol: {symbol!r}")
    url = await search_coin_logo(client, sym)
    if url:
        return url
    return await probe_static_logo(client, sym)
