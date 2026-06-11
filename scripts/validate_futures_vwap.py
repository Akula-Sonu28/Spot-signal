#!/usr/bin/env python3
"""Live validation: futures VWAP proxy vs raw futures VWAP on spot (today)."""

from __future__ import annotations

import argparse
import os
import sys

# Allow running as `python scripts/validate_futures_vwap.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.futures_vwap import fetch_merged_today, vwap_long_ok, vwap_short_ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate futures VWAP proxy for today")
    parser.add_argument("--token", default=os.environ.get("UPSTOX_TOKEN"), help="Optional Upstox bearer token")
    args = parser.parse_args()

    fut_key, bars = fetch_merged_today(token=args.token)
    if not bars:
        print("No aligned bars returned")
        sys.exit(1)

    bases = [b.basis for b in bars]
    vols = [b.fut_volume for b in bars]
    agree = 0
    checked = 0
    raw_mismatch = 0

    for b in bars:
        if b.futures_vwap is None or b.spot_vwap_proxy is None:
            continue
        checked += 1
        fut_native_long = b.fut_close > b.futures_vwap
        adj_long = vwap_long_ok(b.spot_close, b.spot_vwap_proxy)
        raw_long = b.spot_close > b.futures_vwap
        if fut_native_long == adj_long:
            agree += 1
        if raw_long != fut_native_long:
            raw_mismatch += 1

    print(f"future_key={fut_key}")
    print(f"bars={len(bars)} futures_volume_total={sum(vols):.0f}")
    print(f"basis_pts: min={min(bases):.1f} max={max(bases):.1f} mean={sum(bases)/len(bases):.1f}")
    print(f"direction_agreement_adj_vs_futures_native: {agree}/{checked} ({100*agree/checked:.1f}%)")
    print(f"raw_spot_vs_futures_vwap_mismatches: {raw_mismatch}/{checked}")
    print()
    print("Last 5 bars: time | spot | fut_vwap | adj_vwap | basis | vol | long_ok")
    for b in bars[-5:]:
        t = b.timestamp.strftime("%H:%M")
        print(
            f"{t} | {b.spot_close:.1f} | {b.futures_vwap:.1f} | {b.spot_vwap_proxy:.1f} | "
            f"{b.basis:.1f} | {b.fut_volume:.0f} | {vwap_long_ok(b.spot_close, b.spot_vwap_proxy)}"
        )


if __name__ == "__main__":
    main()
