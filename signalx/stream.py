"""Binance USDT-M websocket streamer core: frame parsing + cascade detection.

Pure, testable pieces only; the async connection driver lives in
scripts/stream.py. Fail-loud: malformed frames raise StreamError.

Streams used: <symbol>@kline_1m (closed-bar events) and !forceOrder@arr
(all-market liquidation orders).
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass

from .models import Kline
from .validation import ValidationError, _strict_float, validate_symbol

WS_BASE = "wss://fstream.binance.com"
FORCE_ORDER_STREAM = "!forceOrder@arr"


class StreamError(RuntimeError):
    """Malformed stream frame. Never swallow this."""


@dataclass(frozen=True)
class KlineEvent:
    symbol: str
    kline: Kline
    closed: bool


@dataclass(frozen=True)
class LiquidationOrder:
    symbol: str
    liquidated_side: str  # LONG when a long was force-closed (exchange SELL side)
    price: float
    qty: float
    ts_ms: int
    notional: float


def _f(value: object, name: str) -> float:
    try:
        return _strict_float(value, name)
    except ValidationError as exc:
        raise StreamError(str(exc)) from exc


def _pos_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StreamError(f"{name} must be a positive int")
    return value


def _sym(value: object) -> str:
    if not isinstance(value, str):
        raise StreamError("symbol must be str")
    try:
        return validate_symbol(value)
    except ValidationError as exc:
        raise StreamError(str(exc)) from exc


def parse_kline_payload(data: dict) -> KlineEvent:
    """Parse a kline event body (data of an `@kline_1m` stream frame)."""
    k = data.get("k")
    if not isinstance(k, dict):
        raise StreamError("kline frame missing 'k'")
    kline = Kline(
        open_time=_pos_int(k.get("t"), "k.t"),
        open=_f(k.get("o"), "k.o"),
        high=_f(k.get("h"), "k.h"),
        low=_f(k.get("l"), "k.l"),
        close=_f(k.get("c"), "k.c"),
        volume=_f(k.get("v"), "k.v"),
        close_time=_pos_int(k.get("T"), "k.T"),
        quote_volume=_f(k.get("q"), "k.q"),
        trades=_pos_int(k.get("n"), "k.n"),
        taker_buy_base=_f(k.get("V"), "k.V"),
        taker_buy_quote=_f(k.get("Q"), "k.Q"),
    )
    return KlineEvent(symbol=_sym(k.get("s")), kline=kline, closed=bool(k.get("x")))


def parse_liquidation_payload(data: dict) -> LiquidationOrder:
    """Parse a forceOrder event body."""
    o = data.get("o")
    if not isinstance(o, dict):
        raise StreamError("forceOrder frame missing 'o'")
    side = o.get("S")
    if side not in ("BUY", "SELL"):
        raise StreamError(f"unexpected liquidation side: {side!r}")
    price = _f(o.get("p"), "o.p")
    qty = _f(o.get("q"), "o.q")
    return LiquidationOrder(
        symbol=_sym(o.get("s")),
        liquidated_side="LONG" if side == "SELL" else "SHORT",
        price=price,
        qty=qty,
        ts_ms=_pos_int(o.get("T"), "o.T"),
        notional=price * qty,
    )


def parse_stream_message(payload: object):
    """Parse one combined-stream frame. Returns KlineEvent, LiquidationOrder or None.

    None covers subscription acks and frames without a routed event.
    """
    if not isinstance(payload, dict):
        raise StreamError("frame must be a dict")
    data = payload.get("data")
    if data is None:
        if "result" in payload:  # subscribe ack
            return None
        raise StreamError("frame missing 'data'")
    if not isinstance(data, dict):
        raise StreamError("frame 'data' must be a dict")
    event = data.get("e")
    if event == "kline":
        return parse_kline_payload(data)
    if event == "forceOrder":
        return parse_liquidation_payload(data)
    return None


class LiquidationCascadeDetector:
    """Flags >= min_count same-side liquidations for a symbol within window_ms.

    Re-fires at most once per cooldown_ms per (symbol, side).
    """

    def __init__(
        self,
        *,
        window_ms: int = 60_000,
        min_count: int = 5,
        cooldown_ms: int | None = None,
    ) -> None:
        self.window_ms = _pos_int(window_ms, "window_ms")
        self.min_count = _pos_int(min_count, "min_count")
        self.cooldown_ms = self.window_ms if cooldown_ms is None else _pos_int(
            cooldown_ms, "cooldown_ms"
        )
        self._events: dict[str, deque] = defaultdict(deque)
        self._last_fire: dict[tuple[str, str], int] = {}

    def add(self, order: LiquidationOrder) -> dict | None:
        if not isinstance(order, LiquidationOrder):
            raise StreamError("cascade detector expects a LiquidationOrder")
        dq = self._events[order.symbol]
        dq.append((order.ts_ms, order.liquidated_side, order.notional))
        cutoff = order.ts_ms - self.window_ms
        while dq and dq[0][0] < cutoff:
            dq.popleft()
        same = [e for e in dq if e[1] == order.liquidated_side]
        if len(same) < self.min_count:
            return None
        key = (order.symbol, order.liquidated_side)
        last = self._last_fire.get(key)
        if last is not None and order.ts_ms - last < self.cooldown_ms:
            return None
        self._last_fire[key] = order.ts_ms
        return {
            "symbol": order.symbol,
            "side": order.liquidated_side,
            "count": len(same),
            "notional": sum(e[2] for e in same),
            "window_ms": self.window_ms,
            "ts_ms": order.ts_ms,
        }


def backoff_delays(max_reconnects: int, *, base_s: float = 1.0, cap_s: float = 60.0) -> list[float]:
    """Reconnect schedule: base * 2**n capped, n = 0..max_reconnects-1."""
    _pos_int(max_reconnects, "max_reconnects")
    if base_s <= 0 or cap_s <= 0:
        raise StreamError("base_s and cap_s must be positive")
    return [min(cap_s, base_s * (2 ** n)) for n in range(max_reconnects)]
