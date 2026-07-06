#!/usr/bin/env python3
"""Run Sgnal v3.9 algorithmic structure audit on historical replay."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.config import DEFAULT_CONFIG, CombinedStrategyConfig
from bot.replay import load_candles_csv, run_replay_fast
from bot.strategy_audit import format_strategy_audit_report, load_vix_by_session, run_strategy_audit
from bot.strategy_j import J_TRAP_ROBUST

ROOT = Path(__file__).resolve().parent.parent
HIST = ROOT / "data" / "historical"
OUT = ROOT / "data" / "research"


def _load_range(from_date: date, to_date: date) -> tuple:
    import pandas as pd

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


def main() -> int:
    parser = argparse.ArgumentParser(description="Sgnal v3.9 algorithmic vulnerability audit")
    parser.add_argument("--from", dest="from_date", default="2025-05-02")
    parser.add_argument("--to", dest="to_date", default="2026-06-12")
    parser.add_argument("--csv", type=Path, default=None, help="Single-session CSV (overrides range)")
    parser.add_argument("--vix-csv", type=Path, default=None, help="Optional VIX by session_date")
    parser.add_argument("--json", type=Path, default=None, help="Write JSON report")
    parser.add_argument("--no-j-plus", action="store_true", help="Replay v3.8-only (J+ disabled)")
    args = parser.parse_args()

    cfg = DEFAULT_CONFIG
    if args.csv:
        df = load_candles_csv(args.csv)
        sessions = [str(df["session_date"].iloc[0])]
        missing = []
    else:
        from_d = date.fromisoformat(args.from_date)
        to_d = date.fromisoformat(args.to_date)
        df, sessions, missing = _load_range(from_d, to_d)

    combined_cfg = CombinedStrategyConfig(
        strategy=cfg,
        enable_j_plus=not args.no_j_plus,
        j_trap=J_TRAP_ROBUST,
    )
    logger = run_replay_fast(df, cfg, combined_cfg=combined_cfg)

    vix = load_vix_by_session(args.vix_csv) if args.vix_csv else None
    report = run_strategy_audit(logger, df, cfg, vix_by_session=vix)

    print(format_strategy_audit_report(report))
    if missing:
        print(f"\nMissing sessions ({len(missing)}): {', '.join(missing[:8])}{'...' if len(missing) > 8 else ''}")
    print(f"\nSessions replayed: {len(sessions)} ({sessions[0]} → {sessions[-1]})")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sessions": len(sessions),
            "from": sessions[0],
            "to": sessions[-1],
            "missing": missing,
            "audit": report.to_dict(),
        }
        args.json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
