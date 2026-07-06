"""Spot vs. option premium diagnostic engine — chop, wick, and decay tracking.

Phase 1 only: informational logging and replay analysis. Does not alter entries/exits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from bot.backtest import ENTRY_TYPES, EXIT_TYPES, TradeRecord, pair_trades
from bot.config import StrategyConfig
from bot.logger import ReplayLogger, SignalEvent
from bot.state import Position, PositionSide

# ponytail: fixed thresholds; tune via CLI flags on chop report script
DEFAULT_CHOP_MIN_BARS = 4
DEFAULT_CHOP_ATR_BAND = 0.3

POSITION_PREMIUM_EVENT = "POSITION_PREMIUM"

# Codebase audit — structural spot-vs-option gaps (see prompt.txt objectives)
SPOT_OPTION_GAPS: dict[str, list[str]] = {
    "time_in_trade": [
        "bot/strategy.py::_check_exit_on_bar — exits only on SL/target/square-off; no velocity/chop exit",
        "bot/combined.py::process_session_bar — open positions pass through bar-by-bar with no time-decay guard",
        "bot/strategy_j.py — J+ trap entries share the same exit path; no theta-aware hold limit",
    ],
    "wick_risk": [
        "bot/strategy.py::_check_exit_on_bar — CLOSE_ONLY_SL uses bar.close for SL, not bar.low/high",
        "bot/strategy.py::_check_exit_on_bar — targets still use bar.high/low (wick can hit target)",
        "bot/config.py — close_only_sl defaults True; mid-candle adverse wicks ignored for SL",
    ],
    "expiry_gamma": [
        "bot/strategy.py::_calc_stop — sl_buffer_pts fixed; no expiry-day widening",
        "bot/option_lookup.py::nearest_weekly_expiry — picks nearest expiry but strategy never tags expiry days",
        "bot/stops.py — v38_stops/capped_stops use same ATR mult on expiry and non-expiry sessions",
    ],
}


@dataclass
class BarChopState:
    """Per-bar chop metrics while a trade is open."""

    bar_index: int
    timestamp: datetime
    close: float
    atr: float | None
    bars_in_trade: int
    spot_from_entry: float
    in_chop_band: bool
    wick_would_sl: bool
    chop_streak: int


@dataclass
class TradeChopMetrics:
    """Aggregated chop / wick diagnostics for one paired trade."""

    session_date: str
    side: str
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    pnl_pts: float
    is_win: bool
    bars_in_trade: int
    max_chop_streak: int
    chop_bars: int
    high_theta_decay_risk: bool
    wick_sl_bars: int
    is_expiry_day: bool
    max_spot_adverse_pts: float
    max_premium_drawdown_pct: float | None = None
    premium_bleed_worse_than_spot: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date,
            "side": self.side,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat(),
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "pnl_pts": round(self.pnl_pts, 2),
            "is_win": self.is_win,
            "bars_in_trade": self.bars_in_trade,
            "max_chop_streak": self.max_chop_streak,
            "chop_bars": self.chop_bars,
            "high_theta_decay_risk": self.high_theta_decay_risk,
            "wick_sl_bars": self.wick_sl_bars,
            "is_expiry_day": self.is_expiry_day,
            "max_spot_adverse_pts": round(self.max_spot_adverse_pts, 2),
            "max_premium_drawdown_pct": (
                round(self.max_premium_drawdown_pct, 1) if self.max_premium_drawdown_pct is not None else None
            ),
            "premium_bleed_worse_than_spot": self.premium_bleed_worse_than_spot,
        }


@dataclass
class ChopSummary:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    high_decay_risk: int = 0
    wins_in_chop: int = 0
    losses_in_chop: int = 0
    wick_sl_bars_total: int = 0
    expiry_day_trades: int = 0
    premium_bleed_flags: int = 0
    trades: list[TradeChopMetrics] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "high_decay_risk": self.high_decay_risk,
            "wins_in_chop": self.wins_in_chop,
            "losses_in_chop": self.losses_in_chop,
            "wick_sl_bars_total": self.wick_sl_bars_total,
            "expiry_day_trades": self.expiry_day_trades,
            "premium_bleed_flags": self.premium_bleed_flags,
            "trades": [t.to_dict() for t in self.trades],
        }


def is_expiry_session(session_date: str, expiry_iso: str | None) -> bool:
    """True when session date matches the held option expiry (gamma elevated)."""
    if not expiry_iso:
        return False
    try:
        return session_date == expiry_iso[:10]
    except (TypeError, ValueError):
        return False


def _spot_adverse_pts(side: str, entry: float, price: float) -> float:
    if side == "CE":
        return max(0.0, entry - price)
    return max(0.0, price - entry)


def _wick_would_hit_sl(
    side: PositionSide,
    stop: float | None,
    bar_high: float,
    bar_low: float,
    bar_close: float,
    *,
    close_only_sl: bool,
) -> bool:
    """Mid-candle wick crosses SL while close-only mode would not exit."""
    if stop is None or not close_only_sl:
        return False
    if side == PositionSide.CE:
        wick_hit = bar_low <= stop
        close_hit = bar_close <= stop
        return wick_hit and not close_hit
    wick_hit = bar_high >= stop
    close_hit = bar_close >= stop
    return wick_hit and not close_hit


def in_chop_band(spot_from_entry: float, atr: float | None, band_mult: float = DEFAULT_CHOP_ATR_BAND) -> bool:
    if atr is None or atr <= 0:
        return abs(spot_from_entry) <= 5.0  # ponytail: flat fallback when ATR missing
    return abs(spot_from_entry) <= band_mult * atr


def analyze_bar_chop(
    position: Position,
    *,
    bar_index: int,
    timestamp: datetime,
    close: float,
    high: float,
    low: float,
    atr: float | None,
    cfg: StrategyConfig,
    prior_chop_streak: int = 0,
    band_mult: float = DEFAULT_CHOP_ATR_BAND,
) -> BarChopState:
    entry = position.entry_price or close
    side = position.side
    bars_in = (
        bar_index - position.entry_bar_index
        if position.entry_bar_index is not None
        else 0
    )
    spot_delta = close - entry if side == PositionSide.CE else entry - close
    chop = in_chop_band(spot_delta, atr, band_mult)
    streak = prior_chop_streak + 1 if chop else 0
    wick = _wick_would_hit_sl(side, position.stop, high, low, close, close_only_sl=cfg.close_only_sl)
    return BarChopState(
        bar_index=bar_index,
        timestamp=timestamp,
        close=close,
        atr=atr,
        bars_in_trade=bars_in,
        spot_from_entry=spot_delta,
        in_chop_band=chop,
        wick_would_sl=wick,
        chop_streak=streak,
    )


def log_position_snapshot(
    position: Position,
    bar: Any,
    logger: ReplayLogger,
    cfg: StrategyConfig,
    *,
    chop_streak: int = 0,
    band_mult: float = DEFAULT_CHOP_ATR_BAND,
) -> SignalEvent:
    """Emit POSITION_PREMIUM diagnostic event (spot fields always; premium enriched live)."""
    state = analyze_bar_chop(
        position,
        bar_index=bar.index,
        timestamp=bar.timestamp,
        close=bar.close,
        high=bar.high,
        low=bar.low,
        atr=bar.atr,
        cfg=cfg,
        prior_chop_streak=chop_streak,
        band_mult=band_mult,
    )
    entry = position.entry_price or bar.close
    adverse = _spot_adverse_pts(position.side.value, entry, bar.close)
    position.max_spot_adverse_pts = max(position.max_spot_adverse_pts, adverse)
    high_decay = (
        state.bars_in_trade >= DEFAULT_CHOP_MIN_BARS
        and state.chop_streak >= DEFAULT_CHOP_MIN_BARS
    )
    return logger.log(
        timestamp=bar.timestamp,
        event_type=POSITION_PREMIUM_EVENT,
        direction="DIAG",
        side=position.side.value,
        price=bar.close,
        stop=position.stop,
        target=position.target,
        reason="CHOP_DECAY_DIAG",
        bar_index=bar.index,
        entry=entry,
        bars_in_trade=state.bars_in_trade,
        spot_from_entry=round(state.spot_from_entry, 2),
        in_chop_band=state.in_chop_band,
        chop_streak=state.chop_streak,
        wick_would_sl=state.wick_would_sl,
        high_theta_decay_risk=high_decay,
        max_spot_adverse_pts=round(position.max_spot_adverse_pts, 2),
        max_premium_drawdown_pct=position.max_premium_drawdown_pct or None,
        premium_bleed_worse_than_spot=position.premium_bleed_worse_than_spot,
        atr=bar.atr,
        is_expiry_day=is_expiry_session(bar.session_date, position.option_expiry),
    )


def _bars_for_trade(
    trade: TradeRecord,
    bar_rows: list[dict[str, Any]],
    entry_bar_index: int | None,
) -> list[dict[str, Any]]:
    if entry_bar_index is None:
        return [
            b
            for b in bar_rows
            if trade.entry_time <= b["timestamp"] < trade.exit_time
        ]
    return [
        b
        for b in bar_rows
        if entry_bar_index < b["index"] <= entry_bar_index + 500
        and trade.entry_time <= b["timestamp"] <= trade.exit_time
    ]


def analyze_trade_chop(
    trade: TradeRecord,
    bar_rows: list[dict[str, Any]],
    cfg: StrategyConfig,
    *,
    entry_bar_index: int | None = None,
    premium_events: list[SignalEvent] | None = None,
    chop_min_bars: int = DEFAULT_CHOP_MIN_BARS,
    band_mult: float = DEFAULT_CHOP_ATR_BAND,
) -> TradeChopMetrics:
    side = PositionSide.CE if trade.side == "CE" else PositionSide.PE
    pos = Position(
        side=side,
        entry_price=trade.entry_price,
        stop=trade.stop,
        target=trade.target,
        entry_time=trade.entry_time,
        entry_bar_index=entry_bar_index,
    )
    bars = _bars_for_trade(trade, bar_rows, entry_bar_index)
    chop_streak = 0
    max_streak = 0
    chop_bars = 0
    wick_bars = 0
    max_adverse = 0.0

    for b in bars:
        state = analyze_bar_chop(
            pos,
            bar_index=b["index"],
            timestamp=b["timestamp"],
            close=b["close"],
            high=b["high"],
            low=b["low"],
            atr=b.get("atr"),
            cfg=cfg,
            prior_chop_streak=chop_streak,
            band_mult=band_mult,
        )
        chop_streak = state.chop_streak
        max_streak = max(max_streak, chop_streak)
        if state.in_chop_band:
            chop_bars += 1
        if state.wick_would_sl:
            wick_bars += 1
        max_adverse = max(max_adverse, _spot_adverse_pts(trade.side, trade.entry_price, b["close"]))

    max_prem_dd: float | None = None
    premium_bleed = False
    if premium_events:
        dds = [
            float(e.extra.get("premium_drawdown_pct", 0))
            for e in premium_events
            if e.extra.get("premium_drawdown_pct") is not None
        ]
        if dds:
            max_prem_dd = max(dds)
            premium_bleed = any(e.extra.get("premium_bleed_worse_than_spot") for e in premium_events)

    high_decay = max_streak >= chop_min_bars and len(bars) >= chop_min_bars
    expiry = is_expiry_session(trade.session_date, None)
    if premium_events:
        exp = premium_events[0].extra.get("option_expiry")
        expiry = is_expiry_session(trade.session_date, str(exp) if exp else None)

    return TradeChopMetrics(
        session_date=trade.session_date,
        side=trade.side,
        entry_time=trade.entry_time,
        exit_time=trade.exit_time,
        entry_price=trade.entry_price,
        exit_price=trade.exit_price,
        pnl_pts=trade.pnl_pts,
        is_win=trade.pnl_pts > 0,
        bars_in_trade=len(bars),
        max_chop_streak=max_streak,
        chop_bars=chop_bars,
        high_theta_decay_risk=high_decay,
        wick_sl_bars=wick_bars,
        is_expiry_day=expiry,
        max_spot_adverse_pts=max_adverse,
        max_premium_drawdown_pct=max_prem_dd,
        premium_bleed_worse_than_spot=premium_bleed,
    )


def _index_bars(df: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for i, row in df.iterrows():
        atr_val = None
        if "atr" in row.index:
            raw = row["atr"]
            if raw == raw:  # NaN check without pandas import
                atr_val = float(raw)
        rows.append(
            {
                "index": int(i),
                "timestamp": row["timestamp"].to_pydatetime(),
                "close": float(row["close"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "atr": atr_val,
            }
        )
    return rows


def _entry_bar_indices(events: list[SignalEvent]) -> dict[tuple[str, str, datetime], int]:
    out: dict[tuple[str, str, datetime], int] = {}
    for e in events:
        if e.event_type in ("BUY_CE", "BUY_PE"):
            key = (e.event_type, e.side, e.timestamp)
            out[key] = e.bar_index
    return out


def analyze_chop_from_replay(
    logger: ReplayLogger,
    df: Any,
    cfg: StrategyConfig,
    *,
    chop_min_bars: int = DEFAULT_CHOP_MIN_BARS,
    band_mult: float = DEFAULT_CHOP_ATR_BAND,
) -> ChopSummary:
    """Run chop/decay analysis on replay events + candle dataframe."""
    bar_rows = _index_bars(df)
    trade_events = [e for e in logger.events if e.event_type in ENTRY_TYPES | EXIT_TYPES]
    trades = pair_trades(trade_events)
    entry_idx = _entry_bar_indices(logger.events)

    prem_by_session: dict[str, list[SignalEvent]] = {}
    for e in logger.events:
        if e.event_type == POSITION_PREMIUM_EVENT:
            prem_by_session.setdefault(e.timestamp.strftime("%Y-%m-%d"), []).append(e)

    metrics: list[TradeChopMetrics] = []
    for t in trades:
        key = (f"BUY_{t.side}", t.side, t.entry_time)
        ebi = entry_idx.get(key)
        prems = [e for e in prem_by_session.get(t.session_date, []) if t.entry_time <= e.timestamp <= t.exit_time]
        metrics.append(
            analyze_trade_chop(
                t,
                bar_rows,
                cfg,
                entry_bar_index=ebi,
                premium_events=prems or None,
                chop_min_bars=chop_min_bars,
                band_mult=band_mult,
            )
        )

    summary = ChopSummary(trades=metrics, total_trades=len(metrics))
    for m in metrics:
        if m.is_win:
            summary.wins += 1
        elif m.pnl_pts < 0:
            summary.losses += 1
        if m.high_theta_decay_risk:
            summary.high_decay_risk += 1
            if m.is_win:
                summary.wins_in_chop += 1
            elif m.pnl_pts < 0:
                summary.losses_in_chop += 1
        summary.wick_sl_bars_total += m.wick_sl_bars
        if m.is_expiry_day:
            summary.expiry_day_trades += 1
        if m.premium_bleed_worse_than_spot:
            summary.premium_bleed_flags += 1
    return summary


def format_audit_report() -> str:
    lines = ["=" * 60, "Spot vs. Option Structural Gap Audit", "=" * 60]
    for category, items in SPOT_OPTION_GAPS.items():
        lines.append(f"\n[{category.upper()}]")
        for item in items:
            lines.append(f"  • {item}")
    lines.append("=" * 60)
    return "\n".join(lines)


def format_chop_summary(summary: ChopSummary) -> str:
    lines = [
        "=" * 60,
        "Sideways Chop & Premium Decay Diagnostic",
        "=" * 60,
        f"Trades analyzed:        {summary.total_trades}",
        f"Wins / Losses:          {summary.wins} / {summary.losses}",
        f"High theta-decay risk:  {summary.high_decay_risk} "
        f"(wins in chop: {summary.wins_in_chop}, losses in chop: {summary.losses_in_chop})",
        f"Wick-would-SL bars:     {summary.wick_sl_bars_total} (close-only SL ignored)",
        f"Expiry-day trades:      {summary.expiry_day_trades}",
        f"Premium bleed flags:    {summary.premium_bleed_flags}",
        "",
        "--- Per-trade ---",
    ]
    for m in summary.trades:
        flag = " [HIGH THETA DECAY RISK]" if m.high_theta_decay_risk else ""
        wick = f" wick_bars={m.wick_sl_bars}" if m.wick_sl_bars else ""
        lines.append(
            f"  {m.session_date} {m.side} {m.entry_time.strftime('%H:%M')}->{m.exit_time.strftime('%H:%M')} "
            f"pnl={m.pnl_pts:+.1f} bars={m.bars_in_trade} chop_streak={m.max_chop_streak}{wick}{flag}"
        )
    lines.append("=" * 60)
    return "\n".join(lines)
