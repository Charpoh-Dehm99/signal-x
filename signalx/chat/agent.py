"""Bounded read-only tool-calling loop for the SIGNAL-X trading partner."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from .ollama import OllamaCloudClient, OllamaReply
from .tools import TOOL_SPECS, ToolRuntime, tool_result_text

MAX_TOOL_ROUNDS = 4
MAX_CALLS_PER_ROUND = 3
TOOL_TIMEOUT_S = 60.0
MAX_TOOL_RESULT_CHARS = 24_000
MAX_TOTAL_TOOL_RESULT_CHARS = 60_000


@dataclass
class AgentResult:
    content: str
    model: str
    done_reason: str | None
    usage: dict[str, Any] = field(default_factory=dict)
    tool_trace: list[dict[str, Any]] = field(default_factory=list)


def _usage_add(total: dict[str, Any], usage: object) -> None:
    if not isinstance(usage, dict):
        return
    for key in ("prompt_tokens", "completion_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            total[key] = int(total.get(key) or 0) + value
    duration = usage.get("total_duration_ms")
    if isinstance(duration, int) and not isinstance(duration, bool):
        total["total_duration_ms"] = int(total.get("total_duration_ms") or 0) + duration
    else:
        total.setdefault("total_duration_ms", None)


def _call_parts(call: object) -> tuple[str, object] | None:
    if not isinstance(call, dict):
        return None
    function = call.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if not isinstance(name, str) or not name or len(name) > 80:
        return None
    return name, function.get("arguments")


def _trace_entry(name: str, raw_arguments: object, result: dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw_arguments, str):
        try:
            args = json.loads(raw_arguments)
        except ValueError:
            args = {}
    else:
        args = raw_arguments if isinstance(raw_arguments, dict) else {}
    entry: dict[str, Any] = {"name": name, "arguments": args, "ok": bool(result.get("ok"))}
    if not entry["ok"] and isinstance(result.get("error"), str):
        entry["error"] = result["error"][:240]
    return entry


async def _execute(runtime: ToolRuntime, name: str, raw_arguments: object) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(
            runtime.execute(name, raw_arguments),
            timeout=TOOL_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return {"ok": False, "error": f"tool timed out after {TOOL_TIMEOUT_S:.0f}s"}
    except Exception as exc:  # noqa: BLE001 - never crash the chat loop
        return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}


async def run_agent(
    client: OllamaCloudClient,
    messages: list[dict],
    runtime: ToolRuntime,
    *,
    max_tokens: int,
) -> AgentResult:
    """Run up to MAX_TOOL_ROUNDS read-only tool rounds, then force a final answer.

    All tool calls are executed server-side against the same validated feeds
    the dashboard uses. The model never receives arbitrary HTTP/order access.
    """
    working = list(messages)
    trace: list[dict[str, Any]] = []
    usage: dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_duration_ms": 0,
        "llm_calls": 0,
    }
    total_tool_chars = 0

    for round_index in range(MAX_TOOL_ROUNDS):
        reply = await client.chat(working, max_tokens=max_tokens, tools=list(TOOL_SPECS))
        usage["llm_calls"] = int(usage.get("llm_calls") or 0) + 1
        _usage_add(usage, reply.usage)

        calls = list(reply.tool_calls)[:MAX_CALLS_PER_ROUND]
        if not calls:
            return AgentResult(
                content=reply.content,
                model=reply.model,
                done_reason=reply.done_reason,
                usage=usage,
                tool_trace=trace,
            )

        working.append(
            {
                "role": "assistant",
                "content": reply.content or "",
                "tool_calls": calls,
            }
        )

        async def one(call: object) -> tuple[str, object, dict[str, Any]]:
            parts = _call_parts(call)
            if parts is None:
                return "invalid_tool_call", {}, {
                    "ok": False,
                    "error": "malformed tool call from the model",
                }
            name, args = parts
            result = await _execute(runtime, name, args)
            trace.append(_trace_entry(name, args, result))
            return name, args, result

        results = await asyncio.gather(*(one(call) for call in calls))
        for name, _args, result in results:
            text = tool_result_text(result)
            if len(text) > MAX_TOOL_RESULT_CHARS:
                text = json.dumps(
                    {"ok": False, "error": "tool result exceeded the size cap"},
                    separators=(",", ":"),
                )
            if total_tool_chars + len(text) > MAX_TOTAL_TOOL_RESULT_CHARS:
                text = json.dumps(
                    {
                        "ok": False,
                        "error": "total tool-result budget exceeded",
                        "hint": "Answer with the evidence already returned.",
                    },
                    separators=(",", ":"),
                )
            total_tool_chars += len(text)
            working.append({"role": "tool", "content": text, "tool_name": name})

    # Tool budget exhausted. Force a no-tools synthesis instead of another
    # tool round, so the response cannot loop indefinitely.
    working.append(
        {
            "role": "user",
            "content": (
                "TOOL BUDGET EXHAUSTED. Answer now using only the chart packet and "
                "tool results already provided. Do not request more tools. State "
                "explicitly what evidence is missing or stale."
            ),
        }
    )
    reply = await client.chat(working, max_tokens=max_tokens)
    usage["llm_calls"] = int(usage.get("llm_calls") or 0) + 1
    _usage_add(usage, reply.usage)
    content = reply.content
    if reply.tool_calls and not content:
        content = (
            "I reached the read-only tool budget before the model produced an "
            "answer. Narrow the question or symbol and try again."
        )
    return AgentResult(
        content=content,
        model=reply.model,
        done_reason=reply.done_reason,
        usage=usage,
        tool_trace=trace,
    )
