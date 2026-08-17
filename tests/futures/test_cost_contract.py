"""Numerical boundary tests for futures execution costs."""
from __future__ import annotations

import math

import pytest

from diepi.futures.cost import CostModel


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"commission_rate": -0.001}, "commission_rate"),
        ({"commission_rate": math.nan}, "commission_rate"),
        ({"slippage_points": -1.0}, "slippage_points"),
        ({"slippage_points": math.inf}, "slippage_points"),
    ],
)
def test_cost_configuration_rejects_negative_or_nonfinite_values(
    kwargs, message
):
    with pytest.raises(ValueError, match=message):
        CostModel(**kwargs)


def test_execution_price_rejects_nonpositive_slippage_adjusted_price():
    model = CostModel(slippage_points=2.0)

    with pytest.raises(ValueError, match="execution price must be positive"):
        model.execution_price(1.0, is_buy=False)


@pytest.mark.parametrize(
    ("multiplier", "lots", "error_type"),
    [(200.5, 1, TypeError), (200, 0, ValueError), (200, True, TypeError)],
)
def test_commission_requires_positive_integral_contract_units(
    multiplier, lots, error_type
):
    with pytest.raises(error_type):
        CostModel().commission(1_000.0, multiplier, lots)
