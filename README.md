# NIFTY Spot Signal Engine

Rule-based intraday **NIFTY 50 spot** signal engine (Pine v3.7 logic). Phase 1 provides **live monitoring + Telegram alerts only** — no broker orders.

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
   strategy.py   → v3.7 rules (reuse Phase 0)
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

**Windows** — see [docs/WINDOWS_SETUP.md](docs/WINDOWS_SETUP.md) for production setup (hidden tasks, SSL fix, enhanced Telegram alerts).

```powershell
py -m pip install -r requirements.txt
copy .env.example .env
# Run PowerShell as Administrator:
powershell -ExecutionPolicy Bypass -File scripts\install_windows_task.ps1
```

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

### Strategy (v3.7)

- OR: 09:15–09:30 (Pine `isInOR` / bar-close semantics)
- Signals: 09:30–15:15 | Square-off: 15:15 | Stop: 15:30
- OR width 25–100 | VWAP (futures proxy) | ADX ≥ 18
- SL = OR low/high + 10pt buffer (close-only, ignore wicks) | Target = 1.2×ATR×2.0 | Max 2 trades/day

### Files

| File | Role |
|------|------|
| `bot/main.py` | Entry point |
| `bot/config.py` | `.env` + strategy params |
| `bot/data_feed.py` | Upstox candles + futures VWAP |
| `bot/scheduler.py` | Market-hours loop |
| `bot/alerts.py` | Telegram messages |
| `bot/strategy.py` | Signal logic (Phase 0) |
| `bot/indicators.py` | ATR/ADX/VWAP (Phase 0) |
| `scripts/check_today.py` | One-shot historical validation |
| `scripts/run_market_session.ps1` | Windows session wrapper |
| `scripts/install_windows_task.ps1` | Windows Task Scheduler install |
| `docs/WINDOWS_SETUP.md` | Full Windows setup guide |

### Pine / TradingView

See `pine/nifty_spot_signal_engine_LOCKED_v3.7.pine` and `docs/TRADING_GUIDE.md`.

### Tests

```bash
python3 -m pytest tests/ -v
```

## Important

Backtest and spot signals ≠ option premium P&L. Execute CE/PE manually after each alert. Paper-trade 20 sessions before risking capital.
