"""Phase 1 event-study logic: pump detection, forward returns, short-outcome simulation.

All functions are pure (pandas/numpy in, plain dicts/DataFrames out) so the same
code can be replayed against live buffers later.

Fail-loud policy: malformed frames raise EventStudyError; nothing is dropped
or coerced silently.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .compression import compression_metrics
from .indicators import atr
from .models import Kline
from .pivots import fractal_pivots, last_swing

OHLCV_COLUMNS = ("open_time", "open", "high", "low", "close", "volume", "quote_volume")


class EventStudyError(RuntimeError):
    """Malformed input to the event study. Never swallow this."""


def _require_columns(df: pd.DataFrame, cols: tuple[str, ...], name: str) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise EventStudyError(f"{name}: missing columns {missing}")


# --------------------------------------------------------------- resampling

def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate 1m klines into `rule` buckets (e.g. '1h', '5min').

    Returns columns: open_time (bucket start, ms), open, high, low, close,
    volume, quote_volume. Input must be sorted by open_time ascending.
    """
    _require_columns(df, OHLCV_COLUMNS, "resample_ohlcv")
    if df.empty:
        raise EventStudyError("resample_ohlcv: empty frame")
    if not df["open_time"].is_monotonic_increasing:
        raise EventStudyError("resample_ohlcv: open_time must be sorted ascending")

    ts = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    work = df.assign(_bucket=ts.dt.floor(rule))
    g = work.groupby("_bucket", sort=True)
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "volume": g["volume"].sum(),
            "quote_volume": g["quote_volume"].sum(),
        }
    )
    out["open_time"] = out.index.astype("int64") // 10**6
    return out.reset_index(drop=True)[list(OHLCV_COLUMNS)]


def to_klines(bars: pd.DataFrame, step_ms: int | None = None) -> list[Kline]:
    """Convert a normalized OHLCV frame into signalx Kline objects."""
    _require_columns(bars, ("open_time", "open", "high", "low", "close", "volume"), "to_klines")
    if bars.empty:
        raise EventStudyError("to_klines: empty frame")
    if step_ms is None:
        if len(bars) > 1:
            step_ms = int(np.median(np.diff(bars["open_time"].to_numpy()[:100])))
        else:
            step_ms = 60_000
    out = []
    for row in bars.itertuples(index=False):
        out.append(
            Kline(
                int(row.open_time), float(row.open), float(row.high), float(row.low),
                float(row.close), float(row.volume), int(row.open_time) + step_ms - 1,
                float(getattr(row, "quote_volume", 0.0)), 0, 0.0, 0.0,
            )
        )
    return out


# ---------------------------------------------------------- pump detection

def pump_events(
    hourly: pd.DataFrame,
    *,
    return_hours: int = 24,
    return_thr: float = 0.20,
    z_window: int = 168,
    z_min_periods: int = 120,
    z_thr: float = 3.0,
    cooldown_hours: int = 48,
) -> pd.DataFrame:
    """Hourly pump events: 24h return >= thr AND volume z-score >= thr.

    Consecutive triggers within `cooldown_hours` collapse into one event
    (keeps the first). Returns columns: idx, open_time, close, return_24h, vol_z.
    """
    _require_columns(hourly, ("open_time", "close", "quote_volume"), "pump_events")
    if not hourly["open_time"].is_monotonic_increasing:
        raise EventStudyError("pump_events: open_time must be sorted ascending")
    n = len(hourly)
    close = hourly["close"].to_numpy(dtype=float)
    qv = hourly["quote_volume"].to_numpy(dtype=float)

    ret = np.full(n, np.nan)
    if n > return_hours:
        ret[return_hours:] = close[return_hours:] / close[:-return_hours] - 1.0

    roll_mean = pd.Series(qv).rolling(z_window, min_periods=z_min_periods).mean().to_numpy()
    roll_std = pd.Series(qv).rolling(z_window, min_periods=z_min_periods).std().to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (qv - roll_mean) / roll_std
    mask = (ret >= return_thr) & (z >= z_thr) & np.isfinite(z)

    cooldown_ms = cooldown_hours * 3_600_000
    rows = []
    last_ms: int | None = None
    for i in np.flatnonzero(mask):
        ms = int(hourly["open_time"].iloc[i])
        if last_ms is not None and ms - last_ms <= cooldown_ms:
            continue
        rows.append(
            {"idx": int(i), "open_time": ms, "close": float(close[i]),
             "return_24h": float(ret[i]), "vol_z": float(z[i])}
        )
        last_ms = ms
    return pd.DataFrame(rows, columns=["idx", "open_time", "close", "return_24h", "vol_z"])


def forward_returns(hourly: pd.DataFrame, idx: int, horizons: tuple[int, ...] = (1, 4, 12, 24, 48, 168)) -> dict:
    """Simple forward returns from hourly close at `idx` (None beyond data)."""
    _require_columns(hourly, ("close",), "forward_returns")
    if idx < 0 or idx >= len(hourly):
        raise EventStudyError("forward_returns: idx out of range")
    base = float(hourly["close"].iloc[idx])
    out: dict[str, float | None] = {}
    for h in horizons:
        j = idx + h
        out[f"+{h}h"] = float(hourly["close"].iloc[j]) / base - 1.0 if j < len(hourly) else None
    return out


# ---------------------------------------------------------- outcome model

def simulate_short(
    bars: pd.DataFrame,
    entry_idx: int,
    stop: float,
    target: float,
    max_bars: int,
    *,
    fee_bps: float = 5.0,
    slip_bps: float = 5.0,
    stop_first: bool = True,
) -> dict:
    """Simulate a short entered at bars.open[entry_idx], exited on stop/target/time.

    - Slippage applied at fill: sell at entry*(1-slip), buy back at stop*(1+slip)
      or target*(1-slip).
    - Fees: round-trip fee_bps charged on entry notional.
    - stop_first: when one bar touches both stop and target, stop wins (pessimistic).
    - Returns gross/net returns in fractions, MFE/MAE in fractions of entry fill.
    """
    _require_columns(bars, ("open", "high", "low", "close"), "simulate_short")
    n = len(bars)
    if not (0 <= entry_idx < n):
        raise EventStudyError("simulate_short: entry_idx out of range")
    if max_bars < 1:
        raise EventStudyError("simulate_short: max_bars must be >= 1")

    entry_raw = float(bars["open"].iloc[entry_idx])
    if stop <= entry_raw:
        raise EventStudyError(f"simulate_short: stop {stop} not above short entry {entry_raw}")
    if target >= entry_raw:
        raise EventStudyError(f"simulate_short: target {target} not below short entry {entry_raw}")

    slip = slip_bps / 10_000.0
    entry_fill = entry_raw * (1.0 - slip)

    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    closes = bars["close"].to_numpy(dtype=float)

    end = min(entry_idx + max_bars - 1, n - 1)
    outcome, exit_idx, exit_fill = "time", end, closes[end] * (1.0 + slip)
    for j in range(entry_idx, end + 1):
        stop_hit = highs[j] >= stop
        target_hit = lows[j] <= target
        if stop_hit and (target_hit is False or stop_first):
            outcome, exit_idx, exit_fill = "stop", j, stop * (1.0 + slip)
            break
        if target_hit:
            outcome, exit_idx, exit_fill = "target", j, target * (1.0 - slip)
            break

    gross = (entry_fill - exit_fill) / entry_fill
    fees = 2.0 * fee_bps / 10_000.0
    net = gross - fees

    window_high = highs[entry_idx:exit_idx + 1]
    window_low = lows[entry_idx:exit_idx + 1]
    mfe = (entry_fill - float(window_low.min())) / entry_fill
    mae = (float(window_high.max()) - entry_fill) / entry_fill

    return {
        "outcome": outcome,
        "entry_fill": entry_fill,
        "exit_fill": exit_fill,
        "gross_return": gross,
        "net_return": net,
        "mfe": mfe,
        "mae": mae,
        "bars_held": exit_idx - entry_idx + 1,
        "exit_idx": exit_idx,
    }


def funding_cost(entry_ms: int, exit_ms: int, funding: pd.DataFrame | None) -> float:
    """Net funding PnL (fraction) for a short held (entry_ms, exit_ms].

    Binance convention: positive rate => longs pay shorts => short receives +rate.
    """
    if funding is None or len(funding) == 0:
        return 0.0
    _require_columns(funding, ("funding_time", "rate"), "funding_cost")
    mask = (funding["funding_time"] > entry_ms) & (funding["funding_time"] <= exit_ms)
    return float(funding.loc[mask, "rate"].sum())


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return (0.0, 1.0)
    if not (0 <= k <= n):
        raise EventStudyError("wilson_ci: require 0 <= k <= n")
    p = k / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (float(max(0.0, center - half)), float(min(1.0, center + half)))


# ------------------------------------------------- signal chain (plateau->BOS)

def find_trigger(
    bars: pd.DataFrame,
    event_idx: int,
    *,
    atr_n: int = 14,
    pivot_k: int = 3,
    plateau_window: int = 36,
    min_dwell: int = 24,
    max_wait_bars: int = 2016,
    max_height_atr: float = 2.0,
    min_containment: float = 0.6,
    volume_mult: float = 1.5,
    stop_buffer_atr: float = 0.5,
) -> dict | None:
    """Find the first plateau -> BOS-down trigger after `event_idx` on `bars`.

    Uses only confirmed pivots (anti-lookahead) and closed bars. Returns entry,
    stop and measured-move target, or None when no trigger occurs in-window.
    """
    _require_columns(bars, OHLCV_COLUMNS, "find_trigger")
    n = len(bars)
    if not (0 <= event_idx < n):
        raise EventStudyError("find_trigger: event_idx out of range")

    klines = to_klines(bars)
    atr_series = atr(klines, atr_n)
    pivots = fractal_pivots(klines, pivot_k)

    start = max(event_idx + 1, plateau_window + 1, pivot_k + 1)
    end = min(n - 2, event_idx + max_wait_bars)
    closes = bars["close"].to_numpy(dtype=float)
    opens = bars["open"].to_numpy(dtype=float)
    highs = bars["high"].to_numpy(dtype=float)
    lows = bars["low"].to_numpy(dtype=float)
    vols = bars["volume"].to_numpy(dtype=float)
    times = bars["open_time"].to_numpy(dtype=np.int64)

    plateau_start: int | None = None
    for i in range(start, end + 1):
        a = atr_series[i]
        if a is None or a <= 0:
            continue

        cm = compression_metrics(
            klines[i + 1 - plateau_window:i + 1], plateau_window, a,
            max_height_atr=max_height_atr, min_containment=min_containment,
        )

        if plateau_start is not None and (i - plateau_start) >= min_dwell:
            swing_low = last_swing(pivots, "L", index=i)
            if swing_low is not None and closes[i] < swing_low.price:
                vol_mean = float(vols[i - 20:i].mean()) if i >= 20 else 0.0
                vol_ok = vol_mean > 0 and vols[i] >= volume_mult * vol_mean
                swing_high = last_swing(pivots, "H", index=i)
                if vol_ok and swing_high is not None and i + 1 <= n - 1:
                    entry = float(opens[i + 1])
                    stop = swing_high.price + stop_buffer_atr * a
                    prev_cm = compression_metrics(
                        klines[i - plateau_window:i], plateau_window, atr_series[i - 1] or a,
                        max_height_atr=max_height_atr, min_containment=min_containment,
                    )
                    target = entry - float(prev_cm["height"])
                    if entry < stop and target < entry:
                        return {
                            "signal_idx": i,
                            "entry_idx": i + 1,
                            "entry": entry,
                            "stop": stop,
                            "target": target,
                            "stop_dist_atr": (stop - entry) / a,
                            "range_height": float(prev_cm["height"]),
                            "vol_ratio": float(vols[i] / vol_mean),
                            "bars_since_event": i - event_idx,
                            "signal_open_time": int(times[i]),
                        }

        if cm["qualifies"]:
            if plateau_start is None:
                plateau_start = i
        else:
            plateau_start = None
    return None
