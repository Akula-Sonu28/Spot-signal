#!/usr/bin/env python3
"""Sweep Setup J robustness filters — target higher PF, fewer trades."""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from bot.backtest import run_backtest
from bot.config import DEFAULT_CONFIG
from bot.logger import ReplayLogger
from bot.replay import _compute_indicators, load_candles_csv
from bot.state import Position
from bot.strategy import build_bar_context
from research.backtests.j_trap_config import JTrapConfig, process_or_fake_break_filtered
from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.risk_engine import ResearchState, SkippedLogger, make_research_day
from research.backtests.options_setups_comparison.slippage import SlippageModel
from scripts.sweep_combined import _classify_sessions

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "historical"
OUT = ROOT / "data" / "research"


def _load_range(from_s: str, to_s: str) -> tuple[pd.DataFrame, list[str]]:
    from_d = date.fromisoformat(from_s)
    to_d = date.fromisoformat(to_s)
    frames, sessions = [], []
    d = from_d
    while d <= to_d:
        if d.weekday() < 5:
            p = HIST / f"{d.isoformat()}_5m.csv"
            if p.exists():
                frames.append(load_candles_csv(p))
                sessions.append(d.isoformat())
        d += timedelta(days=1)
    return pd.concat(frames, ignore_index=True), sessions


def _run_j(df: pd.DataFrame, sessions: list[str], wide_or: list[str], jcfg: JTrapConfig) -> dict:
    from zoneinfo import ZoneInfo

    cfg = DEFAULT_CONFIG
    mask = df["session_date"].isin(wide_or)
    sub = df.loc[mask].reset_index(drop=True)
    enriched = _compute_indicators(sub, cfg)
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
        process_or_fake_break_filtered(
            state, bar, cfg, research, logger, SlippageModel("none"), skipped, jcfg=jcfg,
        )
    s = run_backtest(logger, session_dates=wide_or)
    return {
        "trades": s.total_trades,
        "win_rate_pct": round(s.win_rate_pct, 1),
        "total_pnl_pts": round(s.total_pnl_pts, 1),
        "profit_factor": round(s.profit_factor, 2) if s.profit_factor else None,
        "max_dd_pts": round(s.max_drawdown_pts, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", default="2026-03-02")
    parser.add_argument("--to", dest="to_date", default="2026-06-12")
    args = parser.parse_args()

    cfg = DEFAULT_CONFIG
    df, sessions = _load_range(args.from_date, args.to_date)
    tags = _classify_sessions(df, cfg)
    wide_or = [s for s in sessions if tags.get(s) == "wide_or"]

    base = JTrapConfig()
    base_row = _run_j(df, sessions, wide_or, base)
    print(f"Range {sessions[0]} → {sessions[-1]} | wide_or={len(wide_or)}")
    print(f"J base: {base_row}\n")

    grid = {
        "min_trap_excess_pts": [0, 10, 15, 20],
        "min_reclaim_pts": [0, 5, 8, 12],
        "min_vwap_dist_pts": [0, 8, 12, 20],
        "min_body_ratio": [0, 0.35, 0.45, 0.55],
        "min_adx": [None, 20, 22, 25],
        "skip_both_trapped": [False, True],
        "min_minutes_after_or": [0, 15, 30],
        "max_trades_day": [2, 1],
        "one_trap_side_only": [False, True],
    }

    # Stage 1: single-filter lifts from base
    singles: list[dict] = []
    for key, values in grid.items():
        for val in values:
            if val == grid[key][0]:
                continue
            kw = {k: grid[k][0] for k in grid}
            kw[key] = val
            jcfg = JTrapConfig(**kw)
            row = _run_j(df, sessions, wide_or, jcfg)
            row["tweak"] = f"{key}={val}"
            singles.append(row)

    singles.sort(key=lambda r: (r["profit_factor"] or 0, r["total_pnl_pts"]), reverse=True)
    print("Top single-filter tweaks:")
    for r in singles[:12]:
        pf = r["profit_factor"] or 0
        print(f"  {r['tweak']:40} tr={r['trades']:2} P&L={r['total_pnl_pts']:+7.1f} PF={pf:.2f}")

    # Stage 2: stack best practical combo (hand-picked from literature + stage 1)
    combos = [
        ("robust_v1", JTrapConfig(
            min_trap_excess_pts=15, min_reclaim_pts=8, min_vwap_dist_pts=12,
            min_body_ratio=0.45, min_adx=22, skip_both_trapped=True,
            min_minutes_after_or=30, max_trades_day=1, max_losses_day=1,
            one_trap_side_only=True,
        )),
        ("robust_v2", JTrapConfig(
            min_trap_excess_pts=20, min_reclaim_pts=10, min_vwap_dist_pts=15,
            min_body_ratio=0.5, min_adx=22, skip_both_trapped=True,
            min_minutes_after_or=15, max_trades_day=1, max_losses_day=1,
            one_trap_side_only=True,
        )),
        ("robust_v3", JTrapConfig(
            min_trap_excess_pts=15, min_reclaim_pts=8, min_vwap_dist_pts=8,
            min_body_ratio=0.4, min_adx=20, skip_both_trapped=True,
            min_minutes_after_or=30, max_trades_day=1, max_losses_day=1,
            one_trap_side_only=False,
        )),
        ("quality_only", JTrapConfig(
            min_trap_excess_pts=20, min_reclaim_pts=12, min_vwap_dist_pts=20,
            min_body_ratio=0.55, min_adx=25, skip_both_trapped=True,
            min_minutes_after_or=30, max_trades_day=1, max_losses_day=1,
            one_trap_side_only=True,
        )),
    ]

    print("\nPreset combos:")
    results = [{"name": "base", **base_row}]
    for name, jcfg in combos:
        row = _run_j(df, sessions, wide_or, jcfg)
        row["name"] = name
        results.append(row)
        pf = row["profit_factor"] or 0
        print(f"  {name:14} tr={row['trades']:2} WR={row['win_rate_pct']:5.1f}% "
              f"P&L={row['total_pnl_pts']:+7.1f} PF={pf:.2f} DD={row['max_dd_pts']:.1f}")

    # Full history validation for top preset
    df_full, sess_full = _load_range("2025-05-02", "2026-06-12")
    tags_f = _classify_sessions(df_full, cfg)
    wide_f = [s for s in sess_full if tags_f.get(s) == "wide_or"]
    print(f"\nFull history {sess_full[0]} → {sess_full[-1]} ({len(sess_full)} sessions)")
    for name, jcfg in [("base", base), *combos]:
        row = _run_j(df_full, sess_full, wide_f, jcfg)
        pf = row["profit_factor"] or 0
        print(f"  {name:14} tr={row['trades']:3} P&L={row['total_pnl_pts']:+8.1f} PF={pf:.2f}")

    out = OUT / f"j_robustness_{sessions[0]}_{sessions[-1]}.json"
    out.write_text(json.dumps({"base": base_row, "singles_top": singles[:20], "combos": results}, indent=2))
    print(f"\nWrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
