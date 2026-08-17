# -*- coding: utf-8 -*-
"""开源前体检批次一/二修复的回归测试

教训背景：此前 A6（新股豁免）与 A9（percent 文档）曾"声称已修实未修"，
根因是修复未配端到端测试。本文件为体检确认的每一项修复上锁。
"""

from datetime import datetime

import pandas as pd
import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import Broker, BarData
from diepi.backtest.broker.order import OrderStatus
from tests.backtest.auction_test_policy import EXPLICIT_TEST_AUCTION_POLICY


def _bar(sym, o, pre=None, amount=None, when=None):
    return BarData(
        symbol=sym,
        trade_time=when or datetime(2024, 1, 3, 9, 30),
        open=o, high=o * 1.02, low=o * 0.98, close=o * 1.01,
        vol=10_000_000, amount=amount if amount is not None else o * 10_000_000,
        pre_close=pre if pre is not None else o,
    )


class TestCancelLeakP0:
    """P0：撤单后的竞价单不得再执行/冻结资金"""

    def test_cancel_close_auction_buy_no_leak(self):
        acct = Account(initial_cash=1_000_000.0)
        b = Broker(account=acct)
        b.set_date('20240103')
        b.set_market_phase('trading')
        o = b.buy_at_close('000001.SZ', percent=0.5, current_price=10.0)
        assert b.cancel_order(o.order_id) is True
        b.execute_close_orders({'000001.SZ': _bar('000001.SZ', 10.0)})
        assert acct.frozen_cash == 0.0
        assert acct.cash == 1_000_000.0
        assert o.status == OrderStatus.CANCELLED  # 不得被改写为 REJECTED
        # order_history 不得出现重复条目
        assert len([x for x in acct.order_history if x.order_id == o.order_id]) == 1

    def test_cancel_open_auction_buy_no_leak(self):
        acct = Account(initial_cash=1_000_000.0)
        b = Broker(account=acct)
        b.set_date('20240103')
        b.set_market_phase('before_open')
        b.buy_at_open('000001.SZ', percent=0.5)
        b.cancel_all_orders()
        b.set_market_phase('trading')
        b.execute_open_orders({'000001.SZ': _bar('000001.SZ', 10.0)})
        assert acct.frozen_cash == 0.0
        assert acct.cash == 1_000_000.0

    def test_next_day_full_position_not_starved(self):
        """撤单泄漏的后果面：次日满仓单不得被饿死"""
        acct = Account(initial_cash=1_000_000.0)
        b = Broker(account=acct)
        b.set_date('20240103')
        b.set_market_phase('trading')
        o = b.buy_at_close('000001.SZ', percent=0.5, current_price=10.0)
        b.cancel_order(o.order_id)
        b.execute_close_orders({'000001.SZ': _bar('000001.SZ', 10.0)})
        b.on_day_start('20240104')
        b.set_date('20240104')
        b.set_market_phase('before_open')
        o2 = b.buy_at_open('000001.SZ', percent=0.99)
        b.set_market_phase('trading')
        b.execute_open_orders({
            '000001.SZ': _bar(
                '000001.SZ', 10.0,
                when=datetime(2024, 1, 4, 9, 30),
            )
        })
        # 涨停价 11.0 折算：0.99×100万/11 ≈ 90000 股（若资金被冻死只能买一半）
        assert o2.filled_shares >= 89000


class TestAfterCloseOrderRejection:
    """盘后挂盘中订单显式拒单（不再静默挂到日终被撤）"""

    def test_after_close_limit_buy_rejected(self):
        b = Broker(account=Account(initial_cash=1_000_000.0))
        b.set_date('20240103')
        b.set_market_phase('after_close')
        o = b.buy_at_price('000001.SZ', price=9.5, shares=1000)
        assert o.status == OrderStatus.REJECTED
        assert '盘后' in (o.reject_reason or '')

    def test_after_close_stop_sell_rejected(self):
        acct = Account(initial_cash=100_000.0)
        b = Broker(account=acct)
        acct.freeze_cash(50_000.0)
        acct.buy('000001.SZ', 1000, 10.0, '20240102')
        acct.settle_t1('20240103')
        b.set_date('20240103')
        b.set_market_phase('after_close')
        o = b.sell_stop_loss('000001.SZ', price=9.0, shares=1000)
        assert o.status == OrderStatus.REJECTED

    def test_trading_phase_orders_still_ok(self):
        b = Broker(account=Account(initial_cash=1_000_000.0))
        b.set_date('20240103')
        b.set_market_phase('trading')
        o = b.buy_at_price('000001.SZ', price=9.5, shares=1000)
        assert o.status != OrderStatus.REJECTED


class TestSizingPriceAudit:
    """竞价单委托量折算价的审计可见性"""

    def test_sizing_price_recorded_on_order(self):
        acct = Account(initial_cash=100_000.0)
        b = Broker(account=acct, slippage=0.001)
        b.set_date('20240103')
        b.set_market_phase('before_open')
        o = b.buy_at_open('000002.SZ', percent=0.9)
        b.set_market_phase('trading')
        b.execute_open_orders({'000002.SZ': _bar('000002.SZ', 20.0, pre=20.0)})
        assert o.sizing_price == pytest.approx(22.0)  # 涨停价折算
        assert 'sizing_price' in o.to_dict()


class TestInstrumentsUnification:
    """场内基金判定统一（历史P0：51.8% 基金文件路由不到）"""

    def test_new_prefix_funds_recognized(self):
        from diepi.backtest.instruments import is_exchange_fund
        for s in ('563300.SH', '560010.SH', '589000.SH', '517180.SH',
                  '501018.SH', '508000.SH', '160223.SZ', '159915.SZ', '180101.SZ'):
            assert is_exchange_fund(s), s

    def test_stocks_not_funds(self):
        from diepi.backtest.instruments import is_exchange_fund
        for s in ('000001.SZ', '300750.SZ', '600000.SH', '688012.SH',
                  '920002.BJ', '430047.BJ'):
            assert not is_exchange_fund(s), s

    def test_normalize_bare_fund_codes(self):
        from diepi.backtest.data.data_provider import ParameterValidator
        assert ParameterValidator.normalize_symbol('563300') == '563300.SH'
        assert ParameterValidator.normalize_symbol('160223') == '160223.SZ'
        assert ParameterValidator.normalize_symbol('920002') == '920002.BJ'

    def test_stamp_and_tick_consistent_with_routing(self):
        """三份名单统一后：能路由到的基金，印花税/tick 判定必须一致"""
        from diepi.backtest.instruments import is_exchange_fund
        acct = Account(stamp_duty_rate='auto')
        b = Broker(account=acct)
        for s in ('563300.SH', '589000.SH', '160223.SZ'):
            assert is_exchange_fund(s)
            assert acct.resolve_stamp_rate(s, '20240103') == 0.0
            assert b._price_decimals(s) == 3

    @pytest.mark.integration
    def test_new_prefix_fund_data_readable(self):
        """数据层实测：新段基金必须能读到行情"""
        from diepi.backtest.data.data_provider import DataProvider
        dp = DataProvider()
        df = dp.get_daily('563300.SH', start='20250101', end='20250601',
                          price_mode='execution')
        assert df is not None and len(df) > 0, "563300.SH（A500 新段）应能读到数据"


@pytest.mark.integration
class TestListingRulesActuallyWorks:
    """A6 二次修复：新股豁免这次必须真的生效（此前索引/列错配全量失效）"""

    def test_exempt_dates_nonempty_for_new_stock(self):
        from diepi.backtest.data.data_provider import DataProvider
        from diepi.backtest.engine.listing_rules import compute_limit_exempt_dates
        dp = DataProvider()
        r = compute_limit_exempt_dates(dp, ['001360.SZ'], backtest_start='20230101')
        assert len(r.get('001360.SZ', set())) == 5

    def test_engine_injects_exempt_and_fills_on_big_day(self):
        """001360.SZ 上市次日 +10.5%（现实真实成交日）必须能买入"""
        from diepi.backtest.engine.backtest_engine import BacktestEngine
        from diepi.backtest.strategy.base import Strategy

        class Buy(Strategy):
            def on_before_market_open(self, ctx):
                pos = ctx.get_position()
                if (pos is None or pos.shares == 0) and ctx.current_date == '20230411':
                    ctx.buy_at_open(percent=0.9)

        e = BacktestEngine(symbol='001360.SZ', start_date='20230410',
                           end_date='20230414', initial_cash=100000, freq='daily',
                           daily_auction_liquidity=(
                               EXPLICIT_TEST_AUCTION_POLICY))
        res = e.run(Buy())
        assert res.trade_count > 0
        assert e._context._broker._limit_check_exempt.get('001360.SZ')


class TestPercentDocsFixed:
    """A9 二次修复：percent 文档不得再写『占总资产比例』"""

    def test_no_stale_percent_docs(self):
        import io
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        stale = []
        for rel in ('diepi/backtest/broker/broker.py',
                    'diepi/backtest/engine/context.py',
                    'diepi/backtest/engine/portfolio_context.py',
                    'diepi/backtest/ui/widgets/api_panel.py'):
            t = io.open(root / rel, encoding='utf-8').read()
            for i, line in enumerate(t.splitlines(), 1):
                if 'percent' in line and '占总资产比例' in line:
                    stale.append(f"{rel}:{i}")
        assert not stale, f"percent 文档仍写『占总资产比例』: {stale}"


class TestDeterminismP0:
    """P0：回测结果必须与哈希种子无关"""

    def test_active_symbols_sorted_in_source(self):
        import io
        from pathlib import Path
        src = io.open(Path(__file__).resolve().parents[2] / 'diepi' / 'backtest' /
                      'engine' / 'portfolio_engine.py',
                      encoding='utf-8').read()
        assert 'list(set(selected_symbols' not in src
        assert 'sorted(set(selected_symbols)' in src

    @pytest.mark.integration
    def test_cross_hashseed_reproducible(self):
        """不同 PYTHONHASHSEED 下同一回测结果必须一致（现金竞争场景）"""
        import json
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        code = (
            "def on_before_market_open(ctx):\n"
            "    return ['510300.SH','510500.SH','159915.SZ','510050.SH','588000.SH']\n"
            "def on_day(ctx, bars):\n"
            "    for s in bars.symbols():\n"
            "        if not ctx.get_position(s) or ctx.get_position(s).shares == 0:\n"
            "            ctx.buy_at_close(s, percent=0.5, current_price=bars.get(s).close)\n"
        )
        with tempfile.TemporaryDirectory() as td:
            strat = Path(td) / 'strat.py'
            strat.write_text(code, encoding='utf-8')
            finals = []
            for seed in ('1', '2'):
                env = {**os.environ, 'PYTHONHASHSEED': seed}
                r = subprocess.run(
                    [sys.executable, '-m', 'diepi', str(strat),
                     '--symbols', '510300.SH,510500.SH,159915.SZ,510050.SH,588000.SH',
                     '--start', '20240102', '--end', '20240104',
                     '--cash', '1000000',
                     '--daily-close-cap-yuan', '1000000000000',
                     '--output-dir', td, '-q'],
                    capture_output=True, text=True, env=env, cwd=str(root),
                    encoding='utf-8', errors='replace', timeout=300)
                assert r.returncode == 0, r.stderr[-500:]
                out = json.loads(r.stdout)
                finals.append(out['metrics']['final_asset'])
            assert finals[0] == finals[1], f"跨哈希种子结果不一致: {finals}"


@pytest.mark.integration
class TestNonePoolContract:
    """P0：on_before_market_open 返回 None = 全部股票池（docstring 契约）"""

    def test_only_on_day_strategy_trades(self):
        from diepi.backtest.engine.portfolio_engine import PortfolioEngine
        from diepi.backtest.data.stock_pool import PoolSource
        from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy

        class OnlyOnDay(PortfolioStrategy):
            # 不重写 on_before_market_open（基类返回 None）
            def on_day(self, ctx, bars):
                for s in bars.symbols():
                    pos = ctx.get_position(s)
                    if pos is None or pos.shares == 0:
                        bar = bars.get(s)
                        ctx.buy_at_close(s, percent=0.5, current_price=bar.close)

        engine = PortfolioEngine(
            start_date='20240102', end_date='20240105',
            initial_cash=1_000_000.0, freq='daily',
            pool_source=PoolSource.SPECIFIED, pool_symbols=['510300.SH'],
            daily_auction_liquidity=EXPLICIT_TEST_AUCTION_POLICY,
        )
        result = engine.run(OnlyOnDay())
        assert result.trade_count > 0, "只写 on_day 的最小策略必须能成交（None=全池契约）"


class TestPortfolioContextCancel:
    """GUI 速查宣传的撤单 API 必须真实存在于组合上下文"""

    def test_cancel_methods_exist_and_forward(self):
        from diepi.backtest.engine.portfolio_context import PortfolioContext
        for m in ('cancel_order', 'cancel_orders', 'cancel_all_orders'):
            assert hasattr(PortfolioContext, m), m


class TestCliClassStrategyDetection:
    """CLI 不再静默吞类式策略"""

    def test_class_style_auto_detected(self):
        from diepi.backtest.cli.runner import compile_strategy
        code = (
            "from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy\n"
            "class MyStrat(PortfolioStrategy):\n"
            "    def on_day(self, ctx, bars):\n"
            "        pass\n"
        )
        cls = compile_strategy(code)
        assert cls.__name__ == 'MyStrat'

    def test_empty_file_raises(self):
        from diepi.backtest.cli.runner import compile_strategy
        with pytest.raises(ValueError, match='模块级回调'):
            compile_strategy("X = 1\n")

    def test_gui_and_parallel_share_single_source(self):
        """修后审查P1：类式检测必须覆盖 GUI worker 与 parallel_runner
        （此前两处是旧注入逻辑的逐字拷贝，同型缺陷在更主要入口存活）"""
        from diepi.backtest.engine.parallel_runner import _compile_strategy_in_subprocess
        code = (
            "from diepi.backtest.strategy import Strategy\n"
            "class MyStrat(Strategy):\n"
            "    def on_day(self, ctx, bar):\n"
            "        pass\n"
        )
        assert _compile_strategy_in_subprocess(code).__name__ == 'MyStrat'
        wrong_kind = (
            "from diepi.backtest.strategy import PortfolioStrategy\n"
            "class Wrong(PortfolioStrategy):\n"
            "    pass\n"
        )
        with pytest.raises(ValueError, match="strategy_kind='single'"):
            _compile_strategy_in_subprocess(wrong_kind)
        with pytest.raises(ValueError, match='模块级回调'):
            _compile_strategy_in_subprocess("X = 1\n")
        # 源码层守卫：两处不得再保有独立的注入实现
        import io
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        for rel in ('diepi/backtest/ui/worker.py',
                    'diepi/backtest/engine/parallel_runner.py'):
            src = io.open(root / rel, encoding='utf-8').read()
            assert 'from ..cli.runner import compile_strategy' in src, rel
            assert "setattr(DynamicStrategy" not in src, f"{rel} 仍有独立注入实现"

    def test_mixed_function_and_class_warns(self, capsys):
        """函数+类混合文件：类被忽略时必须显式告警"""
        from diepi.backtest.cli.runner import compile_strategy
        code = (
            "from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy\n"
            "class MyStrat(PortfolioStrategy):\n"
            "    pass\n"
            "def on_day(ctx, bars):\n"
            "    pass\n"
        )
        cls = compile_strategy(code)
        assert cls.__name__ == 'DynamicStrategy'  # 函数式优先
        assert 'MyStrat' in capsys.readouterr().err  # 告警点名被忽略的类


class TestSizingPriceInTrades:
    """修后审查P1：sizing_price 必须落盘到组合 trades（orders.csv 数据源）"""

    @pytest.mark.integration
    def test_portfolio_trades_contain_sizing_price(self):
        from diepi.backtest.engine.portfolio_engine import PortfolioEngine
        from diepi.backtest.data.stock_pool import PoolSource
        from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy

        class BuyOpen(PortfolioStrategy):
            def on_before_market_open(self, ctx):
                if not ctx.get_positions():
                    for s in ctx.get_stock_pool():
                        ctx.buy_at_open(s, percent=0.9)
                return None

        engine = PortfolioEngine(
            start_date='20240102', end_date='20240103',
            initial_cash=1_000_000.0, freq='daily',
            pool_source=PoolSource.SPECIFIED, pool_symbols=['510300.SH'],
            daily_auction_liquidity=EXPLICIT_TEST_AUCTION_POLICY,
        )
        result = engine.run(BuyOpen())
        buys = [t for t in result.trades if t.get('direction') == 'BUY']
        assert buys, "应有买入成交"
        assert buys[0].get('sizing_price') is not None, \
            "组合 trades（orders.csv 数据源）必须含 sizing_price"


class TestTradeCalRobustness:
    """trade_cal 多交易所去重 + is_open 字符串容错"""

    def test_dedupe_and_string_is_open(self):
        from diepi.backtest.data.data_provider import DateHelper
        from diepi.backtest.data.exceptions import DataNotFoundError

        class _StubCache:
            def get_trade_cal(self):
                return pd.DataFrame({
                    'exchange': ['SSE', 'SZSE', 'SSE', 'SZSE'],
                    'cal_date': ['20240102', '20240102', '20240103', '20240103'],
                    'is_open': ['1', '1', '1', '1'],  # 字符串型
                    'pretrade_date': ['20231229'] * 4,
                })

        dh = DateHelper(_StubCache())
        days = dh.get_trade_days_between('20240102', '20240103')
        assert days == ['20240102', '20240103'], f"应去重且容错字符串 is_open: {days}"
        with pytest.raises(DataNotFoundError, match="coverage does not cover"):
            dh.get_trade_days_between('20240101', '20240105')


class TestNaNBarGuard:
    """NaN bar 不得进入撮合层污染账户"""

    def test_is_valid_ohlc_row(self):
        from diepi.backtest.engine.backtest_engine import _is_valid_ohlc_row
        ok = pd.Series({'open': 10.0, 'high': 10.5, 'low': 9.8, 'close': 10.2})
        assert _is_valid_ohlc_row(ok)
        for col in ('open', 'high', 'low', 'close'):
            bad = ok.copy()
            bad[col] = float('nan')
            assert not _is_valid_ohlc_row(bad), col
        zero = ok.copy()
        zero['close'] = 0.0
        assert not _is_valid_ohlc_row(zero)


class TestSleepGating:
    """纯 CLI/API 场景不再执行 GIL 让出 sleep"""

    def test_no_yield_without_gui_callbacks(self):
        from diepi.backtest.engine.portfolio_engine import PortfolioEngine
        e = PortfolioEngine(start_date='20240102', end_date='20240103')
        assert e._needs_gil_yield is False
        e2 = PortfolioEngine(start_date='20240102', end_date='20240103',
                             daily_callback=lambda d: None)
        assert e2._needs_gil_yield is True

    def test_portfolio_sharpe_uses_tdy(self):
        import io
        from pathlib import Path
        src = io.open(Path(__file__).resolve().parents[2] / 'diepi' / 'backtest' /
                      'engine' / 'portfolio_engine.py',
                      encoding='utf-8').read()
        assert '(252 ** 0.5)' not in src, "组合夏普年化因子不得硬编码 √252"
