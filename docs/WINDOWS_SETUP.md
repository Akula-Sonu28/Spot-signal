# Windows Setup Guide

Complete setup for running the NIFTY Spot Signal Engine on Windows 10/11.

Phase 1 is **alerts only** — no broker orders (`AUTO_TRADE` is hardcoded `False`).

This guide matches the **production Windows stack** in the repo: hidden background tasks, duplicate-instance guards, SSL fallback for corporate networks, and enhanced Telegram messages.

## What runs on Windows

| Component | Path | Role |
|-----------|------|------|
| Core bot | `py -m bot.main` or `python -m bot.main` | Live signal engine |
| Telegram remote | `py -m bot.telegram_remote` | `/start`, `/stop`, `/status`, `/tick` |
| Session wrapper | `scripts/run_market_session.ps1` | Starts bot, prevents sleep, lock/log, crash safety-net |
| Remote wrapper | `scripts/run_telegram_remote.ps1` | Always-on Telegram listener (single-instance lock) |
| Task installer | `scripts/install_windows_task.ps1` | Registers both tasks, starts Telegram immediately |
| Process control | `bot/platform_paths.py` | Hidden PowerShell launch, `taskkill` on stop |

**macOS-only (ignore on Windows):** `scripts/*.sh`, `install_launchd.sh`, `com.nifty-spot-signal-engine.*.plist`

## Production features (built in)

| Feature | Where | What it does |
|---------|-------|--------------|
| Hidden background | `install_windows_task.ps1`, `platform_paths.py` | No PowerShell popups; runs with `-WindowStyle Hidden` |
| `py` launcher fallback | `run_*.ps1` | Uses `NIFTY_PYTHON` → `py` → `python` automatically |
| Duplicate guard | `run_telegram_remote.ps1` | `telegram-remote.lock` — only one Telegram poller per bot token |
| Single task instance | `install_windows_task.ps1` | `MultipleInstances: IgnoreNew` on scheduled tasks |
| Auto-restart | Telegram task | Restarts up to 999 times if the remote listener crashes |
| SSL fallback | `alerts.py`, `futures_vwap.py` | Retries HTTPS without cert verify (corporate proxy fix) |
| Crash safety-net | `run_market_session.ps1` | Sends Telegram “session ended unexpectedly” if bot dies without a clean stop |
| Rich `/status` | `telegram_commands.py` | Human-readable session, OR, trades, position |
| Rich trade alerts | `alerts.py` | Breakout distance, R:R, ADX/VWAP/ATR, option OI/spread, P&L on exits |

## Architecture

```
Task Scheduler (logon) — hidden, no window
    └── run_telegram_remote.ps1  [telegram-remote.lock]
            └── py/python -m bot.telegram_remote
                    └── /start → run_market_session.ps1 (hidden)

Task Scheduler (Mon–Fri 09:10) — hidden, no window
    └── run_market_session.ps1  [market-session.lock]
            ├── SetThreadExecutionState (prevent sleep)
            └── py/python -m bot.main
                    ├── Upstox REST (SSL fallback)
                    ├── combined.py (v3.9 day router)
                    └── Telegram alerts (enhanced formatting)
```

## Prerequisites

### Python 3.9+

Download from [python.org](https://www.python.org/downloads/windows/). During install, check:

- **Add python.exe to PATH**
- **Install pip**
- **Install py launcher** (usually included)

Verify in PowerShell:

```powershell
py --version
python --version
pip --version
```

Scripts auto-pick: `NIFTY_PYTHON` env var → `py` → `python`. Override only if needed:

```powershell
[System.Environment]::SetEnvironmentVariable(
    "NIFTY_PYTHON",
    "C:\Users\YOU\AppData\Local\Programs\Python\Python312\python.exe",
    "User"
)
```

Restart PowerShell after changing `NIFTY_PYTHON`.

### Project folder

```powershell
cd C:\Users\YOU\Projects
git clone https://github.com/Akula-Sonu28/Spot-signal.git
cd Spot-signal
git pull origin main
```

Prefer paths without spaces (e.g. `C:\Projects\Spot-signal`).

### Dependencies

```powershell
cd C:\Projects\Spot-signal
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
```

`tzdata` is required on Windows for `Asia/Kolkata` timezone support.

## Credentials (`.env`)

```powershell
copy .env.example .env
notepad .env
```

| Variable | How to get it |
|----------|----------------|
| `UPSTOX_ACCESS_TOKEN` | Upstox developer portal → generate access token (expires; refresh when needed) |
| `TELEGRAM_BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `TELEGRAM_CHAT_ID` | Message your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and read `"chat":{"id":...}` |

Example `.env`:

```env
UPSTOX_ACCESS_TOKEN=your_upstox_access_token_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_numeric_chat_id_here
AUTO_TRADE=false
```

### Strategy v3.9 (optional overrides)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_J_PLUS` | `true` | J+ trap-fade on wide-OR days (`>100` pts) |
| `J_MIN_TRAP_EXCESS` | `15` | Minimum trap wick beyond OR |
| `J_MIN_RECLAIM_PTS` | `8` | Minimum reclaim inside OR |
| `J_MIN_VWAP_DIST` | `8` | Minimum distance from VWAP |
| `J_MIN_BODY_RATIO` | `0.4` | Minimum candle body ratio |
| `J_ADX_MIN` | `20` | ADX floor for J+ |
| `J_MINS_AFTER_OR` | `30` | Earliest J+ entry (~10:00 IST) |
| `J_MAX_TRADES_DAY` | `1` | Max J+ trades per session |
| `J_MAX_LOSSES_DAY` | `1` | Stop J+ after one loss |
| `J_SKIP_BOTH_TRAPPED` | `true` | Skip if both OR sides trapped |

Set `ENABLE_J_PLUS=false` for v3.8-only behavior. See [LOCKED_STRATEGY_v3.9.md](LOCKED_STRATEGY_v3.9.md).

## Smoke tests (before scheduling)

Run from the project root:

```powershell
cd C:\Projects\Spot-signal
```

### Test 1 — Config and Telegram

```powershell
py -m bot.main --once
```

Expected: enhanced startup Telegram message and `Single tick complete.` on the console.

### Test 2 — Test suite (optional)

```powershell
py -m pytest tests/ -v
```

### Test 3 — Telegram remote (manual)

PowerShell window #1:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_telegram_remote.ps1
```

You should receive: **Remote control online**. Send `/help` and `/status` in Telegram.

Running it a second time should log `SKIP already running` (duplicate guard).

### Test 4 — Market session (manual)

During market hours (09:15–15:30 IST), PowerShell window #2:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_market_session.ps1
```

Or send `/start` via Telegram while the remote listener is running.

Check the log:

```powershell
Get-Content data\live\market-session.log -Tail 20
```

Stop with Ctrl+C or `/stop` in Telegram.

### Test 5 — Upstox validation (optional)

```powershell
py scripts\check_today.py
```

## Install automatic scheduling (recommended)

Creates two **hidden background** Windows tasks:

| Task name | When | What |
|-----------|------|------|
| `NiftySpotSignalEngine` | Mon–Fri 09:10 | Market session until ~15:30 IST |
| `NiftySpotSignalEngineTelegram` | At user logon | Always-on Telegram remote (auto-restarts on failure) |

Open **PowerShell as Administrator** (installer comment recommends elevated shell), then:

```powershell
cd C:\Projects\Spot-signal
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
```

The installer will:

1. Register both tasks (hidden, no popup windows)
2. Verify task state and next run time
3. **Start the Telegram remote immediately** — check Telegram for “Remote control online”

### Verify timezone (critical)

1. Open **Task Scheduler** (`taskschd.msc`)
2. Select `NiftySpotSignalEngine` → **Triggers**
3. Confirm timezone: **(UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi**

Wrong timezone = signals at the wrong local time.

### Test tasks manually

```powershell
Start-ScheduledTask -TaskName NiftySpotSignalEngineTelegram
Start-ScheduledTask -TaskName NiftySpotSignalEngine
Get-Content data\live\market-session.log -Tail 20
```

### Uninstall

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall_windows_task.ps1
```

## Daily operation

### Session times (IST)

| Time | Event |
|------|-------|
| 09:10 | Auto-start (scheduled) |
| 09:15 | Monitoring begins |
| 09:30 | Signal window opens |
| 15:15 | Square-off |
| 15:30 | Bot stops |

### Telegram commands

| Command | Action |
|---------|--------|
| `/help` | List commands |
| `/status` | Rich dashboard: time, window state, OR, trades, position |
| `/start` | Start market session (09:15–15:30 IST only) |
| `/stop` | Stop running session |
| `/tick` | Force one poll (~20s, session must be running) |

### What you’ll see in Telegram

- **Bot started** — session info, max trades, waiting for OR
- **OR ready** — high/low, width quality (Narrow/Good/Wide), CE/PE trigger prices
- **BUY CE/PE** — breakout distance, risk/reward, ADX/VWAP/ATR, option ask/OI/spread
- **SL / Target** — spot P&L in pts and %, option reference levels
- **Square off** — P&L + “close option NOW” instruction

## Log and lock files

| File | Contents |
|------|----------|
| `data\live\market-session.log` | Market bot output |
| `data\live\market-session.lock` | PowerShell PID while session runs |
| `data\live\telegram-remote.out.log` | Telegram remote stdout + start/end lines |
| `data\live\telegram-remote.err.log` | Telegram remote errors |
| `data\live\telegram-remote.lock` | Prevents duplicate remote instances |
| `data\live\signals.csv` | Signal event log |
| `data\live\monitor_state.json` | Position / OR state |

Tail the market log:

```powershell
Get-Content data\live\market-session.log -Wait -Tail 30
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `'python' is not recognized` | Install Python with PATH + py launcher; or set `NIFTY_PYTHON` |
| `Config error: UPSTOX_ACCESS_TOKEN` | Fill `.env`; run from project root |
| `ZoneInfoNotFoundError: Asia/Kolkata` | `py -m pip install tzdata` |
| No Telegram messages | Check token and chat ID; message the bot first |
| `Remote control error` in Telegram | Only one remote instance — check `telegram-remote.lock` |
| SSL / certificate errors | Built-in fallback should handle this; check corporate proxy |
| Task runs but nothing happens | Check `data\live\*.log`; confirm you are logged in |
| Wrong signal times | Set Task Scheduler timezone to IST |
| Upstox errors in log | Token expired — refresh in Upstox portal and `.env` |
| Script blocked by policy | Use `-ExecutionPolicy Bypass` (installer already does) |
| `/status` says STOPPED but bot runs | Delete stale `data\live\market-session.lock` |
| Missed 09:10 start (PC was asleep) | Task uses `StartWhenAvailable` — starts when PC wakes if logged in |
| Unexpected “session ended” Telegram | Crash safety-net fired — check `market-session.log` |
| Second remote won’t start | Expected — duplicate guard; one listener per bot token |

## Windows caveats

1. **You must be logged in** — tasks use `LogonType Interactive`. Screen lock is OK; full logout is not.
2. **One Telegram listener only** — enforced by `telegram-remote.lock` and `MultipleInstances: IgnoreNew`.
3. **Everything runs hidden** — no PowerShell windows after install; check logs and Telegram instead.
4. **Lock file stores the PowerShell PID** — `/stop` uses `taskkill /T /F` to stop the wrapper and Python child.
5. **Upstox token expires** — update `.env` when API calls fail.
6. **Power settings** — use “Never sleep” on AC power, or rely on built-in sleep prevention during sessions.

## Quick reference

```powershell
cd C:\Projects\Spot-signal

py -m bot.main --once

powershell -ExecutionPolicy Bypass -File scripts\run_telegram_remote.ps1
powershell -ExecutionPolicy Bypass -File scripts\run_market_session.ps1

powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
powershell -ExecutionPolicy Bypass -File scripts\uninstall_windows_task.ps1

Get-ScheduledTask -TaskName NiftySpotSignalEngine*
Get-Content data\live\market-session.log -Tail 30
```

## See also

- [README.md](../README.md) — project overview
- [TRADING_GUIDE.md](TRADING_GUIDE.md) — strategy and execution notes
