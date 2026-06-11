"""Unit tests for indicator calculations."""

import pytest

from bot.indicators import (
    adx_series,
    atr_series,
    opening_range,
    or_width,
    session_vwap_series,
    wilder_rma,
)


def test_wilder_rma_seed():
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    rma = wilder_rma(values, 3)
    assert rma[0] is None
    assert rma[1] is None
    assert rma[2] == pytest.approx(2.0)
    assert rma[3] == pytest.approx(8.0 / 3.0)
    assert rma[4] == pytest.approx(31.0 / 9.0)


def test_session_vwap_resets():
    tp = [100.0, 110.0, 105.0, 120.0]
    vol = [1000.0, 1000.0, 1000.0, 1000.0]
    sessions = ["2025-01-01", "2025-01-01", "2025-01-02", "2025-01-02"]
    vwap = session_vwap_series(tp, vol, sessions)
    assert vwap[0] == pytest.approx(100.0)
    assert vwap[1] == pytest.approx(105.0)
    assert vwap[2] == pytest.approx(105.0)
    assert vwap[3] == pytest.approx(112.5)


def test_opening_range():
    highs = [100, 105, 103, 110]
    lows = [98, 99, 100, 101]
    flags = [True, True, True, False]
    hi, lo, defined = opening_range(highs, lows, flags)
    assert defined
    assert hi == 105
    assert lo == 98


def test_or_width():
    assert or_width(125.0, 100.0) == pytest.approx(25.0)
    assert or_width(None, 100.0) is None


def test_atr_series_length():
    highs = [10 + i for i in range(20)]
    lows = [9 + i for i in range(20)]
    closes = [9.5 + i for i in range(20)]
    atr = atr_series(highs, lows, closes, 14)
    assert atr[12] is None
    assert atr[13] is not None
    assert atr[19] is not None


def test_adx_series_non_negative():
    highs = [10 + i * 0.5 for i in range(30)]
    lows = [9 + i * 0.5 for i in range(30)]
    closes = [9.5 + i * 0.5 for i in range(30)]
    adx = adx_series(highs, lows, closes, 14)
    valid = [v for v in adx if v is not None]
    assert len(valid) > 0
    assert all(v >= 0 for v in valid)
