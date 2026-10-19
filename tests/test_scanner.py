"""Scanner tests: injection safety, bounded responses, loud failures.

Key security assertions:
- Hostile symbols/intervals raise BEFORE any HTTP call is made.
- Request parameters are passed as a dict, never interpolated into URLs.
- Non-200, oversized, and non-JSON responses raise ScannerError.
- Every excluded row in ranking carries a recorded reason (no silent drops).
"""

from __future__ import annotations

import pytest

from signalx.config import ScanConfig
from signalx.scanner import BinanceScanner, ScannerError
from signalx.validation import ValidationError

VALID_KLINE = ["1000", "1.0", "1.5", "0.9", "1.2", "100", "1059", "120.0", "10", "60", "72.0"]


class MockResponse:
    def __init__(self, status_code=200, payload=None, content=None, bad_json=False):
        self.status_code = status_code
        self._payload = payload
        self._bad_json = bad_json
        self.content = content if content is not None else b"ok"

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class FakeHTTP:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        return self.responses.pop(0)


ROWS = [
    {"symbol": "BTCUSDT", "priceChangePercent": "20.5", "quoteVolume": "100000000", "lastPrice": "50000"},
    {"symbol": "ETHUSDT", "priceChangePercent": "5", "quoteVolume": "100000000", "lastPrice": "3000"},
    {"symbol": "SOLUSDT", "priceChangePercent": "25", "quoteVolume": "1000", "lastPrice": "100"},
    {"symbol": "BTC/USDT", "priceChangePercent": "30", "quoteVolume": "100000000", "lastPrice": "1"},
    {"symbol": "XRPUSDT", "priceChangePercent": "nan", "quoteVolume": "100000000", "lastPrice": "1"},
    {"symbol": None, "priceChangePercent": "30", "quoteVolume": "100000000", "lastPrice": "1"},
    "not a dict",
]


def test_rank_candidates_filters_and_records_reasons():
    scanner = BinanceScanner()
    result = scanner.rank_candidates(ROWS)

    assert [c["symbol"] for c in result["candidates"]] == ["BTCUSDT"]
    assert result["scanned"] == len(ROWS)

    skipped_symbols = {s["symbol"] for s in result["skipped"]}
    assert "ETHUSDT" in skipped_symbols      # below change threshold
    assert "SOLUSDT" in skipped_symbols      # below volume threshold

    rejected_symbols = {r["symbol"] for r in result["rejected"]}
    assert "BTC/USDT" in rejected_symbols    # whitelist violation
    assert "XRPUSDT" in rejected_symbols     # NaN rejected
    assert "None" in rejected_symbols
    assert "?" in rejected_symbols           # non-dict row
    assert all(r["reason"] for r in result["rejected"])


def test_rank_candidates_rejects_non_list():
    with pytest.raises(ScannerError):
        BinanceScanner().rank_candidates({"symbol": "BTCUSDT"})


def test_rank_respects_max_candidates():
    rows = [
        {"symbol": f"AAA{i}USDT", "priceChangePercent": str(20 + i),
         "quoteVolume": "200000000", "lastPrice": "1"}
        for i in range(10)
    ]
    scanner = BinanceScanner(config=ScanConfig(max_candidates=3))
    result = scanner.rank_candidates(rows)
    assert len(result["candidates"]) == 3


@pytest.mark.parametrize(
    "hostile_symbol",
    ["BTCUSDT/../../etc/passwd", "BTCUSDT&foo=bar", "BTC/USDT", "btcusdt", ""],
)
def test_fetch_klines_symbol_injection_blocked_before_http(hostile_symbol):
    fake = FakeHTTP([])
    scanner = BinanceScanner(http_get=fake)
    with pytest.raises(ValidationError):
        scanner.fetch_klines(hostile_symbol)
    assert fake.calls == []  # nothing was ever sent


def test_fetch_klines_interval_injection_blocked():
    fake = FakeHTTP([])
    scanner = BinanceScanner(http_get=fake)
    with pytest.raises(ValidationError):
        scanner.fetch_klines("BTCUSDT", interval="1m&limit=999")
    assert fake.calls == []


@pytest.mark.parametrize("bad_limit", [0, 1001, True, "10", 10.5, -5, None])
def test_fetch_klines_limit_validation(bad_limit):
    fake = FakeHTTP([])
    scanner = BinanceScanner(http_get=fake)
    with pytest.raises(ValidationError):
        scanner.fetch_klines("BTCUSDT", limit=bad_limit)
    assert fake.calls == []


def test_fetch_klines_passes_params_as_dict_not_url():
    fake = FakeHTTP([MockResponse(payload=[VALID_KLINE, VALID_KLINE])])
    scanner = BinanceScanner(http_get=fake)
    klines = scanner.fetch_klines("BTCUSDT", interval="5m", limit=2)

    assert len(klines) == 2
    call = fake.calls[0]
    assert call["url"].endswith("/api/v3/klines")
    assert "BTCUSDT" not in call["url"]          # never interpolated into the URL
    assert call["params"] == {"symbol": "BTCUSDT", "interval": "5m", "limit": 2}
    assert call["timeout"] == scanner.timeout
    assert scanner.timeout > 0


def test_fetch_klines_rejects_malformed_exchange_row():
    bad_row = list(VALID_KLINE)
    bad_row[2] = "0.1"  # high below open/close
    fake = FakeHTTP([MockResponse(payload=[bad_row])])
    with pytest.raises(ValidationError):
        BinanceScanner(http_get=fake).fetch_klines("BTCUSDT")


def test_non_200_raises():
    fake = FakeHTTP([MockResponse(status_code=451, payload={})])
    with pytest.raises(ScannerError):
        BinanceScanner(http_get=fake).fetch_24hr()


def test_invalid_json_raises():
    fake = FakeHTTP([MockResponse(bad_json=True)])
    with pytest.raises(ScannerError):
        BinanceScanner(http_get=fake).fetch_24hr()


def test_oversized_response_raises():
    fake = FakeHTTP([MockResponse(payload=[], content=b"x" * 100)])
    scanner = BinanceScanner(http_get=fake, max_response_bytes=10)
    with pytest.raises(ScannerError):
        scanner.fetch_24hr()


def test_unexpected_payload_shape_raises():
    fake = FakeHTTP([MockResponse(payload={"not": "a list"})])
    with pytest.raises(ScannerError):
        BinanceScanner(http_get=fake).fetch_24hr()


def test_disallowed_path_raises():
    scanner = BinanceScanner(http_get=FakeHTTP([]))
    with pytest.raises(ScannerError):
        scanner._request("https://evil.example.com/steal")
    with pytest.raises(ScannerError):
        scanner._request("/admin/delete")


def test_constructor_validation():
    with pytest.raises(ValueError):
        BinanceScanner(timeout=0)
    with pytest.raises(ValueError):
        BinanceScanner(max_response_bytes=0)
