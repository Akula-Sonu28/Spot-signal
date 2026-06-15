"""Tests for Telegram HTML formatting helpers."""

from __future__ import annotations

from bot.telegram_format import bold, html_escape, price, pts_signed, rupee_signed


def test_html_escape_special_chars() -> None:
    assert html_escape("a<b>&c") == "a&lt;b&gt;&amp;c"


def test_bold_wraps_escaped_text() -> None:
    assert bold("x&y") == "<b>x&amp;y</b>"


def test_price_none() -> None:
    assert price(None) == "—"


def test_price_bold_number() -> None:
    assert price(23931.8) == "<b>23931.80</b>"


def test_pts_signed_positive() -> None:
    assert "🟢" in pts_signed(43.6)
    assert "<b>+43.6 pts</b>" in pts_signed(43.6)


def test_rupee_signed_negative() -> None:
    assert rupee_signed(-12.5) == "-₹<b>12.5</b>"
