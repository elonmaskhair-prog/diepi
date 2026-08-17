# -*- coding: utf-8 -*-
"""
Order 单元测试

测试订单类
"""

import pytest
from datetime import datetime

from diepi.backtest.broker.order import Order, OrderType, OrderSide, OrderStatus


class TestOrder:
    """订单类测试"""

    def test_create_buy_order(self):
        """创建买单"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        assert order.symbol == '000001.SZ'
        assert order.side == OrderSide.BUY
        assert order.is_buy is True
        assert order.is_sell is False
        assert order.shares == 1000

    def test_create_sell_order(self):
        """创建卖单"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            shares=500,
        )
        assert order.side == OrderSide.SELL
        assert order.is_buy is False
        assert order.is_sell is True

    def test_order_types(self):
        """订单类型"""
        # OPEN
        order_open = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.OPEN,
            shares=100,
        )
        assert order_open.order_type == OrderType.OPEN

        # CLOSE
        order_close = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.CLOSE,
            shares=100,
        )
        assert order_close.order_type == OrderType.CLOSE

        # MARKET
        order_market = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=100,
        )
        assert order_market.order_type == OrderType.MARKET

        # LIMIT
        order_limit = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            shares=100,
            price=10.0,
        )
        assert order_limit.order_type == OrderType.LIMIT
        assert order_limit.price == 10.0

        # STOP
        order_stop = Order(
            symbol='000001.SZ',
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            shares=100,
            price=9.0,
        )
        assert order_stop.order_type == OrderType.STOP

    def test_order_status_lifecycle(self):
        """状态流转: PENDING → SUBMITTED → FILLED"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )

        # 初始状态
        assert order.status == OrderStatus.PENDING
        assert order.is_open is True
        assert order.is_done is False

        # 提交
        order.submit()
        assert order.status == OrderStatus.SUBMITTED
        assert order.is_open is True

        # 成交
        order.fill(1000, 10.0)
        assert order.status == OrderStatus.FILLED
        assert order.is_open is False
        assert order.is_done is True

    def test_mark_filled(self):
        """完全成交"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        order.submit()
        order.fill(1000, 10.5)

        assert order.status == OrderStatus.FILLED
        assert order.filled_shares == 1000
        assert order.filled_amount == 10500.0
        assert order.avg_price == 10.5
        assert order.unfilled_shares == 0

    def test_mark_partial(self):
        """部分成交"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        order.submit()
        order.fill(500, 10.0)

        assert order.status == OrderStatus.PARTIAL
        assert order.filled_shares == 500
        assert order.filled_amount == 5000.0
        assert order.unfilled_shares == 500
        assert order.is_open is True  # 部分成交仍然算开放

    def test_mark_rejected(self):
        """拒绝订单"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        order.reject("资金不足")

        assert order.status == OrderStatus.REJECTED
        assert order.reject_reason == "资金不足"
        assert order.is_done is True

    def test_partially_filled_order_cannot_be_rejected(self):
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            shares=1000,
            price=10.0,
        ).submit().fill(300, 10.0)

        with pytest.raises(ValueError, match="cannot reject an order with fills"):
            order.reject("late validation veto")

        assert order.status == OrderStatus.PARTIAL
        assert order.filled_shares == 300
        assert order.reject_reason == ''

        order.cancel()
        assert order.status == OrderStatus.CANCELLED
        assert order.filled_shares == 300

    def test_can_cancel(self):
        """可取消判断"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )

        # 待提交状态可以取消
        assert order.is_open is True
        order.cancel()
        assert order.status == OrderStatus.CANCELLED
        assert order.is_done is True

    def test_cancel_submitted_order(self):
        """取消已提交订单"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            shares=1000,
            price=10.0,
        )
        order.submit()
        order.cancel()

        assert order.status == OrderStatus.CANCELLED

    def test_cannot_cancel_filled(self):
        """已成交订单不能取消"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        order.submit()
        order.fill(1000, 10.0)

        # 尝试取消（应该保持 FILLED）
        order.cancel()
        assert order.status == OrderStatus.FILLED

    def test_unfilled_shares(self):
        """未成交股数计算"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )

        assert order.unfilled_shares == 1000

        order.submit()
        order.fill(300, 10.0)
        assert order.unfilled_shares == 700

        order.fill(700, 10.1)
        assert order.unfilled_shares == 0


class TestOrderMultipleFills:
    """多次成交测试"""

    def test_multiple_partial_fills(self):
        """多次部分成交"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        order.submit()

        # 第一次部分成交
        order.fill(200, 10.0)
        assert order.status == OrderStatus.PARTIAL
        assert order.filled_shares == 200
        assert order.avg_price == 10.0

        # 第二次部分成交
        order.fill(300, 10.2)
        assert order.status == OrderStatus.PARTIAL
        assert order.filled_shares == 500
        # 加权平均价: (200*10 + 300*10.2) / 500 = 10.12
        assert abs(order.avg_price - 10.12) < 0.001

        # 第三次完全成交
        order.fill(500, 10.1)
        assert order.status == OrderStatus.FILLED
        assert order.filled_shares == 1000


class TestOrderToDict:
    """序列化测试"""

    def test_to_dict(self):
        """转换为字典"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            shares=1000,
            price=10.0,
            note='测试订单',
        )
        order.submit()
        order.fill(500, 10.0)

        d = order.to_dict()

        assert d['symbol'] == '000001.SZ'
        assert d['side'] == 'buy'
        assert d['order_type'] == 'limit'
        assert d['price'] == 10.0
        assert d['shares'] == 1000
        assert d['filled_shares'] == 500
        assert 'requested_shares' in d
        assert 'requested_amount' in d
        assert 'requested_percent' in d
        assert 'auto_resized' in d
        assert d['note'] == '测试订单'


class TestOrderEnums:
    """枚举测试"""

    def test_order_type_values(self):
        """订单类型枚举值"""
        assert OrderType.OPEN.value == 'open'
        assert OrderType.CLOSE.value == 'close'
        assert OrderType.MARKET.value == 'market'
        assert OrderType.LIMIT.value == 'limit'
        assert OrderType.STOP.value == 'stop'
        assert OrderType.STOP_PROFIT.value == 'stop_profit'

    def test_order_side_values(self):
        """买卖方向枚举值"""
        assert OrderSide.BUY.value == 'buy'
        assert OrderSide.SELL.value == 'sell'

    def test_order_status_values(self):
        """订单状态枚举值"""
        assert OrderStatus.PENDING.value == 'pending'
        assert OrderStatus.SUBMITTED.value == 'submitted'
        assert OrderStatus.FILLED.value == 'filled'
        assert OrderStatus.PARTIAL.value == 'partial'
        assert OrderStatus.REJECTED.value == 'rejected'
        assert OrderStatus.CANCELLED.value == 'cancelled'


class TestOrderRepr:
    """字符串表示测试"""

    def test_repr(self):
        """__repr__ 方法"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        repr_str = repr(order)

        assert '000001.SZ' in repr_str
        assert 'buy' in repr_str
        assert 'market' in repr_str
        assert '1000' in repr_str


class TestOrderTimestamps:
    """时间戳测试"""

    def test_create_time_auto(self):
        """创建时间自动设置"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        assert order.create_time is not None
        assert isinstance(order.create_time, datetime)

    def test_update_time_on_fill(self):
        """成交时更新时间"""
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        original_time = order.update_time

        order.submit()
        order.fill(1000, 10.0)

        assert order.update_time >= original_time
