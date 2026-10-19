"""Ornstein-Uhlenbeck half-life estimation via AR(1) OLS.

Model: dX_t = kappa(theta - X_t)dt + sigma dW_t
Fit:   X_t = c + phi * X_{t-1} + eps ; kappa = -ln(phi)
Half-life: t_half = ln2 / kappa

Caveat documented in research: phi is biased downward in small samples
(~ (1+3phi)/n), so treat t_half as an order of magnitude, not a constant.
"""

from __future__ import annotations

import math
import statistics


def ou_halflife(values: list[float], *, min_points: int = 50) -> dict:
    """Estimate mean-reversion half-life. Fail loud on degenerate input."""
    if len(values) < min_points:
        raise ValueError(f"need >= {min_points} observations")
    if any(v is None for v in values):
        raise ValueError("series contains None")

    x = values[:-1]
    y = values[1:]
    mx = statistics.mean(x)
    my = statistics.mean(y)
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    var = sum((a - mx) ** 2 for a in x)
    if var == 0:
        raise ValueError("degenerate series (zero variance)")

    phi = cov / var
    if phi >= 1.0 or phi <= 0.0:
        return {
            "phi": phi,
            "halflife": float("inf"),
            "mean_reverting": False,
            "theta": None,
        }

    kappa = -math.log(phi)
    t_half = math.log(2) / kappa
    c = my - phi * mx
    theta = c / (1.0 - phi)
    return {
        "phi": phi,
        "halflife": t_half,
        "mean_reverting": True,
        "theta": theta,
    }
