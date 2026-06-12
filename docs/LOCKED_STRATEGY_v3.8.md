# Locked Strategy v3.8

**Locked:** 2026-06-12  
**Status:** Superseded by **v3.9** for production routing on wide-OR days. v3.8 rules below are unchanged on **valid-OR days** (25–100 pts). See [LOCKED_STRATEGY_v3.9.md](LOCKED_STRATEGY_v3.9.md).

## Entries (unchanged from v3.7)

| Parameter | Value |
|-----------|-------|
| Opening range | 09:15–09:30 IST (15 min) |
| Signal window | 09:30–15:15 |
| Square-off | 15:15 |
| OR width | 25–100 pts (skip outside band) |
| VWAP filter | On (futures proxy in bot) |
| ADX min | 18 |
| Max trades/day | 2 (1 CE + 1 PE) |

## Exits (exit sweep winner — 51-session backtest)

| Parameter | Value |
|-----------|-------|
| `sl_mode` | `OR_RANGE` |
| `close_only_sl` | `true` |
| `sl_buffer_pts` | `10.0` |
| `rr_ratio` | `2.0` |
| `atr_target_mult` | `1.2` |
| `atr_sl_mult` | `1.4` (used for WIDER/ATR modes only) |
| `sl_delay_bars` | `0` |

**Stop:** OR low/high ± 10pt buffer.  
**SL trigger:** Bar **close** through stop (wicks ignored).  
**Target:** `entry ± ATR × 1.2 × 2.0` (decoupled from stop distance).

## Source of truth

| Artifact | Path |
|----------|------|
| Bot config | `bot/config.py` → `StrategyConfig` / `DEFAULT_CONFIG` |
| Bot logic | `bot/strategy.py` |
| Pine (locked) | `pine/nifty_spot_signal_engine_LOCKED_v3.8.pine` |
| Exit research | `scripts/sweep_exits.py`, `data/research/exit_sweep_2026-06-12.json` |
| Entry funnel | `scripts/analyze_entry_funnel.py` |

## Env overrides (optional)

```env
SL_MODE=OR_RANGE
CLOSE_ONLY_SL=true
SL_BUFFER_PTS=10.0
RR_RATIO=2.0
```

## Expected frequency

~0.5 trades/day on average (~35% of sessions fire). ~63% of sessions skipped due to OR width > 100.

See `docs/NON_TRADE_DAY_RESEARCH.md` for Phase 2 alternative (OR fake-break stack) on skipped days.

See `docs/NON_TRADE_DAY_RESEARCH.md` for alternative strategies on skipped days.
