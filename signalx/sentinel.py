"""Universe sentinel core: live snapshot -> state machine features -> signals.

Observation only: the Phase 1 G0 event study did NOT validate a short edge
(reports/phase1/G0_VERDICT.md). Sentinel output is an audited observation,
never an executed order.

Wires signalx.state_machine to the snapshot shape produced by
signalx.live.live_snapshot / signalx.feeds.poll_snapshot and emits a
SignalRecord on the ARMED -> TRIGGERED transition.

Fail-loud: malformed snapshots raise SentinelError. A missing squeeze_guard
(None) counts as blocked, matching the state machine's fail-closed contract.

rehydrate_trackers/restore_state rebuild tracker state from journal rows so a
daemon restart neither loses in-flight chains nor re-emits old signals.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .models import Signal, Side
from .state_machine import State, StateMachine
from .validation import ValidationError, _strict_float, validate_symbol

# pump_score mapping: 0.6 * clamp((change - 15) / 45) + 0.4 * clamp(vol_z / 10)
PUMP_SCORE_CHANGE_FLOOR = 15.0
PUMP_SCORE_CHANGE_SPAN = 45.0
PUMP_SCORE_VOL_SPAN = 10.0
PUMP_SCORE_CHANGE_W = 0.6
PUMP_SCORE_VOL_W = 0.4

FUNDING_EXTREME_PCTILE = 90.0
BREAKOUT_VOLUME_MULT = 1.5
STOP_BUFFER_ATR = 0.5
FALLBACK_STOP_ATR = 2.0
FALLBACK_TP_RR = 2.0
TP2_RR = 3.0

_REQUIRED_SNAPSHOT_KEYS = ("symbol", "price", "atr", "events")
_REASON_EVENTS = (
    "RESISTANCE_TOUCHED",
    "NEAR_RESISTANCE",
    "PLATEAU_ACTIVE",
    "BEARISH_DIVERGENCE",
    "MOMENTUM_DECAY",
)


class SentinelError(RuntimeError):
    """Malformed sentinel input. Never swallow this."""


def _num(value: object, name: str) -> float | None:
    if value is None:
        return None
    try:
        return _strict_float(value, name)
    except ValidationError as exc:
        raise SentinelError(str(exc)) from exc


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


def pump_score(change_24h_pct: float | None, vol_z: float | None) -> float:
    """Clamped 0..1 pump-severity blend. Missing inputs contribute 0.0."""
    change = _num(change_24h_pct, "change_24h_pct")
    vz = _num(vol_z, "vol_z")
    if change is None and vz is None:
        return 0.0
    c = 0.0 if change is None else _clamp01(
        (change - PUMP_SCORE_CHANGE_FLOOR) / PUMP_SCORE_CHANGE_SPAN
    )
    v = 0.0 if vz is None else _clamp01(vz / PUMP_SCORE_VOL_SPAN)
    return PUMP_SCORE_CHANGE_W * c + PUMP_SCORE_VOL_W * v


def _require_snapshot(snapshot: object) -> dict:
    if not isinstance(snapshot, dict):
        raise SentinelError("snapshot must be a dict")
    missing = [k for k in _REQUIRED_SNAPSHOT_KEYS if k not in snapshot]
    if missing:
        raise SentinelError(f"snapshot missing keys: {', '.join(missing)}")
    return snapshot


def features_from_snapshot(
    snapshot: dict,
    *,
    volume_mult: float = BREAKOUT_VOLUME_MULT,
    stop_buffer_atr: float = STOP_BUFFER_ATR,
) -> dict:
    """Map a live snapshot into the state machine feature contract.

    Fail-closed choices: unknown vol_ratio / vol_z -> no breakout/volume credit;
    squeeze_active None passes through as None so the state machine blocks.
    """
    snap = _require_snapshot(snapshot)
    price = _num(snap["price"], "price")
    atr = _num(snap["atr"], "atr")
    if price is None or price <= 0 or atr is None or atr <= 0:
        raise SentinelError("snapshot price/atr must be positive numbers")
    events = snap["events"]
    if not isinstance(events, list):
        raise SentinelError("snapshot events must be a list")

    swing_high = _num(snap.get("swing_high"), "swing_high")
    vol_ratio = _num(snap.get("vol_ratio"), "vol_ratio")
    funding_pctile = _num(snap.get("funding_pctile"), "funding_pctile")
    cm = snap.get("compression")

    return {
        "pump_score": pump_score(snap.get("change_24h_pct"), snap.get("vol_z")),
        "compression_qualifies": bool(isinstance(cm, dict) and cm.get("qualifies")),
        "momentum_decaying": bool(snap.get("momentum_decay")),
        "invalidation": bool(
            swing_high is not None and price > swing_high + stop_buffer_atr * atr
        ),
        "near_resistance": (
            "RESISTANCE_TOUCHED" in events or "NEAR_RESISTANCE" in events
        ),
        "has_bearish_div": bool(snap.get("has_bearish_div")),
        "funding_extreme": bool(
            funding_pctile is not None and funding_pctile >= FUNDING_EXTREME_PCTILE
        ),
        "squeeze_guard": snap.get("squeeze_active"),  # None -> blocked
        "bos_confirmed": bool(snap.get("bos_down")),
        "breakout_volume_ok": bool(
            vol_ratio is not None and vol_ratio >= volume_mult
        ),
        "trade_closed": False,
    }


def trigger_plan(snapshot: dict, *, stop_buffer_atr: float = STOP_BUFFER_ATR) -> dict:
    """Entry/stop/targets for a short trigger. Provisional until G1 backtest.

    stop  = last confirmed swing high + buffer, or entry + 2 ATR when unknown
    tp1   = measured move (compression box height) or 2R fallback
    tp2   = 3R
    """
    snap = _require_snapshot(snapshot)
    entry = _num(snap["price"], "price")
    atr = _num(snap["atr"], "atr")
    if entry is None or entry <= 0 or atr is None or atr <= 0:
        raise SentinelError("trigger_plan: price/atr must be positive")
    swing_high = _num(snap.get("swing_high"), "swing_high")
    if swing_high is None:
        stop = entry + FALLBACK_STOP_ATR * atr
        stop_source = "fallback_2atr"
    else:
        stop = swing_high + stop_buffer_atr * atr
        stop_source = "swing_high+buffer"
    risk = stop - entry
    if risk <= 0:
        raise SentinelError("trigger_plan: non-positive risk (stop below entry)")

    cm = snap.get("compression")
    height = _num(cm.get("height"), "compression.height") if isinstance(cm, dict) else None
    if height is not None and height > 0:
        tp1 = entry - height
        tp1_source = "measured_move"
    else:
        tp1 = entry - FALLBACK_TP_RR * risk
        tp1_source = "fallback_2r"
    tp2 = entry - TP2_RR * risk
    return {
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "risk": risk,
        "rr1": (entry - tp1) / risk,
        "rr2": (entry - tp2) / risk,
        "stop_source": stop_source,
        "tp1_source": tp1_source,
    }


def signal_reasons(snapshot: dict, features: dict) -> tuple[str, ...]:
    """Human-readable, auditable trigger rationale."""
    snap = _require_snapshot(snapshot)
    events = snap["events"]
    reasons = [e for e in _REASON_EVENTS if e in events]
    if features.get("funding_extreme"):
        pct = _num(snap.get("funding_pctile"), "funding_pctile")
        reasons.append(f"FUNDING_PCTILE_{pct:.0f}" if pct is not None else "FUNDING_EXTREME")
    vol_ratio = _num(snap.get("vol_ratio"), "vol_ratio")
    if vol_ratio is not None and features.get("breakout_volume_ok"):
        reasons.append(f"BREAKOUT_VOL_x{vol_ratio:.1f}")
    return tuple(reasons)


@dataclass(frozen=True)
class SignalRecord:
    symbol: str
    ts_ms: int
    entry: float
    stop: float
    tp1: float
    tp2: float
    score: float
    reasons: tuple[str, ...]


def record_to_signal(record: SignalRecord) -> Signal:
    """Adapt a sentinel record to the shared models.Signal (alerts/web)."""
    return Signal(
        symbol=record.symbol,
        side=Side.SHORT,
        entry=record.entry,
        stop=record.stop,
        tp1=record.tp1,
        tp2=record.tp2,
        score=record.score,
        reasons=record.reasons,
    )


class SymbolTracker:
    """Per-symbol state machine + signal emission. One tracker per symbol."""

    def __init__(
        self,
        symbol: str,
        *,
        pump_threshold: float = 0.6,
        plateau_timeout_bars: int = 180,
        armed_timeout_bars: int = 60,
        volume_mult: float = BREAKOUT_VOLUME_MULT,
        stop_buffer_atr: float = STOP_BUFFER_ATR,
    ) -> None:
        self.symbol = validate_symbol(symbol)
        self.volume_mult = volume_mult
        self.stop_buffer_atr = stop_buffer_atr
        self.sm = StateMachine(
            pump_threshold=pump_threshold,
            plateau_timeout_bars=plateau_timeout_bars,
            armed_timeout_bars=armed_timeout_bars,
        )
        self.last_seen_ms = 0
        self.signals_emitted = 0

    @property
    def state(self) -> State:
        return self.sm.state

    def update(self, snapshot: dict, *, now_ms: int) -> dict:
        """One evaluation step on CLOSED bars. Returns a tracker result dict."""
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms <= 0:
            raise SentinelError("now_ms must be a positive int")
        features = features_from_snapshot(
            snapshot, volume_mult=self.volume_mult, stop_buffer_atr=self.stop_buffer_atr
        )
        previous = self.sm.state
        state = self.sm.step(features)
        self.last_seen_ms = now_ms

        signal = None
        plan = None
        if previous != State.TRIGGERED and state == State.TRIGGERED:
            plan = trigger_plan(snapshot, stop_buffer_atr=self.stop_buffer_atr)
            signal = SignalRecord(
                symbol=self.symbol,
                ts_ms=now_ms,
                entry=plan["entry"],
                stop=plan["stop"],
                tp1=plan["tp1"],
                tp2=plan["tp2"],
                score=features["pump_score"],
                reasons=signal_reasons(snapshot, features),
            )
            self.signals_emitted += 1

        return {
            "symbol": self.symbol,
            "ts_ms": now_ms,
            "price": _num(snapshot["price"], "price"),
            "state": state.value,
            "previous": previous.value,
            "transitioned": state != previous,
            "features": features,
            "signal": signal,
            "plan": plan,
        }


def journal_rows(result: dict) -> list[dict]:
    """Flatten one tracker result into journal rows (transition and/or signal)."""
    rows: list[dict] = []
    base = {
        "ts_ms": result["ts_ms"],
        "symbol": result["symbol"],
        "price": result["price"],
        "score": result["features"]["pump_score"],
        "features_json": json.dumps(result["features"], sort_keys=True),
    }
    if result["transitioned"]:
        rows.append({
            **base,
            "event": "TRANSITION",
            "state": result["state"],
            "prev_state": result["previous"],
            "entry": None,
            "stop": None,
            "tp1": None,
            "tp2": None,
            "reasons": None,
        })
    sig = result["signal"]
    if sig is not None:
        rows.append({
            **base,
            "event": "SIGNAL",
            "state": "TRIGGERED",
            "prev_state": result["previous"],
            "entry": sig.entry,
            "stop": sig.stop,
            "tp1": sig.tp1,
            "tp2": sig.tp2,
            "reasons": "; ".join(sig.reasons),
        })
    return rows


STATE_BY_VALUE: dict[str, State] = {s.value: s for s in State}


def latest_journal_states(rows: list[dict]) -> dict[str, dict]:
    """Last recorded state per symbol (rows are chronological, last wins).

    Fail-loud: unknown states or malformed rows raise SentinelError — a
    corrupt journal must never silently reset a chain.
    """
    latest: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise SentinelError("journal row must be a dict")
        sym = row.get("symbol")
        state = row.get("state")
        if not isinstance(sym, str) or state not in STATE_BY_VALUE:
            raise SentinelError(f"bad journal row: symbol={sym!r} state={state!r}")
        ts = row.get("ts_ms")
        latest[sym] = {
            "state": state,
            "ts_ms": ts if isinstance(ts, int) and not isinstance(ts, bool) and ts > 0 else None,
        }
    return latest


def restore_state(
    tracker: SymbolTracker,
    state_value: str,
    *,
    ts_ms: int | None = None,
    bar_ms: int = 0,
    now_ms: int = 0,
) -> SymbolTracker:
    """Force a tracker into a previously journaled state (rehydration).

    bars_in_state is seeded from elapsed time since the recorded transition
    (timeouts stay honest after a restart) when bar_ms/now_ms are provided.
    IDLE is a no-op: fresh trackers are already IDLE.
    """
    if state_value not in STATE_BY_VALUE:
        raise SentinelError(f"unknown journaled state: {state_value!r}")
    state = STATE_BY_VALUE[state_value]
    if state == State.IDLE:
        return tracker
    tracker.sm.state = state
    if bar_ms > 0 and now_ms > 0 and isinstance(ts_ms, int) and ts_ms > 0:
        tracker.sm.bars_in_state = max(0, (now_ms - ts_ms) // bar_ms)
    return tracker


def rehydrate_trackers(
    rows: list[dict],
    symbols: list[str],
    *,
    bar_ms: int = 0,
    now_ms: int = 0,
    **tracker_kwargs,
) -> dict[str, SymbolTracker]:
    """Rebuild trackers for `symbols` from journal rows (daemon restart).

    Symbols absent from the journal (or last seen IDLE) start fresh; journal
    symbols outside `symbols` are ignored (they may have left the universe).
    """
    latest = latest_journal_states(rows)
    out: dict[str, SymbolTracker] = {}
    for sym in symbols:
        tracker = SymbolTracker(sym, **tracker_kwargs)
        info = latest.get(sym)
        if info is not None:
            restore_state(
                tracker, info["state"], ts_ms=info["ts_ms"], bar_ms=bar_ms, now_ms=now_ms
            )
        out[sym] = tracker
    return out
