"""Alert tests: mandatory secret redaction, env handling, loud failures."""

from __future__ import annotations

import pytest

from signalx.alerts import (
    format_signal,
    load_alert_env,
    redact,
    send_alert,
)
from signalx.models import Side, Signal
from signalx.validation import ValidationError


# ------------------------------------------------------------------- redact
def test_redact_api_key():
    out = redact("oops api_key=superSecret123456 in the log")
    assert "superSecret123456" not in out
    assert "***REDACTED***" in out


def test_redact_bearer_token():
    token = "eyJhbGciOiJIUzI1NiJ9.abcdef123456"  # nosec B105 - fake fixture for redaction test
    out = redact(f"Authorization: Bearer {token}")
    assert token not in out
    assert "***REDACTED***" in out


def test_redact_telegram_token_shape():
    token = "1234567890:AAF-abcdefghijklmnopqrstuvwxyz012345"  # nosec B105 - fake fixture
    out = redact(f"using {token} for alerts")
    assert token not in out
    assert "***REDACTED***" in out


def test_redact_rejects_non_string():
    with pytest.raises(TypeError):
        redact(12345)


# ------------------------------------------------------------- format_signal
def _signal(**overrides):
    defaults = dict(
        symbol="BTCUSDT",
        side=Side.SHORT,
        entry=100.0,
        stop=103.0,
        tp1=95.0,
        tp2=90.0,
        score=0.74,
        reasons=("BOS down confirmed", "bearish RSI divergence"),
    )
    defaults.update(overrides)
    return Signal(**defaults)


def test_format_signal_contains_key_fields():
    text = format_signal(_signal())
    assert "SIGNAL-X SHORT BTCUSDT" in text
    assert "score 0.74" in text
    assert "BOS down confirmed" in text


def test_format_signal_rejects_hostile_symbol():
    with pytest.raises(ValidationError):
        format_signal(_signal(symbol="BTC/USDT;rm -rf"))


def test_format_signal_redacts_reasons():
    text = format_signal(_signal(reasons=("leaked secret=abcdef123456",)))
    assert "abcdef123456" not in text
    assert "***REDACTED***" in text


# ------------------------------------------------------------- load_alert_env
def test_load_alert_env_missing_raises():
    with pytest.raises(RuntimeError) as exc:
        load_alert_env({})
    assert "TELEGRAM_BOT_TOKEN" in str(exc.value)
    assert "TELEGRAM_CHAT_ID" in str(exc.value)


def test_load_alert_env_complete():
    env = {
        "TELEGRAM_BOT_TOKEN": "t",  # nosec B105 - placeholder, not a secret
        "TELEGRAM_CHAT_ID": "42",
    }
    assert load_alert_env(env) == env


# ----------------------------------------------------------------- send_alert
class FakePost:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.calls = []

    def __call__(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self

    # response interface
    @property
    def content(self):
        return b"ok"


def test_send_alert_redacts_body_and_passes_timeout():
    post = FakePost()
    ok = send_alert(
        "leak api_key=supersecret123456 here",
        # nosec B106 - fake fixture token (Telegram-shaped) for redaction test
        token="1234567890:AAF-abcdefghijklmnopqrstuvwxyz012345",
        chat_id="42",
        http_post=post,
    )
    assert ok is True
    sent = post.calls[0]
    assert sent["json"]["chat_id"] == "42"
    assert "supersecret123456" not in sent["json"]["text"]
    assert sent["timeout"] > 0
    # NOTE: Telegram's API requires the token in the URL; we only guarantee
    # it never leaks into the message body or logs.
    assert sent["json"]["text"].count("***REDACTED***") >= 1


def test_send_alert_non_200_raises():
    post = FakePost(status_code=500)
    with pytest.raises(RuntimeError):
        send_alert("hi", token="t", chat_id="1", http_post=post)  # nosec B106 - placeholder


def test_send_alert_requires_credentials():
    post = FakePost()
    with pytest.raises(RuntimeError):
        send_alert("hi", token="", chat_id="1", http_post=post)  # nosec B106 - empty by design
    assert post.calls == []
