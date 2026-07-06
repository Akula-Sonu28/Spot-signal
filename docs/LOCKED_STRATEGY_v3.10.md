# Locked Strategy v3.10

**Locked:** 2026-07-06  
**Status:** Production default — supersedes [v3.9](LOCKED_STRATEGY_v3.9.md).

## What changed from v3.9

| Item | v3.9 | v3.10 |
|------|------|-------|
| New entry cutoff | 15:15 (square-off) | **13:30 bar close** |
| J+ `max_consecutive_bars_outside_or` | Unrestricted | **Unrestricted** (unchanged) |
| Exits / square-off | 15:15 | 15:15 (unchanged) |
| Day router (V38 / J+) | OR width 25–100 / >100 | Unchanged |
| `AUTO_TRADE` | false | false (Phase 1) |

### v3.10 afternoon entry gate

- **Config:** `NO_NEW_ENTRIES_AFTER=13:30` (default in `bot/config.py`)
- **Rule:** No new `BUY_CE` or `BUY_PE` when the confirmed 5m bar **close** time is strictly after **13:30 IST**.
- **Implementation:** `bot/strategy.py` → `new_entries_allowed()`; enforced in v3.8 and J+ entry paths.
- Open positions may still exit on SL, target, or 15:15 square-off.

Set `NO_NEW_ENTRIES_AFTER=` (empty) to restore v3.9-style entries through 15:15 for research only.

## Research backing

### 273-session parametric sweep (May 2025 – Jun 2026)

| Variant | Trades | WR | Net P&L (pts) | PF | Max DD |
|---------|--------|-----|---------------|-----|--------|
| Baseline v3.9 | 282 | 61.7% | +3,681 | 1.57 | 490.9 |
| **13:30 gate only** | 253 | 62.5% | +3,551 | 1.58 | 437.8 |
| 13:30 + J+ cap 2 (research) | 221 | 64.3% | +2,919 | 1.55 | 437.8 |

Afternoon bucket (13:30–15:15) showed collapsing edge: PF ~1.40, ~+5 pts/trade vs morning +2,570 pts aggregate.

Reproduce sweep: `python3 scripts/research_sweeps.py --from 2025-05-02 --to 2026-06-09`

### Walk-forward validation (IS / OOS)

| Horizon | Variant | Trades | WR | P&L (pts) | PF |
|---------|---------|--------|-----|-----------|-----|
| IS (May 2025 – Mar 2026) | Baseline | 242 | 61.2% | +2,536 | 1.44 |
| IS | 13:30 + Cap 2 | 198 | 62.6% | +1,886 | 1.38 |
| OOS (Apr – Jun 2026) | Baseline | 39 | 66.7% | +1,156 | 2.77 |
| **OOS** | **13:30 + Cap 2** | **21** | **81.0%** | **+1,040** | **5.22** |

**Decision:** Ship **13:30 time gate only**. J+ consecutive-bars-outside-OR cap **not** locked — OOS J+ sample (3 trades) failed the ≥15 significance gate.

Reproduce: `python3 scripts/walk_forward_validation.py`

## Unchanged from v3.9

- OR 09:15–09:30, monitor through 15:30
- v3.8 breakout on valid-OR days; J+ trap-fade on wide-OR days
- OR_RANGE stop, close-only SL, ATR targets
- Max 2 trades/day (v3.8 path); J+ robust filters

## Source of truth

| Artifact | Path |
|----------|------|
| Entry gate | `bot/strategy.py` → `new_entries_allowed` |
| Config | `bot/config.py` → `LOCKED_STRATEGY_VERSION=3.10`, `no_new_entries_after` |
| Combined dispatcher | `bot/combined.py` |
| v3.8 entries | `bot/strategy.py` → `_process_v38_entries` |
| J+ entries | `bot/strategy_j.py` → `process_bar_j_entries` |
| Research sweep | `scripts/research_sweeps.py` |
| Walk-forward | `scripts/walk_forward_validation.py` |
