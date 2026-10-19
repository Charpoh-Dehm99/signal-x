"""Tests for signalx.universe: S3 listings, normalization, classification."""

from __future__ import annotations

import json

import pytest

from signalx.universe import (
    EXCHANGE_INFO_URL,
    S3_HOST,
    UniverseError,
    build_universe,
    fetch_exchange_info,
    fetch_text,
    listing_url,
    normalize_symbol_dir,
    paginate,
    parse_common_prefixes,
    parse_exchange_info,
    parse_key_page,
)

NS = "http://s3.amazonaws.com/doc/2006-03-01/"


def page(body: str) -> str:
    if "IsTruncated" not in body:
        body += "<IsTruncated>false</IsTruncated>"
    return f'<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="{NS}">{body}</ListBucketResult>'


def common(prefix: str) -> str:
    return f"<CommonPrefixes><Prefix>{prefix}</Prefix></CommonPrefixes>"


def content(key: str) -> str:
    return f"<Contents><Key>{key}</Key><Size>1</Size></Contents>"


class FakeResponse:
    def __init__(self, status_code=200, chunks=(b"x",), text=""):
        self.status_code = status_code
        self._chunks = chunks
        self.text = text
        self.closed = False

    def iter_content(self, chunk_size=1024):
        yield from self._chunks

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0)


# ------------------------------------------------------------------- URLs


class TestListingUrl:
    def test_valid(self):
        url = listing_url("data/futures/um/monthly/klines/")
        assert url.startswith(S3_HOST + "/")
        assert "prefix=data%2Ffutures%2Fum%2Fmonthly%2Fklines%2F" in url
        assert "marker=" not in url

    def test_marker_quoted(self):
        url = listing_url("data/futures/um/monthly/klines/", "data/futures/um/monthly/klines/WMTUSDT/")
        assert "&marker=" in url
        assert " " not in url

    def test_rejects(self):
        for bad in ("data/futures/um/monthly/klines", "etc/passwd/", "data/futures/um/monthly/klines/x y/"):
            with pytest.raises(UniverseError):
                listing_url(bad)


# ---------------------------------------------------------------- fetch_text


class TestFetchText:
    def test_ok(self):
        sess = FakeSession([FakeResponse(chunks=(b"<a>", b"</a>"))])
        assert fetch_text(f"{S3_HOST}/data.binance.vision?prefix=x", session=sess) == "<a></a>"

    def test_refuses_foreign_host(self):
        with pytest.raises(UniverseError):
            fetch_text("https://evil.example/x", session=FakeSession([]))

    def test_http_error(self):
        sess = FakeSession([FakeResponse(status_code=404)])
        with pytest.raises(UniverseError):
            fetch_text(f"{S3_HOST}/data.binance.vision?prefix=x", session=sess)

    def test_retryable_then_success(self):
        sess = FakeSession([FakeResponse(status_code=503), FakeResponse(chunks=(b"ok",))])
        assert fetch_text(f"{S3_HOST}/data.binance.vision?prefix=x", session=sess) == "ok"

    def test_retries_exhausted(self):
        sess = FakeSession([FakeResponse(status_code=503)] * 3)
        with pytest.raises(UniverseError, match="after 3 attempts"):
            fetch_text(f"{S3_HOST}/data.binance.vision?prefix=x", session=sess)

    def test_size_cap(self):
        sess = FakeSession([FakeResponse(chunks=(b"a" * 1024,))])
        with pytest.raises(UniverseError):
            fetch_text(f"{S3_HOST}/data.binance.vision?prefix=x", session=sess, max_bytes=10)

    def test_non_utf8(self):
        sess = FakeSession([FakeResponse(chunks=(b"\xff\xfe\x00",))])
        with pytest.raises(UniverseError):
            fetch_text(f"{S3_HOST}/data.binance.vision?prefix=x", session=sess)


# ------------------------------------------------------------ parse pages


class TestParseCommonPrefixes:
    def test_happy(self):
        text = page(common("data/futures/um/monthly/klines/SOLUSDT/") + common("data/futures/um/monthly/klines/WIFUSDT/"))
        dirs, marker, trunc = parse_common_prefixes(text, "data/futures/um/monthly/klines/")
        assert dirs == ["SOLUSDT", "WIFUSDT"]
        assert marker == "" and trunc is False

    def test_truncated_with_marker(self):
        text = page(common("data/futures/um/monthly/klines/WMTUSDT/") + "<NextMarker>data/futures/um/monthly/klines/WMTUSDT/</NextMarker><IsTruncated>true</IsTruncated>")
        dirs, marker, trunc = parse_common_prefixes(text, "data/futures/um/monthly/klines/")
        assert trunc is True
        assert marker.endswith("WMTUSDT/")

    def test_malformed_xml(self):
        with pytest.raises(UniverseError):
            parse_common_prefixes("<ListBucketResult", "data/futures/um/monthly/klines/")

    def test_nested_prefix_rejected(self):
        text = page(common("data/futures/um/monthly/klines/SOLUSDT/1h/"))
        with pytest.raises(UniverseError):
            parse_common_prefixes(text, "data/futures/um/monthly/klines/")

    def test_missing_istruncated(self):
        text = f'<ListBucketResult xmlns="{NS}">' + common("data/futures/um/monthly/klines/SOLUSDT/") + "</ListBucketResult>"
        with pytest.raises(UniverseError):
            parse_common_prefixes(text, "data/futures/um/monthly/klines/")


class TestParseKeyPage:
    def test_happy_and_checksum_skipped(self):
        text = page(
            content("data/futures/um/monthly/klines/SOLUSDT/1h/SOLUSDT-1h-2024-10.zip")
            + content("data/futures/um/monthly/klines/SOLUSDT/1h/SOLUSDT-1h-2024-10.zip.CHECKSUM")
            + content("data/futures/um/monthly/klines/SOLUSDT/1h/SOLUSDT-1h-2025-01.zip")
        )
        months, _, _ = parse_key_page(text, "SOLUSDT", "1h")
        assert months == ["2024-10", "2025-01"]

    def test_wrong_label_rejected(self):
        text = page(content("data/futures/um/monthly/klines/SOLUSDT/1h/SOLUSDT-1m-2024-10.zip"))
        with pytest.raises(UniverseError):
            parse_key_page(text, "SOLUSDT", "1h")

    def test_foreign_symbol_rejected(self):
        text = page(content("data/futures/um/monthly/klines/SOLUSDT/1h/WIFUSDT-1h-2024-10.zip"))
        with pytest.raises(UniverseError):
            parse_key_page(text, "SOLUSDT", "1h")

    def test_bad_month_rejected(self):
        text = page(content("data/futures/um/monthly/klines/SOLUSDT/1h/SOLUSDT-1h-2024-13.zip"))
        with pytest.raises(UniverseError):
            parse_key_page(text, "SOLUSDT", "1h")

    def test_funding_label(self):
        text = page(content("data/futures/um/monthly/fundingRate/SOLUSDT/SOLUSDT-fundingRate-2024-10.zip"))
        months, _, _ = parse_key_page(text, "SOLUSDT", "fundingRate")
        assert months == ["2024-10"]


class TestPaginate:
    def test_two_pages(self):
        pages = [
            page(common("data/futures/um/monthly/klines/AUSDT/") + "<NextMarker>data/futures/um/monthly/klines/AUSDT/</NextMarker><IsTruncated>true</IsTruncated>"),
            page(common("data/futures/um/monthly/klines/ZUSDT/")),
        ]

        def fetch(marker):
            return pages.pop(0)

        def parse(text):
            return parse_common_prefixes(text, "data/futures/um/monthly/klines/")

        assert paginate(parse, fetch) == ["AUSDT", "ZUSDT"]

    def test_stuck_marker(self):
        text = page(common("data/futures/um/monthly/klines/AUSDT/") + "<NextMarker>data/futures/um/monthly/klines/AUSDT/</NextMarker><IsTruncated>true</IsTruncated>")
        with pytest.raises(UniverseError):
            paginate(lambda t: parse_common_prefixes(t, "data/futures/um/monthly/klines/"), lambda m: text)


# ------------------------------------------------------------- normalize


class TestNormalize:
    def test_plain(self):
        assert normalize_symbol_dir("SOLUSDT") == "SOLUSDT"

    def test_settled_variants(self):
        assert normalize_symbol_dir("AERGOUSDTSETTLED") == "AERGOUSDT"
        assert normalize_symbol_dir("AERGOUSDTSETTLEDSETTLED") == "AERGOUSDT"
        assert normalize_symbol_dir("ICPUSDT_") == "ICPUSDT"

    def test_junk_raises(self):
        for bad in ("", "no-quotes", "BTCUSDT_261225", "a" * 80):
            with pytest.raises(UniverseError):
                normalize_symbol_dir(bad)


# -------------------------------------------------------- build_universe


class TestBuildUniverse:
    def test_classification(self):
        dirs = [
            "SOLUSDT",
            "AERGOUSDT",
            "AERGOUSDTSETTLED",
            "1000LUNCBUSD",
            "BTCUSDT_261225",
            "XAUUSDT",
            "GARBAGE!!",
        ]
        info = {
            "XAUUSDT": {"contract_type": "TRADIFI_PERPETUAL", "status": "TRADING", "quote_asset": "USDT"},
            "SOLUSDT": {"contract_type": "PERPETUAL", "status": "TRADING", "quote_asset": "USDT"},
        }
        universe, excluded = build_universe(dirs, info)
        assert set(universe) == {"SOLUSDT", "AERGOUSDT"}
        assert universe["AERGOUSDT"]["dump_dir"] == "AERGOUSDT"  # plain preferred
        reasons = {raw: reason for raw, reason in excluded}
        assert reasons["XAUUSDT"] == "tradifi"
        assert reasons["1000LUNCBUSD"] == "non-usdt"
        assert reasons["BTCUSDT_261225"] == "quarterly"
        assert reasons["GARBAGE!!"] == "invalid"

    def test_settled_only_dir_kept(self):
        universe, _ = build_universe(["FOOUSDT_", "FOOUSDT"], {})
        assert universe["FOOUSDT"]["dump_dir"] == "FOOUSDT"
        universe, _ = build_universe(["FOOUSDT_"], {})
        assert universe["FOOUSDT"]["dump_dir"] == "FOOUSDT_"

    def test_delisted_kept_when_absent_from_exchangeinfo(self):
        universe, _ = build_universe(["ANTUSDT"], {})
        assert "ANTUSDT" in universe

    def test_non_perpetual_contracts_excluded(self):
        info = {"BTCUSDT": {"contract_type": "CURRENT_QUARTER", "status": "TRADING", "quote_asset": "USDT"}}
        universe, excluded = build_universe(["BTCUSDT", "BTCUSDT_261225"], info)
        assert "BTCUSDT" not in universe
        assert ("BTCUSDT", "non-perpetual") in excluded
        assert ("BTCUSDT_261225", "quarterly") in excluded


# ------------------------------------------------------- exchangeInfo


class TestExchangeInfo:
    def payload(self, entries):
        return json.dumps({"symbols": entries})

    def entry(self, symbol="SOLUSDT", contract="PERPETUAL", status="TRADING", quote="USDT"):
        return {"symbol": symbol, "contractType": contract, "status": status, "quoteAsset": quote}

    def test_parse(self):
        text = self.payload([self.entry(), self.entry("XAUUSDT", "TRADIFI_PERPETUAL")])
        info, skipped = parse_exchange_info(text)
        assert skipped == []
        assert info["SOLUSDT"]["contract_type"] == "PERPETUAL"
        assert info["XAUUSDT"]["contract_type"] == "TRADIFI_PERPETUAL"

    def test_unicode_ticker_skipped_not_fatal(self):
        text = self.payload([self.entry(), self.entry("币安人生USDT")])
        info, skipped = parse_exchange_info(text)
        assert "SOLUSDT" in info
        assert skipped == ["币安人生USDT"]

    def test_unicode_ticker_with_missing_fields_is_skipped(self):
        # Only the symbol field is needed for the skip decision; a unicode
        # ticker without contract metadata must not kill the whole parse.
        text = self.payload([{"symbol": "币安人生USDT"}])
        info, skipped = parse_exchange_info(text)
        assert info == {} and skipped == ["币安人生USDT"]

    def test_duplicate_raises(self):
        with pytest.raises(UniverseError):
            parse_exchange_info(self.payload([self.entry(), self.entry()]))

    def test_missing_field_raises(self):
        with pytest.raises(UniverseError):
            parse_exchange_info(self.payload([{"symbol": "SOLUSDT"}]))

    def test_not_json_raises(self):
        with pytest.raises(UniverseError):
            parse_exchange_info("<html>blocked</html>")

    def test_fetch_exchange_info(self):
        body = self.payload([self.entry()])
        sess = FakeSession([FakeResponse(text=body)])
        info, skipped = fetch_exchange_info(session=sess)
        assert sess.calls == [EXCHANGE_INFO_URL]
        assert "SOLUSDT" in info and skipped == []

    def test_fetch_exchange_info_http_error(self):
        with pytest.raises(UniverseError):
            fetch_exchange_info(session=FakeSession([FakeResponse(status_code=451)]))
