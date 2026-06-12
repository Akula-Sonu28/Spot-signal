#!/usr/bin/env python3
"""Backtest alternative setups on days the locked v3.8 strategy does not trade."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from bot.backtest import run_backtest
from bot.config import DEFAULT_CONFIG, StrategyConfig
from bot.historical_data import trading_days_back
from bot.indicators import or_width
from bot.logger import ReplayLogger
from bot.replay import load_candles_csv, run_replay_fast
from bot.state import PositionSide
from bot.strategy import BarContext, build_bar_context, process_bar, update_or
from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.replay_engine import run_setup_replay
from research.backtests.options_setups_comparison.setups import SETUP_REGISTRY
from research.backtests.options_setups_comparison.setups.common import (
    adx_ok,
    standard_preamble,
    try_enter,
    vwap_long_ok,
    vwap_short_ok,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "historical"
OUT = ROOT / "data" / "research"

# Candidate setups for gap / wide-OR / chop days (skip A baseline, B same OR filter)
ALT_SETUP_IDS = ("F", "H", "D", "E", "C", "G")


def _load_days(days: int) -> tuple[pd.DataFrame, list[str]]:
    day_list = trading_days_back(date.today(), days)
    frames: list[pd.DataFrame] = []
    sessions: list[str] = []
    for d in day_list:
        path = HIST / f"{d.isoformat()}_5m.csv"
        if path.exists():
            frames.append(load_candles_csv(path))
            sessions.append(d.isoformat())
    if not frames:
        raise SystemExit(f"No CSV files under {HIST}")
    return pd.concat(frames, ignore_index=True), sessions


def _session_or_width(session_df: pd.DataFrame, cfg: StrategyConfig) -> float | None:
    day_high: float | None = None
    day_low: float | None = None
    for _, row in session_df.iterrows():
        bar = build_bar_context(
            index=0,
            timestamp=row["timestamp"].to_pydatetime(),
            session_date=str(row["session_date"]),
            o=float(row["open"]),
            h=float(row["high"]),
            l=float(row["low"]),
            c=float(row["close"]),
            vol=float(row["volume"]),
            vwap=None,
            atr=None,
            adx=None,
            cfg=cfg,
        )
        if bar.in_or:
            day_high = bar.high if day_high is None else max(day_high, bar.high)
            day_low = bar.low if day_low is None else min(day_low, bar.low)
    return or_width(day_high, day_low)


def _classify_non_trade(width: float | None, cfg: StrategyConfig) -> str:
    if width is None:
        return "no_or"
    if width < cfg.min_or_range:
        return "or_narrow"
    if width > cfg.max_or_range:
        return "or_wide"
    return "or_ok_no_signal"


def _production_trades_by_session(df: pd.DataFrame, cfg: StrategyConfig) -> dict[str, int]:
    logger = run_replay_fast(df, cfg)
    summary = run_backtest(logger)
    counts: dict[str, int] = defaultdict(int)
    for t in summary.trades:
        counts[t.session_date] += 1
    return dict(counts)


def _filter_sessions(df: pd.DataFrame, sessions: list[str]) -> pd.DataFrame:
    mask = df["session_date"].isin(sessions)
    return df.loc[mask].reset_index(drop=True)


def process_wide_or_bar(
    state,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped,
) -> None:
    """W: OR width 100–200 only — same breakout rules, no max OR cap."""
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window:
        return
    width = or_width(day.or_high, day.or_low)
    if width is None or width <= cfg.max_or_range or width > 200.0:
        return
    if not adx_ok(bar, cfg):
        return

    if bar.close > (day.or_high or float("inf")) and vwap_long_ok(bar, cfg):
        try_enter(
            state, bar, PositionSide.CE, (day.or_low or bar.close) - cfg.sl_buffer_pts,
            "WIDE_ORB_CE", "W", cfg, research, logger, slippage, skipped,
        )
    elif bar.close < (day.or_low or float("-inf")) and vwap_short_ok(bar, cfg):
        try_enter(
            state, bar, PositionSide.PE, (day.or_high or bar.close) + cfg.sl_buffer_pts,
            "WIDE_ORB_PE", "W", cfg, research, logger, slippage, skipped,
        )


def process_gap_fade_simple_bar(
    state,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped,
) -> None:
    """I: Gap-up fade — lose VWAP + close below session open (literature-backed)."""
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window:
        return
    prior = state.prior_close
    if prior is None or prior <= 0 or bar.vwap is None:
        return

    f = day.setup_flags
    gap_pct = (bar.open - prior) / prior * 100.0
    if abs(gap_pct) < research.gap_min_pct:
        return

    if bar.index == f.get("first_bar", -1):
        f["session_open"] = bar.open
        f["gap_up"] = gap_pct > 0
        f["gap_down"] = gap_pct < 0

    # Fade gap-up: price fails back through VWAP and below open
    if f.get("gap_up") and bar.close < bar.vwap and bar.close < f.get("session_open", bar.open):
        try_enter(
            state, bar, PositionSide.PE, bar.high + cfg.sl_buffer_pts,
            "GAP_FADE_SIMPLE_PE", "I", cfg, research, logger, slippage, skipped,
        )
    # Fade gap-down mirror
    if f.get("gap_down") and bar.close > bar.vwap and bar.close > f.get("session_open", bar.open):
        try_enter(
            state, bar, PositionSide.CE, bar.low - cfg.sl_buffer_pts,
            "GAP_FADE_SIMPLE_CE", "I", cfg, research, logger, slippage, skipped,
        )


def _run_alt(
    df: pd.DataFrame,
    setup_id: str,
    processor,
    cfg: StrategyConfig,
    sessions: list[str],
) -> dict:
    research = ResearchConfig()
    logger, skipped = run_setup_replay(
        df, setup_id, processor, cfg, research, SlippageModel("none")
    )
    summary = run_backtest(logger, session_dates=sessions)
    return {
        "setup_id": setup_id,
        "trades": summary.total_trades,
        "win_rate_pct": round(summary.win_rate_pct, 1),
        "total_pnl_pts": round(summary.total_pnl_pts, 1),
        "profit_factor": round(summary.profit_factor, 2) if summary.profit_factor else None,
        "max_dd_pts": round(summary.max_drawdown_pts, 1),
        "skipped": len(skipped.entries),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Alternative setups on non-trade days")
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()

    cfg = DEFAULT_CONFIG
    df, all_sessions = _load_days(args.days)

    prod_counts = _production_trades_by_session(df, cfg)
    non_trade: list[str] = []
    classify: Counter[str] = Counter()

    for session in all_sessions:
        if prod_counts.get(session, 0) > 0:
            continue
        non_trade.append(session)
        sdf = df[df["session_date"] == session]
        w = _session_or_width(sdf, cfg)
        classify[_classify_non_trade(w, cfg)] += 1

    print(f"Sessions: {len(all_sessions)} | Production trades: {sum(prod_counts.values())}")
    print(f"Non-trade days: {len(non_trade)} ({100*len(non_trade)/len(all_sessions):.0f}%)")
    print("Why no trade:")
    for reason, n in classify.most_common():
        print(f"  {reason}: {n}")
    print()

    if not non_trade:
        print("No non-trade days in sample.")
        return 0

    sub = _filter_sessions(df, non_trade)
    results: list[dict] = []

    extra_setups = {
        "W": ("wide_or_breakout", process_wide_or_bar),
        "I": ("gap_fade_simple", process_gap_fade_simple_bar),
    }

    for sid in ALT_SETUP_IDS:
        if sid not in SETUP_REGISTRY:
            continue
        name, proc = SETUP_REGISTRY[sid]
        row = _run_alt(sub, sid, proc, cfg, non_trade)
        row["setup_name"] = name
        results.append(row)

    for sid, (name, proc) in extra_setups.items():
        row = _run_alt(sub, sid, proc, cfg, non_trade)
        row["setup_name"] = name
        results.append(row)

    ranked = sorted(results, key=lambda r: (r["total_pnl_pts"], r["profit_factor"] or 0), reverse=True)
    print(f"Alternatives on {len(non_trade)} non-trade days only:\n")
    print(f"{'ID':>2}  {'Name':22}  Trd   WR%      P&L    PF     DD")
    print("-" * 58)
    for r in ranked:
        print(
            f"{r['setup_id']:>2}  {r['setup_name'][:22]:22}  {r['trades']:3} "
            f"{r['win_rate_pct']:5.1f} {r['total_pnl_pts']:+8.1f} "
            f"{(r['profit_factor'] or 0):5.2f} {r['max_dd_pts']:7.1f}"
        )

    out = {
        "sessions": len(all_sessions),
        "non_trade_days": len(non_trade),
        "classify": dict(classify),
        "non_trade_sessions": non_trade,
        "results": ranked,
    }
    out_path = OUT / f"non_trade_sweep_{date.today().isoformat()}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\nWrote {out_path}")

    best = ranked[0] if ranked else None
    if best and best["trades"] > 0 and (best["profit_factor"] or 0) >= 1.2:
        print(f"\n→ Best candidate: {best['setup_id']} ({best['setup_name']}) — review before live.")
    else:
        print("\n→ No alternative cleared PF≥1.2 on non-trade days — staying flat is likely correct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
