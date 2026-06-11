"""Comparison reports and recommendations for options setups research."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from research.backtests.options_setups_comparison.metrics import SetupMetrics

TZ = ZoneInfo("Asia/Kolkata")


def write_comparison_table(metrics_list: list[SetupMetrics], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "setup_id", "setup_name", "slippage_tier", "data_type", "option_premium",
        "trading_days", "total_trades", "avg_trades_per_day", "win_rate_pct",
        "avg_r", "median_r", "net_r", "profit_factor", "max_drawdown_r",
        "max_losing_streak", "expectancy_per_trade", "expectancy_per_day",
        "ce_trades", "ce_net_r", "pe_trades", "pe_net_r", "skipped_count",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for m in metrics_list:
            w.writerow({
                "setup_id": m.setup_id,
                "setup_name": m.setup_name,
                "slippage_tier": m.slippage_tier,
                "data_type": m.data_type,
                "option_premium": m.option_premium,
                "trading_days": m.trading_days,
                "total_trades": m.total_trades,
                "avg_trades_per_day": round(m.avg_trades_per_day, 3),
                "win_rate_pct": round(m.win_rate_pct, 1),
                "avg_r": round(m.avg_r, 3),
                "median_r": round(m.median_r, 3),
                "net_r": round(m.net_r, 2),
                "profit_factor": round(m.profit_factor, 2) if m.profit_factor else "",
                "max_drawdown_r": round(m.max_drawdown_r, 2),
                "max_losing_streak": m.max_losing_streak,
                "expectancy_per_trade": round(m.expectancy_per_trade, 3),
                "expectancy_per_day": round(m.expectancy_per_day, 3),
                "ce_trades": m.ce_trades,
                "ce_net_r": round(m.ce_net_r, 2),
                "pe_trades": m.pe_trades,
                "pe_net_r": round(m.pe_net_r, 2),
                "skipped_count": m.skipped_count,
            })


def write_assumptions(path: Path, cfg_dict: dict[str, Any]) -> None:
    text = f"""# Assumptions and Limitations

## Data
- **Type:** underlying spot-proxy only (`data_type: underlying_spot_proxy`)
- **Option premium:** NOT available — results do NOT represent option P&L
- **Period:** {cfg_dict.get('from_date')} to {cfg_dict.get('to_date')} (futures-VWAP era)
- **Timeframe:** 5-minute bars (matches live model)
- **Live signal archive:** unavailable — baseline is replicated Setup A, not archived Telegram trades

## Risk engine
- Unified research risk: max 2 trades/day, max 1 CE + 1 PE, stop after 2 losses/day
- Stop loss capped at 1.2×ATR; target 1.8R; same-bar SL priority
- Pass `{cfg_dict.get('risk_pass')}`: {cfg_dict.get('risk_engine_note', '')}

## Slippage
- Tiers: none (0 pts), conservative (2 pts), stress (5 pts) per side
- No brokerage model in repository

## Sample size
- ~46 trading days — high variance; no walk-forward optimization in this run
- No aggressive parameter tuning (fixed thresholds per spec)

## Other
- Index volume is zero in cache — volume filters unavailable
- NIFTY weekly expiry heuristic: Tuesday
- Gap detection uses prior session close from cache
"""
    path.write_text(text, encoding="utf-8")


def generate_recommendation(
    metrics_list: list[SetupMetrics],
    baseline_id: str = "A",
    slippage_tier: str = "conservative",
) -> str:
    """Produce recommendation from unified-pass conservative slippage metrics."""
    subset = [m for m in metrics_list if m.slippage_tier == slippage_tier]
    if not subset:
        subset = metrics_list

    baseline = next((m for m in subset if m.setup_id == baseline_id), None)
    candidates = [m for m in subset if m.setup_id != baseline_id and m.total_trades >= 5]

    ranked = sorted(candidates, key=lambda m: (m.net_r, m.profit_factor or 0), reverse=True)
    best = ranked[0] if ranked else None

    if baseline is None:
        if best:
            return f"INCONCLUSIVE — baseline missing; best research setup: {best.setup_id} ({best.setup_name}) net_r={best.net_r:.2f}"
        return "INCONCLUSIVE — no baseline and no alternative setup trades."

    if best is None:
        return (
            f"KEEP_CURRENT_MODEL — baseline A net_r={baseline.net_r:.2f}; "
            "no alternative setup produced enough trades (min 5)."
        )

    improvement = best.net_r - baseline.net_r
    if improvement < 2.0 or (best.profit_factor or 0) <= (baseline.profit_factor or 0):
        return (
            f"KEEP_CURRENT_MODEL — baseline A net_r={baseline.net_r:.2f} PF={baseline.profit_factor}; "
            f"best alt {best.setup_id} net_r={best.net_r:.2f} does not justify switch on 46-day proxy sample."
        )

    if improvement >= 5.0 and (best.profit_factor or 0) > 1.2:
        return (
            f"REPLACE_WITH_{best.setup_id} — {best.setup_name} outperforms baseline by {improvement:.2f}R "
            f"(conservative slippage). Paper-trade 20 sessions before any live change."
        )

    return (
        f"COMBINE_REVIEW — {best.setup_id} ({best.setup_name}) shows promise (+{improvement:.2f}R vs baseline) "
        f"but sample is short; consider paper-trading A+{best.setup_id} in parallel."
    )


def write_recommendation(path: Path, text: str, metrics_list: list[SetupMetrics]) -> None:
    lines = ["# Recommendation", "", text, "", "## Slippage sensitivity (net R)", ""]
    for m in sorted(metrics_list, key=lambda x: (x.setup_id, x.slippage_tier)):
        lines.append(f"- {m.setup_id} / {m.slippage_tier}: net_r={m.net_r:.2f}, trades={m.total_trades}, PF={m.profit_factor}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def validate_baseline_parity(
    run_metrics: SetupMetrics,
    reference_path: Path,
) -> dict[str, Any]:
    """Compare Setup A none-slippage to existing data/backtest summary."""
    result: dict[str, Any] = {"reference_exists": reference_path.exists(), "parity_ok": False}
    if not reference_path.exists():
        result["note"] = "Reference summary not found"
        return result
    ref = json.loads(reference_path.read_text(encoding="utf-8"))
    result["reference_trades"] = ref.get("total_trades")
    result["run_trades"] = run_metrics.total_trades
    result["reference_pnl"] = ref.get("total_pnl_pts")
    result["run_net_r"] = run_metrics.net_r
    trade_delta = abs((ref.get("total_trades") or 0) - run_metrics.total_trades)
    result["parity_ok"] = trade_delta <= 2
    result["trade_delta"] = trade_delta
    return result


def write_comparison_summary(
    path: Path,
    metrics_list: list[SetupMetrics],
    audit: dict[str, Any],
    parity: dict[str, Any],
    cfg_dict: dict[str, Any],
) -> None:
    payload = {
        "timestamp": datetime.now(TZ).isoformat(),
        "data_type": "underlying_spot_proxy",
        "option_premium": False,
        "audit": audit,
        "baseline_parity": parity,
        "config": cfg_dict,
        "setups": [m.to_dict() for m in metrics_list],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
