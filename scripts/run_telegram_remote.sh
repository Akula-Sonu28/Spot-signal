#!/bin/bash
# Always-on Telegram command listener (single getUpdates poller per bot token).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${NIFTY_PYTHON:-python3}"
exec "$PYTHON" -m bot.telegram_remote
