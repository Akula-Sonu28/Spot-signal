#!/usr/bin/env python3
"""Multi-confirmation alternative setups on production non-trade days."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from bot.backtest import run_backtest
from bot.config import DEFAULT_CONFIG, StrategyConfig
from bot.historical_data import trading_days_back
from bot.indicators import or_width
from bot.logger import ReplayLogger
from bot.replay import _compute_indicators, load_candles_csv, run_replay_fast
from bot.state import Position
from bot.strategy import build_bar_context
from research.backtests.non_trade_setups import (
    MULTI_SETUPS,
    process_gap_fade_stack,
    process_gap_go_stack,
    process_or_fake_break_stack,
    process_orb_ema_volume_stack,
    process_vwap_extension_fade,
    process_vwap_reclaim_volume_stack,
)
from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.indicators_ext import (
    ema_series,
    prior_session_closes,
    rsi_series,
    volume_ratio,
)
from research.backtests.options_setups_comparison.replay_engine import run_setup_replay
from research.backtests.options_setups_comparison.risk_engine import (
    ResearchState,
    SkippedLogger,
    make_research_day,
)
from research.backtests.options_setups_comparison.setups import SETUP_REGISTRY
from research.backtests.options_setups_comparison.slippage import SlippageModel

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "historical"
OUT = ROOT / "data" / "research"

PROCESSORS = {
    "J": process_or_fake_break_stack,
    "K": process_gap_fade_stack,
    "L": process_gap_go_stack,
    "M": process_vwap_extension_fade,
    "N": process_orb_ema_volume_stack,
    "O": process_vwap_reclaim_volume_stack,
}


def _load_days(days: int) -> tuple[pd.DataFrame, list[str]]:
    day_list = trading_days_back(date.today(), days)
    frames, sessions = [], []
    for d in day_list:
        path = HIST / f"{d.isoformat()}_5m.csv"
        if path.exists():
            frames.append(load_candles_csv(path))
            sessions.append(d.isoformat())
    if not frames:
        raise SystemExit(f"No CSV under {HIST}")
    return pd.concat(frames, ignore_index=True), sessions


def _session_or_width(sdf: pd.DataFrame, cfg: StrategyConfig) -> float | None:
    h, l = None, None
    for _, row in sdf.iterrows():
        bar = build_bar_context(
            0, row["timestamp"].to_pydatetime(), str(row["session_date"]),
            float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
            float(row["volume"]), None, None, None, cfg,
        )
        if bar.in_or:
            h = bar.high if h is None else max(h, bar.high)
            l = bar.low if l is None else min(l, bar.low)
    return or_width(h, l)


def _prod_trades_by_session(df: pd.DataFrame, cfg: StrategyConfig) -> dict[str, int]:
    summary = run_backtest(run_replay_fast(df, cfg))
    out: dict[str, int] = defaultdict(int)
    for t in summary.trades:
        out[t.session_date] += 1
    return dict(out)


def run_multi_replay(
    df: pd.DataFrame,
    setup_id: str,
    cfg: StrategyConfig,
) -> tuple[ReplayLogger, SkippedLogger]:
    proc = PROCESSORS[setup_id]
    enriched = _compute_indicators(df, cfg)
    logger = ReplayLogger()
    skipped = SkippedLogger()
    zone = ZoneInfo(cfg.timezone)
    research = ResearchConfig()

    closes = enriched["close"].astype(float).tolist()
    highs = enriched["high"].astype(float).tolist()
    lows = enriched["low"].astype(float).tolist()
    volumes = enriched["volume"].astype(float).tolist()
    ema9 = ema_series(closes, 9)
    ema20 = ema_series(closes, 20)
    rsis = rsi_series(closes, 14)
    session_dates = enriched["session_date"].astype(str).tolist()
    prior_closes = prior_session_closes(enriched, session_dates)

    state = ResearchState()
    current: str | None = None

    for i, row in enriched.iterrows():
        sd = str(row["session_date"])
        if sd != current:
            current = sd
            state = ResearchState(
                position=Position(),
                day=make_research_day(sd),
                prior_close=prior_closes.get(sd),
            )

        bar = build_bar_context(
            int(i), row["timestamp"].to_pydatetime(), sd,
            float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]),
            float(row["volume"]),
            float(row["vwap"]) if pd.notna(row["vwap"]) else None,
            float(row["atr"]) if pd.notna(row["atr"]) else None,
            float(row["adx"]) if pd.notna(row["adx"]) else None,
            cfg, zone,
        )
        idx = int(i)
        kwargs: dict = {}
        if setup_id in ("K", "M"):
            kwargs["rsi"] = rsis[idx]
        if setup_id in ("L", "O"):
            kwargs["ema9"] = ema9[idx]
            kwargs["ema20"] = ema20[idx]
        if setup_id in ("N", "O"):
            kwargs["ema20"] = ema20[idx]
            kwargs["vol_ratio"] = volume_ratio(volumes, idx)
        if setup_id == "O":
            kwargs["highs"] = highs
            kwargs["lows"] = lows

        proc(state, bar, cfg, research, logger, SlippageModel("none"), skipped, **kwargs)

    return logger, skipped


def _run_one(df: pd.DataFrame, sid: str, name: str, cfg: StrategyConfig, sessions: list[str]) -> dict:
    if sid in PROCESSORS:
        logger, skipped = run_multi_replay(df, sid, cfg)
    else:
        _, proc = SETUP_REGISTRY[sid]
        logger, skipped = run_setup_replay(df, sid, proc, cfg, ResearchConfig(), SlippageModel("none"))
    summary = run_backtest(logger, session_dates=sessions)
    confirms = MULTI_SETUPS.get(sid, ("", ""))[1]
    return {
        "setup_id": sid,
        "setup_name": name,
        "confirmations": confirms,
        "trades": summary.total_trades,
        "win_rate_pct": round(summary.win_rate_pct, 1),
        "total_pnl_pts": round(summary.total_pnl_pts, 1),
        "profit_factor": round(summary.profit_factor, 2) if summary.profit_factor else None,
        "max_dd_pts": round(summary.max_drawdown_pts, 1),
        "skipped": len(skipped.entries),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=60)
    args = parser.parse_args()

    cfg = DEFAULT_CONFIG
    df, all_sessions = _load_days(args.days)
    prod = _prod_trades_by_session(df, cfg)
    non_trade = [s for s in all_sessions if prod.get(s, 0) == 0]
    classify = Counter()
    for s in non_trade:
        w = _session_or_width(df[df["session_date"] == s], cfg)
        if w is None:
            classify["no_or"] += 1
        elif w > cfg.max_or_range:
            classify["or_wide"] += 1
        elif w < cfg.min_or_range:
            classify["or_narrow"] += 1
        else:
            classify["or_ok_no_signal"] += 1

    print(f"Sessions {len(all_sessions)} | v3.8 trades {sum(prod.values())} | Non-trade {len(non_trade)}")
    print("Skip reasons:", dict(classify), "\n")

    sub = df[df["session_date"].isin(non_trade)].reset_index(drop=True)
    results: list[dict] = []

    for sid, (key, desc) in MULTI_SETUPS.items():
        results.append(_run_one(sub, sid, key, cfg, non_trade))

    for sid in ("D", "G", "F", "H"):
        if sid in SETUP_REGISTRY:
            name, _ = SETUP_REGISTRY[sid]
            results.append(_run_one(sub, sid, name, cfg, non_trade))

    ranked = sorted(results, key=lambda r: (r["total_pnl_pts"], r["profit_factor"] or 0), reverse=True)
    print("Multi-confirmation sweep (non-trade days only):\n")
    print(f"{'ID':>2}  {'Trd':>3}  {'WR%':>5}  {'P&L':>8}  {'PF':>5}  Confirmations")
    print("-" * 72)
    for r in ranked:
        pf = r["profit_factor"] or 0
        conf = r.get("confirmations", "")[:42]
        print(
            f"{r['setup_id']:>2}  {r['trades']:3}  {r['win_rate_pct']:5.1f} "
            f"{r['total_pnl_pts']:+8.1f}  {pf:5.2f}  {conf}"
        )

    viable = [r for r in ranked if r["trades"] >= 5 and (r["profit_factor"] or 0) >= 1.3]
    print()
    if viable:
        print("Viable (≥5 trades, PF≥1.3):")
        for r in viable:
            print(f"  {r['setup_id']}: {r['setup_name']} — {r['total_pnl_pts']:+.0f} pts, PF {r['profit_factor']}")
    else:
        print("No multi-confirm setup cleared PF≥1.3 with ≥5 trades.")

    out_path = OUT / f"non_trade_multi_{date.today().isoformat()}.json"
    out_path.write_text(json.dumps({"results": ranked, "non_trade_days": len(non_trade)}, indent=2))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
