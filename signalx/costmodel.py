"""Cost/impact model from recorded book-band aggregates (observation only).

First-order model: liquidity recorded per band (quote notional within +/-0.5%,
1%, 2% of mid, top-20 lower bound) is assumed uniform in price within each
band, so a taker's fill walks outward from the best quote. Prices quoted as
distance from mid in bps.

This is deliberately simple; real level ladders / event-time books will
revise it (see reports/cost/METHOD.md). Pure functions, fail-loud.

G0 KILL stands — this informs cost realism, it is not a signal.
"""

from __future__ import annotations

from collections.abc import Sequence

DEFAULT_BAND_EDGES_BPS: tuple[float, ...] = (50.0, 100.0, 200.0)


class CostModelError(RuntimeError):
    """Bad inputs to the cost model. Never swallow this."""


def _num(value: object, name: str, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CostModelError(f"{name} must be a number")
    v = float(value)
    v_ok = v > 0.0 if positive else v >= 0.0
    if v != v or v in (float("inf"), float("-inf")) or not v_ok:
        raise CostModelError(f"{name} must be {'positive' if positive else 'non-negative'}")
    return v


def _depths(band_depths_usd: Sequence[float], band_edges_bps: Sequence[float], hs: float) -> list[float]:
    if len(band_depths_usd) != len(band_edges_bps) or not band_depths_usd:
        raise CostModelError("band depths and edges must be non-empty and equal length")
    out: list[float] = []
    prev = 0.0
    prev_edge = hs
    for i, (d, e) in enumerate(zip(band_depths_usd, band_edges_bps)):
        dv = _num(d, f"band_depths_usd[{i}]", positive=False)
        ev = _num(e, f"band_edges_bps[{i}]", positive=True)
        if ev <= prev_edge:
            raise CostModelError(f"band edge {i} ({ev}) must exceed previous edge ({prev_edge})")
        if dv < prev:
            raise CostModelError(f"band depth {i} must be non-decreasing")
        out.append(dv)
        prev = dv
        prev_edge = ev
    return out


def est_fill_cost_bps(
    notional_usd: float,
    half_spread_bps: float,
    band_depths_usd: Sequence[float],
    band_edges_bps: Sequence[float] = DEFAULT_BAND_EDGES_BPS,
) -> float | None:
    """Estimated VWAP distance from mid (bps) for a taker market order.

    Returns None when notional exceeds the deepest recorded band (not
    estimable from band aggregates alone).
    """
    n = _num(notional_usd, "notional_usd", positive=True)
    hs = _num(half_spread_bps, "half_spread_bps", positive=False)
    if hs >= band_edges_bps[0]:
        raise CostModelError("half spread must be below the first band edge")
    depths = _depths(band_depths_usd, band_edges_bps, hs)

    remaining = n
    pos = 0.0            # cumulative notional consumed
    prev_p = hs          # price distance at the start of the next segment
    acc = 0.0            # sum(price * notional)
    for depth, p_hi in zip(depths, band_edges_bps):
        seg_cap = depth - pos
        if seg_cap <= 0.0 or p_hi <= prev_p:
            continue
        if remaining <= seg_cap:
            frac = remaining / seg_cap
            p_edge = prev_p + frac * (p_hi - prev_p)
            acc += (prev_p + p_edge) / 2.0 * remaining
            return acc / n
        acc += (prev_p + p_hi) / 2.0 * seg_cap
        remaining -= seg_cap
        pos = depth
        prev_p = p_hi
    return None  # beyond recorded depth


def round_trip_cost_bps(
    notional_usd: float,
    fee_bps: float,
    hs_bid_bps: float,
    hs_ask_bps: float,
    depths_bid_usd: Sequence[float],
    depths_ask_usd: Sequence[float],
    band_edges_bps: Sequence[float] = DEFAULT_BAND_EDGES_BPS,
) -> float | None:
    """Round-trip taker cost: entry one side + exit the other, plus 2x fee.

    None when either leg exceeds the recorded depth.
    """
    fee = _num(fee_bps, "fee_bps", positive=False)
    buy = est_fill_cost_bps(notional_usd, hs_ask_bps, depths_ask_usd, band_edges_bps)
    sell = est_fill_cost_bps(notional_usd, hs_bid_bps, depths_bid_usd, band_edges_bps)
    if buy is None or sell is None:
        return None
    return 2.0 * fee + buy + sell
