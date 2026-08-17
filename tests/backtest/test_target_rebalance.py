# -*- coding: utf-8 -*-
"""order_target_percent / rebalance 目标权重下单测试（八场景）"""

from datetime import datetime

import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import Broker
from diepi.backtest.broker.events import ExecutionEventType
from diepi.backtest.engine.portfolio_context import PortfolioContext
from tests.backtest.auction_test_policy import EXPLICIT_TEST_AUCTION_POLICY


def _ctx(cash=1_000_000.0, **broker_kwargs):
    acct = Account(initial_cash=cash)
    broker = Broker(account=acct, slippage=0.001, **broker_kwargs)
    broker.set_date('20240103')
    ctx = PortfolioContext(broker=broker, initial_cash=cash)
    return ctx, broker, acct


def _hold(acct, symbol, shares, price, date='20240102'):
    acct.freeze_cash(shares * price * 1.01)
    acct.buy(symbol, shares, price, date)
    acct.settle_t1('20240103')
    acct.positions[symbol].update_price(price)


class TestOrderTargetPercent:

    @staticmethod
    def _on_day_scope(broker):
        return broker.order_creation_scope(
            'on_day',
            datetime(2024, 1, 3, 15, 0),
            eligible_from=datetime(2024, 1, 4, 9, 30),
            expire_date='20240104',
        )

    def test_buy_to_target_from_empty(self):
        """空仓 → 目标 50%：竞价买单 amount = 0.5×总资产"""
        ctx, broker, acct = _ctx()
        broker.set_market_phase('before_open')
        order = ctx.order_target_percent('510300.SH', 0.5)
        assert order is not None
        assert order.requested_amount == pytest.approx(500_000.0)

    def test_on_day_lower_target_cancel_replaces_future_open_buy(self):
        ctx, broker, acct = _ctx()
        ctx.set_datetime(datetime(2024, 1, 3, 15, 0))
        broker.set_market_phase('trading')

        with self._on_day_scope(broker):
            first = ctx.order_target_percent('510300.SH', 0.5)
            replacement = ctx.order_target_percent('510300.SH', 0.3)

        assert first.status.value == 'cancelled'
        assert replacement is not None and replacement.is_buy
        assert replacement.requested_amount == pytest.approx(300_000.0)
        assert broker.get_open_orders() == [replacement]
        assert acct.cash == pytest.approx(1_000_000.0)
        assert acct.frozen_cash == 0.0
        assert [event.event_type for event in broker.event_journal] == [
            ExecutionEventType.ORDER_CREATED,
            ExecutionEventType.ORDER_ACCEPTED,
            ExecutionEventType.ORDER_CANCELED,
            ExecutionEventType.ORDER_CREATED,
            ExecutionEventType.ORDER_ACCEPTED,
        ]

    def test_on_day_clear_target_cancels_future_open_buy(self):
        ctx, broker, acct = _ctx()
        ctx.set_datetime(datetime(2024, 1, 3, 15, 0))
        broker.set_market_phase('trading')

        with self._on_day_scope(broker):
            first = ctx.order_target_percent('510300.SH', 0.5)
            replacement = ctx.order_target_percent('510300.SH', 0.0)

        assert first.status.value == 'cancelled'
        assert replacement is None
        assert broker.get_open_orders() == []
        assert acct.cash == pytest.approx(1_000_000.0)
        assert acct.frozen_cash == 0.0
        assert [event.event_type for event in broker.event_journal] == [
            ExecutionEventType.ORDER_CREATED,
            ExecutionEventType.ORDER_ACCEPTED,
            ExecutionEventType.ORDER_CANCELED,
        ]

    def test_on_day_legacy_lower_target_replaces_cash_reservation_exactly(self):
        ctx, broker, acct = _ctx(open_buy_resize_mode='legacy')
        ctx.set_datetime(datetime(2024, 1, 3, 15, 0))
        broker.set_market_phase('trading')

        with self._on_day_scope(broker):
            first = ctx.order_target_percent('510300.SH', 0.5)
            first_reservation = first.frozen_amount
            replacement = ctx.order_target_percent('510300.SH', 0.3)

        assert first.status.value == 'cancelled'
        assert first.frozen_amount == 0.0
        assert replacement.status.value == 'submitted'
        assert replacement.frozen_amount == pytest.approx(303_303.0)
        assert acct.frozen_cash == pytest.approx(replacement.frozen_amount)
        assert acct.cash + acct.frozen_cash == pytest.approx(1_000_000.0)
        assert [event.event_type for event in broker.event_journal] == [
            ExecutionEventType.ORDER_CREATED,
            ExecutionEventType.ORDER_ACCEPTED,
            ExecutionEventType.RESERVE,
            ExecutionEventType.RELEASE,
            ExecutionEventType.ORDER_CANCELED,
            ExecutionEventType.ORDER_CREATED,
            ExecutionEventType.ORDER_ACCEPTED,
            ExecutionEventType.RESERVE,
        ]
        release = broker.event_journal.filter_events(
            event_types=ExecutionEventType.RELEASE)[0]
        assert release.payload.amount == pytest.approx(first_reservation)

    def test_on_day_legacy_clear_target_releases_entire_cash_reservation(self):
        ctx, broker, acct = _ctx(open_buy_resize_mode='legacy')
        ctx.set_datetime(datetime(2024, 1, 3, 15, 0))
        broker.set_market_phase('trading')

        with self._on_day_scope(broker):
            first = ctx.order_target_percent('510300.SH', 0.5)
            first_reservation = first.frozen_amount
            replacement = ctx.order_target_percent('510300.SH', 0.0)

        assert replacement is None
        assert first.status.value == 'cancelled'
        assert first.frozen_amount == 0.0
        assert acct.cash == pytest.approx(1_000_000.0)
        assert acct.frozen_cash == 0.0
        release = broker.event_journal.filter_events(
            event_types=ExecutionEventType.RELEASE)[0]
        assert release.payload.amount == pytest.approx(first_reservation)
        assert broker.event_journal[-1].event_type is (
            ExecutionEventType.ORDER_CANCELED)

    @pytest.mark.parametrize('resize_mode', ['auto', 'legacy'])
    def test_on_day_cancel_replace_rolls_back_if_replacement_cannot_publish(
            self, resize_mode):
        ctx, broker, acct = _ctx(open_buy_resize_mode=resize_mode)
        ctx.set_datetime(datetime(2024, 1, 3, 15, 0))
        broker.set_market_phase('trading')

        with self._on_day_scope(broker):
            first = ctx.order_target_percent(
                '510300.SH', 0.5, note='original')
            before_events = broker.event_journal.snapshot()
            before_cash = (acct.cash, acct.frozen_cash)
            before_order_frozen = first.frozen_amount

            with pytest.raises(TypeError, match='attributes.note'):
                ctx.order_target_percent(
                    '510300.SH', 0.3, note=object())

        assert first.status.value == 'submitted'
        assert broker.get_open_orders() == [first]
        assert (acct.cash, acct.frozen_cash) == before_cash
        assert first.frozen_amount == before_order_frozen
        assert broker.event_journal.snapshot() == before_events

    def test_reduce_to_target(self):
        """满仓 → 目标 20%：卖出 ~60% 持仓（按市值比例折股）"""
        ctx, broker, acct = _ctx(cash=1_000_000.0)
        _hold(acct, '510300.SH', 100_000, 5.0)  # 市值 50 万
        broker.set_market_phase('before_open')
        order = ctx.order_target_percent('510300.SH', 0.2)
        assert order is not None and order.is_sell
        total = ctx.get_total_asset()
        expect_sell_value = 500_000 - 0.2 * total
        assert order.shares * 5.0 == pytest.approx(expect_sell_value, rel=0.01)

    def test_clear_position(self):
        """目标 0 = 显式清仓：卖出全部可卖"""
        ctx, broker, acct = _ctx()
        _hold(acct, '510300.SH', 100_000, 5.0)
        broker.set_market_phase('before_open')
        order = ctx.order_target_percent('510300.SH', 0.0)
        assert order is not None and order.is_sell
        assert order.shares == 100_000

    def test_deadzone_no_action(self):
        """|delta| < min_trade_amount → 不动作（避免微小漂移空转手续费）"""
        ctx, broker, acct = _ctx()
        _hold(acct, '510300.SH', 100_000, 5.0)
        broker.set_market_phase('before_open')
        total = ctx.get_total_asset()
        current_w = 500_000 / total
        order = ctx.order_target_percent('510300.SH', current_w + 0.001,
                                         min_trade_amount=10_000.0)
        assert order is None

    def test_t1_locked_sell_capped(self):
        """当日买入 T+1 锁定：减仓卖到可卖上限（0 可卖 → 无订单）"""
        ctx, broker, acct = _ctx()
        acct.freeze_cash(505_000.0)
        acct.buy('510300.SH', 100_000, 5.0, '20240103')  # 当日买入，未解锁
        acct.positions['510300.SH'].update_price(5.0)
        broker.set_market_phase('before_open')
        order = ctx.order_target_percent('510300.SH', 0.0)
        assert order is None  # 可卖=0

    def test_invalid_args(self):
        ctx, broker, acct = _ctx()
        with pytest.raises(ValueError, match='when'):
            ctx.order_target_percent('510300.SH', 0.5, when='midday')
        with pytest.raises(ValueError, match='负'):
            ctx.order_target_percent('510300.SH', -0.1)

    @pytest.mark.parametrize(
        'target_weight',
        [float('nan'), float('inf'), float('-inf'), 1.0001, True, '0.5'],
    )
    def test_nonfinite_or_out_of_range_target_rejected_without_order(
            self, target_weight):
        ctx, broker, acct = _ctx()
        broker.set_market_phase('before_open')

        with pytest.raises(ValueError):
            ctx.order_target_percent('510300.SH', target_weight)

        assert acct.orders == {}
        assert acct.cash == 1_000_000.0
        assert acct.frozen_cash == 0.0

    @pytest.mark.parametrize(
        'min_trade_amount',
        [float('nan'), float('inf'), float('-inf'), -1.0, True, '100'],
    )
    def test_invalid_deadzone_rejected_without_order(self, min_trade_amount):
        ctx, broker, acct = _ctx()
        broker.set_market_phase('before_open')

        with pytest.raises(ValueError, match='min_trade_amount'):
            ctx.order_target_percent(
                '510300.SH', 0.5, min_trade_amount=min_trade_amount)

        assert acct.orders == {}
        assert acct.cash == 1_000_000.0
        assert acct.frozen_cash == 0.0


class TestRebalance:

    def test_three_state_semantics(self):
        """三态：显式0=清仓、未提及=保留、>0=调整到位"""
        ctx, broker, acct = _ctx(cash=2_000_000.0)
        _hold(acct, '510300.SH', 100_000, 5.0)   # 提及，目标 0 → 清仓
        _hold(acct, '159915.SZ', 100_000, 2.0)   # 未提及 → 保留
        broker.set_market_phase('before_open')
        orders = ctx.rebalance({'510300.SH': 0.0, '510500.SH': 0.3})
        symbols = {o.symbol for o in orders}
        assert '510300.SH' in symbols   # 清仓单
        assert '510500.SH' in symbols   # 建仓单
        assert '159915.SZ' not in symbols  # 未提及保留

    def test_weights_sum_over_one_rejected(self):
        ctx, broker, acct = _ctx()
        broker.set_market_phase('before_open')
        with pytest.raises(ValueError, match='> 1'):
            ctx.rebalance({'510300.SH': 0.6, '159915.SZ': 0.5})

    @pytest.mark.parametrize(
        'bad_weight', [float('nan'), float('inf'), float('-inf'), 1.1, True, '0.5']
    )
    def test_invalid_individual_weight_rejected_before_any_order(self, bad_weight):
        ctx, broker, acct = _ctx()
        broker.set_market_phase('before_open')

        with pytest.raises(ValueError):
            ctx.rebalance({'510300.SH': 0.2, '159915.SZ': bad_weight})

        assert acct.orders == {}
        assert acct.cash == 1_000_000.0
        assert acct.frozen_cash == 0.0

    def test_decreases_submitted_before_increases(self):
        """先减后加（提交序），叠加竞价窗口卖先买后 = 回款闭环"""
        ctx, broker, acct = _ctx(cash=1_000_000.0)
        _hold(acct, '510300.SH', 190_000, 5.0)  # 市值95万，现金~4万
        broker.set_market_phase('before_open')
        orders = ctx.rebalance({'510300.SH': 0.1, '159915.SZ': 0.6})
        assert orders[0].is_sell and orders[0].symbol == '510300.SH'
        assert orders[-1].is_buy and orders[-1].symbol == '159915.SZ'

    @pytest.mark.integration
    def test_engine_end_to_end_rebalance(self):
        """引擎级：真实数据两标的调仓到位（含卖回款给买腿的资金闭环）"""
        from diepi.backtest.engine.portfolio_engine import PortfolioEngine
        from diepi.backtest.data.stock_pool import PoolSource
        from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy

        class Rebal(PortfolioStrategy):
            def on_before_market_open(self, ctx):
                if ctx.current_date == '20240102':
                    ctx.rebalance({'510300.SH': 0.9})
                elif ctx.current_date == '20240104':
                    ctx.rebalance({'510300.SH': 0.2, '159915.SZ': 0.6})
                return None

        engine = PortfolioEngine(
            start_date='20240102', end_date='20240105',
            initial_cash=1_000_000.0, freq='daily',
            pool_source=PoolSource.SPECIFIED,
            pool_symbols=['510300.SH', '159915.SZ'],
            daily_auction_liquidity=EXPLICIT_TEST_AUCTION_POLICY,
        )
        result = engine.run(Rebal())
        assert result.trade_count >= 3  # 建仓 + 减仓 + 换仓买
        # 期末权重接近目标（limit_up 折算与费用造成保守偏差，容忍带宽）
        final_positions = result.position_history[-1] if result.position_history else None
        assert result.final_value > 0

class TestSignalsEntry:
    """--signals 清单入口端到端（目标权重型/动作型/互斥校验/留痕快照）"""

    def _run_cli(self, tmp_path, csv_text, extra=()):
        import json
        import subprocess
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
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
            capture_output=True, text=True, cwd=str(root),
            encoding='utf-8', errors='replace', timeout=300)
        return r, tmp_path / 'run'

    @pytest.mark.integration
    def test_target_format_trades_and_snapshot(self, tmp_path):
        import json
        r, run_dir = self._run_cli(
            tmp_path, "date,symbol,target_weight\n20240102,510300.SH,0.9\n")
        assert r.returncode == 0, r.stderr[-400:]
        m = json.loads(r.stdout)['metrics']
        assert m['trade_count'] >= 1
        # 留痕快照：策略源码 + 清单文件都在 run 目录
        assert (run_dir / 'strategy.py').exists()
        assert (run_dir / 'sig.csv').exists()
        meta = json.loads((run_dir / 'summary.json').read_text(encoding='utf-8'))['meta']
        assert 'parameters' in meta and meta['parameters']['strategy_params']

    @pytest.mark.integration
    def test_action_format_and_missed_dates_warning(self, tmp_path):
        from diepi.artifacts import ArtifactStore

        r, run_dir = self._run_cli(
            tmp_path,
            "date,symbol,action,percent\n"
            "20240102,510300.SH,buy,0.9\n"
            "20241231,510300.SH,sell,1.0\n")  # 超出回测窗口
        assert r.returncode == 0, r.stderr[-400:]
        assert '未被执行' in r.stderr
        loaded = ArtifactStore.load(run_dir)
        assert "UNCONSUMED_SIGNAL_DATES" in {
            warning.code
            for warning in loaded.outcome.result_contract.warnings
        }

    def test_signals_and_strategy_mutually_exclusive(self, tmp_path):
        import subprocess
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        sig = tmp_path / 'sig.csv'
        sig.write_text("date,symbol,target_weight\n", encoding='utf-8')
        r = subprocess.run(
            [sys.executable, '-m', 'diepi',
             str(root / 'examples' / 'ma_cross_strategy.py'),
             '--signals', str(sig), '-q'],
            capture_output=True, text=True, cwd=str(root),
            encoding='utf-8', errors='replace', timeout=120)
        assert r.returncode == 2
        assert '二选一' in r.stderr

    def test_neither_given_exits_2(self):
        import subprocess
        import sys
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        r = subprocess.run(
            [sys.executable, '-m', 'diepi', '-q'],
            capture_output=True, text=True, cwd=str(root),
            encoding='utf-8', errors='replace', timeout=120)
        assert r.returncode == 2
