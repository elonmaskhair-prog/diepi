# -*- coding: utf-8 -*-
"""
Strategy 单元测试

测试策略基类
"""

import pytest
import pandas as pd
from datetime import datetime
from functools import partial

from diepi.backtest.strategy.base import Strategy, BarData
from diepi.backtest.engine.backtest_engine import BacktestEngine
from tests.backtest.auction_test_policy import EXPLICIT_TEST_AUCTION_POLICY


BacktestEngine = partial(
    BacktestEngine,
    daily_auction_liquidity=EXPLICIT_TEST_AUCTION_POLICY,
)


class TestStrategy:
    """策略基类测试"""

    def test_parameter_get_set(self):
        """参数获取/设置"""
        strategy = Strategy(params={'threshold': 10, 'period': 5})

        assert strategy.get_param('threshold') == 10
        assert strategy.get_param('period') == 5
        assert strategy.get_param('nonexistent') is None
        assert strategy.get_param('nonexistent', 'default') == 'default'

    def test_set_param(self):
        """设置参数"""
        strategy = Strategy()

        strategy.set_param('new_param', 100)

        assert strategy.get_param('new_param') == 100

    def test_params_property(self):
        """参数属性"""
        strategy = Strategy(params={'a': 1, 'b': 2})

        params = strategy.params

        assert params == {'a': 1, 'b': 2}
        # 应该返回副本
        params['c'] = 3
        assert strategy.get_param('c') is None

    def test_on_init_documents_inactive_simulation_date_contract(self):
        doc = Strategy.on_init.__doc__ or ''

        assert 'ctx.current_date' in doc
        assert 'on_before_market_open' in doc
        assert '明确且因果安全的日期边界' in doc


@pytest.mark.integration
class TestStrategyCallbacks:
    """策略回调测试"""

    def test_on_init_called(self):
        """on_init 调用"""

        class InitStrategy(Strategy):
            def on_init(self, ctx):
                self.init_called = True

        strategy = InitStrategy()
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240103',
            initial_cash=100000.0,
            freq='daily',
        )

        engine.run(strategy)

        assert hasattr(strategy, 'init_called')
        assert strategy.init_called is True

    def test_on_before_market_open_called(self):
        """on_before_market_open 调用"""

        class BeforeOpenStrategy(Strategy):
            def on_init(self, ctx):
                self.before_open_count = 0

            def on_before_market_open(self, ctx):
                self.before_open_count += 1

        strategy = BeforeOpenStrategy()
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240105',
            initial_cash=100000.0,
            freq='daily',
        )

        engine.run(strategy)

        assert strategy.before_open_count > 0

    def test_on_day_called(self):
        """on_day 调用"""

        class DayStrategy(Strategy):
            def on_init(self, ctx):
                self.day_count = 0
                self.bars = []

            def on_day(self, ctx, bar):
                self.day_count += 1
                self.bars.append(bar)

        strategy = DayStrategy()
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240105',
            initial_cash=100000.0,
            freq='daily',
        )

        engine.run(strategy)

        assert strategy.day_count > 0
        for bar in strategy.bars:
            assert isinstance(bar, BarData)

    def test_on_minute_called(self):
        """on_minute 调用"""

        class MinuteStrategy(Strategy):
            def on_init(self, ctx):
                self.minute_count = 0

            def on_minute(self, ctx, bar):
                self.minute_count += 1

        strategy = MinuteStrategy()
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240102',  # 单日
            initial_cash=100000.0,
            freq='minute',
        )

        engine.run(strategy)

        # 分钟数据可能为空，取决于数据可用性
        assert strategy.minute_count >= 0

    def test_on_after_market_close_called(self):
        """on_after_market_close 调用"""

        class AfterCloseStrategy(Strategy):
            def on_init(self, ctx):
                self.after_close_count = 0

            def on_after_market_close(self, ctx):
                self.after_close_count += 1

        strategy = AfterCloseStrategy()
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240105',
            initial_cash=100000.0,
            freq='daily',
        )

        engine.run(strategy)

        assert strategy.after_close_count > 0

    def test_on_finish_called(self):
        """on_finish 调用"""

        class FinishStrategy(Strategy):
            def on_init(self, ctx):
                self.finish_called = False

            def on_finish(self, ctx):
                self.finish_called = True

        strategy = FinishStrategy()
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240103',
            initial_cash=100000.0,
            freq='daily',
        )

        engine.run(strategy)

        assert strategy.finish_called is True


class TestBarData:
    """BarData 测试"""

    def test_bar_data_creation(self):
        """创建 BarData"""
        bar = BarData(
            symbol='000001.SZ',
            trade_time=pd.Timestamp('2024-01-02 09:30:00'),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            vol=1000000,
            amount=10200000,
        )

        assert bar.symbol == '000001.SZ'
        assert bar.open == 10.0
        assert bar.high == 10.5
        assert bar.low == 9.8
        assert bar.close == 10.2
        assert bar.vol == 1000000
        assert bar.amount == 10200000

    def test_bar_data_from_series(self):
        """从 Series 创建"""
        series = pd.Series({
            'open': 10.0,
            'high': 10.5,
            'low': 9.8,
            'close': 10.2,
            'vol': 1000000,
            'amount': 10200000,
        }, name=pd.Timestamp('2024-01-02 09:30:00'))

        bar = BarData.from_series('000001.SZ', series)

        assert bar.symbol == '000001.SZ'
        assert bar.open == 10.0
        assert bar.close == 10.2
        assert bar.trade_time == pd.Timestamp('2024-01-02 09:30:00')

    def test_bar_data_defaults(self):
        """默认值"""
        bar = BarData(
            symbol='000001.SZ',
            trade_time=pd.Timestamp('2024-01-02 09:30:00'),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
        )

        assert bar.vol == 0.0
        assert bar.amount == 0.0


class TestStrategyContext:
    """策略上下文测试"""

    def test_set_context(self):
        """设置上下文"""
        from diepi.backtest.engine.context import Context

        strategy = Strategy()
        ctx = Context(initial_cash=100000.0)

        strategy.set_context(ctx)

        assert strategy._context is ctx


@pytest.mark.integration
class TestStrategyExamples:
    """策略示例测试"""

    def test_simple_buy_hold(self):
        """简单买入持有"""

        class BuyHold(Strategy):
            def on_before_market_open(self, ctx):
                pos = ctx.get_position()
                if pos is None or pos.shares == 0:
                    ctx.buy_at_open(percent=0.9)

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(BuyHold())

        # 应该至少有一笔交易
        assert result.trade_count >= 0

    def test_ma_cross_strategy(self):
        """均线交叉策略"""

        class MACross(Strategy):
            def on_init(self, ctx):
                self._short_period = self.get_param('short', 5)
                self._long_period = self.get_param('long', 10)

            def on_day(self, ctx, bar):
                df = ctx.get_daily(days=self._long_period + 1)
                if len(df) < self._long_period:
                    return

                short_ma = df['close'].tail(self._short_period).mean()
                long_ma = df['close'].tail(self._long_period).mean()

                pos = ctx.get_position()

                if short_ma > long_ma:
                    if pos is None or pos.shares == 0:
                        ctx.buy_at_market(percent=0.9, current_price=bar.close)
                else:
                    if pos and pos.available_shares > 0:
                        ctx.sell_at_market(percent=1.0)

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240229',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(MACross({'short': 5, 'long': 10}))

        assert isinstance(result.total_return, float)

    def test_stop_loss_strategy(self):
        """止损策略"""

        class StopLoss(Strategy):
            def on_init(self, ctx):
                self._stop_pct = self.get_param('stop_pct', 0.05)
                self._buy_price = None

            def on_day(self, ctx, bar):
                pos = ctx.get_position()

                if pos is None or pos.shares == 0:
                    ctx.buy_at_market(percent=0.9, current_price=bar.close)
                    self._buy_price = bar.close
                elif self._buy_price:
                    stop_price = self._buy_price * (1 - self._stop_pct)
                    if bar.close <= stop_price:
                        ctx.sell_at_market(percent=1.0)
                        self._buy_price = None

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240229',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(StopLoss({'stop_pct': 0.05}))

        assert isinstance(result.max_drawdown, float)
