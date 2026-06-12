# Non-Trade Day Research

**Updated:** 2026-06-12 (multi-confirmation sweep)  
**Sample:** 51 sessions, **32 non-trade days** (all OR width > 100 pts)

## Literature (what pros use on gap / wide-OR days)

| Source | Recommendation for 100+ pt / gap days |
|--------|--------------------------------------|
| NiftyTradingPro | **Not** standard ORB — use gap playbook or VWAP mean reversion midday |
| StockeZee / Sahi | ORB + VWAP + volume stack; **exit fast** if break fails back through VWAP |
| NiftyPulse / gap guides | Gap **fade** needs weak OR candle + VWAP loss; gap **go** needs bullish OR + continuation |
| ChartMath | Multi-confirm only — VWAP + RVOL + price action together |

## Multi-confirmation sweep (`scripts/sweep_non_trade_multi.py`)

Production v3.8 on all days: **25 trades, +895 pts, PF 3.03**

On **32 wide-OR non-trade days only:**

| ID | Setup | Confirms | Trades | Win% | P&L | PF | Verdict |
|----|-------|----------|--------|------|-----|-----|---------|
| **J** | **OR fake-break stack** | Wick OR → close inside → VWAP wrong side → bearish bar → ADX | **31** | 45% | **+398** | **1.49** | **Best fit** |
| G | Trend pullback | VWAP slope + HH/HL + EMA touch | 22 | 46% | +215 | 1.59 | Good PF, fewer trades |
| M | VWAP extension fade | 11–14 IST + 1.2 ATR from VWAP + RSI extreme | 23 | 44% | +26 | 1.11 | Marginal |
| K | Gap fade stack | Gap 40pt + OR color + VWAP + open + RSI | 0 | — | 0 | — | **No fire** — gap-up days usually have **green** OR |
| L | Gap go stack | Gap + OR bull + EMA + ADX + break | 0 | — | 0 | — | No fire in sample |
| N | ORB + EMA + volume | 5-layer continuation | 40 | 25% | -561 | 0.62 | Reject |
| F | Failed breakout (old) | 2–3 confirms | 6 | 33% | -62 | 0.67 | Reject |

**26/32** wide days have gap ≥ 40 pts, but gap-**fade** needs a **red** opening range on gap-up days — rare, so K/L stay at 0 trades.

## Recommended Phase 2 strategy: **J — OR Fake-Break Stack**

Fits internet + data better than gap fade or wide ORB:

1. **Wide OR** (> 100 pts) — same days v3.8 skips  
2. **Wick traps OR** (high above OR high, or low below OR low)  
3. **Close back inside** the range (failed breakout)  
4. **VWAP wrong side** (below VWAP for PE fade, above for CE)  
5. **Reversal candle** (bearish close for PE, bullish for CE)  
6. **ADX ≥ 18**

**Exits (research):** structure stop beyond trap wick + v3.8 close-only SL via shared engine.

**Combined estimate (separate days, 51 sessions):** v3.8 **+895** + J on non-trade **+398** ≈ **+1293 pts** (J fires **0** trades on v3.8 trade days — no overlap).

## Do not pursue

- Wide ORB / raising `max_or_range` (loses money in sweep)  
- Gap fade K without OR-bearish filter removed (literature says fade only on **contradictory** opens — too rare to automate blindly)  
- Single-indicator VWAP reclaim (D/O) without volume on these days  

## Commands

```bash
python3 scripts/sweep_non_trade_multi.py --days 60
python3 scripts/analyze_entry_funnel.py --days 60
```

Results: `data/research/non_trade_multi_2026-06-12.json`
