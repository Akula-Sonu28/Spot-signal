# Locked Strategy v3.9

**Locked:** 2026-06-12  
**Status:** Superseded by **v3.10** for production. See [LOCKED_STRATEGY_v3.10.md](LOCKED_STRATEGY_v3.10.md).

## Overview

v3.9 combines **v3.8 OR breakout** (valid-OR days) and **J+ trap-fade** (wide-OR days) in a single session router. One playbook per day — no overlap.

| OR width (pts) | Day mode | Active module | Max trades/day |
|----------------|----------|---------------|----------------|
| `< 25` | `SKIP` | None | 0 |
| `25–100` | `V38` | v3.8 breakout | 2 |
| `> 100` | `J_PLUS` | J+ robust trap-fade | 1 |

Boundary: width `== 100` → **V38**; width `> 100` → **J_PLUS**.

Set `ENABLE_J_PLUS=false` to revert to v3.8-only behavior (wide-OR days skip entries).

## v3.8 module (valid-OR days)

Unchanged from [LOCKED_STRATEGY_v3.8.md](LOCKED_STRATEGY_v3.8.md):

- OR 09:15–09:30 IST, signal window 09:30–15:15, square-off 15:15
- VWAP + ADX filters, max 2 trades/day (1 CE + 1 PE)
- Stop: OR low/high ± 10pt buffer (`OR_RANGE`)
- SL: bar **close** through stop only
- Target: decoupled ATR × 1.2 × 2.0

## J+ module (wide-OR days)

| Parameter | Default (env) |
|-----------|---------------|
| `J_MIN_TRAP_EXCESS` | 15 |
| `J_MIN_RECLAIM_PTS` | 8 |
| `J_MIN_VWAP_DIST` | 8 |
| `J_MIN_BODY_RATIO` | 0.4 |
| `J_ADX_MIN` | 20 |
| `J_MINS_AFTER_OR` | 30 |
| `J_MAX_TRADES_DAY` | 1 |
| `J_MAX_LOSSES_DAY` | 1 |
| `J_SKIP_BOTH_TRAPPED` | true |

- Entry: OR fake-break trap fade after both sides may trap (skip if both trapped when enabled)
- Stop: trap wick + buffer, capped at `ATR × 1.4`
- Target: `RR 2.0` on risk at entry
- Runtime exits: shared `_check_exit_on_bar` (close-only SL, wick target, EOD)

## Backtest (273 sessions, May 2025 – Jun 2026)

| Module | Trades | P&L (pts) | PF |
|--------|--------|-----------|-----|
| v3.8 only | 230 | +2,419 | 1.46 |
| J+ robust | 52 | +1,263 | 2.05 |
| **Combined v3.9** | **282** | **+3,681** | **1.57** |

273 sessions (2025-05-02 → 2026-06-09). Reproduce:

`python3 scripts/backtest_pine_compare.py --from 2025-05-02 --to 2026-06-12 --j-mode robust`

## Source of truth

| Artifact | Path |
|----------|------|
| Router | `bot/day_router.py` |
| Combined dispatcher | `bot/combined.py` → `process_session_bar` |
| v3.8 entries | `bot/strategy.py` |
| J+ entries | `bot/strategy_j.py` |
| Stops (J+) | `bot/stops.py` → `capped_stops` |
| Config | `bot/config.py` → `CombinedStrategyConfig`, `LOCKED_STRATEGY_VERSION=3.9` |
| Pine (locked) | `pine/nifty_spot_signal_engine_LOCKED_v3.9.pine` |
| Pine (combined ref) | `pine/nifty_combined_v38_jplus_v1.pine` |

## State persistence (live)

`DayState` fields for J+ survive restart in `data/live/monitor_state.json`:

- `day_mode`, `losses_today`, `touched_above_or`, `touched_below_or`, `trap_high`, `trap_low`, `active_strategy`
