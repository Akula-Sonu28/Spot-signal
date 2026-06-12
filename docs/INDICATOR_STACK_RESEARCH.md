# Indicator Stack Research — Non-Trade Days

**Date:** 2026-06-12  
**Command:** `python3 scripts/sweep_indicator_stack.py --days 60`  
**Universe:** 32 wide-OR days (v3.8 skips), 51 total sessions

## Indicators implemented

| Indicator | Used in setups |
|-----------|----------------|
| VWAP / AVWAP (session) | All viable setups |
| VPVR POC (session bins) | Setup 08 — 0 trades (too strict) |
| EMA 9/21/50 | 04, 07, 08, 10 |
| HMA 21 | 06, 07 |
| MACD 12/26/9 | 01, 06, 07, 10 |
| Bollinger Bands 20,2 | 02, 12 |
| Keltner Channels | 03 — 0 trades |
| RSI 14 | 01, 03, 07, 09, 12 |
| Stochastic 14,3 | 04, 09, 12 |
| ATR 14 | Keltner + exits |
| CMF 20 | 02, 03, 08, 11 |
| Parabolic SAR | 05, 10 — weak / 0 trades |
| CCI 20 | 05, 09 — weak / 0 trades |
| OBV (session) | 06, 11 — 0 trades |

## Results (ranked by P&L on non-trade days)

| ID | Setup | Confirms | Trades | Win% | P&L | PF |
|----|-------|----------|--------|------|-----|-----|
| **07** | **hma_ema_macd_rsi** | HMA + EMA9/21 + MACD + RSI + VWAP | 49 | 47% | **+432** | **1.57** |
| J | or_fake_break | OR wick trap + VWAP + ADX | 31 | 45% | +398 | 1.49 |
| 01 | macd_vwap_rsi | MACD + RSI + VWAP | 52 | 44% | +373 | 1.37 |
| **04** | **stoch_ema_vwap** | Stoch cross + EMA9/21 + VWAP | 20 | **60%** | +280 | **2.00** |
| 05 | cci_sar_vwap | CCI + SAR + VWAP | 14 | 36% | -4 | 0.99 |
| 12 | bb_rsi_stoch_fade | BB + RSI + Stoch mean revert | 18 | 22% | -197 | 0.61 |
| 02–03, 06, 08–11 | Various 4–5 stacks | — | **0** | — | 0 | — |

**v3.8 production (trade days):** +895 pts, PF 3.03

## What actually works

1. **VWAP is mandatory** — every viable setup anchors to session VWAP (literature + data agree).
2. **Momentum stack beats oscillator pile** — HMA+EMA+MACD+RSI (07) beats RSI+Stoch+CCI triple (09, 0 trades).
3. **Tighter = better PF, looser = more P&L** — Stoch+EMA (04) PF 2.0 but 20 trades; HMA stack (07) PF 1.57 but 49 trades.
4. **Mean-reversion fades lose** — BB+RSI+Stoch fade (12) = -197 pts on volatile wide-OR days.
5. **Volume/POV stacks too strict** — CMF+OBV, POC+CMF never fired on 5m wide-OR sample.

## Recommended Phase 2 (non-trade module)

| Priority | Setup | Role |
|----------|-------|------|
| 1 | **07** HMA+EMA+MACD+RSI+VWAP | Primary — most P&L on skipped days |
| 2 | **04** Stoch+EMA+VWAP | Conservative — PF 2.0, fewer trades |
| 3 | **J** OR fake-break | Structural — complements momentum stacks |

**Do not deploy:** BB fade (12), wide ORB, SAR-only, triple-oscillator without trend filter.

## Combined estimate

| Module | Days | P&L (51 sessions) |
|--------|------|-------------------|
| v3.8 ORB | OR 25–100 | +895 |
| 07 wide-OR | OR > 100 | +432 |
| **Total** | mutually exclusive | **~+1327** |

*Spot points only; exits use research engine with v3.8 close-only SL.*

## Files

- `research/backtests/indicator_stack.py` — indicator math
- `research/backtests/indicator_setups.py` — 12 setup rules
- `scripts/sweep_indicator_stack.py` — runner
- `data/research/indicator_stack_sweep_2026-06-12.json` — full output
