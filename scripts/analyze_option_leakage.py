#!/usr/bin/env python3
"""Spot vs. premium reality report from live/backtest signals.csv telemetry."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot.backtest import ENTRY_TYPES, EXIT_TYPES, TradeRecord
from bot.logger import LIVE_CSV_FIELDS, SignalEvent

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "data" / "live" / "signals.csv"


@dataclass
class LeakageTrade:
    trade: TradeRecord
    entry_row: dict[str, Any]
    exit_row: dict[str, Any]


@dataclass
class LeakageReport:
    source: Path
    total_trades: int
    spot_wins: int
    spot_losses: int
    spot_win_rate_pct: float
    phantom_target_wins: int
    realizable_wins: int
    realizable_win_rate_pct: float
    avg_spread_drag_pct: float | None
    total_entry_slippage_pts: float
    trades_with_slippage: int
    iv_crush_trades: int
    iv_crush_pct: float
    total_spot_pnl_pts: float
    phantom_win_pnl_pts: float
    adjusted_realizable_pnl_pts: float
    notes: list[str] = field(default_factory=list)


def _coerce_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _coerce_bool(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if val is None or val == "":
        return False
    return str(val).strip().lower() in {"1", "true", "yes", "y"}


def _parse_timestamp(val: str) -> datetime:
    return datetime.fromisoformat(val)


def _row_to_event(row: dict[str, str]) -> SignalEvent:
    extra_raw = row.get("extra") or "{}"
    try:
        extra = json.loads(extra_raw) if extra_raw else {}
    except json.JSONDecodeError:
        extra = {}
    for key in LIVE_CSV_FIELDS:
        if key in {
            "timestamp", "event_type", "direction", "side", "price", "stop", "target",
            "reason", "bar_index", "extra",
        }:
            continue
        val = row.get(key)
        if val not in (None, ""):
            extra[key] = val
    return SignalEvent(
        timestamp=_parse_timestamp(row["timestamp"]),
        event_type=row["event_type"],
        direction=row.get("direction", ""),
        side=row.get("side", ""),
        price=float(row.get("price") or 0),
        stop=_coerce_float(row.get("stop")),
        target=_coerce_float(row.get("target")),
        reason=row.get("reason", ""),
        bar_index=int(row.get("bar_index") or -1),
        extra=extra,
    )


def load_signals_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _trade_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return [r for r in rows if r.get("event_type") in ENTRY_TYPES | EXIT_TYPES]


def pair_trades_with_rows(rows: list[dict[str, str]]) -> list[LeakageTrade]:
    trade_rows = _trade_rows(rows)
    events = [_row_to_event(r) for r in trade_rows]
    out: list[LeakageTrade] = []
    open_entry: SignalEvent | None = None
    open_entry_row: dict[str, str] | None = None

    for e, r in zip(events, trade_rows):
        if e.event_type in ENTRY_TYPES:
            open_entry = e
            open_entry_row = r
            continue
        if e.event_type not in EXIT_TYPES or open_entry is None or open_entry_row is None:
            continue
        side = open_entry.side
        if not e.event_type.endswith(f"_{side}") and e.event_type != "SQUARE_OFF":
            continue
        if e.event_type == "SQUARE_OFF" and e.side != side:
            continue
        entry = float(open_entry.price)
        stop = float(open_entry.stop or e.stop or entry)
        target = float(open_entry.target or e.target or entry)
        exit_price = float(e.price)
        pnl = exit_price - entry if side == "CE" else entry - exit_price
        risk = abs(entry - stop) or 1e-9
        trade = TradeRecord(
            session_date=open_entry.timestamp.strftime("%Y-%m-%d"),
            side=side,
            entry_time=open_entry.timestamp,
            exit_time=e.timestamp,
            entry_price=entry,
            exit_price=exit_price,
            stop=stop,
            target=target,
            exit_reason=e.reason or e.event_type,
            pnl_pts=pnl,
            risk_pts=risk,
            r_multiple=pnl / risk,
        )
        out.append(LeakageTrade(trade=trade, entry_row=open_entry_row, exit_row=r))
        open_entry = None
        open_entry_row = None

    return out


def _row_iv_crush(row: dict[str, Any]) -> bool:
    return _coerce_bool(row.get("iv_crush_bleed"))


def _trade_iv_crush(
    lt: LeakageTrade,
    rows: list[dict[str, str]],
) -> bool:
    if _row_iv_crush(lt.entry_row) or _row_iv_crush(lt.exit_row):
        return True
    t0, t1 = lt.trade.entry_time, lt.trade.exit_time
    for row in rows:
        if row.get("event_type") not in {"POSITION_PREMIUM", *EXIT_TYPES, *ENTRY_TYPES}:
            continue
        ts = _parse_timestamp(row["timestamp"])
        if t0 <= ts <= t1 and _row_iv_crush(row):
            return True
    return False


def _phantom_target_win(lt: LeakageTrade) -> bool:
    if lt.trade.pnl_pts <= 0:
        return False
    if lt.exit_row.get("event_type", "").startswith("TARGET"):
        return _coerce_bool(lt.exit_row.get("target_reverted_mid_bar"))
    return False


def analyze_leakage(path: Path, rows: list[dict[str, str]] | None = None) -> LeakageReport:
    rows = rows if rows is not None else load_signals_csv(path)
    paired = pair_trades_with_rows(rows)
    notes: list[str] = []

    if not paired:
        notes.append("No paired trades found — run a backtest replay with telemetry or wait for live entries.")
        return LeakageReport(
            source=path,
            total_trades=0,
            spot_wins=0,
            spot_losses=0,
            spot_win_rate_pct=0.0,
            phantom_target_wins=0,
            realizable_wins=0,
            realizable_win_rate_pct=0.0,
            avg_spread_drag_pct=None,
            total_entry_slippage_pts=0.0,
            trades_with_slippage=0,
            iv_crush_trades=0,
            iv_crush_pct=0.0,
            total_spot_pnl_pts=0.0,
            phantom_win_pnl_pts=0.0,
            adjusted_realizable_pnl_pts=0.0,
            notes=notes,
        )

    spot_wins = [lt for lt in paired if lt.trade.pnl_pts > 0]
    spot_losses = [lt for lt in paired if lt.trade.pnl_pts < 0]
    phantoms = [lt for lt in paired if _phantom_target_win(lt)]
    realizable_wins = len(spot_wins) - len(phantoms)

    drag_vals: list[float] = []
    slip_total = 0.0
    slip_count = 0
    for lt in paired:
        drag = _coerce_float(lt.entry_row.get("spread_drag_pct"))
        if drag is not None:
            drag_vals.append(drag)
        slip = _coerce_float(lt.entry_row.get("entry_slippage_pts"))
        if slip is not None:
            slip_total += abs(slip)
            slip_count += 1

    iv_crush_trades = sum(1 for lt in paired if _trade_iv_crush(lt, rows))
    total_pnl = sum(lt.trade.pnl_pts for lt in paired)
    phantom_pnl = sum(lt.trade.pnl_pts for lt in phantoms)
    adjusted = total_pnl - phantom_pnl

    if not drag_vals:
        notes.append("spread_drag_pct missing on entries — friction averages use rows with telemetry only.")

    n = len(paired)
    return LeakageReport(
        source=path,
        total_trades=n,
        spot_wins=len(spot_wins),
        spot_losses=len(spot_losses),
        spot_win_rate_pct=100.0 * len(spot_wins) / n,
        phantom_target_wins=len(phantoms),
        realizable_wins=realizable_wins,
        realizable_win_rate_pct=100.0 * realizable_wins / n,
        avg_spread_drag_pct=(sum(drag_vals) / len(drag_vals)) if drag_vals else None,
        total_entry_slippage_pts=slip_total,
        trades_with_slippage=slip_count,
        iv_crush_trades=iv_crush_trades,
        iv_crush_pct=100.0 * iv_crush_trades / n,
        total_spot_pnl_pts=total_pnl,
        phantom_win_pnl_pts=phantom_pnl,
        adjusted_realizable_pnl_pts=adjusted,
        notes=notes,
    )


def format_leakage_report(report: LeakageReport) -> str:
    lines = [
        "# Spot vs. Premium Reality Report",
        "",
        f"**Source:** `{report.source}`",
        f"**Trades analyzed:** {report.total_trades}",
        "",
        "## Wick Optimism Audit",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Spot win rate | {report.spot_win_rate_pct:.1f}% ({report.spot_wins}W / {report.spot_losses}L) |",
        f"| Phantom target wins (mid-bar wick only) | {report.phantom_target_wins} |",
        f"| Option-realizable wins | {report.realizable_wins} |",
        f"| Realizable win rate | {report.realizable_win_rate_pct:.1f}% |",
        "",
        "## Friction Metrics Matrix",
        "",
        "| Metric | Value |",
        "|--------|-------|",
    ]
    avg_drag = f"{report.avg_spread_drag_pct:.2f}%" if report.avg_spread_drag_pct is not None else "— (no telemetry)"
    lines.extend([
        f"| Avg spread drag (entries) | {avg_drag} |",
        f"| Cumulative entry slippage | {report.total_entry_slippage_pts:.1f} premium pts ({report.trades_with_slippage} trades) |",
        "",
        "## Volatility Decay Impact",
        "",
        f"- **IV crush bleed events:** {report.iv_crush_trades} / {report.total_trades} trades "
        f"({report.iv_crush_pct:.1f}%) while spot consolidated sideways",
        "",
        "## P&L Degradation Summary",
        "",
        "| Layer | Spot P&L (pts) |",
        "|-------|----------------|",
        f"| Backtest spot total | {report.total_spot_pnl_pts:+.1f} |",
        f"| Phantom target wins (wick-only) | {report.phantom_win_pnl_pts:+.1f} at risk |",
        f"| Adjusted realizable (ex-phantom wins) | {report.adjusted_realizable_pnl_pts:+.1f} |",
        "",
        "_Phantom wins: TARGET hit on wick but bar close reverted — manual option fills often miss._",
    ])
    if report.notes:
        lines.extend(["", "## Notes", ""])
        lines.extend(f"- {n}" for n in report.notes)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Option microstructure leakage analyzer")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="signals.csv path")
    args = parser.parse_args()
    report = analyze_leakage(args.csv)
    print(format_leakage_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
