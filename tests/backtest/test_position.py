# -*- coding: utf-8 -*-
"""
Position 单元测试

测试持仓类
"""

import pytest
from datetime import datetime

from diepi.backtest.broker.position import Position


class TestPosition:
    """持仓类测试"""

    def test_create_position(self):
        """创建持仓"""
        pos = Position(symbol='000001.SZ')
        assert pos.symbol == '000001.SZ'
        assert pos.shares == 0
        assert pos.frozen_shares == 0
        assert pos.cost_basis == 0.0
        assert pos.avg_cost == 0.0

    def test_add_shares(self):
        """增加持仓"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')

        assert pos.shares == 1000
        assert pos.cost_basis == 10000.0
        assert pos.avg_cost == 10.0

    def test_add_shares_multiple(self):
        """多次增加持仓"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')
        pos.buy(500, 12.0, '20240103')

        assert pos.shares == 1500
        assert pos.cost_basis == 16000.0  # 10000 + 6000
        assert abs(pos.avg_cost - 16000.0 / 1500) < 0.001

    def test_remove_shares(self):
        """减少持仓"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240101')

        # 结算 T+1 使股票可卖
        pos.settle_t1('20240102')

        sell_amount = pos.sell(500, 12.0)

        assert pos.shares == 500
        assert sell_amount == 6000.0

    def test_avg_cost_calculation(self):
        """平均成本计算"""
        pos = Position(symbol='000001.SZ')

        pos.buy(1000, 10.0, '20240102')
        assert pos.avg_cost == 10.0

        pos.buy(1000, 12.0, '20240103')
        # 平均成本: (10*1000 + 12*1000) / 2000 = 11
        assert pos.avg_cost == 11.0

    def test_avg_cost_with_commission(self):
        """平均成本含手续费"""
        pos = Position(symbol='000001.SZ')

        # 买入含手续费
        pos.buy(1000, 10.0, '20240102', commission=25.0)

        assert pos.shares == 1000
        assert pos.cost_basis == 10025.0  # 10000 + 25
        assert abs(pos.avg_cost - 10.025) < 0.001

    def test_market_value(self):
        """市值计算"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')
        pos.update_price(12.0)

        assert pos.market_value == 12000.0

    def test_unrealized_profit(self):
        """未实现盈亏"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')
        pos.update_price(12.0)

        # 盈利 = 市值 - 成本 = 12000 - 10000 = 2000
        assert pos.profit == 2000.0
        assert pos.profit_pct == 0.2  # 20%

    def test_freeze_shares(self):
        """冻结股份 (待卖出)"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240101')
        pos.settle_t1('20240102')

        frozen = pos.freeze_shares(500)

        assert frozen == 500
        assert pos.frozen_shares == 500
        assert pos.available_shares == 500

    def test_freeze_shares_exceeds_available(self):
        """冻结超过可用"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240101')
        pos.settle_t1('20240102')

        frozen = pos.freeze_shares(1500)

        # 只能冻结可用数量
        assert frozen == 1000
        assert pos.frozen_shares == 1000

    def test_unfreeze_shares(self):
        """解冻股份"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240101')
        pos.settle_t1('20240102')
        pos.freeze_shares(500)

        pos.unfreeze_shares(300)

        assert pos.frozen_shares == 200
        assert pos.available_shares == 800

    def test_available_shares(self):
        """可用股份 = 总数 - 冻结 - T+1"""
        pos = Position(symbol='000001.SZ')

        # Day 1: 买入 1000 股
        pos.buy(1000, 10.0, '20240102')

        # 当天可卖 = 0 (T+1 限制)
        assert pos.available_shares == 0

        # Day 2: 结算后可卖
        pos.settle_t1('20240103')
        assert pos.available_shares == 1000

        # 冻结 300 股
        pos.freeze_shares(300)
        assert pos.available_shares == 700


class TestTPlusOne:
    """T+1 规则测试"""

    def test_t_plus_1_restriction(self):
        """T+1 限制 (当日买入不可卖)"""
        pos = Position(symbol='000001.SZ')

        # Day 1 买入
        pos.buy(1000, 10.0, '20240102')

        # 当天可卖 = 0
        assert pos.available_shares == 0

        # 尝试卖出（应该失败）
        sell_amount = pos.sell(500, 12.0)
        assert sell_amount == 0.0
        assert pos.shares == 1000

    def test_t_plus_1_next_day_available(self):
        """T+1 次日可卖"""
        pos = Position(symbol='000001.SZ')

        pos.buy(1000, 10.0, '20240102')
        pos.settle_t1('20240103')

        assert pos.available_shares == 1000

    def test_settle_t1(self):
        """T+1 结算"""
        pos = Position(symbol='000001.SZ')

        # Day 1: 买入
        pos.buy(1000, 10.0, '20240102')
        assert pos.available_shares == 0

        # Day 2: 再买入
        pos.buy(500, 11.0, '20240103')
        # Day 2 的股票仍然不可卖
        assert pos.available_shares == 0

        # Day 3: 结算 Day 2 之前的
        pos.settle_t1('20240104')
        # Day 1 和 Day 2 买入的都可卖了
        assert pos.available_shares == 1500

    def test_settle_t1_partial(self):
        """部分结算"""
        pos = Position(symbol='000001.SZ')

        pos.buy(1000, 10.0, '20240102')
        pos.buy(500, 11.0, '20240103')

        # 只结算 Day 2，Day 3 买入的仍然不可卖
        pos.settle_t1('20240103')

        # 只有 Day 1 买入的可卖
        assert pos.available_shares == 1000


class TestHoldDays:
    """持有天数测试"""

    def test_hold_days(self):
        """持仓天数"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')
        pos.set_current_date('20240105')

        # 20240102 到 20240105 = 3 天
        assert pos.hold_days == 3

    def test_hold_days_no_entry(self):
        """无持仓时的持有天数"""
        pos = Position(symbol='000001.SZ')
        pos.set_current_date('20240105')

        assert pos.hold_days == 0

    def test_hold_days_no_current_date(self):
        """未设置当前日期"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')

        assert pos.hold_days == 0


class TestPositionSell:
    """卖出测试"""

    def test_sell_all(self):
        """全部卖出"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240101')
        pos.settle_t1('20240102')

        sell_amount = pos.sell(1000, 12.0)

        assert pos.shares == 0
        assert pos.cost_basis == 0.0
        assert pos.avg_cost == 0.0
        assert sell_amount == 12000.0

    def test_sell_partial(self):
        """部分卖出"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240101')
        pos.settle_t1('20240102')

        sell_amount = pos.sell(400, 12.0)

        assert pos.shares == 600
        assert sell_amount == 4800.0

    def test_sell_clears_entry_date(self):
        """清仓重置入场日期"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')
        pos.settle_t1('20240103')

        pos.sell(1000, 12.0)

        assert pos.entry_date is None

    def test_sell_zero_shares(self):
        """卖出零股"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240101')
        pos.settle_t1('20240102')

        with pytest.raises(ValueError, match='positive integer'):
            pos.sell(0, 12.0)

        assert pos.shares == 1000


class TestPositionNumericBoundary:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"symbol": ""}, "symbol"),
            ({"symbol": "000001.SZ", "shares": -1}, "shares"),
            (
                {"symbol": "000001.SZ", "shares": 1, "frozen_shares": 2,
                 "cost_basis": 10.0, "avg_cost": 10.0},
                "frozen_shares",
            ),
            ({"symbol": "000001.SZ", "cost_basis": float("nan")}, "cost_basis"),
            ({"symbol": "000001.SZ", "current_price": float("inf")}, "current_price"),
        ],
    )
    def test_constructor_rejects_invalid_state(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            Position(**kwargs)

    @pytest.mark.parametrize(
        ("shares", "price", "commission"),
        [
            (0, 10.0, 0.0),
            (100, 0.0, 0.0),
            (100, float("nan"), 0.0),
            (100, 10.0, float("inf")),
            (10 ** 308, 10.0, 0.0),
        ],
    )
    def test_buy_invalid_or_overflow_is_atomic(self, shares, price, commission):
        pos = Position(symbol="000001.SZ")
        before = pos.to_dict(), dict(pos._pending_shares), pos.last_update

        with pytest.raises(ValueError):
            pos.buy(shares, price, "20240102", commission=commission)

        assert (pos.to_dict(), dict(pos._pending_shares), pos.last_update) == before

    @pytest.mark.parametrize("price", [0.0, -1.0, float("nan"), float("inf")])
    def test_update_price_invalid_is_atomic(self, price):
        pos = Position(symbol="000001.SZ")
        pos.buy(100, 10.0, "20240102")
        before = pos.current_price, pos.last_update

        with pytest.raises(ValueError, match="price"):
            pos.update_price(price)

        assert (pos.current_price, pos.last_update) == before

    @pytest.mark.parametrize("shares", [-1, 1.5, float("nan"), float("inf")])
    def test_freeze_and_unfreeze_reject_invalid_shares(self, shares):
        pos = Position(symbol="000001.SZ")
        pos.buy(100, 10.0, "20240101")
        pos.settle_t1("20240102")
        before = pos.frozen_shares

        with pytest.raises(ValueError):
            pos.freeze_shares(shares)
        with pytest.raises(ValueError):
            pos.unfreeze_shares(shares)

        assert pos.frozen_shares == before

    def test_split_rejects_corrupt_existing_state_without_partial_update(self):
        pos = Position(symbol="000001.SZ")
        pos.buy(100, 10.0, "20240102")
        pos.update_price(12.0)
        pos.current_price = float("nan")
        before = (
            pos.shares,
            pos.frozen_shares,
            pos.cost_basis,
            pos.avg_cost,
            dict(pos._pending_shares),
        )

        with pytest.raises(ValueError, match="invalid position state"):
            pos.apply_split_ratio(2.0)

        assert (
            pos.shares,
            pos.frozen_shares,
            pos.cost_basis,
            pos.avg_cost,
            dict(pos._pending_shares),
        ) == before
        assert pos.current_price != pos.current_price


class TestPositionToDict:
    """序列化测试"""

    def test_to_dict(self):
        """转换为字典"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')
        pos.update_price(12.0)
        pos.set_current_date('20240105')

        d = pos.to_dict()

        assert d['symbol'] == '000001.SZ'
        assert d['shares'] == 1000
        assert d['avg_cost'] == 10.0
        assert d['current_price'] == 12.0
        assert d['market_value'] == 12000.0
        assert d['profit'] == 2000.0
        assert d['profit_pct'] == 20.0  # 百分比


class TestPositionRepr:
    """字符串表示测试"""

    def test_repr(self):
        """__repr__ 方法"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240101')
        pos.settle_t1('20240102')

        repr_str = repr(pos)

        assert '000001.SZ' in repr_str
        assert '1000' in repr_str


class TestApplySplitRatio:
    """股票拆分调整测试"""

    def test_apply_split_ratio(self):
        """应用拆分比例"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')
        pos.update_price(12.0)
        market_value_before = pos.market_value

        # 2:1 拆分
        pos.apply_split_ratio(2.0)

        assert pos.shares == 2000
        assert pos.cost_basis == 10000.0  # 成本不变
        assert pos.avg_cost == 5.0  # 成本价减半
        assert pos.current_price == 6.0
        assert pos.market_value == market_value_before

    def test_apply_split_ratio_no_change(self):
        """比例为 1 不变"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')

        pos.apply_split_ratio(1.0)

        assert pos.shares == 1000
        assert pos.avg_cost == 10.0

    def test_apply_split_ratio_adjusts_immediate_fill_mark(self):
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')
        assert pos.current_price == 10.0

        pos.apply_split_ratio(2.0)

        assert pos.shares == 2000
        assert pos.current_price == 5.0
        assert pos.market_value == 10000.0

    def test_buy_and_partial_sell_mark_immediately_to_fill_price(self):
        pos = Position(symbol='000001.SZ')

        pos.buy(1000, 10.0, '20240102', t0=True)
        assert pos.current_price == 10.0
        assert pos.market_value == 10000.0

        pos.sell(400, 11.0)
        assert pos.shares == 600
        assert pos.current_price == 11.0
        assert pos.market_value == 6600.0

        pos.sell(600, 12.0)
        assert pos.shares == 0
        assert pos.current_price == 0.0
        assert pos.market_value == 0.0

    def test_apply_split_ratio_zero(self):
        """非法比例显式失败，且不产生半更新。"""
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')
        before = (pos.shares, pos.avg_cost, dict(pos._pending_shares))

        with pytest.raises(ValueError, match='finite and positive'):
            pos.apply_split_ratio(0.0)

        assert (pos.shares, pos.avg_cost, dict(pos._pending_shares)) == before

    @pytest.mark.parametrize(
        'ratio',
        [float('nan'), float('inf'), -float('inf'), -1.0, None, 'bad'],
    )
    def test_apply_split_ratio_invalid_ratio_raises_atomically(self, ratio):
        pos = Position(symbol='000001.SZ')
        pos.buy(1000, 10.0, '20240102')
        pos.update_price(12.0)
        before = (
            pos.shares,
            pos.frozen_shares,
            pos.cost_basis,
            pos.avg_cost,
            pos.current_price,
            dict(pos._pending_shares),
            pos.last_update,
        )

        with pytest.raises(ValueError, match='finite and positive'):
            pos.apply_split_ratio(ratio)

        after = (
            pos.shares,
            pos.frozen_shares,
            pos.cost_basis,
            pos.avg_cost,
            pos.current_price,
            dict(pos._pending_shares),
            pos.last_update,
        )
        assert after == before

    def test_apply_split_ratio_empty_position(self):
        """空仓不处理"""
        pos = Position(symbol='000001.SZ')

        pos.apply_split_ratio(2.0)

        assert pos.shares == 0
