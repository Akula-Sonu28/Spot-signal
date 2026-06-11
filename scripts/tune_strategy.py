#!/usr/bin/env python3
"""Explore StrategyConfig variants — does NOT modify bot/strategy.py or live config."""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from research.tune import format_tune_report, run_full_pipeline


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Grid-search strategy parameters on cached historical data (research only)"
    )
    parser.add_argument("--from", dest="from_date", help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="End date YYYY-MM-DD")
    parser.add_argument(
        "--futures-only",
        action="store_true",
        help="Only sessions with futures VWAP (more reliable, shorter window)",
    )
    parser.add_argument("--top", type=int, default=20, help="Top N configs to keep")
    args = parser.parse_args()

    report = run_full_pipeline(
        futures_only=args.futures_only,
        from_date=args.from_date,
        to_date=args.to_date,
        top_n=args.top,
    )
    print()
    print(format_tune_report(report))
    print(f"\nJSON: {report['_output_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
