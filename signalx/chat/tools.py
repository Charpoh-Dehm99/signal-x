"""Read-only tool registry for the SIGNAL-X trading partner.

The dashboard already builds a chart packet for the loaded symbol. These tools
let the model ask for *additional* server-side evidence - other symbols, the
gainers board, journal rows, SNIPER diagnostics, the watchlist and paper state -
without giving it shell, HTTP, order or settings access.

Every tool returns a JSON-serialisable dict. Expected upstream/input failures
become ``{"ok": False, "error": ...}`` so the model can recover instead of the
whole chat request failing. Tool results are untrusted data, not instructions.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import tzinfo
from pathlib import Path
from typing import Any

import httpx

from ..feeds import (
    TF_MS,
    FeedError,
    detect_market,
    fetch_gainers,
    poll_snapshot,
)
from ..journal import JournalError, SignalJournal
from ..sniper_v3.assignments import AssignmentStore
from ..sniper_v3.data import (
    fetch_and_evaluate_detailed,
    funding_percentile,
    oi_crowding,
)
from ..sniper_v3.primitives import SniperError
from ..validation import ValidationError, validate_interval, validate_symbol
from .timeutil import local_iso, local_label, local_zone

MAX_ARG_CHARS = 8_000
MAX_TOOL_RESULT_CHARS = 24_000
MAX_SIGNALS = 100
MAX_GAINERS = 50
MARKETS = frozenset({"auto", "spot", "futures"})
CHART_TFS = tuple(TF_MS)

PaperReader = Callable[[], Awaitable[dict[str, Any]]]


class ToolInputError(ValueError):
    """Invalid tool name or arguments supplied by the model."""


def _local_fields(
    ms: int | float | None,
    prefix: str,
    tz: tzinfo | None = None,
) -> dict[str, str | None]:
    return {
        f"{prefix}_local": local_iso(ms, tz=tz),
        f"{prefix}_label": local_label(ms, tz=tz),
    }


@dataclass
class ToolRuntime:
    """Server-side dependencies a read-only tool needs."""

    client: httpx.AsyncClient
    journal_dir: Path
    sniper_store: AssignmentStore | None = None
    sniper_monitor: Any | None = None
    paper_reader: PaperReader | None = None
    display_tz: tzinfo | None = None
    client_now_ms: int | None = None

    def _now_ms(self) -> int:
        """Use the browser clock when present; it may have a different offset."""
        value = self.client_now_ms
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 4_102_444_800_000:
            return value
        return int(time.time() * 1000)

    async def execute(self, name: str, raw_arguments: object) -> dict[str, Any]:
        """Validate + execute one tool, returning a structured result."""
        try:
            args = _arguments(raw_arguments)
            if name == "get_gainers":
                return await self._gainers(args)
            if name == "get_snapshot":
                return await self._snapshot(args)
            if name == "get_signals":
                return await self._signals(args)
            if name == "get_sniper":
                return await self._sniper(args)
            if name == "get_sniper_watchlist":
                return await self._sniper_watchlist(args)
            if name == "get_paper":
                return await self._paper(args)
            raise ToolInputError(f"unknown tool: {name}")
        except ToolInputError as exc:
            return {"ok": False, "error": str(exc)}
        except (FeedError, ValidationError, SniperError, JournalError) as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:300]}"}
        except Exception as exc:  # noqa: BLE001 - tool failures must not kill chat
            # Do not leak secrets or stack traces into the model context.
            detail = getattr(exc, "detail", None)
            message = detail if isinstance(detail, str) else str(exc)
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {message[:300]}",
            }

    async def _gainers(self, args: dict[str, Any]) -> dict[str, Any]:
        _allow(args, {"top", "min_quote_volume"})
        top = _int_arg(args, "top", 10, 1, MAX_GAINERS)
        min_quote_volume = _float_arg(args, "min_quote_volume", 1_000_000.0, 0.0, 1e15)
        rows, universe = await fetch_gainers(
            self.client,
            top=top,
            min_quote_volume=min_quote_volume,
            with_universe=True,
        )
        now_ms = self._now_ms()
        return {
            "ok": True,
            "gainers": rows,
            "universe": universe,
            "top1_count": max(1, -(-universe // 100)),
            "as_of_ms": now_ms,
            "timezone": local_zone(self.display_tz),
            **_local_fields(now_ms, "as_of", self.display_tz),
        }

    async def _snapshot(self, args: dict[str, Any]) -> dict[str, Any]:
        _allow(args, {"symbol", "market", "tf"})
        symbol = _symbol_arg(args)
        market = _market_arg(args.get("market", "auto"))
        tf = _tf_arg(args.get("tf", "5m"))
        if market == "auto":
            market = await detect_market(self.client, symbol)
        # The browser clock is display-only. Feed validation uses the server
        # clock so a caller-supplied timestamp cannot reshape market data.
        fetch_now_ms = int(time.time() * 1000)
        snap = await poll_snapshot(
            self.client,
            symbol,
            market,
            tf,
            fetch_now_ms,
            include_microstructure=True,
        )
        display_ms = self._now_ms()
        return {
            "ok": True,
            "snapshot": snap,
            "as_of_ms": display_ms,
            "timezone": local_zone(self.display_tz),
            **_local_fields(display_ms, "as_of", self.display_tz),
        }

    async def _signals(self, args: dict[str, Any]) -> dict[str, Any]:
        _allow(args, {"limit", "signals_only"})
        limit = _int_arg(args, "limit", 20, 1, MAX_SIGNALS)
        signals_only = _bool_arg(args, "signals_only", True)
        root = Path(self.journal_dir)
        now_ms = self._now_ms()
        if not root.exists():
            return {
                "ok": True,
                "rows": [],
                "latest": {},
                "total": 0,
                "signals_only": signals_only,
                "now_ms": now_ms,
                "latest_ts_ms": None,
                "timezone": local_zone(self.display_tz),
                **_local_fields(now_ms, "now", self.display_tz),
            }
        all_rows = SignalJournal(root).read_all()
        latest: dict[str, dict[str, Any]] = {}
        for row in all_rows:
            latest[row["symbol"]] = {
                "state": row["state"],
                "event": row["event"],
                "prev_state": row["prev_state"],
                "ts_ms": row["ts_ms"],
                **_local_fields(row["ts_ms"], "ts", self.display_tz),
            }
        rows = [row for row in all_rows if row["event"] == "SIGNAL"] if signals_only else all_rows
        rows = rows[-limit:]
        rows.reverse()
        rows = [{**row, **_local_fields(row["ts_ms"], "ts", self.display_tz)} for row in rows]
        return {
            "ok": True,
            "rows": rows,
            "latest": latest,
            "total": len(all_rows),
            "signals_only": signals_only,
            "now_ms": now_ms,
            "latest_ts_ms": max((row["ts_ms"] for row in all_rows), default=None),
            "timezone": local_zone(self.display_tz),
            **_local_fields(now_ms, "now", self.display_tz),
        }

    async def _sniper(self, args: dict[str, Any]) -> dict[str, Any]:
        _allow(args, {"symbol"})
        symbol = _symbol_arg(args)
        inputs, decision, diagnostics = await fetch_and_evaluate_detailed(self.client, symbol)
        if not inputs.klines_5m:
            raise ToolInputError("no closed 5m candles for this symbol")
        as_of_ms = inputs.klines_5m[-1].close_time
        evaluated_ms = self._now_ms()
        return {
            "ok": True,
            "symbol": symbol,
            "status": "setup" if decision is not None else "no_setup",
            "as_of_ms": as_of_ms,
            "as_of_local": local_iso(as_of_ms, tz=self.display_tz),
            "as_of_label": local_label(as_of_ms, tz=self.display_tz),
            "evaluated_at_ms": evaluated_ms,
            "evaluated_at_local": local_iso(evaluated_ms, tz=self.display_tz),
            "evaluated_at_label": local_label(evaluated_ms, tz=self.display_tz),
            "timezone": local_zone(self.display_tz),
            "last_close": inputs.klines_5m[-1].close,
            "context": {
                "funding_rate": inputs.funding_rate,
                "funding_pctile": funding_percentile(inputs),
                "oi_now": inputs.oi_now,
                "oi_crowding": oi_crowding(inputs),
                "bars": {
                    "1h": len(inputs.klines_1h),
                    "15m": len(inputs.klines_15m),
                    "5m": len(inputs.klines_5m),
                },
            },
            "decision": decision.to_dict() if decision is not None else None,
            "diagnostics": diagnostics.to_dict(),
        }

    async def _sniper_watchlist(self, args: dict[str, Any]) -> dict[str, Any]:
        _allow(args, set())
        if self.sniper_store is None:
            raise ToolInputError("SNIPER watchlist store is unavailable")
        if self.sniper_monitor is not None:
            latest = self.sniper_monitor.snapshot()
        else:
            latest = {
                "evaluating": False,
                "heartbeat_ms": None,
                "symbols": self.sniper_store.symbols(),
                "results": {},
                "last_error": None,
            }
        now_ms = self._now_ms()
        return {
            "ok": True,
            **latest,
            "assignments": [row.to_dict() for row in self.sniper_store.assignments()],
            "as_of_ms": now_ms,
            "timezone": local_zone(self.display_tz),
            **_local_fields(now_ms, "as_of", self.display_tz),
        }

    async def _paper(self, args: dict[str, Any]) -> dict[str, Any]:
        _allow(args, set())
        if self.paper_reader is None:
            raise ToolInputError("paper state reader is unavailable")
        data = await self.paper_reader()
        if not isinstance(data, dict):
            raise ToolInputError("paper state reader returned an invalid payload")
        now_ms = self._now_ms()
        return {
            "ok": True,
            "paper": data,
            "as_of_ms": now_ms,
            "timezone": local_zone(self.display_tz),
            **_local_fields(now_ms, "as_of", self.display_tz),
        }


def _arguments(raw: object) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        args = raw
    elif isinstance(raw, str):
        if len(raw) > MAX_ARG_CHARS:
            raise ToolInputError("tool arguments are too large")
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            raise ToolInputError("tool arguments are not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ToolInputError("tool arguments must be a JSON object")
        args = parsed
    else:
        raise ToolInputError("tool arguments must be an object")
    if len(json.dumps(args, separators=(",", ":"), default=str)) > MAX_ARG_CHARS:
        raise ToolInputError("tool arguments are too large")
    return args


def _allow(args: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(args) - allowed
    if unknown:
        raise ToolInputError(f"unknown argument(s): {', '.join(sorted(unknown))}")


def _symbol_arg(args: dict[str, Any]) -> str:
    raw = args.get("symbol")
    if not isinstance(raw, str):
        raise ToolInputError("symbol is required")
    try:
        return validate_symbol(raw.strip().upper())
    except ValidationError as exc:
        raise ToolInputError(str(exc)) from exc


def _market_arg(raw: object) -> str:
    if not isinstance(raw, str) or raw not in MARKETS:
        raise ToolInputError(f"market must be one of {sorted(MARKETS)}")
    return raw


def _tf_arg(raw: object) -> str:
    try:
        tf = validate_interval(raw)
    except ValidationError as exc:
        raise ToolInputError(str(exc)) from exc
    if tf not in TF_MS:
        raise ToolInputError(f"snapshot timeframe must be one of {list(TF_MS)}")
    return tf


def _int_arg(
    args: dict[str, Any],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = args.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolInputError(f"{name} must be an integer")
    if raw < minimum or raw > maximum:
        raise ToolInputError(f"{name} must be in [{minimum}, {maximum}]")
    return raw


def _float_arg(
    args: dict[str, Any],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = args.get(name, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ToolInputError(f"{name} must be a number")
    value = float(raw)
    if not math.isfinite(value) or value < minimum or value > maximum:
        raise ToolInputError(f"{name} must be a finite number in [{minimum}, {maximum}]")
    return value


def _bool_arg(args: dict[str, Any], name: str, default: bool) -> bool:
    raw = args.get(name, default)
    if not isinstance(raw, bool):
        raise ToolInputError(f"{name} must be a boolean")
    return raw


def _tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": parameters,
                "additionalProperties": False,
            },
        },
    }


TOOL_SPECS: tuple[dict[str, Any], ...] = (
    _tool(
        "get_gainers",
        "Read the top USDT-M futures 24h gainers with liquidity filtering. "
        "Use when the user asks what is moving or wants candidates beyond the loaded chart. "
        "Read-only observation; never a trade recommendation.",
        {
            "top": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_GAINERS,
                "description": "Rows to return. Defaults to 10.",
            },
            "min_quote_volume": {
                "type": "number",
                "minimum": 0,
                "description": "Minimum 24h quote volume. Defaults to 1000000.",
            },
        },
    ),
    _tool(
        "get_snapshot",
        "Read a full closed-bar market snapshot for one symbol and timeframe: price, "
        "ATR, RSI, volume, compression, flow/CVD, large prints, book context, funding, "
        "OI and state. Use for a symbol other than the loaded chart.",
        {
            "symbol": {"type": "string", "description": "Binance symbol, e.g. BTCUSDT."},
            "market": {
                "type": "string",
                "enum": ["auto", "spot", "futures"],
                "description": "Market to inspect. Defaults to auto.",
            },
            "tf": {
                "type": "string",
                "enum": list(CHART_TFS),
                "description": "Closed-bar timeframe. Defaults to 5m.",
            },
        },
    ),
    _tool(
        "get_signals",
        "Read recent sentinel journal rows and the latest known state per symbol. "
        "Rows are historical state transitions, not live prices. Use signals_only=false "
        "to include all transitions, not just SIGNAL events.",
        {
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_SIGNALS,
                "description": "Newest rows to return. Defaults to 20.",
            },
            "signals_only": {
                "type": "boolean",
                "description": "Return only SIGNAL events. Defaults to true.",
            },
        },
    ),
    _tool(
        "get_sniper",
        "Evaluate SNIPER V3 for one symbol on closed 1H/15m/5m candles: 1H permission, "
        "15m location, 5m timing, risk plan, funding/OI context and first-failed-gate "
        "diagnostics. This is an observation engine, not a validated signal or order.",
        {
            "symbol": {"type": "string", "description": "Binance symbol, e.g. ETHUSDT."},
        },
    ),
    _tool(
        "get_sniper_watchlist",
        "Read the SNIPER watchlist assignments and the monitor's latest closed-bar "
        "results. Read-only; assigning or removing symbols is deliberately not exposed.",
        {},
    ),
    _tool(
        "get_paper",
        "Read the paper companion's equity, open positions, exits, halt state and "
        "heartbeat. Read-only; no order, halt or state-changing action is exposed.",
        {},
    ),
)


def tool_result_text(result: dict[str, Any]) -> str:
    """Serialise a result for the model, failing closed if it is too large."""
    text = json.dumps(result, separators=(",", ":"), ensure_ascii=False, default=str)
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return json.dumps(
        {
            "ok": False,
            "error": "tool result exceeded the size cap",
            "chars": len(text),
            "hint": "Narrow the query (fewer rows, one symbol) and retry.",
        },
        separators=(",", ":"),
    )
