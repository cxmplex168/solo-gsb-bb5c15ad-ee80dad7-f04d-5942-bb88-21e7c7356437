"""Unit tests for the exact money math (largest remainder allocation)."""

from decimal import Decimal

import pytest

from app.allocation import largest_remainder, q2

D = Decimal


class TestQ2:
    def test_rounds_half_up(self):
        assert q2(D("1.005")) == D("1.01")
        assert q2(D("1.004")) == D("1.00")
        assert q2(D("2.675")) == D("2.68")

    def test_negative_rounds_half_up_away_from_zero(self):
        assert q2(D("-1.005")) == D("-1.01")


class TestLargestRemainder:
    def test_exact_shares_need_no_adjustment(self):
        shares = [D("14.04"), D("1.95")]
        assert largest_remainder(D("15.99"), shares) == [D("14.04"), D("1.95")]

    def test_residual_goes_to_largest_remainder(self):
        # 33.33/33.33/33.34 at 13%: naive rounding gives 4.33+4.33+4.33=12.99,
        # but the invoice-level total is 13.00 -> the largest remainder wins.
        shares = [D("4.3329"), D("4.3329"), D("4.3342")]
        result = largest_remainder(D("13.00"), shares)
        assert result == [D("4.33"), D("4.33"), D("4.34")]
        assert sum(result) == D("13.00")

    def test_ties_broken_by_stable_bucket_order(self):
        shares = [D("1.005"), D("1.005")]
        result = largest_remainder(D("2.01"), shares)
        assert result == [D("1.01"), D("1.00")]
        # deterministic across repeated calls
        assert largest_remainder(D("2.01"), shares) == result

    def test_zero_total(self):
        assert largest_remainder(D("0.00"), [D("0"), D("0")]) == [D("0.00"), D("0.00")]

    def test_empty_shares_with_zero_total(self):
        assert largest_remainder(D("0.00"), []) == []

    def test_empty_shares_with_nonzero_total_rejected(self):
        with pytest.raises(ValueError):
            largest_remainder(D("0.01"), [])

    def test_sum_always_matches_total(self):
        shares = [D("0.333"), D("0.333"), D("0.334")]
        result = largest_remainder(D("1.00"), shares)
        assert sum(result) == D("1.00")

    def test_inconsistent_shares_rejected(self):
        with pytest.raises(ValueError):
            largest_remainder(D("10.00"), [D("1.00"), D("1.00")])
