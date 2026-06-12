# NIFTY Spot Signal Engine

Rule-based intraday **NIFTY 50 spot** signal engine (**locked strategy v3.9** — v3.8 breakout + J+ trap-fade day router). Phase 1 provides **live monitoring + Telegram alerts only** — no broker orders.

## Phases

| Phase | Status | Description |
|-------|--------|-------------|
| 0 | Done | Mock CSV replay + pytest |
| 0.5 | Done | Upstox today vs TradingView validation |
| **1** | **Current** | Live Upstox feed → strategy → Telegram |
| 2 | Future | Optional auto-trade (disabled by default) |

## Phase 1 — Live alerts

### Architecture

```
Upstox REST (index 5m + futures VWAP)
        ↓
   data_feed.py  → warmup + indicators
        ↓
   scheduler.py  → bar-close only, no duplicates
        ↓
   combined.py   → v3.9 day router (v3.8 + J+)
        ↓
   alerts.py     → Telegram
   logger.py     → data/live/signals.csv
```

**REST polling is sufficient** for a 5-minute strategy. WebSocket is not required for Phase 1; add later only if you need sub-minute latency.

### Setup

**macOS / Linux**

```bash
python3 -m pip install -r requirements.txt
cp .env.example .env
# Edit .env with UPSTOX_ACCESS_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
```

**Windows** — follow the [Windows step-by-step guide](#windows-setup-step-by-step) below, or the longer [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md).

### Run

**macOS / Linux**

```bash
# Single tick (testing)
python3 -m bot.main --once

# Full session loop (09:15–15:30 IST)
python3 -m bot.main
```

**Windows** — manual session or Telegram `/start` (remote listener must be running):

```powershell
python -m bot.main --once
powershell -ExecutionPolicy Bypass -File scripts\run_market_session.ps1
```

### Safety

- `AUTO_TRADE` is **hardcoded False** — no order placement code
- Stale or incomplete data → warning Telegram, **no signal**
- Same candle timestamp → never processed twice (persisted state)
- Signals only after **5m close + 15s buffer**

### Strategy (v3.9)

- **Day router** (fixed after OR): `<25` skip | `25–100` v3.8 breakout | `>100` J+ trap-fade
- OR: 09:15–09:30 | Signals: 09:30–15:15 | Square-off: 15:15 | Monitor stop: 15:30
- v3.8: VWAP + ADX, max 2 trades/day, OR-boundary SL (close-only)
- J+: robust trap-fade, max 1 trade/day after ~10:00 (`ENABLE_J_PLUS=true` default)
- See [docs/LOCKED_STRATEGY_v3.9.md](docs/LOCKED_STRATEGY_v3.9.md)

### Files

| File | Role |
|------|------|
| `bot/main.py` | Entry point |
| `bot/config.py` | `.env` + strategy params |
| `bot/data_feed.py` | Upstox candles + futures VWAP |
| `bot/scheduler.py` | Market-hours loop |
| `bot/alerts.py` | Telegram messages |
| `bot/combined.py` | v3.9 session dispatcher |
| `bot/strategy.py` | v3.8 breakout entries |
| `bot/strategy_j.py` | J+ trap-fade entries |
| `bot/indicators.py` | ATR/ADX/VWAP (Phase 0) |
| `scripts/check_today.py` | One-shot historical validation |
| `scripts/run_market_session.ps1` | Windows session wrapper |
| `scripts/install_windows_task.ps1` | Windows Task Scheduler install |
| `docs/WINDOWS_SETUP.md` | Full Windows setup guide |

### Pine / TradingView

See `pine/nifty_spot_signal_engine_LOCKED_v3.9.pine`, `docs/LOCKED_STRATEGY_v3.9.md`, and `docs/TRADING_GUIDE.md`.

### Tests

```bash
python3 -m pytest tests/ -v
```

---

## Windows setup (step by step)

This section explains how to run the bot on **Windows 10 or 11** so it starts automatically on trading days and sends **Telegram alerts only** (no broker orders).

### What you are setting up

Two small background jobs use **Windows Task Scheduler**:

| Job name | When it runs | What it does |
|----------|----------------|--------------|
| `NiftySpotSignalEngineTelegram` | When you log in to Windows | Listens for Telegram commands (`/start`, `/stop`, `/status`) |
| `NiftySpotSignalEngine` | Monday–Friday at **09:10 IST** | Starts the bot for the full market session (~09:15–15:30 IST) |

Both run **hidden** (no PowerShell window on screen). The bot reads NIFTY data from **Upstox**, runs the **v3.9 strategy**, and messages you on **Telegram** when there is a signal.

```
You log in to Windows
    → Telegram remote starts (always on)
    → You can type /status any time

09:10 IST on a weekday
    → Market bot starts automatically
    → After opening range: OR_READY message (v3.8 / J+ / skip day)
    → Trade alerts if rules match
    → Bot stops around 15:30 IST
```

---

### Step 1 — Install Python

1. Download Python 3.9 or newer from [python.org](https://www.python.org/downloads/windows/).
2. Run the installer.
3. **Important:** tick **“Add python.exe to PATH”**.
4. Open **PowerShell** and check:

```powershell
py --version
```

You should see something like `Python 3.12.x`.

If `py` does not work, try `python --version`. You can also set a full path with the `NIFTY_PYTHON` environment variable (see [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md)).

---

### Step 2 — Get the project on your PC

1. Open PowerShell.
2. Go to where you keep projects (example):

```powershell
cd C:\Users\YOUR_NAME\Projects
git clone https://github.com/Akula-Sonu28/Spot-signal.git
cd Spot-signal
```

Use a folder path **without spaces** if you can (e.g. `C:\Projects\Spot-signal`).

To update later:

```powershell
cd C:\Projects\Spot-signal
git pull origin main
```

---

### Step 3 — Install Python packages

Still in the project folder:

```powershell
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
```

This installs everything the bot needs (including timezone support for IST).

---

### Step 4 — Create your `.env` file (secrets)

1. Copy the example file:

```powershell
copy .env.example .env
notepad .env
```

2. Fill in these three values (required):

| Variable | What it is |
|----------|------------|
| `UPSTOX_ACCESS_TOKEN` | From the Upstox developer portal (expires — refresh when signals stop) |
| `TELEGRAM_BOT_TOKEN` | From [@BotFather](https://t.me/BotFather) on Telegram |
| `TELEGRAM_CHAT_ID` | Your numeric chat id (message the bot, then check `getUpdates` on the Telegram API) |

3. Leave `AUTO_TRADE=false`. The bot **never** places orders in this version.

4. Optional v3.9 settings (defaults are fine to start):

```env
ENABLE_J_PLUS=true
```

- `true` = on wide opening-range days, use **J+ trap-fade** (max 1 trade).
- `false` = v3.8 breakout only (wide days = no trades).

Save and close Notepad.

---

### Step 5 — Test before scheduling (important)

Run one test tick. You should get a Telegram message **“LIVE (v3.9)”**:

```powershell
cd C:\Projects\Spot-signal
py -m bot.main --once
```

If this fails, fix `.env` or your internet connection **before** installing the scheduler.

**Optional:** replay a past day to Telegram (does not use the scheduler):

```powershell
py scripts\replay_telegram.py --date 2026-06-10 --date 2026-06-11 --source upstox
```

---

### Step 6 — Install the Windows scheduled tasks

1. Open **PowerShell as Administrator** (right-click → Run as administrator).
2. Run:

```powershell
cd C:\Projects\Spot-signal
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
```

3. The script will:
   - Register both tasks
   - Start the **Telegram remote** immediately
   - Print the next scheduled run time

4. In Telegram you should see something like **“Remote control online”**.

5. Send `/status` — you should get session info, OR (if market is open), and mode (`V38` / `J_PLUS` / etc.).

---

### Step 7 — Set Task Scheduler timezone to IST

If this is wrong, the bot will start at the wrong clock time.

1. Press **Win + R**, type `taskschd.msc`, press Enter.
2. Click **Task Scheduler Library**.
3. Click **`NiftySpotSignalEngine`**.
4. Open the **Triggers** tab → double-click the 09:10 trigger.
5. Set timezone to: **(UTC+05:30) Chennai, Kolkata, Mumbai, New Delhi**.
6. Click OK.

---

### Step 8 — Verify the tasks

In normal PowerShell:

```powershell
Get-ScheduledTask -TaskName NiftySpotSignalEngine, NiftySpotSignalEngineTelegram |
  Format-Table TaskName, State
```

Start them manually once to confirm:

```powershell
Start-ScheduledTask -TaskName NiftySpotSignalEngineTelegram
Start-ScheduledTask -TaskName NiftySpotSignalEngine
Get-Content C:\Projects\Spot-signal\data\live\market-session.log -Tail 20
```

(Change the path if your project folder is different.)

---

### Step 9 — What happens each trading day (IST)

| Time | What you see |
|------|----------------|
| **09:10** | Bot starts automatically (weekdays) |
| **09:15** | Monitoring begins |
| **~09:30** | **OR_READY** Telegram — tells you today’s mode: v3.8 breakout, J+ trap-fade, or skip |
| **09:30–15:15** | **BUY CE/PE** alerts if strategy fires; **SL / Target** on exits |
| **15:15** | Square-off alert if still in a trade |
| **~15:30** | Bot stops for the day |

**Telegram commands** (remote must be running):

| Command | What it does |
|---------|----------------|
| `/status` | Bot running?, OR, day mode, trades today, open position |
| `/start` | Start session manually (only during market hours) |
| `/stop` | Stop the running session |
| `/tick` | Force one poll (~20 seconds) |
| `/help` | List commands |

You still **place option orders yourself** in your broker app. The bot only sends alerts.

---

### Step 10 — Where logs are saved

| File | What it is |
|------|------------|
| `data\live\market-session.log` | Main bot log |
| `data\live\signals.csv` | All signals (CSV) |
| `data\live\monitor_state.json` | OR width, day mode, trap flags, position (survives restart) |
| `data\live\telegram-remote.out.log` | Telegram remote listener log |

Watch the market log live:

```powershell
Get-Content data\live\market-session.log -Wait -Tail 30
```

---

### Step 11 — Manual run (without waiting for 09:10)

During market hours, with Telegram remote already running:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_market_session.ps1
```

Or send **`/start`** in Telegram.

Stop with **Ctrl+C** in that window, or **`/stop`** in Telegram.

---

### Step 12 — Remove the scheduled tasks

```powershell
powershell -ExecutionPolicy Bypass -File scripts\uninstall_windows_task.ps1
```

---

### Windows troubleshooting (simple)

| Problem | What to try |
|---------|-------------|
| No Telegram at 09:10 | Open Task Scheduler → `NiftySpotSignalEngine` → **History** / last run result; check `market-session.log` |
| `/start` does nothing | Is `NiftySpotSignalEngineTelegram` running? Check `telegram-remote.err.log` |
| Wrong start time | Step 7 — timezone must be IST |
| `py` not found | Install Python with PATH, or set `NIFTY_PYTHON` |
| No signals but bot runs | Normal on skip days (narrow OR) or when filters block; check `/status` for mode |
| Upstox errors | Refresh `UPSTOX_ACCESS_TOKEN` in `.env` |
| After `git pull` | Re-run `install_windows_task.ps1` if scripts moved |

More detail: [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md) · Strategy rules: [docs/LOCKED_STRATEGY_v3.9.md](docs/LOCKED_STRATEGY_v3.9.md) · Operator guide: [docs/TRADING_GUIDE.md](docs/TRADING_GUIDE.md)

---

## Important

Backtest and spot signals ≠ option premium P&L. Execute CE/PE manually after each alert. Paper-trade 20 sessions before risking capital.
