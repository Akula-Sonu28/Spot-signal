"""Futures-derived VWAP for spot/index signals (basis-adjusted)."""

from __future__ import annotations

import gzip
import json
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    import pandas as pd
from zoneinfo import ZoneInfo

from bot.indicators import session_vwap_series

INDEX_INSTRUMENT = "NSE_INDEX|Nifty 50"
NSE_JSON_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
INTRADAY_URL = "https://api.upstox.com/v3/historical-candle/intraday/{key}/minutes/5"


@dataclass(frozen=True)
class MergedBar:
    timestamp: datetime
    session_date: str
    spot_open: float
    spot_high: float
    spot_low: float
    spot_close: float
    fut_close: float
    fut_volume: float
    futures_vwap: float | None
    spot_vwap_proxy: float | None
    basis: float


def _encode_key(instrument_key: str) -> str:
    from urllib.parse import quote

    return quote(instrument_key, safe="")


def _http_json(url: str, token: str | None = None) -> dict:
    headers = {"Accept": "application/json", "User-Agent": "nifty-spot-signal-engine/0.1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def nearest_nifty_future_key(as_of: date | None = None) -> str:
    """Return instrument_key for nearest NIFTY index future (not BankNifty/Finnifty)."""
    as_of = as_of or date.today()
    req = urllib.request.Request(NSE_JSON_URL, headers={"User-Agent": "nifty-spot-signal-engine/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = gzip.decompress(resp.read())
    instruments = json.loads(raw)

    candidates: list[tuple[date, str, str]] = []
    for inst in instruments:
        if inst.get("instrument_type") != "FUT":
            continue
        sym = inst.get("trading_symbol") or ""
        if not sym.startswith("NIFTY "):
            continue
        exp = inst.get("expiry")
        if isinstance(exp, int):
            exp_date = datetime.fromtimestamp(exp / 1000, tz=ZoneInfo("Asia/Kolkata")).date()
        elif isinstance(exp, str):
            exp_date = date.fromisoformat(exp[:10])
        else:
            continue
        if exp_date < as_of:
            continue
        candidates.append((exp_date, inst["instrument_key"], sym))

    if not candidates:
        raise RuntimeError("No active NIFTY futures found in Upstox instrument master")
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def fetch_intraday_5m(instrument_key: str, token: str | None = None) -> list[list]:
    url = INTRADAY_URL.format(key=_encode_key(instrument_key))
    payload = _http_json(url, token=token)
    if payload.get("status") != "success":
        raise RuntimeError(f"Upstox candle fetch failed: {payload}")
    candles = payload["data"]["candles"]
    candles.sort(key=lambda c: c[0])
    return candles


def basis_adjusted_vwap_series(
    futures_highs: Sequence[float],
    futures_lows: Sequence[float],
    futures_closes: Sequence[float],
    futures_volumes: Sequence[float],
    spot_closes: Sequence[float],
    session_ids: Sequence[str],
) -> tuple[list[float | None], list[float | None]]:
    """Compute futures VWAP and translate to spot price scale using per-bar basis."""
    typical = [(h + l + c) / 3.0 for h, l, c in zip(futures_highs, futures_lows, futures_closes)]
    fut_vwap = session_vwap_series(typical, futures_volumes, session_ids)
    spot_proxy: list[float | None] = [None] * len(fut_vwap)
    for i, fv in enumerate(fut_vwap):
        if fv is None:
            continue
        spot_proxy[i] = fv - (futures_closes[i] - spot_closes[i])
    return fut_vwap, spot_proxy


def merge_index_and_futures(
    index_candles: list[list],
    futures_candles: list[list],
    tz: str = "Asia/Kolkata",
) -> list[MergedBar]:
    """Align spot index and futures bars by timestamp; attach basis-adjusted VWAP."""
    zone = ZoneInfo(tz)
    fut_by_ts = {c[0]: c for c in futures_candles}
    rows: list[MergedBar] = []

    aligned_index = [c for c in index_candles if c[0] in fut_by_ts]
    if not aligned_index:
        return rows

    f_highs, f_lows, f_closes, f_vols, s_closes, sids, timestamps = [], [], [], [], [], [], []
    for c in aligned_index:
        f = fut_by_ts[c[0]]
        f_highs.append(float(f[2]))
        f_lows.append(float(f[3]))
        f_closes.append(float(f[4]))
        f_vols.append(float(f[5]))
        s_closes.append(float(c[4]))
        ts = datetime.fromisoformat(c[0])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=zone)
        timestamps.append(ts)
        sids.append(ts.strftime("%Y-%m-%d"))

    fut_vwap, spot_vwap = basis_adjusted_vwap_series(
        f_highs, f_lows, f_closes, f_vols, s_closes, sids
    )

    for i, c in enumerate(aligned_index):
        f = fut_by_ts[c[0]]
        rows.append(
            MergedBar(
                timestamp=timestamps[i],
                session_date=sids[i],
                spot_open=float(c[1]),
                spot_high=float(c[2]),
                spot_low=float(c[3]),
                spot_close=float(c[4]),
                fut_close=float(f[4]),
                fut_volume=float(f[5]),
                futures_vwap=fut_vwap[i],
                spot_vwap_proxy=spot_vwap[i],
                basis=float(f[4]) - float(c[4]),
            )
        )
    return rows


def vwap_long_ok(spot_close: float, spot_vwap_proxy: float | None) -> bool:
    return spot_vwap_proxy is not None and spot_close > spot_vwap_proxy


def vwap_short_ok(spot_close: float, spot_vwap_proxy: float | None) -> bool:
    return spot_vwap_proxy is not None and spot_close < spot_vwap_proxy


def fetch_historical_1m(
    instrument_key: str,
    from_date: str,
    to_date: str,
    token: str | None = None,
) -> list[list]:
    """Fetch 1-minute historical candles (v2 API)."""
    key = _encode_key(instrument_key)
    url = f"https://api.upstox.com/v2/historical-candle/{key}/1minute/{to_date}/{from_date}"
    payload = _http_json(url, token=token)
    if payload.get("status") != "success":
        raise RuntimeError(f"Upstox historical fetch failed: {payload}")
    candles = payload["data"]["candles"]
    candles.sort(key=lambda c: c[0])
    return candles


def aggregate_1m_to_5m(candles_1m: list[list], market_open_min: int = 9 * 60 + 15) -> list[list]:
    """Aggregate 1m OHLCV to 5m bars using session bucket starts (matches Upstox 5m labels)."""
    if not candles_1m:
        return []

    buckets: dict[str, list[list]] = {}
    for c in candles_1m:
        ts = datetime.fromisoformat(c[0])
        bar_min = ts.hour * 60 + ts.minute
        offset = bar_min - market_open_min
        if offset < 0:
            continue
        bucket_start = market_open_min + (offset // 5) * 5
        bh, bm = divmod(bucket_start, 60)
        label = ts.replace(hour=bh, minute=bm, second=0, microsecond=0).isoformat()
        buckets.setdefault(label, []).append(c)

    out: list[list] = []
    for label in sorted(buckets.keys()):
        bars = buckets[label]
        o = float(bars[0][1])
        h = max(float(b[2]) for b in bars)
        l = min(float(b[3]) for b in bars)
        cl = float(bars[-1][4])
        vol = sum(float(b[5]) for b in bars)
        out.append([label, o, h, l, cl, vol, 0])
    return out


def merged_bars_to_dataframe(bars: Sequence[MergedBar]) -> "pd.DataFrame":
    """Convert merged spot+futures bars to replay dataframe with futures VWAP."""
    import pandas as pd

    rows = [
        {
            "timestamp": b.timestamp,
            "open": b.spot_open,
            "high": b.spot_high,
            "low": b.spot_low,
            "close": b.spot_close,
            "volume": 0.0,
            "vwap": b.spot_vwap_proxy,
            "session_date": b.session_date,
        }
        for b in bars
    ]
    return pd.DataFrame(rows)


def fetch_merged_today(token: str | None = None, as_of: date | None = None) -> tuple[str, list[MergedBar]]:
    """Pull today's index + nearest NIFTY future and return merged VWAP bars."""
    fut_key = nearest_nifty_future_key(as_of=as_of)
    index_candles = fetch_intraday_5m(INDEX_INSTRUMENT, token=token)
    futures_candles = fetch_intraday_5m(fut_key, token=token)
    return fut_key, merge_index_and_futures(index_candles, futures_candles)
