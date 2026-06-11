"""Spot backtest analytics from replay event logs."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from bot.logger import ReplayLogger, SignalEvent

ENTRY_TYPES = frozenset({"BUY_CE", "BUY_PE"})
EXIT_TYPES = frozenset({"TARGET_CE", "TARGET_PE", "SL_CE", "SL_PE", "SQUARE_OFF"})


@dataclass
class TradeRecord:
    session_date: str
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    stop: float
    target: float
    exit_reason: str
    pnl_pts: float
    risk_pts: float
    r_multiple: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date,
            "side": self.side,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "stop": self.stop,
            "target": self.target,
            "exit_reason": self.exit_reason,
            "pnl_pts": round(self.pnl_pts, 2),
            "risk_pts": round(self.risk_pts, 2),
            "r_multiple": round(self.r_multiple, 2),
        }


@dataclass
class BacktestSummary:
    sessions: int = 0
    sessions_with_trades: int = 0
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakeven: int = 0
    win_rate_pct: float = 0.0
    total_pnl_pts: float = 0.0
    avg_pnl_pts: float = 0.0
    avg_winner_pts: float = 0.0
    avg_loser_pts: float = 0.0
    profit_factor: float | None = None
    max_drawdown_pts: float = 0.0
    trades: list[TradeRecord] = field(default_factory=list)
    skipped_sessions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "sessions_with_trades": self.sessions_with_trades,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "breakeven": self.breakeven,
            "win_rate_pct": round(self.win_rate_pct, 1),
            "total_pnl_pts": round(self.total_pnl_pts, 2),
            "avg_pnl_pts": round(self.avg_pnl_pts, 2),
            "avg_winner_pts": round(self.avg_winner_pts, 2),
            "avg_loser_pts": round(self.avg_loser_pts, 2),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor is not None else None,
            "max_drawdown_pts": round(self.max_drawdown_pts, 2),
            "trades": [t.to_dict() for t in self.trades],
            "skipped_sessions": self.skipped_sessions,
        }


def _session_date(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d")


def _trade_pnl(side: str, entry: float, exit_price: float) -> float:
    if side == "CE":
        return exit_price - entry
    return entry - exit_price


def pair_trades(events: list[SignalEvent]) -> list[TradeRecord]:
    """Pair BUY events with the next exit for the same side."""
    trades: list[TradeRecord] = []
    open_trade: dict[str, Any] | None = None

    for event in events:
        et = event.event_type
        if et in ENTRY_TYPES:
            if open_trade is not None:
                continue
            open_trade = {
                "side": event.side,
                "entry_time": event.timestamp,
                "entry_price": event.price,
                "stop": event.stop,
                "target": event.target,
            }
            continue

        if et not in EXIT_TYPES or open_trade is None:
            continue

        side = open_trade["side"]
        if not et.endswith(f"_{side}") and et != "SQUARE_OFF":
            continue
        if et == "SQUARE_OFF" and event.side != side:
            continue

        entry = float(open_trade["entry_price"])
        stop = float(open_trade["stop"] or event.stop or entry)
        target = float(open_trade["target"] or event.target or entry)
        exit_price = float(event.price)
        risk = abs(entry - stop) or 1e-9
        pnl = _trade_pnl(side, entry, exit_price)
        trades.append(
            TradeRecord(
                session_date=_session_date(open_trade["entry_time"]),
                side=side,
                entry_time=open_trade["entry_time"],
                exit_time=event.timestamp,
                entry_price=entry,
                exit_price=exit_price,
                stop=stop,
                target=target,
                exit_reason=event.reason or et,
                pnl_pts=pnl,
                risk_pts=risk,
                r_multiple=pnl / risk,
            )
        )
        open_trade = None

    return trades


def summarize_trades(
    trades: list[TradeRecord],
    *,
    sessions: int,
    skipped_sessions: list[str] | None = None,
) -> BacktestSummary:
    if not trades:
        return BacktestSummary(
            sessions=sessions,
            skipped_sessions=skipped_sessions or [],
        )

    wins = [t for t in trades if t.pnl_pts > 0]
    losses = [t for t in trades if t.pnl_pts < 0]
    flat = [t for t in trades if t.pnl_pts == 0]
    total_pnl = sum(t.pnl_pts for t in trades)
    gross_win = sum(t.pnl_pts for t in wins)
    gross_loss = abs(sum(t.pnl_pts for t in losses))

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.pnl_pts
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    session_dates = {t.session_date for t in trades}
    return BacktestSummary(
        sessions=sessions,
        sessions_with_trades=len(session_dates),
        total_trades=len(trades),
        wins=len(wins),
        losses=len(losses),
        breakeven=len(flat),
        win_rate_pct=100.0 * len(wins) / len(trades),
        total_pnl_pts=total_pnl,
        avg_pnl_pts=total_pnl / len(trades),
        avg_winner_pts=gross_win / len(wins) if wins else 0.0,
        avg_loser_pts=gross_loss / len(losses) if losses else 0.0,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        max_drawdown_pts=max_dd,
        trades=trades,
        skipped_sessions=skipped_sessions or [],
    )


def run_backtest(logger: ReplayLogger, session_dates: list[str] | None = None) -> BacktestSummary:
    """Analyze replay events; optionally filter to specific session dates."""
    events = logger.events
    if session_dates is not None:
        allowed = set(session_dates)
        events = [e for e in events if _session_date(e.timestamp) in allowed]

    trade_events = [e for e in events if e.event_type in ENTRY_TYPES | EXIT_TYPES]
    trades = pair_trades(trade_events)
    sessions = len(session_dates) if session_dates is not None else len({_session_date(e.timestamp) for e in events})
    return summarize_trades(trades, sessions=sessions)


def format_report(summary: BacktestSummary, *, title: str = "Backtest Report") -> str:
    lines = [
        "=" * 60,
        title,
        "=" * 60,
        f"Sessions:           {summary.sessions} ({summary.sessions_with_trades} with trades)",
        f"Total trades:       {summary.total_trades}",
        f"Wins / Losses:      {summary.wins} / {summary.losses} (BE: {summary.breakeven})",
        f"Win rate:           {summary.win_rate_pct:.1f}%",
        f"Total P&L (spot):   {summary.total_pnl_pts:+.2f} pts",
        f"Avg P&L / trade:    {summary.avg_pnl_pts:+.2f} pts",
        f"Avg winner:         {summary.avg_winner_pts:+.2f} pts",
        f"Avg loser:          {-summary.avg_loser_pts:.2f} pts",
    ]
    if summary.profit_factor is not None:
        lines.append(f"Profit factor:      {summary.profit_factor:.2f}")
    lines.append(f"Max drawdown:       {summary.max_drawdown_pts:.2f} pts")
    if summary.skipped_sessions:
        lines.append(f"Skipped (no data):  {', '.join(summary.skipped_sessions)}")

    if summary.trades:
        lines.append("")
        lines.append("--- Trades ---")
        for t in summary.trades:
            lines.append(
                f"  {t.session_date} {t.side:2} "
                f"{t.entry_time.strftime('%H:%M')}->{t.exit_time.strftime('%H:%M')} "
                f"entry={t.entry_price:.2f} exit={t.exit_price:.2f} "
                f"pnl={t.pnl_pts:+.2f} ({t.r_multiple:+.2f}R) [{t.exit_reason}]"
            )

    lines.append("=" * 60)
    lines.append("Note: Spot points only — not option premium P&L.")
    return "\n".join(lines)
