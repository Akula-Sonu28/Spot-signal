"""CLI: Phase 3 Setup A edge-pocket validation (isolated research)."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.backtest import pair_trades
from bot.config import DEFAULT_CONFIG
from research.backtests.options_setups_comparison.config import OUTPUTS_ROOT
from research.backtests.options_setups_comparison.edge_pockets import (
    DATA_LABEL,
    VARIANTS,
    SlippageSlice,
    VWAP_ERA_CUTOFF,
    build_final_verdict,
    evaluate_variant,
    filter_trades,
    write_edge_pocket_outputs,
)
from research.backtests.options_setups_comparison.robustness import run_baseline_replay
from research.backtests.options_setups_comparison.slippage import SlippageModel
from research.tune import load_cached_history

TZ = ZoneInfo("Asia/Kolkata")
FROM_DATE = "2025-05-02"
TO_DATE = "2026-06-09"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Setup A edge-pocket validation (Phase 3)")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir or (
        OUTPUTS_ROOT / f"setup_a_edge_pockets_{FROM_DATE}_{TO_DATE}_{stamp}"
    )

    print(DATA_LABEL)
    print(f"Phase 3 edge-pocket validation: {FROM_DATE} → {TO_DATE}")

    df, sessions = load_cached_history(
        futures_only=False,
        from_date=FROM_DATE,
        to_date=TO_DATE,
    )
    print(f"Sessions: {len(sessions)}")

    all_trades_by_tier: dict[str, list] = {}
    for tier in ("none", "conservative", "stress"):
        logger = run_baseline_replay(df, DEFAULT_CONFIG, slippage=SlippageModel(tier))
        events = [
            e for e in logger.events
            if e.event_type.startswith("BUY_") or "SL_" in e.event_type
            or "TARGET_" in e.event_type or e.event_type == "SQUARE_OFF"
        ]
        all_trades_by_tier[tier] = pair_trades(events)
        print(f"  Baseline trades ({tier}): {len(all_trades_by_tier[tier])}")

    results: list[dict] = []
    for vdef in VARIANTS:
        slices = {
            tier: SlippageSlice(tier=tier, trades=filter_trades(all_trades_by_tier[tier], vdef))
            for tier in ("none", "conservative", "stress")
        }
        row = evaluate_variant(vdef, slices, all_trades_by_tier, sessions)
        results.append(row)
        print(
            f"  {vdef.variant_id} {vdef.name}: trades={row['trades']} "
            f"netR={row['net_r_none']}/{row['net_r_conservative']}/{row['net_r_stress']} "
            f"status={row['status']}"
        )

    verdict = build_final_verdict(results)
    meta = {
        "from_date": FROM_DATE,
        "to_date": TO_DATE,
        "sessions": len(sessions),
        "default_reporting_slippage": "conservative",
        "vwap_era_cutoff": VWAP_ERA_CUTOFF,
        "warning": (
            "UNDERLYING SPOT PROXY ONLY. No option premium data. "
            "Tuesday = expiry heuristic, not official NSE calendar."
        ),
    }

    write_edge_pocket_outputs(output_dir, results, verdict, meta)

    print(f"\n{verdict['recommendation']}")
    print(f"A0 status: {verdict['a0_status']}")
    print(f"Best candidate: {verdict['best_candidate']}")
    print(f"Outputs: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
