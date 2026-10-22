"""Paper-only broker for the SIGNAL-X companion.

No exchange client, no credentials, no order endpoints. A sentinel ``SIGNAL``
row becomes a simulated short position, managed against stop / TP1 / TP2,
a time stop and a daily-loss halt. Prices are caller-supplied so the module is
deterministic and testable.

This is deliberate: the journal is not a validated edge. The paper broker exists
to measure whether the observations survive fees and slippage before any
testnet/live discussion.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PAPER_VERSION = 1
MAX_TRADES_KEPT = 500


class PaperError(RuntimeError):
    """Invalid paper-broker input or corrupt state. Never swallowed."""


def _now_ms(value: int | None) -> int:
    if value is None:
        return int(time.time() * 1000)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PaperError("now_ms must be a positive int")
    return value


def _num(value: object, name: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PaperError(f"{name} must be a number")
    out = float(value)
    if not math.isfinite(out):
        raise PaperError(f"{name} must be finite")
    if positive and out <= 0:
        raise PaperError(f"{name} must be positive")
    return out


def _day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class PaperConfig:
    """Risk, cost and lifecycle assumptions for the paper account."""

    starting_equity: float = 10_000.0
    risk_pct: float = 0.0025          # 0.25% of current realized equity per trade
    max_leverage: float = 1.0         # notional cap = equity * max_leverage
    taker_fee_bps: float = 5.0        # 0.05% per side
    slippage_bps: float = 5.0         # 0.05% adverse per fill
    daily_loss_limit_pct: float = 2.0 # stop new entries after -2% on the day
    max_hold_ms: int = 4 * 60 * 60 * 1000
    tp1_fraction: float = 0.5         # exit half at TP1, move stop to breakeven
    min_notional: float = 1.0
    max_entry_premium_pct: float = 0.5  # skip a short if price already ran this far above plan

    def __post_init__(self) -> None:
        if not math.isfinite(self.starting_equity) or self.starting_equity <= 0:
            raise PaperError("starting_equity must be positive")
        if not math.isfinite(self.risk_pct) or not (0 < self.risk_pct <= 0.05):
            raise PaperError("risk_pct must be in (0, 0.05]")
        if not math.isfinite(self.max_leverage) or self.max_leverage < 1:
            raise PaperError("max_leverage must be >= 1")
        if not math.isfinite(self.taker_fee_bps) or not (0 <= self.taker_fee_bps < 500):
            raise PaperError("taker_fee_bps must be in [0, 500)")
        if not math.isfinite(self.slippage_bps) or not (0 <= self.slippage_bps < 500):
            raise PaperError("slippage_bps must be in [0, 500)")
        if not math.isfinite(self.daily_loss_limit_pct) or not (0 < self.daily_loss_limit_pct <= 100):
            raise PaperError("daily_loss_limit_pct must be in (0, 100]")
        if isinstance(self.max_hold_ms, bool) or not isinstance(self.max_hold_ms, int) or self.max_hold_ms <= 0:
            raise PaperError("max_hold_ms must be a positive int")
        if not math.isfinite(self.tp1_fraction) or not (0 < self.tp1_fraction <= 1):
            raise PaperError("tp1_fraction must be in (0, 1]")
        if not math.isfinite(self.min_notional) or self.min_notional <= 0:
            raise PaperError("min_notional must be positive")
        if not math.isfinite(self.max_entry_premium_pct) or self.max_entry_premium_pct < 0:
            raise PaperError("max_entry_premium_pct must be >= 0")

    @property
    def fee_rate(self) -> float:
        return self.taker_fee_bps / 10_000.0

    @property
    def slip_rate(self) -> float:
        return self.slippage_bps / 10_000.0


@dataclass
class PaperPosition:
    symbol: str
    signal_ts_ms: int
    opened_ms: int
    entry_price: float
    qty: float
    remaining_qty: float
    stop: float
    original_stop: float
    tp1: float
    tp2: float
    score: float
    tp1_hit: bool = False
    last_price: float | None = None
    last_mark_ms: int | None = None

    @classmethod
    def from_dict(cls, data: object) -> "PaperPosition":
        if not isinstance(data, dict):
            raise PaperError("position must be a dict")
        try:
            return cls(
                symbol=str(data["symbol"]),
                signal_ts_ms=int(data["signal_ts_ms"]),
                opened_ms=int(data["opened_ms"]),
                entry_price=float(data["entry_price"]),
                qty=float(data["qty"]),
                remaining_qty=float(data["remaining_qty"]),
                stop=float(data["stop"]),
                original_stop=float(data["original_stop"]),
                tp1=float(data["tp1"]),
                tp2=float(data["tp2"]),
                score=float(data.get("score", 0.0)),
                tp1_hit=bool(data.get("tp1_hit", False)),
                last_price=None if data.get("last_price") is None else float(data["last_price"]),
                last_mark_ms=None if data.get("last_mark_ms") is None else int(data["last_mark_ms"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PaperError(f"bad paper position: {exc}") from exc


class PaperBroker:
    """Deterministic paper account for SHORT signal rows."""

    def __init__(self, config: PaperConfig | None = None, *, now_ms: int | None = None) -> None:
        self.config = config or PaperConfig()
        now = _now_ms(now_ms)
        self.starting_equity = self.config.starting_equity
        self.day = _day(now)
        self.day_start_equity = self.config.starting_equity
        self.equity = self.config.starting_equity
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.realized_today = 0.0
        self.positions: dict[str, PaperPosition] = {}
        self.trades: list[dict[str, Any]] = []
        self.halted = False
        self.halt_reason: str | None = None
        self.manual_halt = False
        self.last_signal_ts = 0

    # ------------------------------------------------------------ persistence
    @classmethod
    def from_state(
        cls,
        state: object,
        *,
        config: PaperConfig | None = None,
        now_ms: int | None = None,
    ) -> "PaperBroker":
        if not isinstance(state, dict):
            raise PaperError("paper state must be a dict")
        raw_cfg = state.get("config")
        cfg = config
        if cfg is None and isinstance(raw_cfg, dict):
            try:
                cfg = PaperConfig(**raw_cfg)
            except TypeError as exc:
                raise PaperError(f"bad paper config: {exc}") from exc
        cfg = cfg or PaperConfig()
        broker = cls(cfg, now_ms=now_ms)
        broker.starting_equity = _num(state.get("starting_equity", cfg.starting_equity), "starting_equity", positive=True)
        broker.equity = _num(state.get("equity", cfg.starting_equity), "equity")
        broker.realized_pnl = _num(state.get("realized_pnl", 0.0), "realized_pnl")
        broker.fees_paid = _num(state.get("fees_paid", 0.0), "fees_paid")
        broker.realized_today = _num(state.get("realized_today", 0.0), "realized_today")
        broker.day = str(state.get("day") or broker.day)
        broker.day_start_equity = _num(state.get("day_start_equity", broker.equity), "day_start_equity")
        broker.halted = bool(state.get("halted", False))
        broker.halt_reason = None if state.get("halt_reason") is None else str(state["halt_reason"])
        broker.manual_halt = bool(state.get("manual_halt", False))
        broker.last_signal_ts = int(state.get("last_signal_ts", 0) or 0)
        positions = state.get("positions", [])
        if not isinstance(positions, list):
            raise PaperError("paper state positions must be a list")
        for raw in positions:
            pos = PaperPosition.from_dict(raw)
            if pos.symbol in broker.positions:
                raise PaperError(f"duplicate paper position: {pos.symbol}")
            broker.positions[pos.symbol] = pos
        trades = state.get("trades", [])
        if not isinstance(trades, list):
            raise PaperError("paper state trades must be a list")
        broker.trades = [t for t in trades if isinstance(t, dict)][-MAX_TRADES_KEPT:]
        return broker

    def to_state(self, now_ms: int) -> dict[str, Any]:
        now = _now_ms(now_ms)
        self.roll_day(now)
        return {
            "version": PAPER_VERSION,
            "mode": "paper",
            "config": asdict(self.config),
            "starting_equity": self.starting_equity,
            "equity": self.equity,
            "realized_pnl": self.realized_pnl,
            "fees_paid": self.fees_paid,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "realized_today": self.realized_today,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "manual_halt": self.manual_halt,
            "last_signal_ts": self.last_signal_ts,
            "positions": [asdict(p) for p in self.positions.values()],
            "trades": self.trades[-MAX_TRADES_KEPT:],
            "updated_ms": now,
        }

    # ---------------------------------------------------------------- account
    def roll_day(self, now_ms: int) -> None:
        now = _now_ms(now_ms)
        day = _day(now)
        if day == self.day:
            return
        self.day = day
        self.day_start_equity = self.equity
        self.realized_today = 0.0
        self.halted = False
        self.halt_reason = None

    def set_manual_halt(self, halted: bool, reason: str = "manual halt file") -> None:
        self.manual_halt = bool(halted)
        if self.manual_halt and not self.halted:
            self.halt_reason = reason
        elif not self.manual_halt and not self.halted and self.halt_reason and "halt file" in self.halt_reason:
            self.halt_reason = None

    def can_open(self) -> tuple[bool, str]:
        if self.manual_halt:
            return False, self.halt_reason or "manual halt"
        if self.halted:
            return False, self.halt_reason or "daily loss limit"
        return True, "ok"

    def _check_daily_halt(self) -> None:
        if self.halted:
            return
        limit = self.day_start_equity * (self.config.daily_loss_limit_pct / 100.0)
        if self.realized_today <= -limit:
            self.halted = True
            self.halt_reason = (
                f"daily loss limit: {self.realized_today:+.2f} vs -{limit:.2f}"
            )

    def _unrealized(self, pos: PaperPosition) -> float:
        if pos.last_price is None:
            return 0.0
        gross = (pos.entry_price - pos.last_price) * pos.remaining_qty
        fees = (pos.entry_price + pos.last_price) * pos.remaining_qty * self.config.fee_rate
        return gross - fees

    # --------------------------------------------------------------- entries
    def open_from_signal(
        self,
        signal: object,
        mark_price: object,
        now_ms: int,
    ) -> dict[str, Any]:
        """Try to open a SHORT from a journal SIGNAL row.

        Returns ``{"opened": bool, "reason": str, ...}``. A skipped signal still
        advances ``last_signal_ts`` so the companion does not retry it forever.
        """
        now = _now_ms(now_ms)
        self.roll_day(now)
        if not isinstance(signal, dict):
            raise PaperError("signal must be a dict")
        symbol = signal.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise PaperError("signal symbol must be a non-empty string")
        ts_ms = signal.get("ts_ms")
        if isinstance(ts_ms, bool) or not isinstance(ts_ms, int) or ts_ms <= 0:
            raise PaperError("signal ts_ms must be a positive int")
        self.last_signal_ts = max(self.last_signal_ts, ts_ms)

        ok, why = self.can_open()
        if not ok:
            return {"opened": False, "symbol": symbol, "reason": why}
        if symbol in self.positions:
            return {"opened": False, "symbol": symbol, "reason": "position already open"}

        side = signal.get("side")
        if side not in (None, "SHORT"):
            return {"opened": False, "symbol": symbol, "reason": "not a SHORT signal"}
        try:
            entry_plan = _num(signal.get("entry"), "signal.entry", positive=True)
            stop = _num(signal.get("stop"), "signal.stop", positive=True)
            tp1 = _num(signal.get("tp1"), "signal.tp1", positive=True)
            tp2 = _num(signal.get("tp2"), "signal.tp2", positive=True)
            mark = _num(mark_price, "mark_price", positive=True)
            score = _num(signal.get("score", 0.0), "signal.score")
        except PaperError as exc:
            return {"opened": False, "symbol": symbol, "reason": str(exc)}

        if not (stop > entry_plan > 0):
            return {"opened": False, "symbol": symbol, "reason": "stop must be above plan entry"}
        if not (tp1 < entry_plan and tp2 <= tp1):
            return {"opened": False, "symbol": symbol, "reason": "targets must be below entry"}
        if mark <= tp1:
            return {"opened": False, "symbol": symbol, "reason": "target already reached"}
        if mark >= stop:
            return {"opened": False, "symbol": symbol, "reason": "stop already breached"}
        premium_pct = (mark / entry_plan - 1.0) * 100.0
        if premium_pct > self.config.max_entry_premium_pct:
            return {
                "opened": False,
                "symbol": symbol,
                "reason": f"entry premium {premium_pct:.3f}% > {self.config.max_entry_premium_pct:.3f}%",
            }

        entry_fill = mark * (1.0 - self.config.slip_rate)
        risk_per_unit = stop - entry_fill
        if risk_per_unit <= 0:
            return {"opened": False, "symbol": symbol, "reason": "non-positive stop distance"}
        risk_amount = self.equity * self.config.risk_pct
        qty = risk_amount / risk_per_unit
        notional_cap = self.equity * self.config.max_leverage
        if qty * entry_fill > notional_cap:
            qty = notional_cap / entry_fill
        if qty * entry_fill < self.config.min_notional:
            return {"opened": False, "symbol": symbol, "reason": "notional below minimum"}

        pos = PaperPosition(
            symbol=symbol,
            signal_ts_ms=ts_ms,
            opened_ms=now,
            entry_price=entry_fill,
            qty=qty,
            remaining_qty=qty,
            stop=stop,
            original_stop=stop,
            tp1=tp1,
            tp2=tp2,
            score=score,
            last_price=mark,
            last_mark_ms=now,
        )
        self.positions[symbol] = pos
        return {
            "opened": True,
            "symbol": symbol,
            "reason": "open",
            "entry": entry_fill,
            "qty": qty,
            "notional": entry_fill * qty,
            "risk": risk_per_unit * qty,
        }

    # ----------------------------------------------------------------- exits
    def _close(
        self,
        pos: PaperPosition,
        qty: float,
        raw_exit: float,
        now_ms: int,
        reason: str,
    ) -> dict[str, Any]:
        qty = min(float(qty), pos.remaining_qty)
        if qty <= 0:
            raise PaperError("close qty must be positive")
        exit_fill = raw_exit * (1.0 + self.config.slip_rate)
        gross = (pos.entry_price - exit_fill) * qty
        fees = (pos.entry_price + exit_fill) * qty * self.config.fee_rate
        net = gross - fees
        risk_unit = pos.original_stop - pos.entry_price
        r_multiple = (pos.entry_price - exit_fill) / risk_unit if risk_unit > 0 else 0.0

        self.equity += net
        self.realized_pnl += net
        self.realized_today += net
        self.fees_paid += fees
        pos.remaining_qty -= qty
        if pos.remaining_qty <= 1e-12:
            pos.remaining_qty = 0.0
            self.positions.pop(pos.symbol, None)

        trade = {
            "ts_ms": now_ms,
            "symbol": pos.symbol,
            "signal_ts_ms": pos.signal_ts_ms,
            "opened_ms": pos.opened_ms,
            "entry": pos.entry_price,
            "exit": exit_fill,
            "qty": qty,
            "gross": gross,
            "fees": fees,
            "net": net,
            "reason": reason,
            "r_multiple": r_multiple,
        }
        self.trades.append(trade)
        if len(self.trades) > MAX_TRADES_KEPT:
            del self.trades[:-MAX_TRADES_KEPT]
        return trade

    def mark_prices(self, prices: dict[str, float], now_ms: int) -> list[dict[str, Any]]:
        """Mark open positions and return exit fills.

        Trigger order is conservative: stop first, then TP1, then TP2/time.
        If both a stop and a target are inside one polling interval, the stop
        wins.
        """
        now = _now_ms(now_ms)
        self.roll_day(now)
        if not isinstance(prices, dict):
            raise PaperError("prices must be a dict")
        fills: list[dict[str, Any]] = []
        for symbol, pos in list(self.positions.items()):
            raw = prices.get(symbol)
            if raw is None:
                continue
            price = _num(raw, f"price[{symbol}]", positive=True)
            pos.last_price = price
            pos.last_mark_ms = now

            if price >= pos.stop:
                fills.append(self._close(pos, pos.remaining_qty, max(price, pos.stop), now, "STOP"))
                continue

            if not pos.tp1_hit and price <= pos.tp1:
                qty = pos.remaining_qty * self.config.tp1_fraction
                if qty > 0:
                    fills.append(self._close(pos, qty, min(price, pos.tp1), now, "TP1"))
                    if pos.remaining_qty > 0:
                        pos.tp1_hit = True
                        pos.stop = pos.entry_price

            if pos.remaining_qty > 0 and pos.tp1_hit and price <= pos.tp2:
                fills.append(self._close(pos, pos.remaining_qty, min(price, pos.tp2), now, "TP2"))
                continue

            if pos.remaining_qty > 0 and now - pos.opened_ms >= self.config.max_hold_ms:
                fills.append(self._close(pos, pos.remaining_qty, price, now, "TIME"))

        self._check_daily_halt()
        return fills

    # --------------------------------------------------------------- reporting
    def snapshot(self, now_ms: int) -> dict[str, Any]:
        now = _now_ms(now_ms)
        self.roll_day(now)
        unrealized = sum(self._unrealized(p) for p in self.positions.values())
        risk_used = sum((p.stop - p.entry_price) * p.remaining_qty for p in self.positions.values())
        positions = []
        for p in self.positions.values():
            row = asdict(p)
            row["unrealized_pnl"] = self._unrealized(p)
            row["age_ms"] = max(0, now - p.opened_ms)
            positions.append(row)
        return {
            "version": PAPER_VERSION,
            "mode": "paper",
            "config": asdict(self.config),
            "starting_equity": self.starting_equity,
            "equity": self.equity,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": unrealized,
            "total_equity": self.equity + unrealized,
            "fees_paid": self.fees_paid,
            "day": self.day,
            "day_start_equity": self.day_start_equity,
            "realized_today": self.realized_today,
            "halted": self.halted or self.manual_halt,
            "halt_reason": self.halt_reason,
            "manual_halt": self.manual_halt,
            "risk_used": risk_used,
            "risk_used_pct": (risk_used / self.equity * 100.0) if self.equity > 0 else 0.0,
            "positions": positions,
            "recent_trades": self.trades[-20:],
            "last_signal_ts": self.last_signal_ts,
            "updated_ms": now,
        }


def load_paper_state(
    path: str | Path,
    *,
    config: PaperConfig | None = None,
    manual_halt: bool = False,
    now_ms: int | None = None,
) -> PaperBroker | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PaperError(f"cannot read paper state: {exc}") from exc
    broker = PaperBroker.from_state(state, config=config, now_ms=now_ms)
    broker.set_manual_halt(manual_halt)
    return broker


def save_paper_state(broker: PaperBroker, path: str | Path, now_ms: int) -> None:
    if not isinstance(broker, PaperBroker):
        raise PaperError("broker must be a PaperBroker")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    payload = json.dumps(broker.to_state(now_ms), sort_keys=True, indent=2)
    try:
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise PaperError(f"cannot write paper state: {exc}") from exc


def write_paper_heartbeat(
    broker: PaperBroker,
    path: str | Path,
    now_ms: int,
    *,
    source: str = "companion",
) -> None:
    if not isinstance(broker, PaperBroker):
        raise PaperError("broker must be a PaperBroker")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts_ms": now_ms,
        "source": source,
        "mode": "paper",
        "equity": broker.equity,
        "open_positions": len(broker.positions),
        "halted": broker.halted or broker.manual_halt,
        "last_signal_ts": broker.last_signal_ts,
        "updated_ms": broker.to_state(now_ms)["updated_ms"],
    }
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise PaperError(f"cannot write paper heartbeat: {exc}") from exc
