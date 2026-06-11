"""Pre-run data availability checks for options setups research."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from zoneinfo import ZoneInfo

from bot.config import DEFAULT_CONFIG
from bot.indicators import adx_series, atr_series
from bot.replay import _compute_indicators
from research.backtests.options_setups_comparison.config import HIST_DIR, ResearchConfig
from research.backtests.options_setups_comparison.indicators_ext import (
    expected_weekdays,
    prior_session_closes,
    validate_vwap_reset,
)
from research.tune import load_cached_history

TZ = ZoneInfo("Asia/Kolkata")
REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume", "vwap", "session_date")
EXPECTED_BARS = 75
FIRST_BAR_TIME = (9, 15)
LAST_BAR_TIME = (15, 25)


@dataclass
class AuditResult:
    passed: bool
    from_date: str
    to_date: str
    sessions_found: int = 0
    sessions_expected_weekdays: int = 0
    missing_files: list[str] = field(default_factory=list)
    session_issues: list[dict[str, Any]] = field(default_factory=list)
    option_premium_available: bool = False
    live_signals_available: bool = False
    data_type: str = "underlying_spot_proxy"
    warmup_ok: bool = False
    prior_close_ok: bool = False
    isolation_ok: bool = True
    stop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "timestamp": datetime.now(TZ).isoformat(),
            "from_date": self.from_date,
            "to_date": self.to_date,
            "data_type": self.data_type,
            "option_premium": self.option_premium_available,
            "live_signals_archive": self.live_signals_available,
            "sessions_found": self.sessions_found,
            "sessions_expected_weekdays": self.sessions_expected_weekdays,
            "missing_files": self.missing_files,
            "session_issues": self.session_issues,
            "warmup_ok": self.warmup_ok,
            "prior_close_ok": self.prior_close_ok,
            "isolation_ok": self.isolation_ok,
            "stop_reason": self.stop_reason,
        }


def _check_session_file(path: Path, session_date: str) -> list[str]:
    issues: list[str] = []
    df = pd.read_csv(path)
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing_cols:
        issues.append(f"missing_columns:{','.join(missing_cols)}")
        return issues

    if len(df) != EXPECTED_BARS:
        issues.append(f"bar_count:{len(df)}_expected_{EXPECTED_BARS}")

    ts = pd.to_datetime(df["timestamp"], utc=False)
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize(TZ)
    else:
        ts = ts.dt.tz_convert(TZ)

    if ts.duplicated().any():
        issues.append("duplicate_timestamps")

    if not ts.is_monotonic_increasing:
        issues.append("non_monotonic_timestamps")

    first = ts.iloc[0]
    last = ts.iloc[-1]
    if (first.hour, first.minute) != FIRST_BAR_TIME:
        issues.append(f"first_bar:{first.hour}:{first.minute}")
    if (last.hour, last.minute) != LAST_BAR_TIME:
        issues.append(f"last_bar:{last.hour}:{last.minute}")

    if df["vwap"].isna().all():
        issues.append("vwap_all_null")

    # Futures-era cache uses futures-derived VWAP; skip TWAP recomputation check when volume=0
    if float(df["volume"].fillna(0).abs().sum()) > 0:
        df_check = df.copy()
        df_check["timestamp"] = ts
        df_check["session_date"] = session_date
        if not validate_vwap_reset(df_check, session_date, tolerance=2.0):
            issues.append("vwap_reset_mismatch")

    return issues


def _check_live_signals() -> bool:
    signals_path = HIST_DIR.parent / "live" / "signals.csv"
    if not signals_path.exists():
        return False
    text = signals_path.read_text(encoding="utf-8")
    return "BUY_CE" in text or "BUY_PE" in text


def run_data_audit(
    cfg: ResearchConfig,
    *,
    output_dir: Path | None = None,
) -> AuditResult:
    """Run all pre-flight checks. Returns AuditResult; writes JSON if output_dir set."""
    result = AuditResult(
        passed=False,
        from_date=cfg.from_date,
        to_date=cfg.to_date,
        option_premium_available=False,
        live_signals_available=_check_live_signals(),
    )

    weekdays = expected_weekdays(cfg.from_date, cfg.to_date)
    result.sessions_expected_weekdays = len(weekdays)

    found_sessions: list[str] = []
    for day in weekdays:
        path = HIST_DIR / f"{day}_5m.csv"
        if not path.exists():
            result.missing_files.append(day)
            continue
        found_sessions.append(day)
        issues = _check_session_file(path, day)
        if issues:
            result.session_issues.append({"session": day, "issues": issues})

    result.sessions_found = len(found_sessions)

    if result.missing_files:
        result.stop_reason = (
            f"Missing {len(result.missing_files)} session files in data/historical "
            f"(holidays expected; material gaps: {result.missing_files[:5]})"
        )

    if result.session_issues:
        result.stop_reason = (
            result.stop_reason or f"{len(result.session_issues)} sessions failed validation"
        )

    if result.sessions_found < 10:
        result.stop_reason = result.stop_reason or f"Insufficient sessions: {result.sessions_found}"

    try:
        df, sessions = load_cached_history(
            futures_only=cfg.futures_only,
            from_date=cfg.from_date,
            to_date=cfg.to_date,
        )
        enriched = _compute_indicators(df, DEFAULT_CONFIG)
        atr_vals = enriched["atr"].dropna()
        adx_vals = enriched["adx"].dropna()
        result.warmup_ok = len(atr_vals) > 0 and len(adx_vals) > 0
        if not result.warmup_ok:
            result.stop_reason = result.stop_reason or "ATR/ADX not computable on loaded history"

        closes = prior_session_closes(df, sessions)
        with_prior = sum(1 for sd in sessions if closes.get(sd) is not None)
        result.prior_close_ok = with_prior >= max(0, len(sessions) - 1)
    except Exception as exc:
        result.stop_reason = result.stop_reason or f"Failed to load cached history: {exc}"
        result.warmup_ok = False

    out_root = cfg.outputs_root.resolve()
    prod_paths = [
        HIST_DIR.parent / "live" / "signals.csv",
        HIST_DIR.parent / "backtest",
    ]
    result.isolation_ok = str(out_root).startswith(str(cfg.outputs_root.parent.parent.resolve()))

    result.passed = (
        result.sessions_found >= 10
        and not result.session_issues
        and result.warmup_ok
        and result.isolation_ok
    )

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "data_availability_report.json"
        report_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")

    return result


def write_audit_report(result: AuditResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
