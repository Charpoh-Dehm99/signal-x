"""Exhaustion-conditioning filters for the bounded G0 rerun.

Thesis markers (blueprint's "one bounded iteration"):

  1. funding percentile >= 90 (trailing 30d) at trigger time
  2. trigger within 24h of the pump event (288 x 5m bars)
  3. pump size in the top decile of the pump sample

These are hypotheses extracted from the event-study data, not validated edges
(G0 already failed unconditionally). This module is pure: DataFrames in,
columns/flags/stats out. Fail loud (ConditioningError), fail closed (unknown
funding => condition False, never True).
"""

from __future__ import annotations

from typing import Callable

import pandas as pd

from .eventstudy import wilson_ci

FUNDING_COLUMNS = ("funding_time", "rate")
MS_PER_DAY = 86_400_000


class ConditioningError(RuntimeError):
    """Malformed input to the conditioning study. Never swallow this."""


def funding_percentile_at(
    funding: pd.DataFrame | None,
    at_ms: int,
    *,
    lookback_days: int = 30,
    min_ticks: int = 60,
) -> float | None:
    """Percentile (0..100) of the latest funding rate at/before `at_ms`.

    Compared against all rates in the trailing `lookback_days` window.
    Returns None when there is no usable funding history (fail closed).
    """
    if funding is None:
        return None
    if not isinstance(funding, pd.DataFrame):
        raise ConditioningError("funding must be a DataFrame or None")
    missing = [c for c in FUNDING_COLUMNS if c not in funding.columns]
    if missing:
        raise ConditioningError(f"funding: missing columns {missing}")
    if len(funding) == 0:
        return None
    if not funding["funding_time"].is_monotonic_increasing:
        raise ConditioningError("funding: funding_time must be sorted ascending")

    lo = at_ms - lookback_days * MS_PER_DAY
    mask = (funding["funding_time"] > lo) & (funding["funding_time"] <= at_ms)
    window = funding.loc[mask, "rate"]
    if len(window) < min_ticks:
        return None
    rates = window.to_numpy(dtype=float)
    last = float(rates[-1])
    return 100.0 * float((rates <= last).mean())


def top_decile_mask(values: pd.Series, *, quantile: float = 0.9) -> pd.Series:
    """Boolean mask: value >= sample `quantile` (ties included, NaN excluded)."""
    if not isinstance(values, pd.Series):
        raise ConditioningError("top_decile_mask: values must be a Series")
    if not (0.5 <= quantile < 1.0):
        raise ConditioningError("top_decile_mask: quantile must be in [0.5, 1.0)")
    clean = values.dropna()
    if clean.empty:
        return pd.Series(False, index=values.index)
    threshold = float(clean.quantile(quantile))
    return (values >= threshold) & values.notna()


def condition_events(
    events: pd.DataFrame,
    *,
    funding_fn: Callable[[str], pd.DataFrame | None],
    trigger_col: str = "trig_primary",
    bars_col: str = "primary_bars_since_event",
    net_col: str = "primary_net_funding",
    outcome_col: str = "primary_sim_outcome",
    pump_col: str = "return_24h",
    symbol_col: str = "symbol",
    event_ms_col: str = "event_ms",
    funding_lookback_days: int = 30,
    funding_min_ticks: int = 60,
    funding_threshold: float = 90.0,
    max_bars_since_event: int = 288,
    bar_ms: int = 300_000,
    pump_quantile: float = 0.9,
    extra_cost: float = 0.002,
) -> tuple[pd.DataFrame, dict]:
    """Tag every event with the thesis conditions and per-subset stats.

    Funding percentile is evaluated at the approximate trigger entry time:
    event_ms + (bars_since_event + 1) * bar_ms (the entry fills on the bar
    after the BOS signal bar). Unknown funding => condition False (fail closed).

    Returns (events_with_condition_columns, summary_stats).
    """
    for col in (symbol_col, event_ms_col, pump_col, trigger_col):
        if col not in events.columns:
            raise ConditioningError(f"condition_events: missing column {col!r}")

    out = events.copy()
    out["pump_top_decile"] = top_decile_mask(out[pump_col], quantile=pump_quantile)

    trigger = out[trigger_col].fillna(False).astype(bool)
    bars = out[bars_col] if bars_col in out.columns else pd.Series(float("nan"), index=out.index)
    out["trigger_within_24h"] = trigger & bars.notna() & (bars <= max_bars_since_event)

    pctiles = pd.Series(float("nan"), index=out.index)
    cache: dict[str, pd.DataFrame | None] = {}
    for idx in out.index[trigger]:
        sym = str(out.at[idx, symbol_col])
        if sym not in cache:
            cache[sym] = funding_fn(sym)
        if pd.isna(bars.at[idx]):
            raise ConditioningError(f"condition_events: triggered event without {bars_col!r}")
        entry_ms = int(out.at[idx, event_ms_col]) + (int(bars.at[idx]) + 1) * bar_ms
        value = funding_percentile_at(
            cache[sym], entry_ms,
            lookback_days=funding_lookback_days, min_ticks=funding_min_ticks,
        )
        if value is not None:
            pctiles.at[idx] = value
    out["funding_pctile"] = pctiles
    out["funding_known"] = pctiles.notna()
    out["cond_funding"] = pctiles >= funding_threshold

    out["conditioned"] = (
        trigger
        & out["cond_funding"].fillna(False)
        & out["trigger_within_24h"]
        & out["pump_top_decile"]
    )

    if net_col in out.columns:
        out["net_funding_2x"] = out[net_col] - extra_cost
    else:
        out["net_funding_2x"] = float("nan")

    summary = condition_summary(
        out,
        trigger_col=trigger_col,
        net_col=net_col,
        net2x_col="net_funding_2x",
        outcome_col=outcome_col,
    )
    return out, summary


def _subset_stats(
    sub: pd.DataFrame,
    *,
    net_col: str,
    net2x_col: str,
    outcome_col: str,
) -> dict:
    n = len(sub)
    hits = int((sub[outcome_col] == "target").sum()) if n else 0
    lo, hi = wilson_ci(hits, n)
    net = sub[net_col].dropna() if net_col in sub.columns else pd.Series(dtype=float)
    net2x = sub[net2x_col].dropna() if net2x_col in sub.columns else pd.Series(dtype=float)
    return {
        "n": n,
        "t1_hits": hits,
        "t1_share": (hits / n) if n else None,
        "wilson": (lo, hi),
        "median_net": float(net.median()) if len(net) else None,
        "mean_net": float(net.mean()) if len(net) else None,
        "mean_net_2x": float(net2x.mean()) if len(net2x) else None,
    }


def condition_summary(
    df: pd.DataFrame,
    *,
    trigger_col: str = "trig_primary",
    net_col: str = "primary_net_funding",
    net2x_col: str = "net_funding_2x",
    outcome_col: str = "primary_sim_outcome",
) -> dict:
    """Per-subset stats: each filter alone and the full conjunction."""
    trigger = df[trigger_col].fillna(False).astype(bool)
    subsets = {
        "triggered": trigger,
        "funding_known": trigger & df["funding_known"].fillna(False),
        "funding_ge_p90": trigger & df["cond_funding"].fillna(False),
        "within_24h": trigger & df["trigger_within_24h"].fillna(False),
        "top_decile": trigger & df["pump_top_decile"].fillna(False),
        "conditioned": trigger & df["conditioned"].fillna(False),
    }
    return {
        name: _subset_stats(
            df[mask], net_col=net_col, net2x_col=net2x_col, outcome_col=outcome_col
        )
        for name, mask in subsets.items()
    }
