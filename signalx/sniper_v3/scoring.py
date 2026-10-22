"""Small, auditable logistic ranker for SNIPER V3.

The default weights are priors, not fitted edge. Promotion requires walk-forward
out-of-sample validation; see docs/SNIPER_V3.md.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping

FEATURES: tuple[str, ...] = (
    "htf_strength",
    "zone_score",
    "sweep_quality",
    "divergence_strength",
    "trigger_score",
    "mtf_alignment",
    "rr_norm",
    "vol_ratio_norm",
    "delta_norm",
    "funding_penalty",
    "oi_penalty",
)


def _sigmoid(z: float) -> float:
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


@dataclass
class MastermindScorer:
    weights: dict[str, float] = field(
        default_factory=lambda: {
            "htf_strength": 1.40,
            "zone_score": 1.20,
            "sweep_quality": 1.30,
            "divergence_strength": 0.70,
            "trigger_score": 1.40,
            "mtf_alignment": 0.35,
            "rr_norm": 0.60,
            "vol_ratio_norm": 0.50,
            "delta_norm": 0.40,
            "funding_penalty": -1.00,
            "oi_penalty": -0.80,
        }
    )
    bias: float = -2.40
    min_confidence: float = 0.50
    min_expected_value_r: float = 0.0
    cost_r: float = 0.10
    fitted: bool = False

    def features(
        self,
        *,
        htf_strength: float,
        zone_score: float,
        sweep_quality: float,
        divergence_strength: float,
        trigger_score: float,
        rr: float,
        mtf_alignment: float = 0.5,
        volume_ratio: float,
        delta_z: float | None,
        funding_penalty: float = 0.0,
        oi_penalty: float = 0.0,
    ) -> dict[str, float]:
        return {
            "htf_strength": _clamp(htf_strength),
            "zone_score": _clamp(zone_score),
            "sweep_quality": _clamp(sweep_quality),
            "divergence_strength": _clamp(divergence_strength),
            "trigger_score": _clamp(trigger_score),
            "mtf_alignment": _clamp(mtf_alignment),
            "rr_norm": _clamp((rr - 1.5) / 2.0),
            "vol_ratio_norm": _clamp((min(volume_ratio, 4.0) - 1.0) / 3.0),
            "delta_norm": _clamp(abs(delta_z) / 2.0) if delta_z is not None else 0.0,
            "funding_penalty": _clamp(funding_penalty),
            "oi_penalty": _clamp(oi_penalty),
        }

    def logit(self, features: Mapping[str, float]) -> float:
        return self.bias + sum(
            self.weights.get(name, 0.0) * float(features.get(name, 0.0))
            for name in FEATURES
        )

    def confidence(self, features: Mapping[str, float]) -> float:
        return _sigmoid(self.logit(features))

    def expected_value_r(self, confidence: float, rr: float) -> float:
        """Expected R per unit risk before funding; cost is charged in R."""
        return confidence * rr - (1.0 - confidence) - self.cost_r

    def score(
        self, features: Mapping[str, float], rr: float
    ) -> tuple[float, float]:
        confidence = self.confidence(features)
        return confidence, self.expected_value_r(confidence, rr)

    def fit(
        self,
        rows: Iterable[tuple[Mapping[str, float], int]],
        *,
        epochs: int = 800,
        learning_rate: float = 0.15,
        l2: float = 0.01,
    ) -> None:
        """Fit on labelled feature rows (1 = profitable, 0 = not).

        This is intentionally simple and dependency-free. It is not used by the
        live observation path unless explicitly called after out-of-sample
        validation.
        """
        data = list(rows)
        if not data:
            raise ValueError("cannot fit on empty data")
        if any(label not in (0, 1) for _, label in data):
            raise ValueError("labels must be 0 or 1")
        weights = {name: 0.0 for name in FEATURES}
        bias = 0.0
        n = float(len(data))
        for _ in range(epochs):
            grad_w = {name: 0.0 for name in FEATURES}
            grad_b = 0.0
            for features, label in data:
                z = bias + sum(
                    weights[name] * float(features.get(name, 0.0)) for name in FEATURES
                )
                err = _sigmoid(z) - float(label)
                grad_b += err
                for name in FEATURES:
                    grad_w[name] += err * float(features.get(name, 0.0))
            bias -= learning_rate * grad_b / n
            for name in FEATURES:
                weights[name] -= learning_rate * (grad_w[name] / n + l2 * weights[name])
        self.weights = weights
        self.bias = bias
        self.fitted = True
