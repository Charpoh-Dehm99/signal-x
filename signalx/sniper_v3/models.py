"""Dataclasses for the SNIPER V3 observation engine.

These types are deliberately separate from `signalx.models.Signal`: SNIPER is a
new observation path and must never be confused with the legacy short SIGNAL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Direction = Literal["long", "short"]


@dataclass(frozen=True)
class StructureView:
    bias: Literal["long", "short", "neutral"]
    strength: float
    last_high: float | None
    last_low: float | None
    close: float
    bos: Literal["up", "down"] | None = None
    choch: Literal["up", "down"] | None = None


@dataclass(frozen=True)
class ZoneView:
    low: float
    high: float
    tf: str
    touches: int
    score: float
    last_ts: int
    volume: float = 0.0

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def width(self) -> float:
        return self.high - self.low


@dataclass(frozen=True)
class SweepView:
    direction: Direction
    level: float
    extreme: float
    close: float
    index: int
    quality: float
    wick_atr: float
    reclaim_atr: float


@dataclass(frozen=True)
class DivergenceView:
    direction: Direction
    price_a: float
    price_b: float
    osc_a: float
    osc_b: float
    confirmed_index: int
    strength: float


@dataclass(frozen=True)
class TriggerView:
    direction: Direction
    close: float
    micro_bos: bool
    displacement_atr: float
    volume_ratio: float
    delta_z: float | None
    score: float
    confirmed_index: int


@dataclass(frozen=True)
class SniperGate:
    key: str
    label: str
    state: Literal["pass", "fail", "skip", "info"]
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "state": self.state,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class SniperDiagnostics:
    """Why an evaluation did or did not produce a setup.

    This is an explanation layer, not a second signal. The decision remains
    authoritative; diagnostics expose the first failed gate and the checks that
    were reached.
    """

    symbol: str
    as_of_ms: int
    status: Literal["setup", "no_setup"]
    failed_gate: str | None
    reason: str
    direction: Direction | None
    checks: tuple[SniperGate, ...]
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of_ms": self.as_of_ms,
            "status": self.status,
            "failed_gate": self.failed_gate,
            "reason": self.reason,
            "direction": self.direction,
            "checks": [c.to_dict() for c in self.checks],
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class SniperDecision:
    symbol: str
    as_of_ms: int
    direction: Direction
    entry: float
    stop: float
    tp1: float
    tp2: float
    rr: float
    confidence: float
    expected_value_r: float
    setup_score: float
    trigger_score: float
    zone_score: float
    sweep_quality: float
    divergence_strength: float
    htf_strength: float
    funding_penalty: float
    oi_penalty: float
    reasons: tuple[str, ...]
    features: dict[str, float] = field(default_factory=dict)

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "as_of_ms": self.as_of_ms,
            "direction": self.direction,
            "entry": self.entry,
            "stop": self.stop,
            "tp1": self.tp1,
            "tp2": self.tp2,
            "rr": self.rr,
            "confidence": self.confidence,
            "expected_value_r": self.expected_value_r,
            "setup_score": self.setup_score,
            "trigger_score": self.trigger_score,
            "zone_score": self.zone_score,
            "sweep_quality": self.sweep_quality,
            "divergence_strength": self.divergence_strength,
            "htf_strength": self.htf_strength,
            "funding_penalty": self.funding_penalty,
            "oi_penalty": self.oi_penalty,
            "reasons": list(self.reasons),
            "features": dict(self.features),
            "risk_per_unit": self.risk_per_unit,
        }
