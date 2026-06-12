"""Stop/target math for v3.9."""

from __future__ import annotations

import pytest

from bot.config import StrategyConfig
from bot.state import PositionSide
from bot.stops import capped_stops, v38_stops


def test_capped_stops_ce_structure_capped_by_atr() -> None:
    cfg = StrategyConfig(atr_sl_mult=1.4, rr_ratio=2.0, sl_buffer_pts=10.0)
    entry = 100.0
    structure = 80.0
    atr = 10.0
    stop, target = capped_stops(PositionSide.CE, entry, structure, atr, cfg)
    assert stop == pytest.approx(86.0)
    assert target == pytest.approx(128.0)


def test_capped_stops_pe_structure_capped_by_atr() -> None:
    cfg = StrategyConfig(atr_sl_mult=1.4, rr_ratio=2.0, sl_buffer_pts=10.0)
    entry = 100.0
    structure = 120.0
    atr = 10.0
    stop, target = capped_stops(PositionSide.PE, entry, structure, atr, cfg)
    assert stop == pytest.approx(114.0)
    assert target == pytest.approx(72.0)


def test_v38_stops_or_range_delegates() -> None:
    cfg = StrategyConfig(sl_mode="OR_RANGE", sl_buffer_pts=10.0, rr_ratio=2.0, atr_target_mult=1.2)
    stop, target = v38_stops(PositionSide.CE, 100.0, 10.0, 95.0, 90.0, cfg)
    assert stop == pytest.approx(80.0)
    assert target == pytest.approx(124.0)
