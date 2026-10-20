"""Chart overlays: drawable serialization of the signalx engines.

Runs the same pure engines the sentinel uses (fractal/zigzag pivots, structure
labels, BOS/CHoCH breaks, resistance zones, AVWAP, plateau box, RSI divergence,
EMA 8/13/21 triple stack) and returns a JSON-ready payload the web chart can
draw. Observation tool only: G0 was killed 2026-10-17, these layers carry no
signal claim.

Anti-lookahead: callers must pass CLOSED klines only (fetch_klines default
drops the in-progress bar). Every index in the payload refers to a closed bar.
"""

from __future__ import annotations

from .compression import compression_metrics
from .divergence import find_bearish_divergences, find_bullish_divergences, rsi_high_pivots, rsi_low_pivots
from .ema import triple_ema
from .indicators import atr as _atr
from .indicators import avwap as _avwap
from .indicators import rsi as _rsi
from .models import Kline
from .pivots import fractal_pivots, last_swing, zigzag_pivots
from .resistance import cluster_zones
from .structure import detect_break_events, label_structure

MIN_BARS = 60


class OverlayError(RuntimeError):
    """Insufficient/invalid data for overlay construction. Never swallow."""


def _last_defined(seq: list[float | None]) -> float | None:
    return next((v for v in reversed(seq) if v is not None), None)


def _avwap_series(klines: list[Kline], anchor: int) -> tuple[list, list, list]:
    """AVWAP line and +/-1 sigma band, index-aligned (None before the anchor)."""
    values = _avwap(klines, anchor)
    pad: list[None] = [None] * anchor
    return (
        pad + [v for v, _ in values],
        pad + [v + s for v, s in values],
        pad + [v - s for v, s in values],
    )


def _div_json(divs: list[tuple]) -> list[dict]:
    return [
        {"p1i": a.index, "p1p": a.price, "p2i": b.index, "p2p": b.price,
         "r1i": c.index, "r1v": c.price, "r2i": d.index, "r2v": d.price}
        for a, b, c, d in divs
    ]


def build_overlays(
    klines: list[Kline],
    *,
    pivot_k: int = 3,
    zz_k: float = 0.6,
    atr_n: int = 14,
    plateau_window: int = 36,
    max_zones: int = 5,
    max_divs: int = 4,
    box_max_height_atr: float = 4.5,
    box_min_containment: float = 0.5,
) -> dict:
    """Serialize all chart layers for `klines` (closed bars only).

    Display truncation is intentional and part of the API contract:
    `breaks` keeps the latest 6 events, `zones` the top `max_zones` and
    `divs`/`budivs` the latest `max_divs`; RSI/EMA/price series stay aligned
    to every bar in the window.
    """
    n = len(klines)
    if n < MIN_BARS:
        raise OverlayError(f"need >= {MIN_BARS} closed klines, got {n}")

    atr_vals = _atr(klines, atr_n)
    atr_last = _last_defined(atr_vals)
    if atr_last is None or atr_last <= 0:
        raise OverlayError("ATR undefined/zero (not enough bars?)")

    fractals = fractal_pivots(klines, pivot_k)
    zigzag = zigzag_pivots(klines, zz_k, atr_values=atr_vals)
    zigzag_labeled = label_structure(zigzag)
    breaks = detect_break_events(klines, fractals, zigzag_labeled)

    zones = cluster_zones(fractals, atr_value=atr_last, now_index=n - 1)[:max_zones]
    wmax = max((z.strength for z in zones), default=0.0)

    # Ignition proxy for the AVWAP anchor: the heaviest-volume bar in view.
    # Chosen over the whole loaded window, so historical points are hindsight;
    # it is a display anchor, not a causal signal input (see anchor_mode).
    anchor = max(range(n), key=lambda i: klines[i].volume)
    line, upper, lower = _avwap_series(klines, anchor)

    cm = compression_metrics(
        klines[-plateau_window:], plateau_window, atr_last,
        max_height_atr=box_max_height_atr, min_containment=box_min_containment,
    )

    rsi_vals = _rsi([k.close for k in klines], 14)
    ema_res = triple_ema([k.open_time for k in klines], [k.close for k in klines])
    divs = find_bearish_divergences(
        [p for p in fractals if p.kind == "H"], rsi_high_pivots(rsi_vals, k=2)
    )[-max_divs:]
    budivs = find_bullish_divergences(
        [p for p in fractals if p.kind == "L"], rsi_low_pivots(rsi_vals, k=2)
    )[-max_divs:]

    swing_low = last_swing(fractals, "L")
    swing_high = last_swing(fractals, "H")

    return {
        "t0": klines[0].open_time,
        "n": n,
        "atr": atr_last,
        "pivots": [
            {"i": p.index, "p": p.price, "k": p.kind, "c": p.confirmed_index}
            for p in fractals
        ],
        "structure": [
            {"i": lp.pivot.index, "p": lp.pivot.price, "k": lp.pivot.kind,
             "label": lp.label, "c": lp.pivot.confirmed_index}
            for lp in zigzag_labeled
        ],
        "breaks": [
            {"level": b.level, "swing_i": b.swing_index, "break_i": b.break_index,
             "dir": b.direction, "type": b.kind}
            for b in breaks
        ],
        "zones": [
            {"lo": z.low, "hi": z.high, "w": (z.strength / wmax if wmax > 0 else 0.0),
             "touches": z.touches, "strength": z.strength}
            for z in zones
        ],
        "avwap": {
            "anchor_i": anchor,
            "anchor_mode": "display_max_volume",  # hindsight anchor; display only
            "line": line,
            "upper": upper,
            "lower": lower,
        },
        "box": {
            "start_i": n - plateau_window, "lo": cm["box"].low, "hi": cm["box"].high,
            "height_atr": cm["height_atr"], "containment": cm["containment"],
            "score": cm["score"], "qualifies": cm["qualifies"],
        },
        "rsi": rsi_vals,
        "emas": {"8": ema_res.fast, "13": ema_res.mid, "21": ema_res.slow},
        "ema_crosses": [{"i": c.index, "dir": c.direction} for c in ema_res.crosses],
        "divs": _div_json(divs),
        "budivs": _div_json(budivs),
        "swing_low": None if swing_low is None else {"i": swing_low.index, "p": swing_low.price},
        "swing_high": None if swing_high is None else {"i": swing_high.index, "p": swing_high.price},
    }
