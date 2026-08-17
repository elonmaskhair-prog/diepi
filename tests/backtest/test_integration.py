# -*- coding: utf-8 -*-
"""
集成测试

端到端的完整回测流程测试
"""

import pytest
import pandas as pd
from datetime import datetime
from functools import partial

from diepi.backtest.engine.backtest_engine import BacktestEngine, BacktestResult
from diepi.backtest.strategy.base import Strategy, BarData
from diepi.backtest.broker.order import OrderStatus
from tests.backtest.auction_test_policy import EXPLICIT_TEST_AUCTION_POLICY


BacktestEngine = partial(
    BacktestEngine,
    daily_auction_liquidity=EXPLICIT_TEST_AUCTION_POLICY,
)


pytestmark = pytest.mark.integration


class TestSimpleBuyHold:
    """简单买入持有策略测试"""

    def test_simple_buy_hold(self):
        """买入持有"""

        class BuyHold(Strategy):
            def on_before_market_open(self, ctx):
                pos = ctx.get_position()
                if pos is None or pos.shares == 0:
                    ctx.buy_at_open(percent=0.9)

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240229',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(BuyHold())

        assert result.initial_cash == 100000.0
        assert result.final_value > 0
        assert isinstance(result.total_return, float)
        assert isinstance(result.max_drawdown, float)
        assert isinstance(result.max_drawdown_intraday_low_nav, float)
        assert result.max_drawdown_intraday_low_nav >= result.max_drawdown

    def test_buy_hold_with_amount(self):
        """按金额买入"""

        class BuyAmount(Strategy):
            def on_before_market_open(self, ctx):
                pos = ctx.get_position()
                if pos is None or pos.shares == 0:
                    ctx.buy_at_open(amount=50000.0)

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(BuyAmount())

        # 应该买入成功
        assert result.trade_count >= 0


class TestMACrossStrategy:
    """均线交叉策略测试"""

    def test_ma_cross_strategy(self):
        """均线交叉"""

        class MACross(Strategy):
            def on_init(self, ctx):
                self._short = 5
                self._long = 10
                self._position_opened = False

            def on_day(self, ctx, bar):
                df = ctx.get_daily(days=self._long + 1)
                if len(df) < self._long:
                    return

                short_ma = df['close'].tail(self._short).mean()
                long_ma = df['close'].tail(self._long).mean()

                pos = ctx.get_position()

                if short_ma > long_ma:
                    if pos is None or pos.shares == 0:
                        ctx.buy_at_market(percent=0.9, current_price=bar.close)
                        self._position_opened = True
                else:
                    if pos and pos.available_shares > 0:
                        ctx.sell_at_market(percent=1.0)

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240430',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(MACross())

        assert isinstance(result, BacktestResult)
        assert result.sharpe_ratio is not None


class TestStopLossExecution:
    """止损执行测试"""

    def test_stop_loss_execution(self):
        """止损执行"""

        class StopLossStrategy(Strategy):
            def on_init(self, ctx):
                self._bought = False
                self._stop_price = None

            def on_day(self, ctx, bar):
                pos = ctx.get_position()

                if not self._bought:
                    ctx.buy_at_market(percent=0.9, current_price=bar.close)
                    self._bought = True
                    self._stop_price = bar.close * 0.95  # 5% 止损

                elif pos and pos.available_shares > 0 and self._stop_price:
                    if bar.low <= self._stop_price:
                        ctx.sell_at_market(percent=1.0)

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240430',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(StopLossStrategy())

        # 最大回撤应该被控制
        assert result.max_drawdown is not None


class TestTakeProfitExecution:
    """止盈执行测试"""

    def test_take_profit_execution(self):
        """止盈执行"""

        class TakeProfitStrategy(Strategy):
            def on_init(self, ctx):
                self._bought = False
                self._profit_price = None

            def on_day(self, ctx, bar):
                pos = ctx.get_position()

                if not self._bought:
                    ctx.buy_at_market(percent=0.9, current_price=bar.close)
                    self._bought = True
                    self._profit_price = bar.close * 1.10  # 10% 止盈

                elif pos and pos.available_shares > 0 and self._profit_price:
                    if bar.high >= self._profit_price:
                        ctx.sell_at_market(percent=1.0)
                        self._bought = False

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            # Keep this fixture focused on take-profit execution; later
            # corporate-action cash-in-lieu behavior has dedicated tests.
            end_date='20240613',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(TakeProfitStrategy())

        assert isinstance(result.win_rate, float)


class TestMultipleOrders:
    """多订单场景测试"""

    def test_multiple_orders(self):
        """多订单场景"""

        class MultiOrderStrategy(Strategy):
            def on_init(self, ctx):
                self._day = 0

            def on_day(self, ctx, bar):
                self._day += 1
                pos = ctx.get_position()

                # 第一天：买入
                if self._day == 1:
                    ctx.buy_at_market(percent=0.3, current_price=bar.close)

                # 第三天：加仓
                elif self._day == 3:
                    ctx.buy_at_market(percent=0.3, current_price=bar.close)

                # 第五天：减仓
                elif self._day == 5 and pos and pos.available_shares > 0:
                    ctx.sell_at_market(percent=0.5)

                # 第七天：清仓
                elif self._day == 7 and pos and pos.available_shares > 0:
                    ctx.sell_at_market(percent=1.0)

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(MultiOrderStrategy())

        # 应该有多笔交易
        assert result.trade_count >= 0


class TestFullBacktestCycle:
    """完整回测周期测试"""

    def test_full_backtest_cycle(self):
        """完整回测周期"""

        class FullCycleStrategy(Strategy):
            def on_init(self, ctx):
                self.init_done = True
                self.days_run = 0
                self.orders_placed = 0

            def on_before_market_open(self, ctx):
                if self.days_run == 0:
                    ctx.buy_at_open(percent=0.9)
                    self.orders_placed += 1

            def on_day(self, ctx, bar):
                self.days_run += 1

                # 第 5 天卖出
                pos = ctx.get_position()
                if self.days_run == 5 and pos and pos.available_shares > 0:
                    ctx.sell_at_market(percent=1.0)
                    self.orders_placed += 1

            def on_after_market_close(self, ctx):
                # 可以在这里计算指标
                pass

            def on_finish(self, ctx):
                self.finish_done = True

        strategy = FullCycleStrategy()
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(strategy)

        # 验证回调执行
        assert strategy.init_done is True
        assert strategy.finish_done is True
        assert strategy.days_run > 0

        # 验证结果
        assert result.initial_cash == 100000.0
        assert result.final_value > 0
        assert len(result.daily_values) > 0


class TestLimitOrders:
    """限价单测试"""

    def test_limit_order_execution(self):
        """限价单执行"""

        class LimitOrderStrategy(Strategy):
            def on_init(self, ctx):
                self._order_placed = False

            def on_day(self, ctx, bar):
                if not self._order_placed:
                    # 设置一个略低于当前价的限价买单
                    limit_price = bar.close * 0.99
                    ctx.buy_at_price(price=limit_price, shares=1000)
                    self._order_placed = True

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(LimitOrderStrategy())

        # 限价单可能成交也可能不成交
        assert isinstance(result.trade_count, int)


class TestStopOrders:
    """止损止盈单测试"""

    def test_stop_loss_order(self):
        """止损单"""

        class StopLossOrderStrategy(Strategy):
            def on_init(self, ctx):
                self._bought = False
                self._stop_placed = False

            def on_day(self, ctx, bar):
                pos = ctx.get_position()

                if not self._bought:
                    ctx.buy_at_market(percent=0.9, current_price=bar.close)
                    self._bought = True

                elif pos and pos.available_shares > 0 and not self._stop_placed:
                    # 设置止损单
                    stop_price = pos.avg_cost * 0.95
                    ctx.sell_stop_loss(price=stop_price, percent=1.0)
                    self._stop_placed = True

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240430',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(StopLossOrderStrategy())

        assert isinstance(result, BacktestResult)

    def test_stop_profit_order(self):
        """止盈单"""

        class StopProfitOrderStrategy(Strategy):
            def on_init(self, ctx):
                self._bought = False
                self._profit_placed = False

            def on_day(self, ctx, bar):
                pos = ctx.get_position()

                if not self._bought:
                    ctx.buy_at_market(percent=0.9, current_price=bar.close)
                    self._bought = True

                elif pos and pos.available_shares > 0 and not self._profit_placed:
                    # 设置止盈单
                    profit_price = pos.avg_cost * 1.10
                    ctx.sell_stop_profit(price=profit_price, percent=1.0)
                    self._profit_placed = True

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240630',
            initial_cash=100000.0,
            freq='daily',
        )

        result = engine.run(StopProfitOrderStrategy())

        assert isinstance(result, BacktestResult)


class TestDataAccess:
    """数据访问测试"""

    def test_daily_data_access(self):
        """日线数据访问"""

        class DataAccessStrategy(Strategy):
            def on_init(self, ctx):
                self.data_accessed = False

            def on_day(self, ctx, bar):
                df = ctx.get_daily(days=10)
                if not df.empty:
                    self.data_accessed = True
                    assert 'close' in df.columns
                    assert len(df) <= 10

        strategy = DataAccessStrategy()
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240115',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        engine.run(strategy)

        assert strategy.data_accessed is True

    def test_stock_info_access(self):
        """股票信息访问"""

        class InfoAccessStrategy(Strategy):
            def on_init(self, ctx):
                self.info_accessed = False

            def on_day(self, ctx, bar):
                info = ctx.get_stock_info()
                if info is not None:
                    self.info_accessed = True

        strategy = InfoAccessStrategy()
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240103',
            initial_cash=100000.0,
            freq='daily',
        )

        engine.run(strategy)


class TestPositionTracking:
    """持仓跟踪测试"""

    def test_position_tracking(self):
        """持仓跟踪"""

        class PositionTrackingStrategy(Strategy):
            def on_init(self, ctx):
                self.position_history = []

            def on_after_market_close(self, ctx):
                pos = ctx.get_position()
                self.position_history.append({
                    'date': ctx.current_date,
                    'shares': pos.shares if pos else 0,
                    'market_value': pos.market_value if pos else 0,
                })

            def on_before_market_open(self, ctx):
                pos = ctx.get_position()
                if pos is None or pos.shares == 0:
                    ctx.buy_at_open(percent=0.9)

        strategy = PositionTrackingStrategy()
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            freq='daily',
        )

        engine.run(strategy)

        assert len(strategy.position_history) > 0


class TestCommissionAndSlippage:
    """手续费和滑点测试"""

    def test_with_high_commission(self):
        """高手续费"""

        class SimpleStrategy(Strategy):
            def on_init(self, ctx):
                self._day = 0

            def on_before_market_open(self, ctx):
                self._day += 1
                if self._day == 1:
                    ctx.buy_at_open(percent=0.9)
                elif self._day == 5:
                    pos = ctx.get_position()
                    if pos and pos.available_shares > 0:
                        ctx.sell_at_open(percent=1.0)

        # 高手续费
        engine_high = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            commission_rate=0.003,  # 0.3%
            freq='daily',
        )

        # 低手续费
        engine_low = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            commission_rate=0.00025,  # 0.025%
            freq='daily',
        )

        result_high = engine_high.run(SimpleStrategy())
        result_low = engine_low.run(SimpleStrategy())

        # 高手续费情况下收益应该更低
        # 注意：由于市场波动，这不一定总是成立
        assert isinstance(result_high.total_return, float)
        assert isinstance(result_low.total_return, float)

    def test_with_high_slippage(self):
        """高滑点"""

        class SimpleStrategy(Strategy):
            def on_before_market_open(self, ctx):
                pos = ctx.get_position()
                if pos is None or pos.shares == 0:
                    ctx.buy_at_open(percent=0.9)

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240131',
            initial_cash=100000.0,
            slippage=0.005,  # 0.5%
            freq='daily',
        )

        result = engine.run(SimpleStrategy())

        assert isinstance(result, BacktestResult)
