#!/usr/bin/env python3
"""Print Workstream E alert layout samples to stdout (plain text)."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bot.alerts import TelegramAlerter
from bot.logger import SignalEvent
from bot.state import Position, PositionSide
from bot.telegram_format import html_to_plain

TZ = ZoneInfo("Asia/Kolkata")


def main() -> None:
    entry_event = SignalEvent(
        timestamp=datetime(2026, 6, 15, 10, 0, tzinfo=TZ),
        event_type="BUY_CE",
        direction="LONG",
        side="CE",
        price=24165.0,
        stop=24120.0,
        target=24220.0,
        reason="ORB_BREAKOUT+VWAP+ADX",
        extra={
            "or_high": 24150.0,
            "or_low": 24100.0,
            "adx": 22.0,
            "vwap": 24140.0,
            "spread_drag_pct": 4.5,
            "entry_slippage_pts": 2.2,
            "option_strike": 24150,
            "option_type": "CE",
            "option_expiry": "2026-06-26",
            "option_symbol": "NIFTY 24150 CE 26 JUN 26",
            "option_ask": 104.0,
            "option_bid": 99.3,
        },
    )
    entry_text = html_to_plain(TelegramAlerter.format_signal(entry_event))

    pos = Position(
        side=PositionSide.CE,
        entry_price=24165.0,
        stop=24120.0,
        target=24220.0,
        option_symbol="NIFTY 24150 CE 26 JUN 26",
        iv_at_entry=0.20,
        iv_crush_bleed=True,
        last_iv_delta=-0.03,
    )
    status_text = TelegramAlerter.format_position_status(
        pos, spot=24166.2, spot_pnl=1.2,
    )

    print("## Enriched BUY_CE (high spread friction)\n")
    print(entry_text)
    print("\n## POSITION_STATUS (IV crush alarm)\n")
    print(status_text)


if __name__ == "__main__":
    main()
