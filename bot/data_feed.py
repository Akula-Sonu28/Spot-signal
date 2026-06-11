"""Upstox REST candle feed for live 5m monitoring."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

import pandas as pd
from zoneinfo import ZoneInfo

from bot.config import AppConfig
from bot.futures_vwap import (
    INDEX_INSTRUMENT,
    aggregate_1m_to_5m,
    fetch_historical_1m,
    fetch_intraday_5m,
    fetch_merged_today,
    merge_index_and_futures,
    merged_bars_to_dataframe,
    nearest_nifty_future_key,
)
from bot.replay import _compute_indicators


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    session_date: str
    vwap: float | None = None
    atr: float | None = None
    adx: float | None = None


@dataclass
class FeedSnapshot:
    futures_key: str
    dataframe: pd.DataFrame
    fetched_at: datetime
    today_bar_count: int


class DataFeedError(Exception):
    """Raised when candle data is unavailable or invalid."""


def _prior_trading_days(session: date, count: int) -> list[date]:
    found: list[date] = []
    d = session - timedelta(days=1)
    while len(found) < count and (session - d).days < 14:
        if d.weekday() < 5:
            found.append(d)
        d -= timedelta(days=1)
    return list(reversed(found))


def _has_1m_data(instrument: str, day: date, token: str) -> bool:
    try:
        return len(fetch_historical_1m(instrument, day.isoformat(), day.isoformat(), token=token)) > 0
    except Exception:
        return False


def normalize_candle_row(row: pd.Series) -> Candle:
    return Candle(
        timestamp=row["timestamp"].to_pydatetime(),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row.get("volume", 0)),
        session_date=str(row["session_date"]),
        vwap=float(row["vwap"]) if pd.notna(row.get("vwap")) else None,
        atr=float(row["atr"]) if pd.notna(row.get("atr")) else None,
        adx=float(row["adx"]) if pd.notna(row.get("adx")) else None,
    )


def bar_close_time(bar_open: datetime, bar_time_is_open: bool) -> datetime:
    if bar_time_is_open:
        return bar_open + timedelta(minutes=5)
    return bar_open


def is_bar_complete(
    bar_open: datetime,
    now: datetime,
    *,
    bar_time_is_open: bool = True,
    buffer_sec: int = 15,
) -> bool:
    """True when the 5m candle has closed and a short buffer has passed."""
    close_ts = bar_close_time(bar_open, bar_time_is_open)
    return now >= close_ts + timedelta(seconds=buffer_sec)


def fetch_session_dataframe(
    session: date,
    token: str,
    warmup_days: int = 3,
) -> tuple[str, pd.DataFrame]:
    """Index spot + futures VWAP with prior-session warmup for indicators."""
    fut_key = nearest_nifty_future_key(as_of=session)
    _, today_merged = fetch_merged_today(token=token, as_of=session)
    if not today_merged:
        raise DataFeedError("No intraday candles returned for NIFTY index")

    warmup_bars = []
    for day in _prior_trading_days(session, warmup_days):
        if not _has_1m_data(INDEX_INSTRUMENT, day, token):
            continue
        ds = day.isoformat()
        idx_1m = fetch_historical_1m(INDEX_INSTRUMENT, ds, ds, token=token)
        fut_1m = fetch_historical_1m(fut_key, ds, ds, token=token)
        warmup_bars.extend(merge_index_and_futures(aggregate_1m_to_5m(idx_1m), aggregate_1m_to_5m(fut_1m)))

    today_df = merged_bars_to_dataframe(today_merged)
    df = pd.concat([merged_bars_to_dataframe(warmup_bars), today_df], ignore_index=True) if warmup_bars else today_df

    zone = ZoneInfo("Asia/Kolkata")
    ts = pd.to_datetime(df["timestamp"], utc=False)
    df["timestamp"] = ts.dt.tz_localize(zone) if ts.dt.tz is None else ts.dt.tz_convert(zone)
    df["session_date"] = df["timestamp"].dt.strftime("%Y-%m-%d")
    df = df.sort_values("timestamp").reset_index(drop=True)
    return fut_key, _compute_indicators(df)


def fetch_with_retry(
    cfg: AppConfig,
    session: date,
    *,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> FeedSnapshot:
    last_err: Exception | None = None
    for attempt in range(1, cfg.max_fetch_retries + 1):
        try:
            fut_key, df = fetch_session_dataframe(session, cfg.upstox_access_token, cfg.warmup_days)
            today = session.isoformat()
            today_df = df[df["session_date"] == today]
            if today_df.empty:
                raise DataFeedError("Today's session has no candles yet")
            return FeedSnapshot(
                futures_key=fut_key,
                dataframe=df,
                fetched_at=datetime.now(ZoneInfo(cfg.timezone)),
                today_bar_count=len(today_df),
            )
        except Exception as exc:
            last_err = exc
            if on_retry:
                on_retry(attempt, exc)
            if attempt < cfg.max_fetch_retries:
                time.sleep(cfg.retry_backoff_sec * attempt)
    raise DataFeedError(f"Failed after {cfg.max_fetch_retries} attempts: {last_err}") from last_err


def latest_complete_bar_label(
    snapshot: FeedSnapshot,
    now: datetime,
    cfg: AppConfig,
) -> datetime | None:
    """Return the open timestamp of the latest fully closed 5m bar in today's session."""
    today = now.astimezone(ZoneInfo(cfg.timezone)).date().isoformat()
    today_df = snapshot.dataframe[snapshot.dataframe["session_date"] == today]
    complete: datetime | None = None
    for _, row in today_df.iterrows():
        ts = row["timestamp"].to_pydatetime()
        if is_bar_complete(
            ts,
            now,
            bar_time_is_open=cfg.strategy.bar_time_is_open,
            buffer_sec=cfg.candle_close_buffer_sec,
        ):
            complete = ts
    return complete


def is_data_stale(snapshot: FeedSnapshot, now: datetime, cfg: AppConfig) -> bool:
    """Warn if the feed is far behind the expected last closed bar during market hours."""
    zone = ZoneInfo(cfg.timezone)
    now_ist = now.astimezone(zone)
    m = now_ist.hour * 60 + now_ist.minute
    open_m = cfg.strategy.market_open_h * 60 + cfg.strategy.market_open_m
    stop_m = cfg.strategy.monitor_stop_h * 60 + cfg.strategy.monitor_stop_m
    if m < open_m + 5 or m > stop_m:
        return False

    latest = latest_complete_bar_label(snapshot, now, cfg)
    if latest is None:
        return True

    expected_open = now_ist.replace(second=0, microsecond=0) - timedelta(minutes=5)
    # align to 5m grid from 9:15
    base = open_m
    close_min = m
    if cfg.strategy.bar_time_is_open:
        close_min = m  # current minute; last closed bar open is roughly now-5 aligned
    offset = close_min - base
    bucket = base + max(0, (offset // 5) * 5 - 5)
    bh, bm = divmod(bucket, 60)
    expected_open = now_ist.replace(hour=bh, minute=bm, second=0, microsecond=0)

    lag = abs((latest - expected_open).total_seconds())
    return lag > 600  # more than 10 minutes behind
