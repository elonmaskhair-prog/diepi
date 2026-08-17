"""Synthetic contract tests for the pure fee engine."""

from dataclasses import FrozenInstanceError, replace

import pytest

from diepi.backtest.broker.fees import (
    CommissionScope,
    FeeBreakdown,
    FeeEngine,
    FeeSchedule,
    FeeSide,
    ParentOrderFeeState,
)


def _engine(**overrides) -> FeeEngine:
    defaults = {
        "commission_rate": 0.00025,
        "min_commission": 5.0,
        "stamp_duty_rate": 0.001,
        "transfer_fee_rate": 0.0,
    }
    defaults.update(overrides)
    return FeeEngine.from_rates(**defaults)


def _partitioned_commissions(engine, notionals, order_id="order-1", side="buy"):
    state = None
    results = []
    for notional in notionals:
        result = engine.calculate_fill(
            notional,
            parent_order_id=order_id,
            side=side,
            prior_state=state,
        )
        results.append(result)
        state = result.next_state
    return results


def test_default_schedule_uses_parent_order_scope():
    engine = FeeEngine()

    assert engine.commission_scope is CommissionScope.PARENT_ORDER
    assert engine.schedule.commission_rate == 0.00025
    assert engine.schedule.min_commission == 5.0


def test_schedule_accepts_explicit_enum_scope():
    schedule = FeeSchedule(commission_scope=CommissionScope.FILL)

    assert schedule.commission_scope is CommissionScope.FILL


def test_schedule_accepts_exact_string_scope():
    schedule = FeeSchedule(commission_scope="fill")

    assert schedule.commission_scope is CommissionScope.FILL


@pytest.mark.parametrize(
    "scope",
    ["order", "per_fill", "FILL", "parent-order", "", None, 1, True],
)
def test_invalid_commission_scope_is_rejected(scope):
    with pytest.raises(ValueError, match="commission_scope"):
        FeeSchedule(commission_scope=scope)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commission_rate", -0.0001),
        ("commission_rate", float("nan")),
        ("commission_rate", float("inf")),
        ("commission_rate", True),
        ("commission_rate", "0.001"),
        ("min_commission", -1.0),
        ("min_commission", float("nan")),
        ("min_commission", float("inf")),
        ("stamp_duty_rate", -0.001),
        ("stamp_duty_rate", float("nan")),
        ("stamp_duty_rate", float("-inf")),
        ("transfer_fee_rate", -0.00001),
        ("transfer_fee_rate", float("nan")),
        ("transfer_fee_rate", float("inf")),
    ],
)
def test_schedule_rejects_invalid_numeric_inputs(field, value):
    with pytest.raises(ValueError, match=field):
        FeeSchedule(**{field: value})


def test_nonnegative_stress_rate_above_one_is_explicitly_supported():
    result = _engine(
        commission_rate=1.5,
        min_commission=0.0,
        stamp_duty_rate=0.0,
    ).calculate_fill(10.0, parent_order_id="stress", side="buy")

    assert result.commission == 15.0


def test_first_small_fill_applies_minimum_commission_once():
    result = _engine().calculate_fill(
        10_000.0, parent_order_id="order-1", side="buy"
    )

    assert result.commission == 5.0
    assert result.total == 5.0
    assert result.cumulative_commission == 5.0


def test_parent_order_commission_is_cumulative_increment():
    results = _partitioned_commissions(_engine(), [10_000, 10_000, 10_000])

    assert [result.commission for result in results] == [5.0, 0.0, 2.5]
    assert sum(result.commission for result in results) == 7.5
    assert results[-1].cumulative_notional == 30_000.0
    assert results[-1].cumulative_commission == 7.5
    assert results[-1].fill_count == 3


@pytest.mark.parametrize(
    "parts",
    [
        [30_000],
        [10_000, 20_000],
        [20_000, 10_000],
        [10_000, 10_000, 10_000],
        [1_000] * 30,
        [1, 19_999, 10_000],
    ],
)
def test_parent_order_partition_does_not_change_total_minimum_commission(parts):
    results = _partitioned_commissions(_engine(), parts)

    assert sum(result.commission for result in results) == 7.5


def test_exact_minimum_threshold_does_not_charge_twice():
    results = _partitioned_commissions(_engine(), [10_000, 10_000])

    assert [result.commission for result in results] == [5.0, 0.0]
    assert results[-1].cumulative_commission == 5.0


def test_commission_above_minimum_adds_only_new_cumulative_due():
    results = _partitioned_commissions(_engine(), [19_999, 2, 999])

    assert [result.commission for result in results] == [5.0, 0.0, 0.25]
    assert results[-1].cumulative_commission == 5.25


def test_fill_scope_is_explicit_stress_mode():
    engine = _engine(commission_scope="fill")
    results = _partitioned_commissions(engine, [10_000, 10_000, 10_000])

    assert engine.commission_scope is CommissionScope.FILL
    assert [result.commission for result in results] == [5.0, 5.0, 5.0]
    assert results[-1].cumulative_commission == 15.0


def test_fill_scope_above_minimum_charges_each_fill_by_its_own_notional():
    results = _partitioned_commissions(
        _engine(commission_scope="fill"), [40_000, 40_000]
    )

    assert [result.commission for result in results] == [10.0, 10.0]


def test_stamp_duty_is_sell_only():
    engine = _engine(min_commission=0.0, stamp_duty_rate=0.001)
    buy = engine.calculate_fill(100_000, parent_order_id="buy", side="buy")
    sell = engine.calculate_fill(100_000, parent_order_id="sell", side="sell")

    assert buy.stamp_duty == 0.0
    assert sell.stamp_duty == 100.0


@pytest.mark.parametrize("side", [FeeSide.BUY, FeeSide.SELL])
def test_transfer_fee_is_charged_on_both_sides(side):
    result = _engine(
        commission_rate=0.0,
        min_commission=0.0,
        stamp_duty_rate=0.0,
        transfer_fee_rate=0.00002,
    ).calculate_fill(100_000, parent_order_id=side.value, side=side)

    assert result.transfer_fee == 2.0
    assert result.total == 2.0


def test_stamp_duty_exemption_is_component_specific():
    result = _engine(transfer_fee_rate=0.00002).calculate_fill(
        100_000,
        parent_order_id="fund-sell",
        side="sell",
        stamp_duty_exempt=True,
    )

    assert result.commission == 25.0
    assert result.stamp_duty == 0.0
    assert result.transfer_fee == 2.0
    assert result.total == 27.0


def test_transfer_fee_exemption_is_component_specific():
    result = _engine(transfer_fee_rate=0.00002).calculate_fill(
        100_000,
        parent_order_id="sell",
        side="sell",
        transfer_fee_exempt=True,
    )

    assert result.commission == 25.0
    assert result.stamp_duty == 100.0
    assert result.transfer_fee == 0.0
    assert result.total == 125.0


def test_effective_stamp_rate_can_be_resolved_per_fill():
    engine = _engine(stamp_duty_rate=0.001)
    result = engine.calculate_fill(
        100_000,
        parent_order_id="sell-after-rate-change",
        side="sell",
        stamp_duty_rate=0.0005,
    )

    assert result.stamp_duty == 50.0


def test_effective_transfer_rate_can_be_resolved_per_fill():
    engine = _engine(transfer_fee_rate=0.0)
    result = engine.calculate_fill(
        100_000,
        parent_order_id="sse-buy",
        side="buy",
        transfer_fee_rate=0.00001,
    )

    assert result.transfer_fee == 1.0


def test_independent_tax_rate_change_keeps_parent_commission_state_valid():
    engine = _engine(stamp_duty_rate=0.001)
    first = engine.calculate_fill(
        10_000, parent_order_id="A", side="sell"
    )
    second = engine.calculate_fill(
        20_000,
        parent_order_id="A",
        side="sell",
        prior_state=first.next_state,
        stamp_duty_rate=0.0005,
    )

    assert [first.commission, second.commission] == [5.0, 2.5]
    assert [first.stamp_duty, second.stamp_duty] == [10.0, 10.0]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stamp_duty_rate", -0.001),
        ("stamp_duty_rate", float("nan")),
        ("stamp_duty_rate", float("inf")),
        ("stamp_duty_rate", "0.001"),
        ("transfer_fee_rate", -0.001),
        ("transfer_fee_rate", float("nan")),
        ("transfer_fee_rate", float("inf")),
        ("transfer_fee_rate", True),
    ],
)
def test_invalid_per_fill_rate_override_is_rejected(field, value):
    with pytest.raises(ValueError, match=field):
        _engine().calculate_fill(
            100,
            parent_order_id="A",
            side="sell",
            **{field: value},
        )


def test_all_fee_components_are_separate_and_sum_to_total():
    result = _engine(transfer_fee_rate=0.00002).calculate_fill(
        100_000, parent_order_id="sell", side=FeeSide.SELL
    )

    assert result.commission == 25.0
    assert result.stamp_duty == 100.0
    assert result.transfer_fee == 2.0
    assert result.total == 127.0


def test_separate_parent_orders_have_independent_minimum_commission():
    engine = _engine()
    first_a = engine.calculate_fill(10_000, parent_order_id="A", side="buy")
    first_b = engine.calculate_fill(10_000, parent_order_id="B", side="buy")
    second_a = engine.calculate_fill(
        10_000,
        parent_order_id="A",
        side="buy",
        prior_state=first_a.next_state,
    )

    assert first_a.commission == 5.0
    assert first_b.commission == 5.0
    assert second_a.commission == 0.0
    assert first_b.next_state.cumulative_notional == 10_000.0


def test_state_from_another_parent_order_is_rejected():
    prior = _engine().calculate_fill(
        10_000, parent_order_id="A", side="buy"
    ).next_state

    with pytest.raises(ValueError, match="different parent order"):
        _engine().calculate_fill(
            10_000, parent_order_id="B", side="buy", prior_state=prior
        )


def test_state_with_another_side_is_rejected():
    prior = _engine().calculate_fill(
        10_000, parent_order_id="A", side="buy"
    ).next_state

    with pytest.raises(ValueError, match="side"):
        _engine().calculate_fill(
            10_000, parent_order_id="A", side="sell", prior_state=prior
        )


def test_state_from_another_fee_policy_is_rejected():
    prior = _engine().calculate_fill(
        10_000, parent_order_id="A", side="buy"
    ).next_state

    with pytest.raises(ValueError, match="different fee policy"):
        _engine(commission_rate=0.0003).calculate_fill(
            10_000, parent_order_id="A", side="buy", prior_state=prior
        )


def test_forged_parent_commission_state_is_rejected():
    engine = _engine()
    prior = engine.calculate_fill(
        10_000, parent_order_id="A", side="buy"
    ).next_state
    forged = replace(prior, commission_charged=4.99)

    with pytest.raises(ValueError, match="inconsistent"):
        engine.calculate_fill(
            10_000, parent_order_id="A", side="buy", prior_state=forged
        )


@pytest.mark.parametrize("side", ["BUY", "Sell", "long", "", None, 1, True])
def test_invalid_side_is_rejected(side):
    with pytest.raises(ValueError, match="side"):
        _engine().calculate_fill(100, parent_order_id="A", side=side)


@pytest.mark.parametrize("parent_order_id", ["", "   ", None, 1, True])
def test_invalid_parent_order_id_is_rejected(parent_order_id):
    with pytest.raises(ValueError, match="parent_order_id"):
        _engine().calculate_fill(
            100, parent_order_id=parent_order_id, side="buy"
        )


@pytest.mark.parametrize(
    "notional",
    [
        -1.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        "100.0",
        1 + 2j,
        None,
    ],
)
def test_invalid_fill_notional_is_rejected(notional):
    with pytest.raises(ValueError, match="notional"):
        _engine().calculate_fill(notional, parent_order_id="A", side="buy")


def test_zero_notional_is_a_fee_free_no_op():
    engine = _engine()
    prior = engine.calculate_fill(
        10_000, parent_order_id="A", side="sell"
    ).next_state
    result = engine.calculate_fill(
        0.0, parent_order_id="A", side="sell", prior_state=prior
    )

    assert result.commission == 0.0
    assert result.stamp_duty == 0.0
    assert result.transfer_fee == 0.0
    assert result.total == 0.0
    assert result.next_state is prior


def test_zero_notional_does_not_apply_minimum_on_new_order():
    result = _engine().calculate_fill(0, parent_order_id="A", side="buy")

    assert result.total == 0.0
    assert result.fill_count == 0
    assert result.cumulative_notional == 0.0


def test_all_zero_rates_and_minimum_produce_zero_fees():
    result = _engine(
        commission_rate=0.0,
        min_commission=0.0,
        stamp_duty_rate=0.0,
        transfer_fee_rate=0.0,
    ).calculate_fill(1_000_000, parent_order_id="A", side="sell")

    assert result.commission == 0.0
    assert result.stamp_duty == 0.0
    assert result.transfer_fee == 0.0
    assert result.total == 0.0


def test_zero_minimum_uses_percentage_commission_only():
    result = _engine(min_commission=0.0).calculate_fill(
        1_000, parent_order_id="A", side="buy"
    )

    assert result.commission == 0.25


def test_currency_rounding_uses_decimal_half_up_at_half_cent():
    result = _engine(
        commission_rate=1.0,
        min_commission=0.0,
        stamp_duty_rate=0.0,
    ).calculate_fill(0.005, parent_order_id="A", side="buy")

    assert result.commission == 0.01


def test_currency_rounding_below_half_cent_rounds_down():
    result = _engine(
        commission_rate=1.0,
        min_commission=0.0,
        stamp_duty_rate=0.0,
    ).calculate_fill(0.004999999, parent_order_id="A", side="buy")

    assert result.commission == 0.0


def test_cumulative_rounding_charges_only_the_new_cent_delta():
    engine = _engine(
        commission_rate=1.0,
        min_commission=0.0,
        stamp_duty_rate=0.0,
    )
    results = _partitioned_commissions(engine, [0.004, 0.001])

    assert [result.commission for result in results] == [0.0, 0.01]
    assert results[-1].cumulative_commission == 0.01


def test_each_tax_component_is_rounded_independently():
    result = _engine(
        commission_rate=0.0,
        min_commission=0.0,
        stamp_duty_rate=0.00005,
        transfer_fee_rate=0.00005,
    ).calculate_fill(100, parent_order_id="A", side="sell")

    assert result.stamp_duty == 0.01
    assert result.transfer_fee == 0.01
    assert result.total == 0.02


@pytest.mark.parametrize(
    ("field", "value"),
    [("stamp_duty_exempt", 0), ("stamp_duty_exempt", "false"),
     ("transfer_fee_exempt", 1), ("transfer_fee_exempt", None)],
)
def test_exemption_flags_require_real_booleans(field, value):
    kwargs = {field: value}

    with pytest.raises(ValueError, match=field):
        _engine().calculate_fill(
            100, parent_order_id="A", side="sell", **kwargs
        )


def test_calculation_is_pure_for_identical_inputs():
    engine = _engine(transfer_fee_rate=0.00002)
    first = engine.calculate_fill(100_000, parent_order_id="A", side="sell")
    second = engine.calculate_fill(100_000, parent_order_id="A", side="sell")

    assert first == second
    assert engine == _engine(transfer_fee_rate=0.00002)


def test_fee_calculation_has_no_cash_object_to_mutate():
    account_snapshot = {"cash": 100_000.0, "frozen_cash": 25_000.0}
    before = account_snapshot.copy()

    _engine().calculate_fill(10_000, parent_order_id="A", side="buy")

    assert account_snapshot == before


def test_fee_result_and_nested_state_are_immutable():
    result = _engine().calculate_fill(
        10_000, parent_order_id="A", side="buy"
    )

    with pytest.raises(FrozenInstanceError):
        result.total = 999
    with pytest.raises(FrozenInstanceError):
        result.next_state.commission_charged = 999


def test_schedule_and_engine_are_immutable():
    engine = _engine()

    with pytest.raises(FrozenInstanceError):
        engine.schedule.min_commission = 0
    with pytest.raises(FrozenInstanceError):
        engine.schedule = FeeSchedule()


def test_wrong_prior_state_type_is_rejected():
    with pytest.raises(TypeError, match="prior_state"):
        _engine().calculate_fill(
            100, parent_order_id="A", side="buy", prior_state={}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"fill_count": -1},
        {"fill_count": True},
        {"fill_count": 1, "cumulative_notional": 0.0},
        {"cumulative_notional": 1.0},
        {"commission_charged": 1.0},
        {"policy_key": ""},
    ],
)
def test_parent_order_state_rejects_invalid_invariants(changes):
    values = {
        "parent_order_id": "A",
        "side": FeeSide.BUY,
        "cumulative_notional": 0.0,
        "commission_charged": 0.0,
        "fill_count": 0,
        "policy_key": "policy",
    }
    values.update(changes)

    with pytest.raises((ValueError, TypeError)):
        ParentOrderFeeState(**values)


def test_fee_breakdown_rejects_total_that_does_not_match_components():
    state = _engine().initial_state("A", "buy")

    with pytest.raises(ValueError, match="total"):
        FeeBreakdown(
            parent_order_id="A",
            side=FeeSide.BUY,
            notional=100,
            commission=5.0,
            stamp_duty=0.0,
            transfer_fee=0.0,
            total=4.99,
            next_state=state,
        )


def test_fee_breakdown_rejects_unrounded_money():
    state = _engine().initial_state("A", "buy")

    with pytest.raises(ValueError, match="commission"):
        FeeBreakdown(
            parent_order_id="A",
            side=FeeSide.BUY,
            notional=100,
            commission=1.001,
            stamp_duty=0.0,
            transfer_fee=0.0,
            total=1.001,
            next_state=state,
        )


def test_derived_commission_overflow_is_rejected():
    engine = _engine(
        commission_rate=1e308,
        min_commission=0.0,
        stamp_duty_rate=0.0,
    )

    with pytest.raises(ValueError, match="commission"):
        engine.calculate_fill(2.0, parent_order_id="A", side="buy")


def test_cumulative_notional_overflow_is_rejected_before_result():
    engine = _engine(
        commission_rate=0.0,
        min_commission=0.0,
        stamp_duty_rate=0.0,
    )
    prior = engine.calculate_fill(
        1e308, parent_order_id="A", side="buy"
    ).next_state

    with pytest.raises(ValueError, match="cumulative_notional"):
        engine.calculate_fill(
            1e308, parent_order_id="A", side="buy", prior_state=prior
        )
