"""Tests for Premium-Space Lifecycle Management (Phase 2)."""

import pytest
from unittest.mock import Mock, patch

from bot.option_lookup import days_to_expiry, effective_strike_mode, pick_strike
from bot.option_truth import compute_premium_brackets, init_premium_lifecycle, evaluate_velocity_chop
from bot.state import Position, PositionSide
from bot.alerts import _format_premium_brackets, TelegramAlerter
from bot.config import AppConfig


class TestDteItm2Strike:
    """Test DTE helper, ITM2 strike mode, and effective_strike_mode routing."""

    def test_days_to_expiry_calculation(self):
        """Test days_to_expiry calculation with different date scenarios."""
        # Monday session, Tuesday expiry
        assert days_to_expiry("2026-07-06", "2026-07-08") == 2  # Mon -> Tue
        assert days_to_expiry("2026-07-07", "2026-07-08") == 1  # Tue -> Tue (same day)
        assert days_to_expiry("2026-07-08", "2026-07-08") == 0  # Expiry day
        
        # Wednesday session, next Tuesday expiry  
        assert days_to_expiry("2026-07-09", "2026-07-15") == 6  # Wed -> next Tue

    def test_effective_strike_mode_dte_override(self):
        """Test DTE <= 1 triggers ITM2 override."""
        # DTE <= 1 should return ITM2 regardless of configured mode
        assert effective_strike_mode("2026-07-07", "2026-07-08", "ATM") == "ITM2"  # DTE = 1
        assert effective_strike_mode("2026-07-08", "2026-07-08", "ATM_OR_ITM1") == "ITM2"  # DTE = 0
        
        # DTE > 1 should use configured mode
        assert effective_strike_mode("2026-07-06", "2026-07-15", "ATM") == "ATM"  # DTE > 1
        assert effective_strike_mode("2026-07-06", "2026-07-15", "ATM_OR_ITM1") == "ATM"  # ATM_OR_ITM1 -> ATM
        assert effective_strike_mode("2026-07-06", "2026-07-15", "ITM1") == "ITM1"  # ITM1 stays ITM1

    def test_pick_strike_itm2_mode(self):
        """Test ITM2 strike selection: CE atm-100, PE atm+100."""
        spot = 24375.0
        atm = 24400  # rounded to nearest 50
        
        # ITM2: CE = ATM - 100, PE = ATM + 100
        assert pick_strike(spot, "CE", "ITM2") == atm - 100  # 24300
        assert pick_strike(spot, "PE", "ITM2") == atm + 100  # 24500

    def test_dte_itm2_integration(self):
        """Integration test: Monday/Tuesday session uses ITM2, Wednesday uses ATM."""
        spot = 24375.0
        atm = 24400
        
        # Mock session dates
        mon_session = "2026-07-07"  # Monday (DTE = 1 for Tuesday expiry)
        tue_expiry = "2026-07-08"
        
        # Monday should use ITM2 (DTE = 1)
        effective_mode = effective_strike_mode(mon_session, tue_expiry, "ATM_OR_ITM1")
        assert effective_mode == "ITM2"
        assert pick_strike(spot, "CE", effective_mode) == atm - 100
        
        # Wednesday should use ATM (DTE > 1)
        wed_session = "2026-07-09"
        next_tue_expiry = "2026-07-15"
        effective_mode = effective_strike_mode(wed_session, next_tue_expiry, "ATM_OR_ITM1")
        assert effective_mode == "ATM"
        assert pick_strike(spot, "CE", effective_mode) == atm


class TestPremiumBrackets:
    """Test premium bracket calculation and initialization."""

    def test_premium_brackets_math(self):
        """Test bracket calculation: ask=100 → target=140, stop=85, velocity_min=110."""
        ask = 100.0
        brackets = compute_premium_brackets(ask)

        assert brackets.target == 140.0  # 100 * 1.40
        assert brackets.stop == 85.0     # 100 * 0.85
        assert brackets.velocity_min == 110.0  # 100 * 1.10

        # R:R = (140-100) / (100-85) = 40/15 = 2.67
        expected_rr = "1:2.67"
        assert brackets.rr_label == expected_rr

    def test_premium_brackets_edge_cases(self):
        """Test bracket calculation with edge case values."""
        # Very small ask
        brackets = compute_premium_brackets(1.0)
        assert brackets.target == 1.40
        assert brackets.stop == 0.85
        assert brackets.velocity_min == 1.10
        
        # Large ask
        brackets = compute_premium_brackets(500.0)
        assert brackets.target == 700.0
        assert brackets.stop == 425.0
        assert brackets.velocity_min == 550.0

    def test_init_premium_lifecycle(self):
        """Test premium lifecycle initialization sets Position fields."""
        position = Position()
        entry_ask = 100.0
        
        init_premium_lifecycle(position, entry_ask)
        
        assert position.premium_target_price == 140.0
        assert position.premium_stop_price == 85.0
        assert position.premium_velocity_min == 110.0
        assert position.chop_stop_triggered is False
        assert position.chop_stop_alerted is False


class TestVelocityChopStop:
    """Test 20-minute velocity chop stop logic."""

    def _create_position_with_brackets(self, entry_ask=100.0, entry_bar_index=10):
        """Helper to create position with premium lifecycle fields."""
        position = Position()
        position.entry_bar_index = entry_bar_index
        position.option_ask = entry_ask
        position.side = PositionSide.CE
        init_premium_lifecycle(position, entry_ask)
        return position

    def test_chop_stop_fires_bar5_stagnant(self):
        """Test chop stop triggers on bar 5 when LTP < velocity_min."""
        position = self._create_position_with_brackets(entry_ask=100.0, entry_bar_index=10)
        
        # bars_in_trade = 14 - 10 = 4 (>= 4 threshold)
        # ltp = 105 < velocity_min (110) -> should trigger
        result = evaluate_velocity_chop(position, ltp=105.0, bar_index=14)
        
        assert result.triggered is True
        assert result.ltp == 105.0
        assert result.entry_ask == 100.0
        assert result.bars_in_trade == 4
        assert position.chop_stop_triggered is True
        assert position.chop_stop_alerted is True

    def test_chop_stop_suppressed_if_premium_up(self):
        """Test chop stop does not trigger when premium is above velocity_min."""
        position = self._create_position_with_brackets(entry_ask=100.0, entry_bar_index=10)
        
        # bars_in_trade = 4, but ltp = 115 > velocity_min (110) -> no trigger
        result = evaluate_velocity_chop(position, ltp=115.0, bar_index=14)
        
        assert result.triggered is False
        assert result.ltp == 115.0
        assert position.chop_stop_triggered is False
        assert position.chop_stop_alerted is False

    def test_chop_stop_not_enough_time_elapsed(self):
        """Test chop stop does not trigger before 4 bars elapsed."""
        position = self._create_position_with_brackets(entry_ask=100.0, entry_bar_index=10)
        
        # bars_in_trade = 13 - 10 = 3 (< 4 threshold)
        result = evaluate_velocity_chop(position, ltp=105.0, bar_index=13)
        
        assert result.triggered is False
        assert result.bars_in_trade == 3

    def test_chop_stop_fires_once(self):
        """Test chop stop alert fires only once."""
        position = self._create_position_with_brackets(entry_ask=100.0, entry_bar_index=10)
        
        # First evaluation - should trigger
        result1 = evaluate_velocity_chop(position, ltp=105.0, bar_index=14)
        assert result1.triggered is True
        assert position.chop_stop_alerted is True
        
        # Second evaluation - should not trigger again
        result2 = evaluate_velocity_chop(position, ltp=105.0, bar_index=15)
        assert result2.triggered is False  # chop_stop_alerted prevents re-trigger

    def test_chop_stop_missing_data(self):
        """Test chop stop handles missing data gracefully."""
        position = self._create_position_with_brackets(entry_ask=100.0, entry_bar_index=10)
        
        # Missing LTP
        result = evaluate_velocity_chop(position, ltp=None, bar_index=14)
        assert result.triggered is False
        
        # Missing entry_ask
        position.option_ask = None
        result = evaluate_velocity_chop(position, ltp=105.0, bar_index=14)
        assert result.triggered is False


class TestAlertFormatting:
    """Test alert formatting with premium brackets."""

    def test_format_premium_brackets(self):
        """Test format_premium_brackets for position status."""
        position = Position()
        position.premium_target_price = 140.0
        position.premium_stop_price = 85.0
        position.option_ask = 100.0
        position.option_ltp = 110.0

        lines = _format_premium_brackets(position)

        assert len(lines) == 2
        assert "Premium Target (+40%)" in lines[0]
        assert "₹140.00" in lines[0]
        assert "Current LTP: ₹110.00" in lines[0]
        assert "Distance: -30.0 pts" in lines[0]  # ltp (110) - target (140) = -30
        assert "Premium Stop (-15%)" in lines[1]
        assert "₹85.00" in lines[1]
        assert "1:2.67" in lines[1]  # R:R ratio

    def test_format_premium_brackets_positive_distance(self):
        """Test bracket formatting when LTP is above target."""
        position = Position()
        position.premium_target_price = 140.0
        position.premium_stop_price = 85.0
        position.option_ask = 100.0
        position.option_ltp = 145.0  # Above target

        lines = _format_premium_brackets(position)

        assert "Distance: +5.0 pts" in lines[0]  # ltp (145) - target (140) = +5

    def test_format_premium_brackets_missing_fields(self):
        """Test bracket formatting when premium fields are missing."""
        position = Position()
        # No premium lifecycle fields set
        
        lines = _format_premium_brackets(position)
        
        assert lines == []  # Should return empty list

    @patch('bot.alerts.has_telegram_alerts')
    def test_chop_stop_exit_alert_format(self, mock_has_telegram):
        """Test chop stop exit alert formatting."""
        mock_has_telegram.return_value = True
        
        cfg = Mock(spec=AppConfig)
        alerter = TelegramAlerter(cfg)
        
        position = Position()
        position.option_ltp = 95.5
        position.option_symbol = "NIFTY 24300 CE"
        position.side = PositionSide.CE
        
        # Mock the _send_html method to capture the message
        alerter._send_html = Mock()
        
        alerter.chop_stop_exit(position)
        
        # Verify the alert was sent with correct format
        alerter._send_html.assert_called_once()
        message = alerter._send_html.call_args[0][0]
        
        assert "[VELOCITY CHOP STOP]" in message
        assert "₹95.50" in message
        assert "20-minute velocity momentum window" in message
        assert "NIFTY 24300 CE" in message

    def test_alert_layout_samples_visualization(self):
        """Test alert layout samples match expected format."""
        # This test ensures our premium bracket formatting matches the plan spec
        position = Position()
        position.premium_target_price = 140.0
        position.premium_stop_price = 85.0
        position.option_ask = 100.0
        position.option_ltp = 110.0
        position.entry_price = 24400.0
        position.side = PositionSide.CE
        
        # Test position status formatting (used by /position command)
        from bot.alerts import TelegramAlerter
        
        formatted = TelegramAlerter.format_position_status(
            position, 
            spot=24450.0, 
            spot_pnl=50.0
        )
        
        # Should contain premium bracket information
        assert "Premium Target (+40%)" in formatted
        assert "₹140.00" in formatted
        assert "₹85.00" in formatted