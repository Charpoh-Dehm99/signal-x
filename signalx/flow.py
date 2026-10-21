"""Flow / fat-money proxy features from klines, agg trades, and book depth.

Observation-only instrumentation. These features describe aggressive flow,
large prints, and resting liquidity; they do not identify wallets and are not
wired into the sentinel state machine.
"""

from __future__ import annotations

from .book import DepthSnapshot, band_depth
from .models import AggTrade, Kline
from .validation import ValidationError, _strict_float, taker_direction

DEFAULT_FLOW_WINDOW = 12
DEFAULT_LARGE_PRINT_USD = 25_000.0
DEFAULT_BOOK_BAND_PCT = 0.5
CVD_RATIO_THRESHOLD = 0.05
BOOK_IMBALANCE_THRESHOLD = 0.20
PRICE_FLAT_PCT = 0.002
DIVERGENCE_PRICE_PCT = 0.005


class FlowError(RuntimeError):
    """Malformed flow input. Never swallow this."""


def _num(value: object, name: str) -> float:
    try:
        return _strict_float(value, name)
    except ValidationError as exc:
        raise FlowError(str(exc)) from exc


def _window_bounds(window: object) -> int:
    if isinstance(window, bool) or not isinstance(window, int) or window < 2:
        raise FlowError("window must be an int >= 2")
    return window


def cvd_quote_from_klines(klines: list[Kline]) -> list[float]:
    """Cumulative taker buy-minus-sell quote notional from kline fields."""
    if not isinstance(klines, list) or not klines:
        raise FlowError("klines must be a non-empty list")
    total = 0.0
    out: list[float] = []
    for k in klines:
        if not isinstance(k, Kline):
            raise FlowError("klines must contain Kline objects")
        total += 2.0 * _num(k.taker_buy_quote, "taker_buy_quote") - _num(
            k.quote_volume, "quote_volume"
        )
        out.append(total)
    return out


def flow_from_klines(klines: list[Kline], *, window: int = DEFAULT_FLOW_WINDOW) -> dict:
    """Aggressive-flow features from the last `window` closed klines."""
    window = _window_bounds(window)
    if not isinstance(klines, list) or len(klines) < window:
        raise FlowError(f"need >= {window} klines")
    tail = klines[-window:]
    total_qv = sum(_num(k.quote_volume, "quote_volume") for k in tail)
    total_tbq = sum(_num(k.taker_buy_quote, "taker_buy_quote") for k in tail)
    if total_qv > 0:
        cvd_net = 2.0 * total_tbq - total_qv
        cvd_ratio = cvd_net / total_qv
        taker_buy_ratio = total_tbq / total_qv
    else:
        cvd_net = cvd_ratio = taker_buy_ratio = None

    price_change_pct = None
    if tail[0].open > 0:
        price_change_pct = tail[-1].close / tail[0].open - 1.0

    cvd_series = cvd_quote_from_klines(tail)
    cvd_slope_ratio = None
    if total_qv > 0 and len(cvd_series) >= 3:
        cvd_slope_ratio = (cvd_series[-1] - cvd_series[-3]) / total_qv

    qvs = [_num(k.quote_volume, "quote_volume") for k in tail]
    mean_qv = total_qv / window
    large_bar_ratio = max(qvs) / mean_qv if mean_qv > 0 else None

    divergence = None
    if price_change_pct is not None and cvd_ratio is not None:
        if price_change_pct >= DIVERGENCE_PRICE_PCT and cvd_ratio <= -CVD_RATIO_THRESHOLD:
            divergence = "bearish"
        elif price_change_pct <= -DIVERGENCE_PRICE_PCT and cvd_ratio >= CVD_RATIO_THRESHOLD:
            divergence = "bullish"

    return {
        "price_change_pct": price_change_pct,
        "cvd_net_quote": cvd_net,
        "cvd_ratio": cvd_ratio,
        "cvd_slope_ratio": cvd_slope_ratio,
        "taker_buy_ratio": taker_buy_ratio,
        "large_bar_ratio": large_bar_ratio,
        "cvd_price_divergence": divergence,
    }


def flow_from_trades(
    trades: list[AggTrade], *, min_notional: float = DEFAULT_LARGE_PRINT_USD
) -> dict:
    """Large-print and net taker notional from recent agg trades."""
    if not isinstance(trades, list):
        raise FlowError("trades must be a list")
    min_notional = _num(min_notional, "min_notional")
    if min_notional <= 0:
        raise FlowError("min_notional must be positive")

    buy = sell = 0.0
    large_buy = large_sell = 0.0
    large_count = 0
    largest = 0.0
    largest_side = None
    for t in trades:
        if not isinstance(t, AggTrade):
            raise FlowError("trades must contain AggTrade objects")
        price = _num(t.price, "trade.price")
        qty = _num(t.qty, "trade.qty")
        if price <= 0 or qty <= 0:
            raise FlowError("trade price/qty must be positive")
        if not isinstance(t.is_buyer_maker, bool):
            raise FlowError("trade.is_buyer_maker must be bool")
        notional = price * qty
        side = taker_direction(t.is_buyer_maker)
        if side > 0:
            buy += notional
        else:
            sell += notional
        if notional > largest:
            largest = notional
            largest_side = "buy" if side > 0 else "sell"
        if notional >= min_notional:
            large_count += 1
            if side > 0:
                large_buy += notional
            else:
                large_sell += notional

    total = buy + sell
    large_total = large_buy + large_sell
    net = buy - sell
    large_net = large_buy - large_sell
    return {
        "trade_count": len(trades),
        "buy_notional": buy,
        "sell_notional": sell,
        "net_notional": net,
        "net_ratio": net / total if total > 0 else None,
        "large_print_min_usd": min_notional,
        "large_print_count": large_count,
        "large_print_buy_usd": large_buy,
        "large_print_sell_usd": large_sell,
        "large_print_net_usd": large_net,
        "large_print_net_ratio": large_net / large_total if large_total > 0 else None,
        "large_print_net_share": large_net / total if total > 0 else None,
        "large_print_share": large_total / total if total > 0 else None,
        "largest_print_usd": largest,
        "largest_print_side": largest_side,
    }


def flow_from_depth(
    snapshot: DepthSnapshot, *, band_pct: float = DEFAULT_BOOK_BAND_PCT
) -> dict:
    """Resting-liquidity features from one depth snapshot."""
    if not isinstance(snapshot, DepthSnapshot):
        raise FlowError("snapshot must be a DepthSnapshot")
    band_pct = _num(band_pct, "band_pct")
    if band_pct <= 0:
        raise FlowError("band_pct must be positive")
    bid_usd, ask_usd, imbalance = band_depth(snapshot, band_pct)
    best_bid = max(p for p, _ in snapshot.bids)
    best_ask = min(p for p, _ in snapshot.asks)
    mid = (best_bid + best_ask) / 2.0
    bias = None
    if imbalance is not None:
        if imbalance >= BOOK_IMBALANCE_THRESHOLD:
            bias = "bid"
        elif imbalance <= -BOOK_IMBALANCE_THRESHOLD:
            bias = "ask"
        else:
            bias = "balanced"
    return {
        "book_band_pct": band_pct,
        "book_bid_usd": bid_usd,
        "book_ask_usd": ask_usd,
        "book_imbalance": imbalance,
        "book_bias": bias,
        "book_spread_bps": (best_ask - best_bid) / mid * 10_000.0,
    }


def _fat_money_bias(features: dict) -> str | None:
    votes: list[int] = []
    cvd = features.get("cvd_ratio")
    if cvd is not None:
        if cvd >= CVD_RATIO_THRESHOLD:
            votes.append(1)
        elif cvd <= -CVD_RATIO_THRESHOLD:
            votes.append(-1)
    large = features.get("large_print_net_share")
    if large is not None:
        if large >= CVD_RATIO_THRESHOLD:
            votes.append(1)
        elif large <= -CVD_RATIO_THRESHOLD:
            votes.append(-1)
    book = features.get("book_imbalance")
    if book is not None:
        if book >= BOOK_IMBALANCE_THRESHOLD:
            votes.append(1)
        elif book <= -BOOK_IMBALANCE_THRESHOLD:
            votes.append(-1)
    if not votes:
        return None
    if len(votes) < 2:
        return "mixed"
    score = sum(votes)
    need = len(votes) // 2 + 1
    if score >= need:
        return "bull"
    if score <= -need:
        return "bear"
    return "mixed"


def _absorption(features: dict) -> str | None:
    price = features.get("price_change_pct")
    cvd = features.get("cvd_ratio")
    large = features.get("large_print_net_share")
    if price is None or cvd is None:
        return None
    if (
        price >= -PRICE_FLAT_PCT
        and cvd <= -CVD_RATIO_THRESHOLD
        and (large is None or large <= 0)
    ):
        return "distribution"
    if (
        price <= PRICE_FLAT_PCT
        and cvd >= CVD_RATIO_THRESHOLD
        and (large is None or large >= 0)
    ):
        return "accumulation"
    return None


def flow_features(
    klines: list[Kline],
    *,
    agg_trades: list[AggTrade] | None = None,
    depth: DepthSnapshot | None = None,
    window: int = DEFAULT_FLOW_WINDOW,
    min_notional: float = DEFAULT_LARGE_PRINT_USD,
    band_pct: float = DEFAULT_BOOK_BAND_PCT,
) -> dict:
    """Compose kline flow with optional large-print and book features."""
    out = {
        "window_bars": _window_bounds(window),
        "source": "klines",
        "large_print_min_usd": _num(min_notional, "min_notional"),
        "book_band_pct": _num(band_pct, "band_pct"),
        "trade_count": None,
        "buy_notional": None,
        "sell_notional": None,
        "net_notional": None,
        "net_ratio": None,
        "large_print_count": None,
        "large_print_buy_usd": None,
        "large_print_sell_usd": None,
        "large_print_net_usd": None,
        "large_print_net_ratio": None,
        "large_print_net_share": None,
        "large_print_share": None,
        "largest_print_usd": None,
        "largest_print_side": None,
        "book_bid_usd": None,
        "book_ask_usd": None,
        "book_imbalance": None,
        "book_bias": None,
        "book_spread_bps": None,
    }
    out.update(flow_from_klines(klines, window=window))
    sources = ["klines"]
    if agg_trades is not None:
        out.update(flow_from_trades(agg_trades, min_notional=min_notional))
        sources.append("trades")
    if depth is not None:
        out.update(flow_from_depth(depth, band_pct=band_pct))
        sources.append("book")
    out["source"] = "+".join(sources)
    out["fat_money_bias"] = _fat_money_bias(out)
    out["absorption"] = _absorption(out)
    return out
