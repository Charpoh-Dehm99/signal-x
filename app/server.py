"""SIGNAL-X web API: async FastAPI over signalx.feeds + signalx.live.

Endpoints:
  GET /                        static SPA (no build step, no CDN)
  GET /api/health
  GET /api/gainers             USDT-M futures 24h gainers (bulk ticker)
  GET /api/logos               symbol -> CoinGecko icon URL (cosmetic, cached)
  GET /api/signals             sentinel journal: transitions + short signals
  GET /api/paper               paper companion state / P&L (read-only)
  POST /api/paper/halt         create the paper-entry kill switch; no orders
  GET /api/snapshot/{symbol}   one observation snapshot
  GET /api/sniper/{symbol}     SNIPER V3 1H/15m/5m observation (no orders)
  GET /api/sniper/watchlist    assigned symbols + latest monitor results
  POST /api/sniper/watchlist   assign a symbol to the SNIPER watchlist
  DELETE /api/sniper/watchlist/{symbol}  unassign a symbol
  GET /api/stream/{symbol}     SSE push loop for the SPA
  GET /api/klines/{symbol}     OHLCV candles for the chart (incl. live bar)
  GET /api/overlays/{symbol}   algorithm chart layers (closed bars only)
  GET /api/ema-cascade/{symbol} EMA 8/13/21 state per TF + cross propagation
  GET /api/chat/status          Ollama Cloud chat configuration (no key)
  GET /api/chat/skills          chart-interpretation skill catalog
  POST /api/chat                chart analyst reply (selected Ollama Cloud model)
  POST /api/chat/model          persist the dashboard model selection

Observation tool only: the G0 full-universe rerun KILLED the short-edge
hypothesis (reports/g0_full/VERDICT.md). There are no trading endpoints and
signal rows are observations, not validated trade signals.
Security posture: symbol/interval validated, upstream whitelist enforced in
signalx.feeds, errors mapped to HTTP without leaking internals.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException, Query, Request  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from signalx.feeds import (  # noqa: E402
    TF_MS,
    FeedError,
    detect_market,
    fetch_gainers,
    fetch_klines,
    make_client,
    poll_snapshot,
)
from signalx.ema import CASCADE_TFS, TfInfo, cascade_pattern, tf_crosses  # noqa: E402
from signalx.journal import SignalJournal  # noqa: E402
from signalx.live import LiveError  # noqa: E402
from signalx.logos import LogoError, fetch_coin_logos, resolve_coin_logo  # noqa: E402
from signalx.paper import PaperError, load_paper_state  # noqa: E402
from signalx.sniper_v3.assignments import (  # noqa: E402
    AssignmentError,
    AssignmentFullError,
    AssignmentStore,
)
from signalx.sniper_v3.data import (  # noqa: E402
    fetch_and_evaluate_detailed,
    funding_percentile,
    oi_crowding,
)
from signalx.sniper_v3.monitor import SniperMonitor  # noqa: E402
from signalx.sniper_v3.primitives import SniperError  # noqa: E402
from signalx.overlays import MIN_BARS, OverlayError, build_overlays  # noqa: E402
from signalx.validation import ValidationError, validate_interval, validate_symbol  # noqa: E402
from signalx.chat import (  # noqa: E402
    CHART_SKILLS,
    MAX_SKILLS,
    ChatConfig,
    ChatConfigError,
    ChartContextError,
    ChatRequestError,
    OllamaCloudError,
    build_chart_context,
    generate_reply,
    public_models,
    public_skills,
    save_chat_model,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
JOURNAL_DIR = Path(os.environ.get("SIGNALX_JOURNAL_DIR", str(ROOT / "data" / "journal")))
HEARTBEAT_PATH = Path(os.environ.get(
    "SIGNALX_HEARTBEAT", str(ROOT / "data" / "sentinel_heartbeat.json")
))
HEARTBEAT_STALE_MS = 5 * 60 * 1000  # sentinel sweeps every 60s; 5m = offline
PAPER_DIR = Path(os.environ.get(
    "SIGNALX_PAPER_DIR", str(ROOT / "data" / "paper")
))
PAPER_STATE_PATH = PAPER_DIR / "state.json"
PAPER_HEARTBEAT_PATH = PAPER_DIR / "heartbeat.json"
PAPER_HALT_PATH = Path(os.environ.get(
    "SIGNALX_TRADING_HALT", str(Path.home() / ".config" / "signal-x" / "TRADING_HALT")
))
PAPER_HEARTBEAT_STALE_MS = 5 * 60 * 1000
SNIPER_DIR = Path(os.environ.get(
    "SIGNALX_SNIPER_DIR", str(ROOT / "data" / "sniper")
))
SNIPER_ASSIGNMENTS_PATH = SNIPER_DIR / "assignments.json"
SNIPER_RESULTS_PATH = SNIPER_DIR / "latest.json"
sniper_store = AssignmentStore(SNIPER_ASSIGNMENTS_PATH)
MIN_STREAM_INTERVAL_S = 1.0
DEFAULT_STREAM_INTERVAL_S = 30.0
MAX_STREAM_INTERVAL_S = 3600.0
CHART_INTERVALS = frozenset(TF_MS)
ASCII_SYM_RE = re.compile(r"[A-Z0-9]{1,20}")
LOGO_CACHE_TTL_S = 3600.0
LOGO_EXTRA_TTL_S = 86400.0
LOGO_RESOLVE_BUDGET = 12
LOGO_EXTRA_MAX = 1000
EMA_CACHE_MAX = 256
_logo_cache: dict = {"attempt_ms": 0, "updated_ms": None, "symbols": {}, "extra": {}}
EMA_TFS = CASCADE_TFS
EMA_CACHE_TTL_S = 15.0
_ema_cache: dict = {}


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    # The service trims each turn to 4k chars and the total to 12k. Accept a
    # full reply here so a follow-up question is not rejected before trimming.
    content: str = Field(min_length=1, max_length=30_000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    symbol: str = Field(min_length=1, max_length=20)
    market: Literal["auto", "spot", "futures"] = "auto"
    tf: str = Field(default="5m", max_length=8)
    history: list[ChatTurn] = Field(default_factory=list, max_length=10)


class ChatModelRequest(BaseModel):
    model: str = Field(min_length=1, max_length=120)


class PaperHaltRequest(BaseModel):
    reason: str = Field(default="manual dashboard halt", max_length=200)


class SniperAssignRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = make_client()
    monitor = SniperMonitor(
        app.state.client,
        sniper_store,
        SNIPER_RESULTS_PATH,
    )
    app.state.sniper_monitor = monitor
    task = monitor.start()
    try:
        yield
    finally:
        monitor.stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await app.state.client.aclose()


app = FastAPI(
    title="SIGNAL-X Monitor",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)

class SecHeadersMiddleware:
    """Pure-ASGI middleware adding hardening headers to every HTTP response.

    Response headers are augmented in http.response.start without consuming or
    buffering the body, so SSE streams keep flowing untouched. Frontend needs
    'unsafe-inline' styles for a few palette-driven inline custom properties:
    everything else from upstream is text-escaped at render time.
    """

    HEADERS: tuple[tuple[str, str], ...] = (
        ("Content-Security-Policy",
         "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
         "img-src 'self' data: https:; connect-src 'self'; font-src 'self'; "
         "object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
        ("Cross-Origin-Resource-Policy", "same-origin"),
    )
    _ENCODED = tuple((k.encode("latin-1"), v.encode("latin-1")) for k, v in HEADERS)

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapped(message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                path = scope.get("path", "")
                # Localhost tool: never let an edited asset hide behind a
                # heuristic cache on mobile (stale chart.js caused stacked
                # candles on Android). Revalidate assets, never cache API data.
                if path == "/" or path.startswith("/static/"):
                    if b"cache-control" not in present:
                        headers.append((b"cache-control", b"no-cache"))
                elif path.startswith("/api/") and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                for k, v in self._ENCODED:
                    if k.lower() not in present:
                        headers.append((k, v))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapped)


app.add_middleware(SecHeadersMiddleware)


async def get_client(request: Request) -> httpx.AsyncClient:
    client = getattr(request.app.state, "client", None)
    if client is None:  # lazy fallback when lifespan did not run (ASGI tests)
        client = make_client()
        request.app.state.client = client
    return client


def _jsonable(obj: object) -> object:
    """Convert dataclasses (Zone boxes etc.) to plain JSON-safe structures.

    FastAPI's encoder does this for regular responses; the SSE path uses raw
    json.dumps and must not choke on dataclass fields.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    return obj


def _clean_symbol(symbol: str) -> str:
    try:
        return validate_symbol(symbol.upper())
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _clean_tf(tf: str) -> str:
    try:
        return validate_interval(tf)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _clean_chart_tf(tf: str) -> str:
    """Chart timeframes: validated AND supported by the feed layer."""
    clean = _clean_tf(tf)
    if clean not in CHART_INTERVALS:
        raise HTTPException(
            status_code=400,
            detail=f"chart timeframe not supported: {clean!r} (use one of {sorted(CHART_INTERVALS)})",
        )
    return clean


def _map_error(exc: Exception) -> HTTPException:
    if isinstance(exc, FeedError):
        return HTTPException(status_code=502, detail=f"upstream feed error: {exc}")
    if isinstance(exc, (LiveError, OverlayError, SniperError)):
        return HTTPException(status_code=422, detail=f"insufficient/invalid data: {exc}")
    return HTTPException(status_code=400, detail=str(exc))


def _sentinel_status(now_ms: int) -> dict:
    """Freshness of the sentinel daemon from its sweep heartbeat file."""
    try:
        hb = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"known": False, "running": False, "age_ms": None}
    if not isinstance(hb, dict) or not isinstance(hb.get("ts_ms"), int):
        return {"known": False, "running": False, "age_ms": None}
    age_ms = max(0, now_ms - hb["ts_ms"])
    out = {"known": True, "running": age_ms <= HEARTBEAT_STALE_MS,
           "age_ms": age_ms, "ts_ms": hb["ts_ms"]}
    for key in ("tf", "symbols", "active", "rows", "journal_rows"):
        if key in hb:
            out[key] = hb[key]
    return out


@app.get("/api/health")
async def health() -> dict:
    now_ms = int(time.time() * 1000)
    return {
        "ok": True,
        "tool": "signal-x",
        "note": "observation only; G0 rerun killed 2026-10-17 — no validated edge",
        "now_ms": now_ms,
        "sentinel": _sentinel_status(now_ms),
    }


@app.get("/api/chat/status")
async def chat_status() -> dict:
    """Browser-safe chat status. The API key never leaves the server."""
    try:
        cfg = ChatConfig.from_env()
    except ChatConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        **cfg.public(),
        "models": public_models(),
        "skills": len(CHART_SKILLS),
        "max_skills": MAX_SKILLS,
        "max_history": 10,
    }


@app.post("/api/chat/model")
async def set_chat_model(req: ChatModelRequest) -> dict:
    """Persist a model selection; subsequent chat requests use it."""
    try:
        save_chat_model(req.model)
        cfg = ChatConfig.from_env()
    except ChatConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        **cfg.public(),
        "models": public_models(),
        "skills": len(CHART_SKILLS),
        "max_skills": MAX_SKILLS,
        "max_history": 10,
    }


@app.get("/api/chat/skills")
async def chat_skills() -> dict:
    """The full chart-interpretation lens catalog."""
    return {"count": len(CHART_SKILLS), "skills": public_skills()}


@app.post("/api/chat")
async def chat(
    req: ChatRequest,
    client: httpx.AsyncClient = Depends(get_client),
) -> dict:
    """Interpret the current chart with MiniMax-M3.1 Flash via Ollama Cloud.

    The chart packet is built server-side from the same validated feeds and
    closed-bar engines as the dashboard. Chat history is capped and sanitized;
    the model cannot request tools or fetch additional data.
    """
    try:
        cfg = ChatConfig.from_env()
    except ChatConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if not cfg.configured:
        raise HTTPException(
            status_code=503,
            detail="chat is not configured: set OLLAMA_API_KEY (or SIGNALX_OLLAMA_KEY_FILE) and retry",
        )

    sym = _clean_symbol(req.symbol)
    tf = _clean_chart_tf(req.tf)
    market = req.market
    try:
        if market == "auto":
            market = await detect_market(client, sym)
        context = await build_chart_context(
            client,
            sym,
            market,
            tf,
            int(time.time() * 1000),
            journal_dir=JOURNAL_DIR,
        )
        return await generate_reply(
            cfg,
            req.message,
            context,
            [turn.model_dump() for turn in req.history],
        )
    except ChatRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ChartContextError as exc:
        raise HTTPException(
            status_code=502, detail=f"chart data unavailable: {str(exc)[:300]}"
        ) from exc
    except OllamaCloudError as exc:
        raise HTTPException(
            status_code=exc.status_code or 502, detail=str(exc)[:500]
        ) from exc
    except (FeedError, LiveError, OverlayError, ValidationError) as exc:
        raise _map_error(exc) from exc


@app.get("/api/signals")
async def signals(
    limit: int = Query(100, ge=1, le=1000),
    signals_only: bool = Query(False),
) -> dict:
    """Sentinel journal rows (newest first) + latest known state per symbol.

    Rows come from the day-partitioned Parquet journal written by
    scripts/sentinel.py. Transitions are sparse, so the response also carries
    `now_ms` + `latest_ts_ms` and the sentinel's sweep heartbeat; the UI must
    never present fetch time as data freshness. Empty journal => empty rows.
    """
    all_rows = SignalJournal(JOURNAL_DIR).read_all()
    latest: dict[str, dict] = {}
    for row in all_rows:
        latest[row["symbol"]] = {
            "state": row["state"],
            "event": row["event"],
            "prev_state": row["prev_state"],
            "ts_ms": row["ts_ms"],
        }
    rows = [r for r in all_rows if r["event"] == "SIGNAL"] if signals_only else all_rows
    rows = rows[-limit:]
    rows.reverse()
    now_ms = int(time.time() * 1000)
    latest_ts_ms = max((r["ts_ms"] for r in all_rows), default=None)
    return {
        "rows": rows,
        "latest": latest,
        "total": len(all_rows),
        "now_ms": now_ms,
        "latest_ts_ms": latest_ts_ms,
        "sentinel": _sentinel_status(now_ms),
    }


def _paper_heartbeat(now_ms: int) -> dict:
    try:
        hb = json.loads(PAPER_HEARTBEAT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"known": False, "running": False, "age_ms": None}
    if not isinstance(hb, dict) or not isinstance(hb.get("ts_ms"), int):
        return {"known": False, "running": False, "age_ms": None}
    age_ms = max(0, now_ms - hb["ts_ms"])
    out = {"known": True, "running": age_ms <= PAPER_HEARTBEAT_STALE_MS,
           "age_ms": age_ms, "ts_ms": hb["ts_ms"]}
    for key in ("equity", "open_positions", "halted", "last_signal_ts", "source"):
        if key in hb:
            out[key] = hb[key]
    return out


@app.get("/api/paper")
async def paper() -> dict:
    """Read-only paper companion state. No exchange client, no order endpoint."""
    now_ms = int(time.time() * 1000)
    heartbeat = _paper_heartbeat(now_ms)
    if not PAPER_STATE_PATH.exists():
        return {
            "known": False,
            "mode": "paper",
            "now_ms": now_ms,
            "heartbeat": heartbeat,
            "halted": PAPER_HALT_PATH.exists(),
            "manual_halt": PAPER_HALT_PATH.exists(),
            "message": "paper companion not started",
        }
    try:
        broker = load_paper_state(
            PAPER_STATE_PATH,
            manual_halt=PAPER_HALT_PATH.exists(),
            now_ms=now_ms,
        )
        if broker is None:
            raise PaperError("state missing after existence check")
        state = broker.snapshot(now_ms)
    except (OSError, ValueError, PaperError) as exc:
        raise HTTPException(status_code=500, detail=f"paper state unreadable: {str(exc)[:300]}") from exc
    return {
        "known": True,
        "now_ms": now_ms,
        "heartbeat": heartbeat,
        **state,
    }


@app.post("/api/paper/halt")
async def paper_halt(req: PaperHaltRequest) -> dict:
    """Create the kill-switch file. This blocks new paper entries only."""
    reason = req.reason.strip() or "manual dashboard halt"
    try:
        PAPER_HALT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PAPER_HALT_PATH.with_name(PAPER_HALT_PATH.name + ".tmp")
        tmp.write_text(reason + "\n", encoding="utf-8")
        os.replace(tmp, PAPER_HALT_PATH)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot write halt file: {str(exc)[:200]}") from exc
    return {"ok": True, "halted": True, "reason": reason}


@app.get("/api/gainers")
async def gainers(
    top: int = Query(50, ge=1, le=500),
    min_quote_volume: float = Query(1_000_000.0, ge=0.0),
    client: httpx.AsyncClient = Depends(get_client),
) -> dict:
    try:
        rows, universe = await fetch_gainers(
            client, top=top, min_quote_volume=min_quote_volume, with_universe=True
        )
    except FeedError as exc:
        raise _map_error(exc) from exc
    top1_count = max(1, -(-universe // 100))
    return {"gainers": rows, "universe": universe, "top1_count": top1_count}


@app.get("/api/logos")
async def logos(
    symbols: str | None = Query(None, max_length=1200),
    client: httpx.AsyncClient = Depends(get_client),
) -> dict:
    """symbol -> icon URL for dashboard coin badges (CoinGecko, cosmetic).

    Base map = CoinGecko top ~1000 by market cap, refreshed hourly. With
    ?symbols=BTC,ETH,... any symbol missing from the base map is resolved on
    demand (CoinGecko exact-ticker search, then the Binance asset CDN) and
    cached for a day — definitive misses included, so probes are not repeated.
    Always HTTP 200; the SPA falls back to letter avatars for absent symbols.
    """
    now_ms = int(time.time() * 1000)
    if now_ms - _logo_cache["attempt_ms"] > LOGO_CACHE_TTL_S * 1000:
        _logo_cache["attempt_ms"] = now_ms
        try:
            fetched = await fetch_coin_logos(client)
        except LogoError:
            fetched = None
        if fetched is not None:
            _logo_cache["symbols"] = fetched
            _logo_cache["updated_ms"] = now_ms

    wanted: list[str] | None = None
    if symbols:
        cleaned = [s.strip().upper() for s in symbols.split(",")]
        # ASCII-only: Python's str.isalnum() accepts fullwidth/unicode letters
        # that must never reach URL templates or cache keys.
        wanted = list(dict.fromkeys(
            s for s in cleaned if ASCII_SYM_RE.fullmatch(s)
        ))[:60]

    base = _logo_cache["symbols"]
    extra = _logo_cache["extra"]
    if wanted is not None:
        budget = LOGO_RESOLVE_BUDGET
        for sym in wanted:
            if sym in base:
                continue
            rec = extra.get(sym)
            if isinstance(rec, dict) and now_ms - rec.get("ts", 0) < LOGO_EXTRA_TTL_S * 1000:
                continue
            if budget <= 0:
                break
            budget -= 1
            try:
                url = await resolve_coin_logo(client, sym)
            except LogoError:
                break  # transient upstream trouble; retried on a later refresh
            extra[sym] = {"url": url, "ts": now_ms}

    if len(extra) > LOGO_EXTRA_MAX:
        drop = sorted(
            (k for k, r in extra.items() if isinstance(r, dict)),
            key=lambda k: extra[k].get("ts", 0),
        )[: len(extra) - LOGO_EXTRA_MAX]
        for k in drop:
            extra.pop(k, None)

    out: dict[str, str] = {}
    if wanted is None:
        out.update(base)
        out.update({s: r["url"] for s, r in extra.items() if isinstance(r, dict) and r.get("url")})
    else:
        for sym in wanted:
            url = base.get(sym)
            if url is None:
                rec = extra.get(sym)
                url = rec.get("url") if isinstance(rec, dict) else None
            if url:
                out[sym] = url
    return {"symbols": out, "updated_ms": _logo_cache["updated_ms"]}


@app.get("/api/snapshot/{symbol}")
async def snapshot(
    symbol: str,
    market: str = Query("auto", pattern="^(auto|spot|futures)$"),
    tf: str = "5m",
    client: httpx.AsyncClient = Depends(get_client),
) -> dict:
    sym = _clean_symbol(symbol)
    tf = _clean_tf(tf)
    try:
        if market == "auto":
            market = await detect_market(client, sym)
        return await poll_snapshot(
            client, sym, market, tf, int(time.time() * 1000),
            include_microstructure=True,
        )
    except (FeedError, LiveError, ValidationError) as exc:
        raise _map_error(exc) from exc


def _sniper_monitor(request: Request) -> SniperMonitor | None:
    return getattr(request.app.state, "sniper_monitor", None)


@app.get("/api/sniper/watchlist")
async def sniper_watchlist(request: Request) -> dict:
    """Assigned symbols and the monitor's latest closed-bar evaluations."""
    monitor = _sniper_monitor(request)
    latest = monitor.snapshot() if monitor is not None else {
        "evaluating": False,
        "heartbeat_ms": None,
        "symbols": sniper_store.symbols(),
        "results": {},
        "last_error": None,
    }
    return {
        **latest,
        "assignments": [row.to_dict() for row in sniper_store.assignments()],
    }


@app.post("/api/sniper/watchlist")
async def sniper_assign(payload: SniperAssignRequest, request: Request) -> dict:
    """Assign a symbol; the monitor wakes and evaluates it on closed candles."""
    try:
        row, created = sniper_store.add(payload.symbol)
    except AssignmentFullError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AssignmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    monitor = _sniper_monitor(request)
    if monitor is not None:
        monitor.wake()
    return {
        "symbol": row.symbol,
        "created": created,
        "assignments": [a.to_dict() for a in sniper_store.assignments()],
    }


@app.delete("/api/sniper/watchlist/{symbol}")
async def sniper_unassign(symbol: str, request: Request) -> dict:
    try:
        sym = symbol.strip().upper()
        removed = sniper_store.remove(sym)
    except AssignmentError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail=f"not assigned: {sym}")
    monitor = _sniper_monitor(request)
    if monitor is not None:
        monitor.forget(sym)
        monitor.wake()
    return {
        "symbol": sym,
        "removed": True,
        "assignments": [a.to_dict() for a in sniper_store.assignments()],
    }


@app.get("/api/sniper/{symbol}")
async def sniper(
    symbol: str,
    client: httpx.AsyncClient = Depends(get_client),
) -> dict:
    """SNIPER V3 observation: 1H permission -> 15m location -> 5m timing.

    Observation only. A null decision means one or more causal gates failed;
    this endpoint never writes a journal SIGNAL and never places an order.
    """
    sym = _clean_symbol(symbol)
    try:
        inputs, decision, diagnostics = await fetch_and_evaluate_detailed(client, sym)
    except (FeedError, SniperError, ValidationError) as exc:
        raise _map_error(exc) from exc
    if not inputs.klines_5m:
        raise HTTPException(status_code=422, detail="no closed 5m candles")
    return {
        "symbol": sym,
        "status": "setup" if decision is not None else "no_setup",
        "as_of_ms": inputs.klines_5m[-1].close_time,
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


@app.get("/api/klines/{symbol}")
async def klines(
    symbol: str,
    market: str = Query("auto", pattern="^(auto|spot|futures)$"),
    tf: str = "5m",
    limit: int = Query(300, ge=3, le=500),
    client: httpx.AsyncClient = Depends(get_client),
) -> dict:
    """OHLCV candles for the chart. Keeps the trailing in-progress bar so the
    last candle ticks live; never used for signal decisions."""
    sym = _clean_symbol(symbol)
    tf = _clean_chart_tf(tf)
    try:
        if market == "auto":
            market = await detect_market(client, sym)
        rows = await fetch_klines(
            client, market, sym, tf, int(time.time() * 1000),
            limit=limit, min_closed=3, include_incomplete=True,
        )
    except (FeedError, ValidationError) as exc:
        raise _map_error(exc) from exc
    return {
        "symbol": sym,
        "market": market,
        "tf": tf,
        "step_ms": TF_MS[tf],
        "candles": [
            {"t": k.open_time, "o": k.open, "h": k.high, "l": k.low,
             "c": k.close, "v": k.volume, "qv": k.quote_volume}
            for k in rows
        ],
    }


@app.get("/api/overlays/{symbol}")
async def overlays(
    symbol: str,
    market: str = Query("auto", pattern="^(auto|spot|futures)$"),
    tf: str = "5m",
    limit: int = Query(400, ge=MIN_BARS, le=500),
    client: httpx.AsyncClient = Depends(get_client),
) -> dict:
    """Algorithm chart layers: pivots, structure, BOS, resistance zones,
    AVWAP, plateau box, RSI divergence — computed on CLOSED bars only.

    Display truncation: `breaks` = latest 6, `zones` = top 5, `divs`/`budivs`
    = latest 4 per direction. `avwap.anchor_mode` is `display_max_volume`
    (heaviest-volume bar over the loaded window, i.e. hindsight for display,
    never a signal input)."""
    sym = _clean_symbol(symbol)
    tf = _clean_chart_tf(tf)
    try:
        if market == "auto":
            market = await detect_market(client, sym)
        rows = await fetch_klines(
            client, market, sym, tf, int(time.time() * 1000),
            limit=limit, min_closed=MIN_BARS,
        )
        payload = build_overlays(rows)
    except (FeedError, ValidationError, OverlayError) as exc:
        raise _map_error(exc) from exc
    return {"symbol": sym, "market": market, "tf": tf, "step_ms": TF_MS[tf], **payload}


@app.get("/api/ema-cascade/{symbol}")
async def ema_cascade(
    symbol: str,
    market: str = Query("auto", pattern="^(auto|spot|futures)$"),
    limit: int = Query(300, ge=60, le=500),
    client: httpx.AsyncClient = Depends(get_client),
) -> dict:
    """Triple-EMA 8/13/21 state per timeframe + cross propagation timing.

    Closed bars only. For each of 5m/15m/1h/4h the payload carries the current
    stack state, the last few crosses (direction, bar, time) and EMA values;
    `pattern` times the cascade of a cross propagating from the lightest
    timeframe upward. A timeframe whose feed fails is listed in `errors`
    instead of failing the whole response; only a total feed blackout is 502.
    """
    sym = _clean_symbol(symbol)
    now_ms = int(time.time() * 1000)
    if market == "auto":
        try:
            market = await detect_market(client, sym)
        except (FeedError, ValidationError) as exc:
            raise _map_error(exc) from exc
    key = (sym, market, limit)
    hit = _ema_cache.get(key)
    if hit is not None and now_ms - hit[0] < EMA_CACHE_TTL_S * 1000:
        return hit[1]

    infos: list[TfInfo] = []
    errors: dict[str, str] = {}
    for tf in EMA_TFS:
        try:
            rows = await fetch_klines(
                client, market, sym, tf, now_ms, limit=limit, min_closed=30,
            )
        except (FeedError, ValidationError) as exc:
            errors[tf] = str(exc)[:160]
            continue
        infos.append(tf_crosses(
            tf, TF_MS[tf], [k.open_time for k in rows], [k.close for k in rows],
        ))
    if not infos:
        first = next(iter(errors.values()), "no usable klines")
        raise _map_error(FeedError(first))

    payload = {
        "symbol": sym,
        "market": market,
        "now_ms": now_ms,
        "tfs": [
            {
                "tf": i.tf,
                "state": i.state,
                "closed": i.closed,
                "step_ms": i.step_ms,
                "emas": {"8": i.emas[0], "13": i.emas[1], "21": i.emas[2]},
                "crosses": [
                    {"i": c.index, "t": c.time, "dir": c.direction, "prev": c.prev_state}
                    for c in i.crosses[-4:]
                ],
            }
            for i in sorted(infos, key=lambda x: x.step_ms)
        ],
        "pattern": cascade_pattern(infos, now_ms=now_ms),
        "errors": errors,
    }
    if len(_ema_cache) > EMA_CACHE_MAX:
        for k in sorted(_ema_cache, key=lambda k: _ema_cache[k][0])[: len(_ema_cache) - EMA_CACHE_MAX + 1]:
            _ema_cache.pop(k, None)
    _ema_cache[key] = (now_ms, payload)
    return payload


@app.get("/api/stream/{symbol}")
async def stream(
    symbol: str,
    market: str = Query("auto", pattern="^(auto|spot|futures)$"),
    tf: str = "5m",
    interval: float = Query(DEFAULT_STREAM_INTERVAL_S, ge=MIN_STREAM_INTERVAL_S, le=MAX_STREAM_INTERVAL_S),
    polls: int = Query(0, ge=0, le=10_000),
    client: httpx.AsyncClient = Depends(get_client),
) -> StreamingResponse:
    sym = _clean_symbol(symbol)
    tf = _clean_tf(tf)

    async def gen():
        mkt = market
        count = 0
        while True:
            count += 1
            try:
                if mkt == "auto":
                    mkt = await detect_market(client, sym)
                snap = await poll_snapshot(
                    client, sym, mkt, tf, int(time.time() * 1000),
                    include_microstructure=True,
                )
                payload = {"ok": True, "snapshot": _jsonable(snap)}
            except (FeedError, LiveError, ValidationError) as exc:
                payload = {"ok": False, "error": str(exc)[:300]}
            yield f"data: {json.dumps(payload)}\n\n"
            if polls and count >= polls:
                break
            await asyncio.sleep(interval)
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
