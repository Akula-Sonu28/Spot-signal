"""Phase 3 edge-pocket validation for Setup A variants (research-only)."""

from __future__ import annotations

import csv
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from bot.backtest import TradeRecord
from research.backtests.options_setups_comparison.indicators_ext import (
    is_expiry_day,
    time_bucket,
    weekday_name,
)
from research.backtests.options_setups_comparison.robustness import DATA_LABEL, _concentration

VWAP_ERA_CUTOFF = "2026-04-01"
MIN_TRADES_STANDARD = 30
CANDIDATE_MIN_EXPECTANCY = 0.05


@dataclass(frozen=True)
class VariantDef:
    variant_id: str
    name: str
    description: str
    filter_fn: Callable[[TradeRecord], bool]


def _tb(t: TradeRecord) -> str:
    return time_bucket(t.entry_time.isoformat())


VARIANTS: list[VariantDef] = [
    VariantDef("A0", "original", "Full Setup A (no filter)", lambda t: True),
    VariantDef("A1", "pe_only", "PE trades only", lambda t: t.side == "PE"),
    VariantDef("A2", "ce_only", "CE trades only", lambda t: t.side == "CE"),
    VariantDef("A3", "tuesday_expiry_heuristic", "Tuesday expiry-heuristic only", lambda t: is_expiry_day(t.session_date)),
    VariantDef("A4", "non_tuesday", "Exclude Tuesday", lambda t: not is_expiry_day(t.session_date)),
    VariantDef("A5", "morning_0930_1100", "Entry 09:30-11:00 only", lambda t: _tb(t) == "09:30-11:00"),
    VariantDef("A6", "mid_1100_1300", "Entry 11:00-13:00 only", lambda t: _tb(t) == "11:00-13:00"),
    VariantDef("A7", "afternoon_1300_1515", "Entry 13:00-15:15 only", lambda t: _tb(t) == "13:00-15:15"),
    VariantDef("A8", "pe_tuesday", "PE + Tuesday heuristic", lambda t: t.side == "PE" and is_expiry_day(t.session_date)),
    VariantDef("A9", "pe_morning", "PE + 09:30-11:00", lambda t: t.side == "PE" and _tb(t) == "09:30-11:00"),
    VariantDef(
        "A10", "pe_tuesday_morning",
        "PE + Tuesday + 09:30-11:00",
        lambda t: t.side == "PE" and is_expiry_day(t.session_date) and _tb(t) == "09:30-11:00",
    ),
    VariantDef("A11", "exclude_monday", "Exclude Monday entries", lambda t: weekday_name(t.session_date) != "Monday"),
    VariantDef("A12", "exclude_afternoon", "Exclude 13:00-15:15 entries", lambda t: _tb(t) != "13:00-15:15"),
]


@dataclass
class SlippageSlice:
    tier: str
    trades: list[TradeRecord]

    @property
    def net_r(self) -> float:
        return sum(t.r_multiple for t in self.trades)

    @property
    def expectancy(self) -> float:
        return statistics.mean([t.r_multiple for t in self.trades]) if self.trades else 0.0

    @property
    def median_r(self) -> float:
        return statistics.median([t.r_multiple for t in self.trades]) if self.trades else 0.0

    @property
    def win_rate_pct(self) -> float:
        if not self.trades:
            return 0.0
        wins = sum(1 for t in self.trades if t.r_multiple > 0)
        return 100.0 * wins / len(self.trades)

    @property
    def profit_factor(self) -> float | None:
        wins = sum(t.r_multiple for t in self.trades if t.r_multiple > 0)
        losses = abs(sum(t.r_multiple for t in self.trades if t.r_multiple < 0))
        return wins / losses if losses > 0 else None

    @property
    def max_drawdown_r(self) -> float:
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in self.trades:
            equity += t.r_multiple
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return max_dd

    @property
    def avg_winner(self) -> float:
        w = [t.r_multiple for t in self.trades if t.r_multiple > 0]
        return statistics.mean(w) if w else 0.0

    @property
    def avg_loser(self) -> float:
        l = [t.r_multiple for t in self.trades if t.r_multiple < 0]
        return statistics.mean(l) if l else 0.0


def _period_net(trades: list[TradeRecord], pred: Callable[[TradeRecord], bool]) -> dict[str, float]:
    subset = [t for t in trades if pred(t)]
    return {
        "trades": len(subset),
        "net_r": round(sum(t.r_multiple for t in subset), 2),
        "expectancy": round(statistics.mean([t.r_multiple for t in subset]), 3) if subset else 0.0,
    }


def _split_stats(trades: list[TradeRecord], sessions: list[str]) -> dict[str, dict[str, float]]:
    if not sessions:
        return {}
    mid = len(sessions) // 2
    first_dates = set(sessions[:mid])
    return {
        "first_half": _period_net(trades, lambda t: t.session_date in first_dates),
        "second_half": _period_net(trades, lambda t: t.session_date not in first_dates),
        "pre_apr_2026_twap_proxy": _period_net(trades, lambda t: t.session_date < VWAP_ERA_CUTOFF),
        "post_apr_2026_futures_vwap": _period_net(trades, lambda t: t.session_date >= VWAP_ERA_CUTOFF),
    }


def _side_split(trades: list[TradeRecord]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for side in ("CE", "PE"):
        sub = [t for t in trades if t.side == side]
        if sub:
            out[side] = {
                "trades": len(sub),
                "net_r": round(sum(t.r_multiple for t in sub), 2),
                "expectancy": round(statistics.mean([t.r_multiple for t in sub]), 3),
            }
    return out


def _time_split(trades: list[TradeRecord]) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[TradeRecord]] = {}
    for t in trades:
        buckets.setdefault(_tb(t), []).append(t)
    return {
        k: {
            "trades": len(v),
            "net_r": round(sum(t.r_multiple for t in v), 2),
            "expectancy": round(statistics.mean([t.r_multiple for t in v]), 3),
        }
        for k, v in sorted(buckets.items())
    }


def evaluate_variant(
    vdef: VariantDef,
    slices: dict[str, SlippageSlice],
    all_trades_by_tier: dict[str, list[TradeRecord]],
    sessions: list[str],
) -> dict[str, Any]:
    """Apply decision criteria; conservative slippage is primary."""
    cons = slices["conservative"]
    stress = slices["stress"]
    none = slices["none"]
    filtered_cons = cons.trades
    n = len(filtered_cons)

    conc = _concentration(filtered_cons)
    periods = _split_stats(filtered_cons, sessions)
    outliers = conc.get("top3_pct", 0) > 50

    status = "candidate"
    reasons: list[str] = []
    exploratory = n < MIN_TRADES_STANDARD

    if exploratory:
        status = "exploratory"
        reasons.append(f"trades={n} < {MIN_TRADES_STANDARD}")
    if cons.net_r < 0:
        status = "rejected"
        reasons.append("negative under conservative slippage")
    if stress.net_r < 0 and status != "rejected":
        status = "fragile"
        reasons.append("stress slippage negative")
    if not exploratory and status == "candidate":
        if cons.expectancy <= CANDIDATE_MIN_EXPECTANCY:
            status = "fragile"
            reasons.append(f"conservative expectancy {cons.expectancy:.3f} <= {CANDIDATE_MIN_EXPECTANCY}")
        if periods.get("first_half", {}).get("net_r", 0) <= 0 or periods.get("second_half", {}).get("net_r", 0) <= 0:
            status = "fragile"
            reasons.append("not positive in both halves")
        if outliers:
            status = "fragile"
            reasons.append(f"top3 concentration {conc.get('top3_pct', 0):.1f}%")
        if cons.max_drawdown_r > abs(cons.net_r) * 2 and cons.net_r > 0:
            status = "fragile"
            reasons.append("drawdown large vs net R")

    excluded = len(all_trades_by_tier["conservative"]) - n

    return {
        "variant_id": vdef.variant_id,
        "name": vdef.name,
        "description": vdef.description,
        "status": status,
        "decision_reasons": reasons,
        "exploratory": exploratory,
        "trades": n,
        "excluded_trades": excluded,
        "net_r_none": round(none.net_r, 2),
        "net_r_conservative": round(cons.net_r, 2),
        "net_r_stress": round(stress.net_r, 2),
        "expectancy_conservative": round(cons.expectancy, 3),
        "profit_factor_conservative": round(cons.profit_factor, 2) if cons.profit_factor else None,
        "max_drawdown_r_conservative": round(cons.max_drawdown_r, 2),
        "win_rate_conservative_pct": round(cons.win_rate_pct, 1),
        "median_r_conservative": round(cons.median_r, 3),
        "avg_winner_conservative": round(cons.avg_winner, 3),
        "avg_loser_conservative": round(cons.avg_loser, 3),
        "top20pct_contribution_conservative": round(conc.get("top20pct_trades_pct", 0), 1),
        "top3_contribution_conservative": round(conc.get("top3_pct", 0), 1),
        "periods_conservative": periods,
        "ce_pe_split_conservative": _side_split(filtered_cons),
        "time_split_conservative": _time_split(filtered_cons),
    }


def filter_trades(trades: list[TradeRecord], vdef: VariantDef) -> list[TradeRecord]:
    return [t for t in trades if vdef.filter_fn(t)]


def build_final_verdict(results: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [r for r in results if r["status"] == "candidate"]
    candidates.sort(key=lambda r: (r["expectancy_conservative"], r["net_r_conservative"]), reverse=True)

    a0 = next((r for r in results if r["variant_id"] == "A0"), None)
    a0_rejected = a0 is not None and a0["status"] in ("rejected", "fragile")

    best = candidates[0] if candidates else None
    paper_trade = best is not None and best["variant_id"] != "A0"

    if best:
        recommendation = (
            f"CANDIDATE: {best['variant_id']} ({best['name']}) — "
            f"conservative expectancy {best['expectancy_conservative']:.3f}R, "
            f"net R {best['net_r_conservative']:.2f} over {best['trades']} trades."
        )
    elif any(r["status"] == "fragile" and r["net_r_conservative"] > 0 for r in results):
        recommendation = "NO_CANDIDATE — some pockets positive but fail robustness gates; paper-trade none."
    else:
        recommendation = "NO_CANDIDATE — all variants rejected or exploratory under conservative slippage."

    return {
        "best_candidate": best["variant_id"] if best else None,
        "best_candidate_detail": best,
        "original_a0_rejected": a0_rejected,
        "a0_status": a0["status"] if a0 else "unknown",
        "narrowed_pocket_deserves_paper_trade": paper_trade,
        "recommendation": recommendation,
    }


def write_edge_pocket_outputs(
    output_dir: Path,
    results: list[dict[str, Any]],
    verdict: dict[str, Any],
    meta: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "data_label": DATA_LABEL,
        "option_premium": False,
        "research_only": True,
        "warning": (
            "All results are UNDERLYING SPOT PROXY only. "
            "No option premium P&L. Tuesday expiry is a heuristic (weekday=Tuesday), not exchange calendar."
        ),
        "validation": {
            "lookahead_bias": "none — production process_bar bar-close only, indicators precomputed without future bars",
            "vwap_pre_apr_2026": "TWAP-proxy VWAP (index volume=0 in cache)",
            "vwap_post_apr_2026": "futures-derived VWAP",
            "tuesday_expiry": "heuristic only — NIFTY weekly expiry approximated as Tuesday",
            "skipped_trades": "post-hoc filter excludes non-matching baseline trades; see excluded_trades per variant",
            "production_untouched": True,
        },
        "meta": meta,
        "verdict": verdict,
        "variants": results,
    }
    (output_dir / "edge_pockets_summary.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8",
    )

    fields = [
        "variant_id", "name", "status", "trades", "net_r_none", "net_r_conservative",
        "net_r_stress", "expectancy_conservative", "profit_factor_conservative",
        "max_drawdown_r_conservative", "win_rate_conservative_pct", "median_r_conservative",
        "top20pct_contribution_conservative", "decision_reasons",
    ]
    with (output_dir / "edge_pockets_table.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in results:
            row = dict(r)
            row["decision_reasons"] = "; ".join(r.get("decision_reasons", []))
            w.writerow(row)

    lines = [
        "# Setup A Edge-Pocket Validation (Phase 3)",
        "",
        f"**{DATA_LABEL}**",
        "",
        meta.get("warning", payload["warning"]),
        "",
        f"## Verdict",
        "",
        f"**{verdict['recommendation']}**",
        "",
        f"- Original A0 rejected/fragile: **{verdict['original_a0_rejected']}** (status: {verdict['a0_status']})",
        f"- Best candidate: **{verdict['best_candidate'] or 'none'}**",
        f"- Narrowed pocket deserves paper-trade: **{verdict['narrowed_pocket_deserves_paper_trade']}**",
        "",
        "## Validation",
        "",
    ]
    for k, v in payload["validation"].items():
        lines.append(f"- {k}: {v}")

    lines.extend(["", "## Variants (conservative slippage primary)", ""])
    lines.append(
        "| ID | Trades | NetR(none/cons/stress) | Exp(cons) | PF | Status |"
    )
    lines.append("|---|---:|---|---:|---:|---|")
    for r in results:
        lines.append(
            f"| {r['variant_id']} | {r['trades']} | "
            f"{r['net_r_none']}/{r['net_r_conservative']}/{r['net_r_stress']} | "
            f"{r['expectancy_conservative']:.3f} | {r.get('profit_factor_conservative', '')} | {r['status']} |"
        )

    (output_dir / "edge_pockets_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
