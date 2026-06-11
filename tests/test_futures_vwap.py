"""Tests for futures-derived VWAP proxy."""

from __future__ import annotations

from bot.futures_vwap import (
    basis_adjusted_vwap_series,
    merge_index_and_futures,
    vwap_long_ok,
    vwap_short_ok,
)


def test_basis_adjusted_matches_futures_native_direction():
    """Adjusted spot VWAP must agree with futures close vs futures VWAP."""
    f_h = [100.0, 101.0, 102.0, 103.0]
    f_l = [99.0, 100.0, 101.0, 102.0]
    f_c = [100.0, 101.0, 102.0, 103.0]
    f_v = [1000.0, 1000.0, 1000.0, 1000.0]
    s_c = [90.0, 91.0, 92.0, 93.0]  # constant 10-pt basis
    sids = ["2026-06-09"] * 4

    fut_vwap, spot_proxy = basis_adjusted_vwap_series(f_h, f_l, f_c, f_v, s_c, sids)
    for i in range(4):
        if fut_vwap[i] is None or spot_proxy[i] is None:
            continue
        assert (s_c[i] > spot_proxy[i]) == (f_c[i] > fut_vwap[i])
        assert spot_proxy[i] == fut_vwap[i] - (f_c[i] - s_c[i])


def test_merge_aligns_by_timestamp():
    index = [
        ["2026-06-09T09:20:00+05:30", 100, 105, 99, 102, 0, 0],
        ["2026-06-09T09:25:00+05:30", 102, 106, 101, 104, 0, 0],
    ]
    futures = [
        ["2026-06-09T09:20:00+05:30", 110, 115, 109, 112, 5000, 0],
        ["2026-06-09T09:25:00+05:30", 112, 116, 111, 114, 6000, 0],
    ]
    merged = merge_index_and_futures(index, futures)
    assert len(merged) == 2
    assert merged[0].spot_close == 102
    assert merged[0].fut_close == 112
    assert merged[0].basis == 10
    assert merged[0].futures_vwap is not None
    assert merged[0].spot_vwap_proxy is not None


def test_vwap_filter_helpers():
    assert vwap_long_ok(101.0, 100.0)
    assert not vwap_long_ok(99.0, 100.0)
    assert vwap_short_ok(99.0, 100.0)
    assert not vwap_short_ok(101.0, 100.0)
    assert not vwap_long_ok(101.0, None)
