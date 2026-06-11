"""Fetch and cache multi-day NIFTY 5m bars (index OHLC + futures VWAP)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Sequence

import pandas as pd
from zoneinfo import ZoneInfo

from bot.futures_vwap import (
    INDEX_INSTRUMENT,
    aggregate_1m_to_5m,
    fetch_historical_1m,
    fetch_merged_today,
    merge_index_and_futures,
    merged_bars_to_dataframe,
    nearest_nifty_future_key,
)
from bot.indicators import session_vwap_series

TZ = ZoneInfo("Asia/Kolkata")
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "historical"


def prior_trading_days(session: date, count: int) -> list[date]:
    found: list[date] = []
    d = session - timedelta(days=1)
    while len(found) < count and (session - d).days < 30:
        if d.weekday() < 5:
            found.append(d)
        d -= timedelta(days=1)
    return list(reversed(found))


def trading_days_back(from_day: date, count: int) -> list[date]:
    """Return `count` weekdays ending at `from_day` (inclusive)."""
    days: list[date] = []
    d = from_day
    while len(days) < count:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def _has_1m_data(instrument: str, day: date, token: str | None) -> bool:
    ds = day.isoformat()
    try:
        return len(fetch_historical_1m(instrument, ds, ds, token=token)) > 0
    except Exception:
        return False


def _index_5m_to_dataframe(candles_5m: list[list], *, vwap_source: str) -> pd.DataFrame:
    """Index OHLC 5m bars with session TWAP proxy when futures history is unavailable."""
    if not candles_5m:
        return pd.DataFrame()

    timestamps: list[datetime] = []
    opens, highs, lows, closes = [], [], [], []
    session_ids: list[str] = []
    for c in candles_5m:
        ts = datetime.fromisoformat(c[0])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=TZ)
        timestamps.append(ts)
        opens.append(float(c[1]))
        highs.append(float(c[2]))
        lows.append(float(c[3]))
        closes.append(float(c[4]))
        session_ids.append(ts.strftime("%Y-%m-%d"))

    typical = [(h + l + cl) / 3.0 for h, l, cl in zip(highs, lows, closes)]
    unit_vol = [1.0] * len(typical)
    vwap = session_vwap_series(typical, unit_vol, session_ids)

    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": 0.0,
            "vwap": vwap,
            "session_date": session_ids,
            "vwap_source": vwap_source,
        }
    )


def fetch_session_merged(day: date, token: str | None = None) -> pd.DataFrame | None:
    """One session of 5m bars with futures VWAP, or index TWAP proxy as fallback."""
    if day == date.today():
        try:
            _, bars = fetch_merged_today(token=token, as_of=day)
            if not bars:
                return None
            df = merged_bars_to_dataframe(bars)
            df["vwap_source"] = "futures"
            return df
        except Exception:
            return None

    if not _has_1m_data(INDEX_INSTRUMENT, day, token):
        return None

    ds = day.isoformat()
    idx_1m = fetch_historical_1m(INDEX_INSTRUMENT, ds, ds, token=token)
    if not idx_1m:
        return None

    idx_5m = aggregate_1m_to_5m(idx_1m)
    fut_key = nearest_nifty_future_key(as_of=day)
    try:
        fut_1m = fetch_historical_1m(fut_key, ds, ds, token=token)
    except Exception:
        fut_1m = []

    if fut_1m:
        fut_5m = aggregate_1m_to_5m(fut_1m)
        merged = merge_index_and_futures(idx_5m, fut_5m)
        if merged:
            df = merged_bars_to_dataframe(merged)
            df["vwap_source"] = "futures"
            return df

    return _index_5m_to_dataframe(idx_5m, vwap_source="twap_proxy")


def _normalize_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    ts = pd.to_datetime(out["timestamp"], utc=False)
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(TZ)
    else:
        ts = ts.dt.tz_convert(TZ)
    out["timestamp"] = ts
    out["session_date"] = out["timestamp"].dt.strftime("%Y-%m-%d")
    return out.sort_values("timestamp").reset_index(drop=True)


def load_cached_session(day: date) -> pd.DataFrame | None:
    path = CACHE_DIR / f"{day.isoformat()}_5m.csv"
    if not path.exists():
        return None
    return _normalize_timestamps(pd.read_csv(path))


def save_cached_session(day: date, df: pd.DataFrame) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{day.isoformat()}_5m.csv"
    df.to_csv(path, index=False)
    return path


def build_backtest_dataframe(
    session_days: Sequence[date],
    token: str | None = None,
    *,
    warmup_days: int = 3,
    use_cache: bool = True,
) -> tuple[pd.DataFrame, list[str], list[str], dict[str, int]]:
    """Build replay dataframe with indicator warmup before first session.

    Returns (dataframe, loaded_sessions, skipped_sessions, vwap_source_counts).
    """
    if not session_days:
        raise ValueError("session_days must not be empty")

    ordered = sorted(session_days)
    warmup = prior_trading_days(ordered[0], warmup_days) if warmup_days > 0 else []
    all_days = warmup + ordered

    frames: list[pd.DataFrame] = []
    loaded: list[str] = []
    skipped: list[str] = []
    vwap_counts: dict[str, int] = {"futures": 0, "twap_proxy": 0}

    total = len(all_days)
    for i, day in enumerate(all_days, start=1):
        ds = day.isoformat()
        df: pd.DataFrame | None = None
        if use_cache:
            df = load_cached_session(day)
            if df is not None and "vwap_source" not in df.columns:
                df = df.copy()
                df["vwap_source"] = "futures"
        if df is None:
            if i % 25 == 1 or i == total:
                print(f"  Fetching {i}/{total}: {ds} ...", flush=True)
            df = fetch_session_merged(day, token=token)
            if df is not None and use_cache:
                save_cached_session(day, df)
        if df is None or df.empty:
            if day in ordered:
                skipped.append(ds)
            continue
        norm = _normalize_timestamps(df)
        frames.append(norm)
        if day in ordered:
            loaded.append(ds)
            src = str(norm["vwap_source"].iloc[0]) if "vwap_source" in norm.columns else "unknown"
            vwap_counts[src] = vwap_counts.get(src, 0) + 1

    if not frames:
        raise RuntimeError("No session data loaded for backtest")

    full = pd.concat(frames, ignore_index=True)
    full = full.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
    return full.reset_index(drop=True), loaded, skipped, vwap_counts
