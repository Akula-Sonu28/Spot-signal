"""Phase 4: Validate A3 against actual NSE weekly expiry calendar."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from bot.backtest import TradeRecord, pair_trades
from bot.config import DEFAULT_CONFIG
from research.backtests.options_setups_comparison.config import OUTPUTS_ROOT
from research.backtests.options_setups_comparison.edge_pockets import SlippageSlice
from research.backtests.options_setups_comparison.expiry_calendar import (
    build_session_classification,
    load_or_build_expiries,
    tuesday_heuristic_class,
)
from research.backtests.options_setups_comparison.indicators_ext import time_bucket
from research.backtests.options_setups_comparison.robustness import DATA_LABEL, _concentration
from research.backtests.options_setups_comparison.robustness import run_baseline_replay
from research.backtests.options_setups_comparison.slippage import SlippageModel
from research.tune import load_cached_history

TZ = ZoneInfo("Asia/Kolkata")
FROM_DATE = "2025-05-02"
TO_DATE = "2026-06-09"

MIN_TRADES_CANDIDATE = 40
MIN_EXPECTANCY_CONSERVATIVE = 0.20


def _tb(t: TradeRecord) -> str:
    return time_bucket(t.entry_time.isoformat())


def _metrics(trades: list[TradeRecord]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0, "net_r": 0.0, "expectancy": 0.0, "median_r": 0.0, "win_rate_pct": 0.0}
    rs = [t.r_multiple for t in trades]
    wins = [r for r in rs if r > 0]
    return {
        "trades": len(trades),
        "net_r": round(sum(rs), 2),
        "expectancy": round(statistics.mean(rs), 3),
        "median_r": round(statistics.median(rs), 3),
        "win_rate_pct": round(100 * len(wins) / len(rs), 1),
        "profit_factor": round(
            sum(wins) / abs(sum(r for r in rs if r < 0)), 2
        ) if any(r < 0 for r in rs) else None,
    }


def _slippage_slices(all_by_tier: dict[str, list[TradeRecord]], pred) -> dict[str, SlippageSlice]:
    return {
        tier: SlippageSlice(tier=tier, trades=[t for t in trades if pred(t)])
        for tier, trades in all_by_tier.items()
    }


def _evaluate_candidate(label: str, slices: dict[str, SlippageSlice]) -> dict[str, Any]:
    cons = slices["conservative"]
    stress = slices["stress"]
    none = slices["none"]
    n = len(cons.trades)
    status = "candidate"
    reasons: list[str] = []

    if n < MIN_TRADES_CANDIDATE:
        status = "exploratory"
        reasons.append(f"trades={n} < {MIN_TRADES_CANDIDATE}")
    if cons.net_r <= 0:
        status = "rejected"
        reasons.append("conservative net_r <= 0")
    if cons.expectancy <= MIN_EXPECTANCY_CONSERVATIVE:
        if status == "candidate":
            status = "rejected"
        reasons.append(f"conservative expectancy {cons.expectancy:.3f} <= {MIN_EXPECTANCY_CONSERVATIVE}")
    if stress.net_r <= 0:
        status = "rejected"
        reasons.append("stress slippage not positive")

    return {
        "label": label,
        "status": status,
        "reasons": reasons,
        "trades": n,
        "net_r_none": round(none.net_r, 2),
        "net_r_conservative": round(cons.net_r, 2),
        "net_r_stress": round(stress.net_r, 2),
        "expectancy_conservative": round(cons.expectancy, 3),
        "median_r_conservative": round(cons.median_r, 3),
        "win_rate_conservative_pct": round(cons.win_rate_pct, 1),
        "profit_factor_conservative": round(cons.profit_factor, 2) if cons.profit_factor else None,
        "top20pct_contribution": round(_concentration(cons.trades).get("top20pct_trades_pct", 0), 1),
    }


def _walk_forward_thirds(trades: list[TradeRecord], sessions: list[str]) -> dict[str, dict]:
    n = len(sessions)
    if n < 3:
        return {}
    third = n // 3
    cuts = [
        ("first_third", set(sessions[:third])),
        ("middle_third", set(sessions[third : 2 * third])),
        ("last_third", set(sessions[2 * third :])),
    ]
    return {
        name: _metrics([t for t in trades if t.session_date in dates])
        for name, dates in cuts
    }


def _monthly_breakdown(trades: list[TradeRecord]) -> dict[str, dict]:
    buckets: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in trades:
        buckets[t.session_date[:7]].append(t)
    return {m: _metrics(v) for m, v in sorted(buckets.items())}


def _thesis_analysis(
    all_conservative: list[TradeRecord],
    session_meta: dict[str, dict],
) -> dict[str, Any]:
    """Compare Tuesday heuristic vs actual expiry drivers."""
    actual_expiry = [t for t in all_conservative if session_meta[t.session_date]["is_actual_expiry"]]
    tuesday_only = [t for t in all_conservative if tuesday_heuristic_class(t.session_date)]
    tuesday_not_expiry = [
        t for t in all_conservative
        if session_meta[t.session_date]["is_tuesday_heuristic"]
        and not session_meta[t.session_date]["is_actual_expiry"]
    ]
    expiry_not_tuesday = [
        t for t in all_conservative
        if session_meta[t.session_date]["is_actual_expiry"]
        and not session_meta[t.session_date]["is_tuesday_heuristic"]
    ]
    pe_expiry = [t for t in actual_expiry if t.side == "PE"]
    pe_expiry_morning = [t for t in pe_expiry if _tb(t) == "09:30-11:00"]

    return {
        "actual_expiry_day": _metrics(actual_expiry),
        "tuesday_heuristic": _metrics(tuesday_only),
        "tuesday_not_actual_expiry": _metrics(tuesday_not_expiry),
        "actual_expiry_not_tuesday": _metrics(expiry_not_tuesday),
        "pe_on_actual_expiry": _metrics(pe_expiry),
        "pe_expiry_morning": _metrics(pe_expiry_morning),
        "thesis_verdict": _thesis_verdict(actual_expiry, tuesday_only, tuesday_not_expiry, expiry_not_tuesday),
    }


def _thesis_verdict(
    actual: list[TradeRecord],
    tuesday: list[TradeRecord],
    tue_not_exp: list[TradeRecord],
    exp_not_tue: list[TradeRecord],
) -> str:
    a = sum(t.r_multiple for t in actual)
    t = sum(t.r_multiple for t in tuesday)
    if a <= 0 and t > 0:
        return "REJECT_EXPIRY_THESIS — Tuesday heuristic works but actual expiry does not."
    if a > 0 and t > 0 and abs(a - t) < 5:
        return "EXPIRY_AND_TUESDAY_ALIGNED — performance similar on both classifications."
    if a > t:
        return "ACTUAL_EXPIRY_STRONGER — true expiry days outperform Tuesday filter."
    if t > a:
        return "TUESDAY_WEEKDAY_EFFECT — Tuesday filter captures more than expiry alone."
    return "INCONCLUSIVE"


def _build_paper_trade_config(
    best: dict[str, Any] | None,
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    pe_row = next((r for r in results if r.get("label") == "actual_expiry_pe"), None)
    pe_morning = next((r for r in results if r.get("label") == "actual_expiry_pe_morning"), None)

    use_pe = pe_row and pe_row.get("status") in ("candidate", "exploratory")
    use_morning = (
        pe_morning
        and pe_morning.get("expectancy_conservative", 0) > MIN_EXPECTANCY_CONSERVATIVE
        and pe_morning.get("net_r_stress", 0) > 0
    )

    cfg = {
        "enabled": False,
        "data_label": DATA_LABEL,
        "option_premium": False,
        "setup": "A_actual_expiry_research",
        "description": "Paper-trade config from Phase 4 expiry validation — DISABLED by default",
        "filters": {
            "expiry_class": "weekly_expiry",
            "side": "PE" if use_pe else "ALL",
            "time_bucket": "09:30-11:00" if use_morning else "ALL",
        },
        "slippage_assumption": "conservative",
        "min_expectancy_r": MIN_EXPECTANCY_CONSERVATIVE,
        "notes": "Do not enable without 20+ paper sessions confirming fills.",
        "total_variant": best,
        "pe_variant": pe_row,
    }
    if best:
        cfg["recommended_variant"] = best.get("label")
        cfg["backtest_expectancy_conservative"] = best.get("expectancy_conservative")
        cfg["backtest_net_r_conservative"] = best.get("net_r_conservative")
    if pe_row:
        cfg["pe_expectancy_conservative"] = pe_row.get("expectancy_conservative")
        cfg["pe_trades"] = pe_row.get("trades")
    return cfg


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 4 expiry validation")
    parser.add_argument("--refresh-expiries", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir or (
        OUTPUTS_ROOT / f"setup_a_expiry_validation_{FROM_DATE}_{TO_DATE}_{stamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    print(DATA_LABEL)
    print("Phase 4: actual NSE expiry validation")

    df, sessions = load_cached_history(futures_only=False, from_date=FROM_DATE, to_date=TO_DATE)
    expiries, expiry_source = load_or_build_expiries(
        sessions, FROM_DATE, TO_DATE, refresh=args.refresh_expiries,
    )
    print(f"Weekly expiries inferred: {len(expiries)} (sample: {expiries[:3]} ... {expiries[-3:]})")
    print(f"Expiry source: {expiry_source}")
    session_meta = build_session_classification(sessions, expiries)

    # Session classification export
    with (output_dir / "session_expiry_classification.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "session_date", "expiry_class", "weekly_expiry_date", "weekday",
            "is_tuesday_heuristic", "is_actual_expiry", "tuesday_mismatch", "expiry_not_tuesday",
        ])
        w.writeheader()
        for sd in sorted(session_meta):
            w.writerow({"session_date": sd, **session_meta[sd]})

    all_by_tier: dict[str, list[TradeRecord]] = {}
    for tier in ("none", "conservative", "stress"):
        logger = run_baseline_replay(df, DEFAULT_CONFIG, slippage=SlippageModel(tier))
        events = [
            e for e in logger.events
            if e.event_type.startswith("BUY_") or "SL_" in e.event_type
            or "TARGET_" in e.event_type or e.event_type == "SQUARE_OFF"
        ]
        all_by_tier[tier] = pair_trades(events)

    cons_trades = all_by_tier["conservative"]
    results: list[dict[str, Any]] = []

    expiry_classes = ("weekly_expiry", "day_before_expiry", "day_after_expiry", "normal")
    for ec in expiry_classes:
        pred = lambda t, c=ec: session_meta[t.session_date]["expiry_class"] == c
        slices = _slippage_slices(all_by_tier, pred)
        row = _evaluate_candidate(f"expiry_class_{ec}", slices)
        row["expiry_class"] = ec
        # CE/PE split
        row["ce_conservative"] = _metrics([t for t in slices["conservative"].trades if t.side == "CE"])
        row["pe_conservative"] = _metrics([t for t in slices["conservative"].trades if t.side == "PE"])
        # Time buckets
        row["time_buckets_conservative"] = {
            b: _metrics([t for t in slices["conservative"].trades if _tb(t) == b])
            for b in ("09:30-11:00", "11:00-13:00", "13:00-15:15")
        }
        row["walk_forward_conservative"] = _walk_forward_thirds(slices["conservative"].trades, sessions)
        row["monthly_conservative"] = _monthly_breakdown(slices["conservative"].trades)
        results.append(row)
        print(f"  {ec}: trades={row['trades']} netR={row['net_r_conservative']} status={row['status']}")

    # Combined variants for paper-trade candidates
    combos = [
        ("actual_expiry_all", lambda t: session_meta[t.session_date]["expiry_class"] == "weekly_expiry"),
        ("actual_expiry_pe", lambda t: session_meta[t.session_date]["expiry_class"] == "weekly_expiry" and t.side == "PE"),
        ("actual_expiry_pe_morning", lambda t: session_meta[t.session_date]["expiry_class"] == "weekly_expiry" and t.side == "PE" and _tb(t) == "09:30-11:00"),
        ("tuesday_heuristic_pe", lambda t: tuesday_heuristic_class(t.session_date) and t.side == "PE"),
    ]
    for label, pred in combos:
        slices = _slippage_slices(all_by_tier, pred)
        row = _evaluate_candidate(label, slices)
        row["walk_forward_conservative"] = _walk_forward_thirds(slices["conservative"].trades, sessions)
        row["monthly_conservative"] = _monthly_breakdown(slices["conservative"].trades)
        results.append(row)
        print(f"  {label}: trades={row['trades']} exp={row['expectancy_conservative']} status={row['status']}")

    thesis = _thesis_analysis(cons_trades, session_meta)
    candidates = [r for r in results if r["status"] == "candidate"]
    candidates.sort(key=lambda r: r["expectancy_conservative"], reverse=True)
    best = candidates[0] if candidates else None

    paper_cfg = _build_paper_trade_config(best, results)
    (output_dir / "paper_trade_config.json").write_text(
        json.dumps(paper_cfg, indent=2), encoding="utf-8",
    )

    payload = {
        "data_label": DATA_LABEL,
        "option_premium": False,
        "research_only": True,
        "from_date": FROM_DATE,
        "to_date": TO_DATE,
        "sessions": len(sessions),
        "weekly_expiries_count": len(expiries),
        "expiries_sample": expiries[:5] + ["..."] + expiries[-3:],
        "expiry_source": expiry_source,
        "thesis_analysis": thesis,
        "best_candidate": best,
        "all_results": results,
        "paper_trade_config_path": "paper_trade_config.json",
        "decision": (
            f"CANDIDATE: {best['label']}" if best
            else "NO_CANDIDATE — actual expiry variant fails decision rules."
        ),
    }
    (output_dir / "expiry_validation_summary.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8",
    )

    # Comparison table CSV
    fields = [
        "label", "status", "trades", "net_r_none", "net_r_conservative", "net_r_stress",
        "expectancy_conservative", "median_r_conservative", "reasons",
    ]
    with (output_dir / "expiry_validation_table.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in results:
            row = dict(r)
            row["reasons"] = "; ".join(r.get("reasons", []))
            w.writerow(row)

    print(f"\nThesis: {thesis['thesis_verdict']}")
    print(f"Decision: {payload['decision']}")
    print(f"Paper-trade config (disabled): {output_dir / 'paper_trade_config.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
