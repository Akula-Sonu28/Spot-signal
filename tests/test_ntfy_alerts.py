"""Tests for ntfy push alerts."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from bot.alerts import CompositeAlerter, NtfyAlerter, create_alerter
from bot.config import AppConfig, validate_app_config
from bot.telegram_format import html_to_plain


def _cfg(**kwargs) -> AppConfig:
    defaults = dict(
        upstox_access_token="upstox",
        telegram_bot_token="",
        telegram_chat_id="",
        ntfy_topic="my-nifty-signals",
    )
    defaults.update(kwargs)
    return AppConfig(**defaults)


def test_html_to_plain_strips_tags():
    raw = "<b>BUY CE</b>  •  <i>wait</i>  &amp; go"
    assert html_to_plain(raw) == "BUY CE  •  wait  & go"


def test_validate_ntfy_only_ok():
    errors = validate_app_config(_cfg())
    assert errors == []


def test_validate_requires_alert_channel():
    errors = validate_app_config(
        AppConfig(upstox_access_token="x", telegram_bot_token="", telegram_chat_id="")
    )
    assert any("NTIFY_TOPIC" in err for err in errors)


def test_validate_partial_telegram_rejected():
    errors = validate_app_config(
        AppConfig(upstox_access_token="x", telegram_bot_token="tok", telegram_chat_id="")
    )
    assert any("TELEGRAM_CHAT_ID" in err for err in errors)


def test_create_alerter_ntfy_only():
    alerter = create_alerter(_cfg())
    assert isinstance(alerter, NtfyAlerter)


def test_create_alerter_both_channels():
    alerter = create_alerter(
        _cfg(telegram_bot_token="t", telegram_chat_id="1"),
    )
    assert isinstance(alerter, CompositeAlerter)


@patch("bot.alerts._urlopen")
def test_ntfy_send_posts_plain_text(mock_urlopen: MagicMock):
    resp = MagicMock()
    resp.status = 200
    resp.__enter__.return_value = resp
    mock_urlopen.return_value = resp

    alerter = NtfyAlerter(_cfg(ntfy_topic="secret-topic"))
    alerter.send("<b>Hello</b> world", parse_mode="HTML")

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.full_url.startswith("https://")
    assert "secret-topic" in req.full_url
    assert "title=Hello" in req.full_url
    assert req.data == b"Hello world"


@patch("bot.alerts._urlopen")
def test_ntfy_http_error(mock_urlopen: MagicMock):
    import io
    import urllib.error

    mock_urlopen.side_effect = urllib.error.HTTPError(
        "https://ntfy.sh/t",
        404,
        "not found",
        {},
        io.BytesIO(json.dumps({"error": "topic not found"}).encode()),
    )
    alerter = NtfyAlerter(_cfg())
    with pytest.raises(RuntimeError, match="ntfy HTTP 404"):
        alerter.send("ping")
