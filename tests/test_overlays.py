"""Tests for signalx.overlays: drawable serialization of the engines.

Fixture: a deterministic 110-bar series (chop -> volume pump -> pullback ->
weaker higher high -> breakdown -> lower-high bounce -> decline) crafted so
every engine fires at known bars.
"""

from __future__ import annotations

import pytest

from signalx.ema import triple_ema
from signalx.overlays import MIN_BARS, OverlayError, build_overlays
from tests.helpers import MIN_MS, START_MS, mk_kline

SEGS = [
    [(1.0, 800)] * 8 + [(1.004, 1000)] * 8 + [(0.996, 900)] * 8
    + [(1.004, 1100)] * 8 + [(0.996, 850)] * 8,
    [(1.025, 30_000)] + [(1.025, 26_000 - i * 900) for i in range(19)],
    [(0.994, 5_000)] * 8,
    [(1.008, 7_000 - i * 100) for i in range(10)],
    [(0.992, 4_000)] * 14,
    [(1.01, 3_000)] * 5 + [(0.99, 3_500)] * 3 + [(0.995, 3_000)] * 10,
]


def pump_series():
    """Build klines with directional wicks so extremes are cleanly local."""
    closes: list[float] = []
    vols: list[float] = []
    for seg in SEGS:
        for mult, v in seg:
            closes.append(closes[-1] * mult if closes else 100.0)
            vols.append(v)
    out = []
    prev = 100.0
    for i, (c, v) in enumerate(zip(closes, vols)):
        o = prev
        up = c >= o
        wh, wl = (0.30, 0.05) if up else (0.05, 0.30)
        out.append(mk_kline(
            START_MS + i * MIN_MS, o,
            max(o, c) + wh, min(o, c) - wl, c, v=v,
        ))
        prev = c
    return out


@pytest.fixture(scope="module")
def ov():
    return build_overlays(pump_series())


def slowing_decline_series():
    """Dump -> bounce -> slower grind to a lower low: RSI prints a higher low."""
    mults = [1.0015] * 32 + [0.988] * 6 + [1.005] * 6 + [0.995] * 8 + [1.003] * 12
    closes: list[float] = []
    for m in mults:
        closes.append(closes[-1] * m if closes else 100.0)
    out = []
    prev = 100.0
    for i, c in enumerate(closes):
        o = prev
        up = c >= o
        wh, wl = (0.30, 0.05) if up else (0.05, 0.30)
        out.append(mk_kline(
            START_MS + i * MIN_MS, o,
            max(o, c) + wh, min(o, c) - wl, c, v=1000.0,
        ))
        prev = c
    return out


class TestShape:
    def test_payload_keys_and_alignment(self, ov):
        assert ov["n"] == 110
        assert ov["t0"] == START_MS
        assert ov["atr"] > 0
        for key in ("pivots", "structure", "zones", "rsi", "divs", "budivs", "breaks", "emas", "ema_crosses", "box", "avwap"):
            assert key in ov
        assert len(ov["rsi"]) == ov["n"]
        assert len(ov["avwap"]["line"]) == ov["n"]
        assert len(ov["avwap"]["upper"]) == ov["n"]
        assert len(ov["avwap"]["lower"]) == ov["n"]

    def test_ema_stack_arrays_align_with_candles(self, ov):
        klines = pump_series()
        assert set(ov["emas"]) == {"8", "13", "21"}
        for period in ("8", "13", "21"):
            arr = ov["emas"][period]
            assert len(arr) == ov["n"]
            assert all(v is None for v in arr[: int(period) - 1])
            assert arr[-1] is not None
        res = triple_ema([k.open_time for k in klines], [k.close for k in klines])
        assert ov["emas"]["8"] == res.fast and ov["emas"]["21"] == res.slow
        assert ov["ema_crosses"] == [{"i": c.index, "dir": c.direction} for c in res.crosses]
        for cx in ov["ema_crosses"]:
            assert cx["dir"] in ("up", "down") and 0 <= cx["i"] < ov["n"]

    def test_requires_min_bars(self):
        with pytest.raises(OverlayError):
            build_overlays(pump_series()[: MIN_BARS - 1])


class TestEngines:
    def test_structure_alternates_and_labels(self, ov):
        kinds = [s["k"] for s in ov["structure"]]
        assert kinds  # zigzag found pivots
        assert all(a != b for a, b in zip(kinds, kinds[1:]))
        assert all(s["label"] in {"H", "L", "HH", "HL", "LH", "LL"} for s in ov["structure"])

    def test_break_events_classified_bos_and_choch(self, ov):
        assert ov["breaks"]
        assert {e["type"] for e in ov["breaks"]} == {"bos", "choch"}
        assert {e["dir"] for e in ov["breaks"]} == {"up", "down"}
        # the chop left a bearish label bias, so the pump's first break flips it
        assert ov["breaks"][0]["type"] == "choch" and ov["breaks"][0]["dir"] == "up"
        chops = [e for e in ov["breaks"] if e["type"] == "choch"]
        assert [e["dir"] for e in chops] == ["up", "down"]  # then the breakdown flips back

    def test_break_events_reference_real_breaks(self, ov):
        klines = pump_series()
        for ev in ov["breaks"]:
            assert ev["dir"] in {"up", "down"}
            assert ev["type"] in {"bos", "choch"}
            assert 0 <= ev["swing_i"] < ev["break_i"] <= ov["n"] - 1
            if ev["dir"] == "down":
                assert klines[ev["break_i"]].close < ev["level"]
                assert klines[ev["swing_i"]].low == pytest.approx(ev["level"], rel=1e-9)
            else:
                assert klines[ev["break_i"]].close > ev["level"]
                assert klines[ev["swing_i"]].high == pytest.approx(ev["level"], rel=1e-9)

    def test_divergence_pairing(self, ov):
        assert ov["divs"]
        for d in ov["divs"]:
            assert 5 <= d["p2i"] - d["p1i"] <= 60
            assert d["p2p"] > d["p1p"]   # price higher high
            assert d["r2v"] < d["r1v"]   # RSI lower high
            assert d["r2i"] > d["r1i"]

    def test_pump_series_has_no_bullish_divergence(self, ov):
        assert ov["budivs"] == []

    def test_bullish_divergence_pairing(self):
        ov = build_overlays(slowing_decline_series())
        assert ov["budivs"]
        for d in ov["budivs"]:
            assert 5 <= d["p2i"] - d["p1i"] <= 60
            assert d["p2p"] < d["p1p"]   # price lower low
            assert d["r2v"] > d["r1v"]   # RSI higher low
            assert d["r2i"] > d["r1i"]

    def test_zones_bounded_and_normalized(self, ov):
        assert ov["zones"]
        assert len(ov["zones"]) <= 5
        assert max(z["w"] for z in ov["zones"]) == pytest.approx(1.0)
        for z in ov["zones"]:
            assert z["lo"] <= z["hi"]
            assert 0.0 < z["w"] <= 1.0
            assert z["touches"] >= 2

    def test_avwap_anchors_at_max_volume_bar(self, ov):
        klines = pump_series()
        anchor = ov["avwap"]["anchor_i"]
        peak_vol = max(k.volume for k in klines)
        assert klines[anchor].volume == peak_vol
        assert all(v is None for v in ov["avwap"]["line"][:anchor])
        assert ov["avwap"]["line"][anchor] is not None

    def test_box_covers_last_window(self, ov):
        assert ov["box"]["start_i"] == ov["n"] - 36
        assert ov["box"]["lo"] < ov["box"]["hi"]
        assert 0.0 <= ov["box"]["containment"] <= 1.0
        assert isinstance(ov["box"]["qualifies"], bool)

    def test_swing_levels_are_last_confirmed(self, ov):
        assert ov["swing_low"] is not None and ov["swing_high"] is not None
        assert ov["swing_low"]["i"] < ov["n"]
        assert ov["swing_high"]["i"] < ov["n"]

    def test_flat_series_fails_loud_on_zero_atr(self):
        flat = [mk_kline(START_MS + i * MIN_MS, 100.0, 100.0, 100.0, 100.0) for i in range(80)]
        with pytest.raises(OverlayError):
            build_overlays(flat)
