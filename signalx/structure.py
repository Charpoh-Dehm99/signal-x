"""Market structure labeling and break detection."""

from __future__ import annotations

from .models import Break, Kline, LabeledPivot, Pivot


def label_structure(pivots: list[Pivot]) -> list[LabeledPivot]:
    """Label pivots HH/HL/LH/LL against the previous same-kind pivot."""
    last_h: float | None = None
    last_l: float | None = None
    out: list[LabeledPivot] = []
    for p in pivots:
        if p.kind == "H":
            if last_h is None:
                label = "H"
            elif p.price > last_h:
                label = "HH"
            else:
                label = "LH"
            last_h = p.price
        else:
            if last_l is None:
                label = "L"
            elif p.price > last_l:
                label = "HL"
            else:
                label = "LL"
            last_l = p.price
        out.append(LabeledPivot(p, label))
    return out


def detect_choch(labeled: list[LabeledPivot]) -> LabeledPivot | None:
    """Change of character (bearish): first LH after a run that printed an HH."""
    seen_hh = False
    for lp in labeled:
        if lp.label == "HH":
            seen_hh = True
        elif lp.label == "LH" and seen_hh:
            return lp
    return None


def detect_choch_events(labeled: list[LabeledPivot]) -> list[tuple[str, LabeledPivot]]:
    """All change-of-character events in order, as (direction, pivot).

    Bearish: the first LH after the run printed an HH; bullish: the first HL
    after the run printed an LL. The run resets after each event fires.
    """
    events: list[tuple[str, LabeledPivot]] = []
    seen_hh = False
    seen_ll = False
    for lp in labeled:
        if lp.label == "HH":
            seen_hh = True
        elif lp.label == "LL":
            seen_ll = True
        elif lp.label == "LH" and seen_hh:
            events.append(("down", lp))
            seen_hh = seen_ll = False
        elif lp.label == "HL" and seen_ll:
            events.append(("up", lp))
            seen_hh = seen_ll = False
    return events


def _bias_of(label: str) -> str | None:
    if label in ("HH", "HL"):
        return "up"
    if label in ("LH", "LL"):
        return "down"
    return None


def detect_break_events(
    klines: list[Kline],
    pivots: list[Pivot],
    labeled: list[LabeledPivot],
    *,
    max_events: int = 6,
) -> list[Break]:
    """Classified structure breaks: BOS (with the bias) vs CHoCH (against it).

    A break is the first close beyond the most recent confirmed swing while it
    is active (causal: a swing arms only after its confirmation bar and stays
    armed until the next same-kind swing confirms). The bias starts from the
    latest ZigZag structure label at the break bar (HH/HL = up, LH/LL = down)
    and flips on every break — so the first break against the trend is a CHoCH
    (change of character) and later same-direction breaks are BOS again.
    """
    raw: list[tuple[int, Pivot, str]] = []
    for kind, up in (("L", False), ("H", True)):
        swings = sorted(
            (p for p in pivots if p.kind == kind), key=lambda p: p.confirmed_index
        )
        for i, sw in enumerate(swings):
            end = swings[i + 1].confirmed_index if i + 1 < len(swings) else len(klines) - 1
            for bar in range(sw.confirmed_index + 1, end + 1):
                close = klines[bar].close
                if (close > sw.price) if up else (close < sw.price):
                    raw.append((bar, sw, "up" if up else "down"))
                    break
    raw.sort(key=lambda e: (e[0], e[2]))

    labels = sorted(labeled, key=lambda lp: (lp.pivot.confirmed_index, lp.pivot.index))
    li = 0
    label_bias: str | None = None
    bias: str | None = None
    out: list[Break] = []
    for bar, sw, direction in raw:
        while li < len(labels) and labels[li].pivot.confirmed_index <= bar:
            label_bias = _bias_of(labels[li].label) or label_bias
            li += 1
        side = bias if bias is not None else (label_bias or direction)
        out.append(Break(
            kind="bos" if side == direction else "choch",
            direction=direction,
            level=sw.price,
            swing_index=sw.index,
            break_index=bar,
        ))
        bias = direction
    return out[-max_events:]


def detect_bos_down(
    klines: list[Kline],
    pivots: list[Pivot],
    index: int | None = None,
) -> tuple[float, Pivot] | None:
    """Break of structure down: close below the most recent confirmed swing low.

    Only pivots confirmed at or before `index` are considered (no lookahead).
    Returns (broken_level, swing_pivot) or None.
    """
    if index is None:
        index = len(klines) - 1
    if index < 0 or index >= len(klines):
        raise ValueError("index out of range")
    lows = [p for p in pivots if p.kind == "L" and p.confirmed_index <= index]
    if not lows:
        return None
    swing = max(lows, key=lambda p: (p.confirmed_index, p.index))
    if klines[index].close < swing.price:
        return swing.price, swing
    return None
