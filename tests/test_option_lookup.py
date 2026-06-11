"""Tests for option strike selection."""

from __future__ import annotations

from bot.option_lookup import (
    OptionQuote,
    compute_premium_levels,
    format_option_lines,
    format_premium_risk_lines,
    pick_strike,
    round_nifty_strike,
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
