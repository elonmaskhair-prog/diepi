# -*- coding: utf-8 -*-
"""C0-01 event-causality tests backed only by in-memory bars.

These tests intentionally exercise the engines' private event loops.  They are
the first executable contract for the v1 rule that a decision may only match a
strictly later execution window.  No parquet repository, calendar file, or
index provider is consulted.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, Iterable, Optional

import pandas as pd
import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import BarData as BrokerBarData
from diepi.backtest.broker.broker import Broker
from diepi.backtest.broker.events import ExecutionEventType
from diepi.backtest.broker.order import OrderStatus
from diepi.backtest.broker.position import Position
from diepi.backtest.data.stock_pool import PoolSource
from diepi.backtest.engine.backtest_engine import BacktestEngine
from diepi.backtest.engine.context import Context
from diepi.backtest.engine.portfolio_context import PortfolioContext
from diepi.backtest.engine.portfolio_engine import PortfolioEngine
from diepi.backtest.liquidity import AuctionCapSpec, DailyAuctionLiquidityPolicy
from diepi.backtest.session_calendar import TimestampOutsideSessionError
from diepi.backtest.strategy.base import Strategy
from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy


T = "20240102"
T1 = "20240103"
SYMBOL_A = "000001.SZ"
SYMBOL_B = "600000.SH"


DAILY_AUCTION_POLICY = DailyAuctionLiquidityPolicy(
    open_cap=AuctionCapSpec.fixed_yuan(1_000_000_000.0),
    close_cap=AuctionCapSpec.fixed_yuan(1_000_000_000.0),
)


class SyntheticDataProvider:
    """Small DataProvider-shaped object for event-loop tests."""

    price_mode = "hfq"
    execution_price_mode = "hfq"

    def __init__(
        self,
        daily: Dict[str, pd.DataFrame],
        minute: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> None:
        self._daily = {symbol: frame.copy() for symbol, frame in daily.items()}
        self._minute = {
            symbol: frame.copy() for symbol, frame in (minute or {}).items()
        }
        self._context = None

    def set_context(self, context) -> None:
        self._context = context

    def set_price_modes(self, strategy=None, execution=None) -> None:
        if strategy is not None:
            self.price_mode = strategy
        if execution is not None:
            self.execution_price_mode = execution

    @property
    def _trade_days(self):
        days = {
            stamp.strftime("%Y%m%d")
            for frame in self._daily.values()
            for stamp in pd.DatetimeIndex(frame.index)
        }
        return sorted(days)

    def get_trade_days_between(self, start: str, end: str):
        return [day for day in self._trade_days if start <= day <= end]

    def get_prev_trade_day(self, date: str, n: int = 1):
        prior = [day for day in self._trade_days if day < date]
        return prior[-n] if len(prior) >= n else None

    def get_next_trade_day(self, date: str, n: int = 1):
        later = [day for day in self._trade_days if day > date]
        return later[n - 1] if len(later) >= n else None

    def get_adj_ratio(self, symbol: str, date: str) -> float:
        return 1.0

    def get_stock_info(self, symbol=None):
        return pd.DataFrame()

    def get_daily(
        self,
        symbol: str,
        start: str = None,
        end: str = None,
        fields=None,
        price_mode: str = None,
    ) -> pd.DataFrame:
        frame = self._daily.get(symbol)
        if frame is None:
            return pd.DataFrame()
        result = frame
        if start is not None:
            result = result[result.index >= pd.to_datetime(start)]
        if end is not None:
            result = result[result.index <= pd.to_datetime(end)]
        if fields is not None:
            result = result[list(fields)]
        return result.copy()

    def get_minute(
        self,
        symbol: str,
        trade_date: str = None,
        price_mode: str = None,
    ) -> pd.DataFrame:
        frame = self._minute.get(symbol)
        if frame is None:
            return pd.DataFrame()
        result = frame
        if trade_date is not None:
            result = result[result.index.strftime("%Y%m%d") == trade_date]
        return result.copy()


class _RawSplitDataProvider(SyntheticDataProvider):
    execution_price_mode = "raw"

    def get_adj_ratio(self, symbol: str, date: str) -> float:
        return 2.0 if date == T1 else 1.0


def _daily_frame(scale: float = 1.0) -> pd.DataFrame:
    """T prices are deliberately above T+1 prices to avoid sizing noise."""

    return pd.DataFrame(
        {
            "open": [20.0 * scale, 19.0 * scale],
            "high": [20.8 * scale, 19.4 * scale],
            "low": [19.5 * scale, 18.5 * scale],
            "close": [20.5 * scale, 19.1 * scale],
            "pre_close": [20.0 * scale, 20.5 * scale],
            "vol": [10_000_000.0, 10_000_000.0],
            # Daily source unit is thousand yuan; the engine converts it to yuan.
            "amount": [100_000.0, 100_000.0],
        },
        index=pd.to_datetime([T, T1]),
    )


def _minute_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [10.0, 10.6, 10.0],
            "high": [10.1, 10.9, 10.2],
            "low": [9.9, 10.5, 9.8],
            "close": [10.0, 10.8, 10.1],
            "pre_close": [10.0, 10.0, 10.0],
            "vol": [1_000_000.0, 1_000_000.0, 1_000_000.0],
            # Minute source unit is yuan.
            "amount": [1_000_000.0, 1_000_000.0, 1_000_000.0],
        },
        index=pd.to_datetime(
            ["2024-01-02 09:30:00", "2024-01-02 14:56:00", "2024-01-02 15:00:00"]
        ),
    )


def _account() -> Account:
    return Account(
        initial_cash=1_000_000.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )


def _broker(account: Account) -> Broker:
    return Broker(
        account=account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
    )


def _wire_single_engine(
    provider: SyntheticDataProvider,
    strategy: Strategy,
    *,
    freq: str,
) -> BacktestEngine:
    engine = BacktestEngine(
        symbol=SYMBOL_A,
        start_date=T,
        end_date=T1 if freq == "daily" else T,
        initial_cash=1_000_000.0,
        freq=freq,
        slippage=0.0,
        commission_rate=0.0,
        stamp_duty=0.0,
        min_commission=0.0,
        daily_auction_liquidity=DAILY_AUCTION_POLICY,
    )
    broker = _broker(_account())
    context = Context(broker=broker, data_provider=provider)
    context.set_symbol(SYMBOL_A)

    engine._data_provider = provider
    engine._context = context
    engine._strategy = strategy
    engine._daily_values = []
    strategy.set_context(context)
    strategy.on_init(context)
    return engine


def _wire_portfolio_engine(
    provider: SyntheticDataProvider,
    strategy: PortfolioStrategy,
    *,
    freq: str = "daily",
) -> PortfolioEngine:
    symbols = [SYMBOL_A, SYMBOL_B]
    engine = PortfolioEngine(
        start_date=T,
        end_date=T1,
        initial_cash=1_000_000.0,
        freq=freq,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=symbols,
        benchmark="",
        slippage=0.0,
        commission_rate=0.0,
        stamp_duty=0.0,
        min_commission=0.0,
        daily_auction_liquidity=DAILY_AUCTION_POLICY,
    )
    broker = _broker(_account())
    context = PortfolioContext(
        broker=broker,
        data_provider=provider,
        initial_cash=1_000_000.0,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=symbols,
    )

    engine._data_provider = provider
    engine._context = context
    engine._strategy = strategy
    engine._daily_values = []
    engine._position_history = []
    engine._daily_data_cache = {
        symbol: provider.get_daily(symbol) for symbol in symbols
    }
    engine._daily_data_raw_cache = {
        symbol: provider.get_daily(symbol) for symbol in symbols
    }
    engine._date_index_cache = {
        symbol: engine._build_date_index(frame)
        for symbol, frame in engine._daily_data_cache.items()
    }
    engine._date_index_raw_cache = {
        symbol: engine._build_date_index(frame)
        for symbol, frame in engine._daily_data_raw_cache.items()
    }
    strategy.set_context(context)
    strategy.on_init(context)
    return engine


def _snapshot(order):
    return order.filled_shares, order.status


def _created_attributes(broker: Broker, order) -> dict:
    created = [
        event for event in broker.event_journal.snapshot()
        if event.event_type is ExecutionEventType.ORDER_CREATED
        and event.payload.order_id == order.order_id
    ]
    assert len(created) == 1
    return dict(created[0].payload.attributes.entries)


def _submit_buy(ctx, order_kind: str, symbol: str, current_price: float):
    if order_kind == "market":
        # Keep the sizing/frozen-cash estimate above every synthetic execution
        # price.  Otherwise an old same-bar matcher can remain unfilled merely
        # because high > close, producing a false green for the causality test.
        return ctx.buy_at_market(
            symbol, shares=100, current_price=max(current_price, 100.0)
        )
    if order_kind == "close":
        return ctx.buy_at_close(symbol, shares=100, current_price=current_price)
    if order_kind == "limit":
        return ctx.buy_at_price(symbol, price=20.0, shares=100)
    if order_kind == "stop":
        return ctx.buy_stop(
            symbol, price=20.6, shares=100, current_price=current_price
        )
    raise AssertionError(f"unsupported test order kind: {order_kind}")


def test_broker_honors_future_eligibility_through_day_end():
    """A DAY order created after T's execution window belongs to T+1's DAY."""

    broker = _broker(_account())
    broker.set_date(T)
    broker.set_market_phase("trading")
    created_at = datetime(2024, 1, 2, 15, 0)
    eligible_from = datetime(2024, 1, 3)
    with broker.order_creation_scope(
        "on_day",
        created_at,
        eligible_from=eligible_from,
        expire_date=T1,
    ):
        order = broker.buy_at_market(
            SYMBOL_A, shares=100, current_price=100.0
        )

    assert order.created_phase == "on_day"
    assert order.sim_created_at == created_at
    assert order.eligible_from == eligible_from
    assert order.expire_date == T1

    t_bar = BrokerBarData(
        symbol=SYMBOL_A,
        trade_time=datetime(2024, 1, 2, 15, 0),
        open=20.0,
        high=20.8,
        low=19.5,
        close=20.5,
        vol=10_000_000.0,
        amount=100_000_000.0,
        pre_close=20.0,
    )
    t1_bar = BrokerBarData(
        symbol=SYMBOL_A,
        trade_time=datetime(2024, 1, 3, 15, 0),
        open=19.0,
        high=19.4,
        low=18.5,
        close=19.1,
        vol=10_000_000.0,
        amount=100_000_000.0,
        pre_close=20.5,
    )

    broker.execute_market_orders(t_bar)
    after_t_match = _snapshot(order)
    broker.on_day_end()
    after_t_end = _snapshot(order)

    broker.on_day_start(T1)
    broker.set_market_phase("trading")
    broker.reset_bar_used_amount()
    broker.execute_market_orders(t1_bar)

    assert after_t_match == (0, OrderStatus.SUBMITTED)
    assert after_t_end == (0, OrderStatus.SUBMITTED)
    assert order.status == OrderStatus.FILLED
    assert order.filled_shares == 100
    assert order.avg_price == pytest.approx(19.4)


def _raw_split_context():
    provider = _RawSplitDataProvider({SYMBOL_A: _daily_frame()})
    account = _account()
    broker = _broker(account)
    context = Context(broker=broker, data_provider=provider)
    context.set_symbol(SYMBOL_A)
    context.set_datetime(datetime(2024, 1, 2, 15, 0))
    context.set_market_phase("trading")
    return context, broker, account


def _future_order_scope(broker: Broker):
    return broker.order_creation_scope(
        "on_day",
        datetime(2024, 1, 2, 15, 0),
        eligible_from=datetime(2024, 1, 3),
        expire_date=T1,
    )


def test_raw_split_cancels_overnight_sell_before_adjusting_frozen_shares():
    context, broker, account = _raw_split_context()
    position = Position(
        symbol=SYMBOL_A,
        shares=100,
        cost_basis=1_000.0,
        avg_cost=10.0,
        current_price=10.0,
    )
    account.positions[SYMBOL_A] = position
    with _future_order_scope(broker):
        order = broker.sell_at_market(SYMBOL_A, shares=100)

    assert position.frozen_shares == 100
    assert order.frozen_shares == 100
    market_value_before = position.market_value
    total_asset_before = account.total_asset

    context.set_datetime(datetime(2024, 1, 3, 9, 0))
    context._on_day_start()

    assert order.status == OrderStatus.CANCELLED
    assert position.shares == 200
    assert position.frozen_shares == 0
    assert position.current_price == pytest.approx(5.0)
    assert position.market_value == pytest.approx(market_value_before)
    assert account.total_asset == pytest.approx(total_asset_before)
    assert order.frozen_shares == 0
    assert account.order_history.count(order) == 1
    assert order.order_id not in account.orders


def test_raw_split_cancels_unheld_overnight_buy_and_releases_cash_once():
    context, broker, account = _raw_split_context()
    with _future_order_scope(broker):
        order = broker.buy_at_price(SYMBOL_A, price=10.0, shares=100)

    assert account.get_position(SYMBOL_A) is None
    assert order.frozen_amount > 0
    assert account.frozen_cash == pytest.approx(order.frozen_amount)

    context.set_datetime(datetime(2024, 1, 3, 9, 0))
    context._on_day_start()

    assert order.status == OrderStatus.CANCELLED
    assert account.cash == pytest.approx(account.initial_cash)
    assert account.frozen_cash == pytest.approx(0.0)
    assert account.order_history.count(order) == 1
    assert order.order_id not in account.orders


def test_same_source_skip_adjust_keeps_overnight_order_on_ratio_change():
    context, broker, account = _raw_split_context()
    context.set_same_source_symbols({SYMBOL_A}, skip_adjust={SYMBOL_A})
    with _future_order_scope(broker):
        order = broker.buy_at_price(SYMBOL_A, price=10.0, shares=100)
    frozen_before = order.frozen_amount

    context.set_datetime(datetime(2024, 1, 3, 9, 0))
    context._on_day_start()

    assert order.status == OrderStatus.SUBMITTED
    assert account.frozen_cash == pytest.approx(frozen_before)
    assert account.order_history.count(order) == 0
    broker.cancel_all_orders()


class _SingleDailyOrder(Strategy):
    def __init__(self, order_kind: str):
        super().__init__()
        self.order_kind = order_kind
        self.order = None
        self.snapshots = {}

    def on_before_market_open(self, ctx):
        if self.order is not None:
            self.snapshots[("before_open", ctx.current_date)] = _snapshot(self.order)

    def on_day(self, ctx, bar):
        if self.order is None:
            self.order = _submit_buy(
                ctx, self.order_kind, bar.symbol, float(bar.close)
            )

    def on_after_market_close(self, ctx):
        if self.order is not None:
            self.snapshots[("after_close", ctx.current_date)] = _snapshot(self.order)


@pytest.mark.parametrize(
    ("order_kind", "expected_price"),
    [
        ("market", 19.4),
        ("close", 19.1),
        ("limit", 19.0),
        ("stop", 20.6),
    ],
)
def test_single_daily_on_day_order_first_fills_on_t_plus_one(
    order_kind: str,
    expected_price: float,
):
    daily = _daily_frame()
    if order_kind == "stop":
        # The trigger is reachable on both days.  It would fill at 20.6 on T if
        # eligibility were ignored, and therefore cannot pass for sizing reasons.
        daily.loc[pd.to_datetime(T1), "high"] = 20.7
    provider = SyntheticDataProvider({SYMBOL_A: daily})
    strategy = _SingleDailyOrder(order_kind)
    engine = _wire_single_engine(provider, strategy, freq="daily")

    engine._run_day(T)
    engine._run_day(T1)

    assert strategy.snapshots[("after_close", T)] == (
        0,
        OrderStatus.SUBMITTED,
    )
    assert strategy.snapshots[("before_open", T1)] == (
        0,
        OrderStatus.SUBMITTED,
    )
    assert strategy.snapshots[("after_close", T1)] == (
        100,
        OrderStatus.FILLED,
    )
    assert strategy.order.avg_price == pytest.approx(expected_price)


class _PortfolioDailyOrders(PortfolioStrategy):
    def __init__(self, order_kind: str):
        super().__init__()
        self.order_kind = order_kind
        self.orders = {}
        self.snapshots = {}

    def on_before_market_open(self, ctx):
        if self.orders:
            self.snapshots[("before_open", ctx.current_date)] = {
                symbol: _snapshot(order) for symbol, order in self.orders.items()
            }
        return None

    def on_day(self, ctx, bars):
        if not self.orders:
            for symbol in bars.symbols():
                self.orders[symbol] = _submit_buy(
                    ctx,
                    self.order_kind,
                    symbol,
                    float(bars.get(symbol).close),
                )

    def on_after_market_close(self, ctx):
        if self.orders:
            self.snapshots[("after_close", ctx.current_date)] = {
                symbol: _snapshot(order) for symbol, order in self.orders.items()
            }


@pytest.mark.parametrize(
    ("order_kind", "expected_prices"),
    [
        ("market", {SYMBOL_A: 19.4, SYMBOL_B: 38.8}),
        ("close", {SYMBOL_A: 19.1, SYMBOL_B: 38.2}),
    ],
)
def test_portfolio_daily_on_day_orders_first_fill_on_t_plus_one(
    order_kind: str,
    expected_prices: Dict[str, float],
):
    provider = SyntheticDataProvider(
        {SYMBOL_A: _daily_frame(), SYMBOL_B: _daily_frame(scale=2.0)}
    )
    strategy = _PortfolioDailyOrders(order_kind)
    engine = _wire_portfolio_engine(provider, strategy)

    engine._run_day(T)
    engine._run_day(T1)

    assert strategy.snapshots[("after_close", T)] == {
        SYMBOL_A: (0, OrderStatus.SUBMITTED),
        SYMBOL_B: (0, OrderStatus.SUBMITTED),
    }
    assert strategy.snapshots[("before_open", T1)] == {
        SYMBOL_A: (0, OrderStatus.SUBMITTED),
        SYMBOL_B: (0, OrderStatus.SUBMITTED),
    }
    assert strategy.snapshots[("after_close", T1)] == {
        SYMBOL_A: (100, OrderStatus.FILLED),
        SYMBOL_B: (100, OrderStatus.FILLED),
    }
    for symbol, expected_price in expected_prices.items():
        assert strategy.orders[symbol].avg_price == pytest.approx(expected_price)


class _PortfolioPendingOutsideSelection(PortfolioStrategy):
    def __init__(self):
        super().__init__()
        self.order = None
        self.active_symbols = {}

    def on_before_market_open(self, ctx):
        # Deliberately exclude B on both days.  Its pending order, not strategy
        # selection, must keep it in the execution universe on T+1.
        return [SYMBOL_A]

    def on_day(self, ctx, bars):
        self.active_symbols[ctx.current_date] = set(bars.symbols())
        if self.order is None:
            self.order = ctx.buy_at_market(
                SYMBOL_B, shares=100, current_price=100.0
            )


def test_portfolio_pending_symbol_is_loaded_outside_next_day_selection():
    provider = SyntheticDataProvider(
        {SYMBOL_A: _daily_frame(), SYMBOL_B: _daily_frame(scale=2.0)}
    )
    strategy = _PortfolioPendingOutsideSelection()
    engine = _wire_portfolio_engine(provider, strategy)

    engine._run_day(T)
    engine._run_day(T1)

    assert strategy.active_symbols[T] == {SYMBOL_A}
    assert strategy.active_symbols[T1] == {SYMBOL_A, SYMBOL_B}
    assert strategy.order.status == OrderStatus.FILLED
    assert strategy.order.avg_price == pytest.approx(38.8)


def test_portfolio_pending_symbol_missing_t_plus_one_bar_cancels_and_unfreezes():
    provider = SyntheticDataProvider(
        {
            SYMBOL_A: _daily_frame(),
            SYMBOL_B: _daily_frame(scale=2.0).iloc[[0]],
        }
    )
    strategy = _PortfolioPendingOutsideSelection()
    engine = _wire_portfolio_engine(provider, strategy)

    engine._run_day(T)
    assert strategy.order.status == OrderStatus.SUBMITTED
    assert engine._context._broker.account.frozen_cash > 0

    engine._run_day(T1)

    account = engine._context._broker.account
    assert strategy.order.status == OrderStatus.CANCELLED
    assert strategy.order.filled_shares == 0
    assert account.frozen_cash == pytest.approx(0.0)
    assert account.cash == pytest.approx(account.initial_cash)
    assert account.get_position(SYMBOL_B) is None


class _LastDayOrder(Strategy):
    def __init__(self):
        super().__init__()
        self.order = None
        self.finish_state = None

    def on_day(self, ctx, bar):
        if ctx.current_date == T1:
            self.order = ctx.buy_at_market(
                bar.symbol, shares=100, current_price=100.0
            )

    def on_finish(self, ctx):
        account = ctx._broker.account
        self.finish_state = (
            self.order.status,
            account.cash,
            account.frozen_cash,
            account.get_position(SYMBOL_A),
        )


def test_single_engine_run_cancels_last_day_future_order_before_on_finish():
    provider = SyntheticDataProvider({SYMBOL_A: _daily_frame()})
    strategy = _LastDayOrder()
    engine = _wire_single_engine(provider, strategy, freq="daily")
    # Exercise the public run loop while retaining the in-memory wiring.
    engine._init_engine = lambda: None
    engine._create_result = lambda **_: None

    engine.run(strategy)

    assert strategy.order is not None
    assert strategy.finish_state == (
        OrderStatus.CANCELLED,
        pytest.approx(1_000_000.0),
        pytest.approx(0.0),
        None,
    )


class _MinuteOrderAt(Strategy):
    def __init__(self, order_kind: str, submit_time: str):
        super().__init__()
        self.order_kind = order_kind
        self.submit_time = submit_time
        self.order = None
        self.after_close_snapshot = None
        self.after_close_frozen_cash = None

    def on_minute(self, ctx, bar):
        if self.order is not None:
            return
        if pd.Timestamp(bar.trade_time).strftime("%H:%M") != self.submit_time:
            return
        # A deliberately conservative sizing mark keeps this test focused on
        # event eligibility rather than frozen-cash resizing.
        sizing_price = 20.0 if self.order_kind == "market" else float(bar.close)
        self.order = _submit_buy(
            ctx, self.order_kind, bar.symbol, sizing_price
        )

    def on_after_market_close(self, ctx):
        if self.order is not None:
            self.after_close_snapshot = _snapshot(self.order)
            self.after_close_frozen_cash = ctx._broker.account.frozen_cash


def _run_single_minute_order(order_kind: str, submit_time: str):
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider(
        {SYMBOL_A: daily}, {SYMBOL_A: _minute_frame()}
    )
    strategy = _MinuteOrderAt(order_kind, submit_time)
    engine = _wire_single_engine(provider, strategy, freq="minute")
    engine._run_day(T)
    return engine, strategy


class _PreOpenMinuteOrder(Strategy):
    def __init__(self, order_kind: str):
        super().__init__()
        self.order_kind = order_kind
        self.order = None
        self.first_bar_snapshot = None
        self.first_callback_time = None

    def on_before_market_open(self, ctx):
        if self.order_kind == "market":
            self.order = ctx.buy_at_market(
                SYMBOL_A, shares=100, current_price=20.0
            )
        elif self.order_kind == "limit":
            self.order = ctx.buy_at_price(SYMBOL_A, price=10.0, shares=100)
        elif self.order_kind == "stop":
            self.order = ctx.buy_stop(
                SYMBOL_A, price=10.05, shares=100, current_price=20.0
            )
        else:
            raise AssertionError(f"unsupported order kind: {self.order_kind}")

    def on_minute(self, ctx, bar):
        if self.first_bar_snapshot is None:
            self.first_bar_snapshot = _snapshot(self.order)
            self.first_callback_time = pd.Timestamp(bar.trade_time)


@pytest.mark.parametrize(
    ("order_kind", "expected_price"),
    [("market", 10.1), ("limit", 10.0), ("stop", 10.05)],
)
def test_single_minute_preopen_intraday_order_fills_before_first_callback(
    order_kind: str,
    expected_price: float,
):
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider(
        {SYMBOL_A: daily}, {SYMBOL_A: _minute_frame()}
    )
    strategy = _PreOpenMinuteOrder(order_kind)
    engine = _wire_single_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    assert strategy.first_bar_snapshot == (100, OrderStatus.FILLED)
    assert strategy.order.avg_price == pytest.approx(expected_price)
    assert strategy.order.created_phase == "before_open"
    assert strategy.order.sim_created_at == datetime(2024, 1, 2, 9, 0)
    assert strategy.order.eligible_from == datetime(2024, 1, 2)
    assert strategy.order.expire_date == T


def test_single_minute_rejects_out_of_session_row_before_matching():
    invalid = pd.DataFrame(
        {
            "open": [float("nan")],
            "high": [float("nan")],
            "low": [float("nan")],
            "close": [float("nan")],
            "pre_close": [10.0],
            "vol": [0.0],
            "amount": [0.0],
        },
        index=pd.to_datetime(["2024-01-02 09:29:00"]),
    )
    minute = pd.concat([invalid, _minute_frame()])
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider({SYMBOL_A: daily}, {SYMBOL_A: minute})
    strategy = _PreOpenMinuteOrder("market")
    engine = _wire_single_engine(provider, strategy, freq="minute")

    with pytest.raises(TimestampOutsideSessionError, match="outside sessions"):
        engine._run_day(T)

    assert strategy.first_callback_time is None
    assert strategy.first_bar_snapshot is None
    assert strategy.order.filled_shares == 0


class _PortfolioMinuteRemainder(PortfolioStrategy):
    def __init__(self):
        super().__init__()
        self.order = None
        self.after_close_snapshot = None
        self.after_close_frozen_cash = None

    def on_before_market_open(self, ctx):
        return [SYMBOL_A]

    def on_minute(self, ctx, bars):
        if self.order is None:
            # Legal but untouched: pre_close=10 gives a 9.00 lower boundary,
            # while every synthetic minute low remains above it.
            self.order = ctx.buy_at_price(SYMBOL_A, price=9.0, shares=100)

    def on_after_market_close(self, ctx):
        self.after_close_snapshot = _snapshot(self.order)
        self.after_close_frozen_cash = ctx._broker.account.frozen_cash


def test_portfolio_minute_remainder_is_cancelled_before_after_close():
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider(
        {SYMBOL_A: daily}, {SYMBOL_A: _minute_frame()}
    )
    strategy = _PortfolioMinuteRemainder()
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    assert strategy.after_close_snapshot == (0, OrderStatus.CANCELLED)
    assert strategy.after_close_frozen_cash == pytest.approx(0.0)


class _PortfolioPreOpenMinuteOrder(PortfolioStrategy):
    def __init__(self):
        super().__init__()
        self.order = None
        self.first_bar_snapshot = None

    def on_before_market_open(self, ctx):
        self.order = ctx.buy_at_market(
            SYMBOL_A, shares=100, current_price=20.0
        )
        return [SYMBOL_A]

    def on_minute(self, ctx, bars):
        if self.first_bar_snapshot is None:
            self.first_bar_snapshot = _snapshot(self.order)


def test_portfolio_minute_preopen_market_fills_before_first_callback():
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider(
        {SYMBOL_A: daily}, {SYMBOL_A: _minute_frame()}
    )
    strategy = _PortfolioPreOpenMinuteOrder()
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    assert strategy.first_bar_snapshot == (100, OrderStatus.FILLED)
    assert strategy.order.avg_price == pytest.approx(10.1)
    assert strategy.order.created_phase == "before_open"
    assert strategy.order.eligible_from == datetime(2024, 1, 2)
    assert strategy.order.expire_date == T


class _PortfolioBeforeCloseOrders(PortfolioStrategy):
    def __init__(self):
        super().__init__()
        self.closing_symbol_order = None
        self.deferred_symbol_order = None

    def on_before_market_open(self, ctx):
        return [SYMBOL_A]

    def on_before_close(self, ctx):
        self.closing_symbol_order = ctx.schedule_at_close(
            "buy", SYMBOL_A, shares=100, current_price=10.0)
        self.deferred_symbol_order = ctx.schedule_at_close(
            "buy", SYMBOL_B, shares=100, current_price=10.0)


def test_portfolio_minute_close_window_is_stamped_only_for_closing_symbols():
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider(
        {SYMBOL_A: daily}, {SYMBOL_A: _minute_frame()}
    )
    strategy = _PortfolioBeforeCloseOrders()
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    closing = strategy.closing_symbol_order
    deferred = strategy.deferred_symbol_order
    assert closing.status is OrderStatus.FILLED
    assert closing.eligible_from == datetime(2024, 1, 2, 15, 0)
    assert closing.expire_date == T
    assert deferred.status is OrderStatus.SUBMITTED
    assert deferred.eligible_from == datetime(2024, 1, 3)
    assert deferred.expire_date == T1

    closing_attrs = _created_attributes(engine._context._broker, closing)
    deferred_attrs = _created_attributes(engine._context._broker, deferred)
    assert closing_attrs["eligible_from"] == "2024-01-02T15:00:00.000000"
    assert closing_attrs["expire_date"] == T
    assert deferred_attrs["eligible_from"] == "2024-01-03T00:00:00.000000"
    assert deferred_attrs["expire_date"] == T1


class _PortfolioMinuteOrderAt(PortfolioStrategy):
    def __init__(self, order_kind: str, submit_time: str):
        super().__init__()
        self.order_kind = order_kind
        self.submit_time = submit_time
        self.order = None
        self.after_close_snapshot = None
        self.after_close_frozen_cash = None

    def on_before_market_open(self, ctx):
        return [SYMBOL_A]

    def on_minute(self, ctx, bars):
        bar = bars.get(SYMBOL_A)
        if bar is None or self.order is not None:
            return
        if pd.Timestamp(bar.trade_time).strftime("%H:%M") != self.submit_time:
            return
        self.order = _submit_buy(
            ctx, self.order_kind, SYMBOL_A, current_price=20.0
        )

    def on_after_market_close(self, ctx):
        if self.order is not None:
            self.after_close_snapshot = _snapshot(self.order)
            self.after_close_frozen_cash = ctx._broker.account.frozen_cash


@pytest.mark.parametrize(
    ("order_kind", "expected_price"),
    [("market", 10.9), ("close", 10.1)],
)
def test_portfolio_minute_last_normal_callback_fills_in_close_auction(
    order_kind: str,
    expected_price: float,
):
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider(
        {SYMBOL_A: daily}, {SYMBOL_A: _minute_frame()}
    )
    strategy = _PortfolioMinuteOrderAt(order_kind, "09:30")
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    assert strategy.after_close_snapshot == (100, OrderStatus.FILLED)
    assert strategy.order.avg_price == pytest.approx(expected_price)
    assert strategy.order.created_phase == "on_minute"
    assert strategy.order.sim_created_at == datetime(2024, 1, 2, 9, 30)
    assert strategy.order.eligible_from == datetime(
        2024, 1, 2, 9, 30, 0, 1
    )
    assert strategy.order.expire_date == T


@pytest.mark.parametrize("order_kind", ["market", "close"])
def test_portfolio_minute_last_callback_without_auction_is_not_backfilled(
    order_kind: str,
):
    minute = _minute_frame().iloc[:2].copy()
    minute.index = pd.to_datetime(
        ["2024-01-02 09:30:00", "2024-01-02 09:31:00"]
    )
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider({SYMBOL_A: daily}, {SYMBOL_A: minute})
    strategy = _PortfolioMinuteOrderAt(order_kind, "09:31")
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    assert strategy.after_close_snapshot == (0, OrderStatus.CANCELLED)
    assert strategy.after_close_frozen_cash == pytest.approx(0.0)
    assert strategy.order.created_phase == "on_minute"
    assert strategy.order.eligible_from == datetime(
        2024, 1, 2, 9, 31, 0, 1
    )


@pytest.mark.parametrize("order_kind", ["market", "close"])
def test_portfolio_1456_is_continuous_callback_not_implicit_auction(order_kind):
    minute = _minute_frame().iloc[:2].copy()
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider({SYMBOL_A: daily}, {SYMBOL_A: minute})
    strategy = _PortfolioMinuteOrderAt(order_kind, "14:56")
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    assert strategy.order is not None
    assert strategy.after_close_snapshot == (0, OrderStatus.CANCELLED)
    assert strategy.after_close_frozen_cash == pytest.approx(0.0)


@pytest.mark.parametrize(
    ("order_kind", "expected_price"),
    [("market", 10.2), ("close", 10.1)],
)
def test_portfolio_1456_order_uses_distinct_1500_auction(
        order_kind, expected_price):
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider(
        {SYMBOL_A: daily}, {SYMBOL_A: _minute_frame()}
    )
    strategy = _PortfolioMinuteOrderAt(order_kind, "14:56")
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    assert strategy.after_close_snapshot == (100, OrderStatus.FILLED)
    assert strategy.order.avg_price == pytest.approx(expected_price)


class _DualTrackMinuteProvider(SyntheticDataProvider):
    """Synthetic provider whose strategy/execution minute lanes can diverge."""

    def __init__(
        self,
        daily: Dict[str, pd.DataFrame],
        execution_minute: Dict[str, pd.DataFrame],
        strategy_minute: Dict[str, pd.DataFrame],
    ) -> None:
        super().__init__(daily, execution_minute)
        self._strategy_minute = {
            symbol: frame.copy() for symbol, frame in strategy_minute.items()
        }

    def get_minute(
        self,
        symbol: str,
        trade_date: str = None,
        price_mode: str = None,
    ) -> pd.DataFrame:
        source = self._minute if price_mode == "execution" else self._strategy_minute
        frame = source.get(symbol)
        if frame is None:
            return pd.DataFrame()
        result = frame
        if trade_date is not None:
            result = result[result.index.strftime("%Y%m%d") == trade_date]
        return result.copy()


def _two_normal_minute_bars(scale: float = 1.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [10.0 * scale, 10.5 * scale],
            "high": [10.2 * scale, 10.7 * scale],
            "low": [9.8 * scale, 10.3 * scale],
            "close": [10.1 * scale, 10.6 * scale],
            "pre_close": [10.0 * scale, 10.0 * scale],
            "vol": [1_000_000.0, 1_000_000.0],
            "amount": [1_000_000.0, 1_000_000.0],
        },
        index=pd.to_datetime(
            ["2024-01-02 09:30:00", "2024-01-02 09:31:00"]
        ),
    )


class _ObservePortfolioMinuteBars(PortfolioStrategy):
    def __init__(self):
        super().__init__()
        self.seen = []

    def on_before_market_open(self, ctx):
        return [SYMBOL_A]

    def on_minute(self, ctx, bars):
        bar = bars.get(SYMBOL_A)
        if bar is not None:
            self.seen.append((pd.Timestamp(bar.trade_time), float(bar.close)))


def test_portfolio_minute_strategy_lane_is_looked_up_by_timestamp():
    raw = _two_normal_minute_bars()
    strategy_lane = raw.copy()
    strategy_lane.loc[strategy_lane.index[0], ["open", "high", "low", "close"]] = (
        [100.0, 102.0, 98.0, 101.0]
    )
    strategy_lane.loc[strategy_lane.index[1], ["open", "high", "low", "close"]] = (
        [200.0, 202.0, 198.0, 201.0]
    )
    # A positional lookup would now expose 09:31's value at 09:30.
    strategy_lane = strategy_lane.iloc[::-1]
    daily = _daily_frame().iloc[[0]].copy()
    provider = _DualTrackMinuteProvider(
        {SYMBOL_A: daily},
        {SYMBOL_A: raw},
        {SYMBOL_A: strategy_lane},
    )
    strategy = _ObservePortfolioMinuteBars()
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    assert strategy.seen == [
        (pd.Timestamp("2024-01-02 09:30:00"), 101.0),
        (pd.Timestamp("2024-01-02 09:31:00"), 201.0),
    ]


def test_portfolio_minute_strategy_lane_timestamp_mismatch_fails_before_callback():
    raw = _two_normal_minute_bars()
    # The sole strategy row is from the future relative to raw's first window.
    strategy_lane = raw.iloc[[1]].copy()
    strategy_lane.loc[:, ["open", "high", "low", "close"]] = (
        [900.0, 902.0, 898.0, 901.0]
    )
    daily = _daily_frame().iloc[[0]].copy()
    provider = _DualTrackMinuteProvider(
        {SYMBOL_A: daily},
        {SYMBOL_A: raw},
        {SYMBOL_A: strategy_lane},
    )
    strategy = _ObservePortfolioMinuteBars()
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    with pytest.raises(ValueError, match="minute timestamp mismatch"):
        engine._run_day(T)

    assert strategy.seen == []


@pytest.mark.parametrize(
    ("execution_minute", "strategy_minute", "expected_message"),
    [
        ({}, {SYMBOL_A: _two_normal_minute_bars()}, "execution minute data missing"),
        ({SYMBOL_A: _two_normal_minute_bars()}, {}, "strategy minute data missing"),
    ],
)
def test_portfolio_minute_missing_lane_fails_without_daily_downgrade(
    execution_minute, strategy_minute, expected_message
):
    daily = _daily_frame().iloc[[0]].copy()
    provider = _DualTrackMinuteProvider(
        {SYMBOL_A: daily}, execution_minute, strategy_minute
    )
    strategy = _ObservePortfolioMinuteBars()
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    with pytest.raises(ValueError, match=expected_message):
        engine._run_day(T)

    assert strategy.seen == []


def test_portfolio_minute_duplicate_timestamp_fails_before_callback():
    raw = _two_normal_minute_bars()
    raw.index = pd.to_datetime(
        ["2024-01-02 09:30:00", "2024-01-02 09:30:00"]
    )
    daily = _daily_frame().iloc[[0]].copy()
    provider = _DualTrackMinuteProvider(
        {SYMBOL_A: daily},
        {SYMBOL_A: raw},
        {SYMBOL_A: raw.copy()},
    )
    strategy = _ObservePortfolioMinuteBars()
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    with pytest.raises(RuntimeError, match="execution minute data read failed") as caught:
        engine._run_day(T)

    assert "duplicate execution minute timestamp" in str(caught.value.__cause__)
    assert strategy.seen == []


class _StaggeredPortfolioOpen(PortfolioStrategy):
    def __init__(self):
        super().__init__()
        self.order = None
        self.first_callback_snapshot = None
        self.first_symbol_b_snapshot = None

    def on_before_market_open(self, ctx):
        self.order = ctx.buy_at_open(SYMBOL_B, shares=100)
        return [SYMBOL_A, SYMBOL_B]

    def on_minute(self, ctx, bars):
        if self.first_callback_snapshot is None:
            self.first_callback_snapshot = _snapshot(self.order)
        if bars.get(SYMBOL_B) is not None and self.first_symbol_b_snapshot is None:
            self.first_symbol_b_snapshot = _snapshot(self.order)


def test_portfolio_open_requires_exact_0930_bar_for_each_symbol():
    bars_a = _two_normal_minute_bars().iloc[[0]].copy()
    bars_b = _two_normal_minute_bars(scale=2.0).iloc[[1]].copy()
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider(
        {SYMBOL_A: daily, SYMBOL_B: daily * 2.0},
        {SYMBOL_A: bars_a, SYMBOL_B: bars_b},
    )
    strategy = _StaggeredPortfolioOpen()
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    assert strategy.first_callback_snapshot == (0, OrderStatus.SUBMITTED)
    assert strategy.first_symbol_b_snapshot == (0, OrderStatus.SUBMITTED)
    assert strategy.order.status == OrderStatus.CANCELLED
    assert strategy.order.filled_shares == 0
    assert strategy.order.reject_reason == (
        "Open auction cancelled: no market data")


def test_portfolio_close_without_auction_is_cancelled_and_releases_cash():
    minute = _two_normal_minute_bars()
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider({SYMBOL_A: daily}, {SYMBOL_A: minute})
    strategy = _PortfolioMinuteOrderAt("close", "09:30")
    engine = _wire_portfolio_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    account = engine._context._broker.account
    assert strategy.after_close_snapshot == (0, OrderStatus.CANCELLED)
    assert strategy.after_close_frozen_cash == pytest.approx(0.0)
    assert strategy.order.frozen_amount == pytest.approx(0.0)
    assert account.get_position(SYMBOL_A) is None
    assert account.order_history.count(strategy.order) == 1


class _PreOpenSharedLiquidity(Strategy):
    def __init__(self):
        super().__init__()
        self.open_order = None
        self.market_order = None
        self.first_bar_snapshot = None

    def on_before_market_open(self, ctx):
        self.open_order = ctx.buy_at_open(SYMBOL_A, shares=100)
        self.market_order = ctx.buy_at_market(
            SYMBOL_A, shares=100, current_price=20.0
        )

    def on_minute(self, ctx, bar):
        if self.first_bar_snapshot is None:
            self.first_bar_snapshot = (
                _snapshot(self.open_order),
                _snapshot(self.market_order),
            )


def test_single_minute_open_has_priority_and_shares_first_bar_liquidity_cap():
    minute = _minute_frame()
    minute.loc[minute.index[0], "amount"] = 1_500.0
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider({SYMBOL_A: daily}, {SYMBOL_A: minute})
    strategy = _PreOpenSharedLiquidity()
    engine = _wire_single_engine(provider, strategy, freq="minute")

    engine._run_day(T)

    # At 10 yuan, the first bar can fill only one 100-share lot.  OPEN consumes
    # it before ordinary intraday orders; MARKET must wait for the next bar.
    assert strategy.first_bar_snapshot == (
        (100, OrderStatus.FILLED),
        (0, OrderStatus.SUBMITTED),
    )
    assert strategy.market_order.status == OrderStatus.FILLED
    assert strategy.market_order.avg_price == pytest.approx(10.9)


@pytest.mark.parametrize("order_kind", ["market", "close"])
def test_single_minute_closing_auction_is_not_exposed_to_callback(
        order_kind: str):
    engine, strategy = _run_single_minute_order(order_kind, "15:00")

    assert strategy.order is None
    assert strategy.after_close_snapshot is None
    assert strategy.after_close_frozen_cash is None
    assert not [
        order
        for order in engine._context._broker.account.order_history
        if order.filled_shares > 0
    ]


@pytest.mark.parametrize(
    ("order_kind", "expected_price"),
    [("market", 10.2), ("close", 10.1)],
)
def test_single_minute_last_continuous_order_fills_in_close_auction(
    order_kind: str,
    expected_price: float,
):
    engine, strategy = _run_single_minute_order(order_kind, "14:56")

    assert strategy.order is not None
    assert strategy.order.status == OrderStatus.FILLED
    assert strategy.order.filled_shares == 100
    assert strategy.order.avg_price == pytest.approx(expected_price)
    assert len(
        [
            order
            for order in engine._context._broker.account.order_history
            if order.filled_shares > 0
        ]
    ) == 1
