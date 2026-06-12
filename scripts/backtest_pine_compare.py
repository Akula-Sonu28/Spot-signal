#!/usr/bin/env python3
"""Backtest v3.9 combined (v3.8 + J+) on a date range — trade list for Pine visual compare."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from bot.backtest import ENTRY_TYPES, EXIT_TYPES, pair_trades, summarize_trades
from bot.config import DEFAULT_CONFIG, CombinedStrategyConfig, StrategyConfig
from bot.logger import ReplayLogger, SignalEvent
from bot.replay import load_candles_csv, run_replay_fast
from bot.strategy_j import J_TRAP_ROBUST, JTrapConfig
from scripts.sweep_combined import _classify_sessions

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "historical"
OUT = ROOT / "data" / "research"


def _load_range(from_date: date, to_date: date) -> tuple[pd.DataFrame, list[str], list[str]]:
    frames, sessions = [], []
    d = from_date
    missing: list[str] = []
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


def _trade_rows(trades, *, strategy: str) -> list[dict]:
    rows = []
    for t in trades:
        rows.append({
            "strategy": strategy,
            "session_date": t.session_date,
            "side": t.side,
            "entry_time": t.entry_time.strftime("%H:%M"),
            "exit_time": t.exit_time.strftime("%H:%M"),
            "entry_price": round(t.entry_price, 2),
            "exit_price": round(t.exit_price, 2),
            "stop": round(t.stop, 2),
            "target": round(t.target, 2),
            "pnl_pts": round(t.pnl_pts, 2),
            "exit_reason": t.exit_reason,
        })
    return rows


def _events_for_strategy(events: list[SignalEvent], strategy: str) -> list[SignalEvent]:
    """Keep entry/exit chains whose BUY event matches strategy tag."""
    filtered: list[SignalEvent] = []
    active = False
    for event in events:
        et = event.event_type
        if et in ENTRY_TYPES:
            tag = event.extra.get("strategy")
            active = tag == strategy
            if active:
                filtered.append(event)
            continue
        if et in EXIT_TYPES and active:
            filtered.append(event)
            active = False
    return filtered


def _backtest_strategy(
    logger: ReplayLogger,
    *,
    session_count: int,
    strategy: str | None = None,
) -> object:
    events = [e for e in logger.events if e.event_type in ENTRY_TYPES | EXIT_TYPES]
    if strategy is not None:
        events = _events_for_strategy(events, strategy)
    trades = pair_trades(events)
    return summarize_trades(trades, sessions=session_count)


def _day_calendar(sessions: list[str], tags: dict[str, str], v38_trades: set[str], j_trades: set[str]) -> list[dict]:
    rows = []
    for s in sessions:
        j_count = sum(1 for t in j_trades if t == s)
        v_count = sum(1 for t in v38_trades if t == s)
        rows.append({
            "date": s,
            "or_type": tags.get(s, "?"),
            "v38_trades": v_count,
            "j_trades": j_count,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="from_date", default="2026-03-02")
    parser.add_argument("--to", dest="to_date", default="2026-06-12")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--j-mode", choices=("robust", "base"), default="robust")
    args = parser.parse_args()

    from_d = date.fromisoformat(args.from_date)
    to_d = date.fromisoformat(args.to_date)
    cfg = DEFAULT_CONFIG

    df, sessions, missing = _load_range(from_d, to_d)
    tags = _classify_sessions(df, cfg)
    wide_or = [s for s in sessions if tags.get(s) == "wide_or"]

    jcfg = JTrapConfig() if args.j_mode == "base" else J_TRAP_ROBUST
    combined_cfg = CombinedStrategyConfig(strategy=cfg, enable_j_plus=True, j_trap=jcfg)
    combined_logger = run_replay_fast(df, cfg, combined_cfg=combined_cfg)

    v38_only_cfg = CombinedStrategyConfig(strategy=cfg, enable_j_plus=False, j_trap=jcfg)
    v38_logger = run_replay_fast(df, cfg, combined_cfg=v38_only_cfg)

    v38 = _backtest_strategy(v38_logger, session_count=len(sessions), strategy="v38")
    j = _backtest_strategy(combined_logger, session_count=len(wide_or), strategy="j_plus")
    combined = _backtest_strategy(combined_logger, session_count=len(sessions))

    v38_by_day = [t.session_date for t in v38.trades]
    j_by_day = [t.session_date for t in j.trades]

    print(f"Range requested: {from_d} → {to_d}")
    print(f"Sessions loaded: {len(sessions)} ({sessions[0]} → {sessions[-1]})")
    if missing:
        print(f"Missing CSV ({len(missing)}): {', '.join(missing)}")
    print()
    print(f"Day mix: wide_or={sum(1 for s in sessions if tags.get(s)=='wide_or')}  "
          f"valid_or={sum(1 for s in sessions if tags.get(s)=='valid_or')}  "
          f"other={sum(1 for s in sessions if tags.get(s) not in ('wide_or','valid_or'))}")
    print()
    print("--- v3.8 (valid-OR days, J+ disabled) ---")
    print(f"  trades={v38.total_trades}  WR={v38.win_rate_pct:.1f}%  P&L={v38.total_pnl_pts:+.1f}  PF={v38.profit_factor:.2f}  DD={v38.max_drawdown_pts:.1f}")
    print()
    print(f"--- Setup J ({args.j_mode}, wide OR days only) ---")
    pf_j = f"{j.profit_factor:.2f}" if j.profit_factor else "—"
    print(f"  trades={j.total_trades}  WR={j.win_rate_pct:.1f}%  P&L={j.total_pnl_pts:+.1f}  PF={pf_j}  DD={j.max_drawdown_pts:.1f}")
    print()
    pf_c = f"{combined.profit_factor:.2f}" if combined.profit_factor else "—"
    print("--- Combined v3.9 (router) ---")
    print(f"  trades={combined.total_trades}  WR={combined.win_rate_pct:.1f}%  P&L={combined.total_pnl_pts:+.1f}  PF={pf_c}  DD={combined.max_drawdown_pts:.1f}")
    print()

    print("--- Setup J trades (compare to Pine markers) ---")
    for row in _trade_rows(j.trades, strategy="J"):
        print(
            f"  {row['session_date']} {row['entry_time']}-{row['exit_time']} "
            f"{row['side']:2} entry={row['entry_price']} exit={row['exit_price']} "
            f"pnl={row['pnl_pts']:+.1f} [{row['exit_reason']}]"
        )

    print()
    print("--- v3.8 trades ---")
    for row in _trade_rows(v38.trades, strategy="v3.8"):
        print(
            f"  {row['session_date']} {row['entry_time']}-{row['exit_time']} "
            f"{row['side']:2} entry={row['entry_price']} exit={row['exit_price']} "
            f"pnl={row['pnl_pts']:+.1f} [{row['exit_reason']}]"
        )

    payload = {
        "requested_from": args.from_date,
        "requested_to": args.to_date,
        "loaded_from": sessions[0],
        "loaded_to": sessions[-1],
        "sessions": len(sessions),
        "missing_sessions": missing,
        "classification": {
            "wide_or": len(wide_or),
            "valid_or": sum(1 for s in sessions if tags.get(s) == "valid_or"),
        },
        "v38": v38.to_dict(),
        "j_mode": args.j_mode,
        "setup_j": j.to_dict(),
        "combined": combined.to_dict(),
        "calendar": _day_calendar(sessions, tags, set(v38_by_day), set(j_by_day)),
        "j_trades": _trade_rows(j.trades, strategy="J"),
        "v38_trades": _trade_rows(v38.trades, strategy="v3.8"),
    }

    out_path = args.out or OUT / f"pine_compare_{sessions[0]}_{sessions[-1]}.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
