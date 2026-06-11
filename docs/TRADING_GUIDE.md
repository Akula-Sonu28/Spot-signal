# NIFTY Spot Signal Engine — Trading Guide

Sections F through M for the Pine Script v6 strategy in `pine/nifty_spot_signal_engine.pine`.

---

## F. Explanation of the Code

### Architecture

The script is a **strategy** (not indicator) running on the **NIFTY spot** chart, ideally **5-minute** timeframe. It simulates long/short positions on spot to backtest signal quality. In live trading, long = BUY CE and short = BUY PE.

### Key modules

| Module | Purpose |
|--------|---------|
| HTF filter | 15m EMA(21) slope + price position via non-repainting `request.security` |
| VWAP filter | Institutional bias — above = CE only, below = PE only |
| EMA alignment | 9 EMA vs 21 EMA trend on 5m (not crossover — avoids late entries) |
| Momentum gate | RSI + ADX filters chop and weak moves |
| Candle quality | Rejects oversized candles (> 2x ATR) |
| Session filter | Skips first 15 min, last 20 min, squares off at 15:10 IST |
| Risk controls | Max trades/day, cooldown, daily loss/profit lock |
| Exit engine | ATR or swing SL, 2.5R target, VWAP invalidation, EOD exit |

### Non-repainting guarantees

- `calc_on_every_tick = false`
- `process_orders_on_close = true`
- HTF data uses `[1]` offset + `barmerge.lookahead_on`
- All entries evaluated on confirmed bar close only

### Variable state

`activeSl`, `activeTarget`, `activeEntry` persist across bars while in a trade. Trail-to-breakeven updates `activeSl` after 1R favorable move.

---

## G. Alert Setup Guide

### Method 1: Order-fill webhooks (recommended)

1. Open TradingView → NIFTY spot chart → 5m timeframe.
2. Paste script from `pine/nifty_spot_signal_engine.pine` into Pine Editor → Add to chart.
3. Click **Alerts** → **Create Alert**.
4. **Condition:** `NIFTY Spot Signal Engine` → **Order fills only**.
5. **Message:** `{{strategy.order.alert_message}}`
6. **Options:** Once Per Bar Close, expiration as needed.
7. **Webhook URL:** Your bot endpoint (Telegram, TradersPost, AlgoWay, custom).

### Method 2: Signal-only alerts (no order simulation)

Use built-in alert conditions:
- **BUY CE Signal**
- **BUY PE Signal**
- **Square Off Warning**

### JSON payload fields

```json
{
  "signal": "BUY_CE",
  "trade_direction": "BULLISH",
  "underlying": "NIFTY",
  "suggested_option_side": "CE",
  "strike_instruction": "ATM_or_1ITM",
  "expiry_instruction": "current_weekly",
  "timeframe": "5",
  "spot_entry": 24500.0,
  "spot_sl": 24470.0,
  "spot_target": 24575.0,
  "premium_sl_note": "Use ~40-50% premium SL for live options",
  "risk_reward_ratio": "1:2.5",
  "timestamp": "2026-06-09T10:35:00",
  "reason": "HTF_bull+VWAP_above+EMA_align+RSI_momentum"
}
```

### Alert types emitted

| Signal | When |
|--------|------|
| `BUY_CE` | Bullish entry on bar close |
| `BUY_PE` | Bearish entry on bar close |
| `EXIT_CE` / `EXIT_PE` | Strategy exit order fill |
| `STOP_LOSS_HIT` | Position closed at SL |
| `TARGET_HIT` | Position closed at target |
| `SQUARE_OFF` | End-of-day forced exit |

---

## H. How to Use for NIFTY CE/PE Trading

### Workflow

1. **Chart:** NIFTY 50 Index (NSE) on 5-minute candles.
2. **Signal:** Wait for dashboard to show aligned bias + session ACTIVE.
3. **On BUY CE alert:**
   - Round NIFTY spot to nearest 50 strike.
   - Buy **ATM CE** or **1-step ITM CE** (lower strike).
   - Check premium > ₹30, bid-ask spread < 5 points.
4. **On BUY PE alert:**
   - Buy **ATM PE** or **1-step ITM PE** (higher strike).
5. **Stop-loss:** Use spot SL from alert as reference. For premium, approximate: `premium_sl ≈ entry_premium × 0.4–0.5`.
6. **Target:** Spot target from alert; expect ~1.5–2.0R on premium for 2.5R spot target.
7. **Square off:** Exit all option positions by 15:10 IST regardless of signal.

### Strike selection quick reference

| NIFTY Spot | ATM CE | 1-ITM CE | ATM PE | 1-ITM PE |
|------------|--------|----------|--------|----------|
| 24,520 | 24500 CE | 24450 CE | 24500 PE | 24550 PE |

---

## I. Backtesting Checklist

Before trusting Strategy Tester results:

- [ ] Chart is NIFTY **spot/index**, not futures or options
- [ ] Timeframe is **5-minute**
- [ ] Date range includes trending AND choppy days
- [ ] Commission set to ₹40/order (default in script)
- [ ] Slippage set to 2 points (default)
- [ ] Max trades/day respected (default 4)
- [ ] Session filter active (no trades 09:15–09:30, after 15:10)
- [ ] Compare win rate, avg win, avg loss, profit factor
- [ ] Check drawdown periods — are they acceptable?
- [ ] Verify signals visually on chart (no obvious repainting)
- [ ] Run on at least 3 months of data before any live capital
- [ ] **Remember:** Backtest P&L is spot-based, NOT option premium P&L

---

## J. Option Premium Validation Checklist

For each signal type, manually verify on option charts:

- [ ] Open ATM/1-ITM option chart for the signal day
- [ ] Confirm option moved in signal direction within 15–30 min
- [ ] Measure actual premium gain vs spot gain (delta check)
- [ ] Note theta decay during sideways periods after entry
- [ ] Check if SL on spot would have meant SL on premium
- [ ] Record bid-ask spread at entry time
- [ ] On expiry days (Thursday): compare behavior vs non-expiry
- [ ] Track effective RR on premium (expect 1:1.5–2.0 for 1:2.5 spot)
- [ ] Reject signals where premium < ₹30 or spread > 5 points
- [ ] Log 20+ paper trades before going live

---

## K. Optimization Checklist

Avoid overfitting:

- [ ] Change only **one parameter group** at a time
- [ ] Never optimize on < 60 trading days
- [ ] Use walk-forward: optimize on 60 days, test on next 30 days
- [ ] Prefer robust ranges over exact peaks (e.g., ADX 18–22, not exactly 19)
- [ ] Do not optimize RR below 2.0 or above 3.0 without strong justification
- [ ] Keep max trades/day at 3–5 (more = overtrading)
- [ ] If win rate < 40% with 1:2.5 RR, fix logic — don't tighten filters blindly
- [ ] Compare in-sample vs out-of-sample profit factor (should be within 30%)
- [ ] Document every parameter change with date and rationale

### Safe parameters to tune first

1. `ATR Stop-Loss Multiplier` (1.2 – 2.0)
2. `RSI thresholds` (52–58 bull, 42–48 bear)
3. `Cooldown bars` (2 – 5)
4. `Skip open/close minutes`

### Parameters to avoid over-tuning

- EMA lengths (9/21 is standard)
- HTF timeframe (15m is appropriate for 5m chart)
- ADX threshold below 18 (invites chop)

---

## L. Known Limitations

| Limitation | Impact | Workaround |
|------------|--------|------------|
| No option premium backtest | Strategy Tester shows spot P&L only | Validate on option charts manually |
| No strike auto-selection | Alerts include instruction only | Webhook bot or manual strike lookup |
| No IV/greeks data | Cannot filter by IV rank or delta | External screener (Sensibull, Opstra) |
| No bid-ask spread check | May signal into illiquid strikes | Manual spread check before entry |
| Expiry detection is day-of-week only | Doesn't know holiday-adjusted expiry | Manual override on expiry week |
| VWAP resets per chart session | Depends on TradingView session settings | Set chart timezone to IST |
| HTF data is 1 bar delayed | 15m confirmation lags up to 15 min | Acceptable for non-repainting |
| `strategy.exit` SL/TGT alerts share message | EXIT_CE/PE generic on order fill | Separate STOP_LOSS_HIT / TARGET_HIT alerts added |
| 9000+ trade trim in deep backtest | Old trades dropped from tester | Use reasonable date ranges |

---

## M. Practical Cautions Before Live Trading

1. **Paper trade minimum 20 sessions** before risking real capital.
2. **Risk per trade:** Never risk more than 1–2% of capital on option premium.
3. **Spot RR ≠ Premium RR:** A 50-point spot target may yield only 30–40 points on ATM option.
4. **Theta is your enemy:** Don't hold option buys through lunch consolidation (12:00–13:00).
5. **Expiry day (Thursday):** Gamma can help or destroy you. Default is **disabled** — enable only after validation.
6. **First 15 minutes:** Opening range is noisy. Script skips by default — do not override without testing.
7. **News days:** RBI policy, Budget, US CPI — disable trading or reduce size manually.
8. **Slippage:** ATM options can slip 2–5 points in fast markets. Factor into SL.
9. **Max daily loss:** Set `Daily Max Loss` to your hard stop (default 200 spot points reference).
10. **Profit lock:** When daily target hit, stop trading — greed kills intraday edge.
11. **This is not financial advice.** Past backtest performance does not guarantee future results.
12. **Regulatory:** Ensure your broker permits algo/webhook trading if automating.

---

## Quick Start

```text
1. Copy pine/nifty_spot_signal_engine.pine → TradingView Pine Editor
2. Apply to NIFTY 50 Index, 5m chart
3. Configure inputs (session times, max trades, RR)
4. Create alert with {{strategy.order.alert_message}}
5. Paper trade 20+ sessions
6. Validate on option premium charts
7. Go live with small size
```
