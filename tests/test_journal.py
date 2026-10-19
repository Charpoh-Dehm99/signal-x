"""Tests for signalx.journal: schema normalization, day partitions, roundtrip."""

from __future__ import annotations

from pathlib import Path

import pytest

from signalx.journal import JournalError, SignalJournal, day_str

DAY1 = 1_700_000_000_000  # 2023-11-14T22:13:20Z
DAY2 = DAY1 + 86_400_000


def row(ts=DAY1, **over):
    base = {
        "ts_ms": ts,
        "symbol": "TESTUSDT",
        "event": "TRANSITION",
        "state": "ARMED",
        "prev_state": "PLATEAU_BUILDING",
        "price": 100.0,
        "score": 0.8,
        "entry": None,
        "stop": None,
        "tp1": None,
        "tp2": None,
        "reasons": None,
        "features_json": "{}",
    }
    base.update(over)
    return base


@pytest.fixture()
def journal(tmp_path: Path) -> SignalJournal:
    return SignalJournal(tmp_path / "journal")


class TestDayStr:
    def test_day_format(self):
        assert day_str(DAY1) == "2023-11-14"

    def test_bad_ts_raises(self):
        with pytest.raises(JournalError):
            day_str(0)
        with pytest.raises(JournalError):
            day_str(True)
        with pytest.raises(JournalError):
            day_str("now")


class TestNormalize:
    def test_non_dict_raises(self, journal):
        with pytest.raises(JournalError):
            journal.append(["nope"])

    def test_rows_must_be_list(self, journal):
        with pytest.raises(JournalError):
            journal.append(row())

    def test_unknown_column_raises(self, journal):
        with pytest.raises(JournalError):
            journal.append([row(oops=1)])

    def test_missing_ts_raises(self, journal):
        with pytest.raises(JournalError):
            journal.append([row(ts=None)])

    def test_bad_type_raises(self, journal):
        with pytest.raises(JournalError):
            journal.append([row(price="high")])
        with pytest.raises(JournalError):
            journal.append([row(ts_ms="1700000000000")])

    def test_int_float_coerced_to_float(self, journal):
        journal.append([row(price=100)])
        assert journal.read_all()[0]["price"] == 100.0


class TestAppendRead:
    def test_empty_append_is_noop(self, journal):
        assert journal.append([]) == 0
        assert journal.count() == 0

    def test_roundtrip_single_row(self, journal):
        assert journal.append([row()]) == 1
        rows = journal.read_all()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "TESTUSDT"
        assert rows[0]["state"] == "ARMED"
        assert journal.count() == 1

    def test_second_append_same_day_accumulates(self, journal):
        journal.append([row()])
        journal.append([row(state="TRIGGERED")])
        rows = journal.read_all()
        assert len(rows) == 2
        assert {r["state"] for r in rows} == {"ARMED", "TRIGGERED"}

    def test_two_days_two_partitions_in_order(self, journal):
        journal.append([row(ts=DAY2, state="TRIGGERED"), row(ts=DAY1)])
        files = sorted(p.name for p in journal.root.glob("*.parquet"))
        assert files == ["2023-11-14.parquet", "2023-11-15.parquet"]
        rows = journal.read_all()
        assert [r["ts_ms"] for r in rows] == [DAY1, DAY2]

    def test_no_tmp_files_left_behind(self, journal):
        journal.append([row()])
        assert list(journal.root.glob("*.tmp")) == []

    def test_full_float_payload_roundtrip(self, journal):
        journal.append([row(entry=100.0, stop=101.5, tp1=98.0, tp2=95.5, reasons="A; B")])
        r = journal.read_all()[0]
        assert r["stop"] == pytest.approx(101.5)
        assert r["reasons"] == "A; B"

    def test_bad_day_partition_name(self, journal):
        with pytest.raises(JournalError):
            journal.day_file("2023-1-1")
