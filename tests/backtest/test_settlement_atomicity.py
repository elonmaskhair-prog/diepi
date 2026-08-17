"""Synthetic contracts for per-fill settlement atomicity and fee state."""

from copy import deepcopy
from datetime import datetime

import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import BarData, Broker
from diepi.backtest.broker.fees import CommissionScope
from diepi.backtest.broker.order import Order, OrderSide, OrderStatus, OrderType
from diepi.backtest.broker.settlement import (
    SettlementInvariantError,
    SettlementUnitOfWork,
)


SYMBOL = "000001.SZ"
TRADE_DATE = "20240102"


def _bar(amount=10_000.0, minute=30):
    return BarData(
        symbol=SYMBOL,
        trade_time=datetime(2024, 1, 2, 9, minute),
        open=10.0,
        high=10.0,
        low=10.0,
        close=10.0,
        amount=amount,
        pre_close=10.0,
    )


def _buy_setup(*, frozen=10_005.0, commission_scope="parent_order"):
    account = Account(initial_cash=100_000.0)
    assert account.freeze_cash(frozen)
    order = Order(
        symbol=SYMBOL,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        shares=1_000,
        status=OrderStatus.SUBMITTED,
        frozen_amount=frozen,
        order_id="buy-order",
    )
    account.add_order(order)
    broker = Broker(
        account=account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
        commission_scope=commission_scope,
    )
    broker.current_date = TRADE_DATE
    return broker, order


def _sell_setup(*, commission_scope="parent_order"):
    account = Account(initial_cash=100_000.0)
    assert account.freeze_cash(10_005.0)
    account.buy(SYMBOL, 1_000, 10.0, "20240101")
    account.settle_t1(TRADE_DATE)
    assert account.freeze_shares(SYMBOL, 1_000) == 1_000
    order = Order(
        symbol=SYMBOL,
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        shares=1_000,
        status=OrderStatus.SUBMITTED,
        frozen_shares=1_000,
        order_id="sell-order",
    )
    account.add_order(order)
    broker = Broker(
        account=account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
        commission_scope=commission_scope,
    )
    broker.current_date = TRADE_DATE
    return broker, order


def _state(broker, order):
    account = broker.account
    return {
        "cash": account.cash,
        "frozen_cash": account.frozen_cash,
        "positions": {
            symbol: (id(position), deepcopy(position.__dict__))
            for symbol, position in account.positions.items()
        },
        "order": deepcopy(order.__dict__),
        "orders": {order_id: id(value) for order_id, value in account.orders.items()},
        "history": [id(value) for value in account.order_history],
        "bar_used": dict(broker._bar_used_amount),
        "fee_states": dict(broker._order_fee_states),
    }


def test_broker_defaults_to_parent_order_commission_scope():
    broker = Broker(account=Account())

    assert broker.commission_scope == "parent_order"
    assert broker.fee_engine.commission_scope is CommissionScope.PARENT_ORDER


def test_broker_accepts_explicit_fill_commission_stress_scope():
    broker = Broker(account=Account(), commission_scope="fill")

    assert broker.commission_scope == "fill"
    assert broker.fee_engine.commission_scope is CommissionScope.FILL


def test_account_explicit_fee_and_strict_paths_preserve_legacy_public_api():
    account = Account(initial_cash=10_000.0)
    assert account.freeze_cash(1_000.0)

    cost = account.buy(
        SYMBOL,
        100,
        10.0,
        TRADE_DATE,
        fee_total=0.0,
        strict_settlement=True,
    )
    assert cost == 1_000.0
    assert account.positions[SYMBOL].cost_basis == 1_000.0

    # The unchanged legacy path still computes its own minimum commission and
    # permits direct calls without a prior broker reservation.
    legacy = Account(initial_cash=10_000.0)
    assert legacy.buy(
        SYMBOL, 100, 10.0, TRADE_DATE,
        strict_settlement=False,
    ) == 1_005.0


def test_parent_order_buy_partial_fills_charge_minimum_commission_once():
    broker, order = _buy_setup()

    broker._execute_order(order, 10.0, _bar(amount=5_000.0), ref_price=10.0)
    assert order.status is OrderStatus.PARTIAL
    assert order.filled_shares == 500
    assert order.frozen_amount == 5_000.0
    assert order.commission_paid == order.total_fees == 5.0
    assert order.fee_fill_count == 1

    broker.reset_bar_used_amount()
    broker._execute_order(
        order, 10.0, _bar(amount=5_000.0, minute=31), ref_price=10.0
    )

    assert order.status is OrderStatus.FILLED
    assert order.filled_shares == 1_000
    assert order.commission_paid == order.total_fees == 5.0
    assert order.fee_fill_count == 2
    assert broker.account.positions[SYMBOL].cost_basis == 10_005.0
    assert order.order_id not in broker.account.orders
    assert broker.account.order_history[-1] is order
    assert order.to_dict()["commission_paid"] == 5.0
    assert order.to_dict()["fee_fill_count"] == 2
    state = broker._order_fee_states[order.order_id]
    assert state.cumulative_notional == 10_000.0
    assert state.commission_charged == 5.0
    assert state.fill_count == 2


def test_fill_scope_buy_partial_fills_charge_minimum_per_fill():
    broker, order = _buy_setup(frozen=10_010.0, commission_scope="fill")

    broker._execute_order(order, 10.0, _bar(amount=5_000.0), ref_price=10.0)
    broker.reset_bar_used_amount()
    broker._execute_order(
        order, 10.0, _bar(amount=5_000.0, minute=31), ref_price=10.0
    )

    assert order.status is OrderStatus.FILLED
    assert order.commission_paid == order.total_fees == 10.0


def test_transfer_fee_is_included_in_cash_cost_and_order_audit_fields():
    account = Account(initial_cash=100_000.0)
    assert account.freeze_cash(10_005.20)
    order = Order(
        SYMBOL,
        OrderSide.BUY,
        OrderType.MARKET,
        shares=1_000,
        status=OrderStatus.SUBMITTED,
        frozen_amount=10_005.20,
        order_id="transfer-fee-buy",
    )
    account.add_order(order)
    broker = Broker(
        account=account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
        transfer_fee_rate=0.00002,
    )
    broker.current_date = TRADE_DATE

    broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert order.commission_paid == 5.0
    assert order.transfer_fee_paid == pytest.approx(0.20)
    assert order.total_fees == pytest.approx(5.20)
    assert account.positions[SYMBOL].cost_basis == pytest.approx(10_005.20)
    assert order.to_dict()["transfer_fee_paid"] == pytest.approx(0.20)


def test_parent_order_sell_partial_and_full_fee_components_are_auditable():
    broker, order = _sell_setup()

    broker._execute_order(order, 10.0, _bar(amount=5_000.0), ref_price=10.0)
    position = broker.account.positions[SYMBOL]
    assert (position.shares, position.frozen_shares) == (500, 500)
    assert order.status is OrderStatus.PARTIAL
    assert order.frozen_shares == 500
    assert (order.commission_paid, order.stamp_duty_paid, order.total_fees) == (
        5.0,
        5.0,
        10.0,
    )

    broker.reset_bar_used_amount()
    broker._execute_order(
        order, 10.0, _bar(amount=5_000.0, minute=31), ref_price=10.0
    )

    assert order.status is OrderStatus.FILLED
    assert SYMBOL not in broker.account.positions
    assert (order.commission_paid, order.stamp_duty_paid) == (5.0, 10.0)
    assert order.transfer_fee_paid == 0.0
    assert order.total_fees == 15.0
    assert order.fee_fill_count == 2
    assert broker.account.cash == 99_980.0


def test_auto_stamp_duty_uses_the_explicit_fill_date_rate():
    account = Account(initial_cash=100_000.0, stamp_duty_rate="auto")
    assert account.freeze_cash(10_005.0)
    account.buy(SYMBOL, 1_000, 10.0, "20240101")
    account.settle_t1(TRADE_DATE)
    assert account.freeze_shares(SYMBOL, 1_000) == 1_000
    order = Order(
        SYMBOL,
        OrderSide.SELL,
        OrderType.MARKET,
        shares=1_000,
        status=OrderStatus.SUBMITTED,
        frozen_shares=1_000,
        order_id="auto-stamp-sell",
    )
    account.add_order(order)
    broker = Broker(account=account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.current_date = TRADE_DATE

    broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    # Since 2023-08-28, stock stamp duty is 0.05%; FeeEngine receives that
    # resolved rate explicitly rather than the schedule's sentinel fallback.
    assert order.commission_paid == 5.0
    assert order.stamp_duty_paid == 5.0
    assert order.total_fees == 10.0


def test_active_order_identity_is_a_hard_precondition():
    broker, order = _buy_setup()
    replacement = deepcopy(order)
    broker.account.orders[order.order_id] = replacement
    before = _state(broker, order)

    with pytest.raises(SettlementInvariantError, match="active account order object"):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert _state(broker, order) == before


def test_buy_reservation_drift_fails_before_creating_a_position():
    broker, order = _buy_setup()
    broker.account.frozen_cash = 0.0
    before = _state(broker, order)

    with pytest.raises(SettlementInvariantError, match="aggregate active BUY"):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert _state(broker, order) == before
    assert SYMBOL not in broker.account.positions


def test_sell_without_position_cannot_create_a_phantom_fill():
    account = Account(initial_cash=100_000.0)
    order = Order(
        SYMBOL,
        OrderSide.SELL,
        OrderType.MARKET,
        shares=1_000,
        status=OrderStatus.SUBMITTED,
        frozen_shares=1_000,
        order_id="drifted-sell",
    )
    account.add_order(order)
    broker = Broker(account=account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.current_date = TRADE_DATE
    before = _state(broker, order)

    with pytest.raises(SettlementInvariantError, match="aggregate active SELL"):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert _state(broker, order) == before
    assert order.filled_shares == 0


def test_sell_order_and_position_reservations_must_remain_consistent():
    broker, order = _sell_setup()
    broker.account.positions[SYMBOL].frozen_shares = 500
    before = _state(broker, order)

    with pytest.raises(SettlementInvariantError, match="aggregate active SELL"):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert _state(broker, order) == before


def test_multiple_buy_orders_must_reconcile_to_account_frozen_cash():
    broker, order = _buy_setup()
    competing = Order(
        SYMBOL,
        OrderSide.BUY,
        OrderType.MARKET,
        shares=100,
        status=OrderStatus.SUBMITTED,
        frozen_amount=1_005.0,
        order_id="competing-buy",
    )
    # Deliberately omit the matching Account.freeze_cash call.
    broker.account.add_order(competing)
    before = _state(broker, order)

    with pytest.raises(SettlementInvariantError, match="aggregate active BUY"):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert _state(broker, order) == before


def test_multiple_sell_orders_must_reconcile_to_position_frozen_shares():
    broker, order = _sell_setup()
    competing = Order(
        SYMBOL,
        OrderSide.SELL,
        OrderType.MARKET,
        shares=100,
        status=OrderStatus.SUBMITTED,
        frozen_shares=100,
        order_id="competing-sell",
    )
    # Deliberately omit the matching Position.freeze_shares call.
    broker.account.add_order(competing)
    before = _state(broker, order)

    with pytest.raises(SettlementInvariantError, match="aggregate active SELL"):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert _state(broker, order) == before


def test_unmanaged_frozen_cash_baseline_is_preserved_by_successful_fill():
    broker, order = _buy_setup()
    assert broker.account.freeze_cash(1_000.0)

    broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert order.status is OrderStatus.FILLED
    assert order.frozen_amount == 0.0
    assert broker.account.frozen_cash == 1_000.0


def test_unmanaged_frozen_shares_baseline_is_preserved_by_successful_fill():
    account = Account(initial_cash=100_000.0)
    assert account.freeze_cash(11_005.0)
    account.buy(SYMBOL, 1_100, 10.0, "20240101")
    account.settle_t1(TRADE_DATE)
    assert account.freeze_shares(SYMBOL, 1_100) == 1_100
    order = Order(
        SYMBOL,
        OrderSide.SELL,
        OrderType.MARKET,
        shares=1_000,
        status=OrderStatus.SUBMITTED,
        frozen_shares=1_000,
        order_id="sell-with-unmanaged-baseline",
    )
    account.add_order(order)
    broker = Broker(account=account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.current_date = TRADE_DATE

    broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert order.status is OrderStatus.FILLED
    assert order.frozen_shares == 0
    position = account.positions[SYMBOL]
    assert (position.shares, position.frozen_shares) == (100, 100)


def test_changed_unmanaged_reservation_baseline_aborts_and_rolls_back(
    monkeypatch,
):
    broker, order = _buy_setup()
    before = _state(broker, order)
    original = Account.buy

    def leak_frozen_cash(self, *args, **kwargs):
        cost = original(self, *args, **kwargs)
        self.frozen_cash += 1.0
        return cost

    monkeypatch.setattr(Account, "buy", leak_frozen_cash)

    with pytest.raises(SettlementInvariantError, match="postcondition failed"):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert _state(broker, order) == before


@pytest.mark.parametrize("bad_used", [-1.0, float("nan"), float("inf")])
def test_invalid_bar_usage_fails_before_any_settlement(bad_used):
    broker, order = _buy_setup()
    broker._bar_used_amount[SYMBOL] = bad_used
    before = _state(broker, order)

    with pytest.raises(ValueError, match="bar used amount"):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    after = _state(broker, order)
    if bad_used != bad_used:  # NaN is deliberately not equal to itself.
        assert after["cash"] == before["cash"]
        assert after["order"] == before["order"]
        assert after["fee_states"] == before["fee_states"]
        assert after["bar_used"][SYMBOL] != after["bar_used"][SYMBOL]
    else:
        assert after == before


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize(
    "participant", ["account", "order_fill", "fee_audit", "complete"]
)
def test_silent_noop_participant_aborts_and_restores_the_complete_snapshot(
    monkeypatch, side, participant
):
    broker, order = _buy_setup() if side == "buy" else _sell_setup()
    before = _state(broker, order)

    if participant == "account":
        def return_plan_without_mutating(
            self, symbol, shares, price, *args, **kwargs
        ):
            notional = shares * price
            fees = kwargs["fee_total"]
            return notional + fees if side == "buy" else notional - fees

        monkeypatch.setattr(Account, side, return_plan_without_mutating)
    elif participant == "order_fill":
        monkeypatch.setattr(Order, "fill", lambda self, *args, **kwargs: self)
    elif participant == "fee_audit":
        monkeypatch.setattr(
            Order, "record_fees", lambda self, *args, **kwargs: self
        )
    else:
        monkeypatch.setattr(
            Account, "complete_order", lambda self, completed_order: None
        )

    with pytest.raises(SettlementInvariantError):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert _state(broker, order) == before
    assert broker.account.orders[order.order_id] is order


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize(
    "corruption", ["cash", "quantity", "available", "avg_cost"]
)
def test_wrong_post_state_delta_is_detected_and_rolled_back(
    monkeypatch, side, corruption
):
    broker, order = _buy_setup() if side == "buy" else _sell_setup()
    before = _state(broker, order)
    original = getattr(Account, side)

    def settle_then_corrupt(self, symbol, shares, price, *args, **kwargs):
        result = original(self, symbol, shares, price, *args, **kwargs)
        position = self.positions[symbol]
        if corruption == "cash":
            self.cash += 1.0
        elif corruption == "quantity":
            position.shares += 1
        elif corruption == "available":
            if side == "buy":
                position._pending_shares[TRADE_DATE] -= 1
            else:
                position.frozen_shares -= 1
        else:
            position.avg_cost += 1.0
        return result

    monkeypatch.setattr(Account, side, settle_then_corrupt)

    with pytest.raises(SettlementInvariantError):
        broker._execute_order(
            order, 10.0, _bar(amount=5_000.0), ref_price=10.0
        )

    assert _state(broker, order) == before


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("fault", ["account", "order_fill", "liquidity", "complete"])
def test_fault_at_any_commit_stage_restores_the_complete_snapshot(
    monkeypatch, side, fault
):
    broker, order = _buy_setup() if side == "buy" else _sell_setup()
    before = _state(broker, order)

    if fault == "account":
        method_name = side
        original = getattr(Account, method_name)

        def mutate_then_raise(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise RuntimeError("injected account failure")

        monkeypatch.setattr(Account, method_name, mutate_then_raise)
    elif fault == "order_fill":
        original = Order.fill

        def mutate_then_raise(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise RuntimeError("injected order.fill failure")

        monkeypatch.setattr(Order, "fill", mutate_then_raise)
    elif fault == "liquidity":
        original = Broker._consume_amount

        def mutate_then_raise(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise RuntimeError("injected liquidity failure")

        monkeypatch.setattr(Broker, "_consume_amount", mutate_then_raise)
    else:
        original = Account.complete_order

        def mutate_then_raise(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise RuntimeError("injected completion failure")

        monkeypatch.setattr(Account, "complete_order", mutate_then_raise)

    with pytest.raises(RuntimeError, match="injected"):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert _state(broker, order) == before
    assert broker.account.orders[order.order_id] is order


def test_base_exception_also_rolls_back_and_is_not_suppressed(monkeypatch):
    class FatalProbe(BaseException):
        pass

    broker, order = _buy_setup()
    before = _state(broker, order)
    original = Broker._consume_amount

    def mutate_then_raise(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise FatalProbe("fatal probe")

    monkeypatch.setattr(Broker, "_consume_amount", mutate_then_raise)

    with pytest.raises(FatalProbe, match="fatal probe"):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert _state(broker, order) == before


def test_failed_second_partial_fill_does_not_advance_fee_state(monkeypatch):
    broker, order = _buy_setup()
    broker._execute_order(order, 10.0, _bar(amount=5_000.0), ref_price=10.0)
    broker.reset_bar_used_amount()
    before = _state(broker, order)
    prior_fee_state = broker._order_fee_states[order.order_id]
    original = Broker._consume_amount

    def mutate_then_raise(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("second fill failed")

    monkeypatch.setattr(Broker, "_consume_amount", mutate_then_raise)

    with pytest.raises(RuntimeError, match="second fill failed"):
        broker._execute_order(
            order, 10.0, _bar(amount=5_000.0, minute=31), ref_price=10.0
        )

    assert _state(broker, order) == before
    assert broker._order_fee_states[order.order_id] is prior_fee_state
    assert order.filled_shares == 500
    assert order.commission_paid == order.total_fees == 5.0
    assert order.fee_fill_count == 1


def test_failure_after_fee_state_assignment_rolls_back_state_and_totals(
    monkeypatch,
):
    broker, order = _buy_setup()
    broker._execute_order(order, 10.0, _bar(amount=5_000.0), ref_price=10.0)
    broker.reset_bar_used_amount()
    before = _state(broker, order)
    prior_fee_state = broker._order_fee_states[order.order_id]
    original = SettlementUnitOfWork.commit

    def mark_committed_then_raise(self):
        original(self)
        raise RuntimeError("failure after fee-state assignment")

    monkeypatch.setattr(SettlementUnitOfWork, "commit", mark_committed_then_raise)

    with pytest.raises(RuntimeError, match="after fee-state assignment"):
        broker._execute_order(
            order, 10.0, _bar(amount=5_000.0, minute=31), ref_price=10.0
        )

    assert _state(broker, order) == before
    assert broker._order_fee_states[order.order_id] is prior_fee_state
    assert order.commission_paid == order.total_fees == 5.0
    assert order.fee_fill_count == 1


def test_liquidity_noop_is_detected_and_rolled_back(monkeypatch):
    broker, order = _buy_setup()
    before = _state(broker, order)
    monkeypatch.setattr(Broker, "_consume_amount", lambda self, symbol, amount: None)

    with pytest.raises(SettlementInvariantError, match="liquidity consumption"):
        broker._execute_order(order, 10.0, _bar(), ref_price=10.0)

    assert _state(broker, order) == before
