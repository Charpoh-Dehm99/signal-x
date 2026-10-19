"""Static configuration. Tunables live here, validated by tests."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScanConfig:
    min_change_pct: float = 15.0          # 24h price change floor
    min_quote_volume_usdt: float = 5_000_000.0
    max_candidates: int = 50
    quote_asset: str = "USDT"


@dataclass(frozen=True)
class SignalConfig:
    pump_threshold: float = 0.6           # scanner pump_score to enter PUMP state
    arm_threshold: float = 0.5            # confluence p_breakdown -> ARMED
    signal_threshold: float = 0.7         # confluence p_breakdown -> SIGNAL tier
    breakout_volume_mult: float = 1.5     # volume vs 20-bar mean on BOS bar
    stop_atr_buffer: float = 0.5          # stop = swing high + buffer * ATR
    risk_per_trade: float = 0.01          # 1% equity per trade
    plateau_timeout_bars: int = 180       # 3h on 1m bars
    armed_timeout_bars: int = 60          # 1h on 1m bars


DEFAULT_SCAN = ScanConfig()
DEFAULT_SIGNAL = SignalConfig()
