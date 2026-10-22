"""Ollama Cloud client tests with a local mock transport."""

from __future__ import annotations

import json

import httpx
import pytest

from signalx.chat.config import ChatConfig
from signalx.chat.ollama import OllamaCloudClient, OllamaCloudError


def cfg(**overrides):
    base = dict(
        api_key="test-secret",
        model="minimax-m3.1-flash",
        host="https://ollama.test",
        timeout_s=30.0,
        max_tokens=512,
        temperature=0.2,
        think=False,
        key_source="env",
    )
    base.update(overrides)
    return ChatConfig(**base)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_success_request_shape_and_usage():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "minimax-m3.1-flash",
            "message": {"role": "assistant", "content": "  Chart read.  "},
            "done_reason": "stop",
            "prompt_eval_count": 100,
            "eval_count": 20,
            "total_duration": 1_500_000_000,
        })

    client = OllamaCloudClient(cfg(), transport=httpx.MockTransport(handler))
    reply = await client.chat([{"role": "user", "content": "hi"}], max_tokens=256)

    assert seen["url"] == "https://ollama.test/api/chat"
    assert seen["auth"] == "Bearer test-secret"
    assert seen["body"]["model"] == "minimax-m3.1-flash"
    assert seen["body"]["stream"] is False
    assert seen["body"]["think"] is False
    assert seen["body"]["options"] == {"temperature": 0.2, "num_predict": 256}
    assert reply.content == "Chart read."
    assert reply.model == "minimax-m3.1-flash"
    assert reply.usage["prompt_tokens"] == 100
    assert reply.usage["completion_tokens"] == 20
    assert reply.usage["total_duration_ms"] == 1500


@pytest.mark.anyio
async def test_auth_error_is_mapped_and_redacted():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key test-secret"})

    client = OllamaCloudClient(cfg(), transport=httpx.MockTransport(handler))
    with pytest.raises(OllamaCloudError) as excinfo:
        await client.chat([{"role": "user", "content": "hi"}])
    err = excinfo.value
    assert err.status_code == 503
    assert err.upstream_status == 401
    assert "test-secret" not in str(err)
    assert "[redacted]" in str(err)


@pytest.mark.anyio
async def test_rate_limit_is_429():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"})

    client = OllamaCloudClient(cfg(), transport=httpx.MockTransport(handler))
    with pytest.raises(OllamaCloudError) as excinfo:
        await client.chat([{"role": "user", "content": "hi"}])
    assert excinfo.value.status_code == 429


@pytest.mark.anyio
async def test_empty_message_is_502():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"model": "minimax-m3.1-flash", "message": {"content": ""}})

    client = OllamaCloudClient(cfg(), transport=httpx.MockTransport(handler))
    with pytest.raises(OllamaCloudError) as excinfo:
        await client.chat([{"role": "user", "content": "hi"}])
    assert excinfo.value.status_code == 502


@pytest.mark.anyio
async def test_non_json_is_502():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>upstream</html>")

    client = OllamaCloudClient(cfg(), transport=httpx.MockTransport(handler))
    with pytest.raises(OllamaCloudError) as excinfo:
        await client.chat([{"role": "user", "content": "hi"}])
    assert excinfo.value.status_code == 502


def test_missing_key_cannot_construct_client():
    with pytest.raises(OllamaCloudError) as excinfo:
        OllamaCloudClient(cfg(api_key=""))
    assert excinfo.value.status_code == 503
