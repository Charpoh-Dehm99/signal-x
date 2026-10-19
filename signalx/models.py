"""Core dataclasses. Frozen to prevent accidental mutation mid-pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class Kline:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float
    trades: int
    taker_buy_base: float
    taker_buy_quote: float


@dataclass(frozen=True)
class AggTrade:
    timestamp: int
    price: float
    qty: float
    is_buyer_maker: bool


@dataclass(frozen=True)
class Pivot:
    index: int            # bar index of the extreme
    price: float
    kind: str             # 'H' or 'L'
    confirmed_index: int  # first bar index at which this pivot is usable
    volume: float = 0.0


@dataclass(frozen=True)
class LabeledPivot:
    pivot: Pivot
    label: str  # HH | HL | LH | LL | H | L


@dataclass(frozen=True)
class Break:
    kind: str          # 'bos' (with the bias) or 'choch' (against it, flips bias)
    direction: str     # 'up' (broke a swing high) or 'down' (broke a swing low)
    level: float       # broken swing extreme
    swing_index: int   # bar index of the broken swing extreme
    break_index: int   # bar index of the first close beyond the level


@dataclass(frozen=True)
class Zone:
    low: float
    high: float
    strength: float
    touches: int

    @property
    def mid(self) -> float:
        return (self.low + self.high) / 2.0


class Side(str, Enum):
    SHORT = "SHORT"


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: Side
    entry: float
    stop: float
    tp1: float
    tp2: float
    score: float
    reasons: tuple[str, ...]
