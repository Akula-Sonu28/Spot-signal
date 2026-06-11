#!/bin/bash
# Install launchd jobs:
#   1) Telegram remote control (always on)
#   2) Market session bot (Mon–Fri 09:10 IST)

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
UID_NUM="$(id -u)"
GUI_DOMAIN="gui/$UID_NUM"
mkdir -p "$ROOT/data/live"

chmod +x "$ROOT/scripts/run_market_session.sh"
chmod +x "$ROOT/scripts/run_telegram_remote.sh"

install_job() {
  local label="$1"
  local plist_src="$2"
  local plist_dst="$HOME/Library/LaunchAgents/${label}.plist"

  sed "s|__PROJECT_ROOT__|$ROOT|g" "$plist_src" >"$plist_dst"

  launchctl bootout "$GUI_DOMAIN/$label" 2>/dev/null || \
    launchctl unload "$plist_dst" 2>/dev/null || true

  if launchctl bootstrap "$GUI_DOMAIN" "$plist_dst" 2>/dev/null; then
    echo "Loaded (bootstrap): $plist_dst"
  else
    launchctl load "$plist_dst"
    echo "Loaded: $plist_dst"
  fi
}

install_job "com.nifty-spot-signal-engine.telegram" \
  "$ROOT/scripts/com.nifty-spot-signal-engine.telegram.plist"

install_job "com.nifty-spot-signal-engine.market" \
  "$ROOT/scripts/com.nifty-spot-signal-engine.market.plist"

echo ""
echo "Telegram remote: always on — /help, /start, /stop, /status, /tick"
echo "Market session:  Mon–Fri 09:10 IST → bot.main until ~15:30 IST"
echo "Logs:            $ROOT/data/live/market-session.log"
echo "                 $ROOT/data/live/telegram-remote.out.log"
echo ""
echo "Test market:  $ROOT/scripts/run_market_session.sh"
echo "Test remote:  $ROOT/scripts/run_telegram_remote.sh"
echo "Uninstall:    $ROOT/scripts/uninstall_launchd.sh"
