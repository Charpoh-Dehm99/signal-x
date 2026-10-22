"""Ollama Cloud configuration for the SIGNAL-X chart analyst.

The API key is server-side only. It is read from ``OLLAMA_API_KEY`` (or the
legacy ``OLLAMA_CLOUD_API_KEY``), or from a 0600 key file. The browser never
receives it, and the config object deliberately omits it from ``repr``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_HOST = "https://ollama.com"
DEFAULT_MODEL = "minimax-m3.1-flash"
DEFAULT_KEY_FILE = Path.home() / ".config" / "signal-x" / "ollama.key"
DEFAULT_MODEL_FILE = Path.home() / ".config" / "signal-x" / "chat_model"
CHAT_MODELS = (
    ("minimax-m3.1-flash", "MiniMax M3.1 Flash"),
    ("minimax-m3", "MiniMax M3"),
)
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_MAX_TOKENS = 20480
DEFAULT_TEMPERATURE = 0.25
DEFAULT_THINK = "low"
MAX_KEY_CHARS = 4096
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}$")


class ChatConfigError(RuntimeError):
    """Invalid chat configuration (not a missing key)."""


def _clamp_float(raw: str | None, default: float, low: float, high: float) -> float:
    try:
        value = float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN
        return default
    return min(high, max(low, value))


def _clamp_int(raw: str | None, default: int, low: int, high: int) -> int:
    try:
        value = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value))


def _think_value(raw: str | None) -> bool | str:
    """Parse the top-level Ollama ``think`` control.

    MiniMax M3.1 Flash always emits thinking; ``low`` keeps the effort and
    token cost down. Boolean values remain accepted for older models.
    """
    value = (raw or "").strip().lower()
    if not value:
        return DEFAULT_THINK
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    if value in {"low", "medium", "high"}:
        return value
    raise ChatConfigError("SIGNALX_CHAT_THINK must be true, false, low, medium or high")


def _load_key() -> tuple[str, str]:
    """Return ``(key, source)`` without ever logging the key itself."""
    for name in ("OLLAMA_API_KEY", "OLLAMA_CLOUD_API_KEY"):
        key = os.environ.get(name, "").strip()
        if key:
            return key, "env"

    raw_path = os.environ.get("SIGNALX_OLLAMA_KEY_FILE", "").strip()
    path = Path(raw_path).expanduser() if raw_path else DEFAULT_KEY_FILE
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return "", "none"
        key = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if key:
            return key, "file"
    return "", "none"


def _normalize_host(raw: str | None) -> str:
    host = (raw or DEFAULT_HOST).strip().rstrip("/")
    parsed = urlparse(host)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ChatConfigError(
            "SIGNALX_OLLAMA_HOST must be a plain https://host (no path, credentials, query or fragment)"
        )
    return f"https://{parsed.netloc}"


def _normalize_model(raw: str | None) -> str:
    model = (raw or DEFAULT_MODEL).strip()
    if model.startswith("ollama-cloud/"):
        model = model[len("ollama-cloud/"):]
    if model.endswith(":cloud"):
        model = model[:-len(":cloud")]
    if not model or not _MODEL_RE.fullmatch(model):
        raise ChatConfigError(
            "model must be a safe Ollama model id (letters, digits, . _ : / -; max 120 chars)"
        )
    return model


def _model_file_path() -> Path:
    raw_path = os.environ.get("SIGNALX_CHAT_MODEL_FILE", "").strip()
    return Path(raw_path).expanduser() if raw_path else DEFAULT_MODEL_FILE


def _load_model_override() -> str | None:
    path = _model_file_path()
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    if not line:
        return None
    try:
        return _normalize_model(line)
    except ChatConfigError:
        return None


def save_chat_model(raw: str) -> str:
    """Validate, normalize and persist the dashboard model selection."""
    model = _normalize_model(raw)
    path = _model_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(model + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except OSError as exc:
        raise ChatConfigError("could not persist the model selection") from exc
    return model


def public_models() -> list[dict]:
    """Known models offered by the dashboard; custom ids are still accepted."""
    return [{"id": model, "label": label} for model, label in CHAT_MODELS]


@dataclass(frozen=True, repr=False)
class ChatConfig:
    """Resolved, server-side Ollama Cloud settings."""

    api_key: str = field(default="", repr=False)
    model: str = DEFAULT_MODEL
    host: str = DEFAULT_HOST
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_tokens: int = DEFAULT_MAX_TOKENS
    temperature: float = DEFAULT_TEMPERATURE
    think: bool | str = DEFAULT_THINK
    key_source: str = "none"

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_env(cls) -> "ChatConfig":
        key, source = _load_key()
        if len(key) > MAX_KEY_CHARS:
            raise ChatConfigError("OLLAMA_API_KEY is unreasonably long")
        return cls(
            api_key=key,
            model=_load_model_override() or _normalize_model(os.environ.get("SIGNALX_CHAT_MODEL")),
            host=_normalize_host(os.environ.get("SIGNALX_OLLAMA_HOST")),
            timeout_s=_clamp_float(
                os.environ.get("SIGNALX_CHAT_TIMEOUT_S"), DEFAULT_TIMEOUT_S, 10.0, 300.0
            ),
            max_tokens=_clamp_int(
                os.environ.get("SIGNALX_CHAT_MAX_TOKENS"), DEFAULT_MAX_TOKENS, 256, 32768
            ),
            temperature=_clamp_float(
                os.environ.get("SIGNALX_CHAT_TEMPERATURE"), DEFAULT_TEMPERATURE, 0.0, 1.0
            ),
            think=_think_value(os.environ.get("SIGNALX_CHAT_THINK")),
            key_source=source,
        )

    def public(self) -> dict:
        """Browser-safe status: never includes the key or its path."""
        return {
            "configured": self.configured,
            "model": self.model,
            "host": self.host,
            "key_source": self.key_source,
            "max_tokens": self.max_tokens,
            "think": self.think,
        }
