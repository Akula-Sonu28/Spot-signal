"""Tests for option strike selection."""

from __future__ import annotations

import pytest

from bot.option_lookup import (
    OptionQuote,
    compute_premium_levels,
    estimate_premium_change,
    estimate_premium_pnl_per_share,
    format_entry_pnl_lines,
    format_option_lines,
    format_premium_risk_lines,
    pick_strike,
    round_nifty_strike,
    spot_pnl_points,
)


def test_round_strike():
    assert round_nifty_strike(23267) == 23250
    assert round_nifty_strike(23274) == 23250


def test_pick_strike_ce_pe():
    assert pick_strike(23267, "CE", "ATM") == 23250
    assert pick_strike(23267, "CE", "ITM1") == 23200
    assert pick_strike(23267, "PE", "ITM1") == 23300


def test_format_option_lines_with_bid():
    q = OptionQuote(
        strike=23250,
        option_type="CE",
        expiry="2026-06-16",
        trading_symbol="NIFTY 23250 CE 16 JUN 26",
        instrument_key="NSE_FO|1",
        ltp=120.5,
        bid=119.0,
        ask=121.0,
        spread=2.0,
        quote_ok=True,
    )
    text = "\n".join(format_option_lines(q))
    assert "23250 CE" in text
    assert "Ask (buy here): ₹121.00" in text
    assert "Bid: ₹119.00" in text


def test_premium_levels_from_ask():
    sl, tgt = compute_premium_levels(100.0, rr_ratio=1.8)
    assert sl == 50.0
    assert tgt == 190.0


def test_format_premium_risk_uses_ask():
    q = OptionQuote(
        strike=23200,
        option_type="PE",
        expiry="2026-06-09",
        trading_symbol="NIFTY 23200 PE 09 JUN 26",
        instrument_key="x",
        bid=42.0,
        ask=43.0,
    )
    text = "\n".join(format_premium_risk_lines(q, rr_ratio=1.8))
    assert "Entry (ask): ₹43.00" in text
    assert "₹21.50" in text  # 50% SL
    assert "₹81.70" in text  # 43 + 21.5*1.8


def test_premium_levels_none_without_ask():
    assert compute_premium_levels(None) is None
    q = OptionQuote(
        strike=23200,
        option_type="PE",
        expiry="2026-06-09",
        trading_symbol="x",
        instrument_key="x",
        ltp=40.0,
        bid=39.0,
    )
    assert "ask" in "\n".join(format_premium_risk_lines(q)).lower()


def test_spot_pnl_points_pe_and_ce():
    assert spot_pnl_points(23931.80, 23888.15, "PE") == pytest.approx(43.65)
    assert spot_pnl_points(23931.80, 24021.40, "PE") == pytest.approx(-89.6)
    assert spot_pnl_points(23200.0, 23300.0, "CE") == 100.0


def test_estimate_premium_pnl_per_share():
    assert estimate_premium_pnl_per_share(-0.45, 23931.80, 23888.15) == pytest.approx(19.6425)
    assert estimate_premium_pnl_per_share(-0.45, 23931.80, 24021.40) == pytest.approx(-40.32)


def test_format_entry_pnl_lines_with_delta():
    lines = format_entry_pnl_lines(
        entry=23931.80,
        stop=24021.40,
        target=23888.15,
        side="PE",
        delta=-0.45,
        theta=-8.2,
        iv=0.185,
        gamma=0.0004,
        atr=18.2,
    )
    text = "\n".join(lines)
    assert "P&amp;L IF SPOT LEVELS HIT" in text
    assert "+43.6 pts" in text
    assert "-89.6 pts" in text
    assert "OR stop" in text
    assert "<b>δ=-0.45</b>" in text
    assert "<b>θ=-8.2/day</b>" in text
    assert "<b>IV=18.5%</b>" in text
    assert "δ+θ+½γΔS²" in text
    assert "IV flat" in text


def test_estimate_premium_change_includes_theta():
    # 90 min hold, theta -8.2/day -> -8.2 * (90/375) ≈ -1.97
    delta_only = estimate_premium_change(
        spot_entry=23931.80,
        spot_exit=23888.15,
        delta=-0.45,
        hold_minutes=90,
    )
    with_theta = estimate_premium_change(
        spot_entry=23931.80,
        spot_exit=23888.15,
        delta=-0.45,
        theta=-8.2,
        gamma=0.0004,
        hold_minutes=90,
    )
    assert with_theta is not None and delta_only is not None
    assert with_theta < delta_only  # time decay hurts long option


def test_format_entry_pnl_lines_spot_only_without_delta():
    lines = format_entry_pnl_lines(
        entry=23931.80,
        stop=24021.40,
        target=23888.15,
        side="PE",
    )
    text = "\n".join(lines)
    assert "+43.6 pts" in text
    assert "Option est." not in text
