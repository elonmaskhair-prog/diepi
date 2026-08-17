"""Pure synthetic regressions for numeric/account safety boundaries."""

from datetime import datetime
from math import isfinite

import pandas as pd
import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import BarData, Broker
from diepi.backtest.broker.order import Order, OrderSide, OrderStatus, OrderType


SYMBOL = '000001.SZ'


def _bar(**overrides) -> BarData:
    values = {
        'symbol': SYMBOL,
        'trade_time': datetime(2024, 1, 3, 9, 31),
        'open': 10.0,
        'high': 10.5,
        'low': 9.5,
        'close': 10.2,
        'vol': 1_000_000.0,
        'amount': 10_000_000.0,
        'pre_close': 10.0,
    }
    values.update(overrides)
    return BarData(**values)


def _broker_with_position(shares: int = 1_000) -> Broker:
    account = Account(initial_cash=100_000.0)
    account.freeze_cash(20_000.0)
    account.buy(SYMBOL, shares, 10.0, '20240101')
    account.unfreeze_cash(account.frozen_cash)
    account.settle_t1('20240102')
    broker = Broker(account=account, slippage=0.0,
                    liquidity_cap_ratio=1.0)
    broker.set_date('20240103')
    broker.set_market_phase('trading')
    return broker


def _assert_account_money_finite(account: Account) -> None:
    assert isfinite(account.cash)
    assert isfinite(account.frozen_cash)
    assert isfinite(account.total_asset)


@pytest.mark.parametrize('kwargs', [
    {'slippage': float('nan')},
    {'slippage': float('inf')},
    {'slippage': float('-inf')},
    {'slippage': -0.001},
    {'slippage': 1.0},
    {'lot_size': 0},
    {'lot_size': -1},
    {'lot_size': 1.5},
    {'lot_size': float('inf')},
    {'allow_odd_lot_sell': 1},
    {'liquidity_cap_ratio': float('nan')},
    {'liquidity_cap_ratio': float('inf')},
    {'liquidity_cap_ratio': -0.01},
    {'liquidity_cap_ratio': 1.01},
    {'limit_pct_overrides': {SYMBOL: float('nan')}},
    {'limit_pct_overrides': {SYMBOL: float('inf')}},
    {'limit_pct_overrides': {SYMBOL: 0.0}},
    {'limit_pct_overrides': {SYMBOL: -0.1}},
    {'limit_pct_overrides': {SYMBOL: 1.01}},
    {'limit_pct_overrides': {'': 0.1}},
])
def test_broker_constructor_rejects_invalid_numeric_config(kwargs):
    with pytest.raises(ValueError):
        Broker(**kwargs)


def test_broker_constructor_preserves_legal_zero_and_endpoint_sentinels():
    broker = Broker(slippage=0, liquidity_cap_ratio=0,
                    limit_pct_overrides={SYMBOL: 1})

    assert broker.slippage == 0.0
    assert broker.liquidity_cap_ratio == 0.0
    assert broker.limit_pct_overrides[SYMBOL] == 1.0


@pytest.mark.parametrize(('field', 'value'), [
    *[(field, bad)
      for field in ('open', 'high', 'low', 'close')
      for bad in (float('nan'), float('inf'), float('-inf'), 0.0, -1.0)],
    *[(field, bad)
      for field in ('vol', 'amount', 'pre_close')
      for bad in (float('nan'), float('inf'), float('-inf'), -1.0)],
])
def test_bar_rejects_nonfinite_or_out_of_range_market_data(field, value):
    with pytest.raises(ValueError):
        _bar(**{field: value})


@pytest.mark.parametrize('trade_time', [None, '', 'not-a-date', pd.NaT, 0])
def test_bar_rejects_missing_or_unparseable_trade_time(trade_time):
    with pytest.raises(ValueError):
        _bar(trade_time=trade_time)


@pytest.mark.parametrize('overrides', [
    {'high': 10.1, 'close': 10.2},
    {'low': 10.1, 'open': 10.0},
])
def test_bar_rejects_impossible_ohlc_geometry(overrides):
    with pytest.raises(ValueError):
        _bar(**overrides)


def test_bar_preserves_zero_liquidity_and_missing_preclose_sentinels():
    bar = _bar(vol=0, amount=0, pre_close=0,
               trade_time='20240103')

    assert isinstance(bar.trade_time, datetime)
    assert (bar.vol, bar.amount, bar.pre_close) == (0.0, 0.0, 0.0)


@pytest.mark.parametrize('call', [
    lambda broker: broker.buy_at_market(
        SYMBOL, shares=float('nan'), current_price=10.0),
    lambda broker: broker.buy_at_market(
        SYMBOL, shares=float('inf'), current_price=10.0),
    lambda broker: broker.buy_at_market(
        SYMBOL, shares=-100, current_price=10.0),
    lambda broker: broker.buy_at_market(
        SYMBOL, shares=0, current_price=10.0),
    lambda broker: broker.buy_at_market(
        SYMBOL, amount=float('-inf'), current_price=10.0),
    lambda broker: broker.buy_at_market(
        SYMBOL, amount=-1.0, current_price=10.0),
    lambda broker: broker.buy_at_market(
        SYMBOL, percent=0.0, current_price=10.0),
    lambda broker: broker.buy_at_market(
        SYMBOL, percent=1.01, current_price=10.0),
    lambda broker: broker.buy_at_market(
        SYMBOL, shares=100, current_price=float('nan')),
    lambda broker: broker.buy_at_price(
        SYMBOL, price=float('inf'), shares=100),
    # Every supplied parameter is validated even when a higher-priority
    # sizing parameter would otherwise win.
    lambda broker: broker.buy_at_market(
        SYMBOL, shares=100, amount=float('nan'), current_price=10.0),
])
def test_invalid_buy_intents_reject_without_touching_account(call):
    account = Account(initial_cash=100_000.0)
    broker = Broker(account=account)
    broker.set_date('20240103')
    broker.set_market_phase('trading')
    before = (account.cash, account.frozen_cash, dict(account.positions))

    order = call(broker)

    assert order.status == OrderStatus.REJECTED
    assert order.shares == 0
    assert order.price is None
    assert order.amount is None
    assert order.requested_shares is None
    assert order.requested_amount is None
    assert order.requested_percent is None
    assert (account.cash, account.frozen_cash, account.positions) == before
    _assert_account_money_finite(account)


@pytest.mark.parametrize('call', [
    lambda broker: broker.sell_at_market(SYMBOL, shares=float('nan')),
    lambda broker: broker.sell_at_market(SYMBOL, shares=float('inf')),
    lambda broker: broker.sell_at_market(SYMBOL, shares=-1),
    lambda broker: broker.sell_at_market(SYMBOL, shares=0),
    lambda broker: broker.sell_at_market(SYMBOL, percent=float('-inf')),
    lambda broker: broker.sell_at_market(SYMBOL, percent=0),
    lambda broker: broker.sell_at_market(SYMBOL, percent=1.01),
    lambda broker: broker.sell_at_price(
        SYMBOL, price=float('nan'), shares=100),
    lambda broker: broker.sell_at_price(SYMBOL, price=0, shares=100),
    lambda broker: broker.sell_at_market(
        SYMBOL, shares=100, percent=float('nan')),
])
def test_invalid_sell_intents_never_become_full_position_sells(call):
    broker = _broker_with_position()
    account = broker.account
    position = account.get_position(SYMBOL)
    before = (position.shares, position.frozen_shares,
              account.cash, account.frozen_cash)

    order = call(broker)

    assert order.status == OrderStatus.REJECTED
    assert order.shares == 0
    assert order.frozen_shares == 0
    assert (position.shares, position.frozen_shares,
            account.cash, account.frozen_cash) == before
    _assert_account_money_finite(account)


def test_omitted_ordinary_sell_sizing_retains_explicit_legacy_sell_all_sentinel():
    broker = _broker_with_position()
    position = broker.account.get_position(SYMBOL)

    order = broker.sell_at_market(SYMBOL)

    assert order.status == OrderStatus.SUBMITTED
    assert order.shares == 1_000
    assert order.frozen_shares == 1_000
    assert position.frozen_shares == 1_000


def test_second_conditional_sell_cannot_overfreeze_or_create_phantom_fill():
    broker = _broker_with_position()
    position = broker.account.get_position(SYMBOL)

    stop_loss = broker.sell_stop_loss(SYMBOL, price=9.0, percent=1.0)
    stop_profit = broker.sell_stop_profit(SYMBOL, price=11.0, shares=100)

    assert stop_loss.status == OrderStatus.SUBMITTED
    assert stop_loss.frozen_shares == 1_000
    assert stop_profit.status == OrderStatus.REJECTED
    assert stop_profit.frozen_shares == 0
    assert position.frozen_shares == position.shares == 1_000
    assert sum(order.frozen_shares for order in broker.get_open_orders()) <= position.shares

    before_shares = position.shares
    broker.execute_market_orders(_bar(
        open=10.0, high=10.5, low=9.0, close=9.5, pre_close=10.0))
    remaining = broker.account.get_position(SYMBOL)
    after_shares = remaining.shares if remaining is not None else 0

    assert stop_loss.status == OrderStatus.FILLED
    assert stop_loss.filled_shares == before_shares - after_shares == 1_000
    assert stop_profit.filled_shares == 0
    _assert_account_money_finite(broker.account)


def test_order_zero_state_sentinels_remain_valid_but_explicit_sizing_does_not():
    order = Order(symbol=SYMBOL, side=OrderSide.BUY,
                  order_type=OrderType.OPEN)

    assert order.shares == 0
    assert order.filled_shares == 0
    assert order.filled_amount == 0.0
    assert order.avg_price == 0.0
    assert order.frozen_amount == 0.0
    assert order.frozen_shares == 0

    with pytest.raises(ValueError):
        Order(symbol=SYMBOL, side=OrderSide.BUY,
              order_type=OrderType.MARKET, requested_shares=0)
    with pytest.raises(ValueError):
        Order(symbol=SYMBOL, side=OrderSide.BUY,
              order_type=OrderType.MARKET, requested_amount=0)
    with pytest.raises(ValueError):
        Order(symbol=SYMBOL, side=OrderSide.BUY,
              order_type=OrderType.MARKET, requested_percent=0)


@pytest.mark.parametrize(('shares', 'price'), [
    (0, 10.0),
    (-1, 10.0),
    (float('nan'), 10.0),
    (1, 0.0),
    (1, -1.0),
    (1, float('inf')),
    (101, 10.0),
])
def test_invalid_order_fill_is_atomic(shares, price):
    order = Order(symbol=SYMBOL, side=OrderSide.BUY,
                  order_type=OrderType.MARKET, shares=100).submit()
    before = (order.status, order.filled_shares, order.filled_amount,
              order.avg_price, order.update_time)

    with pytest.raises(ValueError):
        order.fill(shares, price)

    assert (order.status, order.filled_shares, order.filled_amount,
            order.avg_price, order.update_time) == before


def test_order_fill_rejects_accumulated_notional_overflow_atomically():
    order = Order(symbol=SYMBOL, side=OrderSide.BUY,
                  order_type=OrderType.MARKET, shares=2).submit()
    order.fill(1, 1e308)
    before = (order.status, order.filled_shares, order.filled_amount,
              order.avg_price, order.update_time)

    with pytest.raises(ValueError):
        order.fill(1, 1e308)

    assert (order.status, order.filled_shares, order.filled_amount,
            order.avg_price, order.update_time) == before


def test_execute_order_rejects_invalid_fill_price_before_account_mutation():
    account = Account(initial_cash=100_000.0)
    broker = Broker(account=account)
    broker.set_date('20240103')
    broker.set_market_phase('trading')
    order = broker.buy_at_market(SYMBOL, shares=100, current_price=10.0)
    before = (account.cash, account.frozen_cash, order.status,
              order.filled_shares, order.filled_amount)

    with pytest.raises(ValueError):
        broker._execute_order(order, float('nan'), _bar())

    assert (account.cash, account.frozen_cash, order.status,
            order.filled_shares, order.filled_amount) == before
    _assert_account_money_finite(account)
