#!/usr/bin/env python3
"""Walk-forward validation for top sweep candidate (13:30 gate + J+ cap 2).

IS: 2025-05-02 – 2026-03-31 | OOS: 2026-04-01 – 2026-06-09
Does not modify locked strategy modules.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.backtest import ENTRY_TYPES, EXIT_TYPES, pair_trades, summarize_trades
from bot.config import DEFAULT_CONFIG, CombinedStrategyConfig
from bot.replay import run_replay_fast
from bot.strategy_j import J_TRAP_ROBUST
from scripts.research_sweeps import (
    OUT,
    SweepCell,
    _load_range,
    _precompute_j_outside_streaks,
    evaluate_cell,
    filter_trade_events,
)
from bot.strategy_audit import index_session_bars

# Chronological walk-forward windows (prompt Workstream B)
IS_FROM = date(2025, 5, 2)
IS_TO = date(2026, 3, 31)
OOS_FROM = date(2026, 4, 1)
OOS_TO = date(2026, 6, 9)

CANDIDATE_TIME_GATE = "13:30"
CANDIDATE_MAX_BARS_OUTSIDE = 2
OVERFIT_PF_RATIO = 1.5
MIN_OOS_J_PLUS_TRADES = 15


@dataclass(frozen=True)
class HorizonResult:
    horizon: str
    variant: str
    cell: SweepCell
    j_plus_trades: int

    def to_dict(self) -> dict[str, Any]:
        d = self.cell.to_dict()
        d["horizon"] = self.horizon
        d["variant"] = self.variant
        d["j_plus_trades"] = self.j_plus_trades
        return d


@dataclass
class WalkForwardReport:
    is_baseline: HorizonResult
    is_candidate: HorizonResult
    oos_baseline: HorizonResult
    oos_candidate: HorizonResult
    alpha_degradation_ratio: float | None
    overfit_warning: bool
    overfit_note: str
    oos_j_plus_significant: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "windows": {
                "in_sample": {"from": IS_FROM.isoformat(), "to": IS_TO.isoformat()},
                "out_of_sample": {"from": OOS_FROM.isoformat(), "to": OOS_TO.isoformat()},
            },
            "candidate": {
                "time_gate": CANDIDATE_TIME_GATE,
                "max_bars_outside_or": CANDIDATE_MAX_BARS_OUTSIDE,
            },
            "rows": [
                self.is_baseline.to_dict(),
                self.is_candidate.to_dict(),
                self.oos_baseline.to_dict(),
                self.oos_candidate.to_dict(),
            ],
            "alpha_degradation_ratio": (
                round(self.alpha_degradation_ratio, 3) if self.alpha_degradation_ratio is not None else None
            ),
            "overfit_warning": self.overfit_warning,
            "overfit_note": self.overfit_note,
            "oos_j_plus_trades": self.oos_candidate.j_plus_trades,
            "oos_j_plus_significant": self.oos_j_plus_significant,
            "min_oos_j_plus_trades": MIN_OOS_J_PLUS_TRADES,
        }


def count_j_plus_trades(events: list) -> int:
    """Count paired trades whose entry event was J+."""
    j_keys = {
        (e.event_type, e.side, e.timestamp)
        for e in events
        if e.event_type in ENTRY_TYPES and (e.extra or {}).get("strategy") == "j_plus"
    }
    trades = pair_trades([e for e in events if e.event_type in ENTRY_TYPES | EXIT_TYPES])
    return sum(1 for t in trades if (f"BUY_{t.side}", t.side, t.entry_time) in j_keys)


def _run_horizon(
    from_d: date,
    to_d: date,
    horizon_label: str,
    cfg: Any = DEFAULT_CONFIG,
) -> tuple[HorizonResult, HorizonResult, list[str]]:
    df, sessions, missing = _load_range(from_d, to_d)
    combined = CombinedStrategyConfig(strategy=cfg, enable_j_plus=True, j_trap=J_TRAP_ROBUST)
    logger = run_replay_fast(df, cfg, combined_cfg=combined)
    session_bars = index_session_bars(df)
    j_streaks = _precompute_j_outside_streaks(logger.events, session_bars)
    n_sessions = len(sessions)

    baseline_cell = evaluate_cell(
        logger.events,
        time_gate=None,
        max_bars_outside_or=None,
        j_streaks=j_streaks,
        session_count=n_sessions,
        cfg=cfg,
    )
    candidate_cell = evaluate_cell(
        logger.events,
        time_gate=CANDIDATE_TIME_GATE,
        max_bars_outside_or=CANDIDATE_MAX_BARS_OUTSIDE,
        j_streaks=j_streaks,
        session_count=n_sessions,
        cfg=cfg,
    )

    cand_events = filter_trade_events(
        logger.events,
        time_gate=CANDIDATE_TIME_GATE,
        max_bars_outside_or=CANDIDATE_MAX_BARS_OUTSIDE,
        j_streaks=j_streaks,
        cfg=cfg,
    )
    j_plus = count_j_plus_trades(cand_events)

    baseline = HorizonResult(horizon_label, "Baseline v3.9", baseline_cell, j_plus_trades=0)
    candidate = HorizonResult(horizon_label, "13:30 + Cap 2", candidate_cell, j_plus_trades=j_plus)
    return baseline, candidate, missing


def run_walk_forward(cfg: Any = DEFAULT_CONFIG) -> WalkForwardReport:
    is_base, is_cand, _ = _run_horizon(IS_FROM, IS_TO, "IS (Train)", cfg)
    oos_base, oos_cand, _ = _run_horizon(OOS_FROM, OOS_TO, "OOS (Test)", cfg)

    pf_is = is_cand.cell.profit_factor
    pf_oos = oos_cand.cell.profit_factor
    alpha_deg: float | None = None
    overfit = False
    note = "PF stable across IS/OOS"

    if pf_is is not None and pf_oos is not None and pf_is > 0:
        alpha_deg = pf_oos / pf_is
        if pf_is > OVERFIT_PF_RATIO * pf_oos:
            overfit = True
            note = (
                f"OVERFIT WARNING: IS PF ({pf_is:.2f}) > {OVERFIT_PF_RATIO}× OOS PF ({pf_oos:.2f})"
            )
    elif pf_is is None or pf_oos is None:
        note = "PF undefined in one horizon (insufficient losses)"

    oos_j_sig = oos_cand.j_plus_trades >= MIN_OOS_J_PLUS_TRADES

    return WalkForwardReport(
        is_baseline=is_base,
        is_candidate=is_cand,
        oos_baseline=oos_base,
        oos_candidate=oos_cand,
        alpha_degradation_ratio=alpha_deg,
        overfit_warning=overfit,
        overfit_note=note,
        oos_j_plus_significant=oos_j_sig,
    )


def _pf_str(pf: float | None) -> str:
    return f"{pf:.2f}" if pf is not None else "—"


def format_walk_forward_table(report: WalkForwardReport) -> str:
    lines = [
        "### Walk-Forward Performance Matrix",
        "| Horizon | Strategy Variant | Total Trades | Win Rate | Net P&L (Pts) | Profit Factor | Max DD |",
        "|:---|:---|:---|:---|:---|:---|:---|",
    ]
    for row in (report.is_baseline, report.is_candidate, report.oos_baseline, report.oos_candidate):
        c = row.cell
        lines.append(
            f"| **{row.horizon}** | {row.variant} | {c.total_trades} | {c.win_rate_pct:.1f}% | "
            f"{c.net_pnl_pts:+.1f} | {_pf_str(c.profit_factor)} | {c.max_drawdown_pts:.1f} |"
        )
    lines.extend([
        "",
        (
            f"**Alpha Degradation Ratio** (PF_OOS / PF_IS, candidate): "
            f"{report.alpha_degradation_ratio:.3f}"
            if report.alpha_degradation_ratio is not None
            else "**Alpha Degradation Ratio**: —"
        ),
        "",
        f"**Overfit check:** {report.overfit_note}",
        "",
        f"**OOS J+ trades (candidate):** {report.oos_candidate.j_plus_trades} "
        f"(minimum {MIN_OOS_J_PLUS_TRADES} required — "
        f"{'PASS' if report.oos_j_plus_significant else 'FAIL'})",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward validation — 13:30 + J+ cap 2")
    parser.add_argument(
        "--json",
        type=Path,
        default=OUT / "walk_forward_report.json",
        help="Output JSON path",
    )
    args = parser.parse_args()

    report = run_walk_forward()
    print(format_walk_forward_table(report))

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
    print(f"\nWrote {args.json}")

    return 1 if report.overfit_warning or not report.oos_j_plus_significant else 0


if __name__ == "__main__":
    raise SystemExit(main())
