"""Read-only tool registry and executor tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from signalx.chat.tools import (
    MAX_TOOL_RESULT_CHARS,
    TOOL_SPECS,
    ToolRuntime,
    tool_result_text,
)
from signalx.journal import SignalJournal


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_registry_is_read_only_and_schema_is_closed():
    names = {spec["function"]["name"] for spec in TOOL_SPECS}
    assert names == {
        "get_gainers",
        "get_snapshot",
        "get_signals",
        "get_sniper",
        "get_sniper_watchlist",
        "get_paper",
    }
    forbidden = {
        "place_order",
        "run_command",
        "halt_paper",
        "assign_sniper",
        "remove_sniper",
        "set_model",
        "set_secret",
    }
    assert not names & forbidden
    for spec in TOOL_SPECS:
        assert spec["type"] == "function"
        assert spec["function"]["parameters"]["additionalProperties"] is False


@pytest.mark.anyio
async def test_gainers_tool_validates_and_calls_feed(monkeypatch):
    calls = {}

    async def fake_gainers(client, *, top, min_quote_volume, with_universe):
        calls.update(top=top, min_quote_volume=min_quote_volume, with_universe=with_universe)
        return [{"symbol": "AAAUSDT", "change_pct": 12.0}], 250

    monkeypatch.setattr("signalx.chat.tools.fetch_gainers", fake_gainers)
    runtime = ToolRuntime(client=object(), journal_dir=Path("/tmp"))
    out = await runtime.execute("get_gainers", {"top": 3, "min_quote_volume": 5000})
    assert out["ok"] is True
    assert out["universe"] == 250
    assert out["top1_count"] == 3
    assert calls == {"top": 3, "min_quote_volume": 5000.0, "with_universe": True}


@pytest.mark.anyio
async def test_snapshot_tool_uses_validated_market_and_tf(monkeypatch):
    calls = {}

    async def fake_detect(client, symbol):
        return "futures"

    async def fake_snapshot(client, symbol, market, tf, now_ms, *, include_microstructure):
        calls.update(symbol=symbol, market=market, tf=tf, micro=include_microstructure)
        return {"symbol": symbol, "state": "ARMED"}

    monkeypatch.setattr("signalx.chat.tools.detect_market", fake_detect)
    monkeypatch.setattr("signalx.chat.tools.poll_snapshot", fake_snapshot)
    runtime = ToolRuntime(client=object(), journal_dir=Path("/tmp"))
    out = await runtime.execute("get_snapshot", {"symbol": "btcusdt", "market": "auto", "tf": "15m"})
    assert out["ok"] is True
    assert out["snapshot"]["symbol"] == "BTCUSDT"
    assert calls == {"symbol": "BTCUSDT", "market": "futures", "tf": "15m", "micro": True}


@pytest.mark.anyio
async def test_signals_tool_reads_journal(tmp_path):
    journal = SignalJournal(tmp_path)
    journal.append([
        {
            "ts_ms": 1_700_000_000_000,
            "symbol": "AAAUSDT",
            "event": "STATE",
            "state": "ARMED",
            "prev_state": "WATCH",
        },
        {
            "ts_ms": 1_700_000_060_000,
            "symbol": "AAAUSDT",
            "event": "SIGNAL",
            "state": "ARMED",
            "prev_state": "ARMED",
        },
        {
            "ts_ms": 1_700_000_120_000,
            "symbol": "BBBUSDT",
            "event": "STATE",
            "state": "WATCH",
            "prev_state": "IDLE",
        },
    ])
    runtime = ToolRuntime(client=object(), journal_dir=tmp_path)
    out = await runtime.execute("get_signals", {"limit": 1, "signals_only": True})
    assert out["ok"] is True
    assert out["total"] == 3
    assert out["latest_ts_ms"] == 1_700_000_120_000
    assert len(out["rows"]) == 1
    assert out["rows"][0]["event"] == "SIGNAL"


@pytest.mark.anyio
async def test_sniper_tool_returns_observation(monkeypatch):
    inputs = SimpleNamespace(
        funding_rate=0.001,
        oi_now=100.0,
        klines_1h=[object()] * 60,
        klines_15m=[object()] * 60,
        klines_5m=[SimpleNamespace(close_time=1_700_000_300_000, close=42.5)],
    )
    decision = SimpleNamespace(to_dict=lambda: {"direction": "long", "rr": 1.8})
    diagnostics = SimpleNamespace(to_dict=lambda: {"ok": True, "first_failed": None})

    async def fake_eval(client, symbol):
        return inputs, decision, diagnostics

    monkeypatch.setattr("signalx.chat.tools.fetch_and_evaluate_detailed", fake_eval)
    monkeypatch.setattr("signalx.chat.tools.funding_percentile", lambda _inputs: 0.9)
    monkeypatch.setattr("signalx.chat.tools.oi_crowding", lambda _inputs: 0.2)
    runtime = ToolRuntime(client=object(), journal_dir=Path("/tmp"))
    out = await runtime.execute("get_sniper", {"symbol": "ethusdt"})
    assert out["ok"] is True
    assert out["symbol"] == "ETHUSDT"
    assert out["status"] == "setup"
    assert out["context"]["funding_pctile"] == 0.9
    assert out["decision"]["direction"] == "long"
    assert out["diagnostics"]["first_failed"] is None
    assert out["as_of_local"] and out["as_of_label"]
    assert "UTC" in out["timezone"]


@pytest.mark.anyio
async def test_watchlist_and_paper_read_without_mutation():
    class Store:
        def symbols(self):
            return ["AAAUSDT"]

        def assignments(self):
            return [SimpleNamespace(to_dict=lambda: {"symbol": "AAAUSDT", "assigned_ms": 1})]

    class Monitor:
        def snapshot(self):
            return {"evaluating": False, "symbols": ["AAAUSDT"], "results": {}, "last_error": None}

    async def paper_reader():
        return {"known": True, "equity": 1000.0, "halted": False}

    runtime = ToolRuntime(
        client=object(),
        journal_dir=Path("/tmp"),
        sniper_store=Store(),
        sniper_monitor=Monitor(),
        paper_reader=paper_reader,
    )
    watch = await runtime.execute("get_sniper_watchlist", {})
    paper = await runtime.execute("get_paper", {})
    assert watch["ok"] is True
    assert watch["assignments"][0]["symbol"] == "AAAUSDT"
    assert paper["ok"] is True
    assert paper["paper"]["equity"] == 1000.0


@pytest.mark.anyio
async def test_invalid_arguments_are_structured_errors():
    runtime = ToolRuntime(client=object(), journal_dir=Path("/tmp"))
    out = await runtime.execute("get_gainers", {"top": 999, "extra": True})
    assert out["ok"] is False
    assert "unknown argument" in out["error"]


def test_tool_result_size_cap_fails_closed():
    huge = {"ok": True, "rows": ["x" * MAX_TOOL_RESULT_CHARS]}
    text = tool_result_text(huge)
    assert len(text) <= 300
    assert '"ok":false' in text
