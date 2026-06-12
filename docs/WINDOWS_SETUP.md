# Windows Setup Guide

Complete setup for running the NIFTY Spot Signal Engine on Windows 10/11.

Phase 1 is **alerts only** — no broker orders (`AUTO_TRADE` is hardcoded `False`).

## What runs on Windows

| Component | Path | Role |
|-----------|------|------|
| Core bot | `python -m bot.main` | Live signal engine |
| Telegram remote | `python -m bot.telegram_remote` | `/start`, `/stop`, `/status`, `/tick` |
| Session wrapper | `scripts/run_market_session.ps1` | Starts bot, prevents sleep, writes lock/log |
| Remote wrapper | `scripts/run_telegram_remote.ps1` | Always-on Telegram listener |
| Task installer | `scripts/install_windows_task.ps1` | Schedules both jobs |
| Process control | `bot/platform_paths.py` | PowerShell launch, `taskkill` on stop |

**macOS-only (ignore on Windows):** `scripts/*.sh`, `install_launchd.sh`, `com.nifty-spot-signal-engine.*.plist`

## Architecture

```
Task Scheduler (logon)
    └── run_telegram_remote.ps1
            └── python -m bot.telegram_remote
                    └── /start launches run_market_session.ps1 via PowerShell

Task Scheduler (Mon–Fri 09:10 IST)
    └── run_market_session.ps1
            ├── SetThreadExecutionState (prevent sleep)
            └── python -m bot.main
                    ├── Upstox REST polling
                    ├── strategy.py (v3.7 rules)
                    └── Telegram alerts
```

## Prerequisites

### Python 3.9+

Download from [python.org](https://www.python.org/downloads/windows/). During install, check:

- **Add python.exe to PATH**
- **Install pip**

Verify in PowerShell:

```powershell
python --version
pip --version
```

If `python` is not found but `py` works, set a persistent override:

```powershell
[System.Environment]::SetEnvironmentVariable("NIFTY_PYTHON", "py", "User")
```

Or use the full path to `python.exe`. Restart PowerShell after changing `NIFTY_PYTHON`.

### Project folder

```powershell
cd C:\Users\YOU\Projects
git clone <your-repo-url> Spot-signal
cd Spot-signal
```

Prefer paths without spaces (e.g. `C:\Projects\Spot-signal`).

### Dependencies

```powershell
cd C:\Projects\Spot-signal
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
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

## Smoke tests (before scheduling)

Run from the project root:

```powershell
cd C:\Projects\Spot-signal
```

### Test 1 — Config and Telegram

```powershell
python -m bot.main --once
```

Expected: startup Telegram message and `Single tick complete.` on the console.

### Test 2 — Test suite (optional)

```powershell
python -m pytest tests/ -v
```

### Test 3 — Telegram remote (manual)

PowerShell window #1:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_telegram_remote.ps1
```

You should receive: **Remote control online**. Send `/help` and `/status` in Telegram.

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
python scripts\check_today.py
```

## Install automatic scheduling

Creates two Windows tasks:

| Task name | When | What |
|-----------|------|------|
| `NiftySpotSignalEngine` | Mon–Fri 09:10 IST | Market session until ~15:30 IST |
| `NiftySpotSignalEngineTelegram` | At user logon | Always-on Telegram remote (auto-restarts on failure) |

```powershell
cd C:\Projects\Spot-signal
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
```

Verify:

```powershell
Get-ScheduledTask -TaskName NiftySpotSignalEngine, NiftySpotSignalEngineTelegram |
    Format-Table TaskName, State
```

### Verify timezone (critical)

1. Open **Task Scheduler** (`taskschd.msc`)
2. Select `NiftySpotSignalEngine` → **Triggers**
3. Confirm timezone: **(UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi**

Wrong timezone = signals at the wrong local time.

### Test tasks immediately

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
| `/status` | Bot state, position, last candle |
| `/start` | Start market session (09:15–15:30 IST only) |
| `/stop` | Stop running session |
| `/tick` | Force one poll (~20s, session must be running) |

## Log files

| File | Contents |
|------|----------|
| `data\live\market-session.log` | Market bot output |
| `data\live\telegram-remote.out.log` | Telegram remote stdout |
| `data\live\telegram-remote.err.log` | Telegram remote errors |
| `data\live\signals.csv` | Signal event log |
| `data\live\monitor_state.json` | Position / OR state |

Tail the market log:

```powershell
Get-Content data\live\market-session.log -Wait -Tail 30
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `'python' is not recognized` | Reinstall Python with “Add to PATH”, or set `NIFTY_PYTHON` |
| `Config error: UPSTOX_ACCESS_TOKEN` | Fill `.env`; run from project root |
| `ZoneInfoNotFoundError: Asia/Kolkata` | `pip install tzdata` |
| No Telegram messages | Check token and chat ID; message the bot first |
| `Remote control error` in Telegram | Only one remote instance per bot token |
| Task runs but nothing happens | Check `data\live\*.log`; confirm you are logged in |
| Wrong signal times | Set Task Scheduler timezone to IST |
| Upstox errors in log | Token expired — refresh in Upstox portal and `.env` |
| Script blocked by policy | Use `-ExecutionPolicy Bypass` (installer already does) |
| `/status` says STOPPED but bot runs | Delete stale `data\live\market-session.lock` |
| Missed 09:10 start (PC was asleep) | Task uses `StartWhenAvailable` — starts when PC wakes if logged in |

## Windows caveats

1. **You must be logged in** — tasks use `LogonType Interactive`. Screen lock is OK; full logout is not.
2. **One Telegram listener only** — never run `run_telegram_remote.ps1` twice.
3. **Lock file stores the PowerShell PID** — `/stop` uses `taskkill /T /F` to stop the wrapper and Python child.
4. **Upstox token expires** — update `.env` when API calls fail.
5. **Power settings** — use “Never sleep” on AC power, or rely on built-in sleep prevention during sessions.

## Quick reference

```powershell
cd C:\Projects\Spot-signal

python -m bot.main --once

powershell -ExecutionPolicy Bypass -File scripts\run_telegram_remote.ps1
powershell -ExecutionPolicy Bypass -File scripts\run_market_session.ps1

powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
powershell -ExecutionPolicy Bypass -File scripts\uninstall_windows_task.ps1

Get-ScheduledTask -TaskName NiftySpotSignalEngine*
```

## See also

- [README.md](../README.md) — project overview
- [TRADING_GUIDE.md](TRADING_GUIDE.md) — strategy and execution notes
