"""Tests for the observation-throughput changes.

Covers the trigger TTL (`lookback`), the anti-chase guard, and the append-only
evaluation journal that replaced the overwritten snapshot.
"""

from __future__ import annotations

import json

import pytest

from signalx.models import Kline
from signalx.sniper_v3.evallog import EvalLog
from signalx.sniper_v3.primitives import require_atr, trigger_view

START_MS = 1_600_000_000_000
BAR_MS = 300_000


def _bars(n: int = 50, *, close: float = 100.0, volume: float = 1_000.0) -> list[Kline]:
    out: list[Kline] = []
    for i in range(n):
        open_time = START_MS + i * BAR_MS
        out.append(
            Kline(
                open_time=open_time,
                open=close,
                high=close + 0.05,
                low=close - 0.05,
                close=close,
                volume=volume,
                close_time=open_time + BAR_MS - 1,
                quote_volume=volume * close,
                trades=10,
                taker_buy_base=volume / 2.0,
                taker_buy_quote=volume * close / 2.0,
            )
        )
    return out


def _breakout_series() -> list[Kline]:
    """Quiet base, a volume break 3 bars before the end, then drift."""
    bars = _bars(50)
    i = len(bars) - 4
    bars[i] = Kline(
        START_MS + i * BAR_MS,
        100.0,
        101.2,
        99.9,
        101.0,
        5_000.0,
        START_MS + (i + 1) * BAR_MS - 1,
        505_000.0,
        10,
        2_500.0,
        252_500.0,
    )
    for j in range(i + 1, len(bars)):
        bars[j] = Kline(
            START_MS + j * BAR_MS,
            101.05,
            101.15,
            100.95,
            101.1,
            800.0,
            START_MS + (j + 1) * BAR_MS - 1,
            80_880.0,
            10,
            400.0,
            40_440.0,
        )
    return bars


def test_trigger_is_single_bar_by_default():
    bars = _breakout_series()
    view = trigger_view(bars, "long", atr_value=require_atr(bars), lookback=1)
    assert view.micro_bos is False


def test_trigger_persists_across_lookback():
    bars = _breakout_series()
    view = trigger_view(bars, "long", atr_value=require_atr(bars), lookback=12)
    assert view.micro_bos is True
    assert view.bars_since == 3
    assert view.confirmed_index == len(bars) - 4


def test_trigger_lookback_stays_causal():
    bars = _breakout_series()
    # Truncating at the break bar must report a fresh trigger, not a future one.
    view = trigger_view(
        bars[: len(bars) - 3], "long", atr_value=require_atr(bars), lookback=12
    )
    assert view.micro_bos is True
    assert view.bars_since == 0


def test_trigger_rejects_a_stale_chase():
    bars = _breakout_series()
    atr = require_atr(bars)
    view = trigger_view(
        bars, "long", atr_value=atr, lookback=12, max_chase_atr=0.1
    )
    assert view.micro_bos is False


# --------------------------------------------------------------------------
# Append-only evaluation journal
# --------------------------------------------------------------------------
def _row(symbol: str, as_of: int, gate: str | None, status: str = "no_setup") -> dict:
    return {
        "symbol": symbol,
        "as_of_ms": as_of,
        "evaluated_ms": as_of + 1_000,
        "status": status,
        "failed_gate": gate,
        "direction": "long",
        "checks": [
            {"key": "htf", "state": "pass"},
            {"key": "location", "state": "fail" if gate else "pass"},
        ],
    }


def test_evallog_appends_and_never_overwrites(tmp_path):
    log = EvalLog(tmp_path)
    log.record(_row("AAAUSDT", 1, "zone"))
    log.record(_row("AAAUSDT", 2, "sweep"))
    log.record(_row("BBBUSDT", 2, None, status="setup"))

    rows = log.rows()
    assert len(rows) == 3
    # The first row survives later writes.
    assert rows[0]["failed_gate"] == "zone"


def test_evallog_funnel_histogram(tmp_path):
    log = EvalLog(tmp_path)
    log.record(_row("AAAUSDT", 1, "zone"))
    log.record(_row("AAAUSDT", 2, "zone"))
    log.record(_row("AAAUSDT", 3, "sweep"))
    log.record(_row("AAAUSDT", 4, None, status="setup"))

    funnel = log.funnel()
    assert funnel["n"] == 4
    assert funnel["setups"] == 1
    assert funnel["setup_rate"] == pytest.approx(0.25)
    assert funnel["failed_gate"]["zone"] == 2
    assert funnel["failed_gate"]["sweep"] == 1
    assert funnel["reached_gate"]["htf"] == 4


def test_evallog_backfills_outcomes_once(tmp_path):
    log = EvalLog(tmp_path)
    bars = _bars(30, close=100.0)
    as_of = bars[0].close_time
    log.record(_row("AAAUSDT", as_of, "zone"))

    written = log.record_outcomes("AAAUSDT", bars, horizons=(3, 12))
    assert written == 1
    # Second sweep must not duplicate the row.
    assert log.record_outcomes("AAAUSDT", bars, horizons=(3, 12)) == 0

    outcomes = log.outcomes()
    assert len(outcomes) == 1
    assert outcomes[0]["fwd_bps_3"] == pytest.approx(0.0)
    assert "dir_fwd_bps_12" in outcomes[0]


def test_evallog_skips_rows_whose_horizon_has_not_elapsed(tmp_path):
    log = EvalLog(tmp_path)
    bars = _bars(10, close=100.0)
    log.record(_row("AAAUSDT", bars[-1].close_time, "zone"))
    assert log.record_outcomes("AAAUSDT", bars, horizons=(3, 12)) == 0
    assert log.outcomes() == []


def test_evallog_ignores_corrupt_lines(tmp_path):
    log = EvalLog(tmp_path)
    log.record(_row("AAAUSDT", 1, "zone"))
    with log.evals_path.open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n")
    assert len(log.rows()) == 1
