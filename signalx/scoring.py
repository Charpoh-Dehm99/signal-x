"""Confluence scoring: weighted feature fusion -> calibrated probability.

Design: few, interpretable features; logistic form. Unknown/missing features
contribute 0 (documented policy). Non-numeric feature values raise TypeError
(no silent coercion of garbage into the model).
"""

from __future__ import annotations

import math

DEFAULT_WEIGHTS: dict[str, float] = {
    "dist_to_resistance_atr": 1.2,  # normalized 0..1 (closer = higher)
    "has_bearish_div": 1.0,
    "vol_decay_slope": 0.8,         # positive = decaying volume pushes
    "compression_score": 0.6,
    "funding_pctile": 0.5,          # normalized 0..1
    "cvd_divergence": 0.8,
    "bos_confirmed": 1.5,
    "htf_bias": 0.7,                # 1 = aligned with 4h downtrend
}

DEFAULT_BIAS = -1.1


def sigmoid(x: float) -> float:
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def score(
    features: dict,
    weights: dict[str, float] | None = None,
    bias: float = DEFAULT_BIAS,
) -> tuple[float, list[tuple[str, float]]]:
    """Return (p_breakdown, top-3 contributing features by |weight * value|)."""
    if not isinstance(features, dict):
        raise TypeError("features must be a dict")
    w = dict(DEFAULT_WEIGHTS if weights is None else weights)

    z = bias
    contribs: list[tuple[str, float]] = []
    for name, weight in w.items():
        value = features.get(name, 0.0)
        if value is None:
            value = 0.0
        if isinstance(value, bool):
            value = 1.0 if value else 0.0
        elif not isinstance(value, (int, float)):
            raise TypeError(f"feature {name!r} must be numeric, got {type(value).__name__}")
        term = weight * float(value)
        z += term
        contribs.append((name, term))

    contribs.sort(key=lambda item: abs(item[1]), reverse=True)
    return sigmoid(z), contribs[:3]
