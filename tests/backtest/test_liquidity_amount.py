# -*- coding: utf-8 -*-
"""
流动性帽与成交额单位回归测试

覆盖两个历史 bug：
1. F1: 单标的 BacktestEngine 构造 BrokerBarData 时未传 amount
   → auto 模式流动性帽=0 → 所有开盘买单被拒（历史上 000001.SZ 2024-01 实跑 0 成交）
2. F2: broker 假设 amount 单位为千元(×1000)，而分钟数据单位是元
   → 分钟模式流动性帽被放大 1000 倍。
   现约定：BrokerBarData.amount 一律为「元」，由引擎在构造时归一化
   （日线源数据千元×1000，分钟源数据元×1）。
"""

from datetime import datetime
from functools import partial

import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import Broker, BarData
from diepi.backtest.broker.order import OrderStatus
from diepi.backtest.engine.backtest_engine import BacktestEngine, _amount_yuan
from diepi.backtest.engine.minute_resampler import resample_minute_data
from diepi.backtest.strategy.base import Strategy
from tests.backtest.auction_test_policy import EXPLICIT_TEST_AUCTION_POLICY


BacktestEngine = partial(
    BacktestEngine,
    daily_auction_liquidity=EXPLICIT_TEST_AUCTION_POLICY,
)


class TestAmountYuanHelper:
    """_amount_yuan 归一化助手"""

    def test_daily_scale(self):
        # 日线 amount=千元 → 元
        assert _amount_yuan(231.9, scale=1000.0) == pytest.approx(231900.0)

    def test_minute_scale(self):
        # 分钟 amount 已是元
        assert _amount_yuan(11655.0) == pytest.approx(11655.0)

    def test_nan_none_missing(self):
        assert _amount_yuan(float('nan')) == 0.0
        assert _amount_yuan(None) == 0.0
        assert _amount_yuan('bad') == 0.0

    def test_negative_and_zero(self):
        assert _amount_yuan(-5.0, scale=1000.0) == 0.0
        assert _amount_yuan(0.0) == 0.0


class TestLiquidityCapUnit:
    """broker 流动性帽以「元」为单位"""

    def _make_bar(self, amount_yuan: float) -> BarData:
        return BarData(
            symbol='000001.SZ',
            trade_time=datetime(2024, 1, 2, 9, 30),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            vol=1000000,
            amount=amount_yuan,
            pre_close=9.8,
        )

    def test_cap_is_ratio_of_amount_in_yuan(self):
        broker = Broker(account=Account(initial_cash=10_000_000.0))
        assert broker._get_available_amount('X', 1_000_000.0) == pytest.approx(800_000.0)

    def test_cap_ratio_parameterized(self):
        broker = Broker(account=Account(initial_cash=10_000_000.0),
                        liquidity_cap_ratio=0.5)
        assert broker._get_available_amount('X', 1_000_000.0) == pytest.approx(500_000.0)

    def test_open_buy_binds_on_liquidity_cap(self):
        """bar 成交额 10 万元 → 帽 8 万元 → 100 万元买单被缩量到 ~8000 股"""
        broker = Broker(account=Account(initial_cash=10_000_000.0), slippage=0.0)
        broker.set_date('20240102')
        broker.set_market_phase('before_open')
        order = broker.buy_at_open('000001.SZ', amount=1_000_000.0)

        broker.set_market_phase('trading')
        bar = self._make_bar(amount_yuan=100_000.0)
        broker.execute_open_orders({'000001.SZ': bar})

        assert order.status == OrderStatus.CANCELLED
        # 帽 = 100000×0.8 = 80000 元；开盘价 10 → 上限 8000 股
        assert order.filled_shares == 8000

    def test_open_buy_zero_amount_cancelled(self):
        """amount=0 的单次开盘窗口零成交后撤销，不残留活动订单。"""
        broker = Broker(account=Account(initial_cash=10_000_000.0), slippage=0.0)
        broker.set_date('20240102')
        broker.set_market_phase('before_open')
        order = broker.buy_at_open('000001.SZ', amount=1_000_000.0)

        broker.set_market_phase('trading')
        bar = self._make_bar(amount_yuan=0.0)
        broker.execute_open_orders({'000001.SZ': bar})

        assert order.status == OrderStatus.CANCELLED
        assert 'residual cancelled' in (order.reject_reason or '')


class _BuyOnceStrategy(Strategy):
    """第一天开盘买入并持有"""

    def on_before_market_open(self, ctx):
        pos = ctx.get_position()
        if pos is None or pos.shares == 0:
            ctx.buy_at_open(percent=0.9)


@pytest.mark.integration
class TestSingleEngineTradesRegression:
    """F1 回归：单标的引擎必须真的能成交（依赖本地真实数据）"""

    def test_daily_backtest_produces_trades(self):
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240115',
            initial_cash=100000.0,
            freq='daily',
        )
        result = engine.run(_BuyOnceStrategy())
        # 历史 bug 下这里是 0 成交（全部 liquidity_limit 拒单）
        assert result.trade_count > 0, "单标的日线引擎开盘买单应能成交"
        assert result.final_value != result.initial_cash

    def test_minute_backtest_produces_trades(self):
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240105',
            initial_cash=100000.0,
            freq='minute',
        )
        result = engine.run(_BuyOnceStrategy())
        assert result.trade_count > 0, "单标的分钟引擎开盘买单应能成交"

    def test_lastbar_liquidity_cap_not_doubled(self):
        """尾盘订单逐 bar 使用流动性帽，同一 bar 不能重复发帽。

        14:56 回调创建的订单最早在 14:57 连续交易 bar 成交，剩余部分
        可继续在独立的 15:00 收盘竞价 bar 成交。按 ``simulated_time``
        聚合每根 bar 的全部 fill，既允许两根 bar 各有一份帽，也能检出
        同一根 bar 内重复 reset 后发放两份帽的历史回归。
        """

        class BuyHugeAtTail(Strategy):
            def on_minute(self, ctx, bar):
                t = str(bar.trade_time)
                if t.endswith('14:56:00'):
                    ctx.buy_at_market(shares=50_000_000, current_price=float(bar.close))

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240104',
            end_date='20240104',
            initial_cash=1_000_000_000.0,
            freq='minute',
            slippage=0.0,
        )
        result = engine.run(BuyHugeAtTail())

        provider = engine._data_provider
        minute = provider.get_minute('000001.SZ', trade_date='20240104',
                                     price_mode='execution')
        bars = resample_minute_data(minute, 1, symbol='000001.SZ')
        cap_by_time = {
            str(row['trade_time']).replace('T', ' ')[:19]:
            float(row['amount']) * 0.8
            for _, row in bars.iterrows()
        }
        buy_turnover_by_time = {}
        for trade in result.trades:
            if str(trade.get('direction', '')).upper() != 'BUY':
                continue
            stamp = str(trade['simulated_time']).replace('T', ' ')[:19]
            buy_turnover_by_time[stamp] = (
                buy_turnover_by_time.get(stamp, 0.0)
                + float(trade.get('amount') or 0)
            )

        assert set(buy_turnover_by_time) == {
            '2024-01-04 14:57:00',
            '2024-01-04 15:00:00',
        }
        for stamp, turnover in buy_turnover_by_time.items():
            cap = cap_by_time[stamp]
            assert turnover > 0
            assert turnover <= cap * 1.001, (
                f"{stamp} 成交额 {turnover:,.0f} 超过单根 bar 帽 "
                f"{cap:,.0f}（同一 bar 的帽可能被重复发放）"
            )

    def test_close_orders_execute_daily(self):
        """单标的日线引擎收盘单回归：buy_at_close 必须真的成交

        历史缺陷：单标的引擎从无 _execute_close_orders 调用点，
        收盘单静默挂到日终被撤，0 成交。
        """

        class BuyAtClose(Strategy):
            def on_day(self, ctx, bar):
                pos = ctx.get_position()
                if pos is None or pos.shares == 0:
                    ctx.buy_at_close(percent=0.5, current_price=float(bar.close))

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240105',
            initial_cash=100000.0,
            freq='daily',
        )
        result = engine.run(BuyAtClose())
        assert result.trade_count > 0, "收盘单应能成交（历史上静默0成交）"

    def test_close_orders_execute_minute(self):
        """单标的分钟引擎收盘单回归"""

        class BuyAtCloseMinute(Strategy):
            def on_minute(self, ctx, bar):
                pos = ctx.get_position()
                if (pos is None or pos.shares == 0) and str(bar.trade_time).endswith('14:30:00'):
                    ctx.buy_at_close(percent=0.5, current_price=float(bar.close))

        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240102',
            initial_cash=100000.0,
            freq='minute',
        )
        result = engine.run(BuyAtCloseMinute())
        assert result.trade_count > 0, "分钟模式收盘单应能成交"

    def test_engine_exposes_broker_params(self):
        """open_buy_resize_mode 与 liquidity_cap_ratio 可从引擎层传入"""
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240103',
            initial_cash=100000.0,
            freq='daily',
            open_buy_resize_mode='legacy',
            liquidity_cap_ratio=0.5,
        )
        engine.run(_BuyOnceStrategy())
        broker = engine._context._broker
        assert broker.open_buy_resize_mode == 'legacy'
        assert broker.liquidity_cap_ratio == 0.5
