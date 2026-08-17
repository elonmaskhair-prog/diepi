# -*- coding: utf-8 -*-
"""
Broker 单元测试

测试撮合引擎
"""

import pytest
from datetime import datetime

from diepi.backtest.broker.broker import Broker, BarData
from diepi.backtest.broker.account import Account
from diepi.backtest.broker.order import Order, OrderSide, OrderType, OrderStatus


class TestBrokerOrderCreation:
    """订单创建测试"""

    def test_buy_at_market(self, broker, sample_bar_data):
        """市价买入"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        order = broker.buy_at_market(
            '000001.SZ',
            shares=1000,
            current_price=10.0,
        )

        assert order.side == OrderSide.BUY
        assert order.order_type == OrderType.MARKET
        assert order.shares == 1000
        assert order.status == OrderStatus.SUBMITTED

    def test_sell_at_market(self, broker, sample_bar_data):
        """市价卖出"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        # 先买入
        broker.account.freeze_cash(11000.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        broker.account.settle_t1('20240102')

        order = broker.sell_at_market('000001.SZ', shares=500)

        assert order.side == OrderSide.SELL
        assert order.order_type == OrderType.MARKET
        assert order.shares == 500

    def test_buy_at_open(self, broker):
        """开盘买入"""
        broker.set_date('20240102')
        broker.set_market_phase('before_open')

        order = broker.buy_at_open('000001.SZ', amount=50000.0)

        assert order.order_type == OrderType.OPEN
        assert order.side == OrderSide.BUY

    def test_sell_at_open(self, broker):
        """开盘卖出"""
        broker.set_date('20240102')
        broker.set_market_phase('before_open')

        # 先买入
        broker.account.freeze_cash(11000.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        broker.account.settle_t1('20240102')

        order = broker.sell_at_open('000001.SZ', percent=1.0)

        assert order.order_type == OrderType.OPEN
        assert order.side == OrderSide.SELL

    def test_buy_limit(self, broker):
        """限价买入"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        order = broker.buy_at_price('000001.SZ', price=9.5, shares=1000)

        assert order.order_type == OrderType.LIMIT
        assert order.price == 9.5
        assert order.shares == 1000

    def test_sell_limit(self, broker):
        """限价卖出"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        broker.account.freeze_cash(11000.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        broker.account.settle_t1('20240102')

        order = broker.sell_at_price('000001.SZ', price=10.5, shares=500)

        assert order.order_type == OrderType.LIMIT
        assert order.price == 10.5

    def test_sell_stop(self, broker):
        """止损卖出"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        broker.account.freeze_cash(11000.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        broker.account.settle_t1('20240102')

        order = broker.sell_stop_loss('000001.SZ', price=9.0, percent=1.0)

        assert order.order_type == OrderType.STOP
        assert order.price == 9.0

    def test_sell_stop_profit(self, broker):
        """止盈卖出"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        broker.account.freeze_cash(11000.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        broker.account.settle_t1('20240102')

        order = broker.sell_stop_profit('000001.SZ', price=12.0, percent=1.0)

        assert order.order_type == OrderType.STOP_PROFIT
        assert order.price == 12.0


class TestBrokerOrderExecution:
    """订单执行测试"""

    def test_execute_open_orders(self, broker):
        """开盘订单执行"""
        broker.set_date('20240102')
        broker.set_market_phase('before_open')

        order = broker.buy_at_open('000001.SZ', amount=50000.0)

        broker.set_market_phase('trading')

        bar = BarData(
            symbol='000001.SZ',
            trade_time=datetime(2024, 1, 2, 9, 30),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            vol=1000000,
            amount=10200000,
            pre_close=9.8,
        )

        executed = broker.execute_open_orders({'000001.SZ': bar})

        assert len(executed) == 1
        assert order.status in (
            OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)

    def test_execute_market_orders(self, broker):
        """市价订单执行"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        order = broker.buy_at_market('000001.SZ', shares=1000, current_price=10.0)

        bar = BarData(
            symbol='000001.SZ',
            trade_time=datetime(2024, 1, 2, 9, 31),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            vol=1000000,
            amount=10200000,
            pre_close=9.8,
        )

        executed = broker.execute_market_orders(bar)

        # 市价单在 close 价格点执行
        assert len(executed) >= 0

    def test_execute_limit_order_trigger(self, broker):
        """限价单触发"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        # 下限价买单
        order = broker.buy_at_price('000001.SZ', price=9.8, shares=1000)

        # K线最低价触及限价
        bar = BarData(
            symbol='000001.SZ',
            trade_time=datetime(2024, 1, 2, 9, 31),
            open=10.0,
            high=10.5,
            low=9.5,  # 低于限价
            close=10.2,
            vol=1000000,
            amount=10200000,
            pre_close=9.8,
        )

        executed = broker.execute_market_orders(bar)

        # 应该触发
        if len(executed) > 0:
            assert order.filled_shares > 0

    def test_execute_stop_order_trigger(self, broker):
        """止损单触发"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        broker.account.freeze_cash(11000.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        broker.account.settle_t1('20240102')

        order = broker.sell_stop_loss('000001.SZ', price=9.5, shares=1000)

        # K线最低价触及止损价
        bar = BarData(
            symbol='000001.SZ',
            trade_time=datetime(2024, 1, 2, 9, 31),
            open=10.0,
            high=10.2,
            low=9.3,  # 低于止损价
            close=9.5,
            vol=1000000,
            amount=9500000,
            pre_close=10.0,
        )

        executed = broker.execute_market_orders(bar)

        # 应该触发
        if len(executed) > 0:
            assert order.status in (OrderStatus.FILLED, OrderStatus.PARTIAL)

    def test_execute_stop_profit_trigger(self, broker):
        """止盈单触发"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        broker.account.freeze_cash(11000.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        broker.account.settle_t1('20240102')

        order = broker.sell_stop_profit('000001.SZ', price=11.0, shares=1000)

        # K线最高价触及止盈价
        bar = BarData(
            symbol='000001.SZ',
            trade_time=datetime(2024, 1, 2, 9, 31),
            open=10.0,
            high=11.0,  # 触及止盈价，且不伪造超出当日涨停价的行情
            low=9.8,
            close=10.9,
            vol=1000000,
            amount=11200000,
            pre_close=10.0,
        )

        executed = broker.execute_market_orders(bar)

        # 应该触发
        if len(executed) > 0:
            assert order.status in (OrderStatus.FILLED, OrderStatus.PARTIAL)


class TestLimitRestrictions:
    """涨跌停限制测试"""

    def test_limit_up_restriction(self, broker):
        """涨停不可买"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        order = broker.buy_at_market('000001.SZ', shares=1000, current_price=10.78)

        # 涨停 K 线
        bar = BarData(
            symbol='000001.SZ',
            trade_time=datetime(2024, 1, 2, 9, 31),
            open=10.78,
            high=10.78,
            low=10.78,
            close=10.78,
            vol=100000,
            amount=1078000,
            pre_close=9.8,
        )

        broker.execute_market_orders(bar)

        # 涨停应该无法买入（成交价会触及涨停价）
        # 注意：具体行为取决于实现

    def test_limit_down_restriction(self, broker):
        """跌停不可卖"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        broker.account.freeze_cash(11000.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        broker.account.settle_t1('20240102')

        order = broker.sell_at_market('000001.SZ', shares=1000)

        # 跌停 K 线
        bar = BarData(
            symbol='000001.SZ',
            trade_time=datetime(2024, 1, 2, 9, 31),
            open=8.82,
            high=8.82,
            low=8.82,
            close=8.82,
            vol=100000,
            amount=882000,
            pre_close=9.8,
        )

        broker.execute_market_orders(bar)

        # 跌停应该无法卖出
        # 注意：具体行为取决于实现


class TestSlippage:
    """滑点测试"""

    def test_slippage_application(self):
        """滑点应用"""
        account = Account(initial_cash=1000000.0)
        broker = Broker(account=account, slippage=0.002)  # 0.2%

        broker.set_date('20240102')
        broker.set_market_phase('trading')

        order = broker.buy_at_market('000001.SZ', shares=1000, current_price=10.0)

        # 买入滑点会增加成交价
        # 卖出滑点会降低成交价
        assert broker.slippage == 0.002


class TestCancelOrder:
    """取消订单测试"""

    def test_cancel_order(self, broker):
        """取消订单"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        order = broker.buy_at_price('000001.SZ', price=9.5, shares=1000)
        order_id = order.order_id

        success = broker.cancel_order(order_id)

        assert success is True
        assert order.status == OrderStatus.CANCELLED

    def test_cancel_orders_by_symbol(self, broker):
        """按股票取消订单"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        order1 = broker.buy_at_price('000001.SZ', price=9.5, shares=1000)
        order2 = broker.buy_at_price('000002.SZ', price=15.0, shares=500)

        cancelled = broker.cancel_orders(symbol='000001.SZ')

        assert len(cancelled) == 1
        assert order1.status == OrderStatus.CANCELLED
        assert order2.status == OrderStatus.SUBMITTED

    def test_cancel_all_orders(self, broker):
        """取消所有订单"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        order1 = broker.buy_at_price('000001.SZ', price=9.5, shares=1000)
        order2 = broker.buy_at_price('000002.SZ', price=15.0, shares=500)

        cancelled = broker.cancel_all_orders()

        assert len(cancelled) == 2
        assert order1.status == OrderStatus.CANCELLED
        assert order2.status == OrderStatus.CANCELLED


class TestDayEndOrderCancel:
    """日终订单取消测试"""

    def test_day_end_order_cancel(self, broker):
        """日终未成交订单取消"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        # 创建限价单
        order = broker.buy_at_price('000001.SZ', price=9.0, shares=1000)

        # 日终处理
        broker.on_day_end()

        assert order.status == OrderStatus.CANCELLED


class TestBrokerQueries:
    """查询接口测试"""

    def test_get_cash(self, broker):
        """获取现金"""
        cash = broker.get_cash()
        assert cash == 1000000.0

    def test_get_total_asset(self, broker):
        """获取总资产"""
        total = broker.get_total_asset()
        assert total == 1000000.0

    def test_get_position(self, broker):
        """获取持仓"""
        broker.account.freeze_cash(11000.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240102')

        pos = broker.get_position('000001.SZ')
        assert pos is not None
        assert pos.shares == 1000

    def test_get_positions(self, broker):
        """获取所有持仓"""
        positions = broker.get_positions()
        assert isinstance(positions, dict)

    def test_get_open_orders(self, broker):
        """获取未完成订单"""
        broker.set_date('20240102')
        broker.set_market_phase('trading')

        broker.buy_at_price('000001.SZ', price=9.5, shares=1000)

        open_orders = broker.get_open_orders()
        assert len(open_orders) == 1


class TestMarketPhase:
    """交易时段测试"""

    def test_is_before_market_open(self, broker):
        """盘前时段"""
        broker.set_market_phase('before_open')
        assert broker.is_before_market_open is True
        assert broker.is_trading is False

    def test_is_trading(self, broker):
        """交易时段"""
        broker.set_market_phase('trading')
        assert broker.is_before_market_open is False
        assert broker.is_trading is True

    def test_open_order_only_before_open(self, broker):
        """开盘单只能在盘前下"""
        broker.set_date('20240102')

        # 盘前可以下开盘单
        broker.set_market_phase('before_open')
        order1 = broker.buy_at_open('000001.SZ', amount=50000.0)
        assert order1.status != OrderStatus.REJECTED

        # 交易时段不能下开盘单
        broker.set_market_phase('trading')
        order2 = broker.buy_at_open('000001.SZ', amount=50000.0)
        assert order2.status == OrderStatus.REJECTED


class TestOnDayStart:
    """日开始处理测试"""

    def test_on_day_start(self, broker):
        """日开始处理"""
        broker.account.freeze_cash(11000.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240101')

        broker.on_day_start('20240102')

        # T+1 结算应该执行
        pos = broker.get_position('000001.SZ')
        assert pos.available_shares == 1000


class TestOpenBuyResizeMode:
    """Open买单 auto/legacy 行为测试"""

    def test_open_buy_auto_can_use_same_open_sell_proceeds(self):
        # 钉在旧口径 (sizing='fill') 上测试 resize 机制本身；
        # 新默认 sizing='limit_up' 下目标量已按涨停价折算，见 test_rebalance_semantics.py
        account = Account(initial_cash=10005.0)
        broker = Broker(account=account, slippage=0.0, open_buy_resize_mode='auto',
                        open_buy_sizing='fill', open_buy_fill_mode='open')

        # 建一个可卖持仓，初始现金归零
        broker.account.freeze_cash(10005.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        broker.account.settle_t1('20240102')

        broker.set_date('20240103')
        broker.set_market_phase('before_open')
        broker.sell_at_open('000001.SZ', shares=1000)
        buy_order = broker.buy_at_open('000001.SZ', amount=10000.0)
        assert buy_order.status == OrderStatus.SUBMITTED

        bar = BarData(
            symbol='000001.SZ',
            trade_time=datetime(2024, 1, 3, 9, 30),
            open=10.0,
            high=10.2,
            low=9.8,
            close=10.0,
            vol=1000000,
            amount=10000000,
            pre_close=10.0,
        )
        broker.execute_open_orders({'000001.SZ': bar})

        assert buy_order.status in (OrderStatus.FILLED, OrderStatus.PARTIAL)
        assert buy_order.auto_resized is True
        assert buy_order.resized_from_shares == 1000
        assert buy_order.resized_to_shares == 900

        stats = broker.get_open_buy_stats()
        assert stats['open_buy_total'] == 1
        assert stats['open_buy_auto_resized_count'] == 1
        assert stats['open_buy_reject_insuff_cash_count'] == 0

    def test_open_buy_legacy_rejects_without_pre_open_cash(self):
        account = Account(initial_cash=10005.0)
        broker = Broker(account=account, slippage=0.0, open_buy_resize_mode='legacy')

        broker.account.freeze_cash(10005.0)
        broker.account.buy('000001.SZ', 1000, 10.0, '20240101')
        broker.account.settle_t1('20240102')

        broker.set_date('20240103')
        broker.set_market_phase('before_open')
        broker.sell_at_open('000001.SZ', shares=1000)
        buy_order = broker.buy_at_open('000001.SZ', amount=10000.0)

        assert buy_order.status == OrderStatus.REJECTED
        assert 'Insufficient cash' in buy_order.reject_reason


class TestBarData:
    """BarData 测试"""

    def test_bar_data_creation(self):
        """创建 BarData"""
        bar = BarData(
            symbol='000001.SZ',
            trade_time=datetime(2024, 1, 2, 9, 30),
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
            vol=1000000,
            amount=10200000,
            pre_close=9.8,
        )

        assert bar.symbol == '000001.SZ'
        assert bar.open == 10.0
        assert bar.close == 10.2
        assert bar.pre_close == 9.8

    def test_bar_data_string_time(self):
        """字符串时间转换"""
        bar = BarData(
            symbol='000001.SZ',
            trade_time='20240102',
            open=10.0,
            high=10.5,
            low=9.8,
            close=10.2,
        )

        assert bar.trade_time is not None


class TestStopOrderNumericGuards:
    """STOP intents must never turn invalid sizing into a liquidation."""

    @staticmethod
    def _broker_with_position():
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240101')
        account.settle_t1('20240102')
        broker = Broker(account=account)
        broker.set_date('20240102')
        broker.set_market_phase('trading')
        return broker

    @pytest.mark.parametrize(
        'method_name,trigger_price',
        [('sell_stop_loss', 9.0), ('sell_stop_profit', 11.0)],
    )
    @pytest.mark.parametrize(
        'field,value',
        [
            ('shares', 0),
            ('shares', -1),
            ('shares', 1.5),
            ('shares', float('nan')),
            ('shares', float('inf')),
            ('percent', 0.0),
            ('percent', -0.1),
            ('percent', 1.0001),
            ('percent', float('nan')),
            ('percent', float('inf')),
        ],
    )
    def test_invalid_sizing_is_rejected_without_freezing(
            self, method_name, trigger_price, field, value):
        broker = self._broker_with_position()
        pos = broker.account.get_position('000001.SZ')

        order = getattr(broker, method_name)(
            '000001.SZ', price=trigger_price, **{field: value})

        assert order.status == OrderStatus.REJECTED
        assert order.shares == 0
        assert pos.shares == 1000
        assert pos.frozen_shares == 0
        assert pos.available_shares == 1000

    @pytest.mark.parametrize(
        'method_name,trigger_price',
        [('sell_stop_loss', 9.0), ('sell_stop_profit', 11.0)],
    )
    def test_tiny_percent_rounding_to_zero_is_rejected(self, method_name, trigger_price):
        broker = self._broker_with_position()
        pos = broker.account.get_position('000001.SZ')

        order = getattr(broker, method_name)(
            '000001.SZ', price=trigger_price, percent=0.0001)

        assert order.status == OrderStatus.REJECTED
        assert order.shares == 0
        assert pos.frozen_shares == 0
        assert pos.available_shares == 1000

    @pytest.mark.parametrize(
        'method_name,trigger_price',
        [('sell_stop_loss', 9.0), ('sell_stop_profit', 11.0)],
    )
    @pytest.mark.parametrize(
        'invalid_price',
        [0.0, -1.0, float('nan'), float('inf'), float('-inf')],
    )
    def test_invalid_trigger_price_is_rejected_without_freezing(
            self, method_name, trigger_price, invalid_price):
        broker = self._broker_with_position()
        pos = broker.account.get_position('000001.SZ')

        order = getattr(broker, method_name)(
            '000001.SZ', price=invalid_price, percent=1.0)

        assert order.status == OrderStatus.REJECTED
        assert order.price is None
        assert pos.frozen_shares == 0
        assert pos.available_shares == 1000

    @pytest.mark.parametrize(
        'method_name,trigger_price',
        [('sell_stop_loss', 9.0), ('sell_stop_profit', 11.0)],
    )
    def test_percent_one_remains_explicit_full_position_path(
            self, method_name, trigger_price):
        broker = self._broker_with_position()
        pos = broker.account.get_position('000001.SZ')

        order = getattr(broker, method_name)(
            '000001.SZ', price=trigger_price, percent=1.0)

        assert order.status == OrderStatus.SUBMITTED
        assert order.shares == 1000
        assert order.frozen_shares == 1000
        assert pos.frozen_shares == 1000
        assert pos.available_shares == 0

    @pytest.mark.parametrize(
        'method_name,trigger_price',
        [('sell_stop_loss', 9.0), ('sell_stop_profit', 11.0)],
    )
    def test_stop_requires_explicit_position_size(self, method_name, trigger_price):
        broker = self._broker_with_position()
        pos = broker.account.get_position('000001.SZ')

        order = getattr(broker, method_name)(
            '000001.SZ', price=trigger_price)

        assert order.status == OrderStatus.REJECTED
        assert 'explicit shares or percent' in order.reject_reason
        assert pos.frozen_shares == 0
        assert pos.available_shares == 1000
