"""Option microstructure telemetry — pure calculators (Phase 1, no execution)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ponytail: fixed thresholds aligned with premium_decay_diag chop band
DEFAULT_CHOP_ATR_BAND = 0.3
IV_CRUSH_DROP_PCT = 10.0
SPREAD_DRAG_WARN_PCT = 3.0

# Premium-space lifecycle constants (Phase 2 — locked +40%/-15% from macro sweep)
PREMIUM_TARGET_MULT = 1.40  # +40% of entry ask
PREMIUM_STOP_MULT = 0.85    # -15% of entry ask (85% remaining)
VELOCITY_MIN_MULT = 1.10    # +10% by bar 5
CHOP_STOP_BAR_THRESHOLD = 4  # fire on 5th trade bar (0-indexed: bars_in_trade >= 4)


@dataclass(frozen=True)
class PremiumBrackets:
    target: float
    stop: float
    velocity_min: float
    rr_label: str


@dataclass(frozen=True)
class VelocityChopResult:
    triggered: bool
    ltp: float | None
    entry_ask: float | None
    bars_in_trade: int


def spread_drag_pct(ask: float | None, bid: float | None) -> float | None:
    if ask is None or bid is None or ask <= 0:
        return None
    return round((ask - bid) / ask * 100.0, 2)


def entry_slippage_premium_pts(
    *,
    signal_bar_close: float,
    alert_spot: float,
    alert_ask: float,
    delta: float | None,
    side: str,
) -> float | None:
    """
    Premium pts drift from spot move signal→alert: δ × Δspot (ask at alert vs δ-implied at signal).
    """
    if delta is None or alert_ask <= 0:
        return None
    d_spot = alert_spot - signal_bar_close if side == "CE" else signal_bar_close - alert_spot
    return round(delta * d_spot, 2)


def audit_target_wick_close(
    *,
    event_type: str,
    target: float | None,
    bar_high: float,
    bar_low: float,
    bar_close: float,
) -> dict[str, Any]:
    """Target exit wick vs close — backtest fills on wick, manual options may not."""
    if target is None or event_type not in ("TARGET_CE", "TARGET_PE"):
        return {}
    if event_type == "TARGET_CE":
        wick_hit = bar_high >= target
        held_on_close = bar_close >= target
    else:
        wick_hit = bar_low <= target
        held_on_close = bar_close <= target
    return {
        "target_held_on_close": held_on_close,
        "target_reverted_mid_bar": bool(wick_hit and not held_on_close),
        "bar_high": bar_high,
        "bar_low": bar_low,
        "bar_close": bar_close,
    }


def iv_delta_from_entry(iv_at_entry: float | None, iv_current: float | None) -> float | None:
    if iv_at_entry is None or iv_current is None:
        return None
    return round(iv_current - iv_at_entry, 4)


def iv_crush_bleed(
    *,
    iv_at_entry: float | None,
    iv_current: float | None,
    spot_from_entry: float,
    atr: float | None,
    chop_band: float = DEFAULT_CHOP_ATR_BAND,
) -> bool:
    """Spot flat (±chop_band×ATR) while IV fell > IV_CRUSH_DROP_PCT relative to entry."""
    if iv_at_entry is None or iv_current is None or iv_at_entry <= 0:
        return False
    if atr is None or atr <= 0:
        flat = abs(spot_from_entry) <= 5.0
    else:
        flat = abs(spot_from_entry) <= chop_band * atr
    if not flat:
        return False
    drop_pct = (iv_at_entry - iv_current) / iv_at_entry * 100.0
    return drop_pct > IV_CRUSH_DROP_PCT


def build_entry_telemetry(
    *,
    signal_bar_close: float,
    alert_spot: float,
    ask: float | None,
    bid: float | None,
    delta: float | None,
    side: str,
    iv: float | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "signal_bar_close": round(signal_bar_close, 2),
        "alert_spot": round(alert_spot, 2),
    }
    if ask is not None:
        out["alert_ask"] = round(ask, 2)
    drag = spread_drag_pct(ask, bid)
    if drag is not None:
        out["spread_drag_pct"] = drag
        if drag > SPREAD_DRAG_WARN_PCT:
            out["high_spread_drag"] = True
    slip = entry_slippage_premium_pts(
        signal_bar_close=signal_bar_close,
        alert_spot=alert_spot,
        alert_ask=ask or 0.0,
        delta=delta,
        side=side,
    )
    if slip is not None:
        out["entry_slippage_pts"] = slip
    if iv is not None:
        out["iv_at_entry"] = round(iv, 4)
    return out


def compute_premium_brackets(entry_ask: float) -> PremiumBrackets:
    """Premium target, stop, velocity min, and R:R label from entry ask."""
    target = round(entry_ask * PREMIUM_TARGET_MULT, 2)
    stop = round(entry_ask * PREMIUM_STOP_MULT, 2)
    velocity_min = round(entry_ask * VELOCITY_MIN_MULT, 2)
    
    # R:R = reward / risk = (target - entry) / (entry - stop)
    reward = target - entry_ask
    risk = entry_ask - stop
    if risk > 0:
        rr_ratio = reward / risk
        rr_label = f"1:{rr_ratio:.2f}"
    else:
        rr_label = "1:∞"  # Edge case: stop >= entry
    
    return PremiumBrackets(
        target=target,
        stop=stop,
        velocity_min=velocity_min,
        rr_label=rr_label,
    )


def init_premium_lifecycle(position, entry_ask: float) -> None:
    """Set premium bracket fields on Position (see Step D in plan)."""
    brackets = compute_premium_brackets(entry_ask)
    position.premium_target_price = brackets.target
    position.premium_stop_price = brackets.stop
    position.premium_velocity_min = brackets.velocity_min
    position.chop_stop_triggered = False
    position.chop_stop_alerted = False


def evaluate_velocity_chop(position, ltp: float | None, bar_index: int) -> VelocityChopResult:
    """
    20-minute velocity chop stop evaluation.
    
    Trigger rule:
    - bars_in_trade >= 4 (four 5m bars elapsed after entry bar → open of the 5th trade bar)
    - ltp < entry_ask * 1.10
    - not position.chop_stop_alerted
    
    Returns: VelocityChopResult(triggered: bool, ltp, entry_ask, bars_in_trade)
    Does NOT call reset_position() or emit spot exits.
    """
    bars_in_trade = bar_index - position.entry_bar_index
    entry_ask = getattr(position, 'option_ask', None)
    
    # Not enough time elapsed
    if bars_in_trade < CHOP_STOP_BAR_THRESHOLD:
        return VelocityChopResult(
            triggered=False,
            ltp=ltp,
            entry_ask=entry_ask,
            bars_in_trade=bars_in_trade,
        )
    
    # Already alerted
    if getattr(position, 'chop_stop_alerted', False):
        return VelocityChopResult(
            triggered=False,
            ltp=ltp,
            entry_ask=entry_ask,
            bars_in_trade=bars_in_trade,
        )
    
    # Missing data
    if ltp is None or entry_ask is None or entry_ask <= 0:
        return VelocityChopResult(
            triggered=False,
            ltp=ltp,
            entry_ask=entry_ask,
            bars_in_trade=bars_in_trade,
        )
    
    # Check velocity condition: ltp < entry_ask * 1.10
    velocity_min = entry_ask * VELOCITY_MIN_MULT
    triggered = ltp < velocity_min
    
    if triggered:
        position.chop_stop_triggered = True
        position.chop_stop_alerted = True
    
    return VelocityChopResult(
        triggered=triggered,
        ltp=ltp,
        entry_ask=entry_ask,
        bars_in_trade=bars_in_trade,
    )
