"""Algorithmic vulnerability & market-structure audit for Sgnal v3.9.

Purely diagnostic — does not alter config or execution. Stress-tests hard-coded
rule cliffs (OR 100pt boundary, ADX lag, fixed SL buffer, time-of-day, J+ trap).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from bot.backtest import ENTRY_TYPES, EXIT_TYPES, TradeRecord, pair_trades, summarize_trades
from bot.config import DEFAULT_CONFIG, CombinedStrategyConfig, StrategyConfig, load_combined_config
from bot.indicators import or_width
from bot.logger import ReplayLogger, SignalEvent
from bot.replay import _compute_indicators
from bot.state import Position, ReplayState, make_day_state
from bot.strategy import build_bar_context, update_or
from bot.strategy_j import J_TRAP_ROBUST
from zoneinfo import ZoneInfo

# Playbook cliff — OR width unstable boundary (pts)
CLIFF_LOW = 85.0
CLIFF_MID = 100.0
CLIFF_HIGH = 115.0

# ADX lag trap — flat velocity after entry
ADX_VELOCITY_BARS = 3
ADX_VELOCITY_ATR_MULT = 0.2
ADX_V38_FLOOR = 18.0
ADX_J_FLOOR = 20.0

# Fixed buffer counterfactual
DYNAMIC_BUFFER_ATR_MULT = 0.15
FIXED_BUFFER_PTS = 10.0

# Time-of-day buckets (IST, bar close minutes from midnight)
BUCKET_MORNING = (9 * 60 + 30, 11 * 60)  # 09:30–11:00
BUCKET_MIDDAY = (11 * 60, 13 * 60 + 30)  # 11:00–13:30
BUCKET_AFTERNOON = (13 * 60 + 30, 15 * 60 + 15)  # 13:30–15:15

# J+ value acceptance
J_BARS_OUTSIDE_OR_LIMIT = 2

ExitSim = Literal["SL", "TARGET", "SQUARE_OFF", "OPEN"]


def _float_or_none(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return None if f != f else f  # NaN
    except (TypeError, ValueError):
        return None


@dataclass
class SegmentStats:
    label: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl_pts: float = 0.0
    win_rate_pct: float = 0.0
    profit_factor: float | None = None
    max_drawdown_pts: float = 0.0
    sessions: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "net_pnl_pts": round(self.net_pnl_pts, 2),
            "win_rate_pct": round(self.win_rate_pct, 1),
            "profit_factor": round(self.profit_factor, 2) if self.profit_factor is not None else None,
            "max_drawdown_pts": round(self.max_drawdown_pts, 2),
            "sessions": self.sessions,
        }


@dataclass
class CliffAudit:
    zone_85_100: SegmentStats
    zone_100_115: SegmentStats
    unstable_cliff: bool
    instability_note: str
    boundary_sessions: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_85_100": self.zone_85_100.to_dict(),
            "zone_100_115": self.zone_100_115.to_dict(),
            "unstable_cliff": self.unstable_cliff,
            "instability_note": self.instability_note,
            "boundary_sessions": self.boundary_sessions,
        }


@dataclass
class AdxLagAudit:
    tagged_trades: int
    tagged_losses: int
    total_losses: int
    loss_share_pct: float
    trades: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tagged_trades": self.tagged_trades,
            "tagged_losses": self.tagged_losses,
            "total_losses": self.total_losses,
            "loss_share_pct": round(self.loss_share_pct, 1),
            "trades": self.trades,
        }


@dataclass
class BufferStopoutAudit:
    sl_exits: int
    fixed_buffer_stopouts: int
    fixed_buffer_stopouts_atr: int
    fixed_buffer_stopouts_vix: int
    vix_available: bool
    trades: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sl_exits": self.sl_exits,
            "fixed_buffer_stopouts": self.fixed_buffer_stopouts,
            "fixed_buffer_stopouts_atr": self.fixed_buffer_stopouts_atr,
            "fixed_buffer_stopouts_vix": self.fixed_buffer_stopouts_vix,
            "vix_available": self.vix_available,
            "trades": self.trades,
        }


@dataclass
class TimeBucketAudit:
    morning: SegmentStats
    midday: SegmentStats
    afternoon: SegmentStats

    def to_dict(self) -> dict[str, Any]:
        return {
            "morning": self.morning.to_dict(),
            "midday": self.midday.to_dict(),
            "afternoon": self.afternoon.to_dict(),
        }


@dataclass
class JAcceptanceAudit:
    j_trades: int
    value_acceptance_short: int
    acceptance_losses: int
    trades: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "j_trades": self.j_trades,
            "value_acceptance_short": self.value_acceptance_short,
            "acceptance_losses": self.acceptance_losses,
            "trades": self.trades,
        }


@dataclass
class StrategyAuditReport:
    cliff: CliffAudit
    adx_lag: AdxLagAudit
    buffer: BufferStopoutAudit
    time_buckets: TimeBucketAudit
    j_acceptance: JAcceptanceAudit
    total_trades: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cliff": self.cliff.to_dict(),
            "adx_lag": self.adx_lag.to_dict(),
            "buffer": self.buffer.to_dict(),
            "time_buckets": self.time_buckets.to_dict(),
            "j_acceptance": self.j_acceptance.to_dict(),
            "total_trades": self.total_trades,
        }


def _bar_close_minutes(ts: datetime, cfg: StrategyConfig) -> int:
    m = ts.hour * 60 + ts.minute
    return m + 5 if cfg.bar_time_is_open else m


def _time_bucket_label(ts: datetime, cfg: StrategyConfig) -> str | None:
    m = _bar_close_minutes(ts, cfg)
    if BUCKET_MORNING[0] <= m < BUCKET_MORNING[1]:
        return "morning"
    if BUCKET_MIDDAY[0] <= m < BUCKET_MIDDAY[1]:
        return "midday"
    if BUCKET_AFTERNOON[0] <= m < BUCKET_AFTERNOON[1]:
        return "afternoon"
    return None


def build_session_or_widths(df: Any, cfg: StrategyConfig = DEFAULT_CONFIG) -> dict[str, float]:
    """Final OR width (pts) per session_date."""
    enriched = _compute_indicators(df, cfg) if "atr" not in df.columns else df
    zone = ZoneInfo(cfg.timezone)
    widths: dict[str, float] = {}
    current: str | None = None
    state = ReplayState(position=Position(), day=None)
    final_w: float | None = None

    for i, row in enriched.iterrows():
        sd = str(row["session_date"])
        if sd != current:
            if current is not None and final_w is not None:
                widths[current] = final_w
            current = sd
            state = ReplayState(position=Position(), day=make_day_state(sd))
            final_w = None

        bar = build_bar_context(
            int(i),
            row["timestamp"].to_pydatetime(),
            sd,
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row["volume"]),
            float(row["vwap"]) if _float_or_none(row.get("vwap")) is not None else None,
            _float_or_none(row.get("atr")),
            _float_or_none(row.get("adx")),
            cfg,
            zone,
        )
        update_or(state.day, bar, cfg)
        if state.day and state.day.or_defined:
            w = or_width(state.day.or_high, state.day.or_low)
            if w is not None:
                final_w = w

    if current is not None and final_w is not None:
        widths[current] = final_w
    return widths


def _index_session_bars(df: Any) -> dict[str, list[dict[str, Any]]]:
    by_session: dict[str, list[dict[str, Any]]] = {}
    for i, row in df.iterrows():
        sd = str(row["session_date"])
        atr = _float_or_none(row.get("atr"))
        adx = _float_or_none(row.get("adx"))
        by_session.setdefault(sd, []).append(
            {
                "index": int(i),
                "timestamp": row["timestamp"].to_pydatetime(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "atr": atr,
                "adx": adx,
            }
        )
    return by_session


def _entry_event_map(events: list[SignalEvent]) -> dict[tuple[str, str, datetime], SignalEvent]:
    out: dict[tuple[str, str, datetime], SignalEvent] = {}
    for e in events:
        if e.event_type in ENTRY_TYPES:
            out[(e.event_type, e.side, e.timestamp)] = e
    return out


def _stats_from_trades(trades: list[TradeRecord], label: str, sessions: int = 0) -> SegmentStats:
    if not trades:
        return SegmentStats(label=label, sessions=sessions)
    s = summarize_trades(trades, sessions=sessions or len({t.session_date for t in trades}))
    return SegmentStats(
        label=label,
        trades=s.total_trades,
        wins=s.wins,
        losses=s.losses,
        net_pnl_pts=s.total_pnl_pts,
        win_rate_pct=s.win_rate_pct,
        profit_factor=s.profit_factor,
        max_drawdown_pts=s.max_drawdown_pts,
        sessions=s.sessions_with_trades,
    )


def audit_playbook_cliff(
    trades: list[TradeRecord],
    or_widths: dict[str, float],
) -> CliffAudit:
    """Compare 85–100 (V38 cliff) vs 100–115 (J+ cliff) boundary sessions."""
    t85_100: list[TradeRecord] = []
    t100_115: list[TradeRecord] = []
    boundary_sessions = 0

    for t in trades:
        w = or_widths.get(t.session_date)
        if w is None or w < CLIFF_LOW or w > CLIFF_HIGH:
            continue
        boundary_sessions += 1
        if w <= CLIFF_MID:
            t85_100.append(t)
        else:
            t100_115.append(t)

    z1 = _stats_from_trades(t85_100, f"OR {CLIFF_LOW:.0f}–{CLIFF_MID:.0f} (V38 zone)")
    z2 = _stats_from_trades(t100_115, f"OR {CLIFF_MID:.0f}–{CLIFF_HIGH:.0f} (J+ zone)")

    unstable = False
    notes: list[str] = []
    if z1.trades >= 3 and z2.trades >= 3:
        wr_delta = abs(z1.win_rate_pct - z2.win_rate_pct)
        pf1 = z1.profit_factor or 0.0
        pf2 = z2.profit_factor or 0.0
        if wr_delta >= 25:
            unstable = True
            notes.append(f"win-rate delta {wr_delta:.0f}pp")
        if (pf1 >= 1.2 and pf2 < 0.9) or (pf2 >= 1.2 and pf1 < 0.9):
            unstable = True
            notes.append(f"PF flip ({pf1:.2f} vs {pf2:.2f})")
        if z1.net_pnl_pts > 0 and z2.net_pnl_pts < -abs(z1.net_pnl_pts) * 0.5:
            unstable = True
            notes.append("P&L sign flip (V38 + / J+ −)")
        elif z2.net_pnl_pts > 0 and z1.net_pnl_pts < -abs(z2.net_pnl_pts) * 0.5:
            unstable = True
            notes.append("P&L sign flip (J+ + / V38 −)")

    return CliffAudit(
        zone_85_100=z1,
        zone_100_115=z2,
        unstable_cliff=unstable,
        instability_note="; ".join(notes) if notes else "metrics stable across cliff zones",
        boundary_sessions=boundary_sessions,
    )


def _post_entry_move(
    bars: list[dict[str, Any]],
    entry_bar_index: int,
    entry_price: float,
    side: str,
    n_bars: int = ADX_VELOCITY_BARS,
) -> float:
    """Max absolute close move from entry over next n bars."""
    moves: list[float] = []
    for b in bars:
        if b["index"] <= entry_bar_index:
            continue
        if b["index"] > entry_bar_index + n_bars:
            break
        if side == "CE":
            moves.append(abs(b["close"] - entry_price))
        else:
            moves.append(abs(entry_price - b["close"]))
    return max(moves) if moves else 0.0


def audit_adx_lag_trap(
    trades: list[TradeRecord],
    events: list[SignalEvent],
    session_bars: dict[str, list[dict[str, Any]]],
    cfg: StrategyConfig = DEFAULT_CONFIG,
) -> AdxLagAudit:
    entry_map = _entry_event_map(events)
    tagged: list[dict[str, Any]] = []
    tagged_losses = 0
    total_losses = sum(1 for t in trades if t.pnl_pts < 0)

    for t in trades:
        key = (f"BUY_{t.side}", t.side, t.entry_time)
        ev = entry_map.get(key)
        if ev is None:
            continue
        extra = ev.extra or {}
        adx = extra.get("adx")
        atr = extra.get("atr")
        strategy = extra.get("strategy", "v38")
        adx_floor = ADX_J_FLOOR if strategy == "j_plus" else ADX_V38_FLOOR
        if adx is None or atr is None or float(atr) <= 0:
            continue
        if float(adx) < adx_floor:
            continue

        bars = session_bars.get(t.session_date, [])
        ebi = ev.bar_index
        move = _post_entry_move(bars, ebi, t.entry_price, t.side)
        threshold = ADX_VELOCITY_ATR_MULT * float(atr)
        is_trap = move < threshold
        if not is_trap:
            continue

        row = {
            "session_date": t.session_date,
            "side": t.side,
            "strategy": strategy,
            "entry_time": t.entry_time.strftime("%H:%M"),
            "adx": round(float(adx), 1),
            "move_15m": round(move, 2),
            "threshold": round(threshold, 2),
            "pnl_pts": round(t.pnl_pts, 2),
            "tag": "ADX_Lag_Trap",
        }
        tagged.append(row)
        if t.pnl_pts < 0:
            tagged_losses += 1

    share = (100.0 * tagged_losses / total_losses) if total_losses else 0.0
    return AdxLagAudit(
        tagged_trades=len(tagged),
        tagged_losses=tagged_losses,
        total_losses=total_losses,
        loss_share_pct=share,
        trades=tagged,
    )


def _dynamic_stop(
    side: str,
    entry_ev: SignalEvent,
    atr: float,
    *,
    buffer_mult: float = DYNAMIC_BUFFER_ATR_MULT,
    vix_ratio: float = 1.0,
) -> float | None:
    """Counterfactual stop using scaled buffer instead of fixed 10pt."""
    extra = entry_ev.extra or {}
    or_high = extra.get("or_high")
    or_low = extra.get("or_low")
    if or_high is None or or_low is None or atr <= 0:
        return None
    buf = buffer_mult * atr * vix_ratio
    strategy = extra.get("strategy", "v38")
    if strategy == "j_plus":
        # J+ uses structure ± buffer; approximate with OR boundary + dynamic buf
        if side == "CE":
            trap_low = float(or_low)  # ponytail: conservative structure proxy
            return trap_low - buf
        trap_high = float(or_high)
        return trap_high + buf
    if side == "CE":
        return float(or_low) - buf
    return float(or_high) + buf


def _simulate_path(
    bars: list[dict[str, Any]],
    entry_bar_index: int,
    side: str,
    stop: float,
    target: float,
    *,
    close_only_sl: bool = True,
    square_off_after_index: int | None = None,
) -> ExitSim:
    for b in bars:
        if b["index"] <= entry_bar_index:
            continue
        if square_off_after_index is not None and b["index"] > square_off_after_index:
            return "SQUARE_OFF"
        c, h, l = b["close"], b["high"], b["low"]
        if side == "CE":
            sl_hit = c <= stop if close_only_sl else l <= stop
            tgt_hit = h >= target
        else:
            sl_hit = c >= stop if close_only_sl else h >= stop
            tgt_hit = l <= target
        if sl_hit:
            return "SL"
        if tgt_hit:
            return "TARGET"
    return "OPEN"


def audit_fixed_buffer_stopout(
    trades: list[TradeRecord],
    events: list[SignalEvent],
    session_bars: dict[str, list[dict[str, Any]]],
    cfg: StrategyConfig = DEFAULT_CONFIG,
    vix_by_session: dict[str, float] | None = None,
    vix_median: float | None = None,
) -> BufferStopoutAudit:
    entry_map = _entry_event_map(events)
    flagged: list[dict[str, Any]] = []
    sl_exits = 0
    atr_flags = 0
    vix_flags = 0

    for t in trades:
        is_sl = "STOP" in (t.exit_reason or "").upper()
        if not is_sl:
            continue
        sl_exits += 1

        key = (f"BUY_{t.side}", t.side, t.entry_time)
        ev = entry_map.get(key)
        if ev is None:
            continue
        extra = ev.extra or {}
        atr = extra.get("atr")
        if atr is None or float(atr) <= 0:
            continue

        bars = session_bars.get(t.session_date, [])
        dyn_stop = _dynamic_stop(t.side, ev, float(atr))
        if dyn_stop is None:
            continue

        actual = _simulate_path(bars, ev.bar_index, t.side, t.stop, t.target, close_only_sl=cfg.close_only_sl)
        dynamic = _simulate_path(bars, ev.bar_index, t.side, dyn_stop, t.target, close_only_sl=cfg.close_only_sl)

        is_atr_flag = actual == "SL" and dynamic == "TARGET"
        is_vix_flag = False
        if vix_by_session and vix_median and vix_median > 0:
            vix = vix_by_session.get(t.session_date)
            if vix is not None:
                ratio = float(vix) / vix_median
                vix_stop = _dynamic_stop(t.side, ev, float(atr), buffer_mult=DYNAMIC_BUFFER_ATR_MULT, vix_ratio=ratio)
                if vix_stop is not None:
                    vix_sim = _simulate_path(bars, ev.bar_index, t.side, vix_stop, t.target, close_only_sl=cfg.close_only_sl)
                    is_vix_flag = actual == "SL" and vix_sim == "TARGET"

        if is_atr_flag or is_vix_flag:
            flagged.append({
                "session_date": t.session_date,
                "side": t.side,
                "entry_time": t.entry_time.strftime("%H:%M"),
                "actual_stop": round(t.stop, 2),
                "dynamic_stop": round(dyn_stop, 2),
                "target": round(t.target, 2),
                "pnl_pts": round(t.pnl_pts, 2),
                "tag": "Fixed_Buffer_Stopout",
                "atr_buffer": is_atr_flag,
                "vix_buffer": is_vix_flag,
            })
            if is_atr_flag:
                atr_flags += 1
            if is_vix_flag:
                vix_flags += 1

    return BufferStopoutAudit(
        sl_exits=sl_exits,
        fixed_buffer_stopouts=len(flagged),
        fixed_buffer_stopouts_atr=atr_flags,
        fixed_buffer_stopouts_vix=vix_flags,
        vix_available=bool(vix_by_session),
        trades=flagged,
    )


def audit_time_buckets(
    trades: list[TradeRecord],
    cfg: StrategyConfig = DEFAULT_CONFIG,
) -> TimeBucketAudit:
    buckets: dict[str, list[TradeRecord]] = {"morning": [], "midday": [], "afternoon": []}
    for t in trades:
        label = _time_bucket_label(t.entry_time, cfg)
        if label:
            buckets[label].append(t)

    return TimeBucketAudit(
        morning=_stats_from_trades(buckets["morning"], "Morning Momentum (09:30–11:00)"),
        midday=_stats_from_trades(buckets["midday"], "Mid-Day Doldrums (11:00–13:30)"),
        afternoon=_stats_from_trades(buckets["afternoon"], "Afternoon Institutional (13:30–15:15)"),
    )


def _bars_outside_or_before_entry(
    bars: list[dict[str, Any]],
    entry_bar_index: int,
    or_high: float,
    or_low: float,
    side: str,
) -> int:
    count = 0
    for b in bars:
        if b["index"] >= entry_bar_index:
            break
        if side == "PE" and b["close"] > or_high:
            count += 1
        elif side == "CE" and b["close"] < or_low:
            count += 1
    return count


def max_consecutive_bars_outside_or(
    bars: list[dict[str, Any]],
    entry_bar_index: int,
    or_high: float,
    or_low: float,
    side: str,
) -> int:
    """Max consecutive 5m closes outside OR before reclaim entry (J+ sweep metric)."""
    max_streak = 0
    streak = 0
    for b in bars:
        if b["index"] >= entry_bar_index:
            break
        outside = (side == "PE" and b["close"] > or_high) or (side == "CE" and b["close"] < or_low)
        if outside:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    return max_streak


def bar_close_minutes(ts: datetime, cfg: StrategyConfig) -> int:
    """IST bar close as minutes from midnight (matches strategy_audit time buckets)."""
    return _bar_close_minutes(ts, cfg)


def index_session_bars(df: Any) -> dict[str, list[dict[str, Any]]]:
    """Per-session OHLC rows keyed by session_date (for sweep precompute)."""
    return _index_session_bars(df)


def audit_j_acceptance(
    trades: list[TradeRecord],
    events: list[SignalEvent],
    session_bars: dict[str, list[dict[str, Any]]],
) -> JAcceptanceAudit:
    entry_map = _entry_event_map(events)
    rows: list[dict[str, Any]] = []
    j_count = 0
    acceptance = 0
    acc_losses = 0

    for t in trades:
        key = (f"BUY_{t.side}", t.side, t.entry_time)
        ev = entry_map.get(key)
        if ev is None or (ev.extra or {}).get("strategy") != "j_plus":
            continue
        j_count += 1
        extra = ev.extra or {}
        or_high, or_low = extra.get("or_high"), extra.get("or_low")
        if or_high is None or or_low is None:
            continue
        outside = _bars_outside_or_before_entry(
            session_bars.get(t.session_date, []),
            ev.bar_index,
            float(or_high),
            float(or_low),
            t.side,
        )
        is_acc = outside > J_BARS_OUTSIDE_OR_LIMIT
        if is_acc:
            acceptance += 1
        row = {
            "session_date": t.session_date,
            "side": t.side,
            "entry_time": t.entry_time.strftime("%H:%M"),
            "bars_outside_or": outside,
            "pnl_pts": round(t.pnl_pts, 2),
            "tag": "Value_Acceptance_Short" if is_acc else "",
        }
        rows.append(row)
        if is_acc and t.pnl_pts < 0:
            acc_losses += 1

    return JAcceptanceAudit(
        j_trades=j_count,
        value_acceptance_short=acceptance,
        acceptance_losses=acc_losses,
        trades=rows,
    )


def load_vix_by_session(path: Any) -> dict[str, float]:
    """Load optional VIX CSV: columns session_date (or date), vix (or close)."""
    import pandas as pd

    df = pd.read_csv(path)
    date_col = "session_date" if "session_date" in df.columns else "date"
    val_col = "vix" if "vix" in df.columns else "close"
    if date_col not in df.columns or val_col not in df.columns:
        raise ValueError(f"VIX CSV needs {date_col} and {val_col} columns")
    out: dict[str, float] = {}
    for _, row in df.iterrows():
        ds = str(row[date_col])[:10]
        out[ds] = float(row[val_col])
    return out


def run_strategy_audit(
    logger: ReplayLogger,
    df: Any,
    cfg: StrategyConfig = DEFAULT_CONFIG,
    *,
    vix_by_session: dict[str, float] | None = None,
) -> StrategyAuditReport:
    """Full 5-part structural audit from replay events + candle dataframe."""
    enriched = _compute_indicators(df, cfg) if "atr" not in df.columns else df
    trade_events = [e for e in logger.events if e.event_type in ENTRY_TYPES | EXIT_TYPES]
    trades = pair_trades(trade_events)
    or_widths = build_session_or_widths(enriched, cfg)
    session_bars = _index_session_bars(enriched)

    vix_median = None
    if vix_by_session:
        vals = list(vix_by_session.values())
        if vals:
            vix_median = sorted(vals)[len(vals) // 2]

    return StrategyAuditReport(
        cliff=audit_playbook_cliff(trades, or_widths),
        adx_lag=audit_adx_lag_trap(trades, logger.events, session_bars, cfg),
        buffer=audit_fixed_buffer_stopout(trades, logger.events, session_bars, cfg, vix_by_session, vix_median),
        time_buckets=audit_time_buckets(trades, cfg),
        j_acceptance=audit_j_acceptance(trades, logger.events, session_bars),
        total_trades=len(trades),
    )


def _fmt_segment(s: SegmentStats) -> list[str]:
    pf = f"{s.profit_factor:.2f}" if s.profit_factor is not None else "—"
    return [
        f"  {s.label}",
        f"    Trades: {s.trades}  |  WR: {s.win_rate_pct:.1f}%  |  Net P&L: {s.net_pnl_pts:+.1f} pts",
        f"    PF: {pf}  |  Max DD: {s.max_drawdown_pts:.1f} pts  |  Sessions w/ trades: {s.sessions}",
    ]


def format_strategy_audit_report(report: StrategyAuditReport) -> str:
    """Structured stdout template for all five algorithmic gap analyses."""
    lines = [
        "=" * 72,
        "Sgnal v3.9 — Algorithmic Vulnerability & Market Structure Audit",
        "=" * 72,
        f"Total trades analyzed: {report.total_trades}",
        "",
        "─" * 72,
        "1. PLAYBOOK CLIFF — OR width boundary unsaturation (85–115 pts)",
        "─" * 72,
        f"  Boundary-zone sessions (OR ∈ [{CLIFF_LOW}, {CLIFF_HIGH}]): {report.cliff.boundary_sessions}",
        *_fmt_segment(report.cliff.zone_85_100),
        *_fmt_segment(report.cliff.zone_100_115),
        f"  Unstable cliff flag: {'YES ⚠' if report.cliff.unstable_cliff else 'no'}",
        f"  Note: {report.cliff.instability_note}",
        "",
        "─" * 72,
        "2. ADX LAG / OPENING CHAOS TRAP",
        "─" * 72,
        f"  ADX_Lag_Trap tagged trades: {report.adx_lag.tagged_trades}",
        f"  Losses from ADX lag traps:  {report.adx_lag.tagged_losses} / {report.adx_lag.total_losses} "
        f"total losses ({report.adx_lag.loss_share_pct:.1f}%)",
    ]
    for row in report.adx_lag.trades[:15]:
        lines.append(
            f"    {row['session_date']} {row['side']} {row['entry_time']} "
            f"ADX={row['adx']} move15m={row['move_15m']:.1f}<{row['threshold']:.1f} "
            f"pnl={row['pnl_pts']:+.1f}"
        )
    if len(report.adx_lag.trades) > 15:
        lines.append(f"    ... +{len(report.adx_lag.trades) - 15} more")

    lines.extend([
        "",
        "─" * 72,
        "3. VOLATILITY INELASTICITY — Fixed 10pt SL buffer vs 0.15×ATR (±VIX)",
        "─" * 72,
        f"  Close-only SL exits:           {report.buffer.sl_exits}",
        f"  Fixed_Buffer_Stopout flags:    {report.buffer.fixed_buffer_stopouts}",
        f"    via 0.15×ATR buffer:         {report.buffer.fixed_buffer_stopouts_atr}",
        f"    via VIX-scaled buffer:       {report.buffer.fixed_buffer_stopouts_vix} "
        f"(VIX data: {'yes' if report.buffer.vix_available else 'no'})",
    ])
    for row in report.buffer.trades[:10]:
        lines.append(
            f"    {row['session_date']} {row['side']} stop {row['actual_stop']}→dyn {row['dynamic_stop']} "
            f"pnl={row['pnl_pts']:+.1f}"
        )

    lines.extend([
        "",
        "─" * 72,
        "4. TIME-OF-DAY DECAY — Entry bucket matrix",
        "─" * 72,
        *_fmt_segment(report.time_buckets.morning),
        *_fmt_segment(report.time_buckets.midday),
        *_fmt_segment(report.time_buckets.afternoon),
        "",
        "─" * 72,
        "5. J+ VALUE ACCEPTANCE ILLUSION — bars_outside_or > 2",
        "─" * 72,
        f"  J+ trades:                     {report.j_acceptance.j_trades}",
        f"  Value_Acceptance_Short flags:  {report.j_acceptance.value_acceptance_short}",
        f"  Acceptance-flag losses:        {report.j_acceptance.acceptance_losses}",
    ])
    for row in report.j_acceptance.trades:
        if row.get("tag"):
            lines.append(
                f"    {row['session_date']} {row['side']} outside={row['bars_outside_or']} "
                f"pnl={row['pnl_pts']:+.1f} [{row['tag']}]"
            )

    lines.append("=" * 72)
    lines.append("Diagnostic only — strategy parameters unchanged.")
    lines.append("=" * 72)
    return "\n".join(lines)
