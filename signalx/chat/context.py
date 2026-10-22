"""Build a compact, server-generated chart packet for the LLM.

The packet is deliberately smaller than the dashboard payload: recent closed
candles, chart-engine summaries, EMA cascade state, flow/derivatives context
and the latest sentinel rows. The model gets numbers and provenance, not raw
upstream payloads it could mistake for instructions.

Anti-lookahead: chart engines run on closed klines only, same as the overlays
API. The selected timeframe's last closed bar is included; the in-progress bar
is not.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ..ema import CASCADE_TFS, TfInfo, cascade_pattern, tf_crosses
from ..feeds import TF_MS, fetch_klines, poll_snapshot
from ..journal import SignalJournal
from ..overlays import MIN_BARS, OverlayError, build_overlays

CONTEXT_CACHE_TTL_MS = 15_000
CONTEXT_CACHE_MAX = 32
CANDLE_LIMIT = 48
RSI_SERIES_LIMIT = 24


class ChartContextError(RuntimeError):
    """The chart packet could not be built from market data."""


@dataclass(frozen=True)
class ChartContext:
    symbol: str
    market: str
    tf: str
    as_of_ms: int
    payload: dict

    def meta(self) -> dict:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "tf": self.tf,
            "as_of": _iso(self.as_of_ms),
            "as_of_ms": self.as_of_ms,
            "data_quality": self.payload.get("data_quality", {}),
        }


_CACHE: dict[tuple[str, str, str, str], ChartContext] = {}


def clear_context_cache() -> None:
    _CACHE.clear()


def _iso(ms: int | float | None) -> str | None:
    if not isinstance(ms, (int, float)) or isinstance(ms, bool) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _num(value: Any, digits: int = 8) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, digits)


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text(value: Any, limit: int = 240) -> str | None:
    if not isinstance(value, str):
        return None
    return value[:limit]


def _last_defined(values: Any) -> float | None:
    if not isinstance(values, list):
        return None
    for value in reversed(values):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return _num(value)
    return None


def _last_time(rows: list, index: Any) -> str | None:
    if not isinstance(index, int) or index < 0 or index >= len(rows):
        return None
    return _iso(rows[index].open_time)


def _pct_change(closes: list[float], bars: int) -> float | None:
    if bars < 1 or len(closes) <= bars:
        return None
    previous = closes[-1 - bars]
    if previous <= 0:
        return None
    return _num((closes[-1] / previous - 1.0) * 100.0, 4)


def _compact_flow(flow: Any) -> dict | None:
    if not isinstance(flow, dict):
        return None
    numeric = (
        "price_change_pct", "cvd_net_quote", "cvd_ratio", "cvd_slope_ratio",
        "taker_buy_ratio", "large_bar_ratio", "cvd_price_divergence",
        "trade_count", "buy_notional", "sell_notional", "net_notional",
        "net_ratio", "large_print_min_usd", "large_print_count",
        "large_print_buy_usd", "large_print_sell_usd", "large_print_net_usd",
        "large_print_net_ratio", "large_print_net_share", "large_print_share",
        "largest_print_usd", "book_band_pct", "book_bid_usd", "book_ask_usd",
        "book_imbalance", "book_spread_bps",
    )
    out = {key: _num(flow.get(key), 6) for key in numeric}
    out["largest_print_side"] = _text(flow.get("largest_print_side"), 12)
    out["book_bias"] = _text(flow.get("book_bias"), 20)
    out["fat_money_bias"] = _text(flow.get("fat_money_bias"), 20)
    out["absorption"] = _text(flow.get("absorption"), 24)
    out["source"] = _text(flow.get("source"), 80)
    return out


def _compact_snapshot(snap: dict | None) -> dict | None:
    if not isinstance(snap, dict):
        return None
    out: dict[str, Any] = {}
    scalar = (
        "symbol", "market", "tf", "price", "atr", "change_24h_pct", "vol_z",
        "vol_ratio", "is_pump", "pump_failed", "n_zones", "swing_low", "swing_high", "bos_down", "bos_up",
        "rsi", "has_bearish_div", "bearish_div_age", "has_bullish_div",
        "bullish_div_age", "divergence_max_age_bars", "momentum_decay",
        "momentum_decay_bias", "funding_now", "funding_pctile", "oi_regime",
        "squeeze_active", "state",
    )
    for key in scalar:
        value = snap.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = _num(value, 8)
        elif isinstance(value, bool) or value is None:
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:80]

    compression = snap.get("compression")
    if isinstance(compression, dict):
        out["compression"] = {
            key: _num(compression.get(key), 6)
            for key in (
                "box_low", "box_high", "height", "height_atr", "containment",
                "score", "qualifies", "touches", "window",
            )
            if key in compression
        }
        if isinstance(compression.get("box"), dict):
            out["compression"]["box"] = {
                "low": _num(compression["box"].get("low"), 8),
                "high": _num(compression["box"].get("high"), 8),
            }

    resistance = snap.get("resistance")
    if isinstance(resistance, dict):
        out["resistance"] = {
            key: _num(resistance.get(key), 8)
            for key in ("low", "high", "strength", "touches", "dist_pct", "dist_atr")
            if key in resistance
        }

    events = snap.get("events")
    if isinstance(events, list):
        out["events"] = [str(event)[:60] for event in events if isinstance(event, str)][:20]
    out["flow"] = _compact_flow(snap.get("flow"))
    flow_errors = snap.get("flow_errors")
    if isinstance(flow_errors, dict):
        out["flow_errors"] = {str(k)[:40]: _text(v, 120) for k, v in flow_errors.items()}
    return out


def _candle_features(rows: list, limit: int = CANDLE_LIMIT) -> list[dict]:
    out: list[dict] = []
    for k in rows[-limit:]:
        span = max(k.high - k.low, 1e-12)
        top = max(k.open, k.close)
        bottom = min(k.open, k.close)
        quote = k.quote_volume
        taker_ratio = (k.taker_buy_quote / quote) if quote > 0 else None
        out.append({
            "t": _iso(k.open_time),
            "o": _num(k.open, 10),
            "h": _num(k.high, 10),
            "l": _num(k.low, 10),
            "c": _num(k.close, 10),
            "v": _num(k.volume, 6),
            "qv": _num(k.quote_volume, 2),
            "dir": "up" if k.close > k.open else "down" if k.close < k.open else "flat",
            "body_pct": _num(abs(k.close - k.open) / span * 100.0, 2),
            "upper_wick_pct": _num((k.high - top) / span * 100.0, 2),
            "lower_wick_pct": _num((bottom - k.low) / span * 100.0, 2),
            "close_pos_pct": _num((k.close - k.low) / span * 100.0, 2),
            "taker_buy_ratio": _num(taker_ratio, 4),
        })
    return out


def _compact_overlays(ov: dict | None, rows: list, price: float) -> dict | None:
    if not isinstance(ov, dict):
        return None
    atr = _num(ov.get("atr"), 10)
    out: dict[str, Any] = {
        "n": _int(ov.get("n")),
        "atr": atr,
        "atr_pct": _num(atr / price * 100.0, 4) if atr and price > 0 else None,
    }

    zones = []
    raw_zones = ov.get("zones")
    if isinstance(raw_zones, list):
        for zone in raw_zones:
            if not isinstance(zone, dict):
                continue
            low, high = _num(zone.get("lo"), 10), _num(zone.get("hi"), 10)
            if low is None or high is None or low > high:
                continue
            mid = (low + high) / 2.0
            side = "inside" if low <= price <= high else "above" if low > price else "below"
            zones.append({
                "lo": low,
                "hi": high,
                "mid": _num(mid, 10),
                "side": side,
                "touches": _int(zone.get("touches")),
                "weight": _num(zone.get("w"), 4),
                "strength": _num(zone.get("strength"), 2),
                "distance_pct": _num((mid - price) / price * 100.0, 4) if price > 0 else None,
                "distance_atr": _num((mid - price) / atr, 3) if atr else None,
            })
    out["zones"] = zones[:5]

    avwap = ov.get("avwap")
    if isinstance(avwap, dict):
        out["avwap"] = {
            "line": _last_defined(avwap.get("line")),
            "upper": _last_defined(avwap.get("upper")),
            "lower": _last_defined(avwap.get("lower")),
            "anchor_i": _int(avwap.get("anchor_i")),
            "anchor_time": _last_time(rows, avwap.get("anchor_i")),
            "anchor_mode": _text(avwap.get("anchor_mode"), 40),
        }

    box = ov.get("box")
    if isinstance(box, dict):
        out["box"] = {
            key: _num(box.get(key), 6)
            for key in ("lo", "hi", "height_atr", "containment", "score")
            if key in box
        }
        out["box"]["qualifies"] = bool(box.get("qualifies"))
        out["box"]["start_i"] = _int(box.get("start_i"))

    for name, limit in (("pivots", 10), ("structure", 8), ("breaks", 6), ("divs", 4), ("budivs", 4)):
        raw = ov.get(name)
        if not isinstance(raw, list):
            continue
        items = []
        for item in raw[-limit:]:
            if not isinstance(item, dict):
                continue
            clean = {
                key: (_num(value, 8) if isinstance(value, (int, float)) and not isinstance(value, bool)
                      else _text(value, 60) if isinstance(value, str) else _int(value))
                for key, value in item.items()
            }
            if "i" in clean:
                clean["time"] = _last_time(rows, clean.get("i"))
            if "break_i" in clean:
                clean["break_time"] = _last_time(rows, clean.get("break_i"))
            items.append(clean)
        out[name] = items

    out["swing_low"] = ov.get("swing_low")
    out["swing_high"] = ov.get("swing_high")
    out["rsi_now"] = _last_defined(ov.get("rsi"))
    rsi = ov.get("rsi")
    out["rsi_series"] = (
        [_num(value, 2) for value in rsi[-RSI_SERIES_LIMIT:]]
        if isinstance(rsi, list) else []
    )
    out["emas"] = {
        key: _last_defined(values)
        for key, values in (ov.get("emas") or {}).items()
        if key in ("8", "13", "21")
    } if isinstance(ov.get("emas"), dict) else {}
    crosses = ov.get("ema_crosses")
    out["ema_crosses"] = [
        {
            "i": _int(c.get("i")),
            "dir": _text(c.get("dir"), 8),
            "time": _last_time(rows, c.get("i")),
        }
        for c in crosses[-4:]
        if isinstance(c, dict)
    ] if isinstance(crosses, list) else []
    return out


def _reference_levels(price: float, ov: dict | None, snap: dict | None) -> dict:
    """Nearby levels from independent engines; no invented round numbers."""
    above: list[dict] = []
    below: list[dict] = []
    inside: list[dict] = []
    if isinstance(ov, dict):
        atr = _num(ov.get("atr"), 10)
        for zone in ov.get("zones") or []:
            if not isinstance(zone, dict):
                continue
            low, high = _num(zone.get("lo"), 10), _num(zone.get("hi"), 10)
            if low is None or high is None:
                continue
            mid = (low + high) / 2.0
            item = {
                "kind": "resistance_zone",
                "lo": low,
                "hi": high,
                "touches": _int(zone.get("touches")),
                "distance_pct": _num((mid - price) / price * 100.0, 4) if price > 0 else None,
                "distance_atr": _num((mid - price) / atr, 3) if atr else None,
            }
            if low <= price <= high:
                inside.append(item)
            elif mid > price:
                above.append(item)
            else:
                below.append(item)

        for key, kind in (("swing_high", "swing_high"), ("swing_low", "swing_low")):
            raw = ov.get(key)
            if isinstance(raw, dict) and _num(raw.get("p"), 10) is not None:
                level = _num(raw.get("p"), 10)
                item = {
                    "kind": kind,
                    "price": level,
                    "distance_pct": _num((level - price) / price * 100.0, 4) if price > 0 else None,
                    "distance_atr": _num((level - price) / atr, 3) if atr else None,
                }
                (above if level > price else below if level < price else inside).append(item)

        avwap = ov.get("avwap")
        if isinstance(avwap, dict):
            for key, kind in (("line", "avwap"), ("upper", "avwap_upper"), ("lower", "avwap_lower")):
                level = _last_defined(avwap.get(key))
                if level is None:
                    continue
                item = {
                    "kind": kind,
                    "price": level,
                    "distance_pct": _num((level - price) / price * 100.0, 4) if price > 0 else None,
                    "distance_atr": _num((level - price) / atr, 3) if atr else None,
                }
                (above if level > price else below if level < price else inside).append(item)

        box = ov.get("box")
        if isinstance(box, dict):
            for key, kind in (("lo", "box_low"), ("hi", "box_high")):
                level = _num(box.get(key), 10)
                if level is None:
                    continue
                item = {
                    "kind": kind,
                    "price": level,
                    "distance_pct": _num((level - price) / price * 100.0, 4) if price > 0 else None,
                    "distance_atr": _num((level - price) / atr, 3) if atr else None,
                }
                (above if level > price else below if level < price else inside).append(item)

    if isinstance(snap, dict):
        resistance = snap.get("resistance")
        if isinstance(resistance, dict):
            low, high = _num(resistance.get("low"), 10), _num(resistance.get("high"), 10)
            if low is not None and high is not None:
                mid = (low + high) / 2.0
                item = {
                    "kind": "snapshot_resistance",
                    "lo": low,
                    "hi": high,
                    "touches": _int(resistance.get("touches")),
                    "distance_pct": _num((mid - price) / price * 100.0, 4) if price > 0 else None,
                }
                (inside if low <= price <= high else above if mid > price else below).append(item)

    def near(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda item: abs(item.get("distance_pct") or 9999.0))[:6]

    return {"above": near(above), "below": near(below), "inside": near(inside)}


def _ema_summary(infos: list[TfInfo], errors: dict[str, str], now_ms: int) -> dict:
    ordered = sorted(infos, key=lambda info: info.step_ms)
    tfs = []
    for info in ordered:
        last_cross = info.crosses[-1] if info.crosses else None
        age = None
        if last_cross is not None:
            age = max(0, info.closed - 1 - last_cross.index)
        tfs.append({
            "tf": info.tf,
            "state": info.state,
            "closed": info.closed,
            "emas": {
                "8": _num(info.emas[0], 10),
                "13": _num(info.emas[1], 10),
                "21": _num(info.emas[2], 10),
            },
            "last_cross": None if last_cross is None else {
                "dir": last_cross.direction,
                "i": last_cross.index,
                "time": _iso(last_cross.time),
                "prev": last_cross.prev_state,
                "age_bars": age,
            },
        })

    pattern = None
    if ordered:
        try:
            pattern = cascade_pattern(ordered, now_ms=now_ms)
        except Exception:
            pattern = None
    states = [info.state for info in ordered if info.state in ("up", "down", "neutral")]
    return {
        "tfs": tfs,
        "pattern": pattern,
        "alignment": {
            "up": states.count("up"),
            "down": states.count("down"),
            "neutral": states.count("neutral"),
            "total": len(states),
            "full_stack": bool(pattern and pattern.get("full_stack")),
        },
        "errors": {str(k): str(v)[:160] for k, v in errors.items()},
    }


def _journal_context(journal_dir: str | Path, symbol: str, now_ms: int) -> dict:
    try:
        rows = SignalJournal(journal_dir).read_all()
    except Exception as exc:  # journal is optional context, never fatal
        return {"available": False, "error": type(exc).__name__}
    mine = [row for row in rows if row.get("symbol") == symbol]
    latest = mine[-1] if mine else None
    latest_age_s = (
        max(0, (now_ms - latest["ts_ms"]) / 1000.0)
        if isinstance(latest, dict) and isinstance(latest.get("ts_ms"), int) else None
    )
    recent = []
    for row in mine[-8:]:
        recent.append({
            "ts": _iso(row.get("ts_ms")),
            "ts_ms": row.get("ts_ms"),
            "event": _text(row.get("event"), 30),
            "state": _text(row.get("state"), 30),
            "prev_state": _text(row.get("prev_state"), 30),
            "price": _num(row.get("price"), 10),
            "score": _num(row.get("score"), 4),
            "entry": _num(row.get("entry"), 10),
            "stop": _num(row.get("stop"), 10),
            "tp1": _num(row.get("tp1"), 10),
            "tp2": _num(row.get("tp2"), 10),
            "reasons": _text(row.get("reasons"), 300),
        })
    recent.reverse()
    return {
        "available": True,
        "rows_total": len(mine),
        "latest": recent[0] if recent else None,
        "latest_age_s": _num(latest_age_s, 1),
        "recent": recent,
    }


def _data_quality(
    *,
    now_ms: int,
    tf: str,
    rows: list,
    snapshot: dict | None,
    snapshot_error: str | None,
    overlays: dict | None,
    overlay_error: str | None,
    ema_errors: dict[str, str],
    journal: dict,
    market: str,
) -> dict:
    last_close_age_s = max(0.0, (now_ms - rows[-1].close_time) / 1000.0) if rows else None
    step_s = TF_MS.get(tf, 0) / 1000.0
    warnings: list[str] = []
    if snapshot is None:
        warnings.append("snapshot unavailable; state, flow, funding and OI context are missing")
    if overlays is None:
        warnings.append("overlays unavailable; zones, structure, AVWAP and RSI series are missing")
    if ema_errors:
        warnings.append("EMA cascade incomplete for: " + ", ".join(sorted(ema_errors)))
    if isinstance(snapshot, dict) and snapshot.get("flow_errors"):
        warnings.append("flow enrichment incomplete: " + ", ".join(sorted(snapshot["flow_errors"])))
    if journal.get("available") and journal.get("latest_age_s") is not None:
        if journal["latest_age_s"] > max(3600.0, step_s * 8):
            warnings.append(
                f"latest journal row for this symbol is {journal['latest_age_s'] / 3600.0:.1f}h old; do not present it as current"
            )
    if market == "spot":
        warnings.append("spot market: funding, open interest and squeeze context are not applicable")

    return {
        "as_of": _iso(now_ms),
        "timeframe": tf,
        "market": market,
        "closed_bars": len(rows),
        "last_bar_close_age_s": _num(last_close_age_s, 1),
        "last_bar_is_recent": bool(
            last_close_age_s is not None and step_s > 0 and last_close_age_s <= step_s * 1.5
        ),
        "snapshot_ok": snapshot is not None,
        "snapshot_error": _text(snapshot_error, 200),
        "overlays_ok": overlays is not None,
        "overlay_error": _text(overlay_error, 200),
        "ema_errors": {str(k): str(v)[:160] for k, v in ema_errors.items()},
        "journal_available": bool(journal.get("available")),
        "journal_latest_age_s": journal.get("latest_age_s"),
        "warnings": warnings,
    }


async def build_chart_context(
    client: httpx.AsyncClient,
    symbol: str,
    market: str,
    tf: str,
    now_ms: int,
    *,
    journal_dir: str | Path,
    use_cache: bool = True,
) -> ChartContext:
    """Fetch and compact the live chart packet.

    Snapshot failure is tolerated if closed klines are available; kline failure
    is fatal because the chart engines cannot run without OHLC. Optional
    enrichment failures are reported in ``data_quality``.
    """
    if market not in ("spot", "futures"):
        raise ValueError("market must be 'spot' or 'futures'")
    if tf not in TF_MS:
        raise ValueError(f"unsupported timeframe: {tf!r}")

    key = (symbol, market, tf, str(journal_dir))
    if use_cache:
        hit = _CACHE.get(key)
        if hit is not None and now_ms - hit.as_of_ms < CONTEXT_CACHE_TTL_MS:
            return hit

    snap_task = poll_snapshot(
        client, symbol, market, tf, now_ms, include_microstructure=True
    )
    rows_task = fetch_klines(
        client, market, symbol, tf, now_ms, limit=200, min_closed=MIN_BARS
    )
    ema_tasks: dict[str, asyncio.Task] = {}
    for ema_tf in CASCADE_TFS:
        if ema_tf == tf:
            continue
        ema_tasks[ema_tf] = asyncio.create_task(
            fetch_klines(client, market, symbol, ema_tf, now_ms, limit=120, min_closed=30)
        )

    results = await asyncio.gather(snap_task, rows_task, *ema_tasks.values(), return_exceptions=True)
    snap_result, rows_result = results[0], results[1]
    ema_results = results[2:]

    if isinstance(rows_result, Exception):
        raise ChartContextError(
            f"closed klines unavailable: {type(rows_result).__name__}: {str(rows_result)[:160]}"
        )
    rows = rows_result
    snapshot = snap_result if isinstance(snap_result, dict) and snap_result else None
    snapshot_error = None if snapshot is not None else (
        f"{type(snap_result).__name__}: {str(snap_result)[:160]}"
        if isinstance(snap_result, Exception) else "empty snapshot"
    )

    overlays = None
    overlay_error = None
    try:
        overlays = build_overlays(rows)
    except (OverlayError, ValueError) as exc:
        overlay_error = f"{type(exc).__name__}: {str(exc)[:160]}"

    infos: list[TfInfo] = []
    ema_errors: dict[str, str] = {}
    for ema_tf, result in zip(ema_tasks, ema_results):
        if isinstance(result, Exception):
            ema_errors[ema_tf] = f"{type(result).__name__}: {str(result)[:160]}"
            continue
        try:
            infos.append(tf_crosses(
                ema_tf, TF_MS[ema_tf], [k.open_time for k in result], [k.close for k in result]
            ))
        except Exception as exc:
            ema_errors[ema_tf] = f"{type(exc).__name__}: {str(exc)[:160]}"

    if tf in CASCADE_TFS:
        if len(rows) >= 30:
            try:
                infos.append(tf_crosses(
                    tf, TF_MS[tf], [k.open_time for k in rows], [k.close for k in rows]
                ))
            except Exception as exc:
                ema_errors[tf] = f"{type(exc).__name__}: {str(exc)[:160]}"
        else:
            ema_errors[tf] = "fewer than 30 closed bars"

    journal = await asyncio.to_thread(_journal_context, journal_dir, symbol, now_ms)
    price = _num(snapshot.get("price"), 10) if snapshot else _num(rows[-1].close, 10)
    if price is None or price <= 0:
        raise ChartContextError("no positive last price in market data")

    data_quality = _data_quality(
        now_ms=now_ms,
        tf=tf,
        rows=rows,
        snapshot=snapshot,
        snapshot_error=snapshot_error,
        overlays=overlays,
        overlay_error=overlay_error,
        ema_errors=ema_errors,
        journal=journal,
        market=market,
    )

    closes = [k.close for k in rows]
    window = rows[-96:] if len(rows) >= 96 else rows
    window_high = max(k.high for k in window)
    window_low = min(k.low for k in window)
    payload = {
        "as_of": _iso(now_ms),
        "symbol": symbol,
        "market": market,
        "timeframe": tf,
        "data_quality": data_quality,
        "price_action": {
            "last_price": price,
            "change_24h_pct": _num(snapshot.get("change_24h_pct"), 4) if snapshot else None,
            "returns_pct": {
                f"{bars}b": _pct_change(closes, bars) for bars in (1, 3, 6, 12, 24, 48)
            },
            "window_high": _num(window_high, 10),
            "window_low": _num(window_low, 10),
            "range_position_pct": _num((price - window_low) / (window_high - window_low) * 100.0, 2)
            if window_high > window_low else None,
            "drawdown_from_window_high_pct": _num((price / window_high - 1.0) * 100.0, 4)
            if window_high > 0 else None,
            "recent_candles": _candle_features(rows),
        },
        "snapshot": _compact_snapshot(snapshot),
        "overlays": _compact_overlays(overlays, rows, price),
        "levels": _reference_levels(price, overlays, snapshot),
        "ema_cascade": _ema_summary(infos, ema_errors, now_ms),
        "sentinel": journal,
    }
    context = ChartContext(symbol=symbol, market=market, tf=tf, as_of_ms=now_ms, payload=payload)

    if use_cache:
        _CACHE[key] = context
        if len(_CACHE) > CONTEXT_CACHE_MAX:
            for old_key in sorted(_CACHE, key=lambda k: _CACHE[k].as_of_ms)[: len(_CACHE) - CONTEXT_CACHE_MAX]:
                _CACHE.pop(old_key, None)
    return context
