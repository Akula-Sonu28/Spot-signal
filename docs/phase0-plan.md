# Phase 0 — Mock / Replay Prototype

Validate Python port of Pine v3.7 **before** Upstox, WebSocket, Telegram, or auto-trading.

## Project structure

```
nifty-spot-signal-engine/
├── bot/
│   ├── __init__.py
│   ├── config.py          # frozen StrategyConfig
│   ├── indicators.py      # ATR, ADX, VWAP, OR helpers
│   ├── state.py           # DayState, Position, ReplayState
│   ├── strategy.py        # bar-close signal + exit logic
│   ├── logger.py          # SignalEvent log + CSV export
│   └── replay.py          # CSV loader + replay driver
├── data/mock/
│   ├── day_trend_up.csv
│   ├── day_chop.csv
│   ├── day_breakdown.csv
│   └── day_or_too_narrow.csv
├── tests/
│   ├── test_indicators.py
│   ├── test_strategy.py
│   └── test_replay.py
├── docs/phase0-plan.md
└── requirements.txt
```

## Mock CSV format

| Column | Type | Notes |
|--------|------|-------|
| `timestamp` | ISO-8601 | **Bar close time** in IST (`+05:30`) or naive IST |
| `open` | float | NIFTY spot |
| `high` | float | |
| `low` | float | |
| `close` | float | Entry/exit evaluated on **close** (bar confirmed) |
| `volume` | int | Required for session VWAP |

Example:

```csv
timestamp,open,high,low,close,volume
2025-01-07T09:20:00+05:30,24120,24150,24100,24130,80000
```

Session: 9:15–15:30 IST, 5-minute bars, 75 bars/day.

## Implementation plan

1. **indicators.py** — Wilder RMA for ATR/ADX (TradingView-compatible), session VWAP reset daily.
2. **state.py** — Per-day OR accumulation, trade counters, single position (`pyramiding=0`).
3. **strategy.py** — On each confirmed bar:
   - Update OR (first 3 × 5m candles).
   - Check SL/target intrabar (SL priority if both hit).
   - Square-off at 15:15 if still open.
   - Entry: breakout + VWAP + ADX + OR width + max trades + one signal per direction/day.
4. **replay.py** — Load CSV, compute indicators without look-ahead, emit event log.
5. **tests** — Unit tests for indicators + integration on mock days.

## Test cases

| Test | Asserts |
|------|---------|
| `test_wilder_rma_seed` | RMA seeds correctly at bar `length-1` |
| `test_session_vwap_resets` | VWAP resets on new `session_date` |
| `test_or_width_filter` | Width 25–100 enforced |
| `test_or_three_bars` | OR uses bars with start 9:15, 9:20, 9:25 |
| `test_buy_ce_on_breakout` | BUY_CE when close > OR high + filters |
| `test_buy_pe_on_breakdown` | BUY_PE when close < OR low + filters |
| `test_max_two_trades_per_day` | Third entry blocked |
| `test_square_off_at_1515` | Open position closed on square-off bar |
| `test_sl_before_target_same_bar` | SL wins if both levels touched |
| `test_replay_loads_csv` | End-to-end replay runs without error |
| `test_narrow_or_no_entry` | OR width < 25 → no trade |

Run: `pytest tests/ -v`

## Success criteria

- [ ] All pytest tests pass offline (no network).
- [ ] Replay produces deterministic event JSON for each mock day.
- [ ] No duplicate entry on same bar; max 2 trades/day enforced.
- [ ] Square-off fires at first bar with close time ≥ 15:15 IST.
- [ ] SL = entry ∓ 1.2×ATR; target = 1.8R from entry (ATR SL mode).
- [ ] Manual spot-check: trade list directionally matches Pine on same CSV export (Phase 0.5).

## Edge cases (can break parity or live trading)

| Risk | Mitigation |
|------|------------|
| **OR boundary** — Pine uses `time_close < 9:30`; we use **bar start** 9:15/9:20/9:25 for 3 candles | Documented; compare vs TV export |
| **ADX/ATR warmup** — first ~14 bars differ if history missing | Include prior day in CSV fixtures |
| **Same-bar SL + target** — ambiguous OHLC path | Conservative: SL first (document) |
| **VWAP with zero volume** | Skip bar in cum vol; may block entries |
| **Gap through stop** — fill at stop price, not gap price | Phase 0 uses stop level (optimistic vs real slippage) |
| **Options ≠ spot** — backtest is spot-only | Never infer option P&L from this engine |
| **Holiday / half session** | Not modeled; needs calendar in Phase 1 |
| **DST** — India has no DST | Use `Asia/Kolkata` only |
| **firedLongToday + maxTrades** — can get 1 CE + 1 PE same day | Matches Pine; not "2 in same direction" |

## Unsafe assumptions (called out)

1. **Spot signals ≠ option fills** — ATM premium, theta, and spread can invalidate edge.
2. **Mock ADX on synthetic data** may not match live NIFTY — golden tests need real TV export.
3. **No slippage/commission** in Phase 0 — Pine used ₹40/order + 2 pts; add in Phase 1 P&L.
4. **Intrabar fill at exact stop/limit** — live options gap through stops.
5. **Auto-trading from alerts without kill switch** — do not enable until Phase 2 with `AUTO_TRADE=False` default and manual soak.

## Phase 1+ (after Phase 0 passes)

1. **Phase 1** — Upstox historical/intraday REST candles → same replay; Telegram alert-only bot.
2. **Phase 1.5** — Export TV trade list + CSV; diff tool vs Python log.
3. **Phase 2** — WebSocket LTP, optional order placement behind feature flag, daily loss cap, single-instance lock.

Do **not** connect broker until Python replay matches Pine on ≥3 real session exports.
