"""Integration tests for CSV replay."""

from pathlib import Path

import pytest

from bot.config import StrategyConfig
from bot.replay import load_candles_csv, run_replay, run_replay_fast

DATA = Path(__file__).resolve().parent.parent / "data" / "mock"


def test_load_candles_csv():
    df = load_candles_csv(DATA / "day_trend_up.csv")
    assert len(df) == 150
    assert "session_date" in df.columns
    assert df["timestamp"].dt.tz is not None


def test_replay_runs_without_error():
    df = load_candles_csv(DATA / "day_chop.csv")
    logger = run_replay_fast(df)
    assert isinstance(logger.events, list)


def test_replay_deterministic():
    df = load_candles_csv(DATA / "day_trend_up.csv")
    a = run_replay_fast(df)
    b = run_replay_fast(df)
    assert [e.to_dict() for e in a.events] == [e.to_dict() for e in b.events]


def test_narrow_or_fixture_no_trade():
    df = load_candles_csv(DATA / "day_or_too_narrow.csv")
    logger = run_replay_fast(df)
    entries = [e for e in logger.events if e.event_type in ("BUY_CE", "BUY_PE")]
    assert len(entries) == 0


def test_replay_slow_matches_fast():
    df = load_candles_csv(DATA / "day_breakdown.csv")
    slow = run_replay(df)
    fast = run_replay_fast(df)
    assert [e.event_type for e in slow.events] == [e.event_type for e in fast.events]


def test_write_csv(tmp_path):
    df = load_candles_csv(DATA / "day_chop.csv")
    logger = run_replay_fast(df)
    out = tmp_path / "events.csv"
    logger.write_csv(out)
    assert out.exists()
