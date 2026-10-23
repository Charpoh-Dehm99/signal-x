"""Minimal Ollama Cloud native chat client (no SDK dependency).

Endpoint: ``POST https://ollama.com/api/chat`` with
``Authorization: Bearer $OLLAMA_API_KEY``. The key stays in this process and is
never returned by the API or written to the browser.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from .config import ChatConfig

MAX_RESPONSE_BYTES = 2_000_000
MAX_ERROR_CHARS = 240
MAX_TOOL_CALLS = 8
MAX_TOOL_ARG_CHARS = 8_000
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


class OllamaCloudError(RuntimeError):
    """Upstream/configuration failure with a safe, non-secret message."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        upstream_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.upstream_status = upstream_status


@dataclass(frozen=True)
class OllamaReply:
    content: str
    model: str
    done_reason: str | None
    usage: dict
    tool_calls: tuple[dict[str, Any], ...] = ()


def _safe_detail(text: str, api_key: str = "") -> str:
    if api_key:
        text = text.replace(api_key, "[redacted]")
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = " ".join(text.split())
    return text[:MAX_ERROR_CHARS] or "no detail"


def _error_detail(resp: httpx.Response, api_key: str) -> str:
    try:
        data = resp.json()
    except ValueError:
        return _safe_detail(resp.text, api_key)
    if isinstance(data, dict):
        detail = data.get("error") or data.get("message")
        if isinstance(detail, str):
            return _safe_detail(detail, api_key)
    return _safe_detail(resp.text, api_key)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts).strip()
    return ""


def _normalize_tool_calls(raw: Any) -> tuple[dict[str, Any], ...]:
    """Validate and normalize Ollama's assistant ``tool_calls`` payload.

    Ollama normally returns arguments as an object, but some models emit a JSON
    string. Both are accepted; malformed calls are dropped rather than trusted.
    """
    if not isinstance(raw, list):
        return ()
    out: list[dict[str, Any]] = []
    for item in raw[:MAX_TOOL_CALLS]:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not _TOOL_NAME_RE.fullmatch(name):
            continue
        args = function.get("arguments")
        if args is None:
            args = {}
        if isinstance(args, str):
            if len(args) > MAX_TOOL_ARG_CHARS:
                continue
            try:
                args = json.loads(args)
            except ValueError:
                continue
        if not isinstance(args, dict):
            continue
        if len(json.dumps(args, separators=(",", ":"), default=str)) > MAX_TOOL_ARG_CHARS:
            continue
        out.append({"function": {"name": name, "arguments": args}})
    return tuple(out)


class OllamaCloudClient:
    def __init__(
        self,
        config: ChatConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not config.configured:
            raise OllamaCloudError(
                "Ollama Cloud is not configured: set OLLAMA_API_KEY (or SIGNALX_OLLAMA_KEY_FILE) and retry",
                status_code=503,
            )
        self.config = config
        self._transport = transport

    async def chat(
        self,
        messages: list[dict],
        *,
        max_tokens: int | None = None,
        tools: list[dict] | None = None,
    ) -> OllamaReply:
        if not isinstance(messages, list) or not messages:
            raise OllamaCloudError("chat messages must be a non-empty list", status_code=500)

        payload = {
            "model": self.config.model,
            "messages": messages,
            "stream": False,
            "think": self.config.think,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": max_tokens or self.config.max_tokens,
            },
        }
        if tools:
            if not isinstance(tools, list):
                raise OllamaCloudError("tools must be a list", status_code=500)
            payload["tools"] = tools
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(
            self.config.timeout_s, connect=10.0, write=30.0, pool=10.0
        )

        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                resp = await client.post(
                    f"{self.config.host}/api/chat", headers=headers, json=payload
                )
        except httpx.TimeoutException as exc:
            raise OllamaCloudError(
                f"Ollama Cloud timed out after {self.config.timeout_s:.0f}s",
                status_code=504,
            ) from exc
        except httpx.HTTPError as exc:
            raise OllamaCloudError(
                f"Ollama Cloud transport error: {type(exc).__name__}",
                status_code=502,
            ) from exc

        if len(resp.content) > MAX_RESPONSE_BYTES:
            raise OllamaCloudError("Ollama Cloud response exceeded the size cap", status_code=502)

        if resp.status_code != 200:
            detail = _error_detail(resp, self.config.api_key)
            mapped = 429 if resp.status_code == 429 else 503 if resp.status_code in (401, 403) else 502
            raise OllamaCloudError(
                f"Ollama Cloud HTTP {resp.status_code}: {detail}",
                status_code=mapped,
                upstream_status=resp.status_code,
            )

        try:
            data = resp.json()
        except ValueError as exc:
            raise OllamaCloudError("Ollama Cloud returned non-JSON", status_code=502) from exc
        if not isinstance(data, dict):
            raise OllamaCloudError("Ollama Cloud returned an unexpected payload", status_code=502)

        message = data.get("message")
        content = _content_text(message.get("content") if isinstance(message, dict) else None)
        tool_calls = _normalize_tool_calls(
            message.get("tool_calls") if isinstance(message, dict) else None
        )
        if not content and not tool_calls:
            raise OllamaCloudError("Ollama Cloud returned an empty message", status_code=502)

        total_ns = data.get("total_duration")
        usage = {
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
            "total_duration_ms": round(total_ns / 1_000_000) if isinstance(total_ns, int) else None,
        }
        return OllamaReply(
            content=content,
            model=str(data.get("model") or self.config.model)[:120],
            done_reason=str(data.get("done_reason"))[:40] if data.get("done_reason") else None,
            usage=usage,
            tool_calls=tool_calls,
        )
