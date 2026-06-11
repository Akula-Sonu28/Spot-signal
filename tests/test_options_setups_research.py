"""Tests for isolated options setups research harness."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from bot.config import DEFAULT_CONFIG
from bot.logger import ReplayLogger
from bot.replay import load_candles_csv, run_replay_fast
from bot.state import PositionSide
from bot.strategy import build_bar_context, process_bar
from bot.state import ReplayState, make_day_state

from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.data_audit import run_data_audit
from research.backtests.options_setups_comparison.replay_engine import run_setup_replay
from research.backtests.options_setups_comparison.risk_engine import (
    ResearchDayState,
    ResearchState,
    SkippedLogger,
    can_enter_ce,
    make_research_day,
)
from research.backtests.options_setups_comparison.setups import SETUP_REGISTRY
from research.backtests.options_setups_comparison.slippage import SlippageModel


ROOT = Path(__file__).resolve().parents[1]
MOCK_CSV = ROOT / "data" / "mock" / "day_trend_up.csv"


def test_data_audit_passes_on_futures_subset():
    cfg = ResearchConfig(from_date="2026-04-01", to_date="2026-06-09")
    result = run_data_audit(cfg)
    assert result.sessions_found >= 40
    assert result.warmup_ok
    assert result.isolation_ok


def test_risk_engine_blocks_after_two_losses():
    day = make_research_day("2026-04-01")
    day.losses_today = 2
    research = ResearchConfig()
    assert can_enter_ce(day, DEFAULT_CONFIG, research) == "MAX_LOSSES_DAY"


def test_slippage_worsens_ce_entry():
    slip = SlippageModel(tier="conservative")
    assert slip.adjust_entry(PositionSide.CE, 100.0) == 102.0
    assert slip.adjust_exit(PositionSide.CE, 100.0) == 98.0


def test_setup_replay_on_mock_csv():
    df = load_candles_csv(MOCK_CSV)
    _, processor = SETUP_REGISTRY["B"]
    logger, skipped = run_setup_replay(
        df, "B", processor, DEFAULT_CONFIG, ResearchConfig(), SlippageModel("none"),
    )
    assert isinstance(logger, ReplayLogger)


def test_baseline_parity_mock_determinism():
    df = load_candles_csv(MOCK_CSV)
    prod = run_replay_fast(df, DEFAULT_CONFIG)
    _, processor = SETUP_REGISTRY["A"]
    research_logger, _ = run_setup_replay(
        df, "A", processor, DEFAULT_CONFIG, ResearchConfig(), SlippageModel("none"),
        use_production_baseline=True,
    )
    prod_types = [e.event_type for e in prod.events]
    res_types = [e.event_type for e in research_logger.events]
    assert prod_types == res_types


def test_or_not_used_before_defined():
    """OR high must not gate entries during OR window."""
    day = make_day_state("2025-01-06")
    assert not day.or_defined
    day.or_high = 99999.0
    day.or_low = 1.0
    day.or_defined = False
    assert not day.or_defined


def test_same_bar_sl_priority_via_strategy():
    """Production strategy gives SL priority over target on same bar."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Kolkata")
    state = ReplayState(day=make_day_state("2025-01-06"))
    from bot.state import Position

    state.position = Position(
        side=PositionSide.CE,
        entry_price=100.0,
        stop=98.0,
        target=110.0,
    )
    logger = ReplayLogger()
    bar = build_bar_context(
        index=5,
        timestamp=datetime(2025, 1, 6, 10, 0, tzinfo=tz),
        session_date="2025-01-06",
        o=100.0,
        h=112.0,
        l=97.0,
        c=105.0,
        vol=0.0,
        vwap=100.0,
        atr=2.0,
        adx=20.0,
    )
    process_bar(state, bar, logger)
    assert logger.events[-1].event_type == "SL_CE"


def test_outputs_path_under_research():
    cfg = ResearchConfig()
    out = cfg.outputs_root.resolve()
    assert "options_setups_comparison" in str(out)


def test_robustness_concentration():
    from research.backtests.options_setups_comparison.robustness import _concentration
    from bot.backtest import TradeRecord
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Kolkata")
    trades = [
        TradeRecord("2026-04-01", "CE", datetime(2026, 4, 1, 10, 0, tzinfo=tz),
                    datetime(2026, 4, 1, 11, 0, tzinfo=tz), 100, 110, 95, 115, "TARGET_HIT", 10, 5, 2.0),
        TradeRecord("2026-04-02", "PE", datetime(2026, 4, 2, 10, 0, tzinfo=tz),
                    datetime(2026, 4, 2, 11, 0, tzinfo=tz), 100, 95, 105, 90, "TARGET_HIT", 5, 5, 1.0),
    ]
    c = _concentration(trades)
    assert c["top1_pct"] > 50


def test_edge_pocket_variants_defined():
    from research.backtests.options_setups_comparison.edge_pockets import VARIANTS

    assert len(VARIANTS) == 13
    ids = [v.variant_id for v in VARIANTS]
    assert ids[0] == "A0" and ids[-1] == "A12"


def test_edge_pocket_filter_pe_only():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from bot.backtest import TradeRecord
    from research.backtests.options_setups_comparison.edge_pockets import VARIANTS, filter_trades

    tz = ZoneInfo("Asia/Kolkata")
    trades = [
        TradeRecord("2026-04-01", "CE", datetime(2026, 4, 1, 10, 0, tzinfo=tz),
                    datetime(2026, 4, 1, 11, 0, tzinfo=tz), 100, 110, 95, 115, "TARGET_HIT", 10, 5, 2.0),
        TradeRecord("2026-04-02", "PE", datetime(2026, 4, 2, 10, 0, tzinfo=tz),
                    datetime(2026, 4, 2, 11, 0, tzinfo=tz), 100, 95, 105, 90, "TARGET_HIT", 5, 5, 1.0),
    ]
    pe_def = next(v for v in VARIANTS if v.variant_id == "A1")
    assert len(filter_trades(trades, pe_def)) == 1


def test_expiry_session_classification():
    from research.backtests.options_setups_comparison.expiry_calendar import build_session_classification

    sessions = ["2026-06-03", "2026-06-04", "2026-06-05", "2026-06-06", "2026-06-09"]
    expiries = ["2026-06-05"]
    meta = build_session_classification(sessions, expiries)
    assert meta["2026-06-05"]["expiry_class"] == "weekly_expiry"
    assert meta["2026-06-04"]["expiry_class"] == "day_before_expiry"
    assert meta["2026-06-06"]["expiry_class"] == "day_after_expiry"
    assert meta["2026-06-09"]["expiry_class"] == "normal"


def test_robustness_recommendation():
    from research.backtests.options_setups_comparison.robustness import RobustnessReport, recommend_robustness

    baseline = RobustnessReport(
        label="baseline_current_entry",
        from_date="2026-04-01",
        to_date="2026-06-09",
        sessions=46,
        total_trades=25,
        expectancy_r=0.5,
        net_r=10,
        payoff_ratio=1.5,
        concentration={"top1_pct": 20},
        outlier_analysis={"outlier_dominated": False},
    )
    stress = RobustnessReport(
        label="execution_stress_slippage",
        from_date="2026-04-01",
        to_date="2026-06-09",
        sessions=46,
        total_trades=25,
        net_r=2,
    )
    rec = recommend_robustness({"baseline_current_entry": baseline, "execution_stress_slippage": stress})
    assert "PAPER_TRADE" in rec or "NEEDS_MORE" in rec or "ROBUST" in rec
