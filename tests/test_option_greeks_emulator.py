"""Unit tests for in-memory option Greeks emulator in research_sweeps."""

from __future__ import annotations

from datetime import datetime

import pytest
from zoneinfo import ZoneInfo

from bot.config import DEFAULT_CONFIG
from bot.logger import SignalEvent
from scripts.research_sweeps import (
    EMULATOR_BASE_PREMIUM,
    PREMIUM_STOP_LOSSES,
    PREMIUM_TARGET_CAPS,
    THETA_CLOCK_DECAY_PER_BAR,
    THETA_CLOCK_INITIAL_TARGET_CAP,
    THETA_CLOCK_TARGET_FLOOR,
    TRAIL_ACTIVATION_MULT,
    TRAIL_CUSHION_PCT,
    current_theta_target_cap,
    effective_trailing_stop,
    evaluate_option_bar,
    evaluate_option_bar_thetaclock,
    evaluate_option_bar_trailing,
    init_option_emulator,
    run_premium_bracket_sweep_matrix,
    simulate_bar_premium,
    simulate_option_trade,
    simulate_option_trade_thetaclock,
    simulate_option_trade_trailing,
    sort_premium_bracket_cells,
    theta_clock_target_premium,
    vega_adaptive_target_cap,
    vega_regime_label,
    compute_vrr,
    resolve_vega_entry_target,
)

TZ = ZoneInfo("Asia/Kolkata")


def _entry(ts: datetime, side: str, bar: int, price: float = 100.0) -> SignalEvent:
    return SignalEvent(
        timestamp=ts,
        event_type=f"BUY_{side}",
        direction="LONG" if side == "CE" else "SHORT",
        side=side,
        price=price,
        stop=price - 10 if side == "CE" else price + 10,
        target=price + 20 if side == "CE" else price - 20,
        bar_index=bar,
    )


def _bar(index: int, close: float) -> dict:
    return {
        "index": index,
        "timestamp": datetime(2026, 7, 7, 10, 0, tzinfo=TZ),
        "open": close,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "atr": 10.0,
    }


def _bar_ohlc(index: int, o: float, h: float, l: float, c: float) -> dict:
    return {
        "index": index,
        "timestamp": datetime(2026, 7, 7, 10, 0, tzinfo=TZ),
        "open": o,
        "high": h,
        "low": l,
        "close": c,
        "atr": 10.0,
    }


def test_init_option_emulator_itm2_on_expiry_eve():
    # Monday 2026-07-06 → Tuesday expiry 2026-07-07, DTE=1 → ITM2
    entry = _entry(datetime(2026, 7, 6, 10, 0, tzinfo=TZ), "CE", 5, 24400.0)
    state = init_option_emulator(entry, cfg=DEFAULT_CONFIG)
    assert state.dte == 1
    assert state.strike_mode == "ITM2"
    assert state.delta == 0.82
    assert state.premium_target == 140.0
    assert state.premium_stop == 85.0
    assert state.velocity_floor == 110.0


def test_simulate_bar_premium_ce_favorable_move():
    entry = _entry(datetime(2026, 7, 9, 11, 0, tzinfo=TZ), "CE", 10, 100.0)
    state = init_option_emulator(entry, cfg=DEFAULT_CONFIG)
    # +20 spot pts × 0.55 delta = +11 premium
    prem = simulate_bar_premium(state, 120.0, bars_in_trade=2)
    theta = (state.theta / 375) * 5 * 2
    expected = round(EMULATOR_BASE_PREMIUM + state.delta * 20 - theta, 2)
    assert prem == expected
    assert prem > EMULATOR_BASE_PREMIUM


def test_evaluate_option_bar_premium_target():
    entry = _entry(datetime(2026, 7, 9, 11, 0, tzinfo=TZ), "CE", 10, 100.0)
    state = init_option_emulator(entry, cfg=DEFAULT_CONFIG)
    # Large favorable move → premium target before spot exit at bar 20
    hit = evaluate_option_bar(state, _bar(12, 180.0), spot_exit_bar_index=20)
    assert hit is not None
    assert hit.exit_reason == "PREMIUM_TARGET"
    assert hit.premium_target_before_spot is True
    assert hit.exit_premium >= state.premium_target


def test_evaluate_option_bar_chop_stop():
    entry = _entry(datetime(2026, 7, 9, 11, 0, tzinfo=TZ), "CE", 10, 100.0)
    state = init_option_emulator(entry, cfg=DEFAULT_CONFIG)
    # Flat spot, 4+ bars → premium decays below velocity floor
    hit = evaluate_option_bar(state, _bar(14, 100.0), spot_exit_bar_index=30)
    assert hit is not None
    assert hit.exit_reason == "CHOP_STOP"
    assert hit.exit_premium < state.velocity_floor


def test_simulate_option_trade_premium_stop_on_adverse_move():
    entry = _entry(datetime(2026, 7, 9, 11, 0, tzinfo=TZ), "CE", 10, 100.0)
    exit_ev = SignalEvent(
        timestamp=datetime(2026, 7, 9, 11, 30, tzinfo=TZ),
        event_type="SL_CE",
        direction="LONG_EXIT",
        side="CE",
        price=85.0,
        stop=90.0,
        target=120.0,
        bar_index=16,
    )
    session_bars = {
        "2026-07-09": [
            _bar(10, 100.0),
            _bar(11, 95.0),
            _bar(12, 85.0),
            _bar(13, 72.0),
            _bar(14, 70.0),
            _bar(15, 68.0),
            _bar(16, 65.0),
        ]
    }
    result, state = simulate_option_trade(entry, exit_ev, session_bars, cfg=DEFAULT_CONFIG)
    assert result.exit_reason == "PREMIUM_STOP"
    assert result.exit_premium <= state.premium_stop


def test_init_option_emulator_dynamic_brackets():
    entry = _entry(datetime(2026, 7, 9, 11, 0, tzinfo=TZ), "CE", 10, 100.0)
    state = init_option_emulator(entry, target_cap_pct=0.40, stop_loss_pct=0.12, cfg=DEFAULT_CONFIG)
    assert state.premium_target == 140.0
    assert state.premium_stop == 88.0


def test_premium_bracket_grid_dimensions():
    assert len(PREMIUM_TARGET_CAPS) == 4
    assert len(PREMIUM_STOP_LOSSES) == 3
    assert len(PREMIUM_TARGET_CAPS) * len(PREMIUM_STOP_LOSSES) == 12


def test_premium_bracket_sweep_sorted_by_profit_factor():
    entry = _entry(datetime(2026, 7, 9, 10, 0, tzinfo=TZ), "CE", 10, 100.0)
    exit_ev = SignalEvent(
        timestamp=datetime(2026, 7, 9, 10, 30, tzinfo=TZ),
        event_type="TARGET_CE",
        direction="LONG_EXIT",
        side="CE",
        price=120.0,
        stop=90.0,
        target=120.0,
        bar_index=14,
    )
    session_bars = {
        "2026-07-09": [_bar(10, 100.0), _bar(11, 105.0), _bar(12, 110.0), _bar(13, 115.0), _bar(14, 120.0)],
    }
    events = [entry, exit_ev]
    cells = run_premium_bracket_sweep_matrix(events, session_bars, session_count=1, cfg=DEFAULT_CONFIG)
    assert len(cells) == 12
    ranked = sort_premium_bracket_cells(cells)
    pfs = [c.profit_factor for c in ranked if c.profit_factor is not None]
    assert pfs == sorted(pfs, reverse=True)


def test_effective_trailing_stop_break_even_floor():
    entry = EMULATOR_BASE_PREMIUM
    static = entry * 0.85
    # Below +20% activation → static stop
    assert effective_trailing_stop(entry, 115.0, static) == static
    # At +20% activation → max(break-even, peak×0.85)
    at_activation = entry * TRAIL_ACTIVATION_MULT
    assert effective_trailing_stop(entry, at_activation, static) == round(
        at_activation * (1.0 - TRAIL_CUSHION_PCT), 2,
    )
    # Peak +25% → peak × (1 − cushion)
    peak = entry * 1.25
    assert effective_trailing_stop(entry, peak, static) == round(peak * (1.0 - TRAIL_CUSHION_PCT), 2)
    # max(entry, dynamic) never below break-even once activated
    assert effective_trailing_stop(entry, peak, static) >= entry


def test_trailing_stop_exit_on_spike_and_retrace():
    """Spike to +25% then retrace → TRAILING_STOP_EXIT at peak×0.85 boundary."""
    entry = _entry(datetime(2026, 7, 9, 11, 0, tzinfo=TZ), "CE", 10, 100.0)
    exit_ev = SignalEvent(
        timestamp=datetime(2026, 7, 9, 12, 0, tzinfo=TZ),
        event_type="TARGET_CE",
        direction="LONG_EXIT",
        side="CE",
        price=120.0,
        stop=90.0,
        target=140.0,
        bar_index=20,
    )
    # Bar 11 spikes (high≈146 → prem ~125); bar 12 retraces (low≈108 → prem ~106)
    session_bars = {
        "2026-07-09": [
            _bar_ohlc(10, 100, 101, 99, 100),
            _bar_ohlc(11, 140, 146, 138, 145),
            _bar_ohlc(12, 130, 132, 108, 115),
            _bar_ohlc(13, 115, 118, 112, 114),
        ]
    }
    result, state = simulate_option_trade_trailing(
        entry, exit_ev, session_bars, cfg=DEFAULT_CONFIG,
    )
    assert result.exit_reason == "TRAILING_STOP_EXIT"
    assert result.exit_premium >= state.base_premium  # locked above break-even
    assert result.bars_in_trade == 2
    # Exit at effective trailing boundary (~106 on ₹100 entry after +25% spike)
    assert 105.0 <= result.exit_premium <= 107.5


def test_trailing_bar_eval_updates_peak_from_high():
    entry = _entry(datetime(2026, 7, 9, 11, 0, tzinfo=TZ), "CE", 10, 100.0)
    state = init_option_emulator(entry, cfg=DEFAULT_CONFIG)
    peak = state.base_premium
    spike = _bar_ohlc(11, 140, 146, 138, 145)
    hit, peak = evaluate_option_bar_trailing(
        state, spike, peak_premium=peak, spot_exit_bar_index=20,
    )
    assert hit is None
    assert peak >= state.base_premium * TRAIL_ACTIVATION_MULT
    retrace = _bar_ohlc(12, 130, 132, 108, 115)
    hit, _ = evaluate_option_bar_trailing(
        state, retrace, peak_premium=peak, spot_exit_bar_index=20,
    )
    assert hit is not None
    assert hit.exit_reason == "TRAILING_STOP_EXIT"
    assert hit.exit_premium >= state.base_premium


def test_theta_clock_target_cap_decays_per_bar():
    assert current_theta_target_cap(0) == THETA_CLOCK_INITIAL_TARGET_CAP
    assert current_theta_target_cap(5) == round(
        THETA_CLOCK_INITIAL_TARGET_CAP - 5 * THETA_CLOCK_DECAY_PER_BAR, 10,
    )
    # Floor guard at +15%
    assert current_theta_target_cap(10) == THETA_CLOCK_TARGET_FLOOR
    assert theta_clock_target_premium(100.0, 0) == 140.0
    assert theta_clock_target_premium(100.0, 8) == 115.0


def test_thetaclock_exits_at_trimmed_target_before_static_cap():
    """After 6 bars, +22.5% trimmed target vs +40% static — moderate rally triggers early exit."""
    entry = _entry(datetime(2026, 7, 9, 11, 0, tzinfo=TZ), "CE", 10, 100.0)
    state = init_option_emulator(
        entry,
        target_cap_pct=THETA_CLOCK_INITIAL_TARGET_CAP,
        stop_loss_pct=0.15,
        cfg=DEFAULT_CONFIG,
    )
    bars_in = 6
    trimmed = theta_clock_target_premium(state.base_premium, bars_in)
    assert trimmed < state.premium_target  # trimmed below static +40%
    # Spot ~141 → premium ~122 on bar 16 (6 bars in trade)
    hit_static = evaluate_option_bar(state, _bar(16, 141.0), spot_exit_bar_index=25)
    hit_theta = evaluate_option_bar_thetaclock(state, _bar(16, 141.0), spot_exit_bar_index=25)
    assert hit_static is None or hit_static.exit_reason != "PREMIUM_TARGET"
    assert hit_theta is not None
    assert hit_theta.exit_reason == "PREMIUM_TARGET"
    assert hit_theta.exit_premium >= trimmed


def test_simulate_option_trade_thetaclock_full_path():
    entry = _entry(datetime(2026, 7, 9, 11, 0, tzinfo=TZ), "CE", 10, 100.0)
    exit_ev = SignalEvent(
        timestamp=datetime(2026, 7, 9, 12, 0, tzinfo=TZ),
        event_type="TARGET_CE",
        direction="LONG_EXIT",
        side="CE",
        price=160.0,
        stop=90.0,
        target=180.0,
        bar_index=20,
    )
    # Fast rally hits trimmed +26% target on bar 14 (4 bars in) before chop stop
    session_bars = {
        "2026-07-09": [
            _bar(10, 100.0),
            _bar(11, 115.0),
            _bar(12, 125.0),
            _bar(13, 138.0),
            _bar(14, 150.0),
            _bar(15, 155.0),
            _bar(20, 160.0),
        ]
    }
    result, _ = simulate_option_trade_thetaclock(entry, exit_ev, session_bars, cfg=DEFAULT_CONFIG)
    assert result.exit_reason == "PREMIUM_TARGET"
    assert result.premium_target_before_spot is True
    assert result.bars_in_trade <= 4


def test_vega_adaptive_target_cap_regimes():
    assert vega_adaptive_target_cap(0.70) == 0.50
    assert vega_adaptive_target_cap(0.85) == 0.40
    assert vega_adaptive_target_cap(1.00) == 0.40
    assert vega_adaptive_target_cap(1.25) == 0.40
    assert vega_adaptive_target_cap(1.30) == 0.20
    assert vega_regime_label(0.70) == "compressed"
    assert vega_regime_label(1.00) == "normal"
    assert vega_regime_label(1.30) == "inflated"


def test_compute_vrr_uses_baseline_when_no_rolling_atr():
    from scripts.research_sweeps import NIFTY_BASELINE_ATR_PROXY

    assert compute_vrr(50.0, None) == pytest.approx(50.0 / NIFTY_BASELINE_ATR_PROXY)
    assert compute_vrr(100.0, 200.0) == pytest.approx(0.5)


def test_resolve_vega_entry_target_compressed_regime():
    entry = _entry(datetime(2026, 7, 9, 11, 0, tzinfo=TZ), "CE", 10, 100.0)
    entry.extra = {"or_high": 110.0, "or_low": 100.0}  # width=10
    vrr, cap, regime = resolve_vega_entry_target(
        entry,
        session_or_widths={},
        rolling_atr_map={"2026-07-09": 200.0},  # vrr=0.05 < 0.85
    )
    assert vrr == pytest.approx(0.05)
    assert cap == 0.50
    assert regime == "compressed"


def test_init_option_emulator_vega_compressed_target():
    entry = _entry(datetime(2026, 7, 9, 11, 0, tzinfo=TZ), "CE", 10, 100.0)
    state = init_option_emulator(entry, target_cap_pct=0.50, stop_loss_pct=0.15, cfg=DEFAULT_CONFIG)
    assert state.premium_target == 150.0
    assert state.premium_stop == 85.0
