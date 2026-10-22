"""Chart packet builder tests with fake feeds and the real chart engines."""

from __future__ import annotations

import math

import httpx
import pytest

from signalx.chat import context as ctxmod
from signalx.chat.context import ChartContextError, build_chart_context, clear_context_cache
from signalx.feeds import FeedError
from tests.helpers import series_from_closes

NOW_MS = 1_700_000_000_000


def fake_rows(n=200):
    closes = [1.0 + 0.2 * math.sin(i / 7) + i * 0.0002 for i in range(n)]
    return series_from_closes(closes, spread=0.01)


def fake_snapshot():
    return {
        "symbol": "AAAUSDT",
        "market": "futures",
        "tf": "15m",
        "price": 1.23,
        "atr": 0.05,
        "change_24h_pct": 12.5,
        "vol_z": 2.1,
        "vol_ratio": 0.42,
        "is_pump": False,
        "pump_failed": False,
        "compression": {
            "height": 0.1, "height_atr": 2.0, "containment": 0.8,
            "score": 0.7, "qualifies": True,
        },
        "resistance": {
            "low": 1.24, "high": 1.30, "strength": 1000.0,
            "touches": 5, "dist_pct": 0.01, "dist_atr": 0.2,
        },
        "swing_low": 1.10,
        "swing_high": 1.35,
        "bos_down": False,
        "bos_up": True,
        "rsi": 61.0,
        "has_bearish_div": False,
        "has_bullish_div": True,
        "momentum_decay": False,
        "momentum_decay_bias": None,
        "funding_now": 0.0001,
        "funding_pctile": 88.0,
        "oi_regime": "TREND_FUEL",
        "squeeze_active": False,
        "n_zones": 3,
        "events": ["PLATEAU_ACTIVE", "BOS_UP"],
        "state": "PLATEAU",
        "flow": {
            "source": "klines+trades+book",
            "cvd_ratio": 0.12,
            "cvd_net_quote": 50_000.0,
            "taker_buy_ratio": 0.56,
            "large_print_count": 2,
            "large_print_net_usd": 20_000.0,
            "large_print_min_usd": 25_000.0,
            "book_imbalance": 0.3,
            "book_bias": "bid",
            "fat_money_bias": "bull",
            "largest_print_side": "buy",
        },
        "flow_errors": {},
    }


@pytest.fixture(autouse=True)
def clear_cache():
    clear_context_cache()
    yield
    clear_context_cache()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_builds_compact_packet_and_caches(monkeypatch, tmp_path):
    calls = {"snap": 0, "klines": 0}
    rows = fake_rows()

    async def fake_snap(client, symbol, market, tf, now_ms, **kw):
        calls["snap"] += 1
        assert kw.get("include_microstructure") is True
        return fake_snapshot()

    async def fake_klines(client, market, symbol, interval, now_ms, **kw):
        calls["klines"] += 1
        return rows

    monkeypatch.setattr(ctxmod, "poll_snapshot", fake_snap)
    monkeypatch.setattr(ctxmod, "fetch_klines", fake_klines)

    async with httpx.AsyncClient() as client:
        first = await build_chart_context(
            client, "AAAUSDT", "futures", "15m", NOW_MS, journal_dir=tmp_path / "journal"
        )
        second = await build_chart_context(
            client, "AAAUSDT", "futures", "15m", NOW_MS, journal_dir=tmp_path / "journal"
        )

    assert first is second
    assert calls == {"snap": 1, "klines": 4}
    assert first.payload["snapshot"]["state"] == "PLATEAU"
    assert first.payload["snapshot"]["flow"]["fat_money_bias"] == "bull"
    assert first.payload["snapshot"]["vol_ratio"] == 0.42
    assert first.payload["snapshot"]["pump_failed"] is False
    assert first.payload["overlays"] is not None
    assert first.payload["overlays"]["atr"] > 0
    assert first.payload["ema_cascade"]["tfs"]
    assert first.payload["price_action"]["recent_candles"]
    assert first.payload["levels"]["above"] or first.payload["levels"]["below"]
    assert first.meta()["symbol"] == "AAAUSDT"
    assert first.meta()["tf"] == "15m"


@pytest.mark.anyio
async def test_snapshot_failure_is_tolerated(monkeypatch, tmp_path):
    rows = fake_rows()

    async def fail_snap(*args, **kwargs):
        raise FeedError("snapshot down")

    async def fake_klines(*args, **kwargs):
        return rows

    monkeypatch.setattr(ctxmod, "poll_snapshot", fail_snap)
    monkeypatch.setattr(ctxmod, "fetch_klines", fake_klines)

    async with httpx.AsyncClient() as client:
        context = await build_chart_context(
            client, "AAAUSDT", "futures", "15m", NOW_MS, journal_dir=tmp_path / "journal"
        )

    assert context.payload["snapshot"] is None
    assert context.payload["data_quality"]["snapshot_ok"] is False
    assert any("snapshot unavailable" in warning for warning in context.payload["data_quality"]["warnings"])
    assert context.payload["price_action"]["last_price"] > 0


@pytest.mark.anyio
async def test_kline_failure_is_fatal(monkeypatch, tmp_path):
    async def fail_rows(*args, **kwargs):
        raise FeedError("klines down")

    async def fake_snap(*args, **kwargs):
        return fake_snapshot()

    monkeypatch.setattr(ctxmod, "poll_snapshot", fake_snap)
    monkeypatch.setattr(ctxmod, "fetch_klines", fail_rows)

    async with httpx.AsyncClient() as client:
        with pytest.raises(ChartContextError):
            await build_chart_context(
                client, "AAAUSDT", "futures", "15m", NOW_MS, journal_dir=tmp_path / "journal"
            )


@pytest.mark.anyio
async def test_spot_context_warns_about_derivatives(monkeypatch, tmp_path):
    rows = fake_rows()
    snap = fake_snapshot()
    snap["market"] = "spot"
    snap["funding_now"] = None
    snap["funding_pctile"] = None
    snap["oi_regime"] = None

    async def fake_snap(*args, **kwargs):
        return snap

    async def fake_klines(*args, **kwargs):
        return rows

    monkeypatch.setattr(ctxmod, "poll_snapshot", fake_snap)
    monkeypatch.setattr(ctxmod, "fetch_klines", fake_klines)

    async with httpx.AsyncClient() as client:
        context = await build_chart_context(
            client, "AAAUSDT", "spot", "15m", NOW_MS, journal_dir=tmp_path / "journal"
        )

    assert any("spot market" in warning for warning in context.payload["data_quality"]["warnings"])
