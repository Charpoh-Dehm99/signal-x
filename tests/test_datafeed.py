"""Tests for signalx.datafeed: URL building, checksums, zip safety, CSV parsing."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
import pytest

from signalx import datafeed
from signalx.datafeed import (
    DataFeedError,
    dump_stem,
    dump_url,
    download,
    parse_checksum,
    read_funding_csv,
    read_kline_csv,
    safe_extract_zip,
    verify_sha256,
)
from signalx.validation import ValidationError

# --------------------------------------------------------------- helpers


def kline_line(ts, o=100.0, h=101.0, low=99.0, c=100.5, v=10.0, trades=5):
    close_time = ts + 59_999
    qv = v * c
    return f"{ts},{o},{h},{low},{c},{v},{close_time},{qv},{trades},{v * 0.4},{qv * 0.4},0"


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


class TestUrls:
    def test_klines_url(self):
        assert (
            dump_url("klines", "SOLUSDT", "2025-09")
            == "https://data.binance.vision/data/futures/um/monthly/klines/SOLUSDT/1m/SOLUSDT-1m-2025-09.zip"
        )

    def test_funding_url(self):
        assert (
            dump_url("funding", "SOLUSDT", "2025-09")
            == "https://data.binance.vision/data/futures/um/monthly/fundingRate/SOLUSDT/SOLUSDT-fundingRate-2025-09.zip"
        )

    def test_stem(self):
        assert dump_stem("funding", "PEPEUSDT", "2024-10") == "PEPEUSDT-fundingRate-2024-10"

    @pytest.mark.parametrize(
        "symbol",
        ["solusdt", "SOL USDT", "SOL/USDT", "SOLUSDT\n", "../../etc/passwd", "", 12345, None],
    )
    def test_hostile_symbols_rejected(self, symbol):
        with pytest.raises(ValidationError):
            dump_url("klines", symbol, "2025-09")

    @pytest.mark.parametrize("month", ["2025-13", "2025-00", "25-09", "2025-9", "2025-09-01", "2025/09\x00", "", None])
    def test_bad_months_rejected(self, month):
        with pytest.raises(ValidationError):
            dump_url("klines", "SOLUSDT", month)

    def test_unknown_kind_rejected(self):
        with pytest.raises(ValidationError):
            dump_url("metrics", "SOLUSDT", "2025-09")

    def test_klines_1h_url(self):
        assert (
            dump_url("klines_1h", "SOLUSDT", "2025-09")
            == "https://data.binance.vision/data/futures/um/monthly/klines/SOLUSDT/1h/SOLUSDT-1h-2025-09.zip"
        )

    def test_daily_klines_url(self):
        assert (
            dump_url("klines_1m_daily", "SOLUSDT", "2026-10-05")
            == "https://data.binance.vision/data/futures/um/daily/klines/SOLUSDT/1m/SOLUSDT-1m-2026-10-05.zip"
        )

    @pytest.mark.parametrize("day", ["2026-10", "2026-13-01", "2026-10-32", "2026-02-30", "20261005", "", None])
    def test_bad_days_rejected(self, day):
        with pytest.raises(ValidationError):
            dump_url("klines_1m_daily", "SOLUSDT", day)

    def test_month_rejected_for_daily_kind(self):
        with pytest.raises(ValidationError):
            dump_url("klines_1m_daily", "SOLUSDT", "2026-10")

    def test_day_rejected_for_monthly_kind(self):
        with pytest.raises(ValidationError):
            dump_url("klines", "SOLUSDT", "2026-10-05")

    def test_storage_groups(self):
        from signalx.datafeed import raw_dir, parquet_path, storage_group

        assert storage_group("klines") == "klines"
        assert storage_group("klines_1m_daily") == "klines"       # daily tail joins monthly parquet
        assert storage_group("klines_1h") == "klines_1h"
        assert storage_group("funding") == "funding"
        root = Path("unused-root")
        assert raw_dir(root, "klines_1m_daily", "SOLUSDT") == root / "raw" / "klines" / "SOLUSDT"
        assert parquet_path(root, "klines_1m_daily", "SOLUSDT") == root / "parquet" / "klines" / "SOLUSDT.parquet"
        assert parquet_path(root, "klines_1h", "SOLUSDT") == root / "parquet" / "klines_1h" / "SOLUSDT.parquet"
        with pytest.raises(ValidationError):
            storage_group("nope")


# -------------------------------------------------------------- checksums


class TestChecksums:
    def test_parse_ok(self):
        digest = "a" * 64
        assert parse_checksum(f"{digest}  file.zip\n") == (digest, "file.zip")

    @pytest.mark.parametrize(
        "text",
        ["", "abc  file.zip", "A" * 64 + "  file.zip", "a" * 64, "a" * 64 + "  a b c"],
    )
    def test_parse_malformed(self, text):
        with pytest.raises(DataFeedError):
            parse_checksum(text)

    def test_verify_ok_and_mismatch(self, tmp_path: Path):
        f = tmp_path / "x.zip"
        f.write_bytes(b"hello")
        import hashlib

        digest = hashlib.sha256(b"hello").hexdigest()
        verify_sha256(f, f"{digest}  x.zip", expected_name="x.zip")
        with pytest.raises(DataFeedError, match="SHA256 mismatch"):
            verify_sha256(f, f"{'0' * 64}  x.zip")
        with pytest.raises(DataFeedError, match="names"):
            verify_sha256(f, f"{digest}  evil.zip", expected_name="x.zip")


# ------------------------------------------------------------- extraction


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return path


class TestSafeExtract:
    def test_extracts_csv(self, tmp_path: Path):
        zp = make_zip(tmp_path / "a.zip", {"good.csv": b"1,2,3"})
        out = safe_extract_zip(zp, tmp_path / "out")
        assert out == [tmp_path / "out" / "good.csv"]
        assert (tmp_path / "out" / "good.csv").read_bytes() == b"1,2,3"

    @pytest.mark.parametrize(
        "name",
        ["../evil.csv", "/etc/evil.csv", "sub/dir/evil.csv", "..\\evil.csv", ".hidden.csv", "evil.sh"],
    )
    def test_hostile_members_rejected(self, tmp_path: Path, name):
        zp = make_zip(tmp_path / "b.zip", {name: b"x"})
        with pytest.raises(DataFeedError):
            safe_extract_zip(zp, tmp_path / "out")
        assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())

    def test_too_many_members(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(datafeed, "MAX_ZIP_MEMBERS", 2)
        zp = make_zip(tmp_path / "c.zip", {"a.csv": b"1", "b.csv": b"2", "c.csv": b"3"})
        with pytest.raises(DataFeedError, match="too many"):
            safe_extract_zip(zp, tmp_path / "out")

    def test_member_too_large(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(datafeed, "MAX_MEMBER_BYTES", 4)
        zp = make_zip(tmp_path / "d.zip", {"big.csv": b"12345"})
        with pytest.raises(DataFeedError, match="too large"):
            safe_extract_zip(zp, tmp_path / "out")


# --------------------------------------------------------------- download


class TestDownload:
    def test_happy_path_atomic(self, tmp_path: Path):
        url = dump_url("klines", "SOLUSDT", "2025-09")
        sess = FakeSession([FakeResponse(200, [b"hello", b" world"])])
        dest = tmp_path / "sub" / "f.zip"
        assert download(url, dest, session=sess) is True
        assert dest.read_bytes() == b"hello world"
        assert not (tmp_path / "sub" / "f.zip.part").exists()

    def test_404_returns_false(self, tmp_path: Path):
        url = dump_url("klines", "SOLUSDT", "2019-01")
        sess = FakeSession([FakeResponse(404)])
        assert download(url, tmp_path / "f.zip", session=sess) is False
        assert not (tmp_path / "f.zip").exists()

    def test_http_error_raises_after_retries(self, tmp_path: Path):
        url = dump_url("klines", "SOLUSDT", "2025-09")
        sess = FakeSession([FakeResponse(500)] * 3)
        with pytest.raises(DataFeedError, match="HTTP 500"):
            download(url, tmp_path / "f.zip", session=sess, retries=2)

    def test_non_whitelisted_host_refused_before_request(self, tmp_path: Path):
        sess = FakeSession([])
        with pytest.raises(DataFeedError, match="non-whitelisted"):
            download("https://evil.example.com/x.zip", tmp_path / "f.zip", session=sess)
        assert sess.calls == []


# ------------------------------------------------------------- kline CSV


class TestReadKlineCsv:
    def write(self, tmp_path: Path, text: str) -> Path:
        p = tmp_path / "k.csv"
        p.write_text(text)
        return p

    def test_no_header(self, tmp_path: Path):
        ts = 1_700_000_000_000
        p = self.write(tmp_path, "\n".join([kline_line(ts), kline_line(ts + 60_000)]) + "\n")
        df = read_kline_csv(p)
        assert list(df.columns)[:6] == ["open_time", "open", "high", "low", "close", "volume"]
        assert len(df) == 2
        assert df["open_time"].dtype == "int64"
        assert df["open_time"].iloc[0] == ts

    def test_with_header_row(self, tmp_path: Path):
        ts = 1_700_000_000_000
        header = ",".join(datafeed.KLINE_COLUMNS[:12])
        p = self.write(tmp_path, header + "\n" + kline_line(ts) + "\n")
        df = read_kline_csv(p)
        assert len(df) == 1

    def test_microsecond_timestamps_normalized(self, tmp_path: Path):
        ts_us = 1_700_000_000_000_000
        line = kline_line(ts_us).replace(str(ts_us + 59_999), str(ts_us + 59_999_999))
        p = self.write(tmp_path, line + "\n")
        df = read_kline_csv(p)
        assert df["open_time"].iloc[0] == ts_us // 1000
        assert df["close_time"].iloc[0] == (ts_us + 59_999_999) // 1000

    def test_ohlc_violation_raises(self, tmp_path: Path):
        ts = 1_700_000_000_000
        p = self.write(tmp_path, f"{ts},100,99,101,100.5,10,{ts + 59999},1005,5,4,400,0\n")
        with pytest.raises(DataFeedError, match="invariants"):
            read_kline_csv(p)

    def test_non_numeric_raises(self, tmp_path: Path):
        ts = 1_700_000_000_000
        p = self.write(tmp_path, f"{ts},abc,101,99,100.5,10,{ts + 59999},1005,5,4,400,0\n")
        with pytest.raises(DataFeedError, match="non-numeric"):
            read_kline_csv(p)

    def test_duplicates_dedup_and_sorted(self, tmp_path: Path):
        ts = 1_700_000_000_000
        p = self.write(
            tmp_path,
            "\n".join([kline_line(ts + 60_000), kline_line(ts), kline_line(ts, c=100.7)]) + "\n",
        )
        df = read_kline_csv(p)
        assert list(df["open_time"]) == [ts, ts + 60_000]
        assert df["close"].iloc[0] == pytest.approx(100.7)  # keep last duplicate

    def test_too_few_columns_raises(self, tmp_path: Path):
        p = self.write(tmp_path, "1,2,3,4,5,6,7,8,9,10\n")
        with pytest.raises(DataFeedError, match="columns"):
            read_kline_csv(p)


# ----------------------------------------------------------- funding CSV


class TestReadFundingCsv:
    def test_with_header(self, tmp_path: Path):
        p = tmp_path / "f.csv"
        p.write_text(
            "calc_time,funding_interval_hours,funding_rate\n"
            "1700000000000,8,0.0001\n"
            "1700028800000,8,-0.0002\n"
        )
        df = read_funding_csv(p)
        assert list(df.columns) == ["funding_time", "interval_hours", "rate"]
        assert df["rate"].iloc[1] == pytest.approx(-0.0002)

    def test_no_header(self, tmp_path: Path):
        p = tmp_path / "f.csv"
        p.write_text("1700000000000,8,0.0001\n")
        df = read_funding_csv(p)
        assert len(df) == 1

    def test_negative_interval_raises(self, tmp_path: Path):
        p = tmp_path / "f.csv"
        p.write_text("1700000000000,-8,0.0001\n")
        with pytest.raises(DataFeedError, match="interval"):
            read_funding_csv(p)

    def test_implausible_rate_raises(self, tmp_path: Path):
        p = tmp_path / "f.csv"
        p.write_text("1700000000000,8,0.9\n")
        with pytest.raises(DataFeedError, match="implausible"):
            read_funding_csv(p)


# --------------------------------------------------------------- plan file


class TestReadPlan:
    def test_happy_dedup_sorted(self, tmp_path: Path):
        from signalx.datafeed import read_plan

        p = tmp_path / "plan.csv"
        p.write_text(
            "kind,symbol,period\n"
            "klines,SOLUSDT,2025-01\n"
            "klines_1m_daily,SOLUSDT,2026-10-05\n"
            "klines,SOLUSDT,2025-01\n"
            "funding,WIFUSDT,2025-01\n"
        )
        tasks = read_plan(p)
        assert tasks == [
            ("funding", "WIFUSDT", "2025-01"),
            ("klines", "SOLUSDT", "2025-01"),
            ("klines_1m_daily", "SOLUSDT", "2026-10-05"),
        ]

    def test_missing_file(self, tmp_path: Path):
        from signalx.datafeed import read_plan

        with pytest.raises(DataFeedError):
            read_plan(tmp_path / "nope.csv")

    def test_bad_header(self, tmp_path: Path):
        from signalx.datafeed import read_plan

        p = tmp_path / "plan.csv"
        p.write_text("symbol,period\nSOLUSDT,2025-01\n")
        with pytest.raises(DataFeedError, match="header"):
            read_plan(p)

    @pytest.mark.parametrize(
        "row",
        [
            "metrics,SOLUSDT,2025-01",
            "klines,../../etc,2025-01",
            "klines,SOLUSDT,2025-13",
            "klines_1m_daily,SOLUSDT,2025-01",
            "klines_1m_daily,SOLUSDT,2026-02-30",
        ],
    )
    def test_bad_rows(self, tmp_path: Path, row):
        from signalx.datafeed import read_plan

        p = tmp_path / "plan.csv"
        p.write_text("kind,symbol,period\n" + row + "\n")
        with pytest.raises(DataFeedError, match="line 2"):
            read_plan(p)


# --------------------------------------------------------------- parquet


class TestParquet:
    def test_roundtrip(self, tmp_path: Path):
        pytest.importorskip("pyarrow")
        df = pd.DataFrame({"open_time": [1, 2], "close": [1.5, 2.5]})
        path = tmp_path / "p" / "x.parquet"
        datafeed.write_parquet(df, path)
        back = datafeed.read_parquet(path)
        pd.testing.assert_frame_equal(df, back)
