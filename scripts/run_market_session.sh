#!/bin/bash
# Start live bot with caffeinate for one market session (09:15–15:30 IST).
# Invoked by launchd on weekdays; exits when bot.main stops after 15:30.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

LOG_DIR="$ROOT/data/live"
LOG="$LOG_DIR/market-session.log"
LOCK="$LOG_DIR/market-session.lock"
PYTHON="${NIFTY_PYTHON:-python3}"

mkdir -p "$LOG_DIR"

log() {
  echo "$(date '+%Y-%m-%d %H:%M:%S %Z') $*" >>"$LOG"
}

if [[ -f "$LOCK" ]]; then
  old_pid="$(cat "$LOCK" 2>/dev/null || true)"
  if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
    log "SKIP already running pid=$old_pid"
    exit 0
  fi
  rm -f "$LOCK"
fi

log "START project=$ROOT"
echo $$ >"$LOCK"
trap 'rm -f "$LOCK"; log "END"' EXIT

# -s: prevent system sleep while bot runs (until ~15:30 IST exit)
# -m: prevent disk sleep during session
# -i: prevent idle sleep (display may still dim)
exec caffeinate -s -m -i "$PYTHON" -m bot.main >>"$LOG" 2>&1
