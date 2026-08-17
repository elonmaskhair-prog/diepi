"""Synthetic contract tests for the pessimistic bar execution model."""

from datetime import datetime

import pytest

from diepi.backtest.broker import (
    CashAuditBundle,
    CashReplaySeed,
    cash_replay_trade_records,
    replay_cash_journal,
)
from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import BarData, Broker
from diepi.backtest.broker.order import OrderStatus
from diepi.backtest.broker.settlement import SettlementInvariantError
from diepi.backtest.engine.backtest_engine import BacktestEngine
from diepi.backtest.engine.portfolio_engine import PortfolioEngine


SYMBOL = "000001.SZ"
ODD_LOT_SYMBOL = "688001.SH"
TRADE_DATE = "20240103"


def _account() -> Account:
    return Account(
        initial_cash=1_000_000.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )


def _broker(**kwargs) -> Broker:
    broker = Broker(
        account=_account(),
        liquidity_cap_ratio=1.0,
        **kwargs,
    )
    broker.set_date(TRADE_DATE)
    broker.set_market_phase("trading")
    return broker


def _bar(*, open=10.0, high=11.0, low=9.0, close=10.5) -> BarData:
    return BarData(
        symbol=SYMBOL,
        trade_time=datetime(2024, 1, 3, 15, 0),
        open=open,
        high=high,
        low=low,
        close=close,
        vol=1_000_000.0,
        amount=20_000_000.0,
        pre_close=10.0,
    )


def _flat_bar(symbol: str, price: float = 10.0, *, amount=20_000_000.0):
    return BarData(
        symbol=symbol,
        trade_time=datetime(2024, 1, 3, 15, 0),
        open=price,
        high=price,
        low=price,
        close=price,
        vol=1_000_000.0,
        amount=amount,
        pre_close=price,
    )


def _seed_sellable_position(broker: Broker, shares=1_000) -> None:
    broker.account.freeze_cash(shares * 10.0)
    broker.account.buy(SYMBOL, shares, 10.0, "20240102")
    broker.account.settle_t1(TRADE_DATE)


def _seed_odd_lot_sell_broker(shares: int) -> Broker:
    account = Account(
        initial_cash=100.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=5.0,
    )
    account.buy(
        ODD_LOT_SYMBOL,
        shares,
        1.0,
        "20240102",
        strict_settlement=False,
    )
    account.settle_t1(TRADE_DATE)
    broker = Broker(
        account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
        commission_scope="fill",
    )
    broker.set_date(TRADE_DATE)
    broker.set_market_phase("trading")
    return broker


def _odd_lot_bar(price: float, *, amount: float = 100.0) -> BarData:
    return BarData(
        symbol=ODD_LOT_SYMBOL,
        trade_time=datetime(2024, 1, 3, 15, 0),
        open=price,
        high=price,
        low=price,
        close=price,
        vol=1_000.0,
        amount=amount,
        pre_close=price,
    )


def test_default_model_is_close_lock():
    assert _broker().bar_liquidity_model == "close_lock"


def test_engine_defaults_expose_close_lock_contract():
    single = BacktestEngine(SYMBOL, "20240101", "20240131")
    portfolio = PortfolioEngine("20240101", "20240131")

    assert single.bar_liquidity_model == "close_lock"
    assert portfolio.bar_liquidity_model == "close_lock"


@pytest.mark.parametrize(
    "engine",
    [
        BacktestEngine(SYMBOL, "20240101", "20240131", bar_liquidity_model="any_touch"),
        PortfolioEngine("20240101", "20240131", bar_liquidity_model="any_touch"),
    ],
)
def test_engine_forwards_explicit_bar_liquidity_model(engine):
    engine._init_engine()

    assert engine._context._broker.bar_liquidity_model == "any_touch"


@pytest.mark.parametrize("bad", [None, "", "one_price", "close", 1])
def test_unknown_bar_liquidity_model_fails_fast(bad):
    with pytest.raises(ValueError, match="bar_liquidity_model"):
        _broker(bar_liquidity_model=bad)


def test_close_lock_allows_buy_after_intrabar_limit_touch_if_close_unlocked():
    broker = _broker(slippage=0.0)
    order = broker.buy_at_market(SYMBOL, shares=100, current_price=12.0)

    broker.execute_market_orders(_bar(high=11.0, close=10.8))

    assert order.status == OrderStatus.FILLED
    assert order.avg_price == pytest.approx(11.0)


def test_close_lock_defers_buy_when_close_is_limit_up_and_retries_later_bar():
    broker = _broker(slippage=0.0)
    order = broker.buy_at_market(SYMBOL, shares=100, current_price=12.0)

    broker.execute_market_orders(_bar(high=11.0, close=11.0))

    assert order.status == OrderStatus.SUBMITTED
    assert "model=close_lock" in order.reject_reason
    assert broker.account.frozen_cash > 0.0

    broker.execute_market_orders(_bar(high=10.9, close=10.8))

    assert order.status == OrderStatus.FILLED
    assert order.reject_reason is None
    assert broker.account.frozen_cash == pytest.approx(0.0)


def test_open_order_does_not_use_future_daily_close_as_liquidity_veto():
    broker = _broker(slippage=0.0)
    broker.set_market_phase("before_open")
    order = broker.buy_at_open(SYMBOL, shares=100)

    broker.execute_open_orders({SYMBOL: _bar(open=10.0, high=11.0, close=11.0)})

    assert order.status == OrderStatus.FILLED
    assert order.avg_price == pytest.approx(10.0)


def test_open_order_uses_only_open_auction_price_for_lock_veto():
    broker = _broker(slippage=0.0)
    broker.set_market_phase("before_open")
    order = broker.buy_at_open(SYMBOL, shares=100)

    broker.execute_open_orders({SYMBOL: _bar(open=11.0, high=11.0, close=10.8)})

    assert order.status == OrderStatus.CANCELLED
    assert "Limit-up blocked buy" in order.reject_reason
    assert order.frozen_amount == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("symbol", "minimum", "cash"),
    [
        ("688001.SH", 200, 199.0),
        ("920002.BJ", 100, 99.0),
    ],
)
def test_open_cash_resize_below_minimum_lot_rejects_without_new_odd_order(
        symbol, minimum, cash):
    account = Account(
        initial_cash=cash,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    broker = Broker(
        account=account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.set_date(TRADE_DATE)
    broker.set_market_phase("before_open")
    order = broker.buy_at_open(symbol, shares=minimum)

    executed = broker.execute_open_orders(
        {symbol: _flat_bar(symbol, price=1.0)})

    assert executed == []
    assert order.status is OrderStatus.REJECTED
    assert "below minimum lot" in order.reject_reason
    assert order.requested_shares == minimum
    assert order.shares == 0
    assert order.filled_shares == 0
    assert order.auto_resized is False
    assert account.cash == pytest.approx(cash)
    assert account.frozen_cash == pytest.approx(0.0)
    assert account.get_position(symbol) is None


def test_open_auction_partial_fill_may_be_below_initial_minimum_lot():
    account = Account(
        initial_cash=1_000.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    broker = Broker(
        account=account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.set_date(TRADE_DATE)
    broker.set_market_phase("before_open")
    order = broker.buy_at_open("688001.SH", shares=200)

    executed = broker.execute_open_orders({
        "688001.SH": _flat_bar("688001.SH", price=1.0, amount=50.0),
    })

    assert executed == [order]
    assert order.status is OrderStatus.CANCELLED
    assert order.filled_shares == 50
    assert account.get_position("688001.SH").shares == 50


@pytest.mark.parametrize(
    ("symbol", "t0_overrides"),
    [
        ("511990.SH", None),
        (SYMBOL, {"000001"}),
    ],
)
def test_intraday_buy_sell_uses_rulebook_settlement_and_keeps_overrides(
        symbol, t0_overrides):
    broker = _broker(slippage=0.0, t0_overrides=t0_overrides)
    bar = _flat_bar(symbol)
    buy = broker.buy_at_market(symbol, shares=100, current_price=10.0)

    broker.execute_market_orders(bar)

    position = broker.account.get_position(symbol)
    assert buy.status is OrderStatus.FILLED
    assert position.available_shares == 100

    broker.reset_bar_used_amount()
    sell = broker.sell_at_market(symbol, shares=100)
    broker.execute_market_orders(bar)

    assert sell.status is OrderStatus.FILLED
    remaining = broker.account.get_position(symbol)
    assert remaining is None or remaining.shares == 0


def test_after_open_open_orders_wait_for_the_next_open_window():
    broker = _broker(slippage=0.0)
    _seed_sellable_position(broker)
    decision = datetime(2024, 1, 3, 9, 30)
    eligible = datetime(2024, 1, 4, 9, 30)
    with broker.order_creation_scope(
            "on_after_open", decision,
            eligible_from=eligible, expire_date="20240104"):
        sell = broker.sell_at_open(SYMBOL, shares=100)
        buy = broker.buy_at_open(SYMBOL, shares=100)

    assert sell.status == OrderStatus.SUBMITTED
    assert buy.status == OrderStatus.SUBMITTED
    assert sell.eligible_from == eligible
    assert buy.eligible_from == eligible

    current = broker.execute_open_orders(
        {SYMBOL: _bar(open=10.0)}, match_at=decision,
        reject_missing=False,
    )
    assert current == []
    assert sell.status == OrderStatus.SUBMITTED
    assert buy.status == OrderStatus.SUBMITTED

    broker.set_date("20240104")
    following = broker.execute_open_orders(
        {SYMBOL: _bar(open=10.0)}, match_at=eligible,
    )

    assert following == [sell, buy]
    assert sell.status == OrderStatus.FILLED
    assert buy.status == OrderStatus.FILLED


def test_close_lock_allows_sell_after_intrabar_limit_touch_if_close_unlocked():
    broker = _broker(slippage=0.0)
    _seed_sellable_position(broker)
    order = broker.sell_at_market(SYMBOL, shares=100)

    broker.execute_market_orders(_bar(low=9.0, close=9.2))

    assert order.status == OrderStatus.FILLED
    assert order.avg_price == pytest.approx(9.0)


def test_close_lock_defers_sell_when_close_is_limit_down_and_retries_later_bar():
    broker = _broker(slippage=0.0)
    _seed_sellable_position(broker)
    order = broker.sell_at_market(SYMBOL, shares=100)

    broker.execute_market_orders(_bar(low=9.0, close=9.0))

    assert order.status == OrderStatus.SUBMITTED
    assert "model=close_lock" in order.reject_reason
    assert order.frozen_shares == 100
    assert broker.account.get_position(SYMBOL).frozen_shares == 100

    broker.execute_market_orders(_bar(low=9.1, close=9.2))

    assert order.status == OrderStatus.FILLED
    assert order.reject_reason is None
    assert order.frozen_shares == 0
    assert broker.account.get_position(SYMBOL).frozen_shares == 0


@pytest.mark.parametrize(
    ("side", "bar"),
    [
        ("buy", _bar(high=11.0, close=10.8)),
        ("sell", _bar(low=9.0, close=9.2)),
    ],
)
def test_any_touch_pressure_mode_defers_intrabar_touch(side, bar):
    broker = _broker(slippage=0.0, bar_liquidity_model="any_touch")
    if side == "buy":
        order = broker.buy_at_market(SYMBOL, shares=100, current_price=12.0)
    else:
        _seed_sellable_position(broker)
        order = broker.sell_at_market(SYMBOL, shares=100)

    broker.execute_market_orders(bar)

    assert order.status == OrderStatus.SUBMITTED
    assert "model=any_touch" in order.reject_reason
    if side == "buy":
        assert broker.account.frozen_cash > 0.0
    else:
        assert order.frozen_shares == 100
        assert broker.account.get_position(SYMBOL).frozen_shares == 100


def test_directional_veto_does_not_block_sell_on_limit_up_close():
    broker = _broker(slippage=0.0)
    _seed_sellable_position(broker)
    order = broker.sell_at_market(SYMBOL, shares=100)

    broker.execute_market_orders(_bar(high=11.0, low=10.0, close=11.0))

    assert order.status == OrderStatus.FILLED


def test_directional_veto_does_not_block_buy_on_limit_down_close():
    broker = _broker(slippage=0.0)
    order = broker.buy_at_market(SYMBOL, shares=100, current_price=12.0)

    broker.execute_market_orders(_bar(open=10.0, high=10.0, low=9.0, close=9.0))

    assert order.status == OrderStatus.FILLED


def test_worst_bar_buy_slippage_is_clamped_to_legal_limit_up():
    broker = _broker(slippage=0.01)
    order = broker.buy_at_market(SYMBOL, shares=100, current_price=12.0)

    broker.execute_market_orders(_bar(high=10.99, close=10.8))

    assert order.status == OrderStatus.FILLED
    assert order.avg_price == pytest.approx(11.0)


def test_modeled_buy_price_below_limit_down_is_an_invariant_error():
    broker = _broker(slippage=0.0)
    order = broker.buy_at_market(SYMBOL, shares=100, current_price=10.0)
    bar = _bar(open=10.0, high=10.1, low=9.9, close=10.0)

    with pytest.raises(SettlementInvariantError, match="modeled BUY"):
        broker._execute_order(order, 8.99, bar, ref_price=10.0)

    assert order.status is OrderStatus.SUBMITTED
    assert order.filled_shares == 0


def test_modeled_sell_price_above_limit_up_is_an_invariant_error():
    broker = _broker(slippage=0.0)
    _seed_sellable_position(broker)
    order = broker.sell_at_market(SYMBOL, shares=100)
    bar = _bar(open=10.0, high=10.1, low=9.9, close=10.0)

    with pytest.raises(SettlementInvariantError, match="modeled SELL"):
        broker._execute_order(order, 11.01, bar, ref_price=10.0)

    assert order.status is OrderStatus.SUBMITTED
    assert order.filled_shares == 0


@pytest.mark.parametrize(
    ("side", "price"),
    [("buy", 11.01), ("sell", 8.99)],
)
def test_explicit_limit_order_outside_band_is_rejected_not_redirected(
        side, price):
    broker = _broker(slippage=0.0)
    if side == "buy":
        order = broker.buy_at_price(SYMBOL, price=price, shares=100)
    else:
        _seed_sellable_position(broker)
        order = broker.sell_at_price(SYMBOL, price=price, shares=100)

    executed = broker.execute_market_orders(
        _bar(open=10.0, high=10.5, low=9.5, close=10.0))

    assert executed == []
    assert order.status is OrderStatus.REJECTED
    assert "outside legal daily price band" in order.reject_reason
    assert order.filled_shares == 0


@pytest.mark.parametrize("order_kind", ["market", "stop"])
def test_continuous_buy_reservation_covers_the_daily_price_upper_bound(
        order_kind):
    """A routine adverse move must not silently erode requested quantity."""

    broker = _broker(slippage=0.01)
    if order_kind == "market":
        order = broker.buy_at_market(
            SYMBOL, shares=1_000, current_price=10.0)
        bar = _bar(open=10.0, high=10.05, low=9.9, close=10.02)
    else:
        order = broker.buy_stop(
            SYMBOL, price=10.0, shares=1_000, current_price=10.0)
        bar = _bar(open=10.05, high=10.2, low=9.9, close=10.1)

    # The natural one-session upper bound is the 11.00 limit-up price.  The
    # former est_price * 1.01 reservation bought only 900 shares in both cases.
    assert order.frozen_amount == pytest.approx(11_000.0)

    broker.execute_market_orders(bar)

    assert order.status == OrderStatus.FILLED
    assert order.filled_shares == 1_000
    assert "frozen_insufficient" not in (order.resize_reason or "")


@pytest.mark.parametrize("order_kind", ["market", "stop"])
@pytest.mark.parametrize("budget_kind", ["amount", "percent"])
def test_budget_sized_continuous_buy_resizes_at_reservation_cash_limit(
        order_kind, budget_kind):
    """Budget requests shrink at the conservative bound instead of rejecting."""

    broker = _broker(slippage=0.0)
    kwargs = (
        {"amount": 910_000.0}
        if budget_kind == "amount"
        else {"percent": 0.91}
    )
    if order_kind == "market":
        order = broker.buy_at_market(
            SYMBOL, current_price=10.0, **kwargs)
    else:
        order = broker.buy_stop(
            SYMBOL, price=10.0, current_price=10.0, **kwargs)

    # The original 91,000-share request needs 1,001,000 at the 11.00
    # reservation price.  The largest exact A-share lot funded by 1,000,000
    # is 90,900 shares (999,900, with the zero-fee fixture).
    assert order.status is OrderStatus.SUBMITTED
    assert order.shares == 90_900
    assert order.requested_shares == 91_000
    if budget_kind == "amount":
        assert order.requested_amount == pytest.approx(910_000.0)
        assert order.requested_percent is None
    else:
        assert order.requested_amount is None
        assert order.requested_percent == pytest.approx(0.91)
    assert order.auto_resized is True
    assert order.resized_from_shares == 91_000
    assert order.resized_to_shares == 90_900
    assert order.resize_reason == "reservation_cash_limit"
    assert order.frozen_amount == pytest.approx(999_900.0)

    created = dict(broker.event_journal[0].payload.attributes.entries)
    assert created["requested_shares"] == 91_000
    assert created["auto_resized"] is True
    assert created["resized_from_shares"] == 91_000
    assert created["resized_to_shares"] == 90_900
    assert created["resize_reason"] == "reservation_cash_limit"

    bar = (
        _bar(open=10.0, high=11.0, low=9.9, close=10.8)
        if order_kind == "market"
        else _bar(open=11.0, high=11.0, low=9.9, close=10.8)
    )
    broker.execute_market_orders(bar)
    assert order.status is OrderStatus.FILLED
    assert order.filled_shares == 90_900
    assert order.avg_price == pytest.approx(11.0)
    assert "frozen_insufficient" not in (order.resize_reason or "")


@pytest.mark.parametrize("order_kind", ["market", "stop"])
@pytest.mark.parametrize("budget_kind", ["amount", "percent"])
def test_budget_sized_continuous_buy_below_cash_limit_keeps_requested_quantity(
        order_kind, budget_kind):
    broker = _broker(slippage=0.0)
    kwargs = (
        {"amount": 900_000.0}
        if budget_kind == "amount"
        else {"percent": 0.90}
    )
    if order_kind == "market":
        order = broker.buy_at_market(
            SYMBOL, current_price=10.0, **kwargs)
    else:
        order = broker.buy_stop(
            SYMBOL, price=10.0, current_price=10.0, **kwargs)

    assert order.status is OrderStatus.SUBMITTED
    assert order.shares == 90_000
    assert order.requested_shares == 90_000
    assert order.auto_resized is False
    assert order.resized_from_shares is None
    assert order.resized_to_shares is None
    assert order.resize_reason == ""
    assert order.frozen_amount == pytest.approx(990_000.0)


@pytest.mark.parametrize("order_kind", ["market", "stop"])
@pytest.mark.parametrize(
    ("symbol", "percent", "expected_shares", "expected_frozen"),
    [
        ("000001.SZ", 0.90, 9_000, 99_000.0),
        ("688001.SH", 0.83, 8_300, 99_600.0),
        ("920002.BJ", 0.76, 7_600, 98_800.0),
    ],
)
def test_board_specific_budget_below_reservation_limit_does_not_resize(
        order_kind, symbol, percent, expected_shares, expected_frozen):
    account = Account(
        initial_cash=100_000.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    broker = Broker(account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.set_date(TRADE_DATE)
    broker.set_market_phase("trading")

    if order_kind == "market":
        order = broker.buy_at_market(
            symbol, percent=percent, current_price=10.0)
    else:
        order = broker.buy_stop(
            symbol, price=10.0, percent=percent, current_price=10.0)

    assert order.status is OrderStatus.SUBMITTED
    assert order.shares == expected_shares
    assert order.auto_resized is False
    assert order.frozen_amount == pytest.approx(expected_frozen)


@pytest.mark.parametrize("order_kind", ["market", "stop"])
@pytest.mark.parametrize(
    ("symbol", "expected_shares", "expected_frozen"),
    [
        ("000001.SZ", 9_000, 99_000.0),
        ("688001.SH", 8_333, 99_996.0),
        ("920002.BJ", 7_692, 99_996.0),
    ],
)
def test_board_specific_full_budget_resizes_to_exact_legal_increment(
        order_kind, symbol, expected_shares, expected_frozen):
    account = Account(
        initial_cash=100_000.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    broker = Broker(account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.set_date(TRADE_DATE)
    broker.set_market_phase("trading")

    if order_kind == "market":
        order = broker.buy_at_market(
            symbol, amount=100_000.0, current_price=10.0)
    else:
        order = broker.buy_stop(
            symbol, price=10.0, amount=100_000.0, current_price=10.0)

    assert order.status is OrderStatus.SUBMITTED
    assert order.requested_shares == 10_000
    assert order.shares == expected_shares
    assert order.auto_resized is True
    assert order.resized_from_shares == 10_000
    assert order.resized_to_shares == expected_shares
    assert order.resize_reason == "reservation_cash_limit"
    assert order.frozen_amount == pytest.approx(expected_frozen)


@pytest.mark.parametrize("order_kind", ["market", "stop"])
@pytest.mark.parametrize(
    ("symbol", "cash", "requested_shares"),
    [
        ("000001.SZ", 1_099.0, 100),
        ("688001.SH", 2_399.0, 239),
        ("920002.BJ", 1_299.0, 129),
    ],
)
def test_reservation_resize_below_board_minimum_rejects_without_mutation(
        order_kind, symbol, cash, requested_shares):
    account = Account(
        initial_cash=cash,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    broker = Broker(account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.set_date(TRADE_DATE)
    broker.set_market_phase("trading")

    if order_kind == "market":
        order = broker.buy_at_market(
            symbol, percent=1.0, current_price=10.0)
    else:
        order = broker.buy_stop(
            symbol, price=10.0, percent=1.0, current_price=10.0)

    assert order.status is OrderStatus.REJECTED
    assert order.shares == requested_shares
    assert order.requested_shares == requested_shares
    assert order.auto_resized is False
    assert order.frozen_amount == 0.0
    assert account.cash == pytest.approx(cash)
    assert account.frozen_cash == 0.0


@pytest.mark.parametrize("budget_kind", ["amount", "percent"])
def test_sequential_budget_orders_use_remaining_cash_and_preserve_reservations(
        budget_kind):
    account = Account(
        initial_cash=1_000_000.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 9, 0))
    broker = Broker(account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.set_date(TRADE_DATE)
    broker.set_market_phase("trading")
    first_kwargs = (
        {"amount": 500_000.0}
        if budget_kind == "amount" else {"percent": 0.5}
    )
    second_kwargs = (
        {"amount": 500_000.0}
        if budget_kind == "amount" else {"percent": 1.0}
    )

    first = broker.buy_at_market(
        SYMBOL, current_price=10.0, **first_kwargs)
    second = broker.buy_stop(
        SYMBOL, price=10.0, current_price=10.0, **second_kwargs)

    assert (first.shares, first.frozen_amount, first.auto_resized) == (
        50_000, 550_000.0, False)
    assert (second.shares, second.frozen_amount, second.auto_resized) == (
        40_900, 449_900.0, True)
    assert account.cash == pytest.approx(100.0)
    assert account.frozen_cash == pytest.approx(999_900.0)
    replay_cash_journal(
        seed, broker.event_journal).state.assert_matches_account(account)


@pytest.mark.parametrize("order_kind", ["market", "stop"])
def test_explicit_share_continuous_buy_remains_all_or_reject(order_kind):
    broker = _broker(slippage=0.0)
    if order_kind == "market":
        order = broker.buy_at_market(
            SYMBOL, shares=100_000, current_price=10.0)
    else:
        order = broker.buy_stop(
            SYMBOL, price=10.0, shares=100_000, current_price=10.0)

    assert order.status is OrderStatus.REJECTED
    assert order.shares == 100_000
    assert order.requested_shares == 100_000
    assert order.auto_resized is False
    assert order.frozen_amount == 0.0
    assert "Insufficient cash" in order.reject_reason


def test_continuous_budget_resize_uses_exact_fee_schedule():
    account = Account(
        initial_cash=100_000.0,
        commission_rate=0.00031,
        stamp_duty_rate=0.0,
        min_commission=6.0,
    )
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 3, 9, 0))
    broker = Broker(
        account=account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
        transfer_fee_rate=0.00001,
    )
    broker.set_date(TRADE_DATE)
    broker.set_market_phase("trading")

    created_at = datetime(2024, 1, 3, 9, 31)
    match_at = datetime(2024, 1, 3, 9, 32)
    with broker.order_creation_scope(
            "on_minute", created_at,
            eligible_from=match_at, expire_date=TRADE_DATE):
        order = broker.buy_at_market(
            SYMBOL, percent=1.0, current_price=10.0)

    assert order.status is OrderStatus.SUBMITTED
    assert order.requested_shares == 10_000
    assert order.shares == 9_000
    assert order.auto_resized is True
    # 9,000 * 11 + 30.69 commission + 0.99 transfer fee.
    assert order.frozen_amount == pytest.approx(99_031.68)
    assert account.available_cash == pytest.approx(968.32)
    assert broker._estimate_buy_total_cost(
        9_100, 11.0, order=order) > 100_000.0

    broker.execute_market_orders(
        BarData(
            symbol=SYMBOL,
            trade_time=match_at,
            open=10.0,
            high=11.0,
            low=9.9,
            close=10.8,
            vol=1_000_000.0,
            amount=20_000_000.0,
            pre_close=10.0,
        ),
        match_at=match_at,
    )
    replayed = replay_cash_journal(seed, broker.event_journal)
    replayed.state.assert_matches_account(account)
    record = cash_replay_trade_records(
        CashAuditBundle.from_parts(seed, broker.event_journal))[0]
    assert record["requested_percent"] == pytest.approx(1.0)
    assert record["requested_shares"] == 10_000
    assert record["resized_from_shares"] == 10_000
    assert record["resized_to_shares"] == 9_000
    assert record["resize_reason"] == "reservation_cash_limit"


def test_on_init_buy_reservation_uses_snapshot_rules_without_a_current_date():
    broker = Broker(
        account=_account(), slippage=0.01, liquidity_cap_ratio=1.0)

    order = broker.buy_at_market(
        SYMBOL, shares=1_000, current_price=10.0)

    assert order.status == OrderStatus.SUBMITTED
    assert order.frozen_amount == pytest.approx(11_000.0)


def test_limit_buy_uses_its_limit_as_the_cash_reservation_upper_bound():
    broker = _broker(slippage=0.01)

    order = broker.buy_at_price(SYMBOL, price=10.0, shares=1_000)

    assert order.frozen_amount == pytest.approx(10_000.0)


def test_worst_bar_sell_slippage_is_clamped_to_legal_limit_down():
    broker = _broker(slippage=0.01)
    _seed_sellable_position(broker)
    order = broker.sell_at_market(SYMBOL, shares=100)

    broker.execute_market_orders(_bar(low=9.01, close=9.2))

    assert order.status == OrderStatus.FILLED
    assert order.avg_price == pytest.approx(9.0)


@pytest.mark.parametrize("resize_mode", ["auto", "legacy"])
@pytest.mark.parametrize("fill_mode", ["open+slip", "open"])
def test_open_sell_uses_raw_open_without_directional_slippage(
        resize_mode, fill_mode):
    broker = _broker(
        slippage=0.01,
        open_buy_resize_mode=resize_mode,
        open_buy_fill_mode=fill_mode,
    )
    _seed_sellable_position(broker)
    broker.set_market_phase("before_open")
    order = broker.sell_at_open(SYMBOL, shares=100)

    broker.execute_open_orders({
        SYMBOL: _bar(open=9.01, high=10.0, low=9.0, close=9.2),
    })

    assert order.status == OrderStatus.FILLED
    assert order.avg_price == pytest.approx(9.01)


def test_sell_with_nonpositive_net_proceeds_rejects_atomically():
    broker = _seed_odd_lot_sell_broker(1)
    account = broker.account
    cash_before = account.cash
    order = broker.sell_at_market(ODD_LOT_SYMBOL, shares=1)

    executed = broker.execute_market_orders(_odd_lot_bar(1.0))

    position = account.get_position(ODD_LOT_SYMBOL)
    assert executed == []
    assert order.status == OrderStatus.REJECTED
    assert order.filled_shares == 0
    assert order.frozen_shares == 0
    assert account.cash == pytest.approx(cash_before)
    assert position.shares == 1
    assert position.available_shares == 1
    assert position.frozen_shares == 0


def test_sell_with_nonpositive_residual_proceeds_cancels_after_partial_fill():
    broker = _seed_odd_lot_sell_broker(2)
    account = broker.account
    order = broker.sell_at_market(ODD_LOT_SYMBOL, shares=2)

    first = broker.execute_market_orders(
        _odd_lot_bar(10.0, amount=10.0))
    assert first == [order]
    assert order.status == OrderStatus.PARTIAL
    assert order.filled_shares == 1
    assert order.frozen_shares == 1
    cash_before_rejected_residual = account.cash

    second = broker.execute_market_orders(_odd_lot_bar(1.0))

    position = account.get_position(ODD_LOT_SYMBOL)
    assert second == []
    assert order.status == OrderStatus.CANCELLED
    assert order.filled_shares == 1
    assert order.frozen_shares == 0
    assert account.cash == pytest.approx(cash_before_rejected_residual)
    assert position.shares == 1
    assert position.available_shares == 1
    assert position.frozen_shares == 0


def test_limit_prices_use_decimal_round_half_up_not_binary_bankers_rounding():
    broker = _broker()

    assert broker._limit_up_price(SYMBOL, 10.05) == pytest.approx(11.06)
    assert broker._limit_down_price(SYMBOL, 10.05) == pytest.approx(9.05)


def test_limit_exemption_disables_veto_and_price_band_clamp():
    broker = _broker(
        slippage=0.01,
        limit_check_exempt={SYMBOL: {TRADE_DATE}},
    )
    order = broker.buy_at_market(SYMBOL, shares=100, current_price=12.0)

    broker.execute_market_orders(_bar(high=11.0, close=11.0))

    assert order.status == OrderStatus.FILLED
    assert order.avg_price == pytest.approx(11.11)
