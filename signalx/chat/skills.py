"""Chart-interpretation skill library for the SIGNAL-X analyst.

These are *prompt lenses*, not trading rules. Each skill tells the model which
kind of evidence to inspect and how to explain it. The router keeps the prompt
focused by sending the core lenses plus the ones whose triggers match the
user's question. The full catalog is exposed at ``/api/chat/skills``.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_SKILLS = 22
DEFAULT_EXTRAS = (
    "pump_anatomy",
    "post_pump_short",
    "breakout_retest",
    "candle_anatomy",
    "trend_continuation",
    "mean_reversion",
    "multi_tf_conflict",
)


@dataclass(frozen=True)
class ChartSkill:
    id: str
    name: str
    category: str
    summary: str
    instructions: str
    triggers: tuple[str, ...] = ()
    core: bool = False


SKILLS: tuple[ChartSkill, ...] = (
    ChartSkill(
        "data_integrity",
        "Data integrity & freshness",
        "foundation",
        "Checks closed-bar age, missing flow, spot/futures differences and stale journal rows.",
        "Before interpreting, state whether the packet is fresh, which fields are missing, and whether the journal is stale. Never fill a missing value with a guess.",
        ("stale", "data", "missing", "fresh", "spot", "futures", "reliable"),
        True,
    ),
    ChartSkill(
        "timeframe_alignment",
        "Multi-timeframe alignment",
        "foundation",
        "Separates the higher-timeframe regime from the lower-timeframe trigger.",
        "Compare 5m, 15m, 1h and 4h explicitly. Name the dominant regime, the conflicting timeframe, and which one should be trusted for context versus timing.",
        ("timeframe", "5m", "15m", "1h", "4h", "multi", "alignment"),
        True,
    ),
    ChartSkill(
        "observation_discipline",
        "Observation discipline",
        "communication",
        "Keeps the answer observational: no validated edge, no order instructions.",
        "The G0 study killed the short-edge hypothesis. Call this an observation, not a signal. Do not tell the user to buy, sell, enter, or size a position; describe conditions, invalidation and uncertainty.",
        ("signal", "trade", "buy", "sell", "short", "long", "entry", "target", "should i"),
        True,
    ),
    ChartSkill(
        "plain_language",
        "Plain-language explanation",
        "communication",
        "Explains the chart so a non-specialist can follow the evidence.",
        "Lead with a plain-English summary. Define each technical term the first time it appears. Prefer short sentences over indicator name-dropping.",
        ("explain", "simple", "beginner", "teach", "what does", "meaning", "eli5"),
        True,
    ),
    ChartSkill(
        "uncertainty",
        "Calibrated uncertainty",
        "communication",
        "Separates fact, inference and speculation; avoids fake probabilities.",
        "Label facts, inferences and unknowns. Do not invent numeric probabilities. If the evidence is mixed, say so and explain what is missing.",
        ("sure", "confidence", "probability", "predict", "will it", "guarantee", "certain"),
        True,
    ),
    ChartSkill(
        "market_structure",
        "Market structure",
        "structure",
        "Reads swing sequence, higher highs/lows, BOS and CHoCH.",
        "Use the supplied pivots and breaks. Distinguish continuation BOS from a change of character. Do not call a break until the packet shows a closed-bar break.",
        ("structure", "bos", "choch", "swing", "higher high", "lower low", "hh", "hl", "ll"),
        True,
    ),
    ChartSkill(
        "ema_regime",
        "EMA regime & cascade",
        "trend",
        "Reads the 8/13/21 stack, cross age and cross-timeframe propagation.",
        "State each timeframe's EMA stack and the age of its latest cross. A stack is a regime, not an entry. Highlight when the lower timeframe has rolled over inside a higher-timeframe uptrend or vice versa.",
        ("ema", "trend", "stack", "cross", "cascade", "moving average"),
        True,
    ),
    ChartSkill(
        "volatility_regime",
        "Volatility & compression",
        "volatility",
        "Interprets ATR, plateau-box height, containment and expansion risk.",
        "Use ATR and the plateau box rather than eyeballing candle size. A non-qualifying box is not a plateau. Compression can resolve either way; do not assign a direction without a break.",
        ("atr", "volatility", "compression", "plateau", "box", "range", "coil"),
        True,
    ),
    ChartSkill(
        "momentum_state",
        "Momentum state",
        "momentum",
        "Reads RSI level, slope and momentum decay without overbought/oversold absolutism.",
        "RSI is context, not a standalone trigger. Say whether momentum is rising, falling, diverging or simply mean-reverting. In a strong trend, oversold can stay oversold.",
        ("rsi", "momentum", "overbought", "oversold", "decay"),
        True,
    ),
    ChartSkill(
        "divergence",
        "Divergence quality",
        "momentum",
        "Checks whether RSI divergence is fresh, confirmed and aligned with structure.",
        "State the divergence age and the bars involved. A fresh divergence near resistance is different from an expired one. Divergence without a structure break is a warning, not a trigger.",
        ("divergence", "div", "rsi high", "rsi low"),
        False,
    ),
    ChartSkill(
        "volume_participation",
        "Volume participation",
        "volume",
        "Uses volume z-score and per-candle volume to judge conviction.",
        "Compare the latest closed-bar volume ratio with the prior 20-bar average. A ratio well below 1 after a push is volume dry-up/damping: price can drift or fade without a structure break. A breakout without volume expansion is suspect. Say when volume is merely average.",
        ("volume", "vol z", "participation", "conviction", "breakout volume", "damping", "damp", "dry up", "dry-up", "cooling", "volume fade", "dump", "dumping", "liquidation"),
        True,
    ),
    ChartSkill(
        "order_flow",
        "Order flow & CVD",
        "flow",
        "Reads taker delta, CVD, absorption and price/flow divergence.",
        "CVD and taker-buy are flow evidence, not predictions. Call out conflicts: price down with CVD up, or price up with CVD down. Do not infer hidden buyers from a single snapshot.",
        ("cvd", "taker", "flow", "absorption", "delta", "order flow"),
        True,
    ),
    ChartSkill(
        "large_prints",
        "Large-print / whale activity",
        "flow",
        "Checks per-trade large prints and their net direction.",
        "Zero large prints means the move lacks large aggressive participation in this window. Do not call it whale distribution without large sells; say the evidence is absent.",
        ("whale", "large print", "large prints", "fat money", "block", "big order", "flow"),
        False,
    ),
    ChartSkill(
        "order_book",
        "Order-book context",
        "flow",
        "Interprets band imbalance, spread and thin-book caveats.",
        "Book imbalance is resting liquidity and can be pulled. Treat it as a short-lived bias, not a commitment. Mention spread/illiquidity when it is wide.",
        ("book", "depth", "imbalance", "spread", "liquidity", "bid", "ask"),
        False,
    ),
    ChartSkill(
        "derivatives_context",
        "Funding, OI & squeeze guard",
        "derivatives",
        "Reads funding percentile, OI regime and squeeze risk.",
        "Funding percentile is positioning context. OI rising with price can be trend fuel; OI rising into a squeeze guard is risk. Never call a squeeze direction from compression alone.",
        ("funding", "open interest", "oi", "squeeze", "positioning", "leverage"),
        True,
    ),
    ChartSkill(
        "levels_confluence",
        "Levels & confluence",
        "levels",
        "Maps resistance zones, swings, AVWAP and box edges around price.",
        "Quote exact levels. Say whether price is inside, above or below each zone. Confluence matters only when independent levels cluster; do not stack the same pivot twice.",
        ("support", "resistance", "zone", "level", "avwap", "vwap", "confluence"),
        True,
    ),
    ChartSkill(
        "scenario_tree",
        "Scenario tree",
        "risk",
        "Builds bull, base and bear paths with concrete triggers.",
        "Give three paths: continuation, range/base, and failure. For each, name the trigger, the level that confirms it and the level that invalidates it. Keep the paths mutually intelligible.",
        ("scenario", "bull", "bear", "base", "if", "plan", "outlook"),
        True,
    ),
    ChartSkill(
        "invalidation",
        "Invalidation map",
        "risk",
        "Defines what would prove the current read wrong.",
        "Always include invalidation. Use packet levels, not arbitrary round numbers. State whether invalidation is a close-through, a wick-through or a time-based failure.",
        ("invalidation", "stop", "wrong", "invalid", "invalidate", "risk"),
        True,
    ),
    ChartSkill(
        "risk_reward",
        "Risk/reward math",
        "risk",
        "Frames distance in ATR/percent and generic risk math without position advice.",
        "Express distances in ATR and percent. If discussing R:R, label it illustrative and do not recommend leverage or a position size.",
        ("risk", "reward", "rr", "r:r", "sizing", "leverage", "stop loss"),
    ),
    ChartSkill(
        "pump_anatomy",
        "Pump anatomy",
        "patterns",
        "Distinguishes ignition, plateau, distribution and retrace phases.",
        "Describe where the move sits in its lifecycle: ignition, vertical expansion, plateau, rollover or retrace. Use the supplied change, volume z, vol_ratio and distance from the recent high/low; do not call a top from price alone. If price stalls while volume collapses, call it damping/cooling rather than a fresh trend.",
        ("pump", "parabolic", "ignition", "post-pump", "vertical", "moon", "damping", "damp", "cooling", "fade", "dump", "dumping", "crash"),
    ),
    ChartSkill(
        "post_pump_short",
        "Post-pump short checklist",
        "patterns",
        "Tests the specific conditions for a post-pump short observation.",
        "Checklist: prior pump, plateau/compression, structure break, volume expansion on the break, flow confirmation, funding/OI not squeezing, and higher-timeframe permission. Say which conditions fail. A high-volume BOS down after a pump is a dump/failed pump, not momentum decay; say whether it is still accelerating or stabilizing.",
        ("short", "distribution", "rollover", "fade", "top", "post-pump", "dump", "dumping", "crash", "liquidation", "waterfall"),
    ),
    ChartSkill(
        "breakout_retest",
        "Breakout & retest",
        "patterns",
        "Separates a confirmed breakout/retest from a first touch.",
        "A break needs a closed bar through the level and follow-through. A retest is only useful if it holds; a failed retest is evidence for the other side. Quote the level and volume.",
        ("breakout", "retest", "break out", "confirmed break", "follow through"),
    ),
    ChartSkill(
        "fakeout_liquidity",
        "Fakeout & liquidity trap",
        "patterns",
        "Looks for wick rejection, failed breaks and stop-run behavior.",
        "A wick through a level that closes back inside is a failed break, not a confirmed BOS. Distinguish liquidity sweep from continuation and say what would confirm the trap.",
        ("fakeout", "fake out", "trap", "stop hunt", "liquidity sweep", "wick"),
    ),
    ChartSkill(
        "candle_anatomy",
        "Candle anatomy",
        "patterns",
        "Reads body/wick ratios, rejection and engulfing behavior.",
        "Use the supplied body and wick ratios. A long upper wick at resistance is rejection evidence; a long lower wick at support is absorption evidence. Do not over-read one candle.",
        ("candle", "wick", "body", "rejection", "engulf", "doji", "pin bar"),
    ),
    ChartSkill(
        "chart_patterns",
        "Classical chart patterns",
        "patterns",
        "Names only patterns actually supported by the supplied swings.",
        "Scan for a supported pattern and name its status (forming/confirmed/failed), timeframe, defining pivots/levels and invalidation. Check flags, wedges, triangles, ranges, double tops/bottoms, head-and-shoulders, breakouts/retests and failed breaks. If none is clean, say 'No clean pattern' and describe the structure instead.",
        ("flag", "wedge", "triangle", "double top", "double bottom", "head and shoulders", "pattern"),
        True,
    ),
    ChartSkill(
        "trend_continuation",
        "Trend continuation",
        "trend",
        "Tests whether a pullback is corrective or a trend change.",
        "A pullback is continuation evidence while higher lows and the higher-timeframe EMA regime survive. Name the first structural level that would turn the pullback into a reversal.",
        ("pullback", "continuation", "higher low", "dip", "correction"),
    ),
    ChartSkill(
        "mean_reversion",
        "Mean reversion",
        "volatility",
        "Assesses overextension versus trend strength.",
        "Mean reversion is a hypothesis, not a default. Use distance from EMA/AVWAP, RSI and candle extension; say when trend strength makes reversion risky.",
        ("mean reversion", "extended", "overextended", "stretched", "rubber band", "snap back"),
    ),
    ChartSkill(
        "squeeze_expansion",
        "Squeeze & expansion",
        "volatility",
        "Separates compression from a directional squeeze setup.",
        "Compression narrows the range; expansion chooses direction later. Funding/OI can make one side crowded. Do not claim the squeeze will resolve in a particular direction.",
        ("squeeze", "expansion", "coil", "compressed", "volatility expansion"),
    ),
    ChartSkill(
        "funding_positioning",
        "Funding & positioning",
        "derivatives",
        "Reads crowded positioning from funding percentile and OI.",
        "High funding means longs pay; it does not guarantee a reversal. Combine with price location and OI behavior. Say when the percentile is merely elevated versus extreme.",
        ("crowded", "funding extreme", "positioning", "longs pay", "shorts pay", "funding"),
    ),
    ChartSkill(
        "multi_tf_conflict",
        "Conflict resolution",
        "foundation",
        "Resolves disagreements between timeframes without forcing alignment.",
        "When timeframes disagree, state the conflict plainly. Higher timeframe usually sets regime; lower timeframe sets timing. Do not average conflicting signals into a fake consensus.",
        ("conflict", "disagree", "mixed", "not aligned", "conflicting"),
        True,
    ),
    ChartSkill(
        "catalyst_caveat",
        "Catalyst caveat",
        "foundation",
        "Prevents invented news explanations.",
        "The packet contains no news, social sentiment or announcement data. If the user asks why a move happened, say the chart packet cannot establish the catalyst and stick to market evidence.",
        ("news", "catalyst", "announcement", "why did", "fundamental", "twitter", "x post"),
    ),
    ChartSkill(
        "session_context",
        "Session context",
        "foundation",
        "Uses timestamps for session context without inventing volume-profile data.",
        "You may note the UTC session (Asia/London/New York) from timestamps, but do not claim session volume behavior that is not in the packet.",
        ("session", "asia", "london", "new york", "time of day", "utc"),
    ),
    ChartSkill(
        "relative_strength",
        "Relative strength",
        "foundation",
        "Checks whether comparison data exists before claiming outperformance.",
        "The packet is single-symbol. Unless a benchmark is explicitly present, do not claim relative strength versus BTC or the market; say the comparison data is unavailable.",
        ("relative", "outperform", "underperform", "vs btc", "market strength", "benchmark"),
    ),
    ChartSkill(
        "volume_profile",
        "Volume profile proxy",
        "levels",
        "Uses AVWAP/zones as a proxy and flags the missing full profile.",
        "The packet has AVWAP and pivot zones, not a full volume profile/POC. Do not invent value-area levels. Use AVWAP and zone clusters as the available proxy.",
        ("volume profile", "poc", "value area", "control point", "profile"),
    ),
    ChartSkill(
        "trade_management",
        "Observation management",
        "risk",
        "Discusses management as scenario tracking, not order instructions.",
        "If asked about managing a position, frame it as invalidation tracking and scenario updates. Do not issue execution instructions or personalized financial advice.",
        ("manage", "trail", "take profit", "tp", "move stop", "hold"),
    ),
    ChartSkill(
        "teaching_mode",
        "Teaching mode",
        "communication",
        "Turns the answer into a compact lesson with definitions and a recap.",
        "Teach the concept behind the read: define the indicator, explain why it matters, then apply it to this chart. End with a one-sentence recap the user can remember.",
        ("teach", "learn", "explain like", "how do i read", "lesson", "study"),
    ),
)


CHART_SKILLS = SKILLS
SKILL_BY_ID = {skill.id: skill for skill in SKILLS}
CORE_SKILLS = tuple(skill for skill in SKILLS if skill.core)


def select_skills(message: str, *, max_skills: int = MAX_SKILLS) -> list[ChartSkill]:
    """Choose the core lenses plus question-specific lenses.

    Core skills always travel with the request so the model cannot forget the
    observation-only rule or the timeframe hierarchy. Extra slots go to the
    highest trigger match, with stable registry order as the tie-breaker.
    """
    text = (message or "").lower()
    if max_skills < 1:
        raise ValueError("max_skills must be >= 1")

    selected: list[ChartSkill] = list(CORE_SKILLS[:max_skills])
    remaining = max_skills - len(selected)
    if remaining <= 0:
        return selected

    scored: list[tuple[int, int, ChartSkill]] = []
    for index, skill in enumerate(SKILLS):
        if skill.core:
            continue
        hits = sum(1 for trigger in skill.triggers if trigger in text)
        if hits:
            scored.append((hits, index, skill))
    scored.sort(key=lambda item: (-item[0], item[1]))
    if scored:
        selected.extend(skill for _, _, skill in scored[:remaining])
    else:
        selected.extend(SKILL_BY_ID[sid] for sid in DEFAULT_EXTRAS[:remaining])
    order = {skill.id: i for i, skill in enumerate(SKILLS)}
    return sorted(selected, key=lambda skill: order[skill.id])


def skill_prompt(skills: list[ChartSkill]) -> str:
    return "\n".join(
        f"- [{skill.category}] {skill.name}: {skill.instructions}" for skill in skills
    )


def public_skills() -> list[dict]:
    return [
        {
            "id": skill.id,
            "name": skill.name,
            "category": skill.category,
            "summary": skill.summary,
            "core": skill.core,
        }
        for skill in SKILLS
    ]
