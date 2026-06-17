"""Live NIFTY signal monitor — Telegram alerts only (Phase 1)."""

from __future__ import annotations

import argparse
import signal
import sys

from bot.alerts import create_alerter
from bot.config import AUTO_TRADE, LOCKED_STRATEGY_VERSION, load_app_config, validate_app_config
from bot.logger import LiveEventLogger
from bot.scheduler import LiveScheduler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NIFTY Spot Signal Engine — live alerts")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--once", action="store_true", help="Run a single tick (testing)")
    args = parser.parse_args(argv)

    if AUTO_TRADE is not False:
        print("FATAL: AUTO_TRADE must be False", file=sys.stderr)
        return 1

    cfg = load_app_config(args.env)
    errors = validate_app_config(cfg)
    if errors:
        for err in errors:
            print(f"Config error: {err}", file=sys.stderr)
        return 1

    alerter = create_alerter(cfg)
    event_log = LiveEventLogger(cfg.event_log_csv)
    scheduler = LiveScheduler(cfg, alerter, event_log)

    def _shutdown(signum: int, _frame: object) -> None:
        scheduler.stop()
        try:
            alerter.bot_stopped(f"signal {signum}")
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    if args.once:
        try:
            alerter.bot_started()
            event_log.log_system(
                "BOT_STARTED",
                f"Live monitor started strategy=v{LOCKED_STRATEGY_VERSION} (AUTO_TRADE=False)",
            )
        except Exception as exc:
            print(f"Failed to send startup alert: {exc}", file=sys.stderr)
            return 1
        scheduler.process_tick()
        print("Single tick complete.")
        return 0

    from bot.scheduler import after_monitor_close

    now = scheduler._now()
    if after_monitor_close(now, cfg.strategy):
        print("Market closed for today (after 15:30 IST). Exiting without starting.")
        return 0

    try:
        alerter.bot_started()
        event_log.log_system(
            "BOT_STARTED",
            f"Live monitor started strategy=v{LOCKED_STRATEGY_VERSION} (AUTO_TRADE=False)",
        )
    except Exception as exc:
        print(f"Failed to send startup alert: {exc}", file=sys.stderr)
        return 1

    ran_session = False
    try:
        scheduler.run_forever()
        ran_session = True
    finally:
        if ran_session:
            try:
                alerter.bot_stopped("market close or manual stop")
                event_log.log_system("BOT_STOPPED", "Monitor stopped")
            except Exception:
                pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
