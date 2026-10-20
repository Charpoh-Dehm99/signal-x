"""SIGNAL-X web API: async FastAPI over signalx.feeds + signalx.live.

Endpoints:
  GET /                        static SPA (no build step, no CDN)
  GET /api/health
  GET /api/gainers             USDT-M futures 24h gainers (bulk ticker)
  GET /api/logos               symbol -> CoinGecko icon URL (cosmetic, cached)
  GET /api/signals             sentinel journal: transitions + short signals
  GET /api/snapshot/{symbol}   one observation snapshot
  GET /api/stream/{symbol}     SSE push loop for the SPA
  GET /api/klines/{symbol}     OHLCV candles for the chart (incl. live bar)
  GET /api/overlays/{symbol}   algorithm chart layers (closed bars only)
  GET /api/ema-cascade/{symbol} EMA 8/13/21 state per TF + cross propagation

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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from fastapi import Depends, FastAPI, HTTPException, Query, Request  # noqa: E402
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
from signalx.overlays import MIN_BARS, OverlayError, build_overlays  # noqa: E402
from signalx.validation import ValidationError, validate_interval, validate_symbol  # noqa: E402

STATIC_DIR = Path(__file__).resolve().parent / "static"
JOURNAL_DIR = Path(os.environ.get("SIGNALX_JOURNAL_DIR", str(ROOT / "data" / "journal")))
HEARTBEAT_PATH = Path(os.environ.get(
    "SIGNALX_HEARTBEAT", str(ROOT / "data" / "sentinel_heartbeat.json")
))
HEARTBEAT_STALE_MS = 5 * 60 * 1000  # sentinel sweeps every 60s; 5m = offline
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = make_client()
    try:
        yield
    finally:
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
    if isinstance(exc, (LiveError, OverlayError)):
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
        return await poll_snapshot(client, sym, market, tf, int(time.time() * 1000))
    except (FeedError, LiveError, ValidationError) as exc:
        raise _map_error(exc) from exc


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
                snap = await poll_snapshot(client, sym, mkt, tf, int(time.time() * 1000))
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
