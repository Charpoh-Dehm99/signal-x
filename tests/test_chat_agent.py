"""Bounded tool-calling loop tests with a scripted fake model."""

from __future__ import annotations

from copy import deepcopy

import pytest

from signalx.chat.agent import MAX_TOOL_ROUNDS, run_agent
from signalx.chat.ollama import OllamaReply
from signalx.chat.tools import ToolRuntime


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeRuntime:
    def __init__(self):
        self.calls = []

    async def execute(self, name, raw_arguments):
        self.calls.append((name, raw_arguments))
        return {"ok": True, "gainers": [{"symbol": "AAAUSDT"}], "universe": 1}


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages, *, max_tokens=None, tools=None):
        self.calls.append({"messages": deepcopy(messages), "tools": tools})
        return self.replies.pop(0)


def tool_reply(name="get_gainers", args=None):
    return OllamaReply(
        content="",
        model="fake-model",
        done_reason="tool_calls",
        usage={"prompt_tokens": 10, "completion_tokens": 2},
        tool_calls=({"function": {"name": name, "arguments": args or {"top": 3}}},),
    )


def final_reply(text="Final read."):
    return OllamaReply(
        content=text,
        model="fake-model",
        done_reason="stop",
        usage={"prompt_tokens": 5, "completion_tokens": 4},
    )


@pytest.mark.anyio
async def test_tool_round_then_final_answer():
    runtime = FakeRuntime()
    client = FakeClient([tool_reply(), final_reply()])
    result = await run_agent(
        client,
        [{"role": "user", "content": "what is moving?"}],
        runtime,
        max_tokens=123,
    )
    assert result.content == "Final read."
    assert result.tool_trace == [
        {"name": "get_gainers", "arguments": {"top": 3}, "ok": True}
    ]
    assert runtime.calls == [("get_gainers", {"top": 3})]
    assert client.calls[0]["tools"] is not None
    assert client.calls[0]["messages"][-1]["role"] == "user"
    assert client.calls[1]["messages"][-2]["role"] == "assistant"
    assert client.calls[1]["messages"][-2]["tool_calls"][0]["function"]["name"] == "get_gainers"
    assert client.calls[1]["messages"][-1]["role"] == "tool"
    assert client.calls[1]["messages"][-1]["tool_name"] == "get_gainers"
    assert result.usage["prompt_tokens"] == 15
    assert result.usage["completion_tokens"] == 6
    assert result.usage["llm_calls"] == 2


@pytest.mark.anyio
async def test_tool_budget_forces_no_tools_synthesis():
    runtime = FakeRuntime()
    client = FakeClient([tool_reply() for _ in range(MAX_TOOL_ROUNDS)] + [final_reply("Budget answer.")])
    result = await run_agent(
        client,
        [{"role": "user", "content": "loop"}],
        runtime,
        max_tokens=50,
    )
    assert result.content == "Budget answer."
    assert len(runtime.calls) == MAX_TOOL_ROUNDS
    assert len(client.calls) == MAX_TOOL_ROUNDS + 1
    assert client.calls[-1]["tools"] is None
    assert "TOOL BUDGET EXHAUSTED" in client.calls[-1]["messages"][-1]["content"]


@pytest.mark.anyio
async def test_unknown_tool_returns_structured_error():
    class Runtime:
        async def execute(self, name, raw_arguments):
            return {"ok": False, "error": "unknown tool: nope"}

    client = FakeClient([tool_reply("nope", {}), final_reply()])
    result = await run_agent(
        client,
        [{"role": "user", "content": "try it"}],
        Runtime(),
        max_tokens=50,
    )
    assert result.tool_trace[0]["name"] == "nope"
    assert result.tool_trace[0]["ok"] is False
    assert "unknown tool" in result.tool_trace[0]["error"]


def test_runtime_type_is_importable():
    assert ToolRuntime is not None
