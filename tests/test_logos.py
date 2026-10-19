"""Tests for signalx.logos: CoinGecko icon map against an httpx MockTransport."""

from __future__ import annotations

import httpx
import pytest

from signalx.logos import (
    SEARCH_URL,
    STATIC_LOGO_URL,
    LogoError,
    fetch_coin_logos,
    probe_static_logo,
    resolve_coin_logo,
    search_coin_logo,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def mk_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


def market_row(sym: str, img: object) -> dict:
    return {"symbol": sym, "image": img}


def paged_handler(pages: dict[int, list], calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(int(request.url.params["page"]))
        page = int(request.url.params["page"])
        if page not in pages:
            return httpx.Response(500, json={"error": f"unrouted page {page}"})
        return httpx.Response(200, json=pages[page])
    return handler


class TestFetchCoinLogos:
    @pytest.mark.anyio
    async def test_upper_symbol_map_first_wins_and_stops_early(self):
        seen_params: list[dict] = []
        page1 = [market_row(f"sym{i}", f"https://cdn.example/{i}.png") for i in range(250)]
        page1[0] = market_row("btc", "https://cdn.example/btc.png")
        page2 = [
            market_row("BTC", "https://cdn.example/btc-lower-cap.png"),  # dup: first wins
            market_row("eth", "https://cdn.example/eth.png"),
            market_row("sol", "https://cdn.example/sol.png"),
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            seen_params.append(dict(request.url.params))
            page = int(request.url.params["page"])
            return httpx.Response(200, json={1: page1, 2: page2}.get(page, []))

        async with mk_client(handler) as client:
            out = await fetch_coin_logos(client)
        assert out["BTC"] == "https://cdn.example/btc.png"
        assert out["ETH"] == "https://cdn.example/eth.png"
        assert out["SYM249"] == "https://cdn.example/249.png"
        assert seen_params[0]["vs_currency"] == "usd"
        assert seen_params[0]["order"] == "market_cap_desc"
        assert seen_params[0]["sparkline"] == "false"
        assert [p["page"] for p in seen_params] == ["1", "2"]  # short page 2 stopped it

    @pytest.mark.anyio
    async def test_first_page_failure_raises(self):
        async with mk_client(lambda req: httpx.Response(500, json={})) as client:
            with pytest.raises(LogoError):
                await fetch_coin_logos(client)

    @pytest.mark.anyio
    async def test_later_page_failure_keeps_partial_map(self):
        calls: list[int] = []
        page1 = [market_row(f"sym{i}", f"https://cdn.example/{i}.png") for i in range(250)]
        handler = paged_handler({1: page1}, calls=calls)
        async with mk_client(handler) as client:
            out = await fetch_coin_logos(client)
        assert len(out) == 250
        assert calls == [1, 2]  # non-200 raises without retry; map kept from page 1

    @pytest.mark.anyio
    async def test_transport_error_retried_once(self):
        state = {"n": 0}
        rows = [market_row("aaa", "https://cdn.example/a.png")]

        def handler(request: httpx.Request) -> httpx.Response:
            state["n"] += 1
            if state["n"] == 1:
                raise httpx.ConnectError("boom", request=request)
            return httpx.Response(200, json=rows)

        async with mk_client(handler) as client:
            out = await fetch_coin_logos(client)
        assert out == {"AAA": "https://cdn.example/a.png"}
        assert state["n"] == 2

    @pytest.mark.anyio
    async def test_malformed_rows_skipped(self):
        rows = [
            market_row("ok", "https://cdn.example/ok.png"),
            {"symbol": "noimg"},
            market_row("http", "http://cdn.example/insecure.png"),
            {"symbol": "imgnotstr", "image": 123},
            {"image": "https://cdn.example/nosym.png"},
            "not-a-dict",
        ]
        async with mk_client(lambda req: httpx.Response(200, json=rows)) as client:
            out = await fetch_coin_logos(client)
        assert out == {"OK": "https://cdn.example/ok.png"}

    @pytest.mark.anyio
    async def test_non_list_payload_raises(self):
        async with mk_client(lambda req: httpx.Response(200, json={"not": "a list"})) as client:
            with pytest.raises(LogoError):
                await fetch_coin_logos(client)

    @pytest.mark.anyio
    async def test_bounds_validated(self):
        async with mk_client(lambda req: httpx.Response(200, json=[])) as client:
            with pytest.raises(LogoError):
                await fetch_coin_logos(client, pages=0)
            with pytest.raises(LogoError):
                await fetch_coin_logos(client, per_page=251)


def search_payload(coins: list) -> httpx.Response:
    return httpx.Response(200, json={"coins": coins})


class TestSearchCoinLogo:
    @pytest.mark.anyio
    async def test_exact_ticker_best_rank_wins(self):
        coins = [
            {"symbol": "ARIA", "market_cap_rank": None, "large": "https://cdn.example/aria-low.png"},
            {"symbol": "ARIA", "market_cap_rank": 1356, "large": "https://cdn.example/aria.png"},
            {"symbol": "ARI", "market_cap_rank": 5, "large": "https://cdn.example/ari.png"},
        ]

        async with mk_client(lambda req: search_payload(coins)) as client:
            url = await search_coin_logo(client, "ARIA")
        assert url == "https://cdn.example/aria.png"

    @pytest.mark.anyio
    async def test_lowercase_ticker_and_image_fallbacks(self):
        coins = [{"symbol": "soxs", "market_cap_rank": 10145, "thumb": "https://cdn.example/soxs.png"}]

        async with mk_client(lambda req: search_payload(coins)) as client:
            url = await search_coin_logo(client, "SOXS")
        assert url == "https://cdn.example/soxs.png"

    @pytest.mark.anyio
    async def test_no_exact_match_is_none(self):
        coins = [{"symbol": "PROS", "market_cap_rank": 302, "large": "https://cdn.example/pros.png"}]

        async with mk_client(lambda req: search_payload(coins)) as client:
            assert await search_coin_logo(client, "PHAROS") is None

    @pytest.mark.anyio
    async def test_sends_lowercase_query_and_raises_on_bad_payload(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v3/search"
            seen.append(request.url.params["query"])
            return httpx.Response(200, json={"not": "coins"})

        async with mk_client(handler) as client:
            with pytest.raises(LogoError):
                await search_coin_logo(client, "MSTR")
        assert seen == ["mstr"]

    @pytest.mark.anyio
    async def test_non_200_raises(self):
        async with mk_client(lambda req: httpx.Response(429, json={})) as client:
            with pytest.raises(LogoError):
                await search_coin_logo(client, "BTC")


class TestProbeStaticLogo:
    @pytest.mark.anyio
    async def test_present_returns_url(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            return httpx.Response(200, content=b"png")

        async with mk_client(handler) as client:
            url = await probe_static_logo(client, "pharos")
        assert url == STATIC_LOGO_URL.format("PHAROS")
        assert seen == ["/static/assets/logos/PHAROS.png"]

    @pytest.mark.anyio
    async def test_missing_key_is_none(self):
        async with mk_client(lambda req: httpx.Response(403, content=b"denied")) as client:
            assert await probe_static_logo(client, "BTCDOM") is None


class TestResolveCoinLogo:
    @pytest.mark.anyio
    async def test_search_hit_skips_static_probe(self):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/api/v3/search":
                return search_payload([{"symbol": "BNC", "market_cap_rank": 3969, "large": "https://cdn.example/bnc.png"}])
            return httpx.Response(403, content=b"denied")

        async with mk_client(handler) as client:
            url = await resolve_coin_logo(client, "BNC")
        assert url == "https://cdn.example/bnc.png"
        assert paths == ["/api/v3/search"]

    @pytest.mark.anyio
    async def test_search_miss_falls_back_to_static(self):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if request.url.path == "/api/v3/search":
                return search_payload([])
            return httpx.Response(200, content=b"png")

        async with mk_client(handler) as client:
            url = await resolve_coin_logo(client, "era")
        assert url == STATIC_LOGO_URL.format("ERA")
        assert paths == ["/api/v3/search", "/static/assets/logos/ERA.png"]

    @pytest.mark.anyio
    async def test_invalid_symbol_rejected_without_requests(self):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(200, json={})

        async with mk_client(handler) as client:
            with pytest.raises(LogoError):
                await resolve_coin_logo(client, "NOT A SYMBOL")
        assert paths == []
