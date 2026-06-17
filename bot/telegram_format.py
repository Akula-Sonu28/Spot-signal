"""Telegram HTML formatting helpers (bold / monospace — no custom colours)."""

from __future__ import annotations

import re

DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━"


def html_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def bold(text: str) -> str:
    return f"<b>{html_escape(text)}</b>"


def italic(text: str) -> str:
    return f"<i>{html_escape(text)}</i>"


def code(text: str) -> str:
    return f"<code>{html_escape(text)}</code>"


def section(title: str) -> str:
    """Section header — bold with leading emoji preserved."""
    return bold(title)


def price(value: float | None, *, decimals: int = 2) -> str:
    if value is None:
        return "—"
    return bold(f"{value:.{decimals}f}")


def pts_signed(value: float) -> str:
    emoji = "🟢" if value >= 0 else "🔴"
    return f"{emoji} {bold(f'{value:+.1f} pts')}"


def rupee_signed(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}₹{bold(f'{abs(value):.1f}')}"


def html_unescape(text: str) -> str:
    return (
        text.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )


def html_to_plain(text: str) -> str:
    """Strip Telegram HTML tags for plain-text channels (e.g. ntfy)."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html_unescape(text)
