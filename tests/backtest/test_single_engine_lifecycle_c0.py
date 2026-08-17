"""Pure synthetic C0 boundaries for the single-symbol engine."""

from datetime import datetime

import pandas as pd
import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import Broker
from diepi.backtest.broker.events import ExecutionEventType
from diepi.backtest.broker.order import OrderStatus
from diepi.backtest.engine.backtest_engine import BacktestEngine
from diepi.backtest.engine.context import Context
from diepi.backtest.engine.portfolio_context import PortfolioContext
from diepi.backtest.engine.portfolio_engine import PortfolioEngine
from diepi.backtest.liquidity import AuctionCapSpec, DailyAuctionLiquidityPolicy
from diepi.backtest.data.contract import (
    AmountUnit,
    DataContractError,
    Frequency,
    PriceSpace,
    validate_and_align_pair,
)
from diepi.backtest.data.stock_pool import PoolSource
from diepi.backtest.strategy.base import Strategy
from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy


T = '20240102'
SYMBOL = '000001.SZ'


def _created_attributes(broker, order):
    created = [
        event for event in broker.event_journal.snapshot()
        if event.event_type is ExecutionEventType.ORDER_CREATED
        and event.payload.order_id == order.order_id
    ]
    assert len(created) == 1
    return dict(created[0].payload.attributes.entries)


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            'open': [10.0],
            'high': [10.5],
            'low': [9.5],
            'close': [10.2],
            'pre_close': [10.0],
            'vol': [1_000_000.0],
            'amount': [10_000.0],
        },
        index=pd.to_datetime(['2024-01-02']),
    )


def _minute_frame(times=('09:30', '14:59')) -> pd.DataFrame:
    rows = len(times)
    return pd.DataFrame(
        {
            'open': [10.0 + i * 0.1 for i in range(rows)],
            'high': [10.2 + i * 0.1 for i in range(rows)],
            'low': [9.8 + i * 0.1 for i in range(rows)],
            'close': [10.1 + i * 0.1 for i in range(rows)],
            'pre_close': [10.0] * rows,
            'vol': [1_000_000.0] * rows,
            'amount': [10_000_000.0] * rows,
        },
        index=pd.to_datetime([f'2024-01-02 {value}:00' for value in times]),
    )


class _Provider:
    price_mode = 'hfq'
    execution_price_mode = 'hfq'

    def __init__(self, *, trade_days=None, daily=None,
                 strategy_daily=None, execution_daily=None,
                 strategy_minute=None, execution_minute=None):
        self.trade_days = list(trade_days or [])
        self.daily = daily.copy() if daily is not None else pd.DataFrame()
        self.strategy_daily = (
            strategy_daily.copy()
            if strategy_daily is not None else self.daily.copy()
        )
        self.execution_daily = (
            execution_daily.copy()
            if execution_daily is not None else self.daily.copy()
        )
        self.strategy_minute = (
            strategy_minute.copy()
            if strategy_minute is not None else pd.DataFrame()
        )
        self.execution_minute = (
            execution_minute.copy()
            if execution_minute is not None else pd.DataFrame()
        )
        self.context = None

    def set_context(self, context):
        self.context = context

    def set_price_modes(self, strategy=None, execution=None):
        if strategy is not None:
            self.price_mode = strategy
        if execution is not None:
            self.execution_price_mode = execution

    def get_trade_days_between(self, start, end):
        return list(self.trade_days)

    def get_daily(self, symbol=None, start=None, end=None,
                  price_mode=None, **kwargs):
        if price_mode == 'execution':
            return self.execution_daily.copy()
        if price_mode == 'strategy':
            return self.strategy_daily.copy()
        return self.daily.copy()

    def get_minute(self, symbol=None, trade_date=None,
                   price_mode=None, **kwargs):
        frame = (
            self.execution_minute
            if price_mode == 'execution' else self.strategy_minute
        )
        return frame.copy()

    def get_next_trade_day(self, date, n=1):
        return None


def _wire_engine(provider: _Provider, *, freq='daily', legacy=False,
                 daily_auction_liquidity=None):
    account = Account(
        initial_cash=100_000.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    broker = Broker(
        account=account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
        open_buy_resize_mode='legacy' if legacy else 'auto',
    )
    context = Context(broker=broker, data_provider=provider)
    context.set_symbol(SYMBOL)
    engine = BacktestEngine(
        symbol=SYMBOL,
        start_date=T,
        end_date=T,
        initial_cash=100_000.0,
        freq=freq,
        slippage=0.0,
        commission_rate=0.0,
        stamp_duty=0.0,
        min_commission=0.0,
        liquidity_cap_ratio=1.0,
        daily_auction_liquidity=daily_auction_liquidity,
    )
    engine._data_provider = provider
    engine._context = context
    engine._init_engine = lambda: None
    return engine, account


class _LifecycleStrategy(Strategy):
    def __init__(self, *, fail_init=False, fail_finish=False):
        super().__init__()
        self.fail_init = fail_init
        self.fail_finish = fail_finish
        self.order = None
        self.finish_calls = 0
        self.finish_state = None

    def on_init(self, ctx):
        self.order = ctx.buy_at_market(
            SYMBOL, shares=100, current_price=10.0)
        if self.fail_init:
            raise RuntimeError('init-primary')

    def on_finish(self, ctx):
        self.finish_calls += 1
        account = ctx._broker.account
        self.finish_state = (
            self.order.status,
            account.cash,
            account.frozen_cash,
        )
        if self.fail_finish:
            raise RuntimeError('finish-secondary')


def test_empty_calendar_still_cleans_orders_and_calls_finish_once():
    engine, account = _wire_engine(_Provider(trade_days=[]))
    strategy = _LifecycleStrategy()

    result = engine.run(strategy)

    assert result.final_value == result.initial_cash
    assert strategy.finish_calls == 1
    assert strategy.finish_state == (
        OrderStatus.CANCELLED,
        pytest.approx(account.initial_cash),
        pytest.approx(0.0),
    )
    assert account.get_open_orders() == []


def test_runtime_error_calls_finish_but_finish_error_never_masks_primary():
    provider = _Provider(trade_days=[T], daily=_daily_frame())
    engine, account = _wire_engine(provider)
    strategy = _LifecycleStrategy(fail_finish=True)

    def fail_day(*args, **kwargs):
        raise LookupError('run-primary')

    engine._run_day = fail_day

    with pytest.raises(LookupError, match='run-primary'):
        engine.run(strategy)

    assert strategy.finish_calls == 1
    assert strategy.order.status == OrderStatus.CANCELLED
    assert strategy.finish_state == (
        OrderStatus.CANCELLED,
        pytest.approx(account.initial_cash),
        pytest.approx(0.0),
    )
    assert account.frozen_cash == pytest.approx(0.0)
    assert account.cash == pytest.approx(account.initial_cash)


def test_on_init_error_cleans_orders_without_calling_finish():
    engine, account = _wire_engine(_Provider(trade_days=[]))
    strategy = _LifecycleStrategy(fail_init=True)

    with pytest.raises(RuntimeError, match='init-primary'):
        engine.run(strategy)

    assert strategy.finish_calls == 0
    assert strategy.order.status == OrderStatus.CANCELLED
    assert account.frozen_cash == pytest.approx(0.0)
    assert account.cash == pytest.approx(account.initial_cash)


def test_finish_error_propagates_when_no_primary_error_exists():
    engine, account = _wire_engine(_Provider(trade_days=[]))
    strategy = _LifecycleStrategy(fail_finish=True)

    with pytest.raises(RuntimeError, match='finish-secondary'):
        engine.run(strategy)

    assert strategy.finish_calls == 1
    assert strategy.order.status == OrderStatus.CANCELLED
    assert account.frozen_cash == pytest.approx(0.0)


def _wire_portfolio_run(provider: _Provider, *, stop_check=None):
    account = Account(
        initial_cash=100_000.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    broker = Broker(
        account=account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
    )
    context = PortfolioContext(
        broker=broker,
        data_provider=provider,
        initial_cash=100_000.0,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=[SYMBOL],
    )
    engine = PortfolioEngine(
        start_date=T,
        end_date=T,
        initial_cash=100_000.0,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=[SYMBOL],
        benchmark='',
        stop_check=stop_check,
        slippage=0.0,
        commission_rate=0.0,
        stamp_duty=0.0,
        min_commission=0.0,
        liquidity_cap_ratio=1.0,
    )
    engine._data_provider = provider
    engine._context = context
    engine._init_engine = lambda: None
    engine._preload_data = lambda pool: None
    return engine, account


class _PortfolioLifecycleStrategy(PortfolioStrategy):
    def __init__(self, *, fail_init=False, fail_finish=False):
        super().__init__()
        self.fail_init = fail_init
        self.fail_finish = fail_finish
        self.order = None
        self.finish_calls = 0
        self.finish_state = None

    def on_init(self, ctx):
        self.order = ctx.buy_at_market(
            SYMBOL, shares=100, current_price=10.0)
        if self.fail_init:
            raise RuntimeError('portfolio-init-primary')

    def on_finish(self, ctx):
        self.finish_calls += 1
        account = ctx._broker.account
        self.finish_state = (
            self.order.status,
            account.cash,
            account.frozen_cash,
        )
        if self.fail_finish:
            raise RuntimeError('portfolio-finish-secondary')


def test_portfolio_empty_calendar_cleans_and_finishes_once():
    engine, account = _wire_portfolio_run(_Provider(trade_days=[]))
    strategy = _PortfolioLifecycleStrategy()

    result = engine.run(strategy)

    assert result.final_value == result.initial_cash
    assert strategy.finish_calls == 1
    assert strategy.finish_state == (
        OrderStatus.CANCELLED,
        pytest.approx(account.initial_cash),
        pytest.approx(0.0),
    )


def test_portfolio_runtime_error_keeps_primary_and_still_finalizes():
    provider = _Provider(trade_days=[T], daily=_daily_frame())
    engine, account = _wire_portfolio_run(provider)
    strategy = _PortfolioLifecycleStrategy(fail_finish=True)
    engine._run_day = lambda *args, **kwargs: (_ for _ in ()).throw(
        LookupError('portfolio-run-primary'))

    with pytest.raises(LookupError, match='portfolio-run-primary'):
        engine.run(strategy)

    assert engine.last_result_contract.status.value == 'FAILED'
    assert strategy.finish_calls == 1
    assert strategy.order.status == OrderStatus.CANCELLED
    assert account.cash == pytest.approx(account.initial_cash)
    assert account.frozen_cash == pytest.approx(0.0)


def test_portfolio_init_error_cleans_without_finish():
    engine, account = _wire_portfolio_run(_Provider(trade_days=[]))
    strategy = _PortfolioLifecycleStrategy(fail_init=True)

    with pytest.raises(RuntimeError, match='portfolio-init-primary'):
        engine.run(strategy)

    assert engine.last_result_contract.status.value == 'FAILED'
    assert strategy.finish_calls == 0
    assert strategy.order.status == OrderStatus.CANCELLED
    assert account.cash == pytest.approx(account.initial_cash)
    assert account.frozen_cash == pytest.approx(0.0)


def test_portfolio_stop_is_a_completed_lifecycle_not_a_finish_skip():
    provider = _Provider(trade_days=[T], daily=_daily_frame())
    engine, account = _wire_portfolio_run(provider, stop_check=lambda: True)
    strategy = _PortfolioLifecycleStrategy()

    engine.run(strategy)

    assert engine._stopped is True
    assert strategy.finish_calls == 1
    assert strategy.order.status == OrderStatus.CANCELLED
    assert account.frozen_cash == pytest.approx(0.0)


def test_portfolio_finish_error_is_visible_without_primary_error():
    engine, account = _wire_portfolio_run(_Provider(trade_days=[]))
    strategy = _PortfolioLifecycleStrategy(fail_finish=True)

    with pytest.raises(RuntimeError, match='portfolio-finish-secondary'):
        engine.run(strategy)

    assert strategy.finish_calls == 1
    assert strategy.order.status == OrderStatus.CANCELLED
    assert account.frozen_cash == pytest.approx(0.0)


class _PreOpenOrderAndFinish(Strategy):
    def __init__(self):
        super().__init__()
        self.order = None
        self.minute_calls = 0
        self.day_calls = 0
        self.finish_calls = 0

    def on_before_market_open(self, ctx):
        self.order = ctx.buy_at_market(
            SYMBOL, shares=100, current_price=10.0)

    def on_minute(self, ctx, bar):
        self.minute_calls += 1

    def on_day(self, ctx, bar):
        self.day_calls += 1

    def on_finish(self, ctx):
        self.finish_calls += 1


@pytest.mark.parametrize(
    ('strategy_minute', 'execution_minute', 'message'),
    [
        (pd.DataFrame(), pd.DataFrame(), 'Minute execution data required'),
        (pd.DataFrame(), _minute_frame(), 'Minute strategy data required'),
    ],
)
def test_public_minute_run_missing_lane_fails_fast_and_releases(
        strategy_minute, execution_minute, message):
    provider = _Provider(
        trade_days=[T],
        daily=_daily_frame(),
        strategy_minute=strategy_minute,
        execution_minute=execution_minute,
    )
    engine, account = _wire_engine(provider, freq='minute')
    strategy = _PreOpenOrderAndFinish()

    with pytest.raises(ValueError, match=message):
        engine.run(strategy)

    assert strategy.minute_calls == 0
    assert strategy.day_calls == 0
    assert strategy.finish_calls == 1
    assert strategy.order.status == OrderStatus.CANCELLED
    assert account.frozen_cash == pytest.approx(0.0)
    assert account.cash == pytest.approx(account.initial_cash)
    assert account.get_position(SYMBOL) is None


class _ObserveMinuteLane(Strategy):
    def __init__(self):
        super().__init__()
        self.seen = []

    def on_minute(self, ctx, bar):
        self.seen.append(
            (pd.Timestamp(bar.trade_time), float(bar.close)))


def test_single_minute_strategy_lane_is_aligned_by_timestamp():
    raw = _minute_frame(times=('09:30', '09:31'))
    strategy_lane = raw.copy()
    strategy_lane.loc[strategy_lane.index[0],
                      ['open', 'high', 'low', 'close']] = (
        [100.0, 102.0, 98.0, 101.0]
    )
    strategy_lane.loc[strategy_lane.index[1],
                      ['open', 'high', 'low', 'close']] = (
        [200.0, 202.0, 198.0, 201.0]
    )
    strategy_lane = strategy_lane.iloc[::-1]
    provider = _Provider(
        trade_days=[T], daily=_daily_frame(),
        strategy_minute=strategy_lane, execution_minute=raw,
    )
    engine, _ = _wire_engine(provider, freq='minute')
    strategy = _ObserveMinuteLane()
    engine._strategy = strategy
    strategy.set_context(engine._context)

    engine._run_day(T)

    assert strategy.seen == [
        (pd.Timestamp('2024-01-02 09:30:00'), 101.0),
        (pd.Timestamp('2024-01-02 09:31:00'), 201.0),
    ]


def test_single_minute_timestamp_mismatch_fails_before_callback():
    raw = _minute_frame(times=('09:30', '09:31'))
    strategy_lane = raw.iloc[[1]].copy()
    provider = _Provider(
        trade_days=[T], daily=_daily_frame(),
        strategy_minute=strategy_lane, execution_minute=raw,
    )
    engine, _ = _wire_engine(provider, freq='minute')
    strategy = _ObserveMinuteLane()
    engine._strategy = strategy
    strategy.set_context(engine._context)

    with pytest.raises(ValueError, match='Minute timestamp mismatch'):
        engine._run_day(T)

    assert strategy.seen == []


@pytest.mark.parametrize(
    ('strategy_daily', 'execution_daily', 'message'),
    [
        (pd.DataFrame(), _daily_frame(), 'Daily strategy data required'),
        (_daily_frame(), pd.DataFrame(), 'Daily execution data required'),
    ],
)
def test_single_daily_missing_lane_never_substitutes(
        strategy_daily, execution_daily, message):
    provider = _Provider(
        strategy_daily=strategy_daily,
        execution_daily=execution_daily,
    )
    engine, _ = _wire_engine(provider, freq='daily')
    strategy = _ObserveMinuteLane()
    engine._strategy = strategy
    strategy.set_context(engine._context)

    with pytest.raises(ValueError, match=message):
        engine._run_daily_bar(T)


def test_single_daily_duplicate_key_fails_before_callback():
    duplicate = pd.concat([_daily_frame(), _daily_frame()])
    provider = _Provider(
        strategy_daily=duplicate,
        execution_daily=duplicate,
    )
    engine, _ = _wire_engine(provider, freq='daily')
    strategy = _ObserveMinuteLane()
    engine._strategy = strategy
    strategy.set_context(engine._context)

    with pytest.raises(ValueError, match='Daily data key must be unique'):
        engine._run_daily_bar(T)


def test_single_daily_wrong_timestamp_fails_before_callback():
    wrong = _daily_frame()
    wrong.index = pd.to_datetime(['2024-01-03'])
    provider = _Provider(
        strategy_daily=wrong,
        execution_daily=wrong,
    )
    engine, _ = _wire_engine(provider, freq='daily')
    strategy = _ObserveMinuteLane()
    engine._strategy = strategy
    strategy.set_context(engine._context)

    with pytest.raises(ValueError, match='Daily timestamp mismatch'):
        engine._run_daily_bar(T)


def test_portfolio_daily_misaligned_lane_raises_exact_data_contract_error():
    strategy_other_day = _daily_frame()
    strategy_other_day.index = pd.to_datetime(['2024-01-03'])
    provider = _Provider(
        strategy_daily=strategy_other_day,
        execution_daily=_daily_frame(),
    )

    def strict_pair(symbol, *, frequency, pre_close_exempt_dates=(), **kwargs):
        return validate_and_align_pair(
            provider.strategy_daily,
            provider.execution_daily,
            symbol=symbol,
            strategy_price_space=PriceSpace.HFQ,
            execution_price_space=PriceSpace.HFQ,
            strategy_amount_unit=AmountUnit.THOUSAND_YUAN,
            execution_amount_unit=AmountUnit.THOUSAND_YUAN,
            frequency=Frequency(frequency),
            pre_close_exempt_dates=pre_close_exempt_dates,
        )

    provider.get_aligned_pair = strict_pair
    engine, _ = _wire_portfolio_run(provider)

    with pytest.raises(DataContractError) as caught:
        engine._run_daily_bars(T, [SYMBOL])

    assert "KEY_MISMATCH" in caught.value.report.issue_codes


def _install_strict_daily_pair(provider, captured):
    def strict_pair(symbol, *, frequency, pre_close_exempt_dates=(), **kwargs):
        captured.append((symbol, frequency, set(pre_close_exempt_dates)))
        return validate_and_align_pair(
            provider.strategy_daily,
            provider.execution_daily,
            symbol=symbol,
            strategy_price_space=PriceSpace.HFQ,
            execution_price_space=PriceSpace.HFQ,
            strategy_amount_unit=AmountUnit.THOUSAND_YUAN,
            execution_amount_unit=AmountUnit.THOUSAND_YUAN,
            frequency=Frequency(frequency),
            pre_close_exempt_dates=pre_close_exempt_dates,
        )

    provider.get_aligned_pair = strict_pair


def test_single_engine_forwards_its_computed_symbol_exemptions_to_adapter():
    daily = _daily_frame()
    daily.loc[:, 'pre_close'] = 0.0
    provider = _Provider(
        trade_days=[T],
        strategy_daily=daily,
        execution_daily=daily,
    )
    captured = []
    _install_strict_daily_pair(provider, captured)
    engine, _ = _wire_engine(provider, freq='daily')
    engine._context._broker.set_limit_check_exempt({SYMBOL: {T}})

    engine.run(Strategy())

    assert captured == [(SYMBOL, 'daily', {T})]
    assert engine._data_quality_reports[0].pre_close_exempt_dates == (T,)


def test_single_engine_formal_adapter_never_mirrors_a_missing_daily_lane():
    provider = _Provider(
        trade_days=[T],
        strategy_daily=pd.DataFrame(),
        execution_daily=_daily_frame(),
    )
    _install_strict_daily_pair(provider, [])
    engine, _ = _wire_engine(provider, freq='daily')

    with pytest.raises(DataContractError) as caught:
        engine.run(Strategy())

    assert "MISSING_DATASET" in caught.value.report.issue_codes


def test_portfolio_preload_forwards_each_symbols_computed_exemptions_to_adapter():
    daily = _daily_frame()
    daily.loc[:, 'pre_close'] = 0.0
    provider = _Provider(
        strategy_daily=daily,
        execution_daily=daily,
    )
    captured = []
    _install_strict_daily_pair(provider, captured)
    engine, _ = _wire_portfolio_run(provider)
    engine._context._broker.set_limit_check_exempt({SYMBOL: {T}})

    PortfolioEngine._preload_data(engine, [SYMBOL])

    assert captured == [(SYMBOL, 'daily', {T})]
    assert engine._data_quality_reports[0].pre_close_exempt_dates == (T,)


class _MinuteOrderAt(Strategy):
    def __init__(self, order_kind, submit_time):
        super().__init__()
        self.order_kind = order_kind
        self.submit_time = submit_time
        self.order = None
        self.frozen_on_submit = None
        self.after_close_state = None

    def on_minute(self, ctx, bar):
        if self.order is not None:
            return
        if pd.Timestamp(bar.trade_time).strftime('%H:%M') != self.submit_time:
            return
        if self.order_kind == 'market':
            self.order = ctx.buy_at_market(
                SYMBOL, shares=100, current_price=20.0)
        else:
            self.order = ctx.buy_at_close(
                SYMBOL, shares=100, current_price=20.0)
        self.frozen_on_submit = ctx._broker.account.frozen_cash

    def on_after_market_close(self, ctx):
        if self.order is not None:
            self.after_close_state = (
                self.order.status,
                self.order.filled_shares,
                ctx._broker.account.frozen_cash,
            )


@pytest.mark.parametrize('order_kind', ['market', 'close'])
def test_last_continuous_minute_order_is_not_backfilled_when_auction_missing(
        order_kind):
    minute = _minute_frame(times=('09:30', '14:56'))
    provider = _Provider(
        trade_days=[T], daily=_daily_frame(),
        strategy_minute=minute, execution_minute=minute,
    )
    engine, account = _wire_engine(
        provider, freq='minute', legacy=True)
    strategy = _MinuteOrderAt(order_kind, '14:56')
    engine._strategy = strategy
    strategy.set_context(engine._context)

    engine._run_day(T)

    assert strategy.frozen_on_submit > 0
    assert strategy.after_close_state == (
        OrderStatus.CANCELLED, 0, pytest.approx(0.0))
    assert account.cash == pytest.approx(account.initial_cash)
    assert account.get_position(SYMBOL) is None


def test_last_normal_row_is_not_relabelled_as_close_auction():
    minute = _minute_frame(times=('09:30', '14:56'))
    provider = _Provider(
        trade_days=[T], daily=_daily_frame(),
        strategy_minute=minute, execution_minute=minute,
    )
    engine, account = _wire_engine(
        provider, freq='minute', legacy=True)
    strategy = _MinuteOrderAt('close', '09:30')
    engine._strategy = strategy
    strategy.set_context(engine._context)

    engine._run_day(T)

    assert strategy.frozen_on_submit > 0
    assert strategy.order.status == OrderStatus.CANCELLED
    assert strategy.order.filled_shares == 0
    assert account.frozen_cash == pytest.approx(0.0)
    assert account.get_position(SYMBOL) is None


def test_distinct_close_auction_can_fill_prior_normal_bar_close_order():
    minute = _minute_frame(
        times=('09:30', '14:56', '14:57', '15:00'))
    provider = _Provider(
        trade_days=[T], daily=_daily_frame(),
        strategy_minute=minute, execution_minute=minute,
    )
    engine, account = _wire_engine(
        provider, freq='minute', legacy=True)
    strategy = _MinuteOrderAt('close', '14:56')
    engine._strategy = strategy
    strategy.set_context(engine._context)

    engine._run_day(T)

    assert strategy.order.status == OrderStatus.FILLED
    assert strategy.order.filled_shares == 100
    assert account.frozen_cash == pytest.approx(0.0)
    assert account.get_position(SYMBOL) is not None


class _AfterOpenOrders(Strategy):
    def __init__(self):
        super().__init__()
        self.open_payload = None
        self.close_order = None
        self.market_order = None
        self.after_close = None

    def on_after_open(self, ctx, bar):
        self.open_payload = bar
        self.close_order = ctx.schedule_at_close(
            'buy', SYMBOL, shares=100, current_price=bar.open)
        self.market_order = ctx.buy_at_market(
            SYMBOL, shares=100, current_price=bar.open)

    def on_after_market_close(self, ctx):
        self.after_close = (
            self.close_order.status,
            self.market_order.status,
        )


def test_daily_after_open_exposes_only_open_and_promotes_only_close_intent():
    provider = _Provider(trade_days=[T], daily=_daily_frame())
    engine, account = _wire_engine(
        provider,
        freq='daily',
        daily_auction_liquidity=DailyAuctionLiquidityPolicy(
            close_cap=AuctionCapSpec.fixed_yuan(1_000_000.0),
        ),
    )
    strategy = _AfterOpenOrders()
    engine._strategy = strategy
    strategy.set_context(engine._context)

    engine._run_day(T, next_trade_date='20240103')

    assert strategy.open_payload.open == pytest.approx(10.0)
    assert not hasattr(strategy.open_payload, 'high')
    assert not hasattr(strategy.open_payload, 'low')
    assert not hasattr(strategy.open_payload, 'close')
    assert strategy.after_close == (
        OrderStatus.FILLED, OrderStatus.SUBMITTED)
    assert strategy.close_order.eligible_from == pd.Timestamp(
        '2024-01-02 15:00:00')
    assert strategy.close_order.expire_date == T
    assert strategy.market_order.eligible_from == pd.Timestamp('2024-01-03')
    assert strategy.market_order.expire_date == '20240103'
    close_attrs = _created_attributes(engine._context._broker, strategy.close_order)
    market_attrs = _created_attributes(engine._context._broker, strategy.market_order)
    assert close_attrs['eligible_from'] == '2024-01-02T15:00:00.000000'
    assert close_attrs['expire_date'] == T
    assert market_attrs['eligible_from'] == '2024-01-03T00:00:00.000000'
    assert market_attrs['expire_date'] == '20240103'
    assert account.get_position(SYMBOL).shares == 100


class _BeforeCloseOrder(Strategy):
    def __init__(self):
        super().__init__()
        self.seen_minutes = []
        self.callback_time = None
        self.last_seen_at_callback = None
        self.current_bar_time_at_callback = None
        self.order = None
        self.derived_market_order = None

    def on_minute(self, ctx, bar):
        self.seen_minutes.append(
            pd.Timestamp(bar.trade_time).strftime('%H:%M'))

    def on_before_close(self, ctx):
        self.callback_time = pd.Timestamp(ctx.current_time)
        self.last_seen_at_callback = self.seen_minutes[-1]
        self.current_bar_time_at_callback = pd.Timestamp(
            ctx._current_bar.trade_time)
        self.order = ctx.schedule_at_close(
            'buy', SYMBOL, shares=100, current_price=10.0)
        self.derived_market_order = ctx.buy_at_market(amount=10_500.0)


def test_minute_before_close_cannot_observe_auction_bar_and_close_can_fill():
    minute = _minute_frame(
        times=('09:30', '14:56', '14:57', '14:58', '15:00'))
    provider = _Provider(
        trade_days=[T], daily=_daily_frame(),
        strategy_minute=minute, execution_minute=minute,
    )
    engine, account = _wire_engine(provider, freq='minute')
    strategy = _BeforeCloseOrder()
    engine._strategy = strategy
    strategy.set_context(engine._context)

    engine._run_day(T, next_trade_date='20240103')

    assert strategy.callback_time == pd.Timestamp('2024-01-02 14:58:00')
    assert strategy.last_seen_at_callback == '14:57'
    assert strategy.seen_minutes == ['09:30', '14:56', '14:57']
    assert strategy.current_bar_time_at_callback == pd.Timestamp(
        '2024-01-02 14:57:00')
    # 14:57 high=10.4 -> floor(10_500/10.4) to a 100-share lot = 1,000.
    # Reading the hidden 15:00 auction high=10.6 would incorrectly yield 900.
    assert strategy.derived_market_order.shares == 1_000
    assert strategy.order.status == OrderStatus.FILLED
    assert strategy.order.created_phase == 'on_before_close'
    assert strategy.order.eligible_from == pd.Timestamp(
        '2024-01-02 15:00:00')
    created_attrs = _created_attributes(
        engine._context._broker, strategy.order)
    assert created_attrs['eligible_from'] == '2024-01-02T15:00:00.000000'
    assert created_attrs['expire_date'] == T
    assert account.get_position(SYMBOL).shares == 100
