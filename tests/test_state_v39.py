"""DayState v3.9 fields persist through JSON round-trip."""

from __future__ import annotations

from bot.state import DayState, _day_from_dict, _day_to_dict


def test_day_state_v39_fields_round_trip() -> None:
    day = DayState(
        session_date="2026-03-02",
        or_high=150.0,
        or_low=40.0,
        or_defined=True,
        day_mode="J_PLUS",
        losses_today=1,
        touched_above_or=True,
        touched_below_or=False,
        trap_high=155.0,
        trap_low=None,
        first_trap_side="PE",
        active_strategy="j_plus",
    )
    restored = _day_from_dict(_day_to_dict(day))
    assert restored.day_mode == "J_PLUS"
    assert restored.first_trap_side == "PE"
    assert restored.losses_today == 1
    assert restored.touched_above_or is True
    assert restored.trap_high == 155.0
    assert restored.active_strategy == "j_plus"
