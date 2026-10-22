"""Tests for signalx.feeds: async REST layer against an httpx MockTransport."""

from __future__ import annotations

import pytest

import httpx

from signalx.book import DepthSnapshot
from signalx.feeds import (
    FUTURES_BASE,
    MAX_RESPONSE_BYTES,
    SPOT_BASE,
    FeedError,
    _get_json,
    detect_market,
    fetch_agg_trades,
    fetch_depth,
    fetch_funding,
    fetch_gainers,
    fetch_klines,
    fetch_open_interest,
    fetch_price,
    fetch_ticker,
    poll_snapshot,
)

NOW = 1_700_000_100_000  # aligned to a 5m boundary + one bucket


@pytest.fixture
def anyio_backend():
    return "asyncio"


def kline_rows(n: int, step_ms: int, end_open_ms: int):
    rows = []
    for i in range(n):
        ot = end_open_ms - (n - 1 - i) * step_ms
        c = 100.0 + i * 0.1
        rows.append([ot, c - 0.05, c + 0.1, c - 0.1, c, 10.0, ot + step_ms - 1,
                     c * 10.0, 5, 5.0, c * 5.0])
    return rows


BULK_TICKER = [
    {"symbol": "AAAUSDT", "priceChangePercent": "30.5", "quoteVolume": "5000000", "lastPrice": "1.5"},
    {"symbol": "BBBUSDT", "priceChangePercent": "10.0", "quoteVolume": "2000000", "lastPrice": "2.5"},
    {"symbol": "ETHBTC", "priceChangePercent": "99.0", "quoteVolume": "9000000", "lastPrice": "0.05"},
    {"symbol": "OLDUSDT_240628", "priceChangePercent": "80.0", "quoteVolume": "9000000", "lastPrice": "1"},
    {"symbol": "BADUSDT", "priceChangePercent": "oops", "quoteVolume": "9000000", "lastPrice": "1"},
    {"symbol": "TINYUSDT", "priceChangePercent": "50.0", "quoteVolume": "100", "lastPrice": "1"},
]


def default_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    q = dict(request.url.params)
    if path in ("/fapi/v1/klines", "/api/v3/klines"):
        step = 300_000 if q.get("interval", "5m") == "5m" else 3_600_000
        return httpx.Response(200, json=kline_rows(60, step, NOW))
    if path in ("/fapi/v1/ticker/24hr", "/api/v3/ticker/24hr") and "symbol" not in q:
        return httpx.Response(200, json=BULK_TICKER)
    if path in ("/fapi/v1/ticker/24hr", "/api/v3/ticker/24hr"):
        return httpx.Response(200, json={"symbol": q["symbol"], "priceChangePercent": "12.5"})
    if path in ("/fapi/v1/ticker/price", "/api/v3/ticker/price"):
        return httpx.Response(200, json={"symbol": q["symbol"], "price": "123.45"})
    if path == "/fapi/v1/premiumIndex":
        return httpx.Response(200, json={"lastFundingRate": "0.0001"})
    if path == "/fapi/v1/fundingRate":
        return httpx.Response(200, json=[{"fundingRate": "0.0001"}] * 90)
    if path == "/fapi/v1/openInterest":
        return httpx.Response(200, json={"openInterest": "12345"})
    if path == "/futures/data/openInterestHist":
        return httpx.Response(200, json=[
            {"sumOpenInterest": str(100 + i), "timestamp": NOW - (287 - i) * 300_000}
            for i in range(288)
        ])
    if path in ("/fapi/v1/aggTrades", "/api/v3/aggTrades"):
        return httpx.Response(200, json=[
            {"T": NOW - 3_000 + i, "p": "10.0", "q": "3000.0" if i == 0 else "1.0", "m": False}
            for i in range(3)
        ])
    if path in ("/fapi/v1/depth", "/api/v3/depth"):
        return httpx.Response(200, json={
            "T": NOW,
            "bids": [["100", "5"], ["99", "1"]],
            "asks": [["101", "1"]],
        })
    return httpx.Response(404, json={"msg": "unrouted"})


def mk_client(handler=default_handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)


# ---------------------------------------------------------------- _get_json


class TestGetJson:
    @pytest.mark.anyio
    async def test_bad_base_rejected(self):
        async with mk_client() as client:
            with pytest.raises(FeedError):
                await _get_json(client, "https://evil.example", "/fapi/v1/klines", {})

    @pytest.mark.anyio
    async def test_bad_path_rejected(self):
        async with mk_client() as client:
            with pytest.raises(FeedError):
                await _get_json(client, FUTURES_BASE, "/admin/keys", {})

    @pytest.mark.anyio
    async def test_params_must_be_dict(self):
        async with mk_client() as client:
            with pytest.raises(FeedError):
                await _get_json(client, FUTURES_BASE, "/fapi/v1/klines", ["symbol"])  # type: ignore[arg-type]

    @pytest.mark.anyio
    async def test_http_error(self):
        handler = lambda r: httpx.Response(400, json={"msg": "bad"})  # noqa: E731
        async with mk_client(handler) as client:
            with pytest.raises(FeedError):
                await _get_json(client, FUTURES_BASE, "/fapi/v1/klines", {})

    @pytest.mark.anyio
    async def test_non_json(self):
        handler = lambda r: httpx.Response(200, text="<html>nope</html>")  # noqa: E731
        async with mk_client(handler) as client:
            with pytest.raises(FeedError):
                await _get_json(client, FUTURES_BASE, "/fapi/v1/klines", {})

    @pytest.mark.anyio
    async def test_oversized(self):
        handler = lambda r: httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1))  # noqa: E731
        async with mk_client(handler) as client:
            with pytest.raises(FeedError):
                await _get_json(client, FUTURES_BASE, "/fapi/v1/klines", {})


# ------------------------------------------------------------------ fetches


class TestFetches:
    @pytest.mark.anyio
    async def test_fetch_klines_parses_and_drops_incomplete(self):
        async with mk_client() as client:
            klines = await fetch_klines(client, "futures", "AAAUSDT", "5m", NOW)
        # 60 served, last one starts at NOW -> incomplete -> dropped
        assert len(klines) == 59
        assert klines[-1].open_time == NOW - 300_000
        assert klines[-1].close == pytest.approx(100.0 + 58 * 0.1)

    @pytest.mark.anyio
    async def test_fetch_klines_include_incomplete_keeps_live_bar(self):
        async with mk_client() as client:
            klines = await fetch_klines(
                client, "futures", "AAAUSDT", "5m", NOW, include_incomplete=True
            )
        assert len(klines) == 60
        assert klines[-1].open_time == NOW  # in-progress candle kept for charting

    @pytest.mark.anyio
    async def test_fetch_klines_too_few_closed(self):
        async with mk_client() as client:
            with pytest.raises(FeedError):
                await fetch_klines(client, "futures", "AAAUSDT", "5m", NOW, min_closed=1000)

    @pytest.mark.anyio
    async def test_fetch_klines_bad_interval(self):
        async with mk_client() as client:
            with pytest.raises(FeedError):
                await fetch_klines(client, "futures", "AAAUSDT", "7m", NOW)

    @pytest.mark.anyio
    async def test_fetch_price_valid_and_hostile(self):
        async with mk_client() as client:
            assert await fetch_price(client, "futures", "AAAUSDT") == pytest.approx(123.45)
        async with mk_client() as client:
            with pytest.raises(FeedError):
                await fetch_price(client, "futures", "../etc/passwd")

    @pytest.mark.anyio
    async def test_fetch_ticker_valid_and_hostile(self):
        async with mk_client() as client:
            assert await fetch_ticker(client, "futures", "AAAUSDT") == pytest.approx(12.5)
        handler = lambda r: httpx.Response(200, json={"priceChangePercent": True})  # noqa: E731
        async with mk_client(handler) as client:
            with pytest.raises(FeedError):
                await fetch_ticker(client, "futures", "AAAUSDT")

    @pytest.mark.anyio
    async def test_fetch_gainers_filters_and_sorts(self):
        async with mk_client() as client:
            gainers = await fetch_gainers(client, top=10, min_quote_volume=1_000_000.0)
        assert [g["symbol"] for g in gainers] == ["AAAUSDT", "BBBUSDT"]

    @pytest.mark.anyio
    async def test_fetch_gainers_with_universe(self):
        async with mk_client() as client:
            rows, universe = await fetch_gainers(
                client, top=1, min_quote_volume=1_000_000.0, with_universe=True
            )
        assert [g["symbol"] for g in rows] == ["AAAUSDT"]
        assert universe == 2

    @pytest.mark.anyio
    async def test_fetch_gainers_top_bounds(self):
        async with mk_client() as client:
            with pytest.raises(FeedError):
                await fetch_gainers(client, top=0)
            with pytest.raises(FeedError):
                await fetch_gainers(client, top=501)

    @pytest.mark.anyio
    async def test_fetch_agg_trades_and_depth(self):
        async with mk_client() as client:
            trades = await fetch_agg_trades(client, "futures", "AAAUSDT")
            depth = await fetch_depth(client, "futures", "AAAUSDT", now_ms=NOW)
        assert len(trades) == 3
        assert trades[0].price == pytest.approx(10.0)
        assert trades[0].qty == pytest.approx(3000.0)
        assert isinstance(depth, DepthSnapshot)
        assert depth.symbol == "AAAUSDT"
        assert depth.ts_ms == NOW
        assert depth.bids[0] == (100.0, 5.0)

    @pytest.mark.anyio
    async def test_fetch_funding_and_oi(self):
        async with mk_client() as client:
            now, hist = await fetch_funding(client, "AAAUSDT")
            assert now == pytest.approx(0.0001) and len(hist) == 90
            oi_now, oi_hist = await fetch_open_interest(client, "AAAUSDT", "5m")
            assert oi_now == pytest.approx(12345.0) and len(oi_hist) == 288
            oi_now, oi_hist, oi_times = await fetch_open_interest(
                client, "AAAUSDT", "5m", with_times=True
            )
            assert len(oi_times) == len(oi_hist) == 288
            assert oi_times[0] == NOW - 287 * 300_000
            assert oi_times[-1] == NOW


class TestMarketDetectAndSnapshot:
    @pytest.mark.anyio
    async def test_detect_market_futures_then_spot(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/fapi/v1/ticker/24hr":
                symbol = dict(request.url.params).get("symbol")
                if symbol == "SPOTONLYUSDT":
                    return httpx.Response(400, json={"code": -1121})
                return httpx.Response(200, json={"priceChangePercent": "1.0"})
            return default_handler(request)

        async with mk_client(handler) as client:
            assert await detect_market(client, "AAAUSDT") == "futures"
            assert await detect_market(client, "SPOTONLYUSDT") == "spot"

    @pytest.mark.anyio
    async def test_poll_snapshot_futures_shape(self):
        async with mk_client() as client:
            snap = await poll_snapshot(client, "AAAUSDT", "futures", "5m", NOW)
        assert snap["symbol"] == "AAAUSDT"
        assert snap["market"] == "futures"
        assert snap["state"] in ("WATCH", "PUMP", "PLATEAU", "ARMED", "SQUEEZE_RISK")
        assert snap["funding_now"] == pytest.approx(0.0001)
        assert snap["funding_pctile"] == pytest.approx(100.0)
        assert snap["oi_regime"] is not None
        assert len(snap["closes"]) == len(snap["closes_time"]) == 59
        assert snap["bar_time"] == NOW - 300_000

    @pytest.mark.anyio
    async def test_poll_snapshot_microstructure_flow(self):
        async with mk_client() as client:
            snap = await poll_snapshot(
                client, "AAAUSDT", "futures", "5m", NOW,
                include_microstructure=True,
            )
        flow = snap["flow"]
        assert flow["source"] == "klines+trades+book"
        assert flow["large_print_count"] == 1
        assert flow["large_print_net_usd"] == pytest.approx(30_000.0)
        assert flow["book_imbalance"] is not None
        assert snap["flow_errors"] == {}

    @pytest.mark.anyio
    async def test_poll_snapshot_microstructure_failure_is_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path in ("/fapi/v1/aggTrades", "/api/v3/aggTrades"):
                return httpx.Response(400, json={"msg": "nope"})
            return default_handler(request)

        async with mk_client(handler) as client:
            snap = await poll_snapshot(
                client, "AAAUSDT", "futures", "5m", NOW,
                include_microstructure=True,
            )
        assert "agg_trades" in snap["flow_errors"]
        assert snap["flow"]["source"] == "klines+book"
        assert snap["flow"]["large_print_count"] is None

    @pytest.mark.anyio
    async def test_poll_snapshot_spot_no_funding(self):
        async with mk_client() as client:
            snap = await poll_snapshot(client, "AAAUSDT", "spot", "5m", NOW)
        assert snap["market"] == "spot"
        assert snap["funding_now"] is None
        assert snap["squeeze_active"] is None


class TestFetchKlinesRange:
    @pytest.mark.anyio
    async def test_range_params_forwarded(self):
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(dict(request.url.params))
            return httpx.Response(200, json=kline_rows(10, 60_000, 1_700_000_040_000))

        async with mk_client(handler) as client:
            klines = await fetch_klines(
                client, "futures", "AAAUSDT", "1m", NOW,
                limit=200, min_closed=1,
                start_ms=1_700_000_000_000, end_ms=1_700_000_045_000,
            )
        assert seen["startTime"] == "1700000000000"
        assert seen["endTime"] == "1700000045000"
        assert seen["limit"] == "200"
        assert len(klines) == 10

    @pytest.mark.anyio
    async def test_bad_range_raises(self):
        async with mk_client() as client:
            with pytest.raises(FeedError):
                await fetch_klines(client, "futures", "AAAUSDT", "1m", NOW, min_closed=1, start_ms=-5)
            with pytest.raises(FeedError):
                await fetch_klines(client, "futures", "AAAUSDT", "1m", NOW, min_closed=1, end_ms=True)
