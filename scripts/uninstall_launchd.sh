#!/bin/bash
set -euo pipefail

UID_NUM="$(id -u)"
GUI_DOMAIN="gui/$UID_NUM"

for LABEL in com.nifty-spot-signal-engine.market com.nifty-spot-signal-engine.telegram; do
  PLIST_DST="$HOME/Library/LaunchAgents/${LABEL}.plist"
  launchctl bootout "$GUI_DOMAIN/$LABEL" 2>/dev/null || \
    launchctl unload "$PLIST_DST" 2>/dev/null || true
  rm -f "$PLIST_DST"
  echo "Removed $PLIST_DST"
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOCK="$ROOT/data/live/market-session.lock"
if [[ -f "$LOCK" ]]; then
  pid="$(cat "$LOCK" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Stopping running session pid=$pid"
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$LOCK"
fi

echo "Done."
