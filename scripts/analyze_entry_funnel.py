#!/usr/bin/env python3
"""Diagnose v3.8 entry filter funnel on V38 days; combined router for trade counts."""

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

from bot.combined import process_session_bar
from bot.config import StrategyConfig, load_combined_config
from bot.day_router import DayMode, ensure_day_mode
from bot.historical_data import trading_days_back
from bot.indicators import or_width
from bot.replay import load_candles_csv, run_replay_fast, _compute_indicators
from bot.state import DayState, Position, PositionSide, ReplayState, make_day_state, reset_position
from bot.logger import ReplayLogger
from bot.strategy import build_bar_context, _calc_stops, _calc_target

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "historical"
OUT = ROOT / "data" / "research"


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


def _analyze_session(
    session_df: pd.DataFrame,
    cfg: StrategyConfig,
    global_index_start: int,
) -> dict:
    """Count raw breakouts vs each filter rejection (first-hit per bar per side)."""
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(cfg.timezone)
    enriched = _compute_indicators(session_df.reset_index(drop=True), cfg)
    day = make_day_state(str(enriched["session_date"].iloc[0]))
    position = Position()
    state = ReplayState(day=day, position=position, bar_index=global_index_start)

    counts = Counter()
    session_date = day.session_date
    combined = load_combined_config(cfg)
    logger = ReplayLogger()
    session_day_mode: str | None = None

    # Per-day flags for funnel
    had_or = False
    raw_long_bars = 0
    raw_short_bars = 0
    width: float | None = None
    final_or_width: float | None = None

    for local_i, row in enriched.iterrows():
        bar = build_bar_context(
            index=global_index_start + int(local_i),
            timestamp=row["timestamp"].to_pydatetime(),
            session_date=session_date,
            o=float(row["open"]),
            h=float(row["high"]),
            l=float(row["low"]),
            c=float(row["close"]),
            vol=float(row["volume"]),
            vwap=float(row["vwap"]) if pd.notna(row["vwap"]) else None,
            atr=float(row["atr"]) if pd.notna(row["atr"]) else None,
            adx=float(row["adx"]) if pd.notna(row["adx"]) else None,
            cfg=cfg,
            tz=zone,
        )

        # Mirror update_or
        if bar.in_or:
            day.or_bars_seen += 1
            day.or_high = bar.high if day.or_high is None else max(day.or_high, bar.high)
            day.or_low = bar.low if day.or_low is None else min(day.or_low, bar.low)
        if bar.is_after_or and day.or_high is not None and day.or_low is not None:
            day.or_defined = True
            had_or = True

        width = or_width(day.or_high, day.or_low)
        if day.or_defined and width is not None:
            final_or_width = width
        or_width_ok = width is not None and cfg.min_or_range <= width <= cfg.max_or_range

        mode = ensure_day_mode(day, cfg)
        if mode is not None:
            session_day_mode = mode.value

        if not bar.is_entry_window or not day.or_defined:
            process_session_bar(state, bar, logger, combined)
            continue

        if mode == DayMode.V38:
            long_break = bar.close > (day.or_high or float("inf"))
            short_break = bar.close < (day.or_low or float("-inf"))

            if long_break:
                raw_long_bars += 1
                _tally_long_filter(counts, bar, day, cfg, or_width_ok, position)
            if short_break:
                raw_short_bars += 1
                _tally_short_filter(counts, bar, day, cfg, or_width_ok, position)

        process_session_bar(state, bar, logger, combined)

    fw = final_or_width
    or_ok = fw is not None and cfg.min_or_range <= fw <= cfg.max_or_range
    return {
        "session": session_date,
        "had_or": had_or,
        "or_width_ok": or_ok,
        "or_width_pts": round(fw, 1) if fw is not None else None,
        "raw_long_bars": raw_long_bars,
        "raw_short_bars": raw_short_bars,
        "trades": day.trades_today,
        "day_mode": session_day_mode,
        "filter_counts": dict(counts),
    }


def _tally_long_filter(
    counts: Counter,
    bar,
    day: DayState,
    cfg: StrategyConfig,
    or_width_ok: bool,
    position: Position,
) -> None:
    counts["raw_long_breakout"] += 1
    if not or_width_ok:
        w = or_width(day.or_high, day.or_low)
        if w is not None and w < cfg.min_or_range:
            counts["blocked_or_narrow"] += 1
        elif w is not None and w > cfg.max_or_range:
            counts["blocked_or_wide"] += 1
        else:
            counts["blocked_or_width"] += 1
        return
    if bar.adx is None or bar.adx < cfg.adx_min:
        counts["blocked_adx"] += 1
        return
    if cfg.use_vwap_filter and (bar.vwap is None or bar.close <= bar.vwap):
        counts["blocked_vwap"] += 1
        return
    if position.side != PositionSide.FLAT:
        counts["blocked_in_position"] += 1
        return
    if day.fired_long_today:
        counts["blocked_already_fired_ce"] += 1
        return
    if day.trades_today >= cfg.max_trades_per_day:
        counts["blocked_max_trades"] += 1
        return
    if bar.atr is None or day.or_high is None or day.or_low is None:
        counts["blocked_missing_atr"] += 1
        return
    stop, target = _calc_stops(PositionSide.CE, bar.close, bar.atr, day.or_high, day.or_low, cfg)
    if stop is not None and bar.close <= stop:
        counts["blocked_bad_stop_geometry"] += 1
        return
    if _calc_target(PositionSide.CE, bar.close, bar.atr, cfg) <= bar.close:
        counts["blocked_bad_target_geometry"] += 1
        return
    counts["would_enter_ce"] += 1


def _tally_short_filter(
    counts: Counter,
    bar,
    day: DayState,
    cfg: StrategyConfig,
    or_width_ok: bool,
    position: Position,
) -> None:
    counts["raw_short_breakout"] += 1
    if not or_width_ok:
        w = or_width(day.or_high, day.or_low)
        if w is not None and w < cfg.min_or_range:
            counts["blocked_or_narrow"] += 1
        elif w is not None and w > cfg.max_or_range:
            counts["blocked_or_wide"] += 1
        else:
            counts["blocked_or_width"] += 1
        return
    if bar.adx is None or bar.adx < cfg.adx_min:
        counts["blocked_adx"] += 1
        return
    if cfg.use_vwap_filter and (bar.vwap is None or bar.close >= bar.vwap):
        counts["blocked_vwap"] += 1
        return
    if position.side != PositionSide.FLAT:
        counts["blocked_in_position"] += 1
        return
    if day.fired_short_today:
        counts["blocked_already_fired_pe"] += 1
        return
    if day.trades_today >= cfg.max_trades_per_day:
        counts["blocked_max_trades"] += 1
        return
    if bar.atr is None or day.or_high is None or day.or_low is None:
        counts["blocked_missing_atr"] += 1
        return
    stop, target = _calc_stops(PositionSide.PE, bar.close, bar.atr, day.or_high, day.or_low, cfg)
    if stop is not None and bar.close >= stop:
        counts["blocked_bad_stop_geometry"] += 1
        return
    if _calc_target(PositionSide.PE, bar.close, bar.atr, cfg) >= bar.close:
        counts["blocked_bad_target_geometry"] += 1
        return
    counts["would_enter_pe"] += 1


def run_funnel(df: pd.DataFrame, sessions: list[str], cfg: StrategyConfig) -> dict:
    per_session: list[dict] = []
    totals = Counter()
    trades_per_day = Counter()

    idx = 0
    for session in sessions:
        sdf = df[df["session_date"] == session].copy()
        if sdf.empty:
            continue
        result = _analyze_session(sdf, cfg, idx)
        per_session.append(result)
        trades_per_day[result["trades"]] += 1
        for k, v in result["filter_counts"].items():
            totals[k] += v
        idx += len(sdf)

    days_with_trade = sum(1 for r in per_session if r["trades"] > 0)
    days_no_or_break = sum(
        1 for r in per_session if r["raw_long_bars"] == 0 and r["raw_short_bars"] == 0 and r["had_or"]
    )
    days_or_width_fail = sum(1 for r in per_session if r["had_or"] and not r["or_width_ok"])
    mode_counts = Counter(r.get("day_mode") or "UNKNOWN" for r in per_session)

    return {
        "sessions": len(per_session),
        "day_mode_counts": dict(mode_counts),
        "days_with_trades": days_with_trade,
        "days_no_breakout": days_no_or_break,
        "days_or_width_fail_all_day": days_or_width_fail,
        "trades_per_day_dist": dict(trades_per_day),
        "filter_totals": dict(totals),
        "per_session": per_session,
    }


def _print_report(report: dict, cfg: StrategyConfig) -> None:
    n = report["sessions"]
    print(f"Entry funnel — {n} sessions (v3.8 tallies on V38 days only; trades via v3.9 router)")
    modes = report.get("day_mode_counts", {})
    if modes:
        print(f"  Day modes: {', '.join(f'{k}={v}' for k, v in sorted(modes.items()))}")
    print(f"  Days with ≥1 trade:     {report['days_with_trades']} ({100*report['days_with_trades']/n:.0f}%)")
    print(f"  Days with 0 OR break:   {report['days_no_breakout']} ({100*report['days_no_breakout']/n:.0f}%)")
    print(f"  Days OR width invalid:  {report['days_or_width_fail_all_day']} (whole day)")
    print(f"  Trades/day distribution: {report['trades_per_day_dist']}")
    print()
    print("Filter blocks (bar-level, both sides):")
    ft = report["filter_totals"]
    order = [
        "raw_long_breakout",
        "raw_short_breakout",
        "blocked_or_narrow",
        "blocked_or_wide",
        "blocked_adx",
        "blocked_vwap",
        "blocked_in_position",
        "blocked_already_fired_ce",
        "blocked_already_fired_pe",
        "blocked_max_trades",
        "blocked_bad_stop_geometry",
        "blocked_bad_target_geometry",
        "would_enter_ce",
        "would_enter_pe",
    ]
    raw = ft.get("raw_long_breakout", 0) + ft.get("raw_short_breakout", 0)
    for key in order:
        if key in ft:
            pct = 100 * ft[key] / raw if raw and key.startswith("blocked") else ""
            suffix = f"  ({pct:.0f}% of raw breaks)" if pct else ""
            print(f"  {key:32} {ft[key]:5}{suffix}")
    print()
    print(f"Config: adx>={cfg.adx_min} or={cfg.min_or_range}-{cfg.max_or_range} "
          f"vwap={cfg.use_vwap_filter} max_trades={cfg.max_trades_per_day}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Entry filter funnel analysis")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--out", type=Path, help="Write JSON report")
    args = parser.parse_args()

    df, sessions = _load_days(args.days)
    if "session_date" not in df.columns:
        df["session_date"] = df["timestamp"].dt.strftime("%Y-%m-%d")

    cfg = StrategyConfig(
        sl_mode="OR_RANGE",
        close_only_sl=True,
        sl_buffer_pts=10.0,
    )
    report = run_funnel(df, sessions, cfg)
    _print_report(report, cfg)

    # Sessions with breakouts but zero trades
    missed = [
        r for r in report["per_session"]
        if r["trades"] == 0 and (r["raw_long_bars"] > 0 or r["raw_short_bars"] > 0)
    ]
    if missed:
        print(f"\n{len(missed)} days had OR breaks but 0 trades (top blockers):")
        for r in missed[:12]:
            fc = r["filter_counts"]
            top = max(
                (("adx", fc.get("blocked_adx", 0)),
                 ("vwap", fc.get("blocked_vwap", 0)),
                 ("or_narrow", fc.get("blocked_or_narrow", 0)),
                 ("or_wide", fc.get("blocked_or_wide", 0)),
                 ("in_pos", fc.get("blocked_in_position", 0))),
                key=lambda x: x[1],
            )
            print(
                f"  {r['session']} OR={r['or_width_pts']}pts "
                f"L={r['raw_long_bars']} S={r['raw_short_bars']} → top block: {top[0]}({top[1]})"
            )

    out_path = args.out or OUT / f"entry_funnel_{date.today().isoformat()}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    slim = {k: v for k, v in report.items() if k != "per_session"}
    slim["missed_days"] = len(missed)
    out_path.write_text(json.dumps(slim, indent=2))
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
