"""API tests via httpx ASGI transport with the feed layer monkeypatched."""

from __future__ import annotations

import json

import httpx
import pytest

from app import server
from signalx.feeds import FeedError
from signalx.live import LiveError
from signalx.logos import LogoError
from signalx.models import Zone
from tests.helpers import series_from_closes

FAKE_SNAP = {
    "symbol": "AAAUSDT",
    "price": 1.23,
    "atr": 0.05,
    "change_24h_pct": 25.0,
    "vol_z": 4.0,
    "is_pump": True,
    "compression": None,
    "n_zones": 1,
    "resistance": None,
    "swing_low": None,
    "bos_down": False,
    "bos_up": False,
    "rsi": 60.0,
    "has_bearish_div": False,
    "momentum_decay": False,
    "funding_now": 0.0001,
    "funding_pctile": 99.0,
    "oi_regime": "TREND_FUEL",
    "squeeze_active": False,
    "events": [],
    "state": "PUMP",
    "market": "futures",
    "tf": "5m",
    "closes": [1.0, 1.1, 1.23],
    "closes_time": [1, 2, 3],
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test")


class TestHealthAndStatic:
    @pytest.mark.anyio
    async def test_health(self):
        async with make_client() as c:
            r = await c.get("/api/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True

    @pytest.mark.anyio
    async def test_index_html_served(self):
        async with make_client() as c:
            r = await c.get("/")
        assert r.status_code == 200
        assert "SIGNAL-X" in r.text

    @pytest.mark.anyio
    async def test_static_css_served(self):
        async with make_client() as c:
            r = await c.get("/static/style.css")
        assert r.status_code == 200
        assert "text/css" in r.headers["content-type"]


class TestGainers:
    @pytest.mark.anyio
    async def test_gainers_ok(self, monkeypatch):
        async def fake(client, *, top=50, min_quote_volume=0.0, with_universe=False):
            rows = [{"symbol": "AAAUSDT", "change_pct": 25.0, "quote_volume": 5e6, "last_price": 1.2}]
            return (rows, 250) if with_universe else rows

        monkeypatch.setattr(server, "fetch_gainers", fake)
        async with make_client() as c:
            r = await c.get("/api/gainers?top=1")
        assert r.status_code == 200
        body = r.json()
        assert body["gainers"][0]["symbol"] == "AAAUSDT"
        assert body["universe"] == 250
        assert body["top1_count"] == 3

    @pytest.mark.anyio
    async def test_gainers_upstream_error_is_502(self, monkeypatch):
        async def fake(client, *, top=50, min_quote_volume=0.0, with_universe=False):
            raise FeedError("bulk ticker down")

        monkeypatch.setattr(server, "fetch_gainers", fake)
        async with make_client() as c:
            r = await c.get("/api/gainers")
        assert r.status_code == 502
        assert "upstream" in r.json()["detail"]

    @pytest.mark.anyio
    async def test_gainers_top_out_of_range_is_422(self):
        async with make_client() as c:
            r = await c.get("/api/gainers?top=0")
        assert r.status_code == 422


def fresh_logo_cache() -> dict:
    return {"attempt_ms": 0, "updated_ms": None, "symbols": {}, "extra": {}}


class TestLogos:
    @pytest.mark.anyio
    async def test_logos_ok_then_cached(self, monkeypatch):
        calls = {"n": 0}

        async def fake(client):
            calls["n"] += 1
            return {"BTC": "https://cdn.example/btc.png", "Q": "https://cdn.example/q.png"}

        monkeypatch.setattr(server, "fetch_coin_logos", fake)
        monkeypatch.setattr(server, "_logo_cache", fresh_logo_cache())
        async with make_client() as c:
            r1 = await c.get("/api/logos")
            r2 = await c.get("/api/logos")
        assert r1.status_code == 200
        body = r1.json()
        assert body["symbols"]["Q"] == "https://cdn.example/q.png"
        assert isinstance(body["updated_ms"], int)
        assert calls["n"] == 1  # second request served from cache
        assert r2.json()["symbols"] == body["symbols"]

    @pytest.mark.anyio
    async def test_upstream_failure_without_cache_is_empty_200(self, monkeypatch):
        async def fake(client):
            raise LogoError("HTTP 429 from CoinGecko")

        monkeypatch.setattr(server, "fetch_coin_logos", fake)
        monkeypatch.setattr(server, "_logo_cache", fresh_logo_cache())
        async with make_client() as c:
            r = await c.get("/api/logos")
        assert r.status_code == 200
        assert r.json() == {"symbols": {}, "updated_ms": None}

    @pytest.mark.anyio
    async def test_upstream_failure_serves_stale_map(self, monkeypatch):
        async def fake(client):
            raise LogoError("CoinGecko down")

        monkeypatch.setattr(server, "fetch_coin_logos", fake)
        monkeypatch.setattr(server, "_logo_cache", {
            "attempt_ms": 0,
            "updated_ms": 1234,
            "symbols": {"ETH": "https://cdn.example/eth.png"},
            "extra": {},
        })
        async with make_client() as c:
            r = await c.get("/api/logos")
        body = r.json()
        assert body["symbols"] == {"ETH": "https://cdn.example/eth.png"}
        assert body["updated_ms"] == 1234

    @pytest.mark.anyio
    async def test_symbols_param_filters_and_resolves(self, monkeypatch):
        resolved: list[str] = []

        async def fake_fetch(client):
            return {"BTC": "https://cdn.example/btc.png"}

        async def fake_resolve(client, sym):
            resolved.append(sym)
            return "https://cdn.example/" + sym.lower() + ".png" if sym in ("Q", "NEW") else None

        monkeypatch.setattr(server, "fetch_coin_logos", fake_fetch)
        monkeypatch.setattr(server, "resolve_coin_logo", fake_resolve)
        monkeypatch.setattr(server, "_logo_cache", fresh_logo_cache())
        async with make_client() as c:
            r = await c.get("/api/logos?symbols=q,btc,new,eth,??,abcdefghijklmnopqrstuvw")
        body = r.json()
        assert body["symbols"] == {
            "Q": "https://cdn.example/q.png",
            "BTC": "https://cdn.example/btc.png",
            "NEW": "https://cdn.example/new.png",
        }
        assert resolved == ["Q", "NEW", "ETH"]  # invalid / over-length tokens dropped server-side

    @pytest.mark.anyio
    async def test_symbols_param_ascii_only(self, monkeypatch):
        """Fullwidth/unicode letters must never reach the resolver or cache keys."""
        resolved: list[str] = []

        async def fake_fetch(client):
            return {"BTC": "https://cdn.example/btc.png"}

        async def fake_resolve(client, sym):
            resolved.append(sym)
            return None

        monkeypatch.setattr(server, "fetch_coin_logos", fake_fetch)
        monkeypatch.setattr(server, "resolve_coin_logo", fake_resolve)
        monkeypatch.setattr(server, "_logo_cache", fresh_logo_cache())
        async with make_client() as c:
            # %EF%BC%A2%EF%BC%B4%EF%BC%A3 == ＢＴＣ (fullwidth ASCII lookalikes)
            r = await c.get("/api/logos?symbols=%EF%BC%A2%EF%BC%B4%EF%BC%A3,BTC")
        assert r.status_code == 200
        assert r.json()["symbols"] == {"BTC": "https://cdn.example/btc.png"}
        assert resolved == []

    @pytest.mark.anyio
    async def test_definitive_miss_negatively_cached(self, monkeypatch):
        calls = {"n": 0}

        async def fake_fetch(client):
            return {}

        async def fake_resolve(client, sym):
            calls["n"] += 1
            return None

        monkeypatch.setattr(server, "fetch_coin_logos", fake_fetch)
        monkeypatch.setattr(server, "resolve_coin_logo", fake_resolve)
        monkeypatch.setattr(server, "_logo_cache", fresh_logo_cache())
        async with make_client() as c:
            r1 = await c.get("/api/logos?symbols=SKDD")
            r2 = await c.get("/api/logos?symbols=SKDD")
        assert calls["n"] == 1  # second request served by the negative cache
        assert r1.json()["symbols"] == {} and r2.json()["symbols"] == {}

    @pytest.mark.anyio
    async def test_transient_failure_retried_next_request(self, monkeypatch):
        calls = {"n": 0}

        async def fake_fetch(client):
            return {}

        async def fake_resolve(client, sym):
            calls["n"] += 1
            raise LogoError("rate limited")

        monkeypatch.setattr(server, "fetch_coin_logos", fake_fetch)
        monkeypatch.setattr(server, "resolve_coin_logo", fake_resolve)
        monkeypatch.setattr(server, "_logo_cache", fresh_logo_cache())
        async with make_client() as c:
            r1 = await c.get("/api/logos?symbols=ARIA")
            r2 = await c.get("/api/logos?symbols=ARIA")
        assert calls["n"] == 2  # transient failures are not negatively cached
        assert r1.json()["symbols"] == {} and r2.json()["symbols"] == {}

    @pytest.mark.anyio
    async def test_cached_extra_skips_resolution(self, monkeypatch):
        calls: list[str] = []

        async def fake_fetch(client):
            return {}

        async def fake_resolve(client, sym):
            calls.append(sym)
            return "https://cdn.example/fresh.png"

        monkeypatch.setattr(server, "fetch_coin_logos", fake_fetch)
        monkeypatch.setattr(server, "resolve_coin_logo", fake_resolve)
        cache = fresh_logo_cache()
        cache["extra"]["Q"] = {"url": "https://cdn.example/q.png", "ts": int(1e15)}
        monkeypatch.setattr(server, "_logo_cache", cache)
        async with make_client() as c:
            r = await c.get("/api/logos?symbols=q")
        assert r.json()["symbols"] == {"Q": "https://cdn.example/q.png"}
        assert calls == []  # fresh extra entry: no upstream call

    @pytest.mark.anyio
    async def test_full_map_merges_extras(self, monkeypatch):
        async def fake_fetch(client):
            return {"BTC": "https://cdn.example/btc.png"}

        monkeypatch.setattr(server, "fetch_coin_logos", fake_fetch)
        cache = fresh_logo_cache()
        cache["extra"]["Q"] = {"url": "https://cdn.example/q.png", "ts": 1}
        cache["extra"]["SKDD"] = {"url": None, "ts": 1}
        monkeypatch.setattr(server, "_logo_cache", cache)
        async with make_client() as c:
            r = await c.get("/api/logos")
        assert r.json()["symbols"] == {
            "BTC": "https://cdn.example/btc.png",
            "Q": "https://cdn.example/q.png",
        }


class TestSnapshot:
    @pytest.mark.anyio
    async def test_bad_symbol_is_400(self):
        async with make_client() as c:
            r = await c.get("/api/snapshot/not a symbol")
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_bad_tf_is_400(self):
        async with make_client() as c:
            r = await c.get("/api/snapshot/AAAUSDT?tf=7m")
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_snapshot_ok(self, monkeypatch):
        async def fake_detect(client, symbol):
            return "futures"

        async def fake_poll(client, symbol, market, tf, now_ms):
            return FAKE_SNAP

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "poll_snapshot", fake_poll)
        async with make_client() as c:
            r = await c.get("/api/snapshot/AAAUSDT")
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "AAAUSDT"
        assert body["market"] == "futures"
        assert body["state"] == "PUMP"

    @pytest.mark.anyio
    async def test_snapshot_feed_error_is_502(self, monkeypatch):
        async def fake_detect(client, symbol):
            return "futures"

        async def fake_poll(client, symbol, market, tf, now_ms):
            raise FeedError("HTTP 400 for /fapi/v1/klines")

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "poll_snapshot", fake_poll)
        async with make_client() as c:
            r = await c.get("/api/snapshot/AAAUSDT")
        assert r.status_code == 502

    @pytest.mark.anyio
    async def test_snapshot_live_error_is_422(self, monkeypatch):
        async def fake_detect(client, symbol):
            return "futures"

        async def fake_poll(client, symbol, market, tf, now_ms):
            raise LiveError("need >= 50 5m klines")

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "poll_snapshot", fake_poll)
        async with make_client() as c:
            r = await c.get("/api/snapshot/AAAUSDT")
        assert r.status_code == 422


def chart_klines(n: int = 200):
    import math

    closes = [100 + 5 * math.sin(i / 7) + i * 0.02 for i in range(n)]
    return series_from_closes(closes)


class TestKlines:
    @pytest.mark.anyio
    async def test_bad_symbol_is_400(self):
        async with make_client() as c:
            r = await c.get("/api/klines/not a symbol")
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_chart_unsupported_tf_is_400(self):
        async with make_client() as c:
            r = await c.get("/api/klines/AAAUSDT?tf=1d")
        assert r.status_code == 400
        assert "chart timeframe" in r.json()["detail"]

    @pytest.mark.anyio
    async def test_klines_ok(self, monkeypatch):
        async def fake_detect(client, symbol):
            return "futures"

        async def fake_fetch(client, market, symbol, interval, now_ms, **kw):
            assert kw.get("include_incomplete") is True
            return chart_klines(100)

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "fetch_klines", fake_fetch)
        async with make_client() as c:
            r = await c.get("/api/klines/AAAUSDT?tf=5m&limit=100")
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "AAAUSDT"
        assert body["market"] == "futures"
        assert body["step_ms"] == 300_000
        assert len(body["candles"]) == 100
        assert set(body["candles"][0]) == {"t", "o", "h", "l", "c", "v", "qv"}

    @pytest.mark.anyio
    async def test_klines_feed_error_is_502(self, monkeypatch):
        async def fake_detect(client, symbol):
            return "futures"

        async def fake_fetch(client, market, symbol, interval, now_ms, **kw):
            raise FeedError("klines down")

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "fetch_klines", fake_fetch)
        async with make_client() as c:
            r = await c.get("/api/klines/AAAUSDT")
        assert r.status_code == 502

    @pytest.mark.anyio
    async def test_klines_limit_bounds(self):
        async with make_client() as c:
            low = await c.get("/api/klines/AAAUSDT?limit=2")
            high = await c.get("/api/klines/AAAUSDT?limit=501")
        assert low.status_code == 422
        assert high.status_code == 422


class TestOverlays:
    @pytest.mark.anyio
    async def test_overlays_ok_runs_real_engines(self, monkeypatch):
        klines = chart_klines(200)

        async def fake_detect(client, symbol):
            return "futures"

        async def fake_fetch(client, market, symbol, interval, now_ms, **kw):
            assert kw.get("include_incomplete") is None  # closed bars only
            return klines

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "fetch_klines", fake_fetch)
        async with make_client() as c:
            r = await c.get("/api/overlays/AAAUSDT?tf=5m")
        assert r.status_code == 200
        body = r.json()
        assert body["symbol"] == "AAAUSDT"
        assert body["t0"] == klines[0].open_time
        assert body["n"] == 200
        assert len(body["rsi"]) == 200
        assert len(body["avwap"]["line"]) == 200
        assert body["step_ms"] == 300_000
        assert "structure" in body and "zones" in body and "breaks" in body

    @pytest.mark.anyio
    async def test_overlays_too_few_bars_is_422(self, monkeypatch):
        async def fake_detect(client, symbol):
            return "futures"

        async def fake_fetch(client, market, symbol, interval, now_ms, **kw):
            return chart_klines(30)

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "fetch_klines", fake_fetch)
        async with make_client() as c:
            r = await c.get("/api/overlays/AAAUSDT")
        assert r.status_code == 422

    @pytest.mark.anyio
    async def test_overlays_bad_symbol_is_400(self):
        async with make_client() as c:
            r = await c.get("/api/overlays/lobster%20USDT")
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_overlays_limit_lower_bound(self):
        async with make_client() as c:
            r = await c.get("/api/overlays/AAAUSDT?limit=59")
        assert r.status_code == 422


class TestEmaCascade:
    @pytest.mark.anyio
    async def test_cascade_ok_and_cached(self, monkeypatch):
        calls = {"n": 0}
        klines = chart_klines(200)

        async def fake_detect(client, symbol):
            return "futures"

        async def fake_fetch(client, market, symbol, interval, now_ms, **kw):
            calls["n"] += 1
            assert kw.get("include_incomplete") is None  # closed bars only
            return klines

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "fetch_klines", fake_fetch)
        monkeypatch.setattr(server, "_ema_cache", {})
        async with make_client() as c:
            r1 = await c.get("/api/ema-cascade/AAAUSDT")
            r2 = await c.get("/api/ema-cascade/AAAUSDT")
        assert r1.status_code == 200 and r2.status_code == 200
        body = r1.json()
        assert body["symbol"] == "AAAUSDT" and body["market"] == "futures"
        assert [t["tf"] for t in body["tfs"]] == ["5m", "15m", "1h", "4h"]
        for t in body["tfs"]:
            assert t["state"] in (None, "up", "down", "neutral")
            assert t["closed"] == 200
            assert t["step_ms"] > 0
            assert set(t["emas"]) == {"8", "13", "21"}
            for cx in t["crosses"]:
                assert cx["dir"] in ("up", "down") and cx["prev"] != cx["dir"]
                assert 0 <= cx["i"] < 200
        pat = body["pattern"]
        assert set(pat) == {"direction", "chain", "intervals_ms", "full_stack"}
        assert set(body["errors"]) == set()
        assert calls["n"] == 4  # one fetch per timeframe; second request cached

    @pytest.mark.anyio
    async def test_cascade_partial_feed_failure_is_tolerated(self, monkeypatch):
        async def fake_detect(client, symbol):
            return "futures"

        async def fake_fetch(client, market, symbol, interval, now_ms, **kw):
            if interval == "4h":
                raise FeedError("too few closed klines")
            return chart_klines(200)

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "fetch_klines", fake_fetch)
        monkeypatch.setattr(server, "_ema_cache", {})
        async with make_client() as c:
            r = await c.get("/api/ema-cascade/AAAUSDT")
        assert r.status_code == 200
        body = r.json()
        assert [t["tf"] for t in body["tfs"]] == ["5m", "15m", "1h"]
        assert "4h" in body["errors"]

    @pytest.mark.anyio
    async def test_cascade_total_feed_blackout_is_502(self, monkeypatch):
        async def fake_detect(client, symbol):
            return "futures"

        async def fake_fetch(client, market, symbol, interval, now_ms, **kw):
            raise FeedError("klines down")

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "fetch_klines", fake_fetch)
        monkeypatch.setattr(server, "_ema_cache", {})
        async with make_client() as c:
            r = await c.get("/api/ema-cascade/AAAUSDT")
        assert r.status_code == 502

    @pytest.mark.anyio
    async def test_cascade_bad_symbol_and_limit_bounds(self):
        async with make_client() as c:
            bad = await c.get("/api/ema-cascade/lobster%20USDT")
            low = await c.get("/api/ema-cascade/AAAUSDT?limit=59")
        assert bad.status_code == 400
        assert low.status_code == 422


def sse_payloads(text: str) -> list[dict]:
    """Parse SSE frames, skipping the terminating `event: end` frame."""
    out = []
    for frame in text.split("\n\n"):
        if not frame.strip() or "event: end" in frame:
            continue
        for line in frame.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: "):]))
    return out


class TestStream:
    @pytest.mark.anyio
    async def test_stream_sse_frames_and_end(self, monkeypatch):
        calls = {"n": 0}

        async def fake_detect(client, symbol):
            return "futures"

        async def fake_poll(client, symbol, market, tf, now_ms):
            calls["n"] += 1
            return FAKE_SNAP | {"price": 1.0 + calls["n"] / 100}

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "poll_snapshot", fake_poll)
        async with make_client() as c:
            r = await c.get("/api/stream/AAAUSDT?polls=2&interval=1")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        payloads = sse_payloads(r.text)
        assert len(payloads) == 2
        assert all(p["ok"] for p in payloads)
        assert payloads[0]["snapshot"]["price"] != payloads[1]["snapshot"]["price"]
        assert "event: end" in r.text

    @pytest.mark.anyio
    async def test_stream_upstream_failure_pushes_error_frame(self, monkeypatch):
        async def fake_detect(client, symbol):
            return "futures"

        async def fake_poll(client, symbol, market, tf, now_ms):
            raise FeedError("boom")

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "poll_snapshot", fake_poll)
        async with make_client() as c:
            r = await c.get("/api/stream/AAAUSDT?polls=1&interval=1")
        payloads = sse_payloads(r.text)
        assert len(payloads) == 1
        assert payloads[0]["ok"] is False
        assert "boom" in payloads[0]["error"]

    @pytest.mark.anyio
    async def test_stream_serializes_dataclass_fields(self, monkeypatch):
        snap = FAKE_SNAP | {
            "compression": {
                "height_atr": 1.0, "containment": 0.9, "score": 0.8,
                "qualifies": True, "height": 0.05, "box": Zone(1.0, 1.05, 2.0, 3),
            }
        }

        async def fake_detect(client, symbol):
            return "futures"

        async def fake_poll(client, symbol, market, tf, now_ms):
            return snap

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "poll_snapshot", fake_poll)
        async with make_client() as c:
            r = await c.get("/api/stream/AAAUSDT?polls=1&interval=1")
        payloads = sse_payloads(r.text)
        assert len(payloads) == 1
        box = payloads[0]["snapshot"]["compression"]["box"]
        assert box == {"low": 1.0, "high": 1.05, "strength": 2.0, "touches": 3}

    @pytest.mark.anyio
    async def test_stream_interval_bounds(self):
        async with make_client() as c:
            r = await c.get("/api/stream/AAAUSDT?interval=0.1")
        assert r.status_code == 422


def seed_journal(root) -> None:
    from signalx.journal import SignalJournal

    j = SignalJournal(root)
    j.append([
        {
            "ts_ms": 1_700_000_000_000,
            "symbol": "AAAUSDT",
            "event": "TRANSITION",
            "state": "ARMED",
            "prev_state": "PLATEAU_BUILDING",
            "price": 1.23,
            "score": 0.7,
        }
    ])
    j.append([
        {
            "ts_ms": 1_700_000_060_000,
            "symbol": "AAAUSDT",
            "event": "SIGNAL",
            "state": "TRIGGERED",
            "prev_state": "ARMED",
            "price": 1.20,
            "score": 0.8,
            "entry": 1.20,
            "stop": 1.26,
            "tp1": 1.10,
            "tp2": 1.02,
            "reasons": "RESISTANCE_TOUCHED; BREAKOUT_VOL_x2.0",
        }
    ])


class TestSignals:
    @pytest.mark.anyio
    async def test_empty_journal_returns_empty_rows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "JOURNAL_DIR", tmp_path)
        async with make_client() as c:
            r = await c.get("/api/signals")
        assert r.status_code == 200
        body = r.json()
        assert body["rows"] == []
        assert body["latest"] == {}
        assert body["total"] == 0

    @pytest.mark.anyio
    async def test_rows_newest_first_and_latest_map(self, tmp_path, monkeypatch):
        seed_journal(tmp_path)
        monkeypatch.setattr(server, "JOURNAL_DIR", tmp_path)
        monkeypatch.setattr(server, "HEARTBEAT_PATH", tmp_path / "missing.json")
        async with make_client() as c:
            r = await c.get("/api/signals")
        body = r.json()
        assert body["total"] == 2
        assert [row["event"] for row in body["rows"]] == ["SIGNAL", "TRANSITION"]
        assert body["rows"][0]["entry"] == 1.20
        assert body["latest"]["AAAUSDT"]["state"] == "TRIGGERED"
        assert body["latest_ts_ms"] == 1_700_000_060_000
        assert body["now_ms"] >= body["latest_ts_ms"]
        assert body["sentinel"]["known"] is False

    @pytest.mark.anyio
    async def test_live_sentinel_heartbeat_reported_running(self, tmp_path, monkeypatch):
        import time as _time

        hb = tmp_path / "heartbeat.json"
        hb.write_text(json.dumps({
            "ts_ms": int(_time.time() * 1000),
            "tf": "5m", "symbols": 42, "active": 3, "rows": 1, "journal_rows": 9,
        }))
        monkeypatch.setattr(server, "JOURNAL_DIR", tmp_path / "journal")
        monkeypatch.setattr(server, "HEARTBEAT_PATH", hb)
        async with make_client() as c:
            r = await c.get("/api/signals")
        sent = r.json()["sentinel"]
        assert sent["known"] is True
        assert sent["running"] is True
        assert sent["age_ms"] < server.HEARTBEAT_STALE_MS
        assert sent["symbols"] == 42

    @pytest.mark.anyio
    async def test_stale_or_missing_heartbeat_reports_offline(self, tmp_path, monkeypatch):
        hb = tmp_path / "heartbeat.json"
        hb.write_text(json.dumps({"ts_ms": 1_000}))
        monkeypatch.setattr(server, "JOURNAL_DIR", tmp_path / "journal")
        monkeypatch.setattr(server, "HEARTBEAT_PATH", hb)
        async with make_client() as c:
            stale = (await c.get("/api/signals")).json()["sentinel"]
            health = (await c.get("/api/health")).json()["sentinel"]
        assert stale["known"] is True and stale["running"] is False
        assert health["known"] is True and health["running"] is False

        monkeypatch.setattr(server, "HEARTBEAT_PATH", tmp_path / "missing.json")
        async with make_client() as c:
            missing = (await c.get("/api/signals")).json()["sentinel"]
        assert missing == {"known": False, "running": False, "age_ms": None}

    @pytest.mark.anyio
    async def test_signals_only_filter_keeps_total_unfiltered(self, tmp_path, monkeypatch):
        seed_journal(tmp_path)
        monkeypatch.setattr(server, "JOURNAL_DIR", tmp_path)
        async with make_client() as c:
            r = await c.get("/api/signals?signals_only=true&limit=1")
        body = r.json()
        assert len(body["rows"]) == 1
        assert body["rows"][0]["event"] == "SIGNAL"
        assert body["total"] == 2

    @pytest.mark.anyio
    async def test_limit_bounds_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "JOURNAL_DIR", tmp_path)
        async with make_client() as c:
            low = await c.get("/api/signals?limit=0")
            high = await c.get("/api/signals?limit=1001")
        assert low.status_code == 422
        assert high.status_code == 422


class TestSecurityHeaders:
    @pytest.mark.anyio
    async def test_hardening_headers_on_api_and_index(self):
        async with make_client() as c:
            api = await c.get("/api/health")
            index = await c.get("/")
        for r in (api, index):
            assert r.status_code == 200
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["x-frame-options"] == "DENY"
            assert r.headers["referrer-policy"] == "no-referrer"
            assert "camera=()" in r.headers["permissions-policy"]
            csp = r.headers["content-security-policy"]
            assert "script-src 'self'" in csp
            assert "frame-ancestors 'none'" in csp
            assert "form-action 'none'" in csp

    @pytest.mark.anyio
    async def test_stream_headers_not_duplicated(self, monkeypatch):
        """SSE ships its own Cache-Control; hardening must not duplicate keys
        and must reach streaming responses."""
        async def fake_detect(client, sym):
            return "futures"

        async def fake_poll(client, sym, market, tf, now_ms):
            return json.loads(json.dumps(FAKE_SNAP))

        monkeypatch.setattr(server, "detect_market", fake_detect)
        monkeypatch.setattr(server, "poll_snapshot", fake_poll)
        async with make_client() as c:
            r = await c.get("/api/stream/AAAUSDT?polls=1&tf=5m")
        assert r.status_code == 200
        names = [k.lower() for k, _ in r.headers.multi_items()]
        assert names.count("content-security-policy") == 1
        assert names.count("cache-control") == 1
        assert names.count("x-frame-options") == 1
