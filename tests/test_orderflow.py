"""Order-flow tests: CVD, OI matrix, squeeze guard (fail-closed)."""

from __future__ import annotations

import pytest

from signalx.models import AggTrade
from signalx.orderflow import (
    cvd_from_agg_trades,
    cvd_from_klines,
    oi_price_regime,
    squeeze_guard,
)
from signalx.validation import ValidationError
from tests.helpers import mk_kline


def test_cvd_from_klines():
    k1 = mk_kline(0, 1, 1, 1, 1, v=1000.0, tbb=600.0)
    k2 = mk_kline(60_000, 1, 1, 1, 1, v=1000.0, tbb=400.0)
    assert cvd_from_klines([k1]) == [200.0]
    assert cvd_from_klines([k1, k2]) == [200.0, 0.0]


def test_cvd_from_agg_trades():
    trades = [
        AggTrade(1, 1.0, 2.0, False),  # taker bought => +2
        AggTrade(2, 1.0, 3.0, True),   # taker sold    => -3
    ]
    assert cvd_from_agg_trades(trades) == [2.0, -1.0]


def test_oi_price_regimes():
    assert oi_price_regime(1.0, 1.0, True) == "TREND_FUEL"
    assert oi_price_regime(1.0, 0.01, True) == "ABSORPTION"
    assert oi_price_regime(1.0, 0.01, False) == "NEUTRAL"
    assert oi_price_regime(-1.0, -1.0, False) == "FLUSH"
    assert oi_price_regime(-1.0, 1.0, False) == "NEUTRAL"


def test_oi_price_regime_fail_loud_on_missing():
    with pytest.raises(ValidationError):
        oi_price_regime(None, 1.0, False)
    with pytest.raises(ValueError):
        oi_price_regime(1.0, 1.0, False, flat_pct=-0.1)


def test_squeeze_guard_blocks_extremes():
    assert squeeze_guard(True, 95.0, True) is True
    assert squeeze_guard(True, 50.0, True) is False
    assert squeeze_guard(False, 95.0, True) is False
    assert squeeze_guard(True, 95.0, False) is False


@pytest.mark.parametrize(
    "args",
    [
        (None, 95.0, True),
        (True, None, True),
        (True, 95.0, None),
    ],
)
def test_squeeze_guard_fails_closed_on_unknown(args):
    assert squeeze_guard(*args) is True
