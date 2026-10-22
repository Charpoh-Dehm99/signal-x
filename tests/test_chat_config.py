"""Ollama Cloud config tests: key sources, normalization, no secret leaks."""

from __future__ import annotations

import pytest

from signalx.chat import config as chat_config
from signalx.chat.config import ChatConfig, ChatConfigError, save_chat_model


@pytest.fixture(autouse=True)
def isolated_key(monkeypatch, tmp_path):
    for name in (
        "OLLAMA_API_KEY",
        "OLLAMA_CLOUD_API_KEY",
        "SIGNALX_OLLAMA_KEY_FILE",
        "SIGNALX_OLLAMA_HOST",
        "SIGNALX_CHAT_MODEL",
        "SIGNALX_CHAT_MODEL_FILE",
        "SIGNALX_CHAT_TIMEOUT_S",
        "SIGNALX_CHAT_MAX_TOKENS",
        "SIGNALX_CHAT_TEMPERATURE",
        "SIGNALX_CHAT_THINK",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(chat_config, "DEFAULT_KEY_FILE", tmp_path / "missing.key")
    monkeypatch.setattr(chat_config, "DEFAULT_MODEL_FILE", tmp_path / "missing.model")
    return tmp_path


def test_no_key_is_unconfigured():
    cfg = ChatConfig.from_env()
    assert cfg.configured is False
    assert cfg.model == "minimax-m3.1-flash"
    assert cfg.max_tokens == 20480
    assert cfg.host == "https://ollama.com"
    assert cfg.key_source == "none"
    assert cfg.think == "low"
    assert "api_key" not in cfg.public()


def test_env_key_is_preferred(monkeypatch, isolated_key):
    key_file = isolated_key / "ollama.key"
    key_file.write_text("file-key\n", encoding="utf-8")
    monkeypatch.setenv("OLLAMA_API_KEY", "env-key")
    cfg = ChatConfig.from_env()
    assert cfg.configured is True
    assert cfg.api_key == "env-key"
    assert cfg.key_source == "env"
    assert "env-key" not in repr(cfg)
    assert "env-key" not in str(cfg.public())


def test_legacy_env_key(monkeypatch):
    monkeypatch.setenv("OLLAMA_CLOUD_API_KEY", "legacy-key")
    cfg = ChatConfig.from_env()
    assert cfg.api_key == "legacy-key"
    assert cfg.key_source == "env"


def test_key_file(monkeypatch, isolated_key):
    key_file = isolated_key / "ollama.key"
    key_file.write_text("\n  file-key  \n", encoding="utf-8")
    monkeypatch.setenv("SIGNALX_OLLAMA_KEY_FILE", str(key_file))
    cfg = ChatConfig.from_env()
    assert cfg.api_key == "file-key"
    assert cfg.key_source == "file"


def test_model_normalization(monkeypatch):
    monkeypatch.setenv("SIGNALX_CHAT_MODEL", "ollama-cloud/minimax-m3.1-flash:cloud")
    cfg = ChatConfig.from_env()
    assert cfg.model == "minimax-m3.1-flash"


def test_model_file_overrides_env(monkeypatch, isolated_key):
    model_file = isolated_key / "chat_model"
    model_file.write_text("minimax-m3:cloud\n", encoding="utf-8")
    monkeypatch.setattr(chat_config, "DEFAULT_MODEL_FILE", model_file)
    monkeypatch.setenv("SIGNALX_CHAT_MODEL", "minimax-m3.1-flash")
    assert ChatConfig.from_env().model == "minimax-m3"


def test_save_model_persists_canonical_id(monkeypatch, isolated_key):
    model_file = isolated_key / "chat_model"
    monkeypatch.setattr(chat_config, "DEFAULT_MODEL_FILE", model_file)
    assert save_chat_model("ollama-cloud/minimax-m3:cloud") == "minimax-m3"
    assert model_file.read_text(encoding="utf-8") == "minimax-m3\n"


def test_save_model_rejects_unsafe_id(monkeypatch, isolated_key):
    model_file = isolated_key / "chat_model"
    monkeypatch.setattr(chat_config, "DEFAULT_MODEL_FILE", model_file)
    with pytest.raises(ChatConfigError):
        save_chat_model("../../etc/passwd")


def test_invalid_host_rejected(monkeypatch):
    monkeypatch.setenv("SIGNALX_OLLAMA_HOST", "http://ollama.com/api")
    with pytest.raises(ChatConfigError):
        ChatConfig.from_env()


def test_numeric_env_is_clamped(monkeypatch):
    monkeypatch.setenv("SIGNALX_CHAT_TIMEOUT_S", "9999")
    monkeypatch.setenv("SIGNALX_CHAT_MAX_TOKENS", "1")
    monkeypatch.setenv("SIGNALX_CHAT_TEMPERATURE", "2")
    cfg = ChatConfig.from_env()
    assert cfg.timeout_s == 300.0
    assert cfg.max_tokens == 256
    assert cfg.temperature == 1.0


def test_think_effort_env(monkeypatch):
    monkeypatch.setenv("SIGNALX_CHAT_THINK", "high")
    assert ChatConfig.from_env().think == "high"


def test_invalid_think_rejected(monkeypatch):
    monkeypatch.setenv("SIGNALX_CHAT_THINK", "banana")
    with pytest.raises(ChatConfigError):
        ChatConfig.from_env()
