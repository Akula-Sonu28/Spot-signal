"""Phase 2 robustness diagnostics for Setup A (research-only, underlying proxy)."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from zoneinfo import ZoneInfo

from bot.backtest import ENTRY_TYPES, EXIT_TYPES, TradeRecord, pair_trades
from bot.config import DEFAULT_CONFIG, StrategyConfig
from bot.logger import ReplayLogger, SignalEvent
from bot.replay import _compute_indicators, run_replay_fast
from bot.state import DayState, Position, PositionSide, ReplayState, make_day_state, reset_position
from bot.strategy import BarContext, _calc_stops, _check_exit_on_bar, build_bar_context, process_bar

from research.backtests.options_setups_comparison.indicators_ext import (
    is_expiry_day,
    time_bucket,
    weekday_name,
)
from research.backtests.options_setups_comparison.replay_engine import apply_slippage_to_events
from research.backtests.options_setups_comparison.slippage import SlippageModel

TZ = ZoneInfo("Asia/Kolkata")
DATA_LABEL = "UNDERLYING-PROXY / RESEARCH ONLY"

RISK_PRESETS: dict[str, tuple[float, float]] = {
    "sl_1.0_atr_tgt_1.5r": (1.0, 1.5),
    "sl_1.2_atr_tgt_1.8r": (1.2, 1.8),
    "sl_1.5_atr_tgt_2.0r": (1.5, 2.0),
}


@dataclass
class TradeDiagnostics:
    trade: TradeRecord
    mae_r: float | None = None
    mfe_r: float | None = None


@dataclass
class RobustnessReport:
    label: str
    from_date: str
    to_date: str
    sessions: int
    data_type: str = "underlying_spot_proxy"
    option_premium: bool = False
    research_only: bool = True
    total_trades: int = 0
    median_r: float = 0.0
    avg_winner_r: float = 0.0
    avg_loser_r: float = 0.0
    payoff_ratio: float | None = None
    expectancy_r: float = 0.0
    net_r: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float | None = None
    concentration: dict[str, float] = field(default_factory=dict)
    ce: dict[str, Any] = field(default_factory=dict)
    pe: dict[str, Any] = field(default_factory=dict)
    by_weekday: dict[str, dict[str, float]] = field(default_factory=dict)
    by_time_bucket: dict[str, dict[str, float]] = field(default_factory=dict)
    by_expiry: dict[str, dict[str, float]] = field(default_factory=dict)
    exit_counts: dict[str, int] = field(default_factory=dict)
    mae_mfe: dict[str, float | None] = field(default_factory=dict)
    outlier_analysis: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "data_label": DATA_LABEL,
            "from_date": self.from_date,
            "to_date": self.to_date,
            "sessions": self.sessions,
            "data_type": self.data_type,
            "option_premium": self.option_premium,
            "research_only": self.research_only,
            "total_trades": self.total_trades,
            "median_r": round(self.median_r, 3),
            "avg_winner_r": round(self.avg_winner_r, 3),
            "avg_loser_r": round(self.avg_loser_r, 3),
            "payoff_ratio": round(self.payoff_ratio, 3) if self.payoff_ratio is not None else None,
            "expectancy_r": round(self.expectancy_r, 3),
            "net_r": round(self.net_r, 2),
            "win_rate_pct": round(self.win_rate_pct, 1),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor else None,
            "concentration": {k: round(v, 3) for k, v in self.concentration.items()},
            "ce": self.ce,
            "pe": self.pe,
            "by_weekday": self.by_weekday,
            "by_time_bucket": self.by_time_bucket,
            "by_expiry": self.by_expiry,
            "exit_counts": self.exit_counts,
            "mae_mfe": self.mae_mfe,
            "outlier_analysis": self.outlier_analysis,
        }


def _config_for_preset(preset: str) -> StrategyConfig:
    atr_mult, rr = RISK_PRESETS[preset]
    return replace(DEFAULT_CONFIG, atr_sl_mult=atr_mult, rr_ratio=rr)


def run_baseline_replay(
    df: pd.DataFrame,
    cfg: StrategyConfig = DEFAULT_CONFIG,
    *,
    slippage: SlippageModel | None = None,
    delayed_entry: bool = False,
) -> ReplayLogger:
    """Run Setup A (production process_bar) with optional variants."""
    if delayed_entry:
        return _run_delayed_entry_replay(df, cfg)
    logger = run_replay_fast(df, cfg)
    if slippage and slippage.tier != "none":
        logger = apply_slippage_to_events(logger, slippage)
    return logger


def _run_delayed_entry_replay(df: pd.DataFrame, cfg: StrategyConfig) -> ReplayLogger:
    """Enter on the bar AFTER the signal bar, at that bar's open."""
    import copy

    enriched = _compute_indicators(df, cfg)
    logger = ReplayLogger()
    state = ReplayState()
    zone = ZoneInfo(cfg.timezone)
    current_session: str | None = None
    pending: dict[str, Any] | None = None

    for i, row in enriched.iterrows():
        session_date = str(row["session_date"])
        if session_date != current_session:
            current_session = session_date
            state.day = make_day_state(session_date)
            state.position = Position()
            pending = None

        bar = build_bar_context(
            index=int(i),
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

        if pending is not None and state.position.side == PositionSide.FLAT:
            side = pending["side"]
            entry = float(row["open"])
            atr = float(row["atr"]) if pd.notna(row["atr"]) else pending["atr"]
            or_high = pending["or_high"]
            or_low = pending["or_low"]
            if atr is not None and or_high is not None and or_low is not None:
                stop, target = _calc_stops(side, entry, atr, or_high, or_low, cfg)
                risk_ok = (entry - stop > 0) if side == PositionSide.CE else (stop - entry > 0)
                if risk_ok:
                    state.position.side = side
                    state.position.entry_price = entry
                    state.position.stop = stop
                    state.position.target = target
                    state.position.entry_time = bar.timestamp
                    state.position.entry_bar_index = bar.index
                    et = "BUY_CE" if side == PositionSide.CE else "BUY_PE"
                    logger.log(
                        timestamp=bar.timestamp,
                        event_type=et,
                        direction="LONG" if side == PositionSide.CE else "SHORT",
                        side=side.value,
                        price=entry,
                        stop=stop,
                        target=target,
                        reason="DELAYED_ENTRY",
                        bar_index=bar.index,
                    )
            pending = None

        pre_side = state.position.side
        probe = copy.deepcopy(state)
        probe_logger = ReplayLogger()
        process_bar(probe, bar, probe_logger, cfg)
        probe_entries = [e for e in probe_logger.events if e.event_type in ENTRY_TYPES]

        if probe_entries and pre_side == PositionSide.FLAT:
            state.day = probe.day
            e = probe_entries[-1]
            pending = {
                "side": PositionSide.CE if e.side == "CE" else PositionSide.PE,
                "atr": bar.atr,
                "or_high": state.day.or_high if state.day else None,
                "or_low": state.day.or_low if state.day else None,
            }
        else:
            process_bar(state, bar, logger, cfg)

    return logger


def _normalize_exit_reason(reason: str) -> str:
    r = reason.upper()
    if "TARGET" in r:
        return "TARGET_HIT"
    if "STOP" in r or "SL" in r:
        return "STOP_HIT"
    if "SQUARE" in r or "EOD" in r:
        return "EOD_SQUARE_OFF"
    return reason


def _side_stats(trades: list[TradeRecord]) -> dict[str, Any]:
    if not trades:
        return {"trades": 0, "net_r": 0.0, "win_rate_pct": 0.0, "avg_r": 0.0}
    rs = [t.r_multiple for t in trades]
    wins = [r for r in rs if r > 0]
    return {
        "trades": len(trades),
        "net_r": round(sum(rs), 2),
        "win_rate_pct": round(100 * len(wins) / len(rs), 1),
        "avg_r": round(statistics.mean(rs), 3),
        "median_r": round(statistics.median(rs), 3),
    }


def _bucket_stats(trades: list[TradeRecord], key_fn: Any) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[float]] = {}
    for t in trades:
        buckets.setdefault(key_fn(t), []).append(t.r_multiple)
    out: dict[str, dict[str, float]] = {}
    for k, rs in buckets.items():
        wins = [r for r in rs if r > 0]
        out[k] = {
            "trades": len(rs),
            "net_r": round(sum(rs), 2),
            "win_rate_pct": round(100 * len(wins) / len(rs), 1) if rs else 0.0,
            "avg_r": round(statistics.mean(rs), 3) if rs else 0.0,
        }
    return out


def _concentration(trades: list[TradeRecord]) -> dict[str, float]:
    if not trades:
        return {"top1_pct": 0.0, "top3_pct": 0.0, "top20pct_trades_pct": 0.0}
    rs = sorted([t.r_multiple for t in trades], reverse=True)
    net = sum(rs)
    if abs(net) < 1e-9:
        return {"top1_pct": 0.0, "top3_pct": 0.0, "top20pct_trades_pct": 0.0}
    top1 = rs[0] / net * 100
    top3 = sum(rs[:3]) / net * 100
    n_top = max(1, int(len(rs) * 0.2))
    top20 = sum(rs[:n_top]) / net * 100
    return {"top1_pct": top1, "top3_pct": top3, "top20pct_trades_pct": top20}


def _compute_mae_mfe(
    trades: list[TradeRecord],
    df: pd.DataFrame,
) -> tuple[list[TradeDiagnostics], dict[str, float | None]]:
    """MAE/MFE in R-multiples using intraday candle path."""
    ts_index = pd.to_datetime(df["timestamp"])
    diags: list[TradeDiagnostics] = []
    all_mae: list[float] = []
    all_mfe: list[float] = []

    for trade in trades:
        mask = (ts_index >= trade.entry_time) & (ts_index <= trade.exit_time)
        path = df.loc[mask]
        if path.empty:
            diags.append(TradeDiagnostics(trade=trade))
            continue
        risk = trade.risk_pts or 1e-9
        highs = path["high"].astype(float)
        lows = path["low"].astype(float)
        if trade.side == "CE":
            mae = (trade.entry_price - lows.min()) / risk
            mfe = (highs.max() - trade.entry_price) / risk
        else:
            mae = (highs.max() - trade.entry_price) / risk
            mfe = (trade.entry_price - lows.min()) / risk
        all_mae.append(mae)
        all_mfe.append(mfe)
        diags.append(TradeDiagnostics(trade=trade, mae_r=mae, mfe_r=mfe))

    summary = {
        "avg_mae_r": round(statistics.mean(all_mae), 3) if all_mae else None,
        "avg_mfe_r": round(statistics.mean(all_mfe), 3) if all_mfe else None,
        "median_mae_r": round(statistics.median(all_mae), 3) if all_mae else None,
        "median_mfe_r": round(statistics.median(all_mfe), 3) if all_mfe else None,
    }
    return diags, summary


def _outlier_analysis(trades: list[TradeRecord]) -> dict[str, Any]:
    if not trades:
        return {"outlier_dominated": False, "note": "no trades"}
    rs = [t.r_multiple for t in trades]
    net = sum(rs)
    sorted_trades = sorted(trades, key=lambda t: t.r_multiple, reverse=True)
    top1_share = sorted_trades[0].r_multiple / net * 100 if net else 0
    without_best = net - sorted_trades[0].r_multiple
    return {
        "outlier_dominated": top1_share > 50 or (net > 0 and without_best <= 0),
        "top1_trade_r": round(sorted_trades[0].r_multiple, 2),
        "top1_share_of_net_pct": round(top1_share, 1),
        "net_r_without_top1": round(without_best, 2),
        "net_r_without_top3": round(net - sum(t.r_multiple for t in sorted_trades[:3]), 2),
    }


def build_robustness_report(
    label: str,
    logger: ReplayLogger,
    sessions: list[str],
    df: pd.DataFrame,
    from_date: str,
    to_date: str,
) -> RobustnessReport:
    events = [e for e in logger.events if e.event_type in ENTRY_TYPES | EXIT_TYPES]
    trades = pair_trades(events)
    rs = [t.r_multiple for t in trades]
    wins = [t for t in trades if t.r_multiple > 0]
    losses = [t for t in trades if t.r_multiple < 0]

    avg_winner = statistics.mean([t.r_multiple for t in wins]) if wins else 0.0
    avg_loser = statistics.mean([t.r_multiple for t in losses]) if losses else 0.0
    payoff = abs(avg_winner / avg_loser) if avg_loser != 0 else None

    gross_win = sum(t.r_multiple for t in wins)
    gross_loss = abs(sum(t.r_multiple for t in losses))

    exit_counts: dict[str, int] = {"TARGET_HIT": 0, "STOP_HIT": 0, "EOD_SQUARE_OFF": 0}
    for t in trades:
        key = _normalize_exit_reason(t.exit_reason)
        exit_counts[key] = exit_counts.get(key, 0) + 1

    _, mae_mfe = _compute_mae_mfe(trades, df)
    concentration = _concentration(trades)
    outlier = _outlier_analysis(trades)

    ce_trades = [t for t in trades if t.side == "CE"]
    pe_trades = [t for t in trades if t.side == "PE"]

    return RobustnessReport(
        label=label,
        from_date=from_date,
        to_date=to_date,
        sessions=len(sessions),
        total_trades=len(trades),
        median_r=statistics.median(rs) if rs else 0.0,
        avg_winner_r=avg_winner,
        avg_loser_r=avg_loser,
        payoff_ratio=payoff,
        expectancy_r=statistics.mean(rs) if rs else 0.0,
        net_r=sum(rs),
        win_rate_pct=100 * len(wins) / len(rs) if rs else 0.0,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        concentration=concentration,
        ce=_side_stats(ce_trades),
        pe=_side_stats(pe_trades),
        by_weekday=_bucket_stats(trades, lambda t: weekday_name(t.session_date)),
        by_time_bucket=_bucket_stats(trades, lambda t: time_bucket(t.entry_time.isoformat())),
        by_expiry=_bucket_stats(
            trades,
            lambda t: "expiry" if is_expiry_day(t.session_date) else "non_expiry",
        ),
        exit_counts=exit_counts,
        mae_mfe=mae_mfe,
        outlier_analysis=outlier,
    )


def recommend_robustness(reports: dict[str, RobustnessReport]) -> str:
    """Final fragility verdict from scenario reports."""
    baseline = reports.get("baseline_current_entry")
    stress = reports.get("execution_stress_slippage")
    delayed = reports.get("execution_delayed_1bar")

    if baseline is None or baseline.total_trades < 20:
        return "NEEDS_MORE_DATA — insufficient trade sample for robustness verdict."

    flags: list[str] = []
    if baseline.expectancy_r <= 0:
        flags.append("negative baseline expectancy")
    if baseline.outlier_analysis.get("outlier_dominated"):
        flags.append("outlier-dominated returns")
    if baseline.concentration.get("top1_pct", 0) > 40:
        flags.append(f"top-1 trade = {baseline.concentration['top1_pct']:.0f}% of net R")
    if stress and stress.net_r <= 0:
        flags.append("stress slippage erases edge")
    if delayed and delayed.net_r <= 0:
        flags.append("1-bar delay erases edge")
    if baseline.payoff_ratio is not None and baseline.payoff_ratio < 1.0:
        flags.append("payoff ratio < 1")

    positive_subperiods = sum(
        1 for r in reports.values()
        if r.label.startswith("period_") and r.expectancy_r > 0
    )
    period_count = sum(1 for r in reports.values() if r.label.startswith("period_"))
    if period_count >= 2 and positive_subperiods < period_count:
        flags.append("inconsistent across sub-periods")

    if not flags and baseline.expectancy_r > 0.1 and (stress is None or stress.net_r > 0):
        return "ROBUST_ENOUGH_TO_PAPER_TRADE — edge survives slippage stress on extended proxy sample; paper-trade 20 sessions before live capital."

    if len(flags) >= 3 or (baseline.net_r > 0 and baseline.outlier_analysis.get("net_r_without_top1", 0) <= 0):
        return f"LIKELY_FRAGILE — {'; '.join(flags)}."

    if baseline.expectancy_r > 0 and stress and stress.net_r > 0:
        return f"NEEDS_MORE_DATA — positive but with concerns: {'; '.join(flags) or 'marginal concentration'}."

    return f"REJECT — {'; '.join(flags) or 'negative expectancy on proxy sample'}."


def write_robustness_outputs(
    output_dir: Path,
    reports: dict[str, RobustnessReport],
    trade_diags: dict[str, list[TradeDiagnostics]],
    recommendation: str,
    meta: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "data_label": DATA_LABEL,
        "option_premium": False,
        "research_only": True,
        "meta": meta,
        "recommendation": recommendation,
        "scenarios": {k: v.to_dict() for k, v in reports.items()},
    }
    (output_dir / "robustness_summary.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8",
    )

    lines = [
        f"# Setup A Robustness Diagnostics",
        "",
        f"**{DATA_LABEL}**",
        "",
        f"**Recommendation:** {recommendation}",
        "",
        "## Scenarios",
        "",
    ]
    for key, r in reports.items():
        lines.append(f"### {key}")
        lines.append(f"- Trades: {r.total_trades} | Net R: {r.net_r:.2f} | Expectancy: {r.expectancy_r:.3f}")
        lines.append(f"- Median R: {r.median_r:.3f} | Payoff: {r.payoff_ratio} | PF: {r.profit_factor}")
        lines.append(f"- Concentration top1/top3/top20%: {r.concentration}")
        lines.append(f"- Exits: {r.exit_counts}")
        lines.append(f"- Outliers: {r.outlier_analysis}")
        lines.append("")

    (output_dir / "robustness_report.md").write_text("\n".join(lines), encoding="utf-8")

    import csv

    for key, diags in trade_diags.items():
        if not diags:
            continue
        path = output_dir / f"trades_mae_mfe_{key}.csv"
        fields = [
            "session_date", "side", "entry_time", "exit_time", "r_multiple",
            "exit_reason", "mae_r", "mfe_r",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for d in diags:
                t = d.trade
                w.writerow({
                    "session_date": t.session_date,
                    "side": t.side,
                    "entry_time": t.entry_time.isoformat(),
                    "exit_time": t.exit_time.isoformat(),
                    "r_multiple": round(t.r_multiple, 2),
                    "exit_reason": t.exit_reason,
                    "mae_r": round(d.mae_r, 3) if d.mae_r is not None else "",
                    "mfe_r": round(d.mfe_r, 3) if d.mfe_r is not None else "",
                })
