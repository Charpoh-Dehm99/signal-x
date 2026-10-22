"""Chat API route tests: status, catalog, validation and error mapping."""

from __future__ import annotations

import httpx
import pytest

from app import server
from signalx.chat import config as chat_config
from signalx.chat.context import ChartContext, ChartContextError
from signalx.chat.ollama import OllamaCloudError


@pytest.fixture(autouse=True)
def no_ambient_key(monkeypatch, tmp_path):
    for name in (
        "OLLAMA_API_KEY",
        "OLLAMA_CLOUD_API_KEY",
        "SIGNALX_OLLAMA_KEY_FILE",
        "SIGNALX_CHAT_MODEL",
        "SIGNALX_CHAT_MODEL_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(chat_config, "DEFAULT_KEY_FILE", tmp_path / "missing.key")
    monkeypatch.setattr(chat_config, "DEFAULT_MODEL_FILE", tmp_path / "missing.model")


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://test")


def fake_context() -> ChartContext:
    return ChartContext(
        symbol="AAAUSDT",
        market="futures",
        tf="15m",
        as_of_ms=1_700_000_000_000,
        payload={"data_quality": {}, "snapshot": {}, "price_action": {}},
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_status_unconfigured():
    async with make_client() as client:
        response = await client.get("/api/chat/status")
    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is False
    assert body["model"] == "minimax-m3.1-flash"
    assert body["skills"] >= 30
    assert "api_key" not in body


@pytest.mark.anyio
async def test_status_configured_never_leaks_key(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "super-secret-key")
    async with make_client() as client:
        response = await client.get("/api/chat/status")
    assert response.status_code == 200
    assert response.json()["configured"] is True
    assert "super-secret-key" not in response.text


@pytest.mark.anyio
async def test_set_model_persists_and_status_reflects(monkeypatch, tmp_path):
    model_file = tmp_path / "chat_model"
    monkeypatch.setattr(chat_config, "DEFAULT_MODEL_FILE", model_file)
    async with make_client() as client:
        response = await client.post("/api/chat/model", json={"model": "minimax-m3:cloud"})
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "minimax-m3"
    assert any(item["id"] == "minimax-m3" for item in body["models"])
    assert model_file.read_text(encoding="utf-8") == "minimax-m3\n"

    async with make_client() as client:
        status = await client.get("/api/chat/status")
    assert status.json()["model"] == "minimax-m3"


@pytest.mark.anyio
async def test_set_model_rejects_unsafe_id():
    async with make_client() as client:
        response = await client.post("/api/chat/model", json={"model": "../../etc/passwd"})
    assert response.status_code == 400


@pytest.mark.anyio
async def test_skills_catalog():
    async with make_client() as client:
        response = await client.get("/api/chat/skills")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == len(body["skills"])
    assert body["count"] >= 30
    assert all("instructions" not in skill for skill in body["skills"])


@pytest.mark.anyio
async def test_chat_requires_key():
    async with make_client() as client:
        response = await client.post(
            "/api/chat", json={"message": "hi", "symbol": "AAAUSDT", "tf": "15m"}
        )
    assert response.status_code == 503
    assert "OLLAMA_API_KEY" in response.json()["detail"]


@pytest.mark.anyio
async def test_chat_success_with_mocked_context_and_model(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "api-secret")
    seen = {}

    async def fake_detect(client, symbol):
        return "futures"

    async def fake_context_build(client, symbol, market, tf, now_ms, *, journal_dir):
        seen["context_args"] = (symbol, market, tf)
        return fake_context()

    async def fake_reply(cfg, message, context, history):
        seen["reply"] = (message, context.symbol, history)
        return {
            "reply": "A measured chart read.",
            "model": "minimax-m3.1-flash",
            "skills_used": ["data_integrity"],
            "context": context.meta(),
            "usage": {},
        }

    monkeypatch.setattr(server, "detect_market", fake_detect)
    monkeypatch.setattr(server, "build_chart_context", fake_context_build)
    monkeypatch.setattr(server, "generate_reply", fake_reply)

    async with make_client() as client:
        response = await client.post(
            "/api/chat",
            json={
                "message": "what is the structure?",
                "symbol": "AAAUSDT",
                "market": "auto",
                "tf": "15m",
                "history": [{"role": "user", "content": "hello"}],
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "A measured chart read."
    assert seen["context_args"] == ("AAAUSDT", "futures", "15m")
    assert seen["reply"][0] == "what is the structure?"
    assert seen["reply"][2] == [{"role": "user", "content": "hello"}]
    assert "api-secret" not in response.text


@pytest.mark.anyio
async def test_chat_bad_timeframe_is_400(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "api-secret")
    async with make_client() as client:
        response = await client.post(
            "/api/chat", json={"message": "hi", "symbol": "AAAUSDT", "tf": "1d"}
        )
    assert response.status_code == 400
    assert "chart timeframe" in response.json()["detail"]


@pytest.mark.anyio
async def test_chat_bad_symbol_is_400(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "api-secret")
    async with make_client() as client:
        response = await client.post(
            "/api/chat", json={"message": "hi", "symbol": "not a symbol", "tf": "15m"}
        )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_chat_context_failure_is_502(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "api-secret")

    async def fake_detect(client, symbol):
        return "futures"

    async def fail_context(*args, **kwargs):
        raise ChartContextError("closed klines unavailable")

    monkeypatch.setattr(server, "detect_market", fake_detect)
    monkeypatch.setattr(server, "build_chart_context", fail_context)

    async with make_client() as client:
        response = await client.post(
            "/api/chat", json={"message": "hi", "symbol": "AAAUSDT", "tf": "15m"}
        )
    assert response.status_code == 502
    assert "chart data unavailable" in response.json()["detail"]


@pytest.mark.anyio
async def test_chat_ollama_error_maps_status(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "api-secret")

    async def fake_detect(client, symbol):
        return "futures"

    async def fake_context_build(*args, **kwargs):
        return fake_context()

    async def fail_reply(*args, **kwargs):
        raise OllamaCloudError("Ollama Cloud HTTP 401: bad key", status_code=503)

    monkeypatch.setattr(server, "detect_market", fake_detect)
    monkeypatch.setattr(server, "build_chart_context", fake_context_build)
    monkeypatch.setattr(server, "generate_reply", fail_reply)

    async with make_client() as client:
        response = await client.post(
            "/api/chat", json={"message": "hi", "symbol": "AAAUSDT", "tf": "15m"}
        )
    assert response.status_code == 503
    assert "Ollama Cloud" in response.json()["detail"]


@pytest.mark.anyio
async def test_chat_long_history_turn_is_accepted(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "api-secret")
    seen = {}

    async def fake_detect(client, symbol):
        return "futures"

    async def fake_context_build(*args, **kwargs):
        return fake_context()

    async def fake_reply(cfg, message, context, history):
        seen["history"] = history
        return {
            "reply": "History accepted.",
            "model": "minimax-m3.1-flash",
            "skills_used": [],
            "context": context.meta(),
            "usage": {},
        }

    monkeypatch.setattr(server, "detect_market", fake_detect)
    monkeypatch.setattr(server, "build_chart_context", fake_context_build)
    monkeypatch.setattr(server, "generate_reply", fake_reply)

    async with make_client() as client:
        response = await client.post(
            "/api/chat",
            json={
                "message": "follow up",
                "symbol": "AAAUSDT",
                "tf": "15m",
                "history": [{"role": "assistant", "content": "x" * 5000}],
            },
        )
    assert response.status_code == 200
    assert len(seen["history"][0]["content"]) == 5000


@pytest.mark.anyio
async def test_chat_history_limit_is_422(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "api-secret")
    async with make_client() as client:
        response = await client.post(
            "/api/chat",
            json={
                "message": "hi",
                "symbol": "AAAUSDT",
                "tf": "15m",
                "history": [{"role": "user", "content": "x"} for _ in range(11)],
            },
        )
    assert response.status_code == 422
