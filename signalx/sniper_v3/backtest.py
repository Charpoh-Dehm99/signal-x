"""Event-study harness for SNIPER V3.

Conservative assumptions:
- entry at the next closed 5m bar's open;
- stop-first when stop and target are both touched in the same 5m candle;
- fees + slippage charged on both entry and exit;
- no position sizing, compounding, or funding included in R.

This module does not place orders.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from statistics import mean, median
from typing import Iterable

from signalx.models import Kline

from .engine import SniperEngine
from .models import SniperDecision


@dataclass(frozen=True)
class TradeResult:
    symbol: str
    direction: str
    entry_ts: int
    exit_ts: int
    entry: float
    stop: float
    target: float
    exit: float
    outcome: str
    r_multiple: float
    mfe_r: float
    mae_r: float
    bars_held: int

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts,
            "entry": self.entry,
            "stop": self.stop,
            "target": self.target,
            "exit": self.exit,
            "outcome": self.outcome,
            "r_multiple": self.r_multiple,
            "mfe_r": self.mfe_r,
            "mae_r": self.mae_r,
            "bars_held": self.bars_held,
        }


def _fill(price: float, direction: str, bps: float, *, entering: bool) -> float:
    cost = bps / 10_000.0
    if direction == "long":
        return price * (1.0 + cost) if entering else price * (1.0 - cost)
    return price * (1.0 - cost) if entering else price * (1.0 + cost)


def simulate_trade(
    decision: SniperDecision,
    future: list[Kline],
    *,
    max_bars: int = 288,
    fee_bps: float = 5.0,
    slip_bps: float = 5.0,
) -> TradeResult | None:
    """Simulate one decision on 5m bars starting at the next bar."""
    if not future or max_bars < 1 or fee_bps < 0 or slip_bps < 0:
        return None
    bars = future[:max_bars]
    first = bars[0]
    direction = decision.direction
    entry = _fill(first.open, direction, fee_bps + slip_bps, entering=True)
    stop = decision.stop
    target = decision.tp1
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    mfe = 0.0
    mae = 0.0
    outcome = "timeout"
    exit_raw = bars[-1].close
    exit_index = len(bars) - 1
    for i, bar in enumerate(bars):
        if direction == "long":
            mfe = max(mfe, (bar.high - entry) / risk)
            mae = min(mae, (bar.low - entry) / risk)
            if bar.open <= stop:
                outcome, exit_raw, exit_index = "stop", bar.open, i
                break
            if bar.open >= target:
                outcome, exit_raw, exit_index = "tp1", bar.open, i
                break
            hit_stop = bar.low <= stop
            hit_target = bar.high >= target
        else:
            mfe = max(mfe, (entry - bar.low) / risk)
            mae = min(mae, (entry - bar.high) / risk)
            if bar.open >= stop:
                outcome, exit_raw, exit_index = "stop", bar.open, i
                break
            if bar.open <= target:
                outcome, exit_raw, exit_index = "tp1", bar.open, i
                break
            hit_stop = bar.high >= stop
            hit_target = bar.low <= target

        # Pessimistic stop-first when a bar spans both levels.
        if hit_stop and hit_target:
            outcome = "stop"
            exit_raw = stop
            exit_index = i
            break
        if hit_stop:
            outcome = "stop"
            exit_raw = stop
            exit_index = i
            break
        if hit_target:
            outcome = "tp1"
            exit_raw = target
            exit_index = i
            break

    exit_fill = _fill(exit_raw, direction, fee_bps + slip_bps, entering=False)
    pnl = exit_fill - entry if direction == "long" else entry - exit_fill
    return TradeResult(
        symbol=decision.symbol,
        direction=direction,
        entry_ts=first.open_time,
        exit_ts=bars[exit_index].close_time,
        entry=entry,
        stop=stop,
        target=target,
        exit=exit_fill,
        outcome=outcome,
        r_multiple=pnl / risk,
        mfe_r=mfe,
        mae_r=mae,
        bars_held=exit_index + 1,
    )


def replay(
    engine: SniperEngine,
    klines_1h: list[Kline],
    klines_15m: list[Kline],
    klines_5m: list[Kline],
    *,
    max_bars: int = 288,
    fee_bps: float = 5.0,
    slip_bps: float = 5.0,
    stride: int = 1,
    max_trades: int | None = None,
) -> list[TradeResult]:
    """Walk 5m closes, evaluating only with information available at that close."""
    if stride < 1:
        raise ValueError("stride must be >= 1")
    if len(klines_1h) < 50 or len(klines_15m) < 50 or len(klines_5m) < 51:
        return []
    h_times = [k.close_time for k in klines_1h]
    m_times = [k.close_time for k in klines_15m]
    trades: list[TradeResult] = []
    skip_until = 0
    for i in range(50, len(klines_5m) - 1, stride):
        if i < skip_until:
            continue
        as_of = klines_5m[i].close_time
        h_end = bisect_right(h_times, as_of)
        m_end = bisect_right(m_times, as_of)
        if h_end < 50 or m_end < 50:
            continue
        decision = engine.evaluate(
            klines_1h[:h_end],
            klines_15m[:m_end],
            klines_5m[:i + 1],
            as_of_ms=as_of,
        )
        if decision is None:
            continue
        result = simulate_trade(
            decision,
            klines_5m[i + 1:],
            max_bars=max_bars,
            fee_bps=fee_bps,
            slip_bps=slip_bps,
        )
        if result is None:
            continue
        trades.append(result)
        skip_until = i + 1 + result.bars_held
        if max_trades is not None and len(trades) >= max_trades:
            break
    return trades


def summarize(trades: Iterable[TradeResult]) -> dict:
    rows = list(trades)
    if not rows:
        return {
            "n": 0,
            "win_rate": None,
            "mean_r": None,
            "median_r": None,
            "total_r": 0.0,
            "profit_factor": None,
            "max_drawdown_r": 0.0,
            "outcomes": {},
        }
    rs = [t.r_multiple for t in rows]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    outcomes: dict[str, int] = {}
    for row in rows:
        outcomes[row.outcome] = outcomes.get(row.outcome, 0) + 1
    return {
        "n": len(rows),
        "win_rate": len(wins) / len(rows),
        "mean_r": mean(rs),
        "median_r": median(rs),
        "total_r": sum(rs),
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "max_drawdown_r": max_dd,
        "outcomes": outcomes,
    }
