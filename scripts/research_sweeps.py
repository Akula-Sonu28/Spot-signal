#!/usr/bin/env python3
"""Memory-optimized parametric sweep — legacy time/J+ matrix + advanced option-defense grid.

One baseline replay; counterfactual cells via in-memory event post-filtering.
Does not modify locked strategy modules (see .cursor/rules/research-sweep-guardrails.mdc).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from bot.backtest import ENTRY_TYPES, EXIT_TYPES, BacktestSummary, TradeRecord, pair_trades, summarize_trades
from bot.config import DEFAULT_CONFIG, CombinedStrategyConfig
from bot.logger import ReplayLogger, SignalEvent
from bot.option_truth import (
    CHOP_STOP_BAR_THRESHOLD,
    PREMIUM_STOP_MULT,
    PREMIUM_TARGET_MULT,
    VELOCITY_MIN_MULT,
    audit_target_wick_close,
)
from bot.indicators import or_width
from bot.replay import load_candles_csv, run_replay_fast
from bot.strategy_audit import bar_close_minutes, build_session_or_widths, index_session_bars, max_consecutive_bars_outside_or
from bot.strategy_j import J_TRAP_ROBUST

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "historical"
OUT = ROOT / "data" / "research"

# Legacy 4×4 matrix (afternoon gate × J+ bars-outside-OR)
TIME_GATES: tuple[str | None, ...] = (None, "14:30", "14:00", "13:30")
MAX_BARS_OUTSIDE: tuple[int | None, ...] = (None, 3, 2, 1)

# Advanced option-defense grid
MAX_SPREAD_DRAG_PCT: tuple[float | None, ...] = (None, 2.0, 3.0, 4.0)
TARGET_BUFFER_MULT_ATR: tuple[float | None, ...] = (None, 0.1, 0.2)
EXPIRY_ENTRY_CUTOFF_TIME: tuple[str | None, ...] = (None, "12:00", "13:00")

# In-memory Greeks option emulator (Phase 2 research — no live quotes)
EMULATOR_BASE_PREMIUM = 100.0
TRADING_MINUTES_PER_SESSION = 375
BAR_MINUTES = 5
MORNING_IV_CRUSH_TOTAL_PTS = 2.5
MORNING_IV_CRUSH_BARS = 3
MORNING_ENTRY_CLOSE_MIN = 9 * 60 + 15   # 09:15 IST bar close
MORNING_ENTRY_CLOSE_MAX = 9 * 60 + 45   # 09:45 IST bar close

GREEKS_ATM = {"delta": 0.55, "theta": 18.0, "iv": 0.11}
GREEKS_ITM2 = {"delta": 0.82, "theta": 12.0, "iv": 0.105}

# Premium bracket parametric sweep grid (target cap × stop-loss cushion)
PREMIUM_TARGET_CAPS: tuple[float, ...] = (0.25, 0.30, 0.35, 0.40)
PREMIUM_STOP_LOSSES: tuple[float, ...] = (0.12, 0.15, 0.18)
DEFAULT_TARGET_CAP_PCT = PREMIUM_TARGET_MULT - 1.0   # 0.40
DEFAULT_STOP_LOSS_PCT = 1.0 - PREMIUM_STOP_MULT      # 0.15

# Alpha-locking trailing stop (DEACTIVATED — PF 0.84 whipsaw; code retained for tests)
TRAIL_ACTIVATION_MULT = 1.20   # +20% peak → break-even floor armed
TRAIL_CUSHION_PCT = 0.15       # 15% below peak once activated

# Theta Clock target trimming (research-only — dynamic target decay vs holding time)
THETA_CLOCK_INITIAL_TARGET_CAP = 0.40   # +40% at entry
THETA_CLOCK_STOP_LOSS_PCT = 0.15        # −15% static floor
THETA_CLOCK_DECAY_PER_BAR = 0.035       # 3.5% cap reduction per 5m bar
THETA_CLOCK_TARGET_FLOOR = 0.15         # hard minimum +15% target cap

# Vega Adaptive target scaling (research-only — VRR regime switching)
NIFTY_BASELINE_ATR_PROXY = 250.0        # ponytail: fallback when rolling 20d ATR unavailable
VEGA_ROLLING_ATR_WINDOW = 20
VEGA_VRR_COMPRESSED_MAX = 0.85
VEGA_VRR_INFLATED_MIN = 1.25
VEGA_TARGET_COMPRESSED = 0.50           # +50% on cheap vol (VRR < 0.85)
VEGA_TARGET_NORMAL = 0.40               # +40% production benchmark
VEGA_TARGET_INFLATED = 0.20             # +20% defensive scalp (VRR > 1.25)
VEGA_STOP_LOSS_PCT = 0.15               # −15% hard-locked across regimes


@dataclass(frozen=True)
class SweepCell:
    time_gate: str | None
    max_bars_outside_or: int | None
    total_trades: int
    win_rate_pct: float
    net_pnl_pts: float
    profit_factor: float | None
    max_drawdown_pts: float
    max_spread_drag_pct: float | None = None
    target_buffer_mult_atr: float | None = None
    expiry_entry_cutoff: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "time_gate": self.time_gate if self.time_gate else "Baseline",
            "max_bars_outside_or": self.max_bars_outside_or if self.max_bars_outside_or is not None else "Unrestricted",
            "max_spread_drag_pct": self.max_spread_drag_pct if self.max_spread_drag_pct is not None else "Unrestricted",
            "target_buffer_mult_atr": self.target_buffer_mult_atr if self.target_buffer_mult_atr is not None else "None",
            "expiry_entry_cutoff": self.expiry_entry_cutoff if self.expiry_entry_cutoff else "Unrestricted",
            "total_trades": self.total_trades,
            "win_rate_pct": round(self.win_rate_pct, 1),
            "net_pnl_pts": round(self.net_pnl_pts, 2),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor is not None else None,
            "max_drawdown_pts": round(self.max_drawdown_pts, 2),
        }


@dataclass(frozen=True)
class PremiumBracketCell:
    target_cap_pct: float
    stop_loss_pct: float
    total_trades: int
    win_rate_pct: float
    net_premium_pts: float
    profit_factor: float | None
    max_drawdown_pts: float
    chop_stop_exits: int
    premium_target_early: int
    premium_stop_exits: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_cap_pct": self.target_cap_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "target_label": f"+{self.target_cap_pct * 100:.0f}%",
            "stop_label": f"-{self.stop_loss_pct * 100:.0f}%",
            "total_trades": self.total_trades,
            "win_rate_pct": round(self.win_rate_pct, 1),
            "net_premium_pts": round(self.net_premium_pts, 2),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor is not None else None,
            "max_drawdown_pts": round(self.max_drawdown_pts, 2),
            "chop_stop_exits": self.chop_stop_exits,
            "premium_target_early": self.premium_target_early,
            "premium_stop_exits": self.premium_stop_exits,
        }


@dataclass(frozen=True)
class OptionEmulatorState:
    """Mock option contract state at spot entry (research-only)."""

    session_date: str
    side: str
    entry_spot: float
    entry_bar_index: int
    entry_time: datetime
    dte: int
    strike_mode: str
    base_premium: float
    premium_target: float
    premium_stop: float
    velocity_floor: float
    delta: float
    theta: float
    base_iv: float
    morning_iv_crush: bool


@dataclass(frozen=True)
class OptionEmulatedExit:
    exit_premium: float
    exit_reason: str
    bars_in_trade: int
    premium_target_before_spot: bool


@dataclass(frozen=True)
class OptionEmulationMetrics:
    total_trades: int
    win_rate_pct: float
    net_pnl_pts: float
    profit_factor: float | None
    max_drawdown_pts: float
    chop_stop_exits: int
    premium_target_before_spot: int
    premium_stop_exits: int
    spot_exit_fallback: int
    trailing_stop_exits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "win_rate_pct": round(self.win_rate_pct, 1),
            "net_pnl_pts": round(self.net_pnl_pts, 2),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor is not None else None,
            "max_drawdown_pts": round(self.max_drawdown_pts, 2),
            "chop_stop_exits": self.chop_stop_exits,
            "premium_target_before_spot": self.premium_target_before_spot,
            "premium_stop_exits": self.premium_stop_exits,
            "spot_exit_fallback": self.spot_exit_fallback,
            "trailing_stop_exits": self.trailing_stop_exits,
        }


@dataclass(frozen=True)
class TrailingComparisonRow:
    """Three-way strategy comparison for trailing research mode."""

    name: str
    total_trades: int
    win_rate_pct: float
    net_pnl_pts: float
    profit_factor: float | None
    max_drawdown_pts: float
    chop_stop_exits: int = 0
    premium_target_exits: int = 0
    premium_stop_exits: int = 0
    trailing_stop_exits: int = 0
    spot_exit_fallback: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total_trades": self.total_trades,
            "win_rate_pct": round(self.win_rate_pct, 1),
            "net_pnl_pts": round(self.net_pnl_pts, 2),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor is not None else None,
            "max_drawdown_pts": round(self.max_drawdown_pts, 2),
            "chop_stop_exits": self.chop_stop_exits,
            "premium_target_exits": self.premium_target_exits,
            "premium_stop_exits": self.premium_stop_exits,
            "trailing_stop_exits": self.trailing_stop_exits,
            "spot_exit_fallback": self.spot_exit_fallback,
        }


def _nearest_tuesday_expiry(session_date: str) -> str:
    """Next Tuesday on or after session_date (NIFTY weekly expiry heuristic)."""
    d = date.fromisoformat(session_date)
    days_ahead = (1 - d.weekday()) % 7
    return (d + timedelta(days=days_ahead)).isoformat()


def _dte(session_date: str) -> int:
    return (date.fromisoformat(_nearest_tuesday_expiry(session_date)) - date.fromisoformat(session_date)).days


def _strike_mode_for_dte(dte: int) -> str:
    return "ITM2" if dte <= 1 else "ATM"


def _greeks_for_mode(strike_mode: str) -> dict[str, float]:
    return GREEKS_ITM2 if strike_mode == "ITM2" else GREEKS_ATM


def _is_morning_or_entry(entry_ts: datetime, cfg: Any = DEFAULT_CONFIG) -> bool:
    close_m = bar_close_minutes(entry_ts, cfg)
    return MORNING_ENTRY_CLOSE_MIN <= close_m <= MORNING_ENTRY_CLOSE_MAX


def init_option_emulator(
    entry: SignalEvent,
    *,
    target_cap_pct: float = DEFAULT_TARGET_CAP_PCT,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    cfg: Any = DEFAULT_CONFIG,
) -> OptionEmulatorState:
    """Initialize mock option contract at spot entry."""
    session_date = entry.timestamp.strftime("%Y-%m-%d")
    dte = _dte(session_date)
    strike_mode = _strike_mode_for_dte(dte)
    greeks = _greeks_for_mode(strike_mode)
    base = EMULATOR_BASE_PREMIUM
    return OptionEmulatorState(
        session_date=session_date,
        side=entry.side,
        entry_spot=float(entry.price),
        entry_bar_index=int(entry.bar_index),
        entry_time=entry.timestamp,
        dte=dte,
        strike_mode=strike_mode,
        base_premium=base,
        premium_target=round(base * (1.0 + target_cap_pct), 2),
        premium_stop=round(base * (1.0 - stop_loss_pct), 2),
        velocity_floor=round(base * VELOCITY_MIN_MULT, 2),
        delta=greeks["delta"],
        theta=greeks["theta"],
        base_iv=greeks["iv"],
        morning_iv_crush=_is_morning_or_entry(entry.timestamp, cfg),
    )


def _signed_spot_move(side: str, entry_spot: float, bar_close: float) -> float:
    if side == "CE":
        return bar_close - entry_spot
    return entry_spot - bar_close


def _iv_crush_penalty(state: OptionEmulatorState, bars_in_trade: int) -> float:
    if not state.morning_iv_crush:
        return 0.0
    bars_with_crush = min(bars_in_trade + 1, MORNING_IV_CRUSH_BARS)
    return round(MORNING_IV_CRUSH_TOTAL_PTS * bars_with_crush / MORNING_IV_CRUSH_BARS, 4)


def simulate_bar_premium(
    state: OptionEmulatorState,
    bar_close: float,
    *,
    bars_in_trade: int,
) -> float:
    """Premium at bar close: base + δ·ΔS − θ·(5m/session) − morning IV crush."""
    return _simulate_premium_at_spot(state, bar_close, bars_in_trade=bars_in_trade)


def _simulate_premium_at_spot(
    state: OptionEmulatorState,
    spot: float,
    *,
    bars_in_trade: int,
) -> float:
    delta_pts = state.delta * _signed_spot_move(state.side, state.entry_spot, spot)
    theta_pts = (state.theta / TRADING_MINUTES_PER_SESSION) * BAR_MINUTES * bars_in_trade
    iv_penalty = _iv_crush_penalty(state, bars_in_trade)
    return round(state.base_premium + delta_pts - theta_pts - iv_penalty, 2)


def _bar_spot_extreme(side: str, bar: dict[str, Any], extreme: str) -> float:
    """Spot price at bar high/low adjusted for CE vs PE favorable direction."""
    if side == "CE":
        return float(bar["high"] if extreme == "high" else bar["low"])
    return float(bar["low"] if extreme == "high" else bar["high"])


def effective_trailing_stop(
    entry_ask: float,
    peak_premium: float,
    static_stop: float,
) -> float:
    """
    Two-stage alpha lock: static −15% until peak +20%, then max(break-even, peak×0.85).
    """
    activation = entry_ask * TRAIL_ACTIVATION_MULT
    if peak_premium < activation:
        return static_stop
    dynamic = round(peak_premium * (1.0 - TRAIL_CUSHION_PCT), 2)
    return max(entry_ask, dynamic)


def current_theta_target_cap(bars_in_trade: int) -> float:
    """Trimmed target cap for bar N: max(+15% floor, 40% − 3.5%×bars)."""
    return max(
        THETA_CLOCK_TARGET_FLOOR,
        THETA_CLOCK_INITIAL_TARGET_CAP - bars_in_trade * THETA_CLOCK_DECAY_PER_BAR,
    )


def theta_clock_target_premium(entry_ask: float, bars_in_trade: int) -> float:
    cap = current_theta_target_cap(bars_in_trade)
    return round(entry_ask * (1.0 + cap), 2)


def build_session_mean_atr(df: pd.DataFrame, cfg: Any = DEFAULT_CONFIG) -> dict[str, float]:
    """Mean bar ATR per session scaled to daily vol proxy (comparable to OR width)."""
    work = df
    if "atr" not in work.columns or work["atr"].isna().all():
        from bot.indicators import atr_series

        work = df.copy()
        work["atr"] = atr_series(
            work["high"].to_numpy(),
            work["low"].to_numpy(),
            work["close"].to_numpy(),
            cfg.atr_length,
        )
    bars_per_session = TRADING_MINUTES_PER_SESSION / BAR_MINUTES
    scale = bars_per_session**0.5  # ponytail: 5m ATR → session-scale vol for VRR vs OR width
    out: dict[str, float] = {}
    for sd, grp in work.groupby("session_date"):
        vals = grp["atr"].dropna()
        if len(vals):
            out[str(sd)] = float(vals.mean()) * scale
    return out


def build_rolling_atr_map(
    sessions: list[str],
    session_atr: dict[str, float],
    *,
    window: int = VEGA_ROLLING_ATR_WINDOW,
) -> dict[str, float]:
    """Rolling mean ATR over prior sessions (excludes current day)."""
    rolling: dict[str, float] = {}
    history: list[float] = []
    for sd in sessions:
        if len(history) >= window:
            rolling[sd] = sum(history[-window:]) / window
        elif history:
            rolling[sd] = sum(history) / len(history)
        if sd in session_atr:
            history.append(session_atr[sd])
    return rolling


def entry_or_width_pts(
    entry: SignalEvent,
    session_or_widths: dict[str, float],
) -> float | None:
    extra = entry.extra or {}
    w = or_width(extra.get("or_high"), extra.get("or_low"))
    if w is not None:
        return w
    return session_or_widths.get(entry.timestamp.strftime("%Y-%m-%d"))


def compute_vrr(
    or_width_pts: float,
    rolling_atr: float | None,
) -> float:
    denom = rolling_atr if rolling_atr and rolling_atr > 0 else NIFTY_BASELINE_ATR_PROXY
    return or_width_pts / denom


def vega_adaptive_target_cap(vrr: float) -> float:
    if vrr < VEGA_VRR_COMPRESSED_MAX:
        return VEGA_TARGET_COMPRESSED
    if vrr > VEGA_VRR_INFLATED_MIN:
        return VEGA_TARGET_INFLATED
    return VEGA_TARGET_NORMAL


def vega_regime_label(vrr: float) -> str:
    if vrr < VEGA_VRR_COMPRESSED_MAX:
        return "compressed"
    if vrr > VEGA_VRR_INFLATED_MIN:
        return "inflated"
    return "normal"


def resolve_vega_entry_target(
    entry: SignalEvent,
    *,
    session_or_widths: dict[str, float],
    rolling_atr_map: dict[str, float],
) -> tuple[float, float, str]:
    """Return (vrr, target_cap_pct, regime) for one entry."""
    sd = entry.timestamp.strftime("%Y-%m-%d")
    width = entry_or_width_pts(entry, session_or_widths)
    if width is None:
        width = 50.0  # ponytail: neutral OR proxy when telemetry missing
    vrr = compute_vrr(width, rolling_atr_map.get(sd))
    return vrr, vega_adaptive_target_cap(vrr), vega_regime_label(vrr)


def evaluate_option_bar(
    state: OptionEmulatorState,
    bar: dict[str, Any],
    *,
    spot_exit_bar_index: int,
) -> OptionEmulatedExit | None:
    """
    Return early option exit if premium stop/target/chop fires before spot exit bar.
    None → continue holding until next bar or spot exit.
    """
    bars_in_trade = int(bar["index"]) - state.entry_bar_index
    if bars_in_trade < 0:
        return None
    premium = simulate_bar_premium(state, float(bar["close"]), bars_in_trade=bars_in_trade)

    if premium <= state.premium_stop:
        return OptionEmulatedExit(
            exit_premium=premium,
            exit_reason="PREMIUM_STOP",
            bars_in_trade=bars_in_trade,
            premium_target_before_spot=False,
        )
    if premium >= state.premium_target and int(bar["index"]) < spot_exit_bar_index:
        return OptionEmulatedExit(
            exit_premium=premium,
            exit_reason="PREMIUM_TARGET",
            bars_in_trade=bars_in_trade,
            premium_target_before_spot=True,
        )
    if (
        bars_in_trade >= CHOP_STOP_BAR_THRESHOLD
        and premium < state.velocity_floor
        and int(bar["index"]) < spot_exit_bar_index
    ):
        return OptionEmulatedExit(
            exit_premium=premium,
            exit_reason="CHOP_STOP",
            bars_in_trade=bars_in_trade,
            premium_target_before_spot=False,
        )
    return None


def simulate_option_trade(
    entry: SignalEvent,
    exit_event: SignalEvent,
    session_bars: dict[str, list[dict[str, Any]]],
    *,
    target_cap_pct: float = DEFAULT_TARGET_CAP_PCT,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    cfg: Any = DEFAULT_CONFIG,
) -> tuple[OptionEmulatedExit, OptionEmulatorState]:
    """Bar-by-bar premium path for one paired spot trade."""
    state = init_option_emulator(
        entry,
        target_cap_pct=target_cap_pct,
        stop_loss_pct=stop_loss_pct,
        cfg=cfg,
    )
    sd = state.session_date
    bars = session_bars.get(sd, [])
    trade_bars = [
        b for b in bars
        if state.entry_bar_index <= int(b["index"]) <= int(exit_event.bar_index)
    ]
    trade_bars.sort(key=lambda b: int(b["index"]))

    spot_exit_bar = int(exit_event.bar_index)
    early: OptionEmulatedExit | None = None

    for bar in trade_bars:
        if int(bar["index"]) == state.entry_bar_index:
            continue
        hit = evaluate_option_bar(state, bar, spot_exit_bar_index=spot_exit_bar)
        if hit is not None:
            early = hit
            break

    if early is not None:
        return early, state

    exit_bar = next((b for b in trade_bars if int(b["index"]) == spot_exit_bar), trade_bars[-1] if trade_bars else None)
    if exit_bar is None:
        final_premium = state.base_premium
        bars_in_trade = 0
    else:
        bars_in_trade = int(exit_bar["index"]) - state.entry_bar_index
        final_premium = simulate_bar_premium(state, float(exit_bar["close"]), bars_in_trade=bars_in_trade)

    return (
        OptionEmulatedExit(
            exit_premium=final_premium,
            exit_reason=exit_event.event_type,
            bars_in_trade=bars_in_trade,
            premium_target_before_spot=False,
        ),
        state,
    )


def evaluate_option_bar_thetaclock(
    state: OptionEmulatorState,
    bar: dict[str, Any],
    *,
    spot_exit_bar_index: int,
) -> OptionEmulatedExit | None:
    """Bar eval with time-decaying premium target (Theta Clock trimming)."""
    bars_in_trade = int(bar["index"]) - state.entry_bar_index
    if bars_in_trade < 0:
        return None
    premium = simulate_bar_premium(state, float(bar["close"]), bars_in_trade=bars_in_trade)
    trimmed_target = theta_clock_target_premium(state.base_premium, bars_in_trade)

    if premium <= state.premium_stop:
        return OptionEmulatedExit(
            exit_premium=premium,
            exit_reason="PREMIUM_STOP",
            bars_in_trade=bars_in_trade,
            premium_target_before_spot=False,
        )
    if premium >= trimmed_target and int(bar["index"]) < spot_exit_bar_index:
        return OptionEmulatedExit(
            exit_premium=premium,
            exit_reason="PREMIUM_TARGET",
            bars_in_trade=bars_in_trade,
            premium_target_before_spot=True,
        )
    if (
        bars_in_trade >= CHOP_STOP_BAR_THRESHOLD
        and premium < state.velocity_floor
        and int(bar["index"]) < spot_exit_bar_index
    ):
        return OptionEmulatedExit(
            exit_premium=premium,
            exit_reason="CHOP_STOP",
            bars_in_trade=bars_in_trade,
            premium_target_before_spot=False,
        )
    return None


def simulate_option_trade_thetaclock(
    entry: SignalEvent,
    exit_event: SignalEvent,
    session_bars: dict[str, list[dict[str, Any]]],
    *,
    cfg: Any = DEFAULT_CONFIG,
) -> tuple[OptionEmulatedExit, OptionEmulatorState]:
    """Bar-by-bar premium path with Theta Clock target trimming."""
    state = init_option_emulator(
        entry,
        target_cap_pct=THETA_CLOCK_INITIAL_TARGET_CAP,
        stop_loss_pct=THETA_CLOCK_STOP_LOSS_PCT,
        cfg=cfg,
    )
    sd = state.session_date
    bars = session_bars.get(sd, [])
    trade_bars = [
        b for b in bars
        if state.entry_bar_index <= int(b["index"]) <= int(exit_event.bar_index)
    ]
    trade_bars.sort(key=lambda b: int(b["index"]))

    spot_exit_bar = int(exit_event.bar_index)
    early: OptionEmulatedExit | None = None

    for bar in trade_bars:
        if int(bar["index"]) == state.entry_bar_index:
            continue
        hit = evaluate_option_bar_thetaclock(state, bar, spot_exit_bar_index=spot_exit_bar)
        if hit is not None:
            early = hit
            break

    if early is not None:
        return early, state

    exit_bar = next((b for b in trade_bars if int(b["index"]) == spot_exit_bar), trade_bars[-1] if trade_bars else None)
    if exit_bar is None:
        final_premium = state.base_premium
        bars_in_trade = 0
    else:
        bars_in_trade = int(exit_bar["index"]) - state.entry_bar_index
        final_premium = simulate_bar_premium(state, float(exit_bar["close"]), bars_in_trade=bars_in_trade)

    return (
        OptionEmulatedExit(
            exit_premium=final_premium,
            exit_reason=exit_event.event_type,
            bars_in_trade=bars_in_trade,
            premium_target_before_spot=False,
        ),
        state,
    )


def evaluate_option_bar_trailing(
    state: OptionEmulatorState,
    bar: dict[str, Any],
    *,
    peak_premium: float,
    spot_exit_bar_index: int,
) -> tuple[OptionEmulatedExit | None, float]:
    """
    Bar eval with high-water mark + two-stage trailing stop (research-only).
    Returns (exit_or_none, updated_peak_premium).
    """
    bars_in_trade = int(bar["index"]) - state.entry_bar_index
    if bars_in_trade < 0:
        return None, peak_premium

    entry_ask = state.base_premium
    prem_high = _simulate_premium_at_spot(
        state, _bar_spot_extreme(state.side, bar, "high"), bars_in_trade=bars_in_trade,
    )
    prem_low = _simulate_premium_at_spot(
        state, _bar_spot_extreme(state.side, bar, "low"), bars_in_trade=bars_in_trade,
    )
    prem_close = _simulate_premium_at_spot(
        state, float(bar["close"]), bars_in_trade=bars_in_trade,
    )
    peak_premium = max(peak_premium, prem_high)
    eff_stop = effective_trailing_stop(entry_ask, peak_premium, state.premium_stop)
    activation = entry_ask * TRAIL_ACTIVATION_MULT

    if peak_premium >= activation and prem_low <= eff_stop:
        return (
            OptionEmulatedExit(
                exit_premium=eff_stop,
                exit_reason="TRAILING_STOP_EXIT",
                bars_in_trade=bars_in_trade,
                premium_target_before_spot=False,
            ),
            peak_premium,
        )
    if peak_premium < activation and prem_low <= state.premium_stop:
        return (
            OptionEmulatedExit(
                exit_premium=state.premium_stop,
                exit_reason="PREMIUM_STOP",
                bars_in_trade=bars_in_trade,
                premium_target_before_spot=False,
            ),
            peak_premium,
        )
    if prem_close >= state.premium_target and int(bar["index"]) < spot_exit_bar_index:
        return (
            OptionEmulatedExit(
                exit_premium=prem_close,
                exit_reason="PREMIUM_TARGET",
                bars_in_trade=bars_in_trade,
                premium_target_before_spot=True,
            ),
            peak_premium,
        )
    if (
        bars_in_trade >= CHOP_STOP_BAR_THRESHOLD
        and prem_close < state.velocity_floor
        and int(bar["index"]) < spot_exit_bar_index
    ):
        return (
            OptionEmulatedExit(
                exit_premium=prem_close,
                exit_reason="CHOP_STOP",
                bars_in_trade=bars_in_trade,
                premium_target_before_spot=False,
            ),
            peak_premium,
        )
    return None, peak_premium


def simulate_option_trade_trailing(
    entry: SignalEvent,
    exit_event: SignalEvent,
    session_bars: dict[str, list[dict[str, Any]]],
    *,
    target_cap_pct: float = DEFAULT_TARGET_CAP_PCT,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    cfg: Any = DEFAULT_CONFIG,
) -> tuple[OptionEmulatedExit, OptionEmulatorState]:
    """Bar-by-bar premium path with alpha-locking trailing stop."""
    state = init_option_emulator(
        entry,
        target_cap_pct=target_cap_pct,
        stop_loss_pct=stop_loss_pct,
        cfg=cfg,
    )
    sd = state.session_date
    bars = session_bars.get(sd, [])
    trade_bars = [
        b for b in bars
        if state.entry_bar_index <= int(b["index"]) <= int(exit_event.bar_index)
    ]
    trade_bars.sort(key=lambda b: int(b["index"]))

    spot_exit_bar = int(exit_event.bar_index)
    peak_premium = state.base_premium
    early: OptionEmulatedExit | None = None

    for bar in trade_bars:
        if int(bar["index"]) == state.entry_bar_index:
            continue
        hit, peak_premium = evaluate_option_bar_trailing(
            state, bar, peak_premium=peak_premium, spot_exit_bar_index=spot_exit_bar,
        )
        if hit is not None:
            early = hit
            break

    if early is not None:
        return early, state

    exit_bar = next((b for b in trade_bars if int(b["index"]) == spot_exit_bar), trade_bars[-1] if trade_bars else None)
    if exit_bar is None:
        final_premium = state.base_premium
        bars_in_trade = 0
    else:
        bars_in_trade = int(exit_bar["index"]) - state.entry_bar_index
        final_premium = simulate_bar_premium(state, float(exit_bar["close"]), bars_in_trade=bars_in_trade)

    return (
        OptionEmulatedExit(
            exit_premium=final_premium,
            exit_reason=exit_event.event_type,
            bars_in_trade=bars_in_trade,
            premium_target_before_spot=False,
        ),
        state,
    )


def _pair_entry_exit_events(events: list[SignalEvent]) -> list[tuple[SignalEvent, SignalEvent]]:
    """Pair BUY events with the next exit (preserves bar_index on both legs)."""
    pairs: list[tuple[SignalEvent, SignalEvent]] = []
    open_entry: SignalEvent | None = None

    for event in sorted(events, key=lambda e: (e.timestamp, e.bar_index)):
        if event.event_type in ENTRY_TYPES:
            if open_entry is None:
                open_entry = event
            continue
        if event.event_type not in EXIT_TYPES or open_entry is None:
            continue
        side = open_entry.side
        if not event.event_type.endswith(f"_{side}") and event.event_type != "SQUARE_OFF":
            continue
        if event.event_type == "SQUARE_OFF" and event.side != side:
            continue
        pairs.append((open_entry, event))
        open_entry = None

    return pairs


def run_option_greeks_emulation(
    events: list[SignalEvent],
    session_bars: dict[str, list[dict[str, Any]]],
    *,
    session_count: int = 0,
    target_cap_pct: float = DEFAULT_TARGET_CAP_PCT,
    stop_loss_pct: float = DEFAULT_STOP_LOSS_PCT,
    use_trailing: bool = False,
    use_thetaclock: bool = False,
    cfg: Any = DEFAULT_CONFIG,
) -> tuple[OptionEmulationMetrics, list[TradeRecord]]:
    """Emulate Phase 2 premium rules on baseline spot trade pairs."""
    premium_trades: list[TradeRecord] = []
    chop_stop = premium_target_early = premium_stop = spot_fallback = trailing_stop = 0
    stop_price = round(EMULATOR_BASE_PREMIUM * (1.0 - stop_loss_pct), 2)
    target_price = round(EMULATOR_BASE_PREMIUM * (1.0 + target_cap_pct), 2)
    risk = EMULATOR_BASE_PREMIUM - stop_price
    if use_thetaclock:
        sim_fn = simulate_option_trade_thetaclock
    elif use_trailing:
        sim_fn = simulate_option_trade_trailing  # ponytail: deactivated in CLI — PF 0.84
    else:
        sim_fn = simulate_option_trade

    for entry_ev, exit_ev in _pair_entry_exit_events(events):
        if use_thetaclock:
            result, _state = sim_fn(entry_ev, exit_ev, session_bars, cfg=cfg)
        else:
            result, _state = sim_fn(
                entry_ev,
                exit_ev,
                session_bars,
                target_cap_pct=target_cap_pct,
                stop_loss_pct=stop_loss_pct,
                cfg=cfg,
            )
        prem_pnl = round(result.exit_premium - EMULATOR_BASE_PREMIUM, 2)

        if result.exit_reason == "CHOP_STOP":
            chop_stop += 1
        elif result.exit_reason == "PREMIUM_TARGET":
            premium_target_early += 1
        elif result.exit_reason == "PREMIUM_STOP":
            premium_stop += 1
        elif result.exit_reason == "TRAILING_STOP_EXIT":
            trailing_stop += 1
        else:
            spot_fallback += 1

        premium_trades.append(
            TradeRecord(
                session_date=entry_ev.timestamp.strftime("%Y-%m-%d"),
                side=entry_ev.side,
                entry_time=entry_ev.timestamp,
                exit_time=exit_ev.timestamp,
                entry_price=EMULATOR_BASE_PREMIUM,
                exit_price=result.exit_premium,
                stop=stop_price,
                target=target_price,
                exit_reason=result.exit_reason,
                pnl_pts=prem_pnl,
                risk_pts=risk,
                r_multiple=prem_pnl / risk if risk else 0.0,
            )
        )

    summary = summarize_trades(premium_trades, sessions=session_count)
    metrics = OptionEmulationMetrics(
        total_trades=summary.total_trades,
        win_rate_pct=summary.win_rate_pct,
        net_pnl_pts=summary.total_pnl_pts,
        profit_factor=summary.profit_factor,
        max_drawdown_pts=summary.max_drawdown_pts,
        chop_stop_exits=chop_stop,
        premium_target_before_spot=premium_target_early,
        premium_stop_exits=premium_stop,
        spot_exit_fallback=spot_fallback,
        trailing_stop_exits=trailing_stop,
    )
    return metrics, premium_trades


def run_vega_adaptive_emulation(
    events: list[SignalEvent],
    session_bars: dict[str, list[dict[str, Any]]],
    *,
    session_or_widths: dict[str, float],
    rolling_atr_map: dict[str, float],
    session_count: int = 0,
    cfg: Any = DEFAULT_CONFIG,
) -> tuple[OptionEmulationMetrics, list[TradeRecord], dict[str, int]]:
    """Emulate premium rules with VRR-scaled target cap at entry."""
    premium_trades: list[TradeRecord] = []
    chop_stop = premium_target_early = premium_stop = spot_fallback = 0
    regime_counts = {"compressed": 0, "normal": 0, "inflated": 0}
    stop_price = round(EMULATOR_BASE_PREMIUM * (1.0 - VEGA_STOP_LOSS_PCT), 2)
    risk = EMULATOR_BASE_PREMIUM - stop_price

    for entry_ev, exit_ev in _pair_entry_exit_events(events):
        _vrr, target_cap, regime = resolve_vega_entry_target(
            entry_ev,
            session_or_widths=session_or_widths,
            rolling_atr_map=rolling_atr_map,
        )
        regime_counts[regime] += 1
        target_price = round(EMULATOR_BASE_PREMIUM * (1.0 + target_cap), 2)
        result, _state = simulate_option_trade(
            entry_ev,
            exit_ev,
            session_bars,
            target_cap_pct=target_cap,
            stop_loss_pct=VEGA_STOP_LOSS_PCT,
            cfg=cfg,
        )
        prem_pnl = round(result.exit_premium - EMULATOR_BASE_PREMIUM, 2)

        if result.exit_reason == "CHOP_STOP":
            chop_stop += 1
        elif result.exit_reason == "PREMIUM_TARGET":
            premium_target_early += 1
        elif result.exit_reason == "PREMIUM_STOP":
            premium_stop += 1
        else:
            spot_fallback += 1

        premium_trades.append(
            TradeRecord(
                session_date=entry_ev.timestamp.strftime("%Y-%m-%d"),
                side=entry_ev.side,
                entry_time=entry_ev.timestamp,
                exit_time=exit_ev.timestamp,
                entry_price=EMULATOR_BASE_PREMIUM,
                exit_price=result.exit_premium,
                stop=stop_price,
                target=target_price,
                exit_reason=result.exit_reason,
                pnl_pts=prem_pnl,
                risk_pts=risk,
                r_multiple=prem_pnl / risk if risk else 0.0,
            )
        )

    summary = summarize_trades(premium_trades, sessions=session_count)
    metrics = OptionEmulationMetrics(
        total_trades=summary.total_trades,
        win_rate_pct=summary.win_rate_pct,
        net_pnl_pts=summary.total_pnl_pts,
        profit_factor=summary.profit_factor,
        max_drawdown_pts=summary.max_drawdown_pts,
        chop_stop_exits=chop_stop,
        premium_target_before_spot=premium_target_early,
        premium_stop_exits=premium_stop,
        spot_exit_fallback=spot_fallback,
    )
    return metrics, premium_trades, regime_counts


def build_trailing_comparison(
    spot: BacktestSummary,
    static_option: OptionEmulationMetrics,
    trailing_option: OptionEmulationMetrics,
) -> list[TrailingComparisonRow]:
    return [
        TrailingComparisonRow(
            name="Spot-Only Baseline",
            total_trades=spot.total_trades,
            win_rate_pct=spot.win_rate_pct,
            net_pnl_pts=spot.total_pnl_pts,
            profit_factor=spot.profit_factor,
            max_drawdown_pts=spot.max_drawdown_pts,
        ),
        TrailingComparisonRow(
            name="Option Phase 2 Optimized (+40% / −15%)",
            total_trades=static_option.total_trades,
            win_rate_pct=static_option.win_rate_pct,
            net_pnl_pts=static_option.net_pnl_pts,
            profit_factor=static_option.profit_factor,
            max_drawdown_pts=static_option.max_drawdown_pts,
            chop_stop_exits=static_option.chop_stop_exits,
            premium_target_exits=static_option.premium_target_before_spot,
            premium_stop_exits=static_option.premium_stop_exits,
            spot_exit_fallback=static_option.spot_exit_fallback,
        ),
        TrailingComparisonRow(
            name="Option Phase 2 Dynamic Trailing",
            total_trades=trailing_option.total_trades,
            win_rate_pct=trailing_option.win_rate_pct,
            net_pnl_pts=trailing_option.net_pnl_pts,
            profit_factor=trailing_option.profit_factor,
            max_drawdown_pts=trailing_option.max_drawdown_pts,
            chop_stop_exits=trailing_option.chop_stop_exits,
            premium_target_exits=trailing_option.premium_target_before_spot,
            premium_stop_exits=trailing_option.premium_stop_exits,
            trailing_stop_exits=trailing_option.trailing_stop_exits,
            spot_exit_fallback=trailing_option.spot_exit_fallback,
        ),
    ]


def format_trailing_comparison_table(rows: list[TrailingComparisonRow]) -> str:
    header = (
        "| Strategy | Trades | Win Rate | Net Pts | Profit Factor | Max DD | "
        "Chop | Prem Tgt | Prem SL | Trail | Spot Fb |"
    )
    sep = "|" + "|".join(["---"] * 11) + "|"
    lines = [header, sep]
    for r in rows:
        pf = f"{r.profit_factor:.2f}" if r.profit_factor is not None else "—"
        lines.append(
            f"| {r.name} | {r.total_trades} | {r.win_rate_pct:.1f}% | {r.net_pnl_pts:+.1f} | "
            f"{pf} | {r.max_drawdown_pts:.1f} | {r.chop_stop_exits} | {r.premium_target_exits} | "
            f"{r.premium_stop_exits} | {r.trailing_stop_exits} | {r.spot_exit_fallback} |"
        )
    return "\n".join(lines)


def build_thetaclock_comparison(
    spot: BacktestSummary,
    static_option: OptionEmulationMetrics,
    thetaclock_option: OptionEmulationMetrics,
) -> list[TrailingComparisonRow]:
    rows = [
        TrailingComparisonRow(
            name="Spot-Only Baseline",
            total_trades=spot.total_trades,
            win_rate_pct=spot.win_rate_pct,
            net_pnl_pts=spot.total_pnl_pts,
            profit_factor=spot.profit_factor,
            max_drawdown_pts=spot.max_drawdown_pts,
        ),
        TrailingComparisonRow(
            name="Option Phase 2 Optimized (+40% / −15%)",
            total_trades=static_option.total_trades,
            win_rate_pct=static_option.win_rate_pct,
            net_pnl_pts=static_option.net_pnl_pts,
            profit_factor=static_option.profit_factor,
            max_drawdown_pts=static_option.max_drawdown_pts,
            chop_stop_exits=static_option.chop_stop_exits,
            premium_target_exits=static_option.premium_target_before_spot,
            premium_stop_exits=static_option.premium_stop_exits,
            spot_exit_fallback=static_option.spot_exit_fallback,
        ),
        TrailingComparisonRow(
            name="Theta Clock Target Trimming",
            total_trades=thetaclock_option.total_trades,
            win_rate_pct=thetaclock_option.win_rate_pct,
            net_pnl_pts=thetaclock_option.net_pnl_pts,
            profit_factor=thetaclock_option.profit_factor,
            max_drawdown_pts=thetaclock_option.max_drawdown_pts,
            chop_stop_exits=thetaclock_option.chop_stop_exits,
            premium_target_exits=thetaclock_option.premium_target_before_spot,
            premium_stop_exits=thetaclock_option.premium_stop_exits,
            spot_exit_fallback=thetaclock_option.spot_exit_fallback,
        ),
    ]
    return sorted(rows, key=lambda r: r.profit_factor or 0.0, reverse=True)


def format_thetaclock_comparison_table(rows: list[TrailingComparisonRow]) -> str:
    header = (
        "| Rank | Strategy | Trades | Win Rate | Net Pts | Profit Factor | Max DD | "
        "Chop | Prem Tgt | Prem SL | Spot Fb |"
    )
    sep = "|" + "|".join(["---"] * 11) + "|"
    lines = [header, sep]
    for rank, r in enumerate(rows, start=1):
        pf = f"{r.profit_factor:.2f}" if r.profit_factor is not None else "—"
        lines.append(
            f"| {rank} | {r.name} | {r.total_trades} | {r.win_rate_pct:.1f}% | {r.net_pnl_pts:+.1f} | "
            f"{pf} | {r.max_drawdown_pts:.1f} | {r.chop_stop_exits} | {r.premium_target_exits} | "
            f"{r.premium_stop_exits} | {r.spot_exit_fallback} |"
        )
    return "\n".join(lines)


def build_vegaadaptive_comparison(
    spot: BacktestSummary,
    static_option: OptionEmulationMetrics,
    vega_option: OptionEmulationMetrics,
) -> list[TrailingComparisonRow]:
    rows = [
        TrailingComparisonRow(
            name="Spot-Only Baseline",
            total_trades=spot.total_trades,
            win_rate_pct=spot.win_rate_pct,
            net_pnl_pts=spot.total_pnl_pts,
            profit_factor=spot.profit_factor,
            max_drawdown_pts=spot.max_drawdown_pts,
        ),
        TrailingComparisonRow(
            name="Static +40% / −15% (Production)",
            total_trades=static_option.total_trades,
            win_rate_pct=static_option.win_rate_pct,
            net_pnl_pts=static_option.net_pnl_pts,
            profit_factor=static_option.profit_factor,
            max_drawdown_pts=static_option.max_drawdown_pts,
            chop_stop_exits=static_option.chop_stop_exits,
            premium_target_exits=static_option.premium_target_before_spot,
            premium_stop_exits=static_option.premium_stop_exits,
            spot_exit_fallback=static_option.spot_exit_fallback,
        ),
        TrailingComparisonRow(
            name="Vega Adaptive Target Cap Engine",
            total_trades=vega_option.total_trades,
            win_rate_pct=vega_option.win_rate_pct,
            net_pnl_pts=vega_option.net_pnl_pts,
            profit_factor=vega_option.profit_factor,
            max_drawdown_pts=vega_option.max_drawdown_pts,
            chop_stop_exits=vega_option.chop_stop_exits,
            premium_target_exits=vega_option.premium_target_before_spot,
            premium_stop_exits=vega_option.premium_stop_exits,
            spot_exit_fallback=vega_option.spot_exit_fallback,
        ),
    ]
    return sorted(rows, key=lambda r: r.profit_factor or 0.0, reverse=True)


def run_premium_bracket_sweep_matrix(
    events: list[SignalEvent],
    session_bars: dict[str, list[dict[str, Any]]],
    session_count: int,
    *,
    cfg: Any = DEFAULT_CONFIG,
) -> list[PremiumBracketCell]:
    """12-cell grid: premium target cap × stop-loss cushion."""
    cells: list[PremiumBracketCell] = []
    for target_cap in PREMIUM_TARGET_CAPS:
        for stop_loss in PREMIUM_STOP_LOSSES:
            metrics, _ = run_option_greeks_emulation(
                events,
                session_bars,
                session_count=session_count,
                target_cap_pct=target_cap,
                stop_loss_pct=stop_loss,
                cfg=cfg,
            )
            cells.append(
                PremiumBracketCell(
                    target_cap_pct=target_cap,
                    stop_loss_pct=stop_loss,
                    total_trades=metrics.total_trades,
                    win_rate_pct=metrics.win_rate_pct,
                    net_premium_pts=metrics.net_pnl_pts,
                    profit_factor=metrics.profit_factor,
                    max_drawdown_pts=metrics.max_drawdown_pts,
                    chop_stop_exits=metrics.chop_stop_exits,
                    premium_target_early=metrics.premium_target_before_spot,
                    premium_stop_exits=metrics.premium_stop_exits,
                )
            )
    return cells


def sort_premium_bracket_cells(cells: list[PremiumBracketCell]) -> list[PremiumBracketCell]:
    """Rank highest → lowest by option profit factor (None last)."""
    return sorted(
        cells,
        key=lambda c: (c.profit_factor is not None, c.profit_factor or 0.0),
        reverse=True,
    )


def format_premium_bracket_table(cells: list[PremiumBracketCell]) -> str:
    """Sorted markdown table — premium bracket parametric sweep."""
    ranked = sort_premium_bracket_cells(cells)
    header = (
        "| Target Cap | Stop Loss | Trades | Win Rate | Net Premium Pts | "
        "Profit Factor | Max DD | Chop | Prem Tgt | Prem SL |"
    )
    sep = "|" + "|".join(["---"] * 10) + "|"
    lines = [header, sep]
    for c in ranked:
        pf = f"{c.profit_factor:.2f}" if c.profit_factor is not None else "—"
        lines.append(
            f"| +{c.target_cap_pct * 100:.0f}% | -{c.stop_loss_pct * 100:.0f}% | "
            f"{c.total_trades} | {c.win_rate_pct:.1f}% | {c.net_premium_pts:+.1f} | "
            f"{pf} | {c.max_drawdown_pts:.1f} | {c.chop_stop_exits} | "
            f"{c.premium_target_early} | {c.premium_stop_exits} |"
        )
    return "\n".join(lines)


def format_strategy_comparison_table(
    spot: BacktestSummary,
    option: OptionEmulationMetrics,
) -> str:
    """Spot baseline vs Option-Emulated Phase 2 summary."""
    header = "| Strategy Version | Total Trades | Win Rate | Profit Factor | Max Drawdown (Pts) |"
    sep = "|" + "|".join(["---"] * 5) + "|"
    spot_pf = f"{spot.profit_factor:.2f}" if spot.profit_factor is not None else "—"
    opt_pf = f"{option.profit_factor:.2f}" if option.profit_factor is not None else "—"
    lines = [
        header,
        sep,
        (
            f"| Spot-only baseline | {spot.total_trades} | {spot.win_rate_pct:.1f}% | "
            f"{spot_pf} | {spot.max_drawdown_pts:.1f} |"
        ),
        (
            f"| Option-Emulated Phase 2 | {option.total_trades} | {option.win_rate_pct:.1f}% | "
            f"{opt_pf} | {option.max_drawdown_pts:.1f} |"
        ),
    ]
    return "\n".join(lines)


def _load_range(from_date: date, to_date: date) -> tuple[pd.DataFrame, list[str], list[str]]:
    frames, sessions = [], []
    missing: list[str] = []
    d = from_date
    while d <= to_date:
        if d.weekday() < 5:
            ds = d.isoformat()
            p = HIST / f"{ds}_5m.csv"
            if p.exists():
                frames.append(load_candles_csv(p))
                sessions.append(ds)
            else:
                missing.append(ds)
        d += timedelta(days=1)
    if not frames:
        raise SystemExit(f"No CSV between {from_date} and {to_date}")
    return pd.concat(frames, ignore_index=True), sessions, missing


def _parse_gate_minutes(gate: str) -> int:
    h, m = gate.split(":")
    return int(h) * 60 + int(m)


def _gate_label(gate: str | None) -> str:
    return "Baseline" if gate is None else gate


def _bars_label(cap: int | None) -> str:
    return "Unrestricted" if cap is None else str(cap)


def _spread_label(pct: float | None) -> str:
    return "Unrestricted" if pct is None else f"{pct:.1f}%"


def _buffer_label(mult: float | None) -> str:
    return "None" if mult is None else f"{mult:.1f}×ATR"


def _is_expiry_day(session_date: str) -> bool:
    """NIFTY weekly expiry heuristic (Tuesday) — research-only."""
    return date.fromisoformat(session_date).weekday() == 1


def _proxy_spread_drag_pct(extra: dict[str, Any], session_date: str) -> float:
    """ponytail: OR-width proxy when live option quotes absent in replay."""
    if extra.get("spread_drag_pct") is not None:
        return float(extra["spread_drag_pct"])
    or_h, or_l = extra.get("or_high"), extra.get("or_low")
    width = float(or_h) - float(or_l) if or_h is not None and or_l is not None else 50.0
    base = 1.8 + width * 0.015
    if _is_expiry_day(session_date):
        base += 0.8
    return round(min(base, 8.0), 2)


def _bar_lookup(
    session_bars: dict[str, list[dict[str, Any]]],
    session_date: str,
    bar_index: int,
) -> dict[str, Any] | None:
    for bar in session_bars.get(session_date, []):
        if bar["index"] == bar_index:
            return bar
    return None


def enrich_events_for_sweep(
    events: list[SignalEvent],
    session_bars: dict[str, list[dict[str, Any]]],
) -> None:
    """Attach research telemetry on replay events (in-memory only)."""
    for event in events:
        extra = dict(event.extra or {})
        sd = event.timestamp.strftime("%Y-%m-%d")
        if event.event_type in ENTRY_TYPES:
            extra.setdefault("spread_drag_pct", _proxy_spread_drag_pct(extra, sd))
            extra.setdefault("is_expiry_day", _is_expiry_day(sd))
            bar = _bar_lookup(session_bars, sd, event.bar_index)
            if bar and bar.get("atr") is not None:
                extra.setdefault("atr", bar["atr"])
            event.extra = extra
        elif event.event_type in ("TARGET_CE", "TARGET_PE"):
            bar = _bar_lookup(session_bars, sd, event.bar_index)
            if bar:
                extra.setdefault("bar_high", bar["high"])
                extra.setdefault("bar_low", bar["low"])
                extra.setdefault("bar_close", bar["close"])
                extra.setdefault("atr", bar.get("atr"))
                audit = audit_target_wick_close(
                    event_type=event.event_type,
                    target=event.target,
                    bar_high=float(bar["high"]),
                    bar_low=float(bar["low"]),
                    bar_close=float(bar["close"]),
                )
                extra.update(audit)
            event.extra = extra


def _precompute_j_outside_streaks(
    events: list[SignalEvent],
    session_bars: dict[str, list[dict[str, Any]]],
) -> dict[tuple[str, str, datetime], int]:
    """Map (event_type, side, timestamp) → max consecutive bars outside OR before J+ entry."""
    out: dict[tuple[str, str, datetime], int] = {}
    for e in events:
        if e.event_type not in ENTRY_TYPES:
            continue
        extra = e.extra or {}
        if extra.get("strategy") != "j_plus":
            continue
        or_high, or_low = extra.get("or_high"), extra.get("or_low")
        if or_high is None or or_low is None:
            continue
        sd = e.timestamp.strftime("%Y-%m-%d")
        streak = max_consecutive_bars_outside_or(
            session_bars.get(sd, []),
            e.bar_index,
            float(or_high),
            float(or_low),
            e.side,
        )
        out[(e.event_type, e.side, e.timestamp)] = streak
    return out


def _spread_drag_for_entry(event: SignalEvent) -> float | None:
    extra = event.extra or {}
    val = extra.get("spread_drag_pct")
    return float(val) if val is not None else None


def _entry_blocked(
    event: SignalEvent,
    *,
    time_gate: str | None = None,
    max_bars_outside_or: int | None = None,
    max_spread_drag_pct: float | None = None,
    expiry_entry_cutoff: str | None = None,
    j_streaks: dict[tuple[str, str, datetime], int] | None = None,
    cfg: Any = DEFAULT_CONFIG,
) -> bool:
    j_streaks = j_streaks or {}
    if time_gate is not None:
        if bar_close_minutes(event.timestamp, cfg) > _parse_gate_minutes(time_gate):
            return True
    if max_bars_outside_or is not None:
        extra = event.extra or {}
        if extra.get("strategy") == "j_plus":
            key = (event.event_type, event.side, event.timestamp)
            streak = j_streaks.get(key, 0)
            if streak > max_bars_outside_or:
                return True
    if max_spread_drag_pct is not None:
        drag = _spread_drag_for_entry(event)
        if drag is not None and drag > max_spread_drag_pct:
            return True
    if expiry_entry_cutoff is not None:
        sd = event.timestamp.strftime("%Y-%m-%d")
        extra = event.extra or {}
        expiry = extra.get("is_expiry_day")
        if expiry is None:
            expiry = _is_expiry_day(sd)
        if expiry and bar_close_minutes(event.timestamp, cfg) > _parse_gate_minutes(expiry_entry_cutoff):
            return True
    return False


def _target_exit_blocked(
    event: SignalEvent,
    *,
    target_buffer_mult_atr: float | None,
    open_entry: SignalEvent | None,
) -> bool:
    if target_buffer_mult_atr is None or event.event_type not in ("TARGET_CE", "TARGET_PE"):
        return False
    extra = event.extra or {}
    entry_extra = (open_entry.extra or {}) if open_entry else {}
    atr = extra.get("atr") or entry_extra.get("atr")
    target = event.target
    bar_high = extra.get("bar_high")
    bar_low = extra.get("bar_low")
    if atr is None or target is None or bar_high is None or bar_low is None:
        return False
    buffer = target_buffer_mult_atr * float(atr)
    if event.event_type == "TARGET_CE":
        return float(bar_high) < float(target) + buffer
    return float(bar_low) > float(target) - buffer


def filter_trade_events(
    events: list[SignalEvent],
    *,
    time_gate: str | None = None,
    max_bars_outside_or: int | None = None,
    max_spread_drag_pct: float | None = None,
    target_buffer_mult_atr: float | None = None,
    expiry_entry_cutoff: str | None = None,
    j_streaks: dict[tuple[str, str, datetime], int] | None = None,
    cfg: Any = DEFAULT_CONFIG,
) -> list[SignalEvent]:
    """Chronological post-filter: drop blocked entries; defer weak target exits."""
    j_streaks = j_streaks or {}
    trade_types = ENTRY_TYPES | EXIT_TYPES
    ordered = sorted(
        (e for e in events if e.event_type in trade_types),
        key=lambda e: (e.timestamp, e.bar_index),
    )
    out: list[SignalEvent] = []
    position_open = False
    open_entry: SignalEvent | None = None

    for e in ordered:
        if e.event_type in ENTRY_TYPES:
            if _entry_blocked(
                e,
                time_gate=time_gate,
                max_bars_outside_or=max_bars_outside_or,
                max_spread_drag_pct=max_spread_drag_pct,
                expiry_entry_cutoff=expiry_entry_cutoff,
                j_streaks=j_streaks,
                cfg=cfg,
            ):
                position_open = False
                open_entry = None
                continue
            out.append(e)
            position_open = True
            open_entry = e
            continue

        if e.event_type in EXIT_TYPES:
            if not position_open:
                continue
            if _target_exit_blocked(e, target_buffer_mult_atr=target_buffer_mult_atr, open_entry=open_entry):
                continue
            out.append(e)
            position_open = False
            open_entry = None

    return out


def evaluate_cell(
    events: list[SignalEvent],
    *,
    time_gate: str | None = None,
    max_bars_outside_or: int | None = None,
    max_spread_drag_pct: float | None = None,
    target_buffer_mult_atr: float | None = None,
    expiry_entry_cutoff: str | None = None,
    j_streaks: dict[tuple[str, str, datetime], int] | None = None,
    session_count: int = 0,
    cfg: Any = DEFAULT_CONFIG,
) -> SweepCell:
    filtered = filter_trade_events(
        events,
        time_gate=time_gate,
        max_bars_outside_or=max_bars_outside_or,
        max_spread_drag_pct=max_spread_drag_pct,
        target_buffer_mult_atr=target_buffer_mult_atr,
        expiry_entry_cutoff=expiry_entry_cutoff,
        j_streaks=j_streaks,
        cfg=cfg,
    )
    trades = pair_trades(filtered)
    summary = summarize_trades(trades, sessions=session_count)
    return SweepCell(
        time_gate=time_gate,
        max_bars_outside_or=max_bars_outside_or,
        max_spread_drag_pct=max_spread_drag_pct,
        target_buffer_mult_atr=target_buffer_mult_atr,
        expiry_entry_cutoff=expiry_entry_cutoff,
        total_trades=summary.total_trades,
        win_rate_pct=summary.win_rate_pct,
        net_pnl_pts=summary.total_pnl_pts,
        profit_factor=summary.profit_factor,
        max_drawdown_pts=summary.max_drawdown_pts,
    )


def run_sweep_matrix(
    logger: ReplayLogger,
    df: pd.DataFrame,
    session_count: int,
    cfg: Any = DEFAULT_CONFIG,
) -> list[SweepCell]:
    """Legacy 16-cell matrix (time gate × J+ bars outside OR)."""
    session_bars = index_session_bars(df)
    enrich_events_for_sweep(logger.events, session_bars)
    j_streaks = _precompute_j_outside_streaks(logger.events, session_bars)
    cells: list[SweepCell] = []
    for gate in TIME_GATES:
        for cap in MAX_BARS_OUTSIDE:
            cells.append(
                evaluate_cell(
                    logger.events,
                    time_gate=gate,
                    max_bars_outside_or=cap,
                    j_streaks=j_streaks,
                    session_count=session_count,
                    cfg=cfg,
                )
            )
    return cells


def run_advanced_sweep_matrix(
    logger: ReplayLogger,
    df: pd.DataFrame,
    session_count: int,
    cfg: Any = DEFAULT_CONFIG,
) -> list[SweepCell]:
    """36-cell grid: spread drag × target buffer × expiry entry cutoff."""
    session_bars = index_session_bars(df)
    enrich_events_for_sweep(logger.events, session_bars)
    j_streaks = _precompute_j_outside_streaks(logger.events, session_bars)
    cells: list[SweepCell] = []
    for spread in MAX_SPREAD_DRAG_PCT:
        for buf in TARGET_BUFFER_MULT_ATR:
            for expiry_cut in EXPIRY_ENTRY_CUTOFF_TIME:
                cells.append(
                    evaluate_cell(
                        logger.events,
                        max_spread_drag_pct=spread,
                        target_buffer_mult_atr=buf,
                        expiry_entry_cutoff=expiry_cut,
                        j_streaks=j_streaks,
                        session_count=session_count,
                        cfg=cfg,
                    )
                )
    return cells


def format_matrix_table(cells: list[SweepCell]) -> str:
    header = (
        "| Time Gate | J+ Max Bars Outside | Total Trades | Combined Win Rate | "
        "Combined Net P&L (Pts) | Combined Profit Factor | Max Drawdown |"
    )
    sep = "|" + "|".join(["---"] * 7) + "|"
    lines = [header, sep]
    for c in cells:
        pf = f"{c.profit_factor:.2f}" if c.profit_factor is not None else "—"
        lines.append(
            f"| {_gate_label(c.time_gate)} | {_bars_label(c.max_bars_outside_or)} | "
            f"{c.total_trades} | {c.win_rate_pct:.1f}% | {c.net_pnl_pts:+.1f} | {pf} | {c.max_drawdown_pts:.1f} |"
        )
    return "\n".join(lines)


def format_advanced_table(cells: list[SweepCell]) -> str:
    header = (
        "| Max Spread Drag | Target Buffer | Expiry Cutoff | Trades | Win Rate | "
        "Net P&L (Pts) | Profit Factor | Max DD |"
    )
    sep = "|" + "|".join(["---"] * 8) + "|"
    lines = [header, sep]
    for c in cells:
        pf = f"{c.profit_factor:.2f}" if c.profit_factor is not None else "—"
        lines.append(
            f"| {_spread_label(c.max_spread_drag_pct)} | {_buffer_label(c.target_buffer_mult_atr)} | "
            f"{_gate_label(c.expiry_entry_cutoff)} | {c.total_trades} | {c.win_rate_pct:.1f}% | "
            f"{c.net_pnl_pts:+.1f} | {pf} | {c.max_drawdown_pts:.1f} |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sgnal parametric research sweeps")
    parser.add_argument("--from", dest="from_date", default="2025-05-02")
    parser.add_argument("--to", dest="to_date", default="2026-06-12")
    parser.add_argument(
        "--mode",
        choices=("legacy", "advanced", "premium", "thetaclock", "vegaadaptive", "both"),
        default="advanced",
        help="legacy=4×4; advanced=spread×buffer; premium=grid; thetaclock=theta trim; vegaadaptive=VRR cap; both=all",
    )
    parser.add_argument("--json", type=Path, default=None, help="Write JSON payload")
    parser.add_argument("--no-json", action="store_true", help="Skip JSON output")
    args = parser.parse_args()

    from_d = date.fromisoformat(args.from_date)
    to_d = date.fromisoformat(args.to_date)
    df, sessions, missing = _load_range(from_d, to_d)

    cfg = DEFAULT_CONFIG
    combined = CombinedStrategyConfig(strategy=cfg, enable_j_plus=True, j_trap=J_TRAP_ROBUST)
    logger = run_replay_fast(df, cfg, combined_cfg=combined)

    legacy_cells: list[SweepCell] = []
    advanced_cells: list[SweepCell] = []
    premium_cells: list[PremiumBracketCell] = []

    session_bars = index_session_bars(df)
    enrich_events_for_sweep(logger.events, session_bars)
    j_streaks = _precompute_j_outside_streaks(logger.events, session_bars)
    baseline_events = filter_trade_events(logger.events, j_streaks=j_streaks, cfg=cfg)

    if args.mode in ("legacy", "both"):
        legacy_cells = run_sweep_matrix(logger, df, len(sessions), cfg)
        print("## Legacy sweep (afternoon gate × J+ bars outside OR)\n")
        print(format_matrix_table(legacy_cells))
        print()

    if args.mode in ("advanced", "both"):
        advanced_cells = run_advanced_sweep_matrix(logger, df, len(sessions), cfg)
        print("## Advanced option-defense sweep (spread × target buffer × expiry cutoff)\n")
        print(format_advanced_table(advanced_cells))
        print()

    if args.mode in ("premium", "both"):
        premium_cells = run_premium_bracket_sweep_matrix(
            baseline_events, session_bars, len(sessions), cfg=cfg,
        )
        print("## Premium bracket parametric sweep (target cap × stop loss)\n")
        print(format_premium_bracket_table(premium_cells))
        print()
        best = sort_premium_bracket_cells(premium_cells)[0] if premium_cells else None
        if best:
            pf = f"{best.profit_factor:.2f}" if best.profit_factor is not None else "—"
            print(
                f"Top matrix: +{best.target_cap_pct * 100:.0f}% target / "
                f"-{best.stop_loss_pct * 100:.0f}% stop → PF={pf} "
                f"net={best.net_premium_pts:+.1f} prem pts DD={best.max_drawdown_pts:.1f}"
            )
            print()

    # Phase 2 option Greeks emulator — spot baseline vs premium rules
    spot_summary = summarize_trades(pair_trades(baseline_events), sessions=len(sessions))
    option_metrics, _option_trades = run_option_greeks_emulation(
        baseline_events, session_bars, session_count=len(sessions), cfg=cfg,
    )
    trailing_metrics: OptionEmulationMetrics | None = None
    trailing_rows: list[TrailingComparisonRow] = []
    thetaclock_metrics: OptionEmulationMetrics | None = None
    thetaclock_rows: list[TrailingComparisonRow] = []
    vega_metrics: OptionEmulationMetrics | None = None
    vega_rows: list[TrailingComparisonRow] = []
    vega_regime_counts: dict[str, int] = {}

    if args.mode == "vegaadaptive":
        session_or_widths = build_session_or_widths(df, cfg)
        session_atr = build_session_mean_atr(df, cfg)
        rolling_atr = build_rolling_atr_map(sessions, session_atr)
        vega_metrics, _, vega_regime_counts = run_vega_adaptive_emulation(
            baseline_events,
            session_bars,
            session_or_widths=session_or_widths,
            rolling_atr_map=rolling_atr,
            session_count=len(sessions),
            cfg=cfg,
        )
        vega_rows = build_vegaadaptive_comparison(spot_summary, option_metrics, vega_metrics)
        print("## Option Greeks emulator — Vega Adaptive target cap (ranked by PF)\n")
        print(format_thetaclock_comparison_table(vega_rows))
        print()
        print(
            f"Vega Adaptive exits: chop_stop={vega_metrics.chop_stop_exits} "
            f"premium_target={vega_metrics.premium_target_before_spot} "
            f"premium_stop={vega_metrics.premium_stop_exits} "
            f"spot_fallback={vega_metrics.spot_exit_fallback}"
        )
        print(
            f"VRR regimes: compressed(+50%)={vega_regime_counts.get('compressed', 0)} "
            f"normal(+40%)={vega_regime_counts.get('normal', 0)} "
            f"inflated(+20%)={vega_regime_counts.get('inflated', 0)}"
        )
        print()

    if args.mode == "thetaclock":
        thetaclock_metrics, _ = run_option_greeks_emulation(
            baseline_events, session_bars, session_count=len(sessions), use_thetaclock=True, cfg=cfg,
        )
        thetaclock_rows = build_thetaclock_comparison(spot_summary, option_metrics, thetaclock_metrics)
        print("## Option Greeks emulator — Theta Clock target trimming (ranked by PF)\n")
        print(format_thetaclock_comparison_table(thetaclock_rows))
        print()
        print(
            f"Theta Clock exits: chop_stop={thetaclock_metrics.chop_stop_exits} "
            f"premium_target={thetaclock_metrics.premium_target_before_spot} "
            f"premium_stop={thetaclock_metrics.premium_stop_exits} "
            f"spot_fallback={thetaclock_metrics.spot_exit_fallback}"
        )
        print()

    if args.mode not in ("premium", "thetaclock", "vegaadaptive"):
        print("## Option Greeks emulator — Spot vs Phase 2 premium rules\n")
        print(format_strategy_comparison_table(spot_summary, option_metrics))
        print()
        print(
            f"Phase 2 exits: chop_stop={option_metrics.chop_stop_exits} "
            f"premium_target_early={option_metrics.premium_target_before_spot} "
            f"premium_stop={option_metrics.premium_stop_exits} "
            f"spot_fallback={option_metrics.spot_exit_fallback}"
        )
        print()

    baseline = advanced_cells[0] if advanced_cells else (legacy_cells[0] if legacy_cells else None)
    if baseline:
        print(
            f"Baseline calibration: trades={baseline.total_trades} WR={baseline.win_rate_pct:.1f}% "
            f"P&L={baseline.net_pnl_pts:+.1f} PF={baseline.profit_factor:.2f} DD={baseline.max_drawdown_pts:.1f}"
        )
    print(f"Sessions replayed: {len(sessions)} ({sessions[0]} → {sessions[-1]})")
    if missing:
        print(f"Missing CSV ({len(missing)}): {', '.join(missing[:6])}{'...' if len(missing) > 6 else ''}")

    if not args.no_json:
        out_path = args.json or OUT / {
            "advanced": "sweep_advanced.json",
            "premium": "sweep_advanced.json",
            "thetaclock": "sweep_advanced.json",
            "vegaadaptive": "sweep_advanced.json",
            "legacy": "sweep_matrix.json",
            "both": "sweep_advanced.json",
        }.get(args.mode, "sweep_matrix.json")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "from": args.from_date,
            "to": args.to_date,
            "mode": args.mode,
            "sessions": len(sessions),
            "missing_sessions": missing,
        }
        if legacy_cells:
            payload["legacy_matrix"] = [c.to_dict() for c in legacy_cells]
        if advanced_cells:
            payload["advanced_matrix"] = [c.to_dict() for c in advanced_cells]
        if premium_cells:
            payload["premium_bracket_matrix"] = [
                c.to_dict() for c in sort_premium_bracket_cells(premium_cells)
            ]
        payload["option_greeks_emulation"] = {
            "spot_baseline": {
                "total_trades": spot_summary.total_trades,
                "win_rate_pct": round(spot_summary.win_rate_pct, 1),
                "net_pnl_pts": round(spot_summary.total_pnl_pts, 2),
                "profit_factor": round(spot_summary.profit_factor, 2) if spot_summary.profit_factor is not None else None,
                "max_drawdown_pts": round(spot_summary.max_drawdown_pts, 2),
            },
            "option_phase2": option_metrics.to_dict(),
            "emulator_config": {
                "base_premium": EMULATOR_BASE_PREMIUM,
                "greeks_atm": GREEKS_ATM,
                "greeks_itm2": GREEKS_ITM2,
                "morning_iv_crush_pts": MORNING_IV_CRUSH_TOTAL_PTS,
                "trail_activation_mult": TRAIL_ACTIVATION_MULT,
                "trail_cushion_pct": TRAIL_CUSHION_PCT,
                "theta_clock_initial_cap": THETA_CLOCK_INITIAL_TARGET_CAP,
                "theta_clock_decay_per_bar": THETA_CLOCK_DECAY_PER_BAR,
                "theta_clock_target_floor": THETA_CLOCK_TARGET_FLOOR,
                "vega_baseline_atr_proxy": NIFTY_BASELINE_ATR_PROXY,
                "vega_vrr_compressed_max": VEGA_VRR_COMPRESSED_MAX,
                "vega_vrr_inflated_min": VEGA_VRR_INFLATED_MIN,
            },
        }
        if vega_metrics is not None:
            payload["vega_adaptive_comparison"] = {
                "rows": [r.to_dict() for r in vega_rows],
                "option_phase2_vega_adaptive": vega_metrics.to_dict(),
                "vrr_regime_counts": vega_regime_counts,
                "exit_breakdown": {
                    "chop_stop": vega_metrics.chop_stop_exits,
                    "premium_target": vega_metrics.premium_target_before_spot,
                    "premium_stop": vega_metrics.premium_stop_exits,
                    "spot_fallback": vega_metrics.spot_exit_fallback,
                },
            }
        if thetaclock_metrics is not None:
            payload["theta_clock_comparison"] = {
                "rows": [r.to_dict() for r in thetaclock_rows],
                "option_phase2_thetaclock": thetaclock_metrics.to_dict(),
                "exit_breakdown": {
                    "chop_stop": thetaclock_metrics.chop_stop_exits,
                    "premium_target": thetaclock_metrics.premium_target_before_spot,
                    "premium_stop": thetaclock_metrics.premium_stop_exits,
                    "spot_fallback": thetaclock_metrics.spot_exit_fallback,
                },
            }
        if trailing_metrics is not None:
            payload["trailing_stop_comparison"] = {
                "rows": [r.to_dict() for r in trailing_rows],
                "option_phase2_trailing": trailing_metrics.to_dict(),
                "exit_breakdown": {
                    "chop_stop": trailing_metrics.chop_stop_exits,
                    "premium_target": trailing_metrics.premium_target_before_spot,
                    "premium_stop": trailing_metrics.premium_stop_exits,
                    "trailing_stop_triggered": trailing_metrics.trailing_stop_exits,
                    "spot_fallback": trailing_metrics.spot_exit_fallback,
                },
            }
        out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
