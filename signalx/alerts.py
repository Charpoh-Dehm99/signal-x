"""Alert formatting and delivery with mandatory secret redaction.

Security properties:
- redact() strips API keys, tokens, and bearer headers from any outbound text.
- format_signal() validates the symbol and redacts the final message.
- Secrets are only loaded from the environment, never hardcoded, and never
  logged. Missing env vars raise loudly instead of sending broken alerts.
"""

from __future__ import annotations

import os
import re

from .models import Signal
from .validation import validate_symbol

REQUIRED_ALERT_ENV = ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")

_KEY_VALUE_RE = re.compile(
    r"(?i)\b(api[-_ ]?key|apikey|secret|token|authorization|bearer)\b\s*[:=]?\s*"
    r"([A-Za-z0-9+/=_\-\.]{8,})"
)
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{30,}\b")

REDACTED = "***REDACTED***"


def redact(text: str) -> str:
    """Return text with credential-shaped substrings replaced. Never logs input."""
    if not isinstance(text, str):
        raise TypeError("text must be str")
    out = _KEY_VALUE_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    out = _TELEGRAM_TOKEN_RE.sub(REDACTED, out)
    return out


def load_alert_env(env: dict | None = None) -> dict:
    """Load required secrets from environment. Raises if anything is missing."""
    source = os.environ if env is None else env
    missing = [key for key in REQUIRED_ALERT_ENV if not source.get(key)]
    if missing:
        raise RuntimeError(f"missing required alert env vars: {', '.join(missing)}")
    return {key: source[key] for key in REQUIRED_ALERT_ENV}


def format_signal(signal: Signal) -> str:
    """Build the alert text. Symbol validated, output redacted."""
    validate_symbol(signal.symbol)
    lines = [
        f"SIGNAL-X {signal.side.value} {signal.symbol}",
        f"entry {signal.entry:.8g}  stop {signal.stop:.8g}",
        f"tp1 {signal.tp1:.8g}  tp2 {signal.tp2:.8g}",
        f"score {signal.score:.2f}",
        "why: " + "; ".join(signal.reasons),
    ]
    return redact("\n".join(lines))


def send_alert(
    text: str,
    token: str,
    chat_id: str,
    *,
    http_post=None,
    timeout: float = 10.0,
) -> bool:
    """Send via Telegram. Text is redacted again here (defense in depth)."""
    import requests

    if http_post is None:
        http_post = requests.post
    if not token or not chat_id:
        raise RuntimeError("token and chat_id required")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = http_post(url, json={"chat_id": chat_id, "text": redact(text)}, timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"telegram send failed: HTTP {response.status_code}")
    return True
