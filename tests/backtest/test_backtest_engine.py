# -*- coding: utf-8 -*-
"""
BacktestEngine 单元测试

测试回测引擎
"""

import pytest
import pandas as pd
from datetime import datetime
from functools import partial

from diepi.backtest.engine.backtest_engine import BacktestEngine, BacktestResult
from diepi.backtest.data.exceptions import DataNotFoundError
from diepi.backtest.strategy.base import Strategy, BarData
from tests.backtest.auction_test_policy import EXPLICIT_TEST_AUCTION_POLICY


BacktestEngine = partial(
    BacktestEngine,
    daily_auction_liquidity=EXPLICIT_TEST_AUCTION_POLICY,
)


class SimpleStrategy(Strategy):
    """简单买入持有策略"""

    def on_before_market_open(self, ctx):
        """盘前买入"""
        pos = ctx.get_position()
        if pos is None or pos.shares == 0:
            ctx.buy_at_open(percent=0.9)


class SellStrategy(Strategy):
    """买入后卖出策略"""

    def on_init(self, ctx):
        self._bought = False
        self._day_count = 0

    def on_before_market_open(self, ctx):
        self._day_count += 1

        if not self._bought:
            ctx.buy_at_open(percent=0.9)
            self._bought = True
        elif self._day_count >= 5:
            pos = ctx.get_position()
            if pos and pos.available_shares > 0:
                ctx.sell_at_open(percent=1.0)


@pytest.mark.integration
class TestBacktestEngine:
    """回测引擎测试"""

    def test_run_daily_backtest(self):
        """日频回测"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(SimpleStrategy())

        assert isinstance(result, BacktestResult)
        assert result.symbol == '000001.SZ'
        assert result.start_date == '20240102'
        assert result.end_date == '20240131'

    def test_run_minute_backtest(self):
        """分钟频回测"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240105',  # 短期测试
            initial_cash=100000.0,
            freq='minute',
        )

        result = engine.run(SimpleStrategy())

        assert isinstance(result, BacktestResult)

    def test_strategy_callbacks(self):
        """策略回调顺序"""

        class CallbackTracker(Strategy):
            def on_init(self, ctx):
                self.callbacks = ['init']

            def on_before_market_open(self, ctx):
                self.callbacks.append('before_open')

            def on_day(self, ctx, bar):
                self.callbacks.append('day')

            def on_after_market_close(self, ctx):
                self.callbacks.append('after_close')

            def on_finish(self, ctx):
                self.callbacks.append('finish')

        strategy = CallbackTracker()
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240103',
            initial_cash=100000.0,
            freq='daily',
        )

        engine.run(strategy)

        # 检查回调顺序
        assert strategy.callbacks[0] == 'init'
        assert strategy.callbacks[-1] == 'finish'
        assert 'before_open' in strategy.callbacks
        assert 'after_close' in strategy.callbacks


@pytest.mark.integration
class TestBacktestResult:
    """回测结果测试"""

    def test_result_metrics(self):
        """结果指标计算"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(SimpleStrategy())

        # 检查基本指标存在
        assert result.initial_cash == 100000.0
        assert isinstance(result.final_value, float)
        assert isinstance(result.total_return, float)

    def test_total_return(self):
        """总收益率"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(SimpleStrategy())

        # 总收益率 = (最终资产 - 初始资金) / 初始资金
        expected = (result.final_value - result.initial_cash) / result.initial_cash
        assert abs(result.total_return - expected) < 0.0001

    def test_annual_return(self):
        """年化收益率"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(SimpleStrategy())

        assert isinstance(result.annual_return, float)

    def test_max_drawdown(self):
        """最大回撤"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(SimpleStrategy())

        # 回撤应该 >= 0
        assert result.max_drawdown >= 0
        assert result.max_drawdown_close_nav >= 0
        assert result.max_drawdown_intraday_low_nav >= result.max_drawdown_close_nav
        assert result.max_drawdown == result.max_drawdown_close_nav

    def test_sharpe_ratio(self):
        """夏普比率"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(SimpleStrategy())

        # sharpe_ratio 可能是 int (0) 或 float
        assert isinstance(result.sharpe_ratio, (int, float))

    def test_win_rate(self):
        """胜率"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(SellStrategy())

        # 胜率在 0-1 之间
        assert 0 <= result.win_rate <= 1

    def test_trade_count(self):
        """交易次数"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(SellStrategy())

        assert isinstance(result.trade_count, int)
        assert result.trade_count >= 0

    def test_daily_values(self):
        """每日净值"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(SimpleStrategy())

        assert isinstance(result.daily_values, pd.DataFrame)
        if not result.daily_values.empty:
            assert 'total_value' in result.daily_values.columns
            assert 'cash' in result.daily_values.columns
            assert 'intraday_low_total_value' in result.daily_values.columns
            assert 'intraday_low_market_value' in result.daily_values.columns

    def test_trades_list(self):
        """交易记录"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(SellStrategy())

        assert isinstance(result.trades, list)

    def test_result_to_dict(self):
        """结果转字典"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(SimpleStrategy())

        d = result.to_dict()

        assert 'symbol' in d
        assert 'total_return' in d
        assert 'annual_return' in d
        assert 'max_drawdown' in d
        assert 'sharpe_ratio' in d


@pytest.mark.integration
class TestBacktestEngineEmpty:
    """空结果测试"""

    def test_empty_date_range(self):
        """空日期范围"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20500101',  # 未来日期
            end_date='20500131',
            initial_cash=100000.0,
            freq='daily',
        )

        with pytest.raises(DataNotFoundError, match="coverage does not cover"):
            engine.run(SimpleStrategy())


class TestBacktestEngineParameters:
    """引擎参数测试"""

    def test_slippage(self):
        """滑点参数"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            slippage=0.002,
        )

        assert engine.slippage == 0.002

    def test_commission_rate(self):
        """佣金费率参数"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            commission_rate=0.0003,
        )

        assert engine.commission_rate == 0.0003

    def test_stamp_duty(self):
        """印花税参数"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            stamp_duty=0.001,
        )

        assert engine.stamp_duty == 0.001


class TestBacktestEnginePriceMode:
    """价格模式测试"""

    def test_strategy_price_mode(self):
        """策略价格模式"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            strategy_price_mode='hfq',
        )

        assert engine.strategy_price_mode == 'hfq'

    def test_execution_price_mode(self):
        """执行价格模式"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            execution_price_mode='raw',
        )

        assert engine.execution_price_mode == 'raw'


class TestWinRateCalculation:
    """胜率计算测试"""

    def test_calculate_win_rate_empty(self):
        """空交易列表"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
        )
        engine._init_engine()

        win_rate = engine._calculate_win_rate([])

        assert win_rate is None

    @pytest.mark.integration
    def test_calculate_win_rate_with_trades(self):
        """有交易的胜率计算"""

        class TradingStrategy(Strategy):
            def on_init(self, ctx):
                self._day = 0

            def on_before_market_open(self, ctx):
                self._day += 1
                pos = ctx.get_position()

                if self._day == 1:
                    ctx.buy_at_open(percent=0.5)
                elif self._day == 5 and pos and pos.available_shares > 0:
                    ctx.sell_at_open(percent=1.0)

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(TradingStrategy())

        # 胜率应该在 0-1 之间
        assert 0 <= result.win_rate <= 1
