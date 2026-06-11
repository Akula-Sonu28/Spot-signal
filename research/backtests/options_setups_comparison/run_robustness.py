"""CLI: Phase 2 Setup A robustness diagnostics (isolated research)."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from bot.config import DEFAULT_CONFIG
from research.backtests.options_setups_comparison.config import OUTPUTS_ROOT
from research.backtests.options_setups_comparison.robustness import (
    DATA_LABEL,
    _compute_mae_mfe,
    build_robustness_report,
    recommend_robustness,
    run_baseline_replay,
    write_robustness_outputs,
    _config_for_preset,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel
from research.tune import load_cached_history

TZ = ZoneInfo("Asia/Kolkata")


def _detect_date_range(futures_only: bool) -> tuple[str, str]:
    """Use widest available cached range."""
    _, sessions = load_cached_history(futures_only=futures_only)
    return sessions[0], sessions[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Setup A robustness diagnostics (research only)")
    parser.add_argument("--from", dest="from_date", default=None)
    parser.add_argument("--to", dest="to_date", default=None)
    parser.add_argument("--futures-only", action="store_true", help="Restrict to futures-VWAP era")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    futures_only = args.futures_only
    auto_from, auto_to = _detect_date_range(futures_only=False)
    from_date = args.from_date or auto_from
    to_date = args.to_date or auto_to

    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir or (
        OUTPUTS_ROOT / f"setup_a_robustness_{from_date}_{to_date}_{stamp}"
    )

    print(DATA_LABEL)
    print(f"Loading cached history {from_date} → {to_date} (futures_only={futures_only})")

    df, sessions = load_cached_history(
        futures_only=futures_only,
        from_date=from_date,
        to_date=to_date,
    )
    print(f"Sessions loaded: {len(sessions)}")

    reports: dict = {}
    trade_diags: dict = {}

    scenarios: list[tuple[str, dict]] = [
        ("baseline_current_entry", {"slippage": SlippageModel("none")}),
        ("execution_conservative_slippage", {"slippage": SlippageModel("conservative")}),
        ("execution_stress_slippage", {"slippage": SlippageModel("stress")}),
        ("execution_delayed_1bar", {"delayed_entry": True}),
    ]

    for key, kwargs in scenarios:
        logger = run_baseline_replay(df, DEFAULT_CONFIG, **kwargs)
        reports[key] = build_robustness_report(
            key, logger, sessions, df, from_date, to_date,
        )
        events = [e for e in logger.events if e.event_type.startswith("BUY_") or "SL_" in e.event_type or "TARGET_" in e.event_type or e.event_type == "SQUARE_OFF"]
        from bot.backtest import pair_trades
        trades = pair_trades(events)
        diags, _ = _compute_mae_mfe(trades, df)
        trade_diags[key] = diags
        print(f"  {key}: trades={reports[key].total_trades} net_r={reports[key].net_r:.2f} exp={reports[key].expectancy_r:.3f}")

    for preset in ("sl_1.0_atr_tgt_1.5r", "sl_1.2_atr_tgt_1.8r", "sl_1.5_atr_tgt_2.0r"):
        cfg = _config_for_preset(preset)
        logger = run_baseline_replay(df, cfg, slippage=SlippageModel("none"))
        key = f"risk_{preset}"
        reports[key] = build_robustness_report(
            key, logger, sessions, df, from_date, to_date,
        )
        print(f"  {key}: trades={reports[key].total_trades} net_r={reports[key].net_r:.2f}")

    # Sub-period stability: split chronologically
    mid = len(sessions) // 2
    if mid > 0:
        first_half = sessions[:mid]
        second_half = sessions[mid:]
        for label, subset in [("period_first_half", first_half), ("period_second_half", second_half)]:

            sub_df = df[df["session_date"].isin(subset)].copy()
            logger = run_baseline_replay(sub_df, DEFAULT_CONFIG)
            reports[label] = build_robustness_report(
                label, logger, subset, sub_df, subset[0], subset[-1],
            )
            print(f"  {label}: trades={reports[label].total_trades} net_r={reports[label].net_r:.2f}")

    recommendation = recommend_robustness(reports)
    meta = {
        "from_date": from_date,
        "to_date": to_date,
        "sessions": len(sessions),
        "futures_only": futures_only,
        "extended_range": from_date == auto_from and to_date == auto_to,
        "auto_range": {"from": auto_from, "to": auto_to},
    }

    write_robustness_outputs(output_dir, reports, trade_diags, recommendation, meta)

    print(f"\nRecommendation: {recommendation}")
    print(f"Outputs: {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
