# -*- coding: utf-8 -*-
"""
CLI 参数化测试

覆盖 F4：
- --param KEY=VALUE 策略模块级参数注入 (compile_strategy strategy_params)
- --limit-pct / --param 的解析
- 费用参数 (min_commission 等) 引擎透传
"""

import pytest
import os
import stat
from types import SimpleNamespace

from diepi import cli
from diepi.backtest.cli.runner import (
    _index_lock,
    _result_contract_lines,
    _validate_index_lock_stat,
    compile_strategy,
)
from diepi.backtest.result_contract import (
    ActualInterval,
    DataCoverage,
    ResultAssumption,
    ResultContract,
    ResultStatus,
    ResultWarning,
)


class TestParamParsing:
    """``python -m diepi`` 的参数解析。"""

    def test_parse_param_value_types(self):
        assert cli._parse_param_value('10') == 10
        assert cli._parse_param_value('0.5') == 0.5
        assert cli._parse_param_value('true') is True
        assert cli._parse_param_value('False') is False
        assert cli._parse_param_value('000001.SZ') == '000001.SZ'

    def test_parse_params(self):
        params = cli._parse_params(['TOP_N=10', 'HOLD_DAYS=5', 'NAME=abc'])
        assert params == {'TOP_N': 10, 'HOLD_DAYS': 5, 'NAME': 'abc'}

    def test_parse_params_rejects_bad_format(self):
        with pytest.raises(ValueError):
            cli._parse_params(['TOP_N'])
        with pytest.raises(ValueError):
            cli._parse_params(['1BAD=1'])

    def test_parse_limit_pct(self):
        assert cli._parse_limit_pct('159781=0.20,510300=0.1') == {
            '159781': 0.20, '510300': 0.1,
        }
        assert cli._parse_limit_pct(None) is None
        assert cli._parse_limit_pct('') is None

    def test_stamp_duty_defaults_to_instrument_aware_auto(self):
        args = cli._run_parser().parse_args(["strategy.py"])

        assert args.stamp_duty == "auto"


def test_index_lock_rejects_hard_link_without_touching_target(tmp_path):
    output = tmp_path / 'results'
    output.mkdir()
    external = tmp_path / 'external.txt'
    external.write_bytes(b'private sentinel')
    os.link(external, output / '.index.csv.lock')

    with pytest.raises(ValueError, match='硬链接'):
        with _index_lock(output):
            pass

    assert external.read_bytes() == b'private sentinel'


def test_index_lock_rejects_non_regular_and_reparse_nodes():
    with pytest.raises(ValueError, match='普通文件'):
        _validate_index_lock_stat(
            SimpleNamespace(
                st_mode=stat.S_IFDIR,
                st_nlink=1,
                st_file_attributes=0,
            ),
            'lock',
        )

    with pytest.raises(ValueError, match='链接或重解析点'):
        _validate_index_lock_stat(
            SimpleNamespace(
                st_mode=stat.S_IFREG,
                st_nlink=1,
                st_file_attributes=getattr(
                    stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400
                ),
            ),
            'lock',
        )

    def test_signal_symbols_are_inferred_as_strings(self, tmp_path):
        path = tmp_path / 'signals.csv'
        path.write_text(
            'date,symbol,target_weight\n'
            '20240102,000001.SZ,0.5\n'
            '20240103,600000.SH,0.5\n'
            '20240104,000001.SZ,0.0\n',
            encoding='utf-8',
        )

        assert cli._infer_signal_symbols(path) == [
            '000001.SZ', '600000.SH']

    def test_signal_symbol_inference_requires_column(self, tmp_path):
        path = tmp_path / 'signals.csv'
        path.write_text(
            'date,target_weight\n20240102,0.5\n', encoding='utf-8')

        with pytest.raises(ValueError, match='symbol'):
            cli._infer_signal_symbols(path)


class TestStrategyParamInjection:
    """compile_strategy 的 strategy_params 注入"""

    CODE = """
TOP_N = 5

def on_init(ctx):
    ctx._test_top_n = TOP_N
"""

    def test_default_module_constant(self):
        cls = compile_strategy(self.CODE)
        strategy = cls()

        class _Ctx:
            pass

        ctx = _Ctx()
        strategy.on_init(ctx)
        assert ctx._test_top_n == 5

    def test_param_overrides_module_constant(self):
        cls = compile_strategy(self.CODE, strategy_params={'TOP_N': 42})
        strategy = cls()

        class _Ctx:
            pass

        ctx = _Ctx()
        strategy.on_init(ctx)
        assert ctx._test_top_n == 42

    def test_all_public_lifecycle_callbacks_are_compiled(self):
        code = """
def on_after_open(ctx, bars):
    ctx.after_open = bars

def on_before_close(ctx):
    ctx.before_close = True
"""
        strategy = compile_strategy(code)()

        class _Ctx:
            pass

        ctx = _Ctx()
        marker = object()
        strategy.on_after_open(ctx, marker)
        strategy.on_before_close(ctx)
        assert ctx.after_open is marker
        assert ctx.before_close is True


def test_terminal_contract_summary_precedes_metrics_and_discloses_warnings():
    contract = ResultContract(
        status=ResultStatus.SUCCESS,
        warnings=(ResultWarning("WINDOW_TRUNCATED", "使用实际数据尾日"),),
        assumptions=(
            ResultAssumption("execution.frequency", "daily"),
            ResultAssumption("model.extra", "documented"),
        ),
        actual_interval=ActualInterval("2024-01-02", "2024-01-03"),
        data_coverage=DataCoverage(2, 2, 1.0),
    )

    lines = _result_contract_lines(contract)

    assert lines[0] == "结果状态:     SUCCESS"
    assert "可排名:       是" in lines
    assert any("2/2 (100.00%)" in line for line in lines)
    assert any("WINDOW_TRUNCATED" in line for line in lines)
    assert any("execution.frequency=daily" in line for line in lines)
    assert any("其余 1 项" in line for line in lines)


class TestFeePlumbing:
    """费用参数从引擎到账户/撮合层的透传"""

    def test_backtest_engine_min_commission_lot_size(self):
        from diepi.backtest.engine.backtest_engine import BacktestEngine
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240102',
            end_date='20240102',
            freq='daily',
            min_commission=1.0,
            lot_size=200,
            transfer_fee_rate=0.00002,
        )
        engine._init_engine()
        assert engine._context._broker.account.min_commission == 1.0
        assert engine._context._broker.lot_size == 200
        assert (
            engine._context._broker.fee_engine.schedule.transfer_fee_rate
            == 0.00002
        )

    def test_portfolio_engine_min_commission(self):
        from diepi.backtest.engine.portfolio_engine import PortfolioEngine
        engine = PortfolioEngine(
            start_date='20240102',
            end_date='20240102',
            min_commission=0.0,
            lot_size=100,
            transfer_fee_rate=0.00003,
        )
        assert engine.min_commission == 0.0
        engine._init_engine()
        assert (
            engine._context._broker.fee_engine.schedule.transfer_fee_rate
            == 0.00003
        )

    def test_parallel_runner_engine_kwargs_complete(self):
        """ParallelRunner 的 engine_kwargs 必须覆盖全部交易参数"""
        from diepi.backtest.engine.parallel_runner import ParallelRunner
        runner = ParallelRunner(
            symbols=['000001.SZ'],
            start_date='20240102',
            end_date='20240105',
            min_commission=1.0,
            lot_size=100,
            open_buy_resize_mode='legacy',
            liquidity_cap_ratio=0.5,
            limit_pct_overrides={'000001': 0.05},
            transfer_fee_rate=0.00004,
            strategy_price_mode='raw',
            execution_price_mode='raw',
        )
        kwargs = runner._engine_kwargs()
        assert kwargs['min_commission'] == 1.0
        assert kwargs['open_buy_resize_mode'] == 'legacy'
        assert kwargs['liquidity_cap_ratio'] == 0.5
        assert kwargs['limit_pct_overrides'] == {'000001': 0.05}
        assert kwargs['transfer_fee_rate'] == 0.00004
        assert kwargs['strategy_price_mode'] == 'raw'
        assert kwargs['execution_price_mode'] == 'raw'
        # 保证可跨进程序列化
        import pickle
        pickle.dumps(kwargs)

    def test_command_line_transfer_fee_flag_reaches_runner(
            self, monkeypatch):
        captured = {}

        def fake_run_backtest(**kwargs):
            captured.update(kwargs)
            return {'rankable': True}

        monkeypatch.setattr(cli, 'run_backtest', fake_run_backtest)
        monkeypatch.setattr(
            cli.sys,
            'argv',
            [
                'diepi', 'strategy.py', '--quiet',
                '--transfer-fee-rate', '0.00006',
            ],
        )

        with pytest.raises(SystemExit) as stopped:
            cli.main()

        assert stopped.value.code == 0
        assert captured['transfer_fee_rate'] == 0.00006


def test_gui_command_forwards_explicit_data_and_results_roots(
    monkeypatch, tmp_path,
):
    from diepi.backtest.ui import main_window

    captured = {}
    monkeypatch.setattr(
        main_window,
        'run_app',
        lambda **kwargs: captured.update(kwargs) or 0,
    )
    data_root = tmp_path / 'market data'
    results_root = tmp_path / 'shared results'

    exit_code = cli.dispatch((
        'gui',
        '--data-root', str(data_root),
        '--results-root', str(results_root),
    ))

    assert exit_code == 0
    assert captured == {
        'data_root': str(data_root),
        'results_root': str(results_root),
    }
