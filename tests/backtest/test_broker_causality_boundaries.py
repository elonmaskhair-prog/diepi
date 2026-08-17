"""Synthetic Broker-only tests for future eligibility and partial bar feeds."""

from contextlib import nullcontext
from datetime import datetime

import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import BarData, Broker
from diepi.backtest.broker.order import OrderStatus


T = '20240102'
T1 = '20240103'
SYMBOL_A = '000001.SZ'
SYMBOL_B = '000002.SZ'


def _account() -> Account:
    return Account(
        initial_cash=1_000_000.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )


def _broker(account=None) -> Broker:
    broker = Broker(
        account=account or _account(),
        slippage=0.0,
        liquidity_cap_ratio=1.0,
    )
    broker.set_date(T)
    return broker


def _bar(symbol: str, when: datetime, *, low=9.0) -> BarData:
    return BarData(
        symbol=symbol,
        trade_time=when,
        open=10.0,
        high=10.5,
        low=low,
        close=10.2,
        vol=1_000_000.0,
        amount=10_000_000.0,
        pre_close=10.0,
    )


def _buy_position(account: Account, symbol: str, *, settle: bool) -> None:
    account.freeze_cash(10_000.0)
    account.buy(symbol, 1_000, 10.0, T)
    account.unfreeze_cash(account.frozen_cash)
    if settle:
        account.settle_t1(T1)


def _on_day_scope(broker: Broker):
    return broker.order_creation_scope(
        'on_day',
        datetime(2024, 1, 2, 15, 0),
        eligible_from=datetime(2024, 1, 3, 9, 30),
        expire_date=T1,
    )


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_on_day_open_order_waits_for_t1_first_open_window(side):
    account = _account()
    if side == 'sell':
        # Deliberately leave T's purchase T+1-locked at submission time.
        _buy_position(account, SYMBOL_A, settle=False)
    broker = _broker(account)
    broker.set_market_phase('trading')

    with _on_day_scope(broker):
        if side == 'buy':
            order = broker.buy_at_open(SYMBOL_A, shares=100)
        else:
            order = broker.sell_at_open(SYMBOL_A, shares=1_000)

    assert order.status == OrderStatus.SUBMITTED
    assert order.created_phase == 'on_day'
    assert order.eligible_from == datetime(2024, 1, 3, 9, 30)
    if side == 'sell':
        assert order.shares == 0
        assert order.frozen_shares == 0
        assert order.requested_shares == 1_000

    # Even an accidental second invocation of T's open matcher cannot fill it.
    broker.execute_open_orders(
        {SYMBOL_A: _bar(SYMBOL_A, datetime(2024, 1, 2, 15, 0))},
        match_at=datetime(2024, 1, 2, 15, 0),
    )
    assert order.status == OrderStatus.SUBMITTED
    assert order.filled_shares == 0
    broker.on_day_end()
    assert order.status == OrderStatus.SUBMITTED

    broker.on_day_start(T1)
    broker.set_market_phase('before_open')
    broker.reset_bar_used_amount()
    before_shares = (
        account.get_position(SYMBOL_A).shares if side == 'sell' else 0)
    broker.execute_open_orders(
        {SYMBOL_A: _bar(SYMBOL_A, datetime(2024, 1, 3, 9, 30))},
        match_at=datetime(2024, 1, 3, 9, 30),
    )

    assert order.status == OrderStatus.FILLED
    assert order.filled_shares == (100 if side == 'buy' else 1_000)
    if side == 'sell':
        remaining = account.get_position(SYMBOL_A)
        after_shares = remaining.shares if remaining is not None else 0
        assert order.filled_shares == before_shares - after_shares


@pytest.mark.parametrize('scope_phase', [None, 'on_minute', 'after_close'])
def test_open_order_outside_preopen_still_rejects_non_on_day_sources(
        scope_phase):
    broker = _broker()
    broker.set_market_phase('trading')
    scope = (
        nullcontext()
        if scope_phase is None
        else broker.order_creation_scope(
            scope_phase,
            datetime(2024, 1, 2, 10, 0),
            eligible_from=datetime(2024, 1, 3, 9, 30),
            expire_date=T1,
        )
    )

    with scope:
        order = broker.buy_at_open(SYMBOL_A, shares=100)

    assert order.status == OrderStatus.REJECTED


def test_partial_open_bar_feed_keeps_other_buy_until_its_first_bar():
    broker = _broker()
    broker.set_market_phase('before_open')
    order_a = broker.buy_at_open(SYMBOL_A, shares=100)
    order_b = broker.buy_at_open(SYMBOL_B, shares=100)
    first_at = datetime(2024, 1, 2, 9, 30)

    executed_a = broker.execute_open_orders(
        {SYMBOL_A: _bar(SYMBOL_A, first_at)},
        match_at=first_at,
        reject_missing=False,
    )

    assert order_a in executed_a
    assert order_a.status == OrderStatus.FILLED
    assert order_b.status == OrderStatus.SUBMITTED
    assert order_b in broker.get_open_orders()

    broker.reset_bar_used_amount()
    executed_b = broker.execute_open_orders(
        {SYMBOL_B: _bar(SYMBOL_B, first_at)},
        match_at=first_at,
        reject_missing=False,
    )
    assert order_b in executed_b
    assert order_b.status == OrderStatus.FILLED


def test_partial_open_bar_feed_keeps_other_sell_frozen_until_its_first_bar():
    account = _account()
    _buy_position(account, SYMBOL_A, settle=True)
    _buy_position(account, SYMBOL_B, settle=True)
    broker = _broker(account)
    broker.set_market_phase('before_open')
    order_a = broker.sell_at_open(SYMBOL_A, shares=1_000)
    order_b = broker.sell_at_open(SYMBOL_B, shares=1_000)
    first_at = datetime(2024, 1, 2, 9, 30)

    broker.execute_open_orders(
        {SYMBOL_A: _bar(SYMBOL_A, first_at)},
        match_at=first_at,
        reject_missing=False,
    )

    assert order_a.status == OrderStatus.FILLED
    assert order_b.status == OrderStatus.SUBMITTED
    assert order_b.frozen_shares == 1_000
    assert account.get_position(SYMBOL_B).frozen_shares == 1_000

    broker.reset_bar_used_amount()
    broker.execute_open_orders(
        {SYMBOL_B: _bar(SYMBOL_B, first_at)},
        match_at=first_at,
        reject_missing=False,
    )
    assert order_b.status == OrderStatus.FILLED


def test_default_missing_open_bar_behavior_is_terminal_cancellation():
    broker = _broker()
    broker.set_market_phase('before_open')
    order = broker.buy_at_open(SYMBOL_B, shares=100)

    broker.execute_open_orders({
        SYMBOL_A: _bar(SYMBOL_A, datetime(2024, 1, 2, 9, 30)),
    })

    assert order.status == OrderStatus.CANCELLED
    assert order.reject_reason == 'Open auction cancelled: no market data'


@pytest.mark.parametrize('order_kind', ['market', 'limit', 'stop', 'close'])
def test_t_locked_position_future_sell_materializes_at_first_eligible_window(
        order_kind):
    account = _account()
    _buy_position(account, SYMBOL_A, settle=False)
    broker = _broker(account)
    broker.set_market_phase('trading')

    with _on_day_scope(broker):
        if order_kind == 'market':
            order = broker.sell_at_market(SYMBOL_A, shares=1_000)
        elif order_kind == 'limit':
            order = broker.sell_at_price(
                SYMBOL_A, price=9.5, shares=1_000)
        elif order_kind == 'stop':
            order = broker.sell_stop_loss(
                SYMBOL_A, price=9.5, shares=1_000)
        else:
            order = broker.sell_at_close(SYMBOL_A, shares=1_000)

    assert order.status == OrderStatus.SUBMITTED
    assert order.shares == 0
    assert order.frozen_shares == 0
    assert order.requested_shares == 1_000

    t_bar = _bar(SYMBOL_A, datetime(2024, 1, 2, 15, 0))
    if order_kind == 'close':
        broker.execute_close_orders(
            {SYMBOL_A: t_bar}, match_at=t_bar.trade_time)
    else:
        broker.execute_market_orders(t_bar, match_at=t_bar.trade_time)
    assert order.status == OrderStatus.SUBMITTED
    assert order.shares == 0
    assert order.frozen_shares == 0
    broker.on_day_end()
    assert order.status == OrderStatus.SUBMITTED

    broker.on_day_start(T1)
    broker.set_market_phase('trading')
    broker.reset_bar_used_amount()
    t1_bar = _bar(SYMBOL_A, datetime(2024, 1, 3, 15, 0))
    before_shares = account.get_position(SYMBOL_A).shares
    if order_kind == 'close':
        broker.execute_close_orders(
            {SYMBOL_A: t1_bar}, match_at=t1_bar.trade_time)
    else:
        broker.execute_market_orders(t1_bar, match_at=t1_bar.trade_time)
    remaining = account.get_position(SYMBOL_A)
    after_shares = remaining.shares if remaining is not None else 0

    assert order.status == OrderStatus.FILLED
    assert order.filled_shares == before_shares - after_shares == 1_000


def test_competing_future_sells_reserve_in_submission_order_without_oversell():
    account = _account()
    _buy_position(account, SYMBOL_A, settle=False)
    broker = _broker(account)
    broker.set_market_phase('trading')

    with _on_day_scope(broker):
        first = broker.sell_at_market(SYMBOL_A, percent=1.0)
        second = broker.sell_at_market(SYMBOL_A, percent=1.0)

    assert first.status == second.status == OrderStatus.SUBMITTED
    assert first.frozen_shares == second.frozen_shares == 0

    broker.on_day_end()
    broker.on_day_start(T1)
    broker.set_market_phase('trading')
    before_shares = account.get_position(SYMBOL_A).shares
    broker.execute_market_orders(
        _bar(SYMBOL_A, datetime(2024, 1, 3, 15, 0)))
    remaining = account.get_position(SYMBOL_A)
    after_shares = remaining.shares if remaining is not None else 0

    assert first.status == OrderStatus.FILLED
    assert second.status == OrderStatus.REJECTED
    assert first.filled_shares == before_shares - after_shares == 1_000
    assert second.filled_shares == 0
    assert first.filled_shares + second.filled_shares <= before_shares


def test_partial_deferred_sell_reserves_old_inventory_before_later_orders():
    account = _account()
    account.freeze_cash(1_000.0)
    account.buy(SYMBOL_A, 100, 10.0, '20240101')
    account.settle_t1(T)
    account.freeze_cash(1_000.0)
    account.buy(SYMBOL_A, 100, 10.0, T)
    account.unfreeze_cash(account.frozen_cash)
    broker = _broker(account)
    broker.set_market_phase('trading')

    with _on_day_scope(broker):
        first = broker.sell_at_market(SYMBOL_A, shares=150)
        second = broker.sell_at_market(SYMBOL_A, shares=100)

    position = account.get_position(SYMBOL_A)
    assert first.status == second.status == OrderStatus.SUBMITTED
    assert (first.shares, first.frozen_shares, first.requested_shares) == (
        100, 100, 150)
    assert (second.shares, second.frozen_shares, second.requested_shares) == (
        0, 0, 100)
    assert position.frozen_shares == 100
    assert position.available_shares == 0

    broker.on_day_end()
    broker.on_day_start(T1)
    broker.set_market_phase('trading')
    broker.execute_market_orders(
        _bar(SYMBOL_A, datetime(2024, 1, 3, 15, 0)))

    assert first.status == OrderStatus.FILLED
    assert second.status == OrderStatus.FILLED
    assert first.filled_shares == 150
    assert second.filled_shares == 50
    assert second.auto_resized is True
    assert second.resized_from_shares == 100
    assert second.resized_to_shares == 50
    assert second.resize_reason == 'deferred_inventory_shortfall'
    assert first.filled_shares + second.filled_shares == 200
    assert account.get_position(SYMBOL_A) is None


def test_cancel_partial_deferred_sell_releases_submission_reservation():
    account = _account()
    account.freeze_cash(1_000.0)
    account.buy(SYMBOL_A, 100, 10.0, '20240101')
    account.settle_t1(T)
    account.freeze_cash(1_000.0)
    account.buy(SYMBOL_A, 100, 10.0, T)
    account.unfreeze_cash(account.frozen_cash)
    broker = _broker(account)
    broker.set_market_phase('trading')

    with _on_day_scope(broker):
        order = broker.sell_at_market(SYMBOL_A, shares=150)

    assert order.frozen_shares == 100
    assert account.get_position(SYMBOL_A).frozen_shares == 100

    assert broker.cancel_order(order.order_id) is True

    assert order.status == OrderStatus.CANCELLED
    assert order.frozen_shares == 0
    assert account.get_position(SYMBOL_A).frozen_shares == 0
