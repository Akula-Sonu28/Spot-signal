"""Tests for scripts/analyze_option_leakage.py."""

from __future__ import annotations

from scripts.analyze_option_leakage import analyze_leakage, format_leakage_report, pair_trades_with_rows


def _row(**kwargs) -> dict[str, str]:
    base = {
        "timestamp": "2026-06-15T10:05:00+05:30",
        "event_type": "BUY_CE",
        "direction": "LONG",
        "side": "CE",
        "price": "100",
        "stop": "90",
        "target": "120",
        "reason": "TEST",
        "bar_index": "5",
        "spread_drag_pct": "4.0",
        "entry_slippage_pts": "1.5",
        "iv_crush_bleed": "False",
        "target_reverted_mid_bar": "",
        "extra": "{}",
    }
    base.update({k: str(v) for k, v in kwargs.items()})
    return base


def test_pair_trades_with_rows_phantom_target():
    rows = [
        _row(event_type="BUY_CE", timestamp="2026-06-15T10:05:00+05:30"),
        _row(
            event_type="TARGET_CE",
            direction="LONG_EXIT",
            timestamp="2026-06-15T11:00:00+05:30",
            price="120",
            target_reverted_mid_bar="True",
        ),
    ]
    paired = pair_trades_with_rows(rows)
    assert len(paired) == 1
    assert paired[0].trade.pnl_pts == 20.0


def test_analyze_leakage_report_metrics(tmp_path):
    rows = [
        _row(event_type="BUY_CE", timestamp="2026-06-15T10:05:00+05:30", spread_drag_pct="4.0", entry_slippage_pts="2.0"),
        _row(
            event_type="TARGET_CE",
            direction="LONG_EXIT",
            timestamp="2026-06-15T11:00:00+05:30",
            price="120",
            target_reverted_mid_bar="True",
        ),
        _row(event_type="BUY_PE", timestamp="2026-06-15T12:05:00+05:30", side="PE", direction="SHORT", price="100", stop="110", target="80", spread_drag_pct="2.0", entry_slippage_pts=""),
        _row(
            event_type="SL_PE",
            direction="SHORT_EXIT",
            side="PE",
            timestamp="2026-06-15T13:00:00+05:30",
            price="110",
            iv_crush_bleed="True",
        ),
    ]
    path = tmp_path / "signals.csv"
    path.write_text("timestamp,event_type,direction,side,price,stop,target,reason,bar_index,spread_drag_pct,entry_slippage_pts,iv_crush_bleed,target_reverted_mid_bar,extra\n")
    report = analyze_leakage(path, rows=rows)
    assert report.total_trades == 2
    assert report.phantom_target_wins == 1
    assert report.realizable_wins == 0
    assert report.avg_spread_drag_pct == 3.0
    assert report.total_entry_slippage_pts == 2.0
    assert report.iv_crush_trades == 1
    text = format_leakage_report(report)
    assert "Spot vs. Premium Reality Report" in text
    assert "Phantom target wins" in text
