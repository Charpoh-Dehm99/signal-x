"""Binance REST scanner with strict input validation and bounded responses.

Security properties enforced here:
- Symbols and intervals are whitelisted before any request is built.
- Request parameters are passed as a dict to the HTTP client (never
  interpolated into URLs), preventing path/query injection.
- Response size is capped; JSON parse failures and non-200 raise loudly.
- Every excluded candidate row carries a recorded reason (no silent drops).
"""

from __future__ import annotations

import requests

from .config import ScanConfig
from .validation import (
    ValidationError,
    _strict_float,
    parse_kline_row,
    validate_interval,
    validate_symbol,
)

ALLOWED_PATH_PREFIXES = ("/api/v3/", "/fapi/v1/")

MAX_RESPONSE_BYTES = 2_000_000
DEFAULT_TIMEOUT = 10.0


class ScannerError(RuntimeError):
    """Network/protocol-level failure talking to the exchange."""


class BinanceScanner:
    BASE_URL = "https://api.binance.com"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        http_get=None,
        timeout: float = DEFAULT_TIMEOUT,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        config: ScanConfig | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be >= 1")
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self._get = http_get if http_get is not None else requests.get
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.config = config or ScanConfig()

    # ------------------------------------------------------------------ HTTP
    def _request(self, path: str, params: dict | None = None):
        if not isinstance(path, str) or not path.startswith(ALLOWED_PATH_PREFIXES):
            raise ScannerError(f"path not allowed: {path!r}")
        url = f"{self.base_url}{path}"
        response = self._get(url, params=params or {}, timeout=self.timeout)
        if response.status_code != 200:
            raise ScannerError(f"HTTP {response.status_code} from {path}")
        if len(response.content) > self.max_response_bytes:
            raise ScannerError("response exceeds size cap")
        try:
            return response.json()
        except ValueError as exc:
            raise ScannerError("invalid JSON from exchange") from exc

    # ---------------------------------------------------------------- public
    def fetch_24hr(self) -> list:
        payload = self._request("/api/v3/ticker/24hr")
        if not isinstance(payload, list):
            raise ScannerError("unexpected 24hr payload shape")
        return payload

    def rank_candidates(self, payload: list) -> dict:
        """Rank gainers. Returns {candidates, skipped, rejected, scanned}.

        - rejected: rows that failed validation (recorded with reason)
        - skipped:  valid rows that did not meet thresholds (recorded with reason)
        """
        if not isinstance(payload, list):
            raise ScannerError("payload must be a list")

        cfg = self.config
        candidates: list[dict] = []
        rejected: list[dict] = []
        skipped: list[dict] = []

        for row in payload:
            if not isinstance(row, dict):
                rejected.append({"symbol": "?", "reason": "row is not an object"})
                continue
            raw_symbol = row.get("symbol")
            label = str(raw_symbol)[:32]
            try:
                symbol = validate_symbol(raw_symbol)
                change = _strict_float(row.get("priceChangePercent"), "priceChangePercent")
                quote_volume = _strict_float(row.get("quoteVolume"), "quoteVolume")
                last_price = _strict_float(row.get("lastPrice"), "lastPrice")
            except ValidationError as exc:
                rejected.append({"symbol": label, "reason": str(exc)})
                continue

            if last_price <= 0:
                rejected.append({"symbol": label, "reason": "non-positive lastPrice"})
                continue
            if not symbol.endswith(cfg.quote_asset):
                skipped.append({"symbol": symbol, "reason": "quote asset filter"})
                continue
            if change < cfg.min_change_pct:
                skipped.append({"symbol": symbol, "reason": "below change threshold"})
                continue
            if quote_volume < cfg.min_quote_volume_usdt:
                skipped.append({"symbol": symbol, "reason": "below volume threshold"})
                continue

            candidates.append({
                "symbol": symbol,
                "change_pct": change,
                "quote_volume": quote_volume,
                "last_price": last_price,
                "pump_score": change,  # placeholder until volume_z history exists
            })

        candidates.sort(key=lambda c: c["pump_score"], reverse=True)
        return {
            "candidates": candidates[: cfg.max_candidates],
            "skipped": skipped,
            "rejected": rejected,
            "scanned": len(payload),
        }

    def fetch_klines(self, symbol: str, interval: str = "1m", limit: int = 200) -> list:
        validate_symbol(symbol)
        validate_interval(interval)
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 1000):
            raise ValidationError("limit must be an int within 1..1000")

        payload = self._request(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(payload, list):
            raise ScannerError("unexpected klines payload shape")
        return [parse_kline_row(row) for row in payload]
