"""API tests for the SNIPER V3 observation endpoint (feed layer monkeypatched)."""

from __future__ import annotations

import httpx
import pytest

from app import server
from signalx.feeds import FeedError
from signalx.models import Kline
from signalx.sniper_v3.data import SniperInputs
from signalx.sniper_v3.models import SniperDecision, SniperDiagnostics, SniperGate
from signalx.sniper_v3.primitives import SniperError


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    )


def _klines(n: int = 60, base: float = 100.0) -> list[Kline]:
    out: list[Kline] = []
    for i in range(n):
        ot = 1_600_000_000_000 + i * 300_000
        close = base + i * 0.1
        out.append(
            Kline(
                ot, close, close + 0.05, close - 0.05, close, 100.0,
                ot + 300_000 - 1, 10_000.0, 10, 50.0, 5_000.0,
            )
        )
    return out


def _inputs() -> SniperInputs:
    five = _klines(60, 100.0)
    return SniperInputs(
        symbol="AAAUSDT",
        klines_1h=_klines(60, 90.0),
        klines_15m=_klines(60, 95.0),
        klines_5m=five,
        funding_rate=0.0001,
        funding_history=[0.0001] * 20,
        oi_now=1_000_000.0,
        oi_history=[1_000_000.0] * 100,
        oi_times=list(range(100)),
    )


def _diagnostics(status: str = "no_setup") -> SniperDiagnostics:
    return SniperDiagnostics(
        symbol="AAAUSDT",
        as_of_ms=_inputs().klines_5m[-1].close_time,
        status=status,  # type: ignore[arg-type]
        failed_gate="sweep" if status == "no_setup" else None,
        reason="no sweep-and-reclaim at the zone" if status == "no_setup" else "all gates aligned",
        direction="long",
        checks=(
            SniperGate("htf", "1H permission", "pass", "LONG 0.80"),
            SniperGate("location", "15m location", "fail" if status == "no_setup" else "pass", "no sweep + reclaim"),
            SniperGate("trigger", "5m trigger", "skip"),
            SniperGate("risk", "Risk / RR", "skip"),
            SniperGate("score", "Confidence / EV", "skip"),
        ),
        metrics={"htf_bias": "long", "htf_strength": 0.8},
    )


def _decision() -> SniperDecision:
    return SniperDecision(
        symbol="AAAUSDT",
        as_of_ms=_inputs().klines_5m[-1].close_time,
        direction="long",
        entry=100.0,
        stop=99.0,
        tp1=102.0,
        tp2=103.0,
        rr=2.0,
        confidence=0.7,
        expected_value_r=0.5,
        setup_score=0.6,
        trigger_score=0.7,
        zone_score=0.8,
        sweep_quality=0.7,
        divergence_strength=0.0,
        htf_strength=0.8,
        funding_penalty=0.0,
        oi_penalty=0.0,
        reasons=("1H_LONG", "5m_bos"),
        features={"rr": 2.0},
    )


class TestSniperEndpoint:
    @pytest.mark.anyio
    async def test_bad_symbol_is_400(self):
        async with _client() as c:
            r = await c.get("/api/sniper/not a symbol")
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_no_setup(self, monkeypatch):
        async def fake(client, symbol):
            return _inputs(), None, _diagnostics()

        monkeypatch.setattr(server, "fetch_and_evaluate_detailed", fake)
        async with _client() as c:
            r = await c.get("/api/sniper/AAAUSDT")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "no_setup"
        assert body["decision"] is None
        assert body["context"]["bars"] == {"1h": 60, "15m": 60, "5m": 60}
        assert body["diagnostics"]["failed_gate"] == "sweep"
        assert body["diagnostics"]["checks"][1]["state"] == "fail"

    @pytest.mark.anyio
    async def test_setup(self, monkeypatch):
        async def fake(client, symbol):
            return _inputs(), _decision(), _diagnostics("setup")

        monkeypatch.setattr(server, "fetch_and_evaluate_detailed", fake)
        async with _client() as c:
            r = await c.get("/api/sniper/AAAUSDT")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "setup"
        assert body["decision"]["direction"] == "long"
        assert body["decision"]["rr"] == 2.0
        assert body["decision"]["risk_per_unit"] == 1.0
        assert body["diagnostics"]["status"] == "setup"
        assert body["diagnostics"]["failed_gate"] is None

    @pytest.mark.anyio
    async def test_feed_error_is_502(self, monkeypatch):
        async def fake(client, symbol):
            raise FeedError("upstream down")

        monkeypatch.setattr(server, "fetch_and_evaluate_detailed", fake)
        async with _client() as c:
            r = await c.get("/api/sniper/AAAUSDT")
        assert r.status_code == 502

    @pytest.mark.anyio
    async def test_sniper_error_is_422(self, monkeypatch):
        async def fake(client, symbol):
            raise SniperError("not enough bars")

        monkeypatch.setattr(server, "fetch_and_evaluate_detailed", fake)
        async with _client() as c:
            r = await c.get("/api/sniper/AAAUSDT")
        assert r.status_code == 422
