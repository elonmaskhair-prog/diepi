"""Broker-only contracts for explicit auction liquidity and one-window TIF."""

from copy import deepcopy
from datetime import datetime

import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import BarData, Broker
from diepi.backtest.broker.order import OrderStatus
from diepi.backtest.broker.settlement import SettlementInvariantError
from diepi.backtest.liquidity import AuctionLiquidityUnavailable


T = "20240102"
SYMBOL_A = "000001.SZ"
SYMBOL_B = "000002.SZ"


def _account() -> Account:
    return Account(
        initial_cash=1_000_000.0,
        commission_rate=0.0,
        min_commission=0.0,
        stamp_duty_rate=0.0,
    )


def _broker(account=None, *, ratio=0.1) -> Broker:
    broker = Broker(
        account=account or _account(),
        slippage=0.0,
        liquidity_cap_ratio=ratio,
        open_buy_sizing="fill",
        open_buy_fill_mode="open",
    )
    broker.set_date(T)
    return broker


def _bar(symbol=SYMBOL_A, *, amount=1_000_000.0, cap=None,
         require=False, window="open") -> BarData:
    return BarData(
        symbol=symbol,
        trade_time=datetime(2024, 1, 2, 9, 30),
        open=10.0,
        high=10.0,
        low=10.0,
        close=10.0,
        amount=amount,
        pre_close=10.0,
        liquidity_cap_amount=cap,
        require_explicit_liquidity_cap=require,
        liquidity_window=window,
    )


def _position(account: Account, symbol=SYMBOL_A, shares=1_000) -> None:
    amount = shares * 10.0
    assert account.freeze_cash(amount)
    account.buy(symbol, shares, 10.0, "20240101", fee_total=0.0)
    account.settle_t1(T)


def _submit(broker: Broker, window: str, side: str, shares=1_000):
    broker.set_market_phase("before_open" if window == "open" else "trading")
    method = getattr(broker, f"{side}_at_{window}")
    return method(SYMBOL_A, shares=shares)


def _execute(broker: Broker, window: str, bar: BarData):
    method = getattr(broker, f"execute_{window}_orders")
    return method({bar.symbol: bar})


@pytest.mark.parametrize("cap", [0.0, -1.0, float("nan"), float("inf")])
def test_explicit_cap_must_be_finite_and_positive(cap):
    with pytest.raises(ValueError, match="liquidity_cap_amount"):
        _bar(cap=cap)


@pytest.mark.parametrize("required", [0, 1, "true", None])
def test_explicit_cap_requirement_must_be_a_bool(required):
    with pytest.raises(ValueError, match="must be a bool"):
        _bar(require=required)


def test_explicit_cap_is_final_and_is_not_multiplied_by_ratio_again():
    broker = _broker(ratio=0.1)
    order = _submit(broker, "open", "buy")
    bar = _bar(cap=5_000.0, require=True)

    _execute(broker, "open", bar)

    assert order.status is OrderStatus.CANCELLED
    assert order.filled_shares == 500
    assert order.unfilled_shares == 500
    assert broker._bar_used_amount[SYMBOL_A] == 5_000.0
    assert broker.account.frozen_cash == 0.0


def test_missing_required_cap_fails_the_whole_auction_before_any_mutation():
    account = _account()
    _position(account, SYMBOL_A)
    _position(account, SYMBOL_B)
    broker = _broker(account)
    broker.set_market_phase("before_open")
    order_a = broker.sell_at_open(SYMBOL_A, shares=1_000)
    order_b = broker.sell_at_open(SYMBOL_B, shares=1_000)
    before = {
        "cash": account.cash,
        "frozen_cash": account.frozen_cash,
        "positions": {
            symbol: deepcopy(position.__dict__)
            for symbol, position in account.positions.items()
        },
        "orders": {
            order.order_id: deepcopy(order.__dict__)
            for order in (order_a, order_b)
        },
        "history": list(account.order_history),
        "bar_used": dict(broker._bar_used_amount),
    }

    with pytest.raises(AuctionLiquidityUnavailable, match="explicit liquidity cap"):
        broker.execute_open_orders({
            SYMBOL_A: _bar(SYMBOL_A, cap=10_000.0, require=True),
            SYMBOL_B: _bar(SYMBOL_B, cap=None, require=True),
        })

    assert account.cash == before["cash"]
    assert account.frozen_cash == before["frozen_cash"]
    assert {
        symbol: position.__dict__
        for symbol, position in account.positions.items()
    } == before["positions"]
    assert order_a.__dict__ == before["orders"][order_a.order_id]
    assert order_b.__dict__ == before["orders"][order_b.order_id]
    assert account.order_history == before["history"]
    assert broker._bar_used_amount == before["bar_used"]


@pytest.mark.parametrize("window", ["open", "close"])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_partial_auction_residual_is_cancelled_and_reservation_released(
    window, side
):
    account = _account()
    if side == "sell":
        _position(account)
    broker = _broker(account)
    order = _submit(broker, window, side)

    _execute(
        broker,
        window,
        _bar(cap=5_000.0, require=True, window=window),
    )

    assert order.status is OrderStatus.CANCELLED
    assert (order.filled_shares, order.unfilled_shares) == (500, 500)
    assert order.order_id not in account.orders
    assert account.order_history[-1] is order
    assert "residual cancelled" in order.reject_reason
    if side == "buy":
        assert order.frozen_amount == 0.0
        assert account.frozen_cash == 0.0
        assert account.positions[SYMBOL_A].shares == 500
    else:
        assert order.frozen_shares == 0
        position = account.positions[SYMBOL_A]
        assert (position.shares, position.frozen_shares) == (500, 0)


@pytest.mark.parametrize("window", ["open", "close"])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_zero_fill_auction_is_cancelled_and_reservation_released(window, side):
    account = _account()
    if side == "sell":
        _position(account)
    broker = _broker(account)
    order = _submit(broker, window, side)

    _execute(broker, window, _bar(amount=0.0, window=window))

    assert order.status is OrderStatus.CANCELLED
    assert order.filled_shares == 0
    assert order.order_id not in account.orders
    if side == "buy":
        assert order.frozen_amount == account.frozen_cash == 0.0
    else:
        assert order.frozen_shares == 0
        assert account.positions[SYMBOL_A].frozen_shares == 0


@pytest.mark.parametrize("window", ["open", "close"])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_missing_symbol_is_terminal_cancel_and_releases_reservation(window, side):
    account = _account()
    if side == "sell":
        _position(account)
    broker = _broker(account)
    order = _submit(broker, window, side)

    getattr(broker, f"execute_{window}_orders")({})

    assert order.status is OrderStatus.CANCELLED
    assert order.filled_shares == 0
    assert "no market data" in order.reject_reason
    assert order.order_id not in account.orders
    if side == "buy":
        assert order.frozen_amount == account.frozen_cash == 0.0
    else:
        assert order.frozen_shares == 0
        assert account.positions[SYMBOL_A].frozen_shares == 0


def test_multiple_orders_share_one_explicit_cap():
    account = _account()
    _position(account)
    broker = _broker(account, ratio=0.1)
    broker.set_market_phase("before_open")
    first = broker.sell_at_open(SYMBOL_A, shares=500)
    second = broker.sell_at_open(SYMBOL_A, shares=500)

    broker.execute_open_orders({
        SYMBOL_A: _bar(cap=7_500.0, require=True),
    })

    assert first.status is OrderStatus.FILLED
    assert first.filled_shares == 500
    assert second.status is OrderStatus.CANCELLED
    assert (second.filled_shares, second.unfilled_shares) == (200, 300)
    assert broker._bar_used_amount[SYMBOL_A] == 7_000.0
    position = account.positions[SYMBOL_A]
    assert (position.shares, position.frozen_shares) == (300, 0)


def test_ordinary_bar_still_uses_amount_times_participation_ratio():
    broker = _broker(ratio=0.5)
    order = _submit(broker, "open", "buy")

    _execute(broker, "open", _bar(amount=10_000.0))

    assert order.status is OrderStatus.CANCELLED
    assert order.filled_shares == 500
    assert broker._bar_used_amount[SYMBOL_A] == 5_000.0


def test_partial_open_feed_does_not_cancel_a_symbol_before_its_bar_arrives():
    account = _account()
    _position(account, SYMBOL_A)
    _position(account, SYMBOL_B)
    broker = _broker(account)
    broker.set_market_phase("before_open")
    first = broker.sell_at_open(SYMBOL_A, shares=1_000)
    waiting = broker.sell_at_open(SYMBOL_B, shares=1_000)

    broker.execute_open_orders(
        {SYMBOL_A: _bar(SYMBOL_A, cap=10_000.0, require=True)},
        reject_missing=False,
    )

    assert first.status is OrderStatus.FILLED
    assert waiting.status is OrderStatus.SUBMITTED
    assert waiting.frozen_shares == 1_000
    assert account.positions[SYMBOL_B].frozen_shares == 1_000


def test_partial_close_feed_does_not_cancel_a_symbol_before_its_bar_arrives():
    account = _account()
    _position(account, SYMBOL_A)
    _position(account, SYMBOL_B)
    broker = _broker(account)
    broker.set_market_phase("trading")
    first = broker.sell_at_close(SYMBOL_A, shares=1_000)
    waiting = broker.sell_at_close(SYMBOL_B, shares=1_000)

    broker.execute_close_orders(
        {SYMBOL_A: _bar(
            SYMBOL_A, cap=10_000.0, require=True, window="close")},
        reject_missing=False,
    )

    assert first.status is OrderStatus.FILLED
    assert waiting.status is OrderStatus.SUBMITTED
    assert waiting.frozen_shares == 1_000
    assert account.positions[SYMBOL_B].frozen_shares == 1_000


def test_used_amount_above_explicit_cap_fails_before_auto_buy_preparation():
    broker = _broker()
    order = _submit(broker, "open", "buy")
    broker._bar_used_amount[SYMBOL_A] = 10_001.0
    before = {
        "cash": broker.account.cash,
        "frozen_cash": broker.account.frozen_cash,
        "order": deepcopy(order.__dict__),
        "pending": list(broker._pending_open_orders),
        "history": list(broker.account.order_history),
        "bar_used": dict(broker._bar_used_amount),
    }

    with pytest.raises(SettlementInvariantError, match="exceeds"):
        _execute(
            broker,
            "open",
            _bar(cap=10_000.0, require=True),
        )

    assert broker.account.cash == before["cash"]
    assert broker.account.frozen_cash == before["frozen_cash"]
    assert order.__dict__ == before["order"]
    assert broker._pending_open_orders == before["pending"]
    assert broker.account.order_history == before["history"]
    assert broker._bar_used_amount == before["bar_used"]


def test_auto_buy_preparation_and_pending_queue_roll_back_on_late_failure(
    monkeypatch,
):
    broker = _broker()
    order = _submit(broker, "open", "buy")
    before = {
        "cash": broker.account.cash,
        "frozen_cash": broker.account.frozen_cash,
        "order": deepcopy(order.__dict__),
        "pending": list(broker._pending_open_orders),
        "history": list(broker.account.order_history),
    }

    def fail_after_preparation(self, *args, **kwargs):
        raise RuntimeError("late auction failure")

    monkeypatch.setattr(Broker, "_execute_order", fail_after_preparation)

    with pytest.raises(RuntimeError, match="late auction failure"):
        _execute(
            broker,
            "open",
            _bar(cap=10_000.0, require=True),
        )

    assert broker.account.cash == before["cash"]
    assert broker.account.frozen_cash == before["frozen_cash"]
    assert order.__dict__ == before["order"]
    assert broker._pending_open_orders == before["pending"]
    assert broker.account.order_history == before["history"]


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_failure_after_residual_cancellation_rolls_back_the_whole_auction(
    monkeypatch, side
):
    account = _account()
    if side == "sell":
        _position(account)
    broker = _broker(account)
    order = _submit(broker, "open", side)
    before = {
        "cash": account.cash,
        "frozen_cash": account.frozen_cash,
        "positions": {
            symbol: deepcopy(position.__dict__)
            for symbol, position in account.positions.items()
        },
        "order": deepcopy(order.__dict__),
        "orders": dict(account.orders),
        "history": list(account.order_history),
        "pending": list(broker._pending_open_orders),
        "bar_used": dict(broker._bar_used_amount),
        "fee_states": dict(broker._order_fee_states),
    }
    original = Broker._cancel_auction_residual

    def cancel_then_fail(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("failure after auction cancellation")

    monkeypatch.setattr(Broker, "_cancel_auction_residual", cancel_then_fail)

    with pytest.raises(RuntimeError, match="after auction cancellation"):
        _execute(
            broker,
            "open",
            _bar(cap=5_000.0, require=True),
        )

    assert account.cash == before["cash"]
    assert account.frozen_cash == before["frozen_cash"]
    assert {
        symbol: position.__dict__
        for symbol, position in account.positions.items()
    } == before["positions"]
    assert order.__dict__ == before["order"]
    assert account.orders == before["orders"]
    assert account.order_history == before["history"]
    assert broker._pending_open_orders == before["pending"]
    assert broker._bar_used_amount == before["bar_used"]
    assert broker._order_fee_states == before["fee_states"]


def test_auction_bar_mapping_symbol_mismatch_fails_before_mutation():
    broker = _broker()
    order = _submit(broker, "open", "buy")
    before = deepcopy(order.__dict__)

    with pytest.raises(SettlementInvariantError, match="mapping key"):
        broker.execute_open_orders({
            SYMBOL_A: _bar(SYMBOL_B, cap=10_000.0, require=True),
        })

    assert order.__dict__ == before
    assert broker.account.frozen_cash == 0.0


def test_rogue_same_id_object_cannot_cancel_the_real_active_order():
    broker = _broker()
    order = _submit(broker, "open", "buy")
    rogue = deepcopy(order)
    before = deepcopy(order.__dict__)

    with pytest.raises(SettlementInvariantError, match="active account order"):
        broker._cancel_auction_residual(rogue, "rogue cancellation")

    assert order.__dict__ == before
    assert broker.account.orders[order.order_id] is order
    assert broker.account.order_history == []
