"""CLI entry point for isolated options setups comparison research."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from research.backtests.options_setups_comparison.config import (
    DEFAULT_FROM,
    DEFAULT_TO,
    OUTPUTS_ROOT,
    SETUP_IDS,
    ResearchConfig,
)
from research.backtests.options_setups_comparison.data_audit import run_data_audit
from research.backtests.options_setups_comparison.metrics import (
    compute_setup_metrics,
    write_equity_curve,
    write_skipped_csv,
    write_trades_csv,
)
from research.backtests.options_setups_comparison.replay_engine import run_setup_replay
from research.backtests.options_setups_comparison.report import (
    generate_recommendation,
    validate_baseline_parity,
    write_assumptions,
    write_comparison_summary,
    write_comparison_table,
    write_recommendation,
)
from research.backtests.options_setups_comparison.setups import SETUP_REGISTRY
from research.backtests.options_setups_comparison.slippage import SlippageModel
from research.tune import load_cached_history

TZ = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parents[3]
BASELINE_REF = ROOT / "data" / "backtest" / "summary_2026-04-01_2026-06-09_20260609_222154.json"


def _parse_list(raw: str, *, upper: bool = True) -> tuple[str, ...]:
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return tuple(x.upper() if upper else x.lower() for x in items)


def _make_run_id(cfg: ResearchConfig, risk_pass: str) -> str:
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    return f"{cfg.from_date}_{cfg.to_date}_{risk_pass}_{stamp}"


def run_pass(
    cfg: ResearchConfig,
    output_dir: Path,
    *,
    risk_pass: str,
    use_production_baseline: bool,
) -> list:
    from research.backtests.options_setups_comparison.metrics import SetupMetrics

    df, sessions = load_cached_history(
        futures_only=cfg.futures_only,
        from_date=cfg.from_date,
        to_date=cfg.to_date,
    )

    all_metrics: list[SetupMetrics] = []
    pass_dir = output_dir / risk_pass
    pass_dir.mkdir(parents=True, exist_ok=True)

    for setup_id in cfg.setup_ids:
        if setup_id not in SETUP_REGISTRY:
            print(f"Unknown setup {setup_id}, skipping", file=sys.stderr)
            continue
        name, processor = SETUP_REGISTRY[setup_id]

        for tier in cfg.slippage_tiers:
            slippage = SlippageModel(tier=tier)
            prod_baseline = use_production_baseline and setup_id == "A"
            logger, skipped = run_setup_replay(
                df,
                setup_id,
                processor,
                cfg.strategy,
                cfg,
                slippage,
                use_production_baseline=prod_baseline,
            )
            metrics = compute_setup_metrics(
                setup_id, name, tier, logger, skipped, sessions,
            )
            all_metrics.append(metrics)

            slug = f"{setup_id}_{name}_{tier}"
            write_trades_csv(metrics.trades, pass_dir / f"trades_{slug}.csv")
            write_skipped_csv(skipped.entries, pass_dir / f"skipped_{slug}.csv")
            write_equity_curve(metrics.trades, pass_dir / "equity_curves" / f"{slug}.csv")

            print(
                f"  [{risk_pass}] {setup_id}/{tier}: trades={metrics.total_trades} "
                f"net_r={metrics.net_r:.2f} PF={metrics.profit_factor}"
            )

    write_comparison_table(all_metrics, pass_dir / "comparison_table.csv")
    return all_metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated options setups comparison research")
    parser.add_argument("--audit-only", action="store_true")
    parser.add_argument("--from", dest="from_date", default=DEFAULT_FROM)
    parser.add_argument("--to", dest="to_date", default=DEFAULT_TO)
    parser.add_argument("--setups", default=",".join(SETUP_IDS))
    parser.add_argument("--slippage", default="none,conservative,stress")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    cfg = ResearchConfig(
        from_date=args.from_date,
        to_date=args.to_date,
        setup_ids=_parse_list(args.setups),
        slippage_tiers=_parse_list(args.slippage, upper=False),
    )

    run_id = _make_run_id(cfg, "full")
    output_dir = args.output_dir or (OUTPUTS_ROOT / run_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    audit = run_data_audit(cfg, output_dir=output_dir)
    if not audit.passed:
        print("Backtest not run because required data is missing")
        print(json.dumps(audit.to_dict(), indent=2))
        return 1

    if args.audit_only:
        print("Data audit PASSED (audit-only mode)")
        print(json.dumps(audit.to_dict(), indent=2))
        return 0

    cfg_dict = {
        "from_date": cfg.from_date,
        "to_date": cfg.to_date,
        "futures_only": cfg.futures_only,
        "setup_ids": list(cfg.setup_ids),
        "slippage_tiers": list(cfg.slippage_tiers),
        "data_type": cfg.data_type,
        "option_premium": cfg.option_premium,
    }
    (output_dir / "backtest_config.json").write_text(
        json.dumps(cfg_dict, indent=2), encoding="utf-8",
    )

    print("Research backtest starting in isolated mode...")
    print(f"Output: {output_dir}")

    # Pass 1: A=production, B-H=unified research
    print("\n--- Pass 1: production baseline A ---")
    pass1 = run_pass(cfg, output_dir, risk_pass="pass1_production_baseline", use_production_baseline=True)

    # Pass 2: all unified (A uses research orb path)
    print("\n--- Pass 2: unified risk engine (all setups) ---")
    pass2 = run_pass(cfg, output_dir, risk_pass="pass2_unified_risk", use_production_baseline=False)

    all_metrics = pass1 + pass2

    baseline_none = next(
        (m for m in pass1 if m.setup_id == "A" and m.slippage_tier == "none"),
        None,
    )
    parity = validate_baseline_parity(baseline_none, BASELINE_REF) if baseline_none else {}

    cfg_dict["risk_pass"] = "pass1 + pass2"
    cfg_dict["risk_engine_note"] = (
        "Pass1: Setup A uses production process_bar; B-H use unified research risk. "
        "Pass2: all setups use unified research processors."
    )

    write_comparison_summary(
        output_dir / "comparison_summary.json",
        all_metrics,
        audit.to_dict(),
        parity,
        cfg_dict,
    )
    write_comparison_table(all_metrics, output_dir / "comparison_table.csv")
    write_assumptions(output_dir / "assumptions.md", cfg_dict)

    unified_conservative = [m for m in pass2 if m.slippage_tier == "conservative"]
    rec = generate_recommendation(unified_conservative, baseline_id="A", slippage_tier="conservative")
    write_recommendation(output_dir / "recommendation.md", rec, pass2)

    print("\nResearch backtest completed in isolated mode")
    print(f"Baseline parity: {parity}")
    print(f"Recommendation: {rec}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
