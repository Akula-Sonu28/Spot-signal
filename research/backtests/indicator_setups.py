"""Multi-indicator setup rules for wide-OR non-trade days."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from bot.config import StrategyConfig
from bot.indicators import or_width
from bot.logger import ReplayLogger
from bot.state import PositionSide
from bot.strategy import BarContext

from research.backtests.indicator_stack import IndicatorStack
from research.backtests.options_setups_comparison.config import ResearchConfig
from research.backtests.options_setups_comparison.indicators_ext import volume_ratio
from research.backtests.options_setups_comparison.risk_engine import ResearchState, SkippedLogger
from research.backtests.options_setups_comparison.setups.common import (
    adx_ok,
    standard_preamble,
    try_enter,
)
from research.backtests.options_setups_comparison.slippage import SlippageModel

WIDE_OR_MIN = 100.0


def _wide_or(day) -> bool:
    w = or_width(day.or_high, day.or_low)
    return w is not None and w > WIDE_OR_MIN


def _v(ind: IndicatorStack, i: int, key: str):
    return getattr(ind, key)[i]


@dataclass(frozen=True)
class SetupSpec:
    setup_id: str
    name: str
    indicators: str
    check_ce: Callable[[int, BarContext, IndicatorStack, list[float]], bool]
    check_pe: Callable[[int, BarContext, IndicatorStack, list[float]], bool]


def _prev(ind: IndicatorStack, i: int, key: str):
    if i < 1:
        return None
    return getattr(ind, key)[i - 1]


# --- CE rules (each uses 3+ indicators) ---

def _ce_macd_vwap_rsi(i, bar, ind, vols) -> bool:
    m, s, h = _v(ind, i, "macd"), _v(ind, i, "macd_signal"), _v(ind, i, "macd_hist")
    hp = _prev(ind, i, "macd_hist")
    r, vw = _v(ind, i, "rsi"), _v(ind, i, "vwap")
    return all(x is not None for x in (m, s, h, hp, r, vw)) and m > s and h > hp and bar.close > vw and 50 <= r <= 72


def _pe_macd_vwap_rsi(i, bar, ind, vols) -> bool:
    m, s, h = _v(ind, i, "macd"), _v(ind, i, "macd_signal"), _v(ind, i, "macd_hist")
    hp = _prev(ind, i, "macd_hist")
    r, vw = _v(ind, i, "rsi"), _v(ind, i, "vwap")
    return all(x is not None for x in (m, s, h, hp, r, vw)) and m < s and h < hp and bar.close < vw and 28 <= r <= 50


def _ce_bb_squeeze_cmf(i, bar, ind, vols) -> bool:
    bw, bu, cm, vw = _v(ind, i, "bb_width"), _v(ind, i, "bb_upper"), _v(ind, i, "cmf"), _v(ind, i, "vwap")
    bwp = _prev(ind, i, "bb_width")
    vr = volume_ratio(vols, i)
    if None in (bw, bu, cm, vw, bwp) or vr is None:
        return False
    squeeze = bw < bwp and bw < 0.012
    return squeeze and bar.close > bu and cm > 0.05 and bar.close > vw and vr >= 1.2


def _pe_bb_squeeze_cmf(i, bar, ind, vols) -> bool:
    bw, bl, cm, vw = _v(ind, i, "bb_width"), _v(ind, i, "bb_lower"), _v(ind, i, "cmf"), _v(ind, i, "vwap")
    bwp = _prev(ind, i, "bb_width")
    vr = volume_ratio(vols, i)
    if None in (bw, bl, cm, vw, bwp) or vr is None:
        return False
    squeeze = bw < bwp and bw < 0.012
    return squeeze and bar.close < bl and cm < -0.05 and bar.close < vw and vr >= 1.2


def _ce_keltner_rsi_cmf(i, bar, ind, vols) -> bool:
    ku, r, cm, vw = _v(ind, i, "kc_upper"), _v(ind, i, "rsi"), _v(ind, i, "cmf"), _v(ind, i, "vwap")
    return ku is not None and r is not None and cm is not None and vw is not None and bar.close > ku and 52 <= r <= 75 and cm > 0 and bar.close > vw


def _pe_keltner_rsi_cmf(i, bar, ind, vols) -> bool:
    kl, r, cm, vw = _v(ind, i, "kc_lower"), _v(ind, i, "rsi"), _v(ind, i, "cmf"), _v(ind, i, "vwap")
    return kl is not None and r is not None and cm is not None and vw is not None and bar.close < kl and 25 <= r <= 48 and cm < 0 and bar.close < vw


def _ce_stoch_ema_vwap(i, bar, ind, vols) -> bool:
    sk, sd, skp, sdp = _v(ind, i, "stoch_k"), _v(ind, i, "stoch_d"), _prev(ind, i, "stoch_k"), _prev(ind, i, "stoch_d")
    e9, e21, vw = _v(ind, i, "ema9"), _v(ind, i, "ema21"), _v(ind, i, "vwap")
    if None in (sk, sd, skp, sdp, e9, e21, vw):
        return False
    cross_up = sk > sd and skp <= sdp and skp < 25
    return cross_up and e9 > e21 and bar.close > vw


def _pe_stoch_ema_vwap(i, bar, ind, vols) -> bool:
    sk, sd, skp, sdp = _v(ind, i, "stoch_k"), _v(ind, i, "stoch_d"), _prev(ind, i, "stoch_k"), _prev(ind, i, "stoch_d")
    e9, e21, vw = _v(ind, i, "ema9"), _v(ind, i, "ema21"), _v(ind, i, "vwap")
    if None in (sk, sd, skp, sdp, e9, e21, vw):
        return False
    cross_dn = sk < sd and skp >= sdp and skp > 75
    return cross_dn and e9 < e21 and bar.close < vw


def _ce_cci_sar_vwap(i, bar, ind, vols) -> bool:
    c, cp, sar, bull, vw = _v(ind, i, "cci"), _prev(ind, i, "cci"), _v(ind, i, "sar"), _v(ind, i, "sar_bull"), _v(ind, i, "vwap")
    if None in (c, cp, sar, vw) or bull is None:
        return False
    return c > -100 and cp <= -100 and bull and bar.close > sar and bar.close > vw


def _pe_cci_sar_vwap(i, bar, ind, vols) -> bool:
    c, cp, sar, bull, vw = _v(ind, i, "cci"), _prev(ind, i, "cci"), _v(ind, i, "sar"), _v(ind, i, "sar_bull"), _v(ind, i, "vwap")
    if None in (c, cp, sar, vw) or bull is None:
        return False
    return c < 100 and cp >= 100 and not bull and bar.close < sar and bar.close < vw


def _ce_obv_macd_hma(i, bar, ind, vols) -> bool:
    obv, obv1, obv2, h, hp, hm, vw = (
        _v(ind, i, "obv"), _prev(ind, i, "obv"), getattr(ind, "obv")[i - 2] if i >= 2 else None,
        _v(ind, i, "macd_hist"), _prev(ind, i, "macd_hist"), _v(ind, i, "hma21"), _v(ind, i, "vwap"),
    )
    if None in (obv, obv1, obv2, h, hp, hm, vw):
        return False
    return obv > obv1 > obv2 and h > hp and bar.close > hm and bar.close > vw


def _pe_obv_macd_hma(i, bar, ind, vols) -> bool:
    obv, obv1, obv2, h, hp, hm, vw = (
        _v(ind, i, "obv"), _prev(ind, i, "obv"), getattr(ind, "obv")[i - 2] if i >= 2 else None,
        _v(ind, i, "macd_hist"), _prev(ind, i, "macd_hist"), _v(ind, i, "hma21"), _v(ind, i, "vwap"),
    )
    if None in (obv, obv1, obv2, h, hp, hm, vw):
        return False
    return obv < obv1 < obv2 and h < hp and bar.close < hm and bar.close < vw


def _ce_hma_ema_macd_rsi(i, bar, ind, vols) -> bool:
    hm, e9, e21, m, s, r, vw = _v(ind, i, "hma21"), _v(ind, i, "ema9"), _v(ind, i, "ema21"), _v(ind, i, "macd"), _v(ind, i, "macd_signal"), _v(ind, i, "rsi"), _v(ind, i, "vwap")
    return all(x is not None for x in (hm, e9, e21, m, s, r, vw)) and bar.close > hm and e9 > e21 and m > s and 45 <= r <= 68 and bar.close > vw


def _pe_hma_ema_macd_rsi(i, bar, ind, vols) -> bool:
    hm, e9, e21, m, s, r, vw = _v(ind, i, "hma21"), _v(ind, i, "ema9"), _v(ind, i, "ema21"), _v(ind, i, "macd"), _v(ind, i, "macd_signal"), _v(ind, i, "rsi"), _v(ind, i, "vwap")
    return all(x is not None for x in (hm, e9, e21, m, s, r, vw)) and bar.close < hm and e9 < e21 and m < s and 32 <= r <= 55 and bar.close < vw


def _ce_poc_cmf_ema(i, bar, ind, vols) -> bool:
    poc, cm, e9, e21, r, vw = _v(ind, i, "poc"), _v(ind, i, "cmf"), _v(ind, i, "ema9"), _v(ind, i, "ema21"), _v(ind, i, "rsi"), _v(ind, i, "vwap")
    pocp = _prev(ind, i, "poc")
    if None in (poc, cm, e9, e21, r, vw) or pocp is None:
        return False
    return bar.close > poc and bar.close > pocp and cm > 0.08 and e9 > e21 and r > 50 and bar.close > vw


def _pe_poc_cmf_ema(i, bar, ind, vols) -> bool:
    poc, cm, e9, e21, r, vw = _v(ind, i, "poc"), _v(ind, i, "cmf"), _v(ind, i, "ema9"), _v(ind, i, "ema21"), _v(ind, i, "rsi"), _v(ind, i, "vwap")
    pocp = _prev(ind, i, "poc")
    if None in (poc, cm, e9, e21, r, vw) or pocp is None:
        return False
    return bar.close < poc and bar.close < pocp and cm < -0.08 and e9 < e21 and r < 50 and bar.close < vw


def _ce_triple_osc_vwap(i, bar, ind, vols) -> bool:
    r, rs, sk, cci, vw = _v(ind, i, "rsi"), _prev(ind, i, "rsi"), _v(ind, i, "stoch_k"), _v(ind, i, "cci"), _v(ind, i, "vwap")
    if None in (r, rs, sk, cci, vw):
        return False
    return r > 40 and rs <= 35 and sk < 30 and cci < -80 and bar.close > vw and bar.close > bar.open


def _pe_triple_osc_vwap(i, bar, ind, vols) -> bool:
    r, rs, sk, cci, vw = _v(ind, i, "rsi"), _prev(ind, i, "rsi"), _v(ind, i, "stoch_k"), _v(ind, i, "cci"), _v(ind, i, "vwap")
    if None in (r, rs, sk, cci, vw):
        return False
    return r < 60 and rs >= 65 and sk > 70 and cci > 80 and bar.close < vw and bar.close < bar.open


def _ce_sar_ema_stack(i, bar, ind, vols) -> bool:
    bull, e9, e21, e50, m, vw = _v(ind, i, "sar_bull"), _v(ind, i, "ema9"), _v(ind, i, "ema21"), _v(ind, i, "ema50"), _v(ind, i, "macd"), _v(ind, i, "vwap")
    bullp = _prev(ind, i, "sar_bull")
    if None in (e9, e21, e50, m, vw) or bull is None or bullp is None:
        return False
    return bull and not bullp and e9 > e21 > e50 and m > 0 and bar.close > vw


def _pe_sar_ema_stack(i, bar, ind, vols) -> bool:
    bull, e9, e21, e50, m, vw = _v(ind, i, "sar_bull"), _v(ind, i, "ema9"), _v(ind, i, "ema21"), _v(ind, i, "ema50"), _v(ind, i, "macd"), _v(ind, i, "vwap")
    bullp = _prev(ind, i, "sar_bull")
    if None in (e9, e21, e50, m, vw) or bull is None or bullp is None:
        return False
    return not bull and bullp and e9 < e21 < e50 and m < 0 and bar.close < vw


def _ce_cmf_obv_vwap(i, bar, ind, vols) -> bool:
    cm, obv, obv1, obv2, vw = _v(ind, i, "cmf"), _v(ind, i, "obv"), _prev(ind, i, "obv"), getattr(ind, "obv")[i - 2] if i >= 2 else None, _v(ind, i, "vwap")
    vwp = _prev(ind, i, "vwap")
    if None in (cm, obv, obv1, obv2, vw, vwp):
        return False
    reclaim = bar.close > vw and bar.close <= vwp
    return cm > 0.1 and obv > obv1 > obv2 and reclaim


def _pe_cmf_obv_vwap(i, bar, ind, vols) -> bool:
    cm, obv, obv1, obv2, vw = _v(ind, i, "cmf"), _v(ind, i, "obv"), _prev(ind, i, "obv"), getattr(ind, "obv")[i - 2] if i >= 2 else None, _v(ind, i, "vwap")
    vwp = _prev(ind, i, "vwap")
    if None in (cm, obv, obv1, obv2, vw, vwp):
        return False
    reject = bar.close < vw and bar.close >= vwp
    return cm < -0.1 and obv < obv1 < obv2 and reject


def _ce_bb_rsi_stoch_fade(i, bar, ind, vols) -> bool:
    """Mean-revert long: pierced lower BB, oscillators oversold, bounce at VWAP."""
    bl, r, sk, sdp, skp, sdp2, vw = _v(ind, i, "bb_lower"), _v(ind, i, "rsi"), _v(ind, i, "stoch_k"), _v(ind, i, "stoch_d"), _prev(ind, i, "stoch_k"), _prev(ind, i, "stoch_d"), _v(ind, i, "vwap")
    if None in (bl, r, sk, sdp, skp, sdp2, vw):
        return False
    touched = bar.low <= bl
    cross = sk > sdp and skp <= sdp2
    return touched and r < 38 and cross and bar.close >= vw * 0.998


def _pe_bb_rsi_stoch_fade(i, bar, ind, vols) -> bool:
    bu, r, sk, sdp, skp, sdp2, vw = _v(ind, i, "bb_upper"), _v(ind, i, "rsi"), _v(ind, i, "stoch_k"), _v(ind, i, "stoch_d"), _prev(ind, i, "stoch_k"), _prev(ind, i, "stoch_d"), _v(ind, i, "vwap")
    if None in (bu, r, sk, sdp, skp, sdp2, vw):
        return False
    touched = bar.high >= bu
    cross = sk < sdp and skp >= sdp2
    return touched and r > 62 and cross and bar.close <= vw * 1.002


INDICATOR_SETUPS: tuple[SetupSpec, ...] = (
    SetupSpec("01", "macd_vwap_rsi", "MACD+RSI+VWAP", _ce_macd_vwap_rsi, _pe_macd_vwap_rsi),
    SetupSpec("02", "bb_squeeze_cmf_vol", "BB+CMF+Vol+VWAP", _ce_bb_squeeze_cmf, _pe_bb_squeeze_cmf),
    SetupSpec("03", "keltner_rsi_cmf", "Keltner+RSI+CMF+VWAP", _ce_keltner_rsi_cmf, _pe_keltner_rsi_cmf),
    SetupSpec("04", "stoch_ema_vwap", "Stoch+EMA+VWAP", _ce_stoch_ema_vwap, _pe_stoch_ema_vwap),
    SetupSpec("05", "cci_sar_vwap", "CCI+SAR+VWAP", _ce_cci_sar_vwap, _pe_cci_sar_vwap),
    SetupSpec("06", "obv_macd_hma", "OBV+MACD+HMA+VWAP", _ce_obv_macd_hma, _pe_obv_macd_hma),
    SetupSpec("07", "hma_ema_macd_rsi", "HMA+EMA+MACD+RSI+VWAP", _ce_hma_ema_macd_rsi, _pe_hma_ema_macd_rsi),
    SetupSpec("08", "poc_cmf_ema", "VPVR POC+CMF+EMA+VWAP", _ce_poc_cmf_ema, _pe_poc_cmf_ema),
    SetupSpec("09", "triple_osc_vwap", "RSI+Stoch+CCI+VWAP", _ce_triple_osc_vwap, _pe_triple_osc_vwap),
    SetupSpec("10", "sar_ema_macd", "SAR+EMA stack+MACD+VWAP", _ce_sar_ema_stack, _pe_sar_ema_stack),
    SetupSpec("11", "cmf_obv_vwap", "CMF+OBV+VWAP reclaim", _ce_cmf_obv_vwap, _pe_cmf_obv_vwap),
    SetupSpec("12", "bb_rsi_stoch_fade", "BB+RSI+Stoch+VWAP fade", _ce_bb_rsi_stoch_fade, _pe_bb_rsi_stoch_fade),
)


def process_indicator_setup_bar(
    state: ResearchState,
    bar: BarContext,
    cfg: StrategyConfig,
    research: ResearchConfig,
    logger: ReplayLogger,
    slippage: SlippageModel,
    skipped: SkippedLogger,
    *,
    spec: SetupSpec,
    stack: IndicatorStack,
    volumes: list[float],
) -> None:
    if standard_preamble(state, bar, cfg, logger, slippage):
        return
    day = state.day
    if day is None or not day.or_defined or not bar.is_entry_window or not _wide_or(day):
        return
    if not adx_ok(bar, cfg):
        return

    i = bar.index
    if spec.check_ce(i, bar, stack, volumes):
        try_enter(
            state, bar, PositionSide.CE, bar.low - cfg.sl_buffer_pts,
            f"{spec.name}_CE", spec.setup_id, cfg, research, logger, slippage, skipped,
        )
    elif spec.check_pe(i, bar, stack, volumes):
        try_enter(
            state, bar, PositionSide.PE, bar.high + cfg.sl_buffer_pts,
            f"{spec.name}_PE", spec.setup_id, cfg, research, logger, slippage, skipped,
        )
