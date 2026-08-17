# -*- coding: utf-8 -*-
"""
Account 单元测试

测试账户类
"""

import pytest
from datetime import datetime

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.position import Position
from diepi.backtest.broker.order import Order, OrderSide, OrderType, OrderStatus


class TestAccount:
    """账户类测试"""

    def test_initial_state(self):
        """初始状态"""
        account = Account(initial_cash=1000000.0)

        assert account.initial_cash == 1000000.0
        assert account.cash == 1000000.0
        assert account.frozen_cash == 0.0
        assert account.market_value == 0.0
        assert account.total_asset == 1000000.0
        assert len(account.positions) == 0

    def test_freeze_cash(self):
        """冻结资金 (买单)"""
        account = Account(initial_cash=100000.0)

        success = account.freeze_cash(30000.0)

        assert success is True
        assert account.cash == 70000.0
        assert account.frozen_cash == 30000.0
        assert account.total_asset == 100000.0  # 总资产不变

    def test_freeze_cash_insufficient(self):
        """冻结资金不足"""
        account = Account(initial_cash=100000.0)

        success = account.freeze_cash(150000.0)

        assert success is False
        assert account.cash == 100000.0
        assert account.frozen_cash == 0.0

    def test_unfreeze_cash(self):
        """解冻资金"""
        account = Account(initial_cash=100000.0)
        account.freeze_cash(30000.0)

        account.unfreeze_cash(10000.0)

        assert account.cash == 80000.0
        assert account.frozen_cash == 20000.0

    def test_unfreeze_cash_exceeds(self):
        """解冻超过冻结金额"""
        account = Account(initial_cash=100000.0)
        account.freeze_cash(30000.0)

        account.unfreeze_cash(50000.0)

        # 只解冻实际冻结的金额
        assert account.cash == 100000.0
        assert account.frozen_cash == 0.0


class TestAccountBuy:
    """买入测试"""

    def test_buy_execution(self):
        """买入执行"""
        account = Account(initial_cash=100000.0)

        # 先冻结资金
        account.freeze_cash(11000.0)

        # 买入
        cost = account.buy('000001.SZ', 1000, 10.0, '20240102')

        # 检查持仓
        pos = account.get_position('000001.SZ')
        assert pos is not None
        assert pos.shares == 1000
        assert pos.avg_cost > 0

        # 检查资金
        assert account.frozen_cash == 11000.0 - cost

    def test_buy_creates_position(self):
        """买入创建持仓"""
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)

        account.buy('000001.SZ', 1000, 10.0, '20240102')

        assert '000001.SZ' in account.positions
        pos = account.positions['000001.SZ']
        assert pos.shares == 1000

    def test_buy_adds_to_position(self):
        """买入增加持仓"""
        account = Account(initial_cash=100000.0)

        # 第一次买入
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240102')

        # 第二次买入
        account.freeze_cash(13000.0)
        account.buy('000001.SZ', 1000, 12.0, '20240103')

        pos = account.get_position('000001.SZ')
        assert pos.shares == 2000

    def test_buy_requires_full_frozen_cash_by_default(self):
        """Public settlement must not create a position without paying for it."""
        account = Account(initial_cash=100000.0)

        with pytest.raises(ValueError, match="exceeds frozen_cash"):
            account.buy('000001.SZ', 1000, 10.0, '20240102')

        assert account.cash == 100000.0
        assert account.frozen_cash == 0.0
        assert account.get_position('000001.SZ') is None

    def test_buy_legacy_non_strict_mode_is_explicit(self):
        """The old fixture/seeding behavior remains opt-in for compatibility."""
        account = Account(initial_cash=100000.0)

        account.buy(
            '000001.SZ', 1000, 10.0, '20240102',
            strict_settlement=False,
        )

        assert account.cash == 100000.0
        assert account.get_position('000001.SZ').shares == 1000


class TestAccountSell:
    """卖出测试"""

    def test_sell_execution(self):
        """卖出执行"""
        account = Account(initial_cash=100000.0)

        # 买入
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240101')

        # T+1 结算
        account.settle_t1('20240102')

        # 卖出
        proceeds = account.sell('000001.SZ', 500, 12.0)

        assert proceeds > 0
        pos = account.get_position('000001.SZ')
        assert pos.shares == 500

    def test_sell_clears_position(self):
        """全部卖出清除持仓"""
        account = Account(initial_cash=100000.0)

        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240101')
        account.settle_t1('20240102')

        account.sell('000001.SZ', 1000, 12.0)

        assert '000001.SZ' not in account.positions

    def test_sell_no_position(self):
        """无持仓卖出"""
        account = Account(initial_cash=100000.0)

        proceeds = account.sell(
            '000001.SZ', 1000, 10.0, strict_settlement=False)

        assert proceeds == 0.0

    def test_sell_requires_exact_available_position_by_default(self):
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240101')
        account.settle_t1('20240102')
        position = account.get_position('000001.SZ')
        before = (account.cash, position.shares, position.cost_basis)

        with pytest.raises(ValueError, match="exceed available_shares"):
            account.sell('000001.SZ', 1001, 12.0, '20240102')

        assert (account.cash, position.shares, position.cost_basis) == before


class TestCommission:
    """手续费测试"""

    def test_commission_calculation(self):
        """佣金计算 (万2.5, 最低5元)"""
        account = Account(
            initial_cash=100000.0,
            commission_rate=0.00025,
            min_commission=5.0,
        )

        # 大额交易
        commission = account.calc_commission(100000.0, is_sell=False)
        assert commission == 25.0  # 100000 * 0.00025 = 25

        # 小额交易（低于最低佣金）
        commission_small = account.calc_commission(10000.0, is_sell=False)
        assert commission_small == 5.0  # 最低佣金

    def test_stamp_duty(self):
        """印花税 (卖出千1)"""
        account = Account(
            initial_cash=100000.0,
            commission_rate=0.00025,
            stamp_duty_rate=0.001,
            min_commission=5.0,
        )

        # 卖出时包含印花税
        commission = account.calc_commission(100000.0, is_sell=True)
        # 佣金 25 + 印花税 100 = 125
        assert commission == 125.0

        # 买入不含印花税
        commission_buy = account.calc_commission(100000.0, is_sell=False)
        assert commission_buy == 25.0

    @pytest.mark.parametrize(
        'trade_date',
        ['20230828', '2023-08-28', '2023/08/28', 20230828, 20230828.0],
    )
    def test_auto_stamp_duty_normalizes_public_date_formats(self, trade_date):
        account = Account(initial_cash=100000.0, stamp_duty_rate='auto')

        assert account.resolve_stamp_rate(
            '000001.SZ', trade_date) == pytest.approx(0.0005)

    def test_auto_stamp_duty_keeps_conservative_fallback_for_invalid_date(self):
        account = Account(initial_cash=100000.0, stamp_duty_rate='auto')

        assert account.resolve_stamp_rate(
            '000001.SZ', 'not-a-date') == pytest.approx(0.001)

    @pytest.mark.parametrize('trade_date', ['2023-8-27', '2023/8/27'])
    def test_auto_stamp_duty_canonicalizes_non_padded_date_before_boundary(
            self, trade_date):
        account = Account(initial_cash=100000.0, stamp_duty_rate='auto')

        assert account.resolve_stamp_rate(
            '000001.SZ', trade_date) == pytest.approx(0.001)
        assert account.resolve_stamp_rate(
            '000001.SZ', '2023-8-28') == pytest.approx(0.0005)


class TestAccountCash:
    """资金管理测试"""

    def test_insufficient_cash(self):
        """资金不足"""
        account = Account(initial_cash=10000.0)

        # 尝试冻结超过可用资金
        success = account.freeze_cash(15000.0)

        assert success is False

    def test_add_cash(self):
        """增加现金"""
        account = Account(initial_cash=100000.0)

        account.add_cash(5000.0)

        assert account.cash == 105000.0

    def test_deduct_frozen(self):
        """扣除冻结资金"""
        account = Account(initial_cash=100000.0)
        account.freeze_cash(30000.0)

        account.deduct_frozen(10000.0)

        assert account.frozen_cash == 20000.0


class TestAccountShares:
    """股票管理测试"""

    def test_freeze_shares(self):
        """冻结股票"""
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240101')
        account.settle_t1('20240102')

        frozen = account.freeze_shares('000001.SZ', 500)

        assert frozen == 500
        pos = account.get_position('000001.SZ')
        assert pos.frozen_shares == 500

    def test_freeze_shares_no_position(self):
        """无持仓冻结"""
        account = Account(initial_cash=100000.0)

        frozen = account.freeze_shares('000001.SZ', 500)

        assert frozen == 0

    def test_unfreeze_shares(self):
        """解冻股票"""
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240101')
        account.settle_t1('20240102')
        account.freeze_shares('000001.SZ', 500)

        account.unfreeze_shares('000001.SZ', 300)

        pos = account.get_position('000001.SZ')
        assert pos.frozen_shares == 200

    def test_insufficient_shares(self):
        """股份不足"""
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240101')
        account.settle_t1('20240102')

        frozen = account.freeze_shares('000001.SZ', 1500)

        # 只能冻结可用数量
        assert frozen == 1000


class TestTotalAsset:
    """总资产测试"""

    def test_total_asset(self):
        """总资产 = 现金 + 冻结 + 市值"""
        account = Account(initial_cash=100000.0)

        # 买入
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240102')

        # 更新价格
        account.update_prices({'000001.SZ': 12.0})

        # 总资产 = 现金 + 剩余冻结 + 市值
        total = account.total_asset
        assert total > 0

    def test_market_value(self):
        """持仓市值"""
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240102')
        account.update_prices({'000001.SZ': 12.0})

        assert account.market_value == 12000.0


class TestSettleT1:
    """T+1 结算测试"""

    def test_settle_t1(self):
        """T+1 结算"""
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240102')

        # 结算前不可卖
        pos = account.get_position('000001.SZ')
        assert pos.available_shares == 0

        # 结算
        account.settle_t1('20240103')

        # 结算后可卖
        assert pos.available_shares == 1000


class TestOrderManagement:
    """订单管理测试"""

    def test_add_order(self):
        """添加订单"""
        account = Account(initial_cash=100000.0)
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )

        account.add_order(order)

        assert order.order_id in account.orders

    def test_get_order(self):
        """获取订单"""
        account = Account(initial_cash=100000.0)
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        account.add_order(order)

        result = account.get_order(order.order_id)

        assert result is order

    def test_get_open_orders(self):
        """获取未完成订单"""
        account = Account(initial_cash=100000.0)

        order1 = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        order1.submit()
        account.add_order(order1)

        order2 = Order(
            symbol='000002.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=500,
        )
        order2.submit()
        order2.fill(500, 10.0)
        account.add_order(order2)

        open_orders = account.get_open_orders()

        assert len(open_orders) == 1
        assert open_orders[0] is order1

    def test_complete_order(self):
        """完成订单"""
        account = Account(initial_cash=100000.0)
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            shares=1000,
        )
        account.add_order(order)
        order.submit()
        order.fill(1000, 10.0)

        account.complete_order(order)

        assert order.order_id not in account.orders
        assert order in account.order_history


class TestCancelExpiredOrders:
    """取消过期订单测试"""

    def test_cancel_expired_orders(self):
        """取消过期订单"""
        account = Account(initial_cash=100000.0)

        # 创建昨天的订单
        order = Order(
            symbol='000001.SZ',
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            shares=1000,
            price=10.0,
            create_date='20240101',
        )
        order.frozen_amount = 10100.0
        account.freeze_cash(10100.0)
        order.submit()
        account.add_order(order)

        # 取消过期订单
        cancelled = account.cancel_expired_orders('20240102')

        assert len(cancelled) == 1
        assert order.status == OrderStatus.CANCELLED
        assert account.frozen_cash == 0.0  # 资金已释放


class TestAccountToDict:
    """序列化测试"""

    def test_to_dict(self):
        """转换为字典"""
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240102')
        account.update_prices({'000001.SZ': 12.0})

        d = account.to_dict()

        assert 'initial_cash' in d
        assert 'cash' in d
        assert 'frozen_cash' in d
        assert 'market_value' in d
        assert 'total_asset' in d
        assert 'positions' in d


class TestAccountRepr:
    """字符串表示测试"""

    def test_repr(self):
        """__repr__ 方法"""
        account = Account(initial_cash=100000.0)

        repr_str = repr(account)

        assert 'cash=' in repr_str
        assert 'total=' in repr_str


class TestAccountNumericGuards:
    """Invalid numeric inputs must fail before account state is mutated."""

    @pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
    @pytest.mark.parametrize(
        'method_name',
        ['freeze_cash', 'unfreeze_cash', 'add_cash', 'deduct_frozen'],
    )
    def test_cash_mutators_reject_non_finite_without_state_change(
            self, method_name, value):
        account = Account(initial_cash=100000.0)
        account.freeze_cash(1000.0)
        before = (account.cash, account.frozen_cash)

        with pytest.raises(ValueError):
            getattr(account, method_name)(value)

        assert (account.cash, account.frozen_cash) == before

    @pytest.mark.parametrize(
        'method_name',
        ['freeze_cash', 'unfreeze_cash', 'add_cash', 'deduct_frozen'],
    )
    def test_cash_mutators_reject_negative_amount(self, method_name):
        account = Account(initial_cash=100000.0)
        account.freeze_cash(1000.0)
        before = (account.cash, account.frozen_cash)

        with pytest.raises(ValueError):
            getattr(account, method_name)(-1.0)

        assert (account.cash, account.frozen_cash) == before

    @pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
    def test_commission_rejects_non_finite_amount(self, value):
        account = Account(initial_cash=100000.0)

        with pytest.raises(ValueError):
            account.calc_commission(value)

        assert account.cash == 100000.0
        assert account.frozen_cash == 0.0

    @pytest.mark.parametrize(
        'kwargs',
        [
            {'initial_cash': float('nan')},
            {'initial_cash': -1.0},
            {'commission_rate': float('inf')},
            {'commission_rate': -0.1},
            {'stamp_duty_rate': float('nan')},
            {'stamp_duty_rate': -0.1},
            {'min_commission': float('inf')},
            {'min_commission': -1.0},
        ],
    )
    def test_account_rejects_invalid_monetary_configuration(self, kwargs):
        with pytest.raises(ValueError):
            Account(**kwargs)

    @pytest.mark.parametrize('price', [float('nan'), float('inf'), float('-inf'), 0.0])
    def test_buy_rejects_invalid_price_before_mutation(self, price):
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)
        before = (account.cash, account.frozen_cash)

        with pytest.raises(ValueError):
            account.buy('000001.SZ', 1000, price, '20240101')

        assert (account.cash, account.frozen_cash) == before
        assert account.get_position('000001.SZ') is None

    @pytest.mark.parametrize('price', [float('nan'), float('inf'), float('-inf'), 0.0])
    def test_sell_rejects_invalid_price_before_mutation(self, price):
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240101')
        account.settle_t1('20240102')
        pos = account.get_position('000001.SZ')
        before = (account.cash, account.frozen_cash, pos.shares, pos.cost_basis)

        with pytest.raises(ValueError):
            account.sell('000001.SZ', 1000, price, '20240102')

        assert (account.cash, account.frozen_cash, pos.shares, pos.cost_basis) == before

    @pytest.mark.parametrize('price', [float('nan'), float('inf'), float('-inf'), 0.0])
    def test_update_prices_is_atomic_for_invalid_price(self, price):
        account = Account(initial_cash=100000.0)
        account.freeze_cash(11000.0)
        account.buy('000001.SZ', 1000, 10.0, '20240101')
        account.update_prices({'000001.SZ': 10.0})

        with pytest.raises(ValueError):
            account.update_prices({'000001.SZ': price})

        assert account.get_position('000001.SZ').current_price == 10.0
