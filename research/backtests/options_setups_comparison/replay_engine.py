"""Research replay engine with injectable setup processors."""

from __future__ import annotations

from typing import Callable

import pandas as pd
from zoneinfo import ZoneInfo

from bot.config import StrategyConfig
from bot.logger import ReplayLogger
from bot.replay import _compute_indicators, run_replay_fast
from bot.state import Position
from bot.strategy import build_bar_context

from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.indicators_ext import (
    ema_series,
    prior_session_closes,
)
from research.backtests.options_setups_comparison.risk_engine import (
    ResearchState,
    SkippedLogger,
    make_research_day,
)
from research.backtests.options_setups_comparison.setups.baseline import process_baseline_bar
from research.backtests.options_setups_comparison.setups.compression import process_compression_bar
from research.backtests.options_setups_comparison.setups.orb_breakout import process_orb_breakout_bar
from research.backtests.options_setups_comparison.setups.trend_pullback import process_trend_pullback_bar
from research.backtests.options_setups_comparison.setups.vwap_reclaim import process_vwap_reclaim_bar
from research.backtests.options_setups_comparison.slippage import SlippageModel

SetupFn = Callable[..., None]


def apply_slippage_to_events(logger: ReplayLogger, slippage: SlippageModel) -> ReplayLogger:
    """Post-hoc slippage for production baseline replay."""
    from bot.backtest import ENTRY_TYPES, EXIT_TYPES
    from bot.state import PositionSide

    adjusted = ReplayLogger()
    for event in logger.events:
        price = event.price
        if event.event_type in ENTRY_TYPES:
            side = PositionSide.CE if event.side == "CE" else PositionSide.PE
            price = slippage.adjust_entry(side, price)
        elif event.event_type in EXIT_TYPES or event.event_type == "SQUARE_OFF":
            side = PositionSide.CE if event.side == "CE" else PositionSide.PE
            price = slippage.adjust_exit(side, price)
        adjusted.log(
            timestamp=event.timestamp,
            event_type=event.event_type,
            direction=event.direction,
            side=event.side,
            price=price,
            stop=event.stop,
            target=event.target,
            reason=event.reason,
            bar_index=event.bar_index,
            **event.extra,
        )
    return adjusted


def run_setup_replay(
    df: pd.DataFrame,
    setup_id: str,
    processor: SetupFn,
    cfg: StrategyConfig,
    research: ResearchConfig,
    slippage: SlippageModel,
    *,
    use_production_baseline: bool = False,
) -> tuple[ReplayLogger, SkippedLogger]:
    """Run one setup over dataframe."""
    if setup_id == "A" and use_production_baseline:
        logger = run_replay_fast(df, cfg)
        if slippage.tier != "none":
            logger = apply_slippage_to_events(logger, slippage)
        return logger, SkippedLogger()

    enriched = _compute_indicators(df, cfg)
    logger = ReplayLogger()
    skipped = SkippedLogger()
    zone = ZoneInfo(cfg.timezone)

    closes = enriched["close"].astype(float).tolist()
    highs = enriched["high"].astype(float).tolist()
    lows = enriched["low"].astype(float).tolist()
    ema9_list = ema_series(closes, 9)
    ema20_list = ema_series(closes, 20)
    vwaps = [float(v) if pd.notna(v) else None for v in enriched["vwap"]]
    session_dates = enriched["session_date"].astype(str).tolist()
    prior_closes = prior_session_closes(enriched, session_dates)

    state = ResearchState()
    current_session: str | None = None
    prev_atr: float | None = None

    for i, row in enriched.iterrows():
        session_date = str(row["session_date"])
        if session_date != current_session:
            current_session = session_date
            state = ResearchState(
                position=Position(),
                day=make_research_day(session_date),
                prior_close=prior_closes.get(session_date),
            )
            if state.day:
                state.day.setup_flags["first_entry_bar"] = int(i)
                state.day.setup_flags["setup_id"] = setup_id

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

        if setup_id == "A" and not use_production_baseline:
            process_orb_breakout_bar(
                state, bar, cfg, research, logger, slippage, skipped,
            )
        elif setup_id == "A":
            process_baseline_bar(
                state, bar, cfg, research, logger, slippage, skipped,
                use_production=True,
            )
        elif setup_id == "D":
            process_vwap_reclaim_bar(
                state, bar, cfg, research, logger, slippage, skipped,
                highs=highs, lows=lows,
            )
        elif setup_id == "E":
            process_compression_bar(
                state, bar, cfg, research, logger, slippage, skipped,
                highs=highs, lows=lows, prev_atr=prev_atr,
            )
        elif setup_id == "G":
            process_trend_pullback_bar(
                state, bar, cfg, research, logger, slippage, skipped,
                ema9=ema9_list[int(i)],
                ema20=ema20_list[int(i)],
                vwaps=vwaps,
                highs=highs,
                lows=lows,
            )
        else:
            processor(state, bar, cfg, research, logger, slippage, skipped)

        prev_atr = float(row["atr"]) if pd.notna(row["atr"]) else prev_atr

    return logger, skipped
