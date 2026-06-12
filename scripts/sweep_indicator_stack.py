#!/usr/bin/env python3
"""Sweep 12 multi-indicator setups (VWAP, MACD, BB, RSI, etc.) on non-trade days."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from bot.backtest import run_backtest
from bot.config import DEFAULT_CONFIG, StrategyConfig
from bot.historical_data import trading_days_back
from bot.logger import ReplayLogger
from bot.replay import _compute_indicators, load_candles_csv, run_replay_fast
from bot.state import Position
from bot.strategy import build_bar_context
from research.backtests.indicator_setups import INDICATOR_SETUPS, process_indicator_setup_bar
from research.backtests.indicator_stack import build_indicator_stack
from research.backtests.non_trade_setups import process_or_fake_break_stack
from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.risk_engine import (
    ResearchState,
    SkippedLogger,
    make_research_day,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "historical"
OUT = ROOT / "data" / "research"


def _load_days(days: int) -> tuple[pd.DataFrame, list[str]]:
    day_list = trading_days_back(date.today(), days)
    frames, sessions = [], []
    for d in day_list:
        p = HIST / f"{d.isoformat()}_5m.csv"
        if p.exists():
            frames.append(load_candles_csv(p))
            sessions.append(d.isoformat())
    if not frames:
        raise SystemExit(f"No CSV under {HIST}")
    return pd.concat(frames, ignore_index=True), sessions


def _non_trade_sessions(df: pd.DataFrame, cfg: StrategyConfig, sessions: list[str]) -> list[str]:
    summary = run_backtest(run_replay_fast(df, cfg))
    traded = {t.session_date for t in summary.trades}
    return [s for s in sessions if s not in traded]


def _run_setup(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    session_filter: list[str],
    spec=None,
) -> dict:
    mask = df["session_date"].isin(session_filter)
    sub = df.loc[mask].reset_index(drop=True)
    enriched = _compute_indicators(sub, cfg)
    stack = build_indicator_stack(enriched)
    volumes = enriched["volume"].astype(float).tolist()
    logger = ReplayLogger()
    skipped = SkippedLogger()
    zone = ZoneInfo(cfg.timezone)
    research = ResearchConfig()
    state = ResearchState()
    current: str | None = None
    for i, row in enriched.iterrows():
        sd = str(row["session_date"])
        if sd != current:
            current = sd
            state = ResearchState(position=Position(), day=make_research_day(sd))
        bar = build_bar_context(
            int(i), row["timestamp"].to_pydatetime(), sd,
            float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
            float(row["volume"]),
            float(row["vwap"]) if pd.notna(row["vwap"]) else None,
            float(row["atr"]) if pd.notna(row["atr"]) else None,
            float(row["adx"]) if pd.notna(row["adx"]) else None,
            cfg, zone,
        )
        if spec is not None:
            process_indicator_setup_bar(
                state, bar, cfg, research, logger, SlippageModel("none"), skipped,
                spec=spec, stack=stack, volumes=volumes,
            )
        else:
            process_or_fake_break_stack(
                state, bar, cfg, research, logger, SlippageModel("none"), skipped,
            )

    summary = run_backtest(logger, session_dates=session_filter)
    return {
        "trades": summary.total_trades,
        "win_rate_pct": round(summary.win_rate_pct, 1),
        "total_pnl_pts": round(summary.total_pnl_pts, 1),
        "profit_factor": round(summary.profit_factor, 2) if summary.profit_factor else None,
        "max_dd_pts": round(summary.max_drawdown_pts, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--min-trades", type=int, default=8)
    parser.add_argument("--min-pf", type=float, default=1.35)
    args = parser.parse_args()

    cfg = DEFAULT_CONFIG
    df, sessions = _load_days(args.days)
    non_trade = _non_trade_sessions(df, cfg, sessions)
    prod = run_backtest(run_replay_fast(df, cfg))

    print(f"Sessions {len(sessions)} | v3.8: {prod.total_trades} trades, {prod.total_pnl_pts:+.0f} pts, PF {prod.profit_factor:.2f}")
    print(f"Non-trade (wide OR) days: {len(non_trade)}\n")
    print("Indicator stack sweep (3+ confirms each):\n")
    print(f"{'ID':>3}  {'Name':22}  {'Indicators':28}  Trd   WR%      P&L    PF")
    print("-" * 78)

    results: list[dict] = []
    for spec in INDICATOR_SETUPS:
        row = _run_setup(df, cfg, non_trade, spec)
        row.update({
            "setup_id": spec.setup_id,
            "name": spec.name,
            "indicators": spec.indicators,
        })
        results.append(row)
        pf = row["profit_factor"] or 0
        print(
            f"{spec.setup_id:>3}  {spec.name[:22]:22}  {spec.indicators[:28]:28}  "
            f"{row['trades']:3}  {row['win_rate_pct']:5.1f} {row['total_pnl_pts']:+8.1f}  {pf:5.2f}"
        )

    j_row = _run_setup(df, cfg, non_trade, None)
    j_row.update({"setup_id": "J", "name": "or_fake_break", "indicators": "OR trap+VWAP+ADX"})
    results.append(j_row)
    print(
        f"{'J':>3}  {'or_fake_break':22}  {'OR trap+VWAP+ADX':28}  "
        f"{j_row['trades']:3}  {j_row['win_rate_pct']:5.1f} {j_row['total_pnl_pts']:+8.1f}  "
        f"{(j_row['profit_factor'] or 0):5.2f}"
    )

    ranked = sorted(results, key=lambda r: (r["total_pnl_pts"], r["profit_factor"] or 0), reverse=True)
    viable = [r for r in ranked if r["trades"] >= args.min_trades and (r["profit_factor"] or 0) >= args.min_pf]

    print()
    if viable:
        print(f"Viable (≥{args.min_trades} trades, PF≥{args.min_pf}):")
        for r in viable[:5]:
            print(f"  {r['setup_id']} {r['name']}: {r['indicators']} → {r['total_pnl_pts']:+.0f} pts PF {r['profit_factor']}")
    else:
        print("No indicator stack setup cleared quality bar.")
        print("Top 3 by P&L:")
        for r in ranked[:3]:
            print(f"  {r['setup_id']} {r['name']}: {r['total_pnl_pts']:+.0f} pts PF {r['profit_factor']} ({r['trades']} tr)")

    out_path = OUT / f"indicator_stack_sweep_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps({"non_trade_days": len(non_trade), "results": ranked}, indent=2))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
