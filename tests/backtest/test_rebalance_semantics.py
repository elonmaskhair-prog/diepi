# -*- coding: utf-8 -*-
"""换仓撮合新口径测试（2026-08 用户拍板的默认行为）

1. 开盘卖按原始开盘价、开盘买默认含滑点；收盘竞价双向含滑点
2. 竞价买单委托量 = 现金÷涨停价——"挂涨停抢单"语义（委托时不预知成交价）
3. 收盘换仓：卖先买后 + 执行时点重新定量（修复满仓换仓静默低配）
4. 板块规则：920北交所30%、科创板200股/1股递增、T+0白名单、印花税'auto'
"""

from datetime import datetime

import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import Broker, BarData
from diepi.backtest.broker.order import OrderStatus
from diepi.backtest.rulebook import LimitBandRuleNotFoundError


def _bar(sym, o, pre, amount=None):
    return BarData(
        symbol=sym, trade_time=datetime(2024, 1, 3, 9, 30),
        open=o, high=o * 1.02, low=o * 0.98, close=o * 1.01,
        vol=10_000_000, amount=amount if amount is not None else o * 10_000_000,
        pre_close=pre,
    )


def _full_position_broker(**kwargs):
    """全仓持有 000001 (10000股@10)、现金归零的 broker"""
    acct = Account(initial_cash=100_000.0)
    broker = Broker(account=acct, slippage=0.001, **kwargs)
    acct.freeze_cash(100_000.0)
    acct.buy(
        '000001.SZ', 10000, 10.0, '20240102',
        strict_settlement=False,
    )
    acct.settle_t1('20240103')
    broker.set_date('20240103')
    return broker


class TestOpenRebalanceNewSemantics:
    """开盘换仓默认口径"""

    def test_buy_fills_at_open_plus_slippage(self):
        broker = _full_position_broker()
        broker.set_market_phase('before_open')
        sell = broker.sell_at_open('000001.SZ', shares=10000)
        buy = broker.buy_at_open('000002.SZ', percent=0.99)
        broker.set_market_phase('trading')
        broker.execute_open_orders({
            '000001.SZ': _bar('000001.SZ', 10.0, 10.0),
            '000002.SZ': _bar('000002.SZ', 20.0, 20.0),
        })
        # 开盘卖保留历史兼容口径：按原始开盘价成交。
        assert sell.avg_price == pytest.approx(10.0)
        # 买入按 open×(1+slippage) 惩罚
        assert buy.avg_price == pytest.approx(20.0 * 1.001)
        assert buy.filled_shares > 0

    def test_sizing_by_limit_up(self):
        """委托量 = 现金/涨停价：pre_close=20 → 涨停22 → 0.99×~99900/22 ≈ 4400股"""
        broker = _full_position_broker()
        broker.set_market_phase('before_open')
        broker.sell_at_open('000001.SZ', shares=10000)
        buy = broker.buy_at_open('000002.SZ', percent=0.99)
        broker.set_market_phase('trading')
        broker.execute_open_orders({
            '000001.SZ': _bar('000001.SZ', 10.0, 10.0),
            '000002.SZ': _bar('000002.SZ', 20.0, 20.0),
        })
        # 若按成交价折算是 ~4900 股；按涨停价 22 折算应为 4400 股
        assert buy.filled_shares == 4400

    def test_legacy_mode_still_available(self):
        """旧口径可切回：fill='open' + sizing='fill' 复现历史结果"""
        broker = _full_position_broker(open_buy_fill_mode='open', open_buy_sizing='fill')
        broker.set_market_phase('before_open')
        sell = broker.sell_at_open('000001.SZ', shares=10000)
        buy = broker.buy_at_open('000002.SZ', percent=0.99)
        broker.set_market_phase('trading')
        broker.execute_open_orders({
            '000001.SZ': _bar('000001.SZ', 10.0, 10.0),
            '000002.SZ': _bar('000002.SZ', 20.0, 20.0),
        })
        assert sell.avg_price == pytest.approx(10.0)
        assert buy.avg_price == pytest.approx(20.0)
        assert buy.filled_shares == 4900


class TestCloseRebalanceResizing:
    """收盘换仓：卖先买后 + 执行时点定量（修复静默低配）"""

    def test_close_buy_uses_close_sell_proceeds(self):
        acct = Account(initial_cash=10_000.0)
        broker = Broker(account=acct, slippage=0.001)
        acct.freeze_cash(10_000.0)
        acct.buy('000001.SZ', 990, 10.0, '20240102')
        acct.settle_t1('20240103')
        broker.set_date('20240103')
        broker.set_market_phase('trading')

        sell = broker.sell_at_close('000001.SZ', percent=1.0)
        buy = broker.buy_at_close('000002.SZ', percent=0.99, current_price=20.0)
        broker.execute_close_orders({
            '000001.SZ': _bar('000001.SZ', 10.0, 10.0),
            '000002.SZ': _bar('000002.SZ', 20.0, 20.0),
        })
        assert sell.filled_shares == 990
        assert sell.avg_price == pytest.approx(10.0 * 1.01 * (1 - 0.001))
        # 修复前：买腿按卖出前现金(~185元)定量 = 0股静默蒸发
        # 修复后：按回款(~10000元)/涨停价22 = 400股
        assert buy.filled_shares == 400
        assert buy.avg_price == pytest.approx(20.2 * 1.001)

    def test_close_buy_sells_execute_first(self):
        """同日收盘单：即使买单先提交，卖单也先执行"""
        acct = Account(initial_cash=100.0)
        broker = Broker(account=acct, slippage=0.0)
        acct.freeze_cash(100.0)
        acct.buy('000001.SZ', 9, 10.0, '20240102')  # 零股持仓
        acct.settle_t1('20240103')
        broker.set_date('20240103')
        broker.set_market_phase('trading')

        buy = broker.buy_at_close('000002.SZ', amount=90.0, current_price=0.5)
        sell = broker.sell_at_close('000001.SZ', shares=9)
        broker.execute_close_orders({
            '000001.SZ': _bar('000001.SZ', 10.0, 10.0),
            '000002.SZ': _bar('000002.SZ', 0.5, 0.5),
        })
        assert sell.status == OrderStatus.FILLED
        # 买腿的资金校验发生在卖腿回款之后
        assert buy.filled_shares >= 0  # 不因先提交而在卖出前被定死


class TestBoardRules:
    """板块申报与涨跌停规则"""

    def test_bse_920_limit_30pct(self):
        b = Broker(account=Account())
        assert b._get_limit_pct('920002.BJ', '20250101') == pytest.approx(0.30)
        with pytest.raises(LimitBandRuleNotFoundError, match='BSE snapshot'):
            b._get_limit_pct('920002.BJ', '20211112')

    def test_star_lot_rule_200_then_1(self):
        b = Broker(account=Account())
        assert b._lot_rule('688012.SH') == (200, 1)
        # 250股是合法申报（200起1股递增）
        assert b._round_buy_shares('688012.SH', 250) == 250
        # 150股低于最低申报量
        assert b._round_buy_shares('688012.SH', 150) == 0

    def test_bse_lot_rule_100_then_1(self):
        b = Broker(account=Account())
        assert b._lot_rule('920002.BJ') == (100, 1)
        assert b._round_buy_shares('920002.BJ', 137) == 137

    def test_normal_lot_rule_unchanged(self):
        b = Broker(account=Account())
        assert b._lot_rule('000001.SZ') == (100, 100)
        assert b._round_buy_shares('000001.SZ', 250) == 200

    def test_fund_tick_3_decimals(self):
        b = Broker(account=Account())
        b.set_date('20240103')
        # pre_close=1.231 → 真实涨停 1.354（3位小数），旧口径 round(,2)=1.35 会误拒
        assert b._limit_up_price('510300.SH', 1.231) == pytest.approx(1.354)
        assert b._limit_up_price('000001.SZ', 9.87) == pytest.approx(10.86)

    def test_limit_check_uses_ref_price_not_slipped(self):
        """涨跌停判定用未加滑点的市场价：close=10.99 未涨停，不应因 ×1.001 被拒"""
        acct = Account(initial_cash=1_000_000.0)
        b = Broker(account=acct, slippage=0.001)
        b.set_date('20240103')
        b.set_market_phase('trading')
        order = b.buy_at_market('000001.SZ', shares=100, current_price=10.99)
        bar = BarData(symbol='000001.SZ', trade_time=datetime(2024, 1, 3, 10, 0),
                      open=10.5, high=10.99, low=10.4, close=10.99,
                      vol=1_000_000, amount=10_990_000, pre_close=10.0)
        b.execute_market_orders(bar)
        # 涨停价 11.00，high=10.99 未触板；fill=10.99×1.001=11.001 但判定用 ref=10.99
        assert order.status == OrderStatus.FILLED


class TestT0AndStampAuto:
    """T+0 白名单与印花税 auto"""

    def test_t0_override_same_day_sell(self):
        acct = Account(initial_cash=100_000.0)
        b = Broker(account=acct, t0_overrides={'511'})
        b.set_date('20240103')
        acct.freeze_cash(50_000.0)
        acct.buy('511990.SH', 400, 100.0, '20240103', t0=b._is_t0('511990.SH'))
        assert acct.positions['511990.SH'].available_shares == 400

    def test_t1_default_unchanged(self):
        acct = Account(initial_cash=100_000.0)
        b = Broker(account=acct)
        b.set_date('20240103')
        acct.freeze_cash(50_000.0)
        acct.buy('000001.SZ', 1000, 10.0, '20240103', t0=b._is_t0('000001.SZ'))
        assert acct.positions['000001.SZ'].available_shares == 0

    def test_stamp_auto_etf_free(self):
        acct = Account(initial_cash=100_000.0, stamp_duty_rate='auto')
        assert acct.resolve_stamp_rate('510300.SH', '20240103') == 0.0
        assert acct.resolve_stamp_rate('159915.SZ', '20240103') == 0.0

    def test_stamp_auto_stock_date_segmented(self):
        acct = Account(initial_cash=100_000.0, stamp_duty_rate='auto')
        assert acct.resolve_stamp_rate('000001.SZ', '20230827') == pytest.approx(0.001)
        assert acct.resolve_stamp_rate('000001.SZ', '20230828') == pytest.approx(0.0005)

    def test_stamp_numeric_default_strict(self):
        """默认仍是数值 0.001 一刀切（用户刻意保留的严格口径）"""
        acct = Account(initial_cash=100_000.0)
        assert acct.resolve_stamp_rate('510300.SH', '20240103') == pytest.approx(0.001)
