"""NIFTY weekly expiry calendar for research (not used in production)."""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

# NSE moved NIFTY weekly expiry to Tuesday effective ~Sep 2024.
NIFTY_TUESDAY_EXPIRY_FROM = date(2024, 9, 3)
# Prior era: Thursday weekly expiry (not used for sessions after May 2025 in our cache).
NIFTY_THURSDAY_EXPIRY_UNTIL = date(2024, 8, 29)

CACHE_PATH = Path(__file__).resolve().parent / "data" / "nifty_weekly_expiries.json"

ExpiryClass = Literal["weekly_expiry", "day_before_expiry", "day_after_expiry", "normal"]


def _iso_week_key(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _target_expiry_weekday(session_date: date) -> int:
    """0=Mon .. 6=Sun. Tuesday=1 post-Sep-2024, Thursday=3 before."""
    if session_date >= NIFTY_TUESDAY_EXPIRY_FROM:
        return 1
    return 3


def infer_weekly_expiry_from_sessions(session_dates: list[str]) -> list[str]:
    """
    Infer historical NIFTY weekly expiry dates from trading sessions.

    Post-Sep-2024: expiry on Tuesday; if Tuesday is a holiday, NSE typically
    moves to the previous trading day in the same week (usually Monday).
    """
    by_week: dict[str, list[str]] = {}
    for sd in sorted(session_dates):
        by_week.setdefault(_iso_week_key(date.fromisoformat(sd)), []).append(sd)

    expiries: list[str] = []
    for week_key in sorted(by_week):
        days = by_week[week_key]
        if not days:
            continue
        ref = date.fromisoformat(days[0])
        target_dow = _target_expiry_weekday(ref)

        # Valid expiry days: Tuesday, or Monday if Tuesday is a holiday (post-2024).
        allowed = {target_dow, target_dow - 1}
        candidates = [sd for sd in days if date.fromisoformat(sd).weekday() in allowed]
        if not candidates:
            continue
        exact = [sd for sd in candidates if date.fromisoformat(sd).weekday() == target_dow]
        if exact:
            expiries.append(exact[0])
        else:
            # Holiday shift: Monday when Tuesday absent from sessions
            expiries.append(candidates[-1])

    return sorted(set(expiries))


def fetch_live_expiries_from_nse(from_date: str, to_date: str) -> list[str]:
    """Optional: current/future expiries from NSE instrument master snapshot."""
    try:
        from bot.option_lookup import _load_nifty_options_index

        start = date.fromisoformat(from_date) - timedelta(days=7)
        end = date.fromisoformat(to_date) + timedelta(days=30)
        return sorted({
            row["expiry_date"].isoformat()
            for row in _load_nifty_options_index()
            if start <= row["expiry_date"] <= end
        })
    except Exception:
        return []


def load_or_build_expiries(
    session_dates: list[str],
    from_date: str,
    to_date: str,
    *,
    refresh: bool = False,
) -> tuple[list[str], str]:
    """
    Build expiry calendar for backtest range.

    Primary: rule-based inference from session list (covers historical weeks).
    Secondary: merge with NSE live master for forward validation.
    """
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)

    if CACHE_PATH.exists() and not refresh:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("expiries"):
            inferred = cached["expiries"]
            source = cached.get("source", "cache")
            return inferred, source

    inferred = infer_weekly_expiry_from_sessions(session_dates)
    live = fetch_live_expiries_from_nse(from_date, to_date)
    merged = sorted(set(inferred) | set(live))

    source = (
        "rule_inferred_from_sessions (NIFTY Tuesday weekly post-2024-09-03) "
        "+ NSE.json.gz live supplement"
        if live
        else "rule_inferred_from_sessions (NIFTY Tuesday weekly post-2024-09-03)"
    )

    CACHE_PATH.write_text(
        json.dumps({
            "source": source,
            "from_date": from_date,
            "to_date": to_date,
            "rule": "Tuesday expiry from 2024-09-03; holiday weeks use prior session in week",
            "expiries": merged,
            "inferred_count": len(inferred),
            "live_nse_count": len(live),
        }, indent=2),
        encoding="utf-8",
    )
    return merged, source


def build_session_classification(
    session_dates: list[str],
    expiries: list[str],
) -> dict[str, dict]:
    """Classify each trading session relative to weekly expiry."""
    expiry_set = set(expiries)
    sorted_sessions = sorted(session_dates)
    out: dict[str, dict] = {}

    for i, sd in enumerate(sorted_sessions):
        d = date.fromisoformat(sd)
        is_tuesday = d.weekday() == 1
        if sd in expiry_set:
            cls: ExpiryClass = "weekly_expiry"
            exp_ref = sd
        else:
            cls = "normal"
            exp_ref = None
            if i + 1 < len(sorted_sessions):
                nxt = sorted_sessions[i + 1]
                if nxt in expiry_set:
                    cls = "day_before_expiry"
                    exp_ref = nxt
            if cls == "normal" and i > 0:
                prev = sorted_sessions[i - 1]
                if prev in expiry_set:
                    cls = "day_after_expiry"
                    exp_ref = prev

        out[sd] = {
            "expiry_class": cls,
            "weekly_expiry_date": exp_ref,
            "weekday": d.strftime("%A"),
            "is_tuesday_heuristic": is_tuesday,
            "is_actual_expiry": sd in expiry_set,
            "tuesday_mismatch": is_tuesday and sd not in expiry_set,
            "expiry_not_tuesday": sd in expiry_set and not is_tuesday,
        }
    return out


def tuesday_heuristic_class(session_date: str) -> bool:
    return date.fromisoformat(session_date).weekday() == 1
