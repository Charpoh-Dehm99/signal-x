"""Paper companion loop: journal SIGNAL rows -> simulated short positions.

The companion is intentionally separate from the sentinel and the web API:
sentinel writes observations, the companion consumes them, and the dashboard
only reads the resulting paper state. No exchange client lives in this module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .journal import SignalJournal
from .paper import (
    PaperBroker,
    PaperConfig,
    load_paper_state,
    save_paper_state,
    write_paper_heartbeat,
)

PriceFn = Callable[[str], Awaitable[float]]


@dataclass(frozen=True)
class CompanionConfig:
    journal_dir: Path
    paper_dir: Path
    halt_path: Path
    market: str = "futures"
    interval_s: float = 15.0
    paper_config: PaperConfig = field(default_factory=PaperConfig)

    def __post_init__(self) -> None:
        if self.market not in ("spot", "futures"):
            raise ValueError("market must be 'spot' or 'futures'")
        if not math.isfinite(self.interval_s) or self.interval_s <= 0:
            raise ValueError("interval_s must be positive")

    @property
    def state_path(self) -> Path:
        return self.paper_dir / "state.json"

    @property
    def heartbeat_path(self) -> Path:
        return self.paper_dir / "heartbeat.json"


def load_or_create_broker(config: CompanionConfig, *, now_ms: int) -> PaperBroker:
    """Resume paper state, or start a fresh account without replaying old signals."""
    journal = SignalJournal(config.journal_dir)
    broker = load_paper_state(
        config.state_path,
        manual_halt=config.halt_path.exists(),
        now_ms=now_ms,
    )
    if broker is None:
        broker = PaperBroker(config.paper_config, now_ms=now_ms)
        rows = journal.read_all()
        latest = max(
            (r.get("ts_ms") for r in rows if isinstance(r.get("ts_ms"), int)),
            default=now_ms,
        )
        broker.last_signal_ts = int(latest)
    broker.set_manual_halt(config.halt_path.exists(), f"halt file: {config.halt_path}")
    return broker


class Companion:
    def __init__(
        self,
        broker: PaperBroker,
        journal: SignalJournal,
        config: CompanionConfig,
        price_fn: PriceFn,
    ) -> None:
        self.broker = broker
        self.journal = journal
        self.config = config
        self.price_fn = price_fn

    async def step(self, now_ms: int) -> list[dict]:
        """One paper evaluation pass. Returns human-readable events."""
        self.broker.set_manual_halt(self.config.halt_path.exists(), f"halt file: {self.config.halt_path}")
        self.broker.roll_day(now_ms)
        events: list[dict] = []

        rows = self.journal.read_all()
        signals = [
            r for r in rows
            if isinstance(r, dict)
            and r.get("event") == "SIGNAL"
            and isinstance(r.get("ts_ms"), int)
            and not isinstance(r.get("ts_ms"), bool)
            and r["ts_ms"] > self.broker.last_signal_ts
        ]
        signals.sort(key=lambda r: (r["ts_ms"], str(r.get("symbol", ""))))

        for sig in signals:
            sym = str(sig.get("symbol", ""))
            if sym in self.broker.positions:
                self.broker.last_signal_ts = max(self.broker.last_signal_ts, sig["ts_ms"])
                events.append({"action": "SKIP", "symbol": sym, "reason": "position already open"})
                continue
            try:
                price = await self.price_fn(sym)
            except Exception as exc:  # quote failures must not kill the loop
                self.broker.last_signal_ts = max(self.broker.last_signal_ts, sig["ts_ms"])
                events.append({
                    "action": "QUOTE_ERROR",
                    "symbol": sym,
                    "reason": f"{type(exc).__name__}: {str(exc)[:120]}",
                })
                continue
            result = self.broker.open_from_signal(sig, price, now_ms)
            result["action"] = "OPEN" if result.get("opened") else "SKIP"
            events.append(result)

        prices: dict[str, float] = {}
        for sym in list(self.broker.positions):
            try:
                prices[sym] = await self.price_fn(sym)
            except Exception as exc:
                events.append({
                    "action": "QUOTE_ERROR",
                    "symbol": sym,
                    "reason": f"{type(exc).__name__}: {str(exc)[:120]}",
                })
        for fill in self.broker.mark_prices(prices, now_ms):
            events.append({"action": "EXIT", **fill})
        return events

    def save(self, now_ms: int) -> None:
        save_paper_state(self.broker, self.config.state_path, now_ms)
        write_paper_heartbeat(self.broker, self.config.heartbeat_path, now_ms)
