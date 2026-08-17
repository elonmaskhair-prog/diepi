# -*- coding: utf-8 -*-
"""
Context 单元测试

测试回测上下文
"""

import pytest
import pandas as pd
from datetime import datetime

from diepi.backtest.engine.context import Context
from diepi.backtest.broker.broker import Broker, BarData as BrokerBarData
from diepi.backtest.broker.account import Account
from diepi.backtest.data.data_provider import DataProvider


class TestContextBasic:
    """基础功能测试"""

    def test_context_creation(self):
        """创建上下文"""
        ctx = Context(initial_cash=1000000.0)

        assert ctx.get_cash() == 1000000.0
        assert ctx.get_total_asset() == 1000000.0

    def test_current_date_time(self, context):
        """当前日期时间"""
        context.set_datetime('2024-01-02 09:30:00')

        assert context.current_date == '20240102'
        assert context.current_time is not None
        assert isinstance(context.current_time, datetime)

    def test_set_symbol(self, context):
        """设置当前股票"""
        context.set_symbol('000002.SZ')

        assert context.current_symbol == '000002.SZ'

    def test_set_market_phase(self, context):
        """设置交易时段"""
        context.set_market_phase('before_open')
        assert context._broker.is_before_market_open is True

        context.set_market_phase('trading')
        assert context._broker.is_trading is True

    def test_on_init_history_read_has_explicit_causal_error(self, monkeypatch):
        """No active simulation date must fail clearly, not inside bisect."""

        ctx = Context(initial_cash=1000000.0)
        ctx.set_symbol('000001.SZ')

        with pytest.raises(RuntimeError, match="unavailable during on_init"):
            ctx.get_daily(days=1)
        with pytest.raises(RuntimeError, match="unavailable during on_init"):
            ctx.get_minute(days=1)
        for query in (
            ctx.is_trade_day,
            ctx.get_prev_trade_day,
            ctx.get_next_trade_day,
        ):
            with pytest.raises(RuntimeError, match="unavailable during on_init"):
                query()

        monkeypatch.setattr(ctx._data, 'is_trade_day', lambda date: True)
        monkeypatch.setattr(
            ctx._data, 'get_prev_trade_day', lambda date, n=1: '20240101')
        monkeypatch.setattr(
            ctx._data, 'get_next_trade_day', lambda date, n=1: '20240103')
        assert ctx.is_trade_day('20240102') is True
        assert ctx.get_prev_trade_day('20240102') == '20240101'
        assert ctx.get_next_trade_day('20240102') == '20240103'


class TestContextTrading:
    """交易接口测试"""

    def test_buy_at_open(self, context):
        """开盘买入"""
        context.set_market_phase('before_open')

        order = context.buy_at_open(amount=50000.0)

        assert order is not None
        assert order.symbol == '000001.SZ'

    def test_sell_at_open(self, context):
        """开盘卖出"""
        context.set_market_phase('before_open')

        # 先建仓
        context._broker.account.freeze_cash(11000.0)
        context._broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        context._broker.account.settle_t1('20240102')

        order = context.sell_at_open(percent=1.0)

        assert order is not None

    def test_buy_at_market(self, context):
        """市价买入"""
        context.set_market_phase('trading')

        order = context.buy_at_market(shares=1000, current_price=10.0)

        assert order is not None

    def test_buy_at_market_derives_conservative_price_from_current_bar(
            self, context):
        context.set_datetime('2024-01-02 10:00:00')
        context.set_market_phase('trading')
        context._set_current_bar(BrokerBarData(
            symbol='000001.SZ',
            trade_time='2024-01-02 10:00:00',
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            amount=1_000_000.0,
            pre_close=10.0,
        ))

        order = context.buy_at_market(shares=100)

        assert order is not None
        assert order.frozen_amount > 0

    def test_sell_at_market(self, context):
        """市价卖出"""
        context.set_market_phase('trading')

        context._broker.account.freeze_cash(11000.0)
        context._broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        context._broker.account.settle_t1('20240102')

        order = context.sell_at_market(shares=500)

        assert order is not None

    def test_buy_at_price(self, context):
        """限价买入"""
        context.set_market_phase('trading')

        order = context.buy_at_price(price=9.5, shares=1000)

        assert order is not None
        # 价格可能经过 hfq -> raw 转换，只检查是否为正数
        assert order.price > 0

    def test_sell_at_price(self, context):
        """限价卖出"""
        context.set_market_phase('trading')

        context._broker.account.freeze_cash(11000.0)
        context._broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        context._broker.account.settle_t1('20240102')

        order = context.sell_at_price(price=10.5, shares=500)

        assert order is not None
        # 价格可能经过 hfq -> raw 转换，只检查是否为正数
        assert order.price > 0

    def test_buy_at_close(self, context):
        """收盘买入"""
        context.set_market_phase('trading')

        order = context.buy_at_close(shares=1000, current_price=10.0)

        assert order is not None

    def test_sell_at_close(self, context):
        """收盘卖出"""
        context.set_market_phase('trading')

        context._broker.account.freeze_cash(11000.0)
        context._broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        context._broker.account.settle_t1('20240102')

        order = context.sell_at_close(percent=1.0)

        assert order is not None

    def test_sell_stop_loss(self, context):
        """止损卖出"""
        context.set_market_phase('trading')

        context._broker.account.freeze_cash(11000.0)
        context._broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        context._broker.account.settle_t1('20240102')

        order = context.sell_stop_loss(price=9.0, percent=1.0)

        assert order is not None
        # 价格可能经过 hfq -> raw 转换，只检查是否为正数
        assert order.price > 0

    def test_sell_stop_profit(self, context):
        """止盈卖出"""
        context.set_market_phase('trading')

        context._broker.account.freeze_cash(11000.0)
        context._broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        context._broker.account.settle_t1('20240102')

        order = context.sell_stop_profit(price=12.0, percent=1.0)

        assert order is not None
        # 价格可能经过 hfq -> raw 转换，只检查是否为正数
        assert order.price > 0

    def test_buy_stop(self, context):
        """突破买入"""
        context.set_market_phase('trading')

        order = context.buy_stop(price=11.0, shares=1000, current_price=10.0)

        assert order is not None
        # 价格可能经过 hfq -> raw 转换，只检查是否为正数
        assert order.price > 0


class TestContextCancelOrders:
    """取消订单测试"""

    def test_cancel_order(self, context):
        """取消订单"""
        context.set_market_phase('trading')

        order = context.buy_at_price(price=9.0, shares=1000)
        success = context.cancel_order(order.order_id)

        assert success is True

    def test_cancel_orders(self, context):
        """批量取消订单"""
        context.set_market_phase('trading')

        order1 = context.buy_at_price(price=9.0, shares=1000)
        order2 = context.buy_at_price(price=9.5, shares=500)

        cancelled = context.cancel_orders()

        assert len(cancelled) == 2

    def test_cancel_all_orders(self, context):
        """取消所有订单"""
        context.set_market_phase('trading')

        context.buy_at_price(price=9.0, shares=1000)

        cancelled = context.cancel_all_orders()

        assert len(cancelled) >= 1


class TestContextQueries:
    """查询接口测试"""

    def test_get_cash(self, context):
        """获取现金"""
        cash = context.get_cash()
        assert cash > 0

    def test_get_total_asset(self, context):
        """获取总资产"""
        total = context.get_total_asset()
        assert total > 0

    def test_get_position(self, context):
        """获取持仓"""
        context._broker.account.freeze_cash(11000.0)
        context._broker.account.buy('000001.SZ', 1000, 10.0, '20240102')

        pos = context.get_position()

        assert pos is not None
        assert pos.shares == 1000

    def test_get_position_none(self, context):
        """获取无持仓"""
        pos = context.get_position('000999.SZ')

        assert pos is None

    def test_get_positions(self, context):
        """获取所有持仓"""
        positions = context.get_positions()

        assert isinstance(positions, dict)

    def test_get_open_orders(self, context):
        """获取未完成订单"""
        context.set_market_phase('trading')
        context.buy_at_price(price=9.0, shares=1000)

        orders = context.get_open_orders()

        assert len(orders) >= 1

    def test_get_stock_pool(self, context):
        """获取股票池"""
        pool = context.get_stock_pool()

        assert isinstance(pool, list)
        assert '000001.SZ' in pool


@pytest.mark.integration
class TestContextData:
    """数据接口测试"""

    def test_get_daily(self, context):
        """获取日线数据"""
        context.set_datetime('2024-01-15 09:30:00')
        context.set_market_phase('trading')

        df = context.get_daily(days=5)

        assert isinstance(df, pd.DataFrame)

    def test_get_minute(self, context):
        """获取分钟数据"""
        context.set_datetime('2024-01-15 10:00:00')
        context.set_market_phase('trading')

        df = context.get_minute(days=1)

        assert isinstance(df, pd.DataFrame)

    def test_get_stock_info(self, context):
        """获取股票信息"""
        info = context.get_stock_info()

        assert info is not None

    def test_is_trade_day(self, context):
        """判断交易日"""
        result = context.is_trade_day('20240102')

        assert isinstance(result, bool)

    def test_get_prev_trade_day(self, context):
        """获取前一交易日"""
        prev = context.get_prev_trade_day('20240103')

        assert prev is not None

    def test_get_next_trade_day(self, context):
        """获取后一交易日"""
        next_day = context.get_next_trade_day('20240102')

        assert next_day is not None


class TestContextDateBoundary:
    """数据边界测试"""

    @pytest.mark.integration
    def test_daily_boundary_before_open(self, context):
        """盘前日线边界"""
        context.set_datetime('2024-01-03 09:00:00')
        context.set_market_phase('before_open')

        boundary = context._get_daily_boundary()

        # 盘前只能看到 T-1
        assert boundary < '20240103'

    def test_daily_boundary_after_close(self, context):
        """盘后日线边界"""
        context.set_datetime('2024-01-03 15:30:00')
        context.set_market_phase('after_close')

        boundary = context._get_daily_boundary()

        # 盘后可以看到当日
        assert boundary == '20240103'

    def test_minute_boundary_trading(self, context):
        """盘中分钟边界"""
        context.set_datetime('2024-01-03 10:30:00')
        context.set_market_phase('trading')

        end_date, end_time = context._get_minute_boundary()

        # 盘中只能看到当前时间之前
        assert end_date == '20240103'
        assert end_time is not None

    def test_minute_boundary_after_open_excludes_current_0930_bar(
            self, context, monkeypatch):
        """on_after_open only knows the opening snapshot, not 09:30 OHLCV."""
        context.set_datetime('2024-01-03 09:30:00')
        context.set_market_phase('trading')
        monkeypatch.setattr(
            context, 'get_prev_trade_day', lambda date, n=1: '20240102'
        )

        with context._broker.order_creation_scope(
                'on_after_open', context.current_time):
            end_date, end_time = context._get_minute_boundary()

        assert (end_date, end_time) == ('20240102', None)


class TestContextSymbolRequired:
    """股票代码必需测试"""

    def test_buy_without_symbol(self):
        """买入无股票代码"""
        ctx = Context(initial_cash=1000000.0)
        ctx.set_datetime('2024-01-02 09:30:00')
        ctx.set_market_phase('before_open')
        # 不设置 current_symbol

        with pytest.raises(ValueError, match="Symbol required"):
            ctx.buy_at_open(amount=50000.0)

    def test_sell_without_symbol(self):
        """卖出无股票代码"""
        ctx = Context(initial_cash=1000000.0)
        ctx.set_datetime('2024-01-02 09:30:00')
        ctx.set_market_phase('before_open')

        with pytest.raises(ValueError, match="Symbol required"):
            ctx.sell_at_open(percent=1.0)


class TestContextInternalMethods:
    """内部方法测试"""

    @pytest.mark.integration
    def test_on_day_start(self, context):
        """日开始处理"""
        context._broker.account.freeze_cash(11000.0)
        context._broker.account.buy('000001.SZ', 1000, 10.0, '20240101')

        context._on_day_start()

        # T+1 结算应该执行
        pos = context.get_position()
        assert pos.available_shares == 1000

    def test_on_day_end(self, context):
        """日结束处理"""
        context.set_market_phase('trading')
        context.buy_at_price(price=9.0, shares=1000)

        context._on_day_end()

        # 未完成订单应该被取消
        orders = context.get_open_orders()
        assert len(orders) == 0

    def test_update_prices(self, context):
        """更新价格"""
        context._broker.account.freeze_cash(11000.0)
        context._broker.account.buy('000001.SZ', 1000, 10.0, '20240102')

        context._update_prices({'000001.SZ': 12.0})

        pos = context.get_position()
        assert pos.current_price == 12.0
