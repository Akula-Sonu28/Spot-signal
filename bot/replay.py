"""CSV candle replay driver for Phase 0 validation."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd
from zoneinfo import ZoneInfo

from bot.combined import process_session_bar
from bot.config import CombinedStrategyConfig, DEFAULT_CONFIG, StrategyConfig, load_combined_config
from bot.indicators import adx_series, atr_series, session_vwap_series
from bot.logger import ReplayLogger
from bot.state import ReplayState, make_day_state
from bot.strategy import build_bar_context


def load_candles_csv(path: str | Path, tz: str = "Asia/Kolkata") -> pd.DataFrame:
    """Load mock or historical 5m candles.

    Required columns: timestamp, open, high, low, close, volume.
    timestamp: ISO-8601 with timezone OR naive IST (interpreted as Asia/Kolkata).
    """
    df = pd.read_csv(path)
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing columns: {sorted(missing)}")

    zone = ZoneInfo(tz)
    ts = pd.to_datetime(df["timestamp"], utc=False)
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(zone)
    else:
        ts = ts.dt.tz_convert(zone)

    out = df.copy()
    out["timestamp"] = ts
    out["session_date"] = out["timestamp"].dt.strftime("%Y-%m-%d")
    out = out.sort_values("timestamp").reset_index(drop=True)
    return out


def _compute_indicators(df: pd.DataFrame, cfg: StrategyConfig = DEFAULT_CONFIG) -> pd.DataFrame:
    highs = df["high"].astype(float).tolist()
    lows = df["low"].astype(float).tolist()
    closes = df["close"].astype(float).tolist()
    volumes = df["volume"].astype(float).tolist()
    session_ids = df["session_date"].tolist()

    df = df.copy()
    if "vwap" not in df.columns:
        typical = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
        df["vwap"] = session_vwap_series(typical, volumes, session_ids)
    df["atr"] = atr_series(highs, lows, closes, cfg.atr_length)
    df["adx"] = adx_series(highs, lows, closes, cfg.adx_length)
    return df


def run_replay(
    df: pd.DataFrame,
    cfg: StrategyConfig = DEFAULT_CONFIG,
    logger: ReplayLogger | None = None,
    combined_cfg: CombinedStrategyConfig | None = None,
) -> ReplayLogger:
    """Replay candles bar-by-bar; indicators use only data up to each bar."""
    combined = combined_cfg or load_combined_config(cfg)
    logger = logger or ReplayLogger()
    state = ReplayState()
    zone = ZoneInfo(cfg.timezone)

    current_session: str | None = None
    for i, row in df.iterrows():
        session_date = str(row["session_date"])
        if session_date != current_session:
            current_session = session_date
            state.day = make_day_state(session_date)
            state.position = state.position.__class__()

        # Rolling indicators: slice history to avoid look-ahead in tests that pass full df
        hist = df.iloc[: i + 1]
        h = hist["high"].astype(float).tolist()
        l = hist["low"].astype(float).tolist()
        c = hist["close"].astype(float).tolist()
        v = hist["volume"].astype(float).tolist()
        tp = [(hh + ll + cc) / 3 for hh, ll, cc in zip(h, l, c)]
        sid = hist["session_date"].tolist()
        vwap_i = session_vwap_series(tp, v, sid)[-1]
        atr_i = atr_series(h, l, c, cfg.atr_length)[-1]
        adx_i = adx_series(h, l, c, cfg.adx_length)[-1]

        bar = build_bar_context(
            index=int(i),
            timestamp=row["timestamp"].to_pydatetime(),
            session_date=session_date,
            o=float(row["open"]),
            h=float(row["high"]),
            l=float(row["low"]),
            c=float(row["close"]),
            vol=float(row["volume"]),
            vwap=vwap_i,
            atr=atr_i,
            adx=adx_i,
            cfg=cfg,
            tz=zone,
        )
        process_session_bar(state, bar, logger, combined)

    return logger


def run_replay_fast(
    df: pd.DataFrame,
    cfg: StrategyConfig = DEFAULT_CONFIG,
    logger: ReplayLogger | None = None,
    combined_cfg: CombinedStrategyConfig | None = None,
) -> ReplayLogger:
    """Faster replay using precomputed indicator columns on full dataframe."""
    enriched = _compute_indicators(df, cfg)
    combined = combined_cfg or load_combined_config(cfg)
    logger = logger or ReplayLogger()
    state = ReplayState()
    zone = ZoneInfo(cfg.timezone)
    current_session: str | None = None

    for i, row in enriched.iterrows():
        session_date = str(row["session_date"])
        if session_date != current_session:
            current_session = session_date
            state.day = make_day_state(session_date)
            from bot.state import Position

            state.position = Position()

        bar = build_bar_context(
            index=int(i),
            timestamp=row["timestamp"].to_pydatetime(),
            session_date=session_date,
            o=float(row["open"]),
            h=float(row["high"]),
            l=float(row["low"]),
            c=float(row["close"]),
            vol=float(row["volume"]),
            vwap=float(row["vwap"]) if pd.notna(row["vwap"]) else None,
            atr=float(row["atr"]) if pd.notna(row["atr"]) else None,
            adx=float(row["adx"]) if pd.notna(row["adx"]) else None,
            cfg=cfg,
            tz=zone,
        )
        process_session_bar(state, bar, logger, combined)

    return logger


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay NIFTY 5m candles through v3.9 combined strategy")
    parser.add_argument("csv", type=Path, help="Path to candle CSV")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional path to write event log CSV",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use precomputed indicators (faster, same result)",
    )
    args = parser.parse_args()

    df = load_candles_csv(args.csv)
    logger = run_replay_fast(df) if args.fast else run_replay(df)

    for event in logger.events:
        print(event.to_json())

    if args.out:
        logger.write_csv(args.out)
        print(f"Wrote {len(logger.events)} events to {args.out}")


if __name__ == "__main__":
    main()
