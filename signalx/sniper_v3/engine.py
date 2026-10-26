"""SNIPER V3 Mastermind engine: 1H permission -> 15m location -> 5m timing."""

from __future__ import annotations

from dataclasses import dataclass

from signalx.models import Kline

from .models import SniperDecision, SniperDiagnostics, SniperGate
from .primitives import (
    SniperError,
    build_zones,
    detect_divergence,
    detect_sweep,
    nearest_zone,
    require_atr,
    structure_view,
    trigger_view,
    zone_broken,
)
from .scoring import MastermindScorer


@dataclass(frozen=True)
class SniperConfig:
    min_bars_1h: int = 50
    min_bars_15m: int = 50
    min_bars_5m: int = 50
    pivot_k: int = 2
    atr_n: int = 14
    min_htf_strength: float = 0.30
    min_zone_score: float = 0.25
    max_zone_distance_atr: float = 1.5
    zone_merge_width_atr: float = 0.35
    zone_half_life_hours: float = 12.0
    sweep_lookback: int = 3
    min_sweep_quality: float = 0.20
    divergence_ttl_bars: int = 12
    min_trigger_vol_ratio: float = 1.10
    min_trigger_score: float = 0.35
    # The 5m break persists for this many closed bars instead of having to land
    # on the exact bar being evaluated (docs: setup TTL = 12 5m bars).
    trigger_lookback_bars: int = 12
    # Reject a persisting break once price has run this far from the break close.
    max_chase_atr: float = 0.75
    # Legacy AND-gate: without a divergence, a sweep had to clear this quality.
    # Off by default so setup_score decides; flip to True to restore the old rule.
    require_divergence_for_weak_sweep: bool = False
    weak_sweep_floor: float = 0.55
    min_setup_score: float = 0.35
    stop_buffer_atr: float = 0.25
    max_stop_atr: float = 3.0
    min_rr: float = 1.5
    min_confidence: float = 0.50
    min_expected_value_r: float = 0.0
    cost_r: float = 0.10
    funding_penalty: float = 0.0
    oi_penalty: float = 0.0


class SniperEngine:
    """Stateless evaluator for a closed 1H/15m/5m snapshot.

    Stateful streaming belongs in the caller/tracker: feed closed higher
    timeframe candles first and evaluate only after a 5m candle closes.
    """

    def __init__(
        self,
        symbol: str,
        *,
        config: SniperConfig | None = None,
        scorer: MastermindScorer | None = None,
    ) -> None:
        self.symbol = symbol.upper()
        self.config = config or SniperConfig()
        self.scorer = scorer or MastermindScorer(
            min_confidence=self.config.min_confidence,
            min_expected_value_r=self.config.min_expected_value_r,
            cost_r=self.config.cost_r,
        )

    def _closed(self, klines: list[Kline], as_of_ms: int) -> list[Kline]:
        return [k for k in klines if k.close_time <= as_of_ms]

    def evaluate(
        self,
        klines_1h: list[Kline],
        klines_15m: list[Kline],
        klines_5m: list[Kline],
        *,
        as_of_ms: int | None = None,
        funding_pctile: float | None = None,
        oi_crowding: float | None = None,
    ) -> SniperDecision | None:
        """Return a decision or `None` when any gate/veto fails."""
        decision, _ = self.evaluate_detailed(
            klines_1h,
            klines_15m,
            klines_5m,
            as_of_ms=as_of_ms,
            funding_pctile=funding_pctile,
            oi_crowding=oi_crowding,
        )
        return decision

    def evaluate_detailed(
        self,
        klines_1h: list[Kline],
        klines_15m: list[Kline],
        klines_5m: list[Kline],
        *,
        as_of_ms: int | None = None,
        funding_pctile: float | None = None,
        oi_crowding: float | None = None,
    ) -> tuple[SniperDecision | None, SniperDiagnostics]:
        """Evaluate and explain the first failed gate.

        The caller must supply candles in ascending close-time order. Candles
        closing after `as_of_ms` are discarded defensively. `evaluate()` remains
        the decision-only wrapper used by existing callers.
        """
        as_of = int(as_of_ms) if as_of_ms is not None else (
            klines_5m[-1].close_time if klines_5m else 0
        )
        checks: dict[str, SniperGate] = {
            "htf": SniperGate("htf", "1H permission", "skip"),
            "location": SniperGate("location", "15m location", "skip"),
            "trigger": SniperGate("trigger", "5m trigger", "skip"),
            "risk": SniperGate("risk", "Risk / RR", "skip"),
            "score": SniperGate("score", "Confidence / EV", "skip"),
        }
        metrics: dict[str, object] = {}
        direction: str | None = None

        def mark(key: str, state: str, detail: str) -> None:
            base = checks[key]
            checks[key] = SniperGate(base.key, base.label, state, detail)  # type: ignore[arg-type]

        def finish(
            decision: SniperDecision | None = None,
            *,
            failed: str | None = None,
            reason: str = "",
        ) -> tuple[SniperDecision | None, SniperDiagnostics]:
            status = "setup" if decision is not None else "no_setup"
            return decision, SniperDiagnostics(
                symbol=self.symbol,
                as_of_ms=as_of,
                status=status,  # type: ignore[arg-type]
                failed_gate=failed,
                reason=reason,
                direction=direction,  # type: ignore[arg-type]
                checks=tuple(checks.values()),
                metrics=dict(metrics),
            )

        if not klines_5m:
            return finish(failed="bars", reason="no closed 5m candles")
        five = self._closed(klines_5m, as_of)
        fifteen = self._closed(klines_15m, as_of)
        one_h = self._closed(klines_1h, as_of)
        metrics.update(
            bars_1h=len(one_h),
            bars_15m=len(fifteen),
            bars_5m=len(five),
        )
        if (
            len(one_h) < self.config.min_bars_1h
            or len(fifteen) < self.config.min_bars_15m
            or len(five) < self.config.min_bars_5m
        ):
            return finish(
                failed="bars",
                reason=(
                    f"need >= {self.config.min_bars_1h} closed bars on each TF; "
                    f"got {len(one_h)}/{len(fifteen)}/{len(five)}"
                ),
            )

        try:
            htf = structure_view(one_h, pivot_k=self.config.pivot_k, label_window=6)
            mtf = structure_view(fifteen, pivot_k=self.config.pivot_k, label_window=6)
            atr_1h = require_atr(one_h, self.config.atr_n)
            atr_15m = require_atr(fifteen, self.config.atr_n)
            atr_5m = require_atr(five, self.config.atr_n)
        except SniperError as exc:
            return finish(failed="structure", reason=f"structure unavailable: {exc}")

        metrics.update(
            htf_bias=htf.bias,
            htf_strength=htf.strength,
            mtf_bias=mtf.bias,
            mtf_strength=mtf.strength,
        )
        if htf.bias == "neutral" or htf.strength < self.config.min_htf_strength:
            mark("htf", "fail", f"{htf.bias} {htf.strength:.2f}")
            reason = (
                "1H structure is neutral"
                if htf.bias == "neutral"
                else (
                    f"1H {htf.bias} strength {htf.strength:.2f} "
                    f"is below {self.config.min_htf_strength:.2f}"
                )
            )
            return finish(failed="htf", reason=reason)
        direction = htf.bias
        mark("htf", "pass", f"{htf.bias.upper()} {htf.strength:.2f}")

        if mtf.bias != "neutral" and mtf.bias != direction and mtf.strength >= 0.75:
            mark(
                "location",
                "fail",
                f"15m {mtf.bias} {mtf.strength:.2f} opposes",
            )
            return finish(
                failed="mtf_opposition",
                reason=(
                    f"15m {mtf.bias} structure {mtf.strength:.2f} "
                    f"opposes the 1H {direction} bias"
                ),
            )
        mtf_alignment = (
            1.0 if mtf.bias == direction else 0.5 if mtf.bias == "neutral" else 0.0
        )
        metrics["mtf_alignment"] = mtf_alignment
        zones = build_zones(
            one_h,
            fifteen,
            atr_value=atr_15m,
            now_ms=as_of,
            pivot_k=self.config.pivot_k,
            merge_width_atr=self.config.zone_merge_width_atr,
            half_life_hours=self.config.zone_half_life_hours,
        )
        zone = nearest_zone(
            zones,
            five[-1].close,
            direction,  # type: ignore[arg-type]
            atr_value=atr_15m,
            max_distance_atr=self.config.max_zone_distance_atr,
        )
        if zone is None:
            mark("location", "fail", "no zone within 1.5 ATR")
            return finish(
                failed="zone",
                reason=(
                    f"no {('support' if direction == 'long' else 'resistance')} "
                    "within 1.5 ATR"
                ),
            )
        metrics.update(
            zone_tf=zone.tf,
            zone_score=zone.score,
            zone_touches=zone.touches,
        )
        if zone.score < self.config.min_zone_score:
            mark("location", "fail", f"zone {zone.score:.2f} too weak")
            return finish(
                failed="zone",
                reason=(
                    f"nearest zone score {zone.score:.2f} "
                    f"is below {self.config.min_zone_score:.2f}"
                ),
            )
        if zone_broken(zone, fifteen, direction, atr_value=atr_15m, buffer_atr=0.25):  # type: ignore[arg-type]
            mark("location", "fail", "zone decisively broken")
            return finish(failed="zone", reason="nearest zone was decisively broken")

        sweep = detect_sweep(
            fifteen,
            zone,
            direction,  # type: ignore[arg-type]
            atr_value=atr_15m,
            lookback=self.config.sweep_lookback,
            min_quality=self.config.min_sweep_quality,
        )
        if sweep is None:
            mark("location", "fail", "no sweep + reclaim")
            return finish(failed="sweep", reason="no sweep-and-reclaim at the zone")
        metrics["sweep_quality"] = sweep.quality

        divergence = detect_divergence(
            fifteen,
            direction,  # type: ignore[arg-type]
            atr_value=atr_15m,
            pivot_k=self.config.pivot_k,
            ttl_bars=self.config.divergence_ttl_bars,
        )
        if (
            divergence is None
            and sweep.quality < self.config.weak_sweep_floor
            and self.config.require_divergence_for_weak_sweep
        ):
            mark("location", "fail", f"sweep {sweep.quality:.2f}, no divergence")
            return finish(
                failed="sweep",
                reason=(
                    f"sweep quality {sweep.quality:.2f} is weak and no "
                    "divergence was confirmed"
                ),
            )
        div_strength = divergence.strength if divergence is not None else 0.0
        metrics["divergence_strength"] = div_strength

        setup_score = 0.45 * zone.score + 0.35 * sweep.quality + 0.20 * div_strength
        metrics["setup_score"] = setup_score
        if setup_score < self.config.min_setup_score:
            mark("location", "fail", f"setup {setup_score:.2f} too weak")
            return finish(
                failed="setup_score",
                reason=(
                    f"setup score {setup_score:.2f} "
                    f"is below {self.config.min_setup_score:.2f}"
                ),
            )
        mark(
            "location",
            "pass",
            f"{zone.tf} zone {zone.score:.2f} · sweep {sweep.quality:.2f}",
        )

        try:
            trigger = trigger_view(
                five,
                direction,  # type: ignore[arg-type]
                atr_value=atr_5m,
                pivot_k=self.config.pivot_k,
                lookback=self.config.trigger_lookback_bars,
                max_chase_atr=self.config.max_chase_atr,
            )
        except SniperError as exc:
            mark("trigger", "fail", "trigger unavailable")
            return finish(failed="trigger", reason=f"5m trigger unavailable: {exc}")
        metrics.update(
            trigger_bos=trigger.micro_bos,
            trigger_volume_ratio=trigger.volume_ratio,
            trigger_score=trigger.score,
            trigger_bars_since=trigger.bars_since,
        )
        if not trigger.micro_bos:
            mark("trigger", "fail", "no fresh break")
            return finish(failed="trigger", reason="no fresh 5m break")
        if trigger.volume_ratio < self.config.min_trigger_vol_ratio:
            mark("trigger", "fail", f"volume {trigger.volume_ratio:.2f}x")
            return finish(
                failed="trigger_volume",
                reason=(
                    f"5m volume {trigger.volume_ratio:.2f}x is below "
                    f"{self.config.min_trigger_vol_ratio:.2f}x"
                ),
            )
        if trigger.score < self.config.min_trigger_score:
            mark("trigger", "fail", f"score {trigger.score:.2f}")
            return finish(
                failed="trigger_score",
                reason=(
                    f"5m trigger score {trigger.score:.2f} is below "
                    f"{self.config.min_trigger_score:.2f}"
                ),
            )
        mark(
            "trigger",
            "pass",
            f"BOS {trigger.volume_ratio:.2f}x · score {trigger.score:.2f}"
            + (f" · {trigger.bars_since}b ago" if trigger.bars_since else ""),
        )

        funding_penalty = self.config.funding_penalty
        if funding_pctile is not None:
            if direction == "long":
                funding_penalty = max(0.0, min(1.0, (funding_pctile - 80.0) / 20.0))
            else:
                funding_penalty = max(0.0, min(1.0, (20.0 - funding_pctile) / 20.0))
        oi_penalty = self.config.oi_penalty
        if oi_crowding is not None:
            oi_penalty = max(0.0, min(1.0, oi_crowding))
        metrics.update(funding_penalty=funding_penalty, oi_penalty=oi_penalty)

        entry = five[-1].close
        if direction == "long":
            stop = min(zone.low, sweep.extreme) - self.config.stop_buffer_atr * atr_5m
            risk = entry - stop
        else:
            stop = max(zone.high, sweep.extreme) + self.config.stop_buffer_atr * atr_5m
            risk = stop - entry
        if risk <= 0 or risk / atr_5m > self.config.max_stop_atr:
            mark("risk", "fail", f"stop {risk / atr_5m:.2f} ATR")
            return finish(
                failed="stop_distance",
                reason=(
                    f"stop risk {risk / atr_5m:.2f} ATR exceeds "
                    f"{self.config.max_stop_atr:.2f} ATR"
                ),
            )

        # Do not target a zone that is too close to clear min_rr.
        min_target_dist = self.config.min_rr * risk
        target_zone = None
        if direction == "long":
            for candidate in sorted(
                (z for z in zones if z.low > entry), key=lambda z: z.low
            ):
                if candidate.low - entry >= min_target_dist:
                    target_zone = candidate
                    break
        else:
            for candidate in sorted(
                (z for z in zones if z.high < entry),
                key=lambda z: z.high,
                reverse=True,
            ):
                if entry - candidate.high >= min_target_dist:
                    target_zone = candidate
                    break
        if target_zone is not None:
            tp1 = target_zone.low if direction == "long" else target_zone.high
        else:
            tp1 = entry + 2.0 * risk if direction == "long" else entry - 2.0 * risk
        if direction == "long" and tp1 <= entry:
            mark("risk", "fail", "target is not above entry")
            return finish(failed="target", reason="target is not above entry")
        if direction == "short" and tp1 >= entry:
            mark("risk", "fail", "target is not below entry")
            return finish(failed="target", reason="target is not below entry")
        rr = abs(tp1 - entry) / risk
        metrics["rr"] = rr
        if rr < self.config.min_rr:
            mark("risk", "fail", f"RR {rr:.2f}")
            return finish(
                failed="rr",
                reason=f"RR {rr:.2f} is below {self.config.min_rr:.2f}",
            )
        tp2 = (
            entry + max(3.0 * risk, tp1 - entry)
            if direction == "long"
            else entry - max(3.0 * risk, entry - tp1)
        )
        mark("risk", "pass", f"RR {rr:.2f} · stop {risk / atr_5m:.2f} ATR")

        features = self.scorer.features(
            htf_strength=htf.strength,
            zone_score=zone.score,
            sweep_quality=sweep.quality,
            divergence_strength=div_strength,
            trigger_score=trigger.score,
            rr=rr,
            mtf_alignment=mtf_alignment,
            volume_ratio=trigger.volume_ratio,
            delta_z=trigger.delta_z,
            funding_penalty=funding_penalty,
            oi_penalty=oi_penalty,
        )
        confidence, ev_r = self.scorer.score(features, rr)
        metrics.update(confidence=confidence, expected_value_r=ev_r)
        if confidence < self.config.min_confidence:
            mark("score", "fail", f"confidence {confidence:.2f}")
            return finish(
                failed="confidence",
                reason=(
                    f"confidence {confidence:.2f} is below "
                    f"{self.config.min_confidence:.2f}"
                ),
            )
        if ev_r < self.config.min_expected_value_r:
            mark("score", "fail", f"EV {ev_r:+.2f}R")
            return finish(
                failed="expected_value",
                reason=(
                    f"expected value {ev_r:+.2f}R is below "
                    f"{self.config.min_expected_value_r:.2f}R"
                ),
            )
        mark("score", "pass", f"confidence {confidence:.2f} · EV {ev_r:+.2f}R")

        reasons = [
            f"1H_{direction.upper()}",
            f"15m_{mtf.bias}",
            f"zone_{zone.tf}_{zone.score:.2f}",
            f"sweep_{sweep.quality:.2f}",
            "5m_bos",
            f"vol_{trigger.volume_ratio:.2f}x",
        ]
        if divergence is not None:
            reasons.append(f"div_{div_strength:.2f}")
        if trigger.delta_z is not None:
            reasons.append(f"delta_z_{trigger.delta_z:+.2f}")
        if trigger.bars_since:
            reasons.append(f"bos_age_{trigger.bars_since}b")
        if funding_penalty > 0:
            reasons.append(f"funding_penalty_{funding_penalty:.2f}")
        if oi_penalty > 0:
            reasons.append(f"oi_penalty_{oi_penalty:.2f}")

        decision = SniperDecision(
            symbol=self.symbol,
            as_of_ms=as_of,
            direction=direction,  # type: ignore[arg-type]
            entry=entry,
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            rr=rr,
            confidence=confidence,
            expected_value_r=ev_r,
            setup_score=setup_score,
            trigger_score=trigger.score,
            zone_score=zone.score,
            sweep_quality=sweep.quality,
            divergence_strength=div_strength,
            htf_strength=htf.strength,
            funding_penalty=funding_penalty,
            oi_penalty=oi_penalty,
            reasons=tuple(reasons),
            features={
                **features,
                "rr": rr,
                "mtf_strength": mtf.strength,
                "volume_ratio": trigger.volume_ratio,
                "delta_z": trigger.delta_z if trigger.delta_z is not None else 0.0,
                "atr_1h": atr_1h,
                "atr_15m": atr_15m,
                "atr_5m": atr_5m,
                "zone_low": zone.low,
                "zone_high": zone.high,
                "sweep_extreme": sweep.extreme,
            },
        )
        return finish(decision=decision, reason="all gates aligned")
