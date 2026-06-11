"""Research setup processors A–H."""

from __future__ import annotations

from typing import Callable

from research.backtests.options_setups_comparison.setups.baseline import process_baseline_bar
from research.backtests.options_setups_comparison.setups.compression import process_compression_bar
from research.backtests.options_setups_comparison.setups.failed_breakout import process_failed_breakout_bar
from research.backtests.options_setups_comparison.setups.gap_hold_fade import process_gap_bar
from research.backtests.options_setups_comparison.setups.orb_breakout import process_orb_breakout_bar
from research.backtests.options_setups_comparison.setups.orb_retest import process_orb_retest_bar
from research.backtests.options_setups_comparison.setups.trend_pullback import process_trend_pullback_bar
from research.backtests.options_setups_comparison.setups.vwap_reclaim import process_vwap_reclaim_bar

SETUP_REGISTRY: dict[str, tuple[str, Callable[..., None]]] = {
    "A": ("baseline", process_baseline_bar),
    "B": ("orb_breakout", process_orb_breakout_bar),
    "C": ("orb_retest", process_orb_retest_bar),
    "D": ("vwap_reclaim", process_vwap_reclaim_bar),
    "E": ("compression", process_compression_bar),
    "F": ("failed_breakout", process_failed_breakout_bar),
    "G": ("trend_pullback", process_trend_pullback_bar),
    "H": ("gap_hold_fade", process_gap_bar),
}
