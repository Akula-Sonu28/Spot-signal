"""Extended metrics for options setups research."""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from bot.backtest import TradeRecord, pair_trades, summarize_trades
from bot.logger import ReplayLogger

from research.backtests.options_setups_comparison.indicators_ext import (
    is_expiry_day,
    time_bucket,
    weekday_name,
)
from research.backtests.options_setups_comparison.risk_engine import SkippedLogger, SkippedTrade


@dataclass
class SetupMetrics:
    setup_id: str
    setup_name: str
    slippage_tier: str
    data_type: str = "underlying_spot_proxy"
    option_premium: bool = False
    trading_days: int = 0
    total_trades: int = 0
    avg_trades_per_day: float = 0.0
    win_rate_pct: float = 0.0
    avg_r: float = 0.0
    median_r: float = 0.0
    gross_profit_r: float = 0.0
    gross_loss_r: float = 0.0
    net_r: float = 0.0
    profit_factor: float | None = None
    max_drawdown_r: float = 0.0
    max_losing_streak: int = 0
    best_trade_r: float = 0.0
    worst_trade_r: float = 0.0
    expectancy_per_trade: float = 0.0
    expectancy_per_day: float = 0.0
    ce_trades: int = 0
    ce_net_r: float = 0.0
    pe_trades: int = 0
    pe_net_r: float = 0.0
    by_weekday: dict[str, dict[str, float]] = field(default_factory=dict)
    by_time_bucket: dict[str, dict[str, float]] = field(default_factory=dict)
    by_volatility_regime: dict[str, dict[str, float]] = field(default_factory=dict)
    expiry_days: dict[str, float] = field(default_factory=dict)
    skipped_count: int = 0
    trades: list[TradeRecord] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "setup_name": self.setup_name,
            "slippage_tier": self.slippage_tier,
            "data_type": self.data_type,
            "option_premium": self.option_premium,
            "trading_days": self.trading_days,
            "total_trades": self.total_trades,
            "avg_trades_per_day": round(self.avg_trades_per_day, 3),
            "win_rate_pct": round(self.win_rate_pct, 1),
            "avg_r": round(self.avg_r, 3),
            "median_r": round(self.median_r, 3),
            "gross_profit_r": round(self.gross_profit_r, 2),
            "gross_loss_r": round(self.gross_loss_r, 2),
            "net_r": round(self.net_r, 2),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor else None,
            "max_drawdown_r": round(self.max_drawdown_r, 2),
            "max_losing_streak": self.max_losing_streak,
            "best_trade_r": round(self.best_trade_r, 2),
            "worst_trade_r": round(self.worst_trade_r, 2),
            "expectancy_per_trade": round(self.expectancy_per_trade, 3),
            "expectancy_per_day": round(self.expectancy_per_day, 3),
            "ce_trades": self.ce_trades,
            "ce_net_r": round(self.ce_net_r, 2),
            "pe_trades": self.pe_trades,
            "pe_net_r": round(self.pe_net_r, 2),
            "by_weekday": self.by_weekday,
            "by_time_bucket": self.by_time_bucket,
            "by_volatility_regime": self.by_volatility_regime,
            "expiry_days": self.expiry_days,
            "skipped_count": self.skipped_count,
        }


def _max_losing_streak(trades: list[TradeRecord]) -> int:
    streak = 0
    best = 0
    for t in trades:
        if t.r_multiple < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    return best


def _bucket_stats(trades: list[TradeRecord], key_fn: Any) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[float]] = {}
    for t in trades:
        k = key_fn(t)
        buckets.setdefault(k, []).append(t.r_multiple)
    out: dict[str, dict[str, float]] = {}
    for k, rs in buckets.items():
        out[k] = {
            "trades": len(rs),
            "net_r": round(sum(rs), 2),
            "win_rate_pct": round(100 * sum(1 for r in rs if r > 0) / len(rs), 1),
        }
    return out


def _volatility_regime(trade: TradeRecord) -> str:
    """Proxy: use risk_pts as volatility proxy tercile labels."""
    if trade.risk_pts < 25:
        return "low_vol"
    if trade.risk_pts < 40:
        return "mid_vol"
    return "high_vol"


def compute_setup_metrics(
    setup_id: str,
    setup_name: str,
    slippage_tier: str,
    logger: ReplayLogger,
    skipped: SkippedLogger,
    session_dates: list[str],
) -> SetupMetrics:
    trade_events = [e for e in logger.events if e.event_type.startswith("BUY_") or "SL_" in e.event_type or "TARGET_" in e.event_type or e.event_type == "SQUARE_OFF"]
    trades = pair_trades(trade_events)
    summary = summarize_trades(trades, sessions=len(session_dates))

    rs = [t.r_multiple for t in trades]
    wins_r = [r for r in rs if r > 0]
    losses_r = [r for r in rs if r < 0]

    equity = 0.0
    peak = 0.0
    max_dd_r = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd_r = max(max_dd_r, peak - equity)

    ce_trades = [t for t in trades if t.side == "CE"]
    pe_trades = [t for t in trades if t.side == "PE"]

    expiry_net: dict[str, float] = {"expiry": 0.0, "non_expiry": 0.0}
    for t in trades:
        key = "expiry" if is_expiry_day(t.session_date) else "non_expiry"
        expiry_net[key] += t.r_multiple

    m = SetupMetrics(
        setup_id=setup_id,
        setup_name=setup_name,
        slippage_tier=slippage_tier,
        trading_days=len(session_dates),
        total_trades=len(trades),
        avg_trades_per_day=len(trades) / len(session_dates) if session_dates else 0.0,
        win_rate_pct=summary.win_rate_pct,
        avg_r=statistics.mean(rs) if rs else 0.0,
        median_r=statistics.median(rs) if rs else 0.0,
        gross_profit_r=sum(wins_r),
        gross_loss_r=abs(sum(losses_r)),
        net_r=sum(rs),
        profit_factor=summary.profit_factor,
        max_drawdown_r=max_dd_r,
        max_losing_streak=_max_losing_streak(trades),
        best_trade_r=max(rs) if rs else 0.0,
        worst_trade_r=min(rs) if rs else 0.0,
        expectancy_per_trade=statistics.mean(rs) if rs else 0.0,
        expectancy_per_day=sum(rs) / len(session_dates) if session_dates else 0.0,
        ce_trades=len(ce_trades),
        ce_net_r=sum(t.r_multiple for t in ce_trades),
        pe_trades=len(pe_trades),
        pe_net_r=sum(t.r_multiple for t in pe_trades),
        by_weekday=_bucket_stats(trades, lambda t: weekday_name(t.session_date)),
        by_time_bucket=_bucket_stats(trades, lambda t: time_bucket(t.entry_time.isoformat())),
        by_volatility_regime=_bucket_stats(trades, _volatility_regime),
        expiry_days={k: round(v, 2) for k, v in expiry_net.items()},
        skipped_count=len(skipped.entries),
        trades=trades,
    )
    return m


def write_trades_csv(trades: list[TradeRecord], path: Any) -> None:
    import csv
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "session_date", "side", "entry_time", "exit_time", "entry_price", "exit_price",
        "stop", "target", "exit_reason", "pnl_pts", "risk_pts", "r_multiple",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for t in trades:
            w.writerow(t.to_dict())


def write_skipped_csv(skipped: list[SkippedTrade], path: Any) -> None:
    import csv
    from dataclasses import asdict
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["session_date", "bar_index", "setup_id", "side", "reason"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for s in skipped:
            w.writerow(asdict(s))


def write_equity_curve(trades: list[TradeRecord], path: Any) -> None:
    import csv
    from pathlib import Path

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    cum = 0.0
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["trade_num", "session_date", "r_multiple", "cumulative_r"])
        w.writeheader()
        for n, t in enumerate(trades, 1):
            cum += t.r_multiple
            w.writerow({
                "trade_num": n,
                "session_date": t.session_date,
                "r_multiple": round(t.r_multiple, 2),
                "cumulative_r": round(cum, 2),
            })
