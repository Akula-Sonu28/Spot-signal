#!/usr/bin/env python3
"""Unified backtest: v3.8 on all days + Phase-2 setups on wide-OR days (same window)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from bot.backtest import pair_trades, run_backtest, summarize_trades
from bot.config import DEFAULT_CONFIG, StrategyConfig
from bot.historical_data import trading_days_back
from bot.indicators import or_width
from bot.logger import ReplayLogger
from bot.replay import load_candles_csv, run_replay_fast
from research.backtests.indicator_setups import INDICATOR_SETUPS
from scripts.sweep_indicator_stack import _run_setup

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "historical"
OUT = ROOT / "data" / "research"

PHASE2_CANDIDATES = ("07", "04", "01", "J")


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


def _classify_sessions(df: pd.DataFrame, cfg: StrategyConfig) -> dict[str, str]:
    """Tag each session: wide_or | valid_or | narrow_or | no_or."""
    from bot.replay import _compute_indicators
    from bot.state import Position, ReplayState, make_day_state, reset_position
    from bot.strategy import build_bar_context, update_or
    from zoneinfo import ZoneInfo

    enriched = _compute_indicators(df, cfg)
    zone = ZoneInfo(cfg.timezone)
    tags: dict[str, str] = {}
    current: str | None = None
    state = ReplayState(position=Position(), day=None)
    final_width: float | None = None

    for i, row in enriched.iterrows():
        sd = str(row["session_date"])
        if sd != current:
            if current is not None:
                tags[current] = _or_tag(final_width, cfg)
            current = sd
            state = ReplayState(position=Position(), day=make_day_state(sd))
            final_width = None

        bar = build_bar_context(
            int(i),
            row["timestamp"].to_pydatetime(),
            sd,
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row["volume"]),
            float(row["vwap"]) if pd.notna(row["vwap"]) else None,
            float(row["atr"]) if pd.notna(row["atr"]) else None,
            float(row["adx"]) if pd.notna(row["adx"]) else None,
            cfg,
            zone,
        )
        update_or(state.day, bar, cfg)
        if state.day and state.day.or_defined:
            w = or_width(state.day.or_high, state.day.or_low)
            if w is not None:
                final_width = w

    if current is not None:
        tags[current] = _or_tag(final_width, cfg)
    return tags


def _or_tag(width: float | None, cfg: StrategyConfig) -> str:
    if width is None:
        return "no_or"
    if width > cfg.max_or_range:
        return "wide_or"
    if width < cfg.min_or_range:
        return "narrow_or"
    return "valid_or"


def _combine(v38_logger: ReplayLogger, phase2_logger: ReplayLogger, sessions: list[str]):
    v38_events = [e for e in v38_logger.events if e.event_type in {"BUY_CE", "BUY_PE", "TARGET_CE", "TARGET_PE", "SL_CE", "SL_PE", "SQUARE_OFF"}]
    p2_events = [e for e in phase2_logger.events if e.event_type in {"BUY_CE", "BUY_PE", "TARGET_CE", "TARGET_PE", "SL_CE", "SL_PE", "SQUARE_OFF"}]
    all_events = sorted(v38_events + p2_events, key=lambda e: e.timestamp)
    trades = pair_trades(all_events)
    return summarize_trades(trades, sessions=len(sessions))


def _fmt(s) -> str:
    pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "—"
    return f"trades={s.total_trades:3}  WR={s.win_rate_pct:5.1f}%  P&L={s.total_pnl_pts:+8.1f}  PF={pf}  DD={s.max_drawdown_pts:.1f}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=300, help="Weekdays back (loads CSVs that exist)")
    args = parser.parse_args()

    cfg = DEFAULT_CONFIG
    df, sessions = _load_days(args.days)
    tags = _classify_sessions(df, cfg)
    wide_or = [s for s in sessions if tags.get(s) == "wide_or"]
    valid_or = [s for s in sessions if tags.get(s) == "valid_or"]
    v38_traded = {t.session_date for t in run_backtest(run_replay_fast(df, cfg)).trades}

    print(f"Window: {sessions[0]} → {sessions[-1]}  ({len(sessions)} sessions)\n")
    print("Day classification:")
    print(f"  wide_or (>100):   {len(wide_or)}")
    print(f"  valid_or (25-100): {len(valid_or)}")
    print(f"  narrow_or (<25):  {sum(1 for s in sessions if tags.get(s) == 'narrow_or')}")
    print(f"  no_or:            {sum(1 for s in sessions if tags.get(s) == 'no_or')}")
    print(f"  v3.8 traded days: {len(v38_traded)}")
    print()

    v38 = run_backtest(run_replay_fast(df, cfg))
    print(f"v3.8 alone (all days):     {_fmt(v38)}")

    by_id = {s.setup_id: s for s in INDICATOR_SETUPS}
    phase2_rows: list[dict] = []

    for sid in PHASE2_CANDIDATES:
        if sid == "J":
            row = _run_setup(df, cfg, wide_or, None)
            row["setup_id"] = "J"
            row["name"] = "or_fake_break"
        else:
            spec = by_id[sid]
            row = _run_setup(df, cfg, wide_or, spec)
            row["setup_id"] = sid
            row["name"] = spec.name
        phase2_rows.append(row)
        print(f"Phase2 {sid} (wide_or only): {_fmt_dict(row)}")

    best = max(phase2_rows, key=lambda r: (r["total_pnl_pts"], r["profit_factor"] or 0))
    print(f"\nBest Phase-2 on wide_or: {best['setup_id']} {best['name']}")

    # Combined: v3.8 events + best phase2 on wide_or (disjoint by OR width)
    best_spec = None if best["setup_id"] == "J" else by_id[best["setup_id"]]
    p2_logger = ReplayLogger()
    _run_setup_logger(df, cfg, wide_or, best_spec, p2_logger)
    combined = _combine(run_replay_fast(df, cfg), p2_logger, sessions)
    uplift = combined.total_pnl_pts - v38.total_pnl_pts
    print(f"\nCombined v3.8 + {best['setup_id']}:  {_fmt(combined)}  (uplift {uplift:+.1f} pts)")

    # 51-day subset for comparison with prior sweeps
    recent = sessions[-51:] if len(sessions) >= 51 else sessions
    recent_set = set(recent)
    recent_wide = [s for s in wide_or if s in recent_set]
    v38_logger = run_replay_fast(df, cfg)
    v38_recent = run_backtest(v38_logger, session_dates=recent)
    p2_recent_logger = ReplayLogger()
    _run_setup_logger(df, cfg, recent_wide, best_spec, p2_recent_logger)
    v38_recent_logger = ReplayLogger()
    allowed = set(recent)
    for e in v38_logger.events:
        if e.timestamp.strftime("%Y-%m-%d") in allowed:
            v38_recent_logger.events.append(e)
    combined_recent = _combine(v38_recent_logger, p2_recent_logger, recent)
    print(f"\n--- Same metrics on last {len(recent)} sessions (prior sweep window) ---")
    print(f"v3.8 alone:                {_fmt(v38_recent)}")
    best_recent = _run_setup(df, cfg, recent_wide, best_spec if best["setup_id"] != "J" else None)
    print(f"Phase2 {best['setup_id']} wide_or:     {_fmt_dict(best_recent)}")
    print(f"Combined:                  {_fmt(combined_recent)}")

    payload = {
        "sessions": len(sessions),
        "from": sessions[0],
        "to": sessions[-1],
        "classification": {
            "wide_or": len(wide_or),
            "valid_or": len(valid_or),
            "v38_traded_days": len(v38_traded),
        },
        "v38": _summary_dict(v38),
        "phase2_on_wide_or": phase2_rows,
        "best_phase2": best["setup_id"],
        "combined": _summary_dict(combined),
        "recent_51": {
            "v38": _summary_dict(v38_recent),
            "combined": _summary_dict(combined_recent),
        },
    }
    out_path = OUT / f"combined_sweep_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


def _fmt_dict(row: dict) -> str:
    pf = f"{row['profit_factor']:.2f}" if row.get("profit_factor") else "—"
    return (
        f"trades={row['trades']:3}  WR={row['win_rate_pct']:5.1f}%  "
        f"P&L={row['total_pnl_pts']:+8.1f}  PF={pf}  DD={row['max_dd_pts']:.1f}"
    )


def _summary_dict(s) -> dict:
    return {
        "trades": s.total_trades,
        "win_rate_pct": round(s.win_rate_pct, 1),
        "total_pnl_pts": round(s.total_pnl_pts, 1),
        "profit_factor": round(s.profit_factor, 2) if s.profit_factor else None,
        "max_dd_pts": round(s.max_drawdown_pts, 1),
    }


def _run_setup_logger(df, cfg, session_filter, spec, logger: ReplayLogger) -> None:
    """Run setup writing to provided logger (for combine)."""
    from zoneinfo import ZoneInfo

    from bot.replay import _compute_indicators
    from bot.state import Position
    from bot.strategy import build_bar_context
    from research.backtests.indicator_stack import build_indicator_stack
    from research.backtests.indicator_setups import process_indicator_setup_bar
    from research.backtests.non_trade_setups import process_or_fake_break_stack
    from research.backtests.options_setups_comparison.config import ResearchConfig
    from research.backtests.options_setups_comparison.risk_engine import ResearchState, SkippedLogger, make_research_day
    from research.backtests.options_setups_comparison.slippage import SlippageModel

    mask = df["session_date"].isin(session_filter)
    sub = df.loc[mask].reset_index(drop=True)
    enriched = _compute_indicators(sub, cfg)
    stack = build_indicator_stack(enriched)
    volumes = enriched["volume"].astype(float).tolist()
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
            int(i),
            row["timestamp"].to_pydatetime(),
            sd,
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row["volume"]),
            float(row["vwap"]) if pd.notna(row["vwap"]) else None,
            float(row["atr"]) if pd.notna(row["atr"]) else None,
            float(row["adx"]) if pd.notna(row["adx"]) else None,
            cfg,
            zone,
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


if __name__ == "__main__":
    raise SystemExit(main())
