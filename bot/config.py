"""Configuration for replay (Phase 0) and live monitoring (Phase 1)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bot.strategy_j import JTrapConfig

# Hard safety default — no broker execution in this phase.
AUTO_TRADE: bool = False

# Frozen production strategy version (see docs/LOCKED_STRATEGY_v3.10.md)
LOCKED_STRATEGY_VERSION: str = "3.10"


@dataclass(frozen=True)
class StrategyConfig:
    """Strategy parameters — v3.7 entries, exit sweep winner (OR stop + close-only SL)."""

    or_minutes: int = 15
    or_bars_5m: int = 3
    min_or_range: float = 25.0
    max_or_range: float = 100.0

    use_vwap_filter: bool = True
    adx_length: int = 14
    adx_min: float = 18.0

    atr_length: int = 14
    use_stop_loss: bool = True
    atr_sl_mult: float = 1.4
    atr_target_mult: float = 1.2
    rr_ratio: float = 2.0
    sl_mode: str = "OR_RANGE"  # ATR | OR_RANGE | WIDER

    # Exit sweep winner: OR_RANGE stop, SL on bar close only, 10pt buffer
    close_only_sl: bool = True
    sl_delay_bars: int = 0
    sl_buffer_pts: float = 10.0

    market_open_h: int = 9
    market_open_m: int = 15
    square_off_h: int = 15
    square_off_m: int = 15
    monitor_stop_h: int = 15
    monitor_stop_m: int = 30
    max_trades_per_day: int = 2

    # v3.10: no new BUY_CE/BUY_PE after this bar close (IST, HH:MM); empty = unrestricted
    no_new_entries_after: str = "13:30"

    bar_time_is_open: bool = True
    timezone: str = "Asia/Kolkata"


@dataclass(frozen=True)
class CombinedStrategyConfig:
    """v3.9 combined router: v3.8 on valid OR days, J+ on wide OR days."""

    strategy: StrategyConfig
    enable_j_plus: bool
    j_trap: JTrapConfig


@dataclass(frozen=True)
class AppConfig:
    """Live monitoring application settings (from environment)."""

    upstox_access_token: str
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    ntfy_topic: str = ""
    ntfy_server: str = "https://ntfy.sh"
    auto_trade: bool = False

    strategy: StrategyConfig = StrategyConfig()
    combined: CombinedStrategyConfig | None = None

    state_file: Path = Path("data/live/monitor_state.json")
    event_log_csv: Path = Path("data/live/signals.csv")

    poll_interval_sec: int = 20
    candle_close_buffer_sec: int = 15
    warmup_days: int = 3
    max_fetch_retries: int = 3
    retry_backoff_sec: float = 2.0

    strike_mode: str = "ATM_OR_ITM1"  # ATM | ITM1 | ATM_OR_ITM1

    # Heads-up when OR is breached on the forming 5m bar (official entry still at bar close)
    enable_early_or_watch: bool = True

    timezone: str = "Asia/Kolkata"


DEFAULT_CONFIG = StrategyConfig()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _load_j_trap_config():
    from bot.strategy_j import JTrapConfig, J_TRAP_ROBUST

    return JTrapConfig(
        min_trap_excess_pts=float(os.getenv("J_MIN_TRAP_EXCESS", J_TRAP_ROBUST.min_trap_excess_pts)),
        min_reclaim_pts=float(os.getenv("J_MIN_RECLAIM_PTS", J_TRAP_ROBUST.min_reclaim_pts)),
        min_vwap_dist_pts=float(os.getenv("J_MIN_VWAP_DIST", J_TRAP_ROBUST.min_vwap_dist_pts)),
        min_body_ratio=float(os.getenv("J_MIN_BODY_RATIO", J_TRAP_ROBUST.min_body_ratio)),
        min_adx=float(os.getenv("J_ADX_MIN", J_TRAP_ROBUST.min_adx or 20.0)),
        skip_both_trapped=_env_bool("J_SKIP_BOTH_TRAPPED", J_TRAP_ROBUST.skip_both_trapped),
        min_minutes_after_or=_env_int("J_MINS_AFTER_OR", J_TRAP_ROBUST.min_minutes_after_or),
        max_trades_day=_env_int("J_MAX_TRADES_DAY", J_TRAP_ROBUST.max_trades_day),
        max_losses_day=_env_int("J_MAX_LOSSES_DAY", J_TRAP_ROBUST.max_losses_day),
        one_trap_side_only=_env_bool("J_ONE_TRAP_SIDE_ONLY", J_TRAP_ROBUST.one_trap_side_only),
    )


def load_combined_config(strategy: StrategyConfig | None = None) -> CombinedStrategyConfig:
    """Build combined v3.9 config from env (used by replay and live)."""
    strat = strategy or DEFAULT_CONFIG
    return CombinedStrategyConfig(
        strategy=strat,
        enable_j_plus=_env_bool("ENABLE_J_PLUS", True),
        j_trap=_load_j_trap_config(),
    )


def load_app_config(env_file: str | Path | None = ".env") -> AppConfig:
    """Load settings from .env and environment variables."""
    if env_file is not None:
        path = Path(env_file)
        if path.exists():
            try:
                from dotenv import load_dotenv

                load_dotenv(path)
            except ImportError:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key, value = key.strip(), value.strip().strip('"').strip("'")
                    os.environ.setdefault(key, value)

    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip()
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    ntfy_topic = os.getenv("NTIFY_TOPIC", "").strip()
    ntfy_server = os.getenv("NTIFY_SERVER", "https://ntfy.sh").strip() or "https://ntfy.sh"

    if _env_bool("AUTO_TRADE", False):
        raise RuntimeError("AUTO_TRADE must remain False in Phase 1 (alerts only).")

    strategy = StrategyConfig(
        min_or_range=float(os.getenv("MIN_OR_RANGE", DEFAULT_CONFIG.min_or_range)),
        max_or_range=float(os.getenv("MAX_OR_RANGE", DEFAULT_CONFIG.max_or_range)),
        adx_min=float(os.getenv("ADX_MIN", DEFAULT_CONFIG.adx_min)),
        use_stop_loss=_env_bool("USE_STOP_LOSS", DEFAULT_CONFIG.use_stop_loss),
        atr_sl_mult=float(os.getenv("ATR_SL_MULT", DEFAULT_CONFIG.atr_sl_mult)),
        atr_target_mult=float(os.getenv("ATR_TARGET_MULT", DEFAULT_CONFIG.atr_target_mult)),
        rr_ratio=float(os.getenv("RR_RATIO", DEFAULT_CONFIG.rr_ratio)),
        sl_mode=os.getenv("SL_MODE", DEFAULT_CONFIG.sl_mode),
        close_only_sl=_env_bool("CLOSE_ONLY_SL", DEFAULT_CONFIG.close_only_sl),
        sl_delay_bars=_env_int("SL_DELAY_BARS", DEFAULT_CONFIG.sl_delay_bars),
        sl_buffer_pts=float(os.getenv("SL_BUFFER_PTS", DEFAULT_CONFIG.sl_buffer_pts)),
        max_trades_per_day=_env_int("MAX_TRADES_PER_DAY", DEFAULT_CONFIG.max_trades_per_day),
        no_new_entries_after=os.getenv("NO_NEW_ENTRIES_AFTER", DEFAULT_CONFIG.no_new_entries_after),
        use_vwap_filter=_env_bool("USE_VWAP_FILTER", DEFAULT_CONFIG.use_vwap_filter),
        timezone=os.getenv("TIMEZONE", DEFAULT_CONFIG.timezone),
    )

    combined = load_combined_config(strategy)

    return AppConfig(
        upstox_access_token=token,
        telegram_bot_token=tg_token,
        telegram_chat_id=tg_chat,
        ntfy_topic=ntfy_topic,
        ntfy_server=ntfy_server,
        auto_trade=False,
        strategy=strategy,
        combined=combined,
        state_file=Path(os.getenv("STATE_FILE", "data/live/monitor_state.json")),
        event_log_csv=Path(os.getenv("EVENT_LOG_CSV", "data/live/signals.csv")),
        poll_interval_sec=_env_int("POLL_INTERVAL_SEC", 20),
        candle_close_buffer_sec=_env_int("CANDLE_CLOSE_BUFFER_SEC", 15),
        warmup_days=_env_int("WARMUP_DAYS", 3),
        max_fetch_retries=_env_int("MAX_FETCH_RETRIES", 3),
        retry_backoff_sec=float(os.getenv("RETRY_BACKOFF_SEC", "2.0")),
        strike_mode=os.getenv("STRIKE_MODE", "ATM_OR_ITM1").upper(),
        enable_early_or_watch=_env_bool("ENABLE_EARLY_OR_WATCH", True),
        timezone=os.getenv("TIMEZONE", "Asia/Kolkata"),
    )


def has_telegram_alerts(cfg: AppConfig) -> bool:
    return bool(cfg.telegram_bot_token and cfg.telegram_chat_id)


def has_ntfy_alerts(cfg: AppConfig) -> bool:
    return bool(cfg.ntfy_topic)


def validate_app_config(cfg: AppConfig) -> list[str]:
    """Return list of missing/invalid settings (empty = OK)."""
    errors: list[str] = []
    if not cfg.upstox_access_token:
        errors.append("UPSTOX_ACCESS_TOKEN is required")
    if cfg.telegram_bot_token and not cfg.telegram_chat_id:
        errors.append("TELEGRAM_CHAT_ID is required when TELEGRAM_BOT_TOKEN is set")
    if cfg.telegram_chat_id and not cfg.telegram_bot_token:
        errors.append("TELEGRAM_BOT_TOKEN is required when TELEGRAM_CHAT_ID is set")
    if not has_telegram_alerts(cfg) and not has_ntfy_alerts(cfg):
        errors.append(
            "Set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID and/or NTIFY_TOPIC for alerts"
        )
    if cfg.auto_trade or AUTO_TRADE is not False:
        errors.append("AUTO_TRADE must be False")
    return errors
