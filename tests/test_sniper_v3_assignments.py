"""SNIPER V3 assignment persistence, API, and monitor tests."""

from __future__ import annotations

import json

import httpx
import pytest

from app import server
from signalx.feeds import FeedError
from signalx.models import Kline
from signalx.sniper_v3.assignments import (
    AssignmentError,
    AssignmentFullError,
    AssignmentStore,
)
from signalx.sniper_v3.data import SniperInputs
from signalx.sniper_v3.models import SniperDiagnostics, SniperGate
from signalx.sniper_v3.monitor import SniperMonitor


@pytest.fixture
def anyio_backend():
    # The server monitor uses asyncio.Event/create_task (uvicorn's loop).
    return "asyncio"


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


def _diagnostics(symbol: str) -> SniperDiagnostics:
    return SniperDiagnostics(
        symbol=symbol,
        as_of_ms=_inputs(symbol).klines_5m[-1].close_time,
        status="no_setup",
        failed_gate="sweep",
        reason="no sweep-and-reclaim at the zone",
        direction="long",
        checks=(SniperGate("location", "15m location", "fail", "no sweep + reclaim"),),
        metrics={},
    )


def _inputs(symbol: str = "AAAUSDT") -> SniperInputs:
    return SniperInputs(
        symbol=symbol,
        klines_1h=_klines(60, 90.0),
        klines_15m=_klines(60, 95.0),
        klines_5m=_klines(60, 100.0),
        funding_rate=0.0001,
        funding_history=[0.0001] * 20,
        oi_now=1_000_000.0,
        oi_history=[1_000_000.0] * 100,
        oi_times=list(range(100)),
    )


class TestAssignmentStore:
    def test_add_is_persistent_and_idempotent(self, tmp_path):
        path = tmp_path / "assignments.json"
        store = AssignmentStore(path)
        row, created = store.add("btcusdt")
        assert created is True
        assert row.symbol == "BTCUSDT"
        again, created = store.add("BTCUSDT")
        assert created is False
        assert again.symbol == "BTCUSDT"
        assert store.symbols() == ["BTCUSDT"]
        assert json.loads(path.read_text())["assignments"][0]["symbol"] == "BTCUSDT"

    def test_remove_and_validation(self, tmp_path):
        store = AssignmentStore(tmp_path / "assignments.json")
        store.add("ETHUSDT")
        assert store.remove("ethusdt") is True
        assert store.remove("ETHUSDT") is False
        with pytest.raises(AssignmentError):
            store.add("bad symbol")

    def test_capacity(self, tmp_path):
        store = AssignmentStore(tmp_path / "assignments.json", max_assignments=1)
        store.add("BTCUSDT")
        with pytest.raises(AssignmentFullError):
            store.add("ETHUSDT")


class TestAssignmentApi:
    @pytest.mark.anyio
    async def test_assign_list_and_remove(self, tmp_path, monkeypatch):
        store = AssignmentStore(tmp_path / "assignments.json")
        monkeypatch.setattr(server, "sniper_store", store)
        async with _client() as c:
            r = await c.post("/api/sniper/watchlist", json={"symbol": "btcusdt"})
            assert r.status_code == 200
            assert r.json()["symbol"] == "BTCUSDT"
            assert r.json()["created"] is True

            r = await c.get("/api/sniper/watchlist")
            assert r.status_code == 200
            body = r.json()
            assert body["symbols"] == ["BTCUSDT"]
            assert body["results"] == {}

            r = await c.delete("/api/sniper/watchlist/BTCUSDT")
            assert r.status_code == 200
            assert r.json()["removed"] is True

            r = await c.delete("/api/sniper/watchlist/BTCUSDT")
            assert r.status_code == 404

    @pytest.mark.anyio
    async def test_invalid_symbol_is_400(self, tmp_path, monkeypatch):
        monkeypatch.setattr(server, "sniper_store", AssignmentStore(tmp_path / "a.json"))
        async with _client() as c:
            r = await c.post("/api/sniper/watchlist", json={"symbol": "bad!"})
        assert r.status_code == 400


class TestSniperMonitor:
    @pytest.mark.anyio
    async def test_evaluates_assigned_symbols(self, tmp_path, monkeypatch):
        import signalx.sniper_v3.monitor as monitor_mod

        async def fake(client, symbol):
            return _inputs(symbol), None, _diagnostics(symbol)

        monkeypatch.setattr(monitor_mod, "fetch_and_evaluate_detailed", fake)
        store = AssignmentStore(tmp_path / "assignments.json")
        store.add("BTCUSDT")
        store.add("ETHUSDT")
        out = tmp_path / "latest.json"
        monitor = SniperMonitor(None, store, out)
        await monitor.evaluate_all()

        snap = monitor.snapshot()
        assert snap["symbols"] == ["BTCUSDT", "ETHUSDT"]
        assert snap["results"]["BTCUSDT"]["status"] == "no_setup"
        assert snap["results"]["BTCUSDT"]["decision"] is None
        assert snap["results"]["BTCUSDT"]["diagnostics"]["failed_gate"] == "sweep"
        payload = json.loads(out.read_text())
        assert payload["results"]["ETHUSDT"]["status"] == "no_setup"

    @pytest.mark.anyio
    async def test_one_feed_error_does_not_kill_sweep(self, tmp_path, monkeypatch):
        import signalx.sniper_v3.monitor as monitor_mod

        async def fake(client, symbol):
            if symbol == "BADUSDT":
                raise FeedError("upstream down")
            return _inputs(symbol), None, _diagnostics(symbol)

        monkeypatch.setattr(monitor_mod, "fetch_and_evaluate_detailed", fake)
        store = AssignmentStore(tmp_path / "assignments.json")
        store.add("BADUSDT")
        store.add("BTCUSDT")
        monitor = SniperMonitor(None, store, tmp_path / "latest.json")
        await monitor.evaluate_all()

        results = monitor.snapshot()["results"]
        assert results["BADUSDT"]["status"] == "error"
        assert "upstream down" in results["BADUSDT"]["error"]
        assert results["BTCUSDT"]["status"] == "no_setup"
