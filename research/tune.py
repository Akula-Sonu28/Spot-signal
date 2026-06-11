"""Grid search and walk-forward validation for StrategyConfig variants."""

from __future__ import annotations

import itertools
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from bot.backtest import BacktestSummary, run_backtest
from bot.config import DEFAULT_CONFIG, StrategyConfig
from bot.replay import run_replay_fast

ROOT = Path(__file__).resolve().parent.parent
HIST_DIR = ROOT / "data" / "historical"
OUT_DIR = ROOT / "data" / "research"


@dataclass(frozen=True)
class TuneResult:
    label: str
    params: dict[str, Any]
    train: BacktestSummary
    test: BacktestSummary
    full: BacktestSummary
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "params": self.params,
            "score": round(self.score, 2),
            "full": {
                "trades": self.full.total_trades,
                "win_rate_pct": round(self.full.win_rate_pct, 1),
                "total_pnl_pts": round(self.full.total_pnl_pts, 2),
                "profit_factor": round(self.full.profit_factor, 2) if self.full.profit_factor else None,
                "max_drawdown_pts": round(self.full.max_drawdown_pts, 2),
            },
            "train": {
                "trades": self.train.total_trades,
                "total_pnl_pts": round(self.train.total_pnl_pts, 2),
                "profit_factor": round(self.train.profit_factor, 2) if self.train.profit_factor else None,
            },
            "test": {
                "trades": self.test.total_trades,
                "total_pnl_pts": round(self.test.total_pnl_pts, 2),
                "profit_factor": round(self.test.profit_factor, 2) if self.test.profit_factor else None,
            },
        }


def load_cached_history(
    *,
    futures_only: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
) -> tuple[pd.DataFrame, list[str]]:
    """Load concatenated sessions from data/historical cache."""
    paths = sorted(HIST_DIR.glob("*_5m.csv"))
    frames: list[pd.DataFrame] = []
    sessions: list[str] = []

    for path in paths:
        day = path.name.replace("_5m.csv", "")
        if from_date and day < from_date:
            continue
        if to_date and day > to_date:
            continue
        df = pd.read_csv(path)
        if futures_only:
            if "vwap_source" in df.columns:
                if not (df["vwap_source"] == "futures").all():
                    continue
            elif day < "2026-04-01":
                # Older cache rows pre-date available futures history on Upstox
                continue
        ts = pd.to_datetime(df["timestamp"], utc=False)
        if ts.dt.tz is None:
            from zoneinfo import ZoneInfo

            ts = ts.dt.tz_localize(ZoneInfo("Asia/Kolkata"))
        df = df.copy()
        df["timestamp"] = ts
        df["session_date"] = df["timestamp"].dt.strftime("%Y-%m-%d")
        frames.append(df)
        sessions.append(day)

    if not frames:
        raise RuntimeError("No cached history in data/historical — run scripts/backtest.py first")

    full = pd.concat(frames, ignore_index=True)
    full = full.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
    return full.reset_index(drop=True), sorted(set(sessions))


def _split_sessions(sessions: list[str], train_ratio: float = 0.7) -> tuple[list[str], list[str]]:
    n = len(sessions)
    cut = max(1, int(n * train_ratio))
    if cut >= n:
        cut = n - 1
    return sessions[:cut], sessions[cut:]


def _config_label(cfg: StrategyConfig) -> str:
    sl_txt = f"sl={cfg.atr_sl_mult}xATR" if cfg.use_stop_loss else "sl=OFF"
    parts = [
        f"adx>={cfg.adx_min}",
        sl_txt,
        f"tgt={cfg.atr_target_mult}xATR RR{cfg.rr_ratio}",
        f"or={cfg.min_or_range}-{cfg.max_or_range}",
        f"vwap={'Y' if cfg.use_vwap_filter else 'N'}",
        f"maxT={cfg.max_trades_per_day}",
        f"slmode={cfg.sl_mode}",
    ]
    if cfg.adx_length != 14 or cfg.atr_length != 14:
        parts.append(f"adxL={cfg.adx_length}/atrL={cfg.atr_length}")
    return " | ".join(parts)


def composite_score(summary: BacktestSummary, *, min_trades: int = 15) -> float:
    """Higher is better; penalizes drawdown and too-few trades."""
    if summary.total_trades < max(3, min_trades):
        return -1e9
    pf = summary.profit_factor or 0.0
    if pf < 1.0:
        return summary.total_pnl_pts - summary.max_drawdown_pts
    return summary.total_pnl_pts + 40.0 * (pf - 1.0) - 0.25 * summary.max_drawdown_pts


def evaluate_config(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    sessions: list[str],
    train_sessions: list[str],
    test_sessions: list[str],
) -> TuneResult:
    logger = run_replay_fast(df, cfg=cfg)
    full = run_backtest(logger, session_dates=sessions)
    train = run_backtest(logger, session_dates=train_sessions)
    test = run_backtest(logger, session_dates=test_sessions)
    min_train = max(5, len(train_sessions) // 20)
    min_test = max(3, len(test_sessions) // 10)
    score = 0.35 * composite_score(train, min_trades=min_train) + 0.65 * composite_score(
        test, min_trades=min_test
    )
    return TuneResult(
        label=_config_label(cfg),
        params={k: v for k, v in asdict(cfg).items() if v != asdict(DEFAULT_CONFIG).get(k)},
        train=train,
        test=test,
        full=full,
        score=score,
    )


def baseline_param_grid() -> Iterator[StrategyConfig]:
    """v3.7 default + one-at-a-time sweeps (fast sensitivity)."""
    yield DEFAULT_CONFIG

    sweeps: dict[str, list[Any]] = {
        "adx_min": [12.0, 13.0, 15.0, 18.0, 20.0],
        "use_stop_loss": [True, False],
        "atr_sl_mult": [1.0, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8],
        "atr_target_mult": [1.0, 1.2, 1.4, 1.6],
        "rr_ratio": [1.2, 1.5, 1.6, 1.8, 2.0, 2.2],
        "min_or_range": [20.0, 25.0, 30.0],
        "max_or_range": [80.0, 100.0, 115.0, 120.0],
        "max_trades_per_day": [1, 2],
        "use_vwap_filter": [True, False],
        "sl_mode": ["ATR", "OR_RANGE", "TIGHTER"],
    }
    for key, values in sweeps.items():
        for val in values:
            if getattr(DEFAULT_CONFIG, key) == val:
                continue
            yield replace(DEFAULT_CONFIG, **{key: val})


def focused_grid(anchor: StrategyConfig) -> Iterator[StrategyConfig]:
    """Cartesian grid around promising anchor (smaller than full factorial)."""
    axes = {
        "adx_min": sorted({anchor.adx_min, 13.0, 15.0, 18.0}),
        "use_stop_loss": [anchor.use_stop_loss],
        "atr_sl_mult": sorted({anchor.atr_sl_mult, 1.2, 1.3, 1.4, 1.5}),
        "atr_target_mult": sorted({anchor.atr_target_mult, 1.0, 1.2, 1.4}),
        "rr_ratio": sorted({anchor.rr_ratio, 1.5, 1.6, 1.8, 2.0}),
        "min_or_range": sorted({anchor.min_or_range, 20.0, 25.0, 30.0}),
        "use_vwap_filter": [anchor.use_vwap_filter],
        "max_trades_per_day": [1, 2] if anchor.max_trades_per_day == 2 else [anchor.max_trades_per_day],
    }
    keys = list(axes.keys())
    for combo in itertools.product(*(axes[k] for k in keys)):
        kw = dict(zip(keys, combo))
        yield replace(
            anchor,
            **kw,
            sl_mode=anchor.sl_mode,
            max_or_range=anchor.max_or_range,
        )


def run_tune(
    *,
    futures_only: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
    phase: str = "all",
    top_n: int = 15,
) -> list[TuneResult]:
    df, sessions = load_cached_history(
        futures_only=futures_only, from_date=from_date, to_date=to_date
    )
    train_s, test_s = _split_sessions(sessions)

    results: list[TuneResult] = []
    configs: list[StrategyConfig] = []

    if phase in ("sensitivity", "all"):
        configs.extend(baseline_param_grid())
    if phase in ("focused", "all") and results:
        best = max(results, key=lambda r: r.score)
        anchor = DEFAULT_CONFIG
        for k, v in best.params.items():
            if hasattr(anchor, k):
                anchor = replace(anchor, **{k: v})
        configs.extend(focused_grid(anchor))

    if phase == "focused" and not results:
        # First pass for focused-only mode
        sens = run_tune(
            futures_only=futures_only,
            from_date=from_date,
            to_date=to_date,
            phase="sensitivity",
            top_n=1,
        )
        anchor = DEFAULT_CONFIG
        for k, v in sens[0].params.items():
            anchor = replace(anchor, **{k: v})
        configs.extend(focused_grid(anchor))

    if phase == "all":
        # After sensitivity, add focused grid around best sensitivity result
        pass  # handled in two-pass below

    seen: set[str] = set()
    unique_configs: list[StrategyConfig] = []
    for cfg in configs:
        key = json.dumps(asdict(cfg), sort_keys=True)
        if key not in seen:
            seen.add(key)
            unique_configs.append(cfg)

    for i, cfg in enumerate(unique_configs, 1):
        if i % 50 == 0:
            print(f"  Evaluating {i}/{len(unique_configs)} ...", flush=True)
        results.append(evaluate_config(df, cfg, sessions, train_s, test_s))

    results.sort(key=lambda r: r.score, reverse=True)
    return results[:top_n] if top_n else results


def run_full_pipeline(
    *,
    futures_only: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
    top_n: int = 20,
) -> dict[str, Any]:
    """Sensitivity pass → focused grid → ranked results + baseline."""
    df, sessions = load_cached_history(
        futures_only=futures_only, from_date=from_date, to_date=to_date
    )
    train_s, test_s = _split_sessions(sessions)
    print(f"Data: {len(sessions)} sessions, {len(df)} bars")
    print(f"Walk-forward: train {train_s[0]}→{train_s[-1]} ({len(train_s)}d) | "
          f"test {test_s[0]}→{test_s[-1]} ({len(test_s)}d)")

    print("\nPhase 1 — parameter sensitivity (one-at-a-time vs v3.7 default)...")
    sens_configs = list(baseline_param_grid())
    sens_results: list[TuneResult] = []
    for cfg in sens_configs:
        sens_results.append(evaluate_config(df, cfg, sessions, train_s, test_s))
    baseline = next(r for r in sens_results if r.params == {})
    sens_results.sort(key=lambda r: r.score, reverse=True)

    best_sens = sens_results[0]
    anchor = DEFAULT_CONFIG
    for k, v in best_sens.params.items():
        anchor = replace(anchor, **{k: v})

    print(f"  Baseline score: {baseline.score:.1f} | "
          f"full P&L {baseline.full.total_pnl_pts:+.0f} | PF {baseline.full.profit_factor}")
    print(f"  Best 1D tweak:  {best_sens.label} → score {best_sens.score:.1f}")

    print("\nPhase 2 — focused multi-param grid around best sensitivity anchor...")
    focused_configs = list(focused_grid(anchor))
    seen = {json.dumps(asdict(c), sort_keys=True) for c in sens_configs}
    focused_results: list[TuneResult] = []
    for i, cfg in enumerate(focused_configs, 1):
        key = json.dumps(asdict(cfg), sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        if i % 100 == 0:
            print(f"  Evaluating focused {i}/{len(focused_configs)} ...", flush=True)
        focused_results.append(evaluate_config(df, cfg, sessions, train_s, test_s))

    all_results = sens_results + focused_results
    all_results.sort(key=lambda r: r.score, reverse=True)
    top = all_results[:top_n]

    # Sensitivity table: delta vs baseline per param family
    sensitivity_rows: list[dict[str, Any]] = []
    for r in sens_results:
        if r.params == {}:
            continue
        key = next(iter(r.params))
        sensitivity_rows.append(
            {
                "param": key,
                "value": r.params[key],
                "full_pnl": round(r.full.total_pnl_pts, 1),
                "delta_pnl": round(r.full.total_pnl_pts - baseline.full.total_pnl_pts, 1),
                "test_pnl": round(r.test.total_pnl_pts, 1),
                "pf": round(r.full.profit_factor, 2) if r.full.profit_factor else None,
                "trades": r.full.total_trades,
                "score": round(r.score, 1),
            }
        )
    sensitivity_rows.sort(key=lambda x: x["score"], reverse=True)

    report = {
        "sessions": len(sessions),
        "train_range": [train_s[0], train_s[-1]],
        "test_range": [test_s[0], test_s[-1]],
        "futures_only": futures_only,
        "baseline": baseline.to_dict(),
        "best_sensitivity": best_sens.to_dict(),
        "top_configs": [r.to_dict() for r in top],
        "sensitivity_highlights": sensitivity_rows[:25],
        "evaluated_configs": len(sens_results) + len(focused_results),
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = "futures" if futures_only else "full"
    out = OUT_DIR / f"tune_{tag}_{sessions[0]}_{sessions[-1]}.json"
    out.write_text(json.dumps(report, indent=2))
    report["_output_path"] = str(out)
    return report


def format_tune_report(report: dict[str, Any]) -> str:
    lines = [
        "=" * 72,
        "STRATEGY TUNE REPORT (research only — production code unchanged)",
        "=" * 72,
        f"Sessions: {report['sessions']} | Train: {report['train_range'][0]}→{report['train_range'][1]} | "
        f"Test: {report['test_range'][0]}→{report['test_range'][1]}",
        f"Configs evaluated: {report['evaluated_configs']}",
        "",
        "--- v3.7 Baseline (locked) ---",
    ]
    b = report["baseline"]["full"]
    lines.append(
        f"  Trades {b['trades']} | WR {b['win_rate_pct']}% | "
        f"P&L {b['total_pnl_pts']:+.1f} | PF {b['profit_factor']} | DD {b['max_drawdown_pts']:.1f}"
    )
    lines.append(f"  Walk-forward score: {report['baseline']['score']:.1f}")
    lines.append("")
    lines.append("--- Top configs (ranked by walk-forward score) ---")
    for i, r in enumerate(report["top_configs"][:12], 1):
        f, t = r["full"], r["test"]
        lines.append(
            f"  {i:2}. score={r['score']:.1f} | full {f['total_pnl_pts']:+.0f}pts "
            f"(PF {f['profit_factor']}) | OOS test {t['total_pnl_pts']:+.0f}pts | {r['label']}"
        )
    lines.append("")
    lines.append("--- Best one-parameter tweaks ---")
    for row in report["sensitivity_highlights"][:12]:
        lines.append(
            f"  {row['param']}={row['value']}: full {row['full_pnl']:+.0f} "
            f"(Δ{baseline_delta(row, report):+.0f}) | test {row['test_pnl']:+.0f} | score {row['score']:.1f}"
        )
    lines.append("=" * 72)
    lines.append("Caution: in-sample tuning overfits. Prefer configs that improve OOS test P&L.")
    return "\n".join(lines)


def baseline_delta(row: dict[str, Any], report: dict[str, Any]) -> float:
    return row.get("delta_pnl", 0.0)
