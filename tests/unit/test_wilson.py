"""TDD — wilson_lower_bound: small-sample-safe ranking score (B50, T-FB.2)."""

from __future__ import annotations

import math

import pytest

from ragent.utility.wilson import wilson_lower_bound


def test_total_zero_returns_zero():
    assert wilson_lower_bound(0, 0) == 0.0


def test_positives_zero_with_dislikes_below_half():
    assert wilson_lower_bound(0, 10) < 0.5


def test_perfect_score_strictly_less_than_one():
    assert wilson_lower_bound(3, 3) < 1.0
    assert wilson_lower_bound(100, 100) < 1.0


def test_monotonic_in_total_at_perfect_score():
    """More all-positive samples increases confidence (lb rises)."""
    assert (
        wilson_lower_bound(3, 3)
        < wilson_lower_bound(10, 10)
        < wilson_lower_bound(100, 100)
        < wilson_lower_bound(1000, 1000)
    )


def test_monotonic_in_positives_fixed_total():
    """At fixed total, more positives strictly raises the lower bound."""
    assert (
        wilson_lower_bound(0, 10)
        < wilson_lower_bound(3, 10)
        < wilson_lower_bound(7, 10)
        < wilson_lower_bound(10, 10)
    )


def test_known_reference_values():
    """Hand-computed values at z=1.96 (4-decimal tolerance)."""
    assert math.isclose(wilson_lower_bound(3, 3), 0.4385, abs_tol=5e-4)
    assert math.isclose(wilson_lower_bound(100, 100), 0.9630, abs_tol=5e-4)
    assert math.isclose(wilson_lower_bound(5, 10), 0.2366, abs_tol=5e-4)


def test_always_in_unit_interval():
    for p in range(0, 11):
        for n in (1, 3, 10, 100, 1000):
            if p > n:
                continue
            lb = wilson_lower_bound(p, n)
            assert 0.0 <= lb <= 1.0


def test_negative_inputs_rejected():
    with pytest.raises(ValueError):
        wilson_lower_bound(-1, 10)
    with pytest.raises(ValueError):
        wilson_lower_bound(5, -1)
    with pytest.raises(ValueError):
        wilson_lower_bound(11, 10)  # positives > total


def test_custom_z_affects_result():
    """Higher z (tighter confidence) lowers the bound."""
    assert wilson_lower_bound(50, 100, z=2.58) < wilson_lower_bound(50, 100, z=1.96)
