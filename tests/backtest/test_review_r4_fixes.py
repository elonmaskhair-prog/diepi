# -*- coding: utf-8 -*-
"""第四轮修后审查确认项的修复回归测试

审查组（6域红队+逐发现怀疑者，2026-08-05）确认 11 个独立缺陷，
本文件按缺陷逐项锁定修复。场景描述见各测试 docstring。
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import Broker, BarData
from diepi.backtest.broker.order import OrderStatus, OrderType
from diepi.backtest.broker.target import (
    TargetAchievementStatus,
    TargetIntent,
    TargetPlanSide,
)
from diepi.backtest.engine.portfolio_context import PortfolioContext

ROOT = Path(__file__).resolve().parents[2]


def _ctx(cash=1_000_000.0):
    acct = Account(initial_cash=cash)
    broker = Broker(account=acct, slippage=0.001)
    broker.set_date('20240103')
    ctx = PortfolioContext(broker=broker, initial_cash=cash)
    return ctx, broker, acct


def _hold(acct, symbol, shares, price, date='20240102', update_price=True):
    acct.freeze_cash(shares * price * 1.01)
    acct.buy(symbol, shares, price, date)
    acct.settle_t1('20240103')
    if update_price:
        acct.positions[symbol].update_price(price)


def _bar(sym, o, h=None, low=None, c=None, ts=None):
    return BarData(
        symbol=sym, trade_time=ts or datetime(2024, 1, 3, 9, 31),
        open=o, high=h if h is not None else o * 1.001,
        low=low if low is not None else o * 0.999,
        close=c if c is not None else o,
        vol=10_000_000, amount=o * 10_000_000, pre_close=o,
    )


def _run_close_target(ctx, broker, symbol, weight, *, price=10.0):
    decision = datetime(2024, 1, 3, 14, 55)
    match_at = datetime(2024, 1, 3, 15, 0)
    with broker.order_creation_scope('on_before_close', decision):
        with broker.target_intent_scope(match_at):
            intent = ctx.order_target_percent(symbol, weight, when='close')
    orders = broker.execute_close_orders(
        {symbol: _bar(symbol, price, c=price, ts=match_at)},
        match_at=match_at,
    )
    achievement = next(
        item for item in broker.target_achievements
        if item.intent_id == intent.intent_id
    )
    return intent, achievement, orders


class TestSameDayPositionValuation:
    """P1：当日新开仓必须立即按成交价估值，目标下调不得反向买入。"""

    def test_close_reduce_after_sameday_open_not_buy(self):
        """当日买入已更新 mark；盘中下调目标仍受 T+1 可卖数量约束。"""
        ctx, broker, acct = _ctx()
        acct.freeze_cash(825_000.0)
        acct.buy('510300.SH', 81_800, 10.0, '20240103')
        assert acct.positions['510300.SH'].current_price == 10.0
        broker.set_market_phase('trading')
        intent, result, orders = _run_close_target(
            ctx, broker, '510300.SH', 0.2, price=10.0)
        assert isinstance(intent, TargetIntent)
        assert result.planned_side is TargetPlanSide.SELL
        assert result.planned_shares == 0
        assert orders == []

    def test_effective_total_asset_includes_sameday_position(self):
        """总资产基数不得塌缩为剩余现金"""
        ctx, broker, acct = _ctx()
        acct.freeze_cash(825_000.0)
        acct.buy('510300.SH', 81_800, 10.0, '20240103')
        total = ctx._effective_total_asset()
        assert total > 900_000  # 现金 ~18万 + 持仓成本 ~82万

    def test_sameday_clear_t0_instrument_sells(self):
        """T+0 品种当日建仓当日清仓（weight=0）必须真的卖"""
        acct = Account(initial_cash=1_000_000.0)
        broker = Broker(account=acct, slippage=0.001, t0_overrides={'511'})
        broker.set_date('20240103')
        ctx = PortfolioContext(broker=broker, initial_cash=1_000_000.0)
        acct.freeze_cash(510_000.0)
        acct.buy('511990.SH', 5_000, 100.0, '20240103', t0=True)
        broker.set_market_phase('trading')
        intent, result, orders = _run_close_target(
            ctx, broker, '511990.SH', 0.0, price=100.0)
        assert isinstance(intent, TargetIntent)
        assert result.planned_side is TargetPlanSide.SELL
        assert result.filled_shares == 5_000
        assert len(orders) == 1 and orders[0].is_sell


class TestTargetPercentIdempotency:
    """P1：重复调用不抵扣同窗挂单 → 双倍建仓/超卖清仓"""

    def test_duplicate_buy_call_no_second_order(self):
        ctx, broker, acct = _ctx()
        broker.set_market_phase('before_open')
        o1 = ctx.order_target_percent('510300.SH', 0.5)
        assert o1 is not None
        o2 = ctx.order_target_percent('510300.SH', 0.5)
        assert o2 is None  # 挂单已承诺 50%，无需补单

    def test_duplicate_sell_call_no_oversell(self):
        ctx, broker, acct = _ctx()
        _hold(acct, '510300.SH', 100_000, 5.0)
        broker.set_market_phase('before_open')
        o1 = ctx.order_target_percent('510300.SH', 0.1)
        assert o1 is not None and o1.is_sell
        o2 = ctx.order_target_percent('510300.SH', 0.1)
        assert o2 is None  # 不得把剩余可卖也卖光

    def test_target_raise_after_pending_buy_only_delta(self):
        """先挂 30% 再上调到 50%：第二单只补 20% 的量"""
        ctx, broker, acct = _ctx()
        broker.set_market_phase('before_open')
        ctx.order_target_percent('510300.SH', 0.3)
        o2 = ctx.order_target_percent('510300.SH', 0.5)
        assert o2 is not None
        assert o2.requested_amount == pytest.approx(200_000.0, rel=0.01)


class TestExplicitClearBypassesDeadzone:
    """P2：显式 weight=0 清仓不受 min_trade_amount 死区约束"""

    def test_dust_position_cleared(self):
        ctx, broker, acct = _ctx()
        _hold(acct, '510300.SH', 800, 5.0)  # 市值 4000 元
        broker.set_market_phase('before_open')
        orders = ctx.rebalance({'510300.SH': 0.0}, min_trade_amount=5000.0)
        assert len(orders) == 1 and orders[0].is_sell
        assert orders[0].shares == 800


class TestFrozenInsufficientNotPrematurelyRejected:
    """G4 回归：连续限价单必须在提交时连同精确费用足额冻结。"""

    def _small_limit_setup(self):
        acct = Account(initial_cash=10_000.0)  # min_commission 默认 5 元
        broker = Broker(account=acct, slippage=0.0)
        broker.set_date('20240103')
        broker.set_market_phase('trading')
        return acct, broker

    def test_exact_fee_reserve_fills_on_first_touched_bar(self):
        acct, broker = self._small_limit_setup()
        o = broker.buy_at_price('000001.SZ', price=3.00, shares=100)
        assert o.status not in (OrderStatus.REJECTED, OrderStatus.CANCELLED)
        assert o.frozen_amount == pytest.approx(305.0)
        broker.execute_market_orders(
            _bar('000001.SZ', 3.00, h=3.01, low=3.00, c=3.00))
        assert o.status == OrderStatus.FILLED
        assert o.filled_shares == 100
        assert acct.frozen_cash == 0.0

    def test_fee_reserve_does_not_create_frozen_insufficient_marker(self):
        acct, broker = self._small_limit_setup()
        o = broker.buy_at_price('000001.SZ', price=3.00, shares=100)
        broker.execute_market_orders(
            _bar('000001.SZ', 3.00, h=3.01, low=3.00, c=3.00))
        assert o.filled_shares == 100
        assert 'frozen_insufficient' not in (o.resize_reason or '')
        broker.cancel_day_end_orders()
        assert o.status == OrderStatus.FILLED
        assert acct.frozen_cash == 0.0

    def test_normal_unfilled_order_still_cancelled(self):
        """无审计标记的普通未成交单日终仍是 CANCELLED（不误伤）"""
        acct, broker = self._small_limit_setup()
        # 2.80 位于 pre_close=3.00 的合法价格带内，但低于当日 low，
        # 因此是合法未触发单；2.00 会被交易所价格带直接拒绝。
        o = broker.buy_at_price('000001.SZ', price=2.80, shares=100)
        broker.execute_market_orders(
            _bar('000001.SZ', 3.00, h=3.01, low=2.99, c=3.00))
        broker.cancel_day_end_orders()
        assert o.status == OrderStatus.CANCELLED


class TestConditionalOrdersNotCommitted:
    """核查组新P1：条件单（止损/止盈/限价）曾被 _pending_order_value 当作
    确定性卖出承诺——目标权重方向反转成买入、近双倍建仓"""

    def test_stop_loss_does_not_flip_reduce_into_buy(self):
        """持仓+挂半仓止损（未触发），小幅下调目标：卖出，不得反向买入"""
        ctx, broker, acct = _ctx()
        _hold(acct, '510300.SH', 90_000, 10.0)  # 市值 90 万
        broker.set_market_phase('trading')
        stop = broker.sell_stop_loss('510300.SH', price=5.0, percent=0.5)
        assert stop is not None
        _, result, orders = _run_close_target(
            ctx, broker, '510300.SH', 0.80, price=10.0)
        assert result.planned_side is TargetPlanSide.SELL, \
            "止损单不得把目标方向反转成买入"
        assert not orders or all(order.is_sell for order in orders)

    def test_full_position_stop_reduce_is_noop_not_buy(self):
        """全仓止损冻结全部股份：下调目标无可卖=None（修复前基线语义），
        绝不能反向挂买单加仓"""
        ctx, broker, acct = _ctx()
        _hold(acct, '510300.SH', 90_000, 10.0)
        broker.set_market_phase('trading')
        broker.sell_stop_loss('510300.SH', price=5.0, percent=1.0)
        _, result, orders = _run_close_target(
            ctx, broker, '510300.SH', 0.80, price=10.0)
        assert result.planned_side is TargetPlanSide.SELL
        assert result.planned_shares == 0
        assert orders == []

    def test_stop_loss_plus_redeclare_same_target_noop(self):
        """挂止损后重复声明当前目标：不得近双倍建仓"""
        ctx, broker, acct = _ctx()
        _hold(acct, '510300.SH', 50_000, 10.0)  # ~50%
        broker.set_market_phase('trading')
        broker.sell_stop_loss('510300.SH', price=9.0, percent=1.0)
        current_w = ctx.get_position_value('510300.SH') / ctx._effective_total_asset()
        _, result, orders = _run_close_target(
            ctx, broker, '510300.SH', current_w, price=10.0)
        assert result.planned_side is TargetPlanSide.NONE
        assert result.status in {
            TargetAchievementStatus.ACHIEVED,
            TargetAchievementStatus.NOOP,
        }
        assert orders == []

    def test_untriggered_buy_stop_does_not_compress_target(self):
        """挂突破买入条件单后声明目标：买入量不得被条件单压缩"""
        ctx, broker, acct = _ctx()
        broker.set_market_phase('trading')
        broker.buy_stop('510300.SH', price=99.0, shares=40_000)
        _, result, orders = _run_close_target(
            ctx, broker, '510300.SH', 0.5, price=10.0)
        assert result.planned_side is TargetPlanSide.BUY
        assert orders and all(order.is_buy for order in orders)


class TestCancelReplaceSemantics:
    """核查组新P2：同窗下调目标曾被静默忽略（挂单不撤不缩量）"""

    def test_lower_target_same_window_cancels_and_replaces(self):
        """先 0.5 再 0.3（无持仓）：撤 500k 挂单、重挂 300k"""
        ctx, broker, acct = _ctx()
        broker.set_market_phase('before_open')
        o1 = ctx.order_target_percent('510300.SH', 0.5)
        o2 = ctx.order_target_percent('510300.SH', 0.3)
        assert o1.status == OrderStatus.CANCELLED
        assert o2 is not None and o2.is_buy
        assert o2.requested_amount == pytest.approx(300_000.0, rel=0.01)

    def test_lower_target_with_position_prefers_cancel_over_churn(self):
        """持仓20% + 挂单30%，下调到 30%：撤挂单+微调，不得清仓重买"""
        ctx, broker, acct = _ctx()
        _hold(acct, '510300.SH', 40_000, 5.0)  # 市值 20 万 = 20%
        broker.set_market_phase('before_open')
        ctx.order_target_percent('510300.SH', 0.5)   # 挂 30 万买单
        o2 = ctx.order_target_percent('510300.SH', 0.3)
        # 撤挂单后 delta = 30万 - 20万持仓 = +10万 → 小额补买，绝不清仓
        assert o2 is None or o2.is_buy
        if o2 is not None:
            assert o2.requested_amount == pytest.approx(100_000.0, rel=0.02)

    def test_raise_target_after_pending_sell_cancels_sell(self):
        """挂减仓卖单后又上调目标：撤卖单而非买回对冲（省一轮换手）"""
        ctx, broker, acct = _ctx()
        _hold(acct, '510300.SH', 100_000, 5.0)  # 50 万
        broker.set_market_phase('before_open')
        o1 = ctx.order_target_percent('510300.SH', 0.1)  # 卖 ~40 万
        assert o1 is not None and o1.is_sell
        o2 = ctx.order_target_percent('510300.SH', 0.5)
        assert o1.status == OrderStatus.CANCELLED
        assert o2 is None or (o2.requested_amount or 0) < 50_000


class TestCancelReplaceAtomicity:
    """收尾核查P2：when/时段不匹配的被拒调用曾先撤好单再被时段门拒
    （在途挂单被静默销毁）——被拒调用必须零副作用"""

    def test_phase_mismatch_close_call_keeps_open_order(self):
        """盘前挂 when='open' 买单后误用 when='close' 下调：
        原单必须保留（替换单被时段门拒绝，不得先销毁好单）"""
        ctx, broker, acct = _ctx()
        broker.set_market_phase('before_open')
        o1 = ctx.order_target_percent('510300.SH', 0.5, when='open')
        assert o1 is not None
        with pytest.raises(ValueError, match='on_after_open/on_before_close'):
            ctx.order_target_percent('510300.SH', 0.3, when='close')
        assert o1.status not in (OrderStatus.CANCELLED,), \
            "时段不匹配的调用不得销毁在途挂单"
        assert broker.target_intents == ()

    def test_phase_mismatch_open_call_keeps_close_order(self):
        """盘中挂 when='close' 买单后误用 when='open' 下调：原单保留"""
        ctx, broker, acct = _ctx()
        broker.set_market_phase('trading')
        decision = datetime(2024, 1, 3, 14, 55)
        match_at = datetime(2024, 1, 3, 15)
        with broker.order_creation_scope('on_before_close', decision):
            with broker.target_intent_scope(match_at):
                o1 = ctx.order_target_percent(
                    '510300.SH', 0.5, when='close')
        assert isinstance(o1, TargetIntent)
        o2 = ctx.order_target_percent('510300.SH', 0.3, when='open')
        assert o1 in broker.target_intents
        assert o2 is None or o2.status == OrderStatus.REJECTED

    def test_missing_bar_close_call_keeps_pending_sell(self):
        """盘中该标的无 bar（替换买单必因缺价被拒）：不得先撤在途卖单"""
        ctx, broker, acct = _ctx()
        _hold(acct, '510300.SH', 100_000, 5.0)
        broker.set_market_phase('trading')
        decision = datetime(2024, 1, 3, 14, 55)
        match_at = datetime(2024, 1, 3, 15)
        with broker.order_creation_scope('on_before_close', decision):
            with broker.target_intent_scope(match_at):
                o1 = ctx.order_target_percent(
                    '510300.SH', 0.1, when='close')
        with broker.order_creation_scope(
                'on_before_close', decision + pd.Timedelta(seconds=1)):
            with broker.target_intent_scope(match_at):
                o2 = ctx.order_target_percent(
                    '510300.SH', 0.5, when='close')
        assert (o1, o2) == broker.target_intents
        assert acct.orders == {}


class TestLegacyPartialOpenPhantom:
    """核查组新P2：开盘撮合已过的 OPEN 残单是死单，不得计入承诺"""

    def test_dead_open_residual_excluded_from_pending(self):
        ctx, broker, acct = _ctx()
        broker.set_market_phase('before_open')
        broker.buy_at_open('510300.SH', amount=400_000.0)
        assert ctx._pending_order_value('510300.SH') > 0  # 盘前=活单
        broker.set_market_phase('trading')  # 开盘撮合已过（模拟残单时段）
        assert ctx._pending_order_value('510300.SH') == 0.0, \
            "开盘已过的 OPEN 残单永不再撮合，不得压制收盘补单"
    """清单装载期校验（NaN/重复行/权重和/sell+amount/未知action）"""

    def _run(self, tmp_path, csv_text, extra=()):
        sig = tmp_path / 'sig.csv'
        sig.write_text(csv_text, encoding='utf-8')
        r = subprocess.run(
            [sys.executable, '-m', 'diepi',
             '--signals', str(sig),
             '--symbols', '510300.SH', '--start', '20240102', '--end', '20240105',
             '--cash', '1000000', '--output-dir', str(tmp_path), '--name', 'run',
             '--daily-open-cap-yuan', '1000000000000',
             '--daily-close-cap-yuan', '1000000000000',
             '-q', *extra],
            capture_output=True, text=True, cwd=str(ROOT),
            encoding='utf-8', errors='replace', timeout=300)
        return r

    @pytest.mark.integration
    def test_nan_target_weight_rejected_at_load(self, tmp_path):
        r = self._run(tmp_path,
                      "date,symbol,target_weight\n"
                      "20240102,510300.SH,0.5\n"
                      "20240103,510300.SH,\n")
        assert r.returncode != 0
        assert '空值' in r.stderr

    @pytest.mark.integration
    def test_non_numeric_target_weight_diagnosed_with_row(self, tmp_path):
        """核查组新P2：'abc' 曾死于裸 pandas TypeError 无行号；现给出首行定位"""
        r = self._run(tmp_path,
                      "date,symbol,target_weight\n"
                      "20240102,510300.SH,abc\n")
        assert r.returncode != 0
        assert '非数值' in r.stderr
        assert '20240102' in r.stderr  # 指认坏行

    @pytest.mark.integration
    def test_duplicate_rows_last_wins(self, tmp_path):
        """同日同标的重复行：末行 0.0 生效（声明式）→ 无成交"""
        r = self._run(tmp_path,
                      "date,symbol,target_weight\n"
                      "20240102,510300.SH,0.9\n"
                      "20240102,510300.SH,0.0\n")
        assert r.returncode == 0, r.stderr[-400:]
        assert '重复' in r.stderr
        m = json.loads(r.stdout)['metrics']
        assert m['trade_count'] == 0  # 末行 0.0：空仓清仓=无动作

    @pytest.mark.integration
    def test_daily_weight_sum_over_one_rejected(self, tmp_path):
        r = self._run(tmp_path,
                      "date,symbol,target_weight\n"
                      "20240102,510300.SH,0.7\n"
                      "20240102,159915.SZ,0.5\n",
                      extra=())
        assert r.returncode != 0
        assert '> 1' in r.stderr

    def test_action_sell_with_amount_rejected(self, tmp_path):
        r = self._run(tmp_path,
                      "date,symbol,action,amount\n"
                      "20240102,510300.SH,buy,500000\n"
                      "20240103,510300.SH,sell,200000\n")
        assert r.returncode != 0
        assert 'amount' in r.stderr

    @pytest.mark.integration
    def test_unknown_action_rejected_at_load(self, tmp_path):
        r = self._run(tmp_path,
                      "date,symbol,action,percent\n"
                      "20240102,510300.SH,hold,0.5\n")
        assert r.returncode != 0
        assert '未知 action' in r.stderr

    def test_param_signals_file_conflict_exit2(self, tmp_path):
        sig = tmp_path / 'sig.csv'
        sig.write_text("date,symbol,target_weight\n", encoding='utf-8')
        r = subprocess.run(
            [sys.executable, '-m', 'diepi',
             '--signals', str(sig), '--param', 'SIGNALS_FILE=other.csv', '-q'],
            capture_output=True, text=True, cwd=str(ROOT),
            encoding='utf-8', errors='replace', timeout=120)
        assert r.returncode == 2
        assert '冲突' in r.stderr

    @pytest.mark.integration
    def test_signals_named_orders_csv_snapshot_prefixed(self, tmp_path):
        """清单文件名撞运行输出 → 快照加 signals_ 前缀不被顶替"""
        sig = tmp_path / 'orders.csv'
        sig.write_text("date,symbol,target_weight\n20240102,510300.SH,0.5\n",
                       encoding='utf-8')
        r = subprocess.run(
            [sys.executable, '-m', 'diepi',
             '--signals', str(sig),
             '--symbols', '510300.SH', '--start', '20240102', '--end', '20240105',
             '--cash', '1000000', '--output-dir', str(tmp_path), '--name', 'run',
             '--daily-open-cap-yuan', '1000000000000',
             '--daily-close-cap-yuan', '1000000000000',
             '-q'],
            capture_output=True, text=True, cwd=str(ROOT),
            encoding='utf-8', errors='replace', timeout=300)
        assert r.returncode == 0, r.stderr[-400:]
        snap = tmp_path / 'run' / 'signals_orders.csv'
        assert snap.exists()
        assert 'target_weight' in snap.read_text(encoding='utf-8')


@pytest.mark.integration
class TestSummaryJsonRobustSerialization:
    """P2：库调用传 set/Path 参数时 summary.json 不得在收尾崩掉丢结果"""

    def test_library_call_with_set_and_path_params(self, tmp_path):
        import numpy as np
        from diepi.backtest.cli.runner import run_backtest
        result = run_backtest(
            strategy_file=str(ROOT / 'examples' / 'ma_cross_strategy.py'),
            start_date='20240102', end_date='20240110',
            initial_cash=1_000_000.0,
            output_dir=tmp_path, run_name='ser',
            pool_symbols=['510300.SH'],
            daily_open_cap_yuan=1_000_000_000_000.0,
            daily_close_cap_yuan=1_000_000_000_000.0,
            t0_overrides={'511', '513'},
            strategy_params={'DATA_DIR': Path('nonexistent-data-dir'),
                             'TOP_N': np.int64(7)},
            verbose=False,
        )
        assert result is not None
        summary = json.loads(
            (tmp_path / 'ser' / 'summary.json').read_text(encoding='utf-8'))
        params = summary['meta']['parameters']['strategy_params']
        # 核查组新P2：np.int64 曾被 str() 腐蚀成 "7"（复现凭证类型失真）
        assert params['TOP_N'] == 7 and isinstance(params['TOP_N'], int)
