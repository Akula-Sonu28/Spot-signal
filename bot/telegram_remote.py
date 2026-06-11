"""Always-on Telegram command listener (start/stop/status the market bot)."""

from __future__ import annotations

import argparse
import signal
import sys

from bot.alerts import TelegramAlerter
from bot.config import load_app_config, validate_app_config
from bot.telegram_commands import run_remote_loop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NIFTY Signal Engine — Telegram remote control")
    parser.add_argument("--env", default=".env", help="Path to .env file")
    parser.add_argument("--poll-interval-sec", type=int, default=2, help="Pause between poll cycles")
    args = parser.parse_args(argv)

    cfg = load_app_config(args.env)
    errors = validate_app_config(cfg)
    if errors:
        for err in errors:
            print(f"Config error: {err}", file=sys.stderr)
        return 1

    alerter = TelegramAlerter(cfg)

    def _shutdown(signum: int, _frame: object) -> None:
        try:
            alerter.send(f"📟 Remote control stopped (signal {signum})")
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    run_remote_loop(cfg, alerter, poll_interval_sec=args.poll_interval_sec)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
