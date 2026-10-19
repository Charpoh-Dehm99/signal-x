"""Per-symbol signal state machine.

One transition per evaluation step. Evaluations happen on closed bars only
(caller's responsibility). Squeeze guard fails closed: missing/None input
counts as "blocked".
"""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    IDLE = "IDLE"
    PUMP = "PUMP_DETECTED"
    PLATEAU = "PLATEAU_BUILDING"
    ARMED = "ARMED"
    TRIGGERED = "TRIGGERED"
    IN_TRADE = "IN_TRADE"
    CLOSED = "CLOSED"


class StateMachine:
    def __init__(
        self,
        *,
        pump_threshold: float = 0.6,
        plateau_timeout_bars: int = 180,
        armed_timeout_bars: int = 60,
    ) -> None:
        if plateau_timeout_bars < 1 or armed_timeout_bars < 1:
            raise ValueError("timeouts must be >= 1")
        self.pump_threshold = pump_threshold
        self.plateau_timeout_bars = plateau_timeout_bars
        self.armed_timeout_bars = armed_timeout_bars
        self.state = State.IDLE
        self.bars_in_state = 0
        self.history: list[tuple[State, State]] = []

    def _timeout(self, state: State) -> bool:
        if state == State.PUMP:
            return self.bars_in_state >= self.plateau_timeout_bars
        if state in (State.PLATEAU, State.ARMED):
            return self.bars_in_state >= self.armed_timeout_bars
        return False

    def _next(self, state: State, f: dict) -> State:
        if self._timeout(state):
            return State.IDLE

        if state == State.IDLE:
            if f.get("pump_score", 0.0) >= self.pump_threshold:
                return State.PUMP

        elif state == State.PUMP:
            if f.get("compression_qualifies") and f.get("momentum_decaying"):
                return State.PLATEAU

        elif state == State.PLATEAU:
            if f.get("invalidation"):
                return State.IDLE
            if f.get("near_resistance") and (f.get("has_bearish_div") or f.get("funding_extreme")):
                return State.ARMED

        elif state == State.ARMED:
            if f.get("invalidation"):
                return State.IDLE
            guard = f.get("squeeze_guard")
            guard_blocks = True if guard is None else bool(guard)  # fail closed
            if f.get("bos_confirmed") and f.get("breakout_volume_ok") and not guard_blocks:
                return State.TRIGGERED

        elif state == State.TRIGGERED:
            return State.IN_TRADE

        elif state == State.IN_TRADE:
            if f.get("trade_closed"):
                return State.CLOSED

        return state

    def step(self, features: dict) -> State:
        if not isinstance(features, dict):
            raise TypeError("features must be a dict")
        previous = self.state
        nxt = self._next(previous, features)
        if nxt != previous:
            self.history.append((previous, nxt))
            self.bars_in_state = 0
        else:
            self.bars_in_state += 1
        self.state = nxt
        return nxt
