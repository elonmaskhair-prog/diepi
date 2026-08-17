import os
import shutil
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pandas as pd
import pytest

from diepi.artifacts import (
    ArtifactPayload,
    ArtifactStore,
    EngineKind,
    ErrorCategory,
    RunError,
    RunOutcome,
    RunProvenance,
    SourceFingerprint,
    build_futures_outcome,
)
from diepi.backtest.broker.events import ExecutionEventJournal
from diepi.backtest.broker.replay import CashAuditBundle, CashReplaySeed
from diepi.backtest.broker.target_execution import TargetExecutionBundle
from diepi.backtest.cli.runner import compile_strategy
from diepi.backtest.comparison import (
    ComparisonBundle,
    ComparisonScope,
    ReferenceIndexResult,
    ReferenceIndexSpec,
    TotalReturnIndexSeries,
)
from diepi.backtest.engine.backtest_engine import BacktestResult
from diepi.backtest.engine.parallel_runner import ParallelResult
from diepi.backtest.engine.portfolio_engine import PortfolioResult
from diepi.backtest.data.stock_pool import PoolSource
from diepi.backtest.data.source_evidence import (
    collect_market_data_fingerprints,
    load_verified_display_daily_source,
    verify_display_daily_source,
)
from diepi.backtest.ui.main_window import (
    _prepare_syntax_check_config,
    _request_worker_shutdown,
    _syntax_check_summary,
    _terminal_result_summary,
)
from diepi.backtest.ui.screens.result_screen import (
    _normalize_nav,
    _result_initial_cash,
    _validated_comparison_view,
    format_result_contract,
)
from diepi.backtest.result_contract import (
    ActualInterval,
    DataCoverage,
    ResultAssumption,
    ResultContract,
    ResultReason,
    ResultStatus,
    ResultWarning,
)
from diepi.backtest.strategy import PortfolioStrategy, Strategy
from diepi.backtest.ui.widgets.kline_chart import HAS_PYQTGRAPH, KLineChart
from diepi.backtest.ui.worker import (
    BacktestWorker,
    GUI_TRACEBACK_MAX_BYTES,
    LoadWorker,
    SaveWorker,
    StrategyCheckResult,
    load_gui_run,
    resolve_gui_results_root,
    save_gui_run,
    _independent_rankability_error,
    _gui_config_from_artifact,
    _normalize_custom_strategy_params,
    _resolve_independent_universe,
    _strategy_kind_for_mode,
    _price_mode_engine_kwargs,
)
from diepi.futures.result import FuturesResult


def _artifact_cash_audit(initial_cash=1_000.0):
    seed = CashReplaySeed(
        as_of=datetime(2024, 1, 2),
        account_id='gui-test',
        initial_cash=initial_cash,
        initial_frozen_cash=0.0,
    )
    return CashAuditBundle.from_parts(seed, ExecutionEventJournal())


def _artifact_daily_values(initial_cash=1_000.0):
    frame = pd.DataFrame(
        {
            'cash': [initial_cash],
            'market_value': [0.0],
            'total_value': [initial_cash],
            'daily_return': [0.0],
        },
        index=pd.to_datetime(['2024-01-02']),
    )
    frame.index.name = 'date'
    return frame


def _write_daily_parquet(path, *, symbol='000001.SZ', close=10.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([{
        'ts_code': symbol,
        'trade_date': '20240102',
        'open': close,
        'high': close,
        'low': close,
        'close': close,
        'pre_close': close,
        'amount': 1_000_000.0,
        'vol': 100_000.0,
    }]).to_parquet(path, index=False)


def _artifact_success_contract():
    return ResultContract(
        status=ResultStatus.SUCCESS,
        actual_interval=ActualInterval('2024-01-02', '2024-01-02'),
        data_coverage=DataCoverage(1, 1, 1.0),
    )


def _artifact_portfolio_result():
    return PortfolioResult(
        start_date='20240102', end_date='20240102',
        initial_cash=1_000.0, final_value=1_000.0,
        total_return=0.0, annual_return=0.0, max_drawdown=0.0,
        trade_count=0, win_rate=None, sharpe_ratio=None,
        daily_values=_artifact_daily_values(), trades=[],
        result_contract=_artifact_success_contract(),
        target_execution=TargetExecutionBundle.empty(),
        cash_audit=_artifact_cash_audit(),
    )


def _artifact_single_result(symbol='600000.SH'):
    return BacktestResult(
        symbol=symbol,
        start_date='20240102', end_date='20240102',
        initial_cash=1_000.0, final_value=1_000.0,
        total_return=0.0, annual_return=0.0, max_drawdown=0.0,
        trade_count=0, win_rate=None, sharpe_ratio=None,
        daily_values=_artifact_daily_values(), trades=[],
        result_contract=_artifact_success_contract(),
        target_execution=TargetExecutionBundle.empty(),
        cash_audit=_artifact_cash_audit(),
    )


def _artifact_parallel_result():
    child = _artifact_single_result()
    return ParallelResult(
        start_date='20240102', end_date='20240102',
        initial_cash=1_000.0,
        total_symbols=1, success_count=1, failed_count=0,
        avg_return=0.0, avg_annual_return=0.0,
        avg_max_drawdown=0.0, avg_sharpe=None, avg_win_rate=None,
        results={child.symbol: child}, errors={},
        ranking_scope=('2024-01-02', '2024-01-02', 1, 1),
        ranking_observation_ids=('2024-01-02',),
        requested_symbols=(child.symbol,),
    )


def _artifact_futures_result():
    result = FuturesResult(
        product='IC',
        start_date='20240102',
        end_date='20240102',
        account_size=1_000.0,
        lots=1,
        multiplier=200,
        final_nav=1_010.0,
        total_return=0.01,
        cagr=0.01,
        sharpe=0.5,
        max_drawdown_close=0.02,
        max_drawdown_worst=0.03,
        trade_count=1,
        win_rate=1.0,
        avg_trade_pnl=10.0,
        daily_nav=pd.DataFrame({
            'date': ['20240102'],
            'nav': [1_010.0],
            'nav_worst': [1_000.0],
            'position': ['FLAT'],
            'contract': [''],
        }),
        trades=pd.DataFrame([{
            'entry_date': '20240102',
            'exit_date': '20240102',
            'pnl_yuan': 10.0,
            'commission_yuan': 0.0,
            'direction': 'LONG',
            'contract': 'IC9999.CFX',
            'exit_reason': 'signal_flat',
        }]),
        expected_calendar_len=1,
        result_contract=_artifact_success_contract(),
    )
    assert result.validate()
    return result


def test_syntax_check_preserves_mode_and_only_compiles_without_mutating_config():
    original = {
        'mode': 'independent',
        'pool_source': 'all_market',
        'max_days': 999,
    }

    checked = _prepare_syntax_check_config(original)

    assert checked['mode'] == 'independent'
    assert checked['strategy_kind'] == 'single'
    assert checked['_syntax_only'] is True
    assert original == {
        'mode': 'independent',
        'pool_source': 'all_market',
        'max_days': 999,
    }


def test_syntax_check_summary_discloses_compile_only_contract():
    result = StrategyCheckResult(
        strategy_kind='single', strategy_class_name='DynamicStrategy')

    summary = _syntax_check_summary(result)

    assert '单标的策略' in summary
    assert 'DynamicStrategy' in summary
    assert '未读取行情、未执行回测' in summary


def test_gui_modes_map_to_one_explicit_strategy_contract():
    assert _strategy_kind_for_mode('portfolio') == 'portfolio'
    assert _strategy_kind_for_mode('independent') == 'single'
    with pytest.raises(ValueError, match='不支持的回测模式'):
        _strategy_kind_for_mode('unknown')


def test_independent_all_market_uses_window_union_and_discloses_st_gap():
    class FakePool:
        def __init__(self):
            self.kwargs = None

        def get_pool(self, **kwargs):
            self.kwargs = kwargs
            return ['000001.SZ', '000004.SZ']

    pool = FakePool()
    symbols, assumptions, warnings = _resolve_independent_universe(
        PoolSource.ALL_MARKET,
        None,
        '20180101',
        '20181231',
        stock_pool=pool,
    )

    assert symbols == ['000001.SZ', '000004.SZ']
    assert pool.kwargs == {
        'source': PoolSource.ALL_MARKET,
        'start_date': '20180101',
        'end_date': '20181231',
        'exclude_st': False,
    }
    assert assumptions['universe.membership'] == (
        'listing_interval_window_union'
    )
    assert assumptions['universe.historical_st_filter'] == (
        'disabled_no_point_in_time_source'
    )
    assert len(warnings) == 1

    result = ParallelResult(
        start_date='20180101',
        end_date='20181231',
        initial_cash=100_000.0,
        total_symbols=2,
        success_count=0,
        failed_count=2,
        avg_return=0.0,
        avg_annual_return=0.0,
        avg_max_drawdown=0.0,
        avg_sharpe=None,
        avg_win_rate=None,
        universe_assumptions=assumptions,
        universe_warnings=warnings,
        requested_symbols=tuple(symbols),
    )
    disclosure = result.to_dict()['universe_disclosure']
    assert disclosure['assumptions'] == assumptions
    assert disclosure['warnings'] == warnings
    assert disclosure['symbols'] == symbols
    assert len(disclosure['symbols_sha256']) == 64
    assert result.is_rankable is False
    assert "不可排名" in _independent_rankability_error(result)

    complete = ParallelResult(
        start_date='20180101',
        end_date='20181231',
        initial_cash=100_000.0,
        total_symbols=2,
        success_count=2,
        failed_count=0,
        avg_return=0.0,
        avg_annual_return=0.0,
        avg_max_drawdown=0.0,
        avg_sharpe=None,
        avg_win_rate=None,
        ranking_scope=('2018-01-02', '2018-12-28', 2, 2),
        ranking_observation_ids=('2018-01-02', '2018-12-28'),
    )
    assert complete.is_rankable is True
    assert _independent_rankability_error(complete) is None

    mismatched = ParallelResult(
        start_date='20180101',
        end_date='20181231',
        initial_cash=100_000.0,
        total_symbols=2,
        success_count=2,
        failed_count=0,
        avg_return=0.0,
        avg_annual_return=0.0,
        avg_max_drawdown=0.0,
        avg_sharpe=None,
        avg_win_rate=None,
        ranking_error='INCONSISTENT_CHILD_SCOPE: synthetic mismatch',
    )
    message = _independent_rankability_error(mismatched)
    assert 'INCONSISTENT_CHILD_SCOPE' in message
    assert '成功 2/2' in message


def test_nav_normalization_and_attribution_use_engine_initial_cash():
    result = SimpleNamespace(
        initial_cash=1_000.0,
        final_value=550.0,
        total_return=-0.45,
    )

    base = _result_initial_cash(result)

    assert base == 1_000.0
    assert _normalize_nav([550.0, 1_100.0], base) == [0.55, 1.1]


def _success_contract():
    return ResultContract(
        status=ResultStatus.SUCCESS,
        actual_interval=ActualInterval('2024-01-02', '2024-01-03'),
        data_coverage=DataCoverage(2, 2, 1.0),
        assumptions=(ResultAssumption(
            'data_coverage.unit', 'portfolio_trade_day'),),
    )


def _result_with_validated_comparison():
    scope = ComparisonScope(('20240102', '20240103'))
    spec = ReferenceIndexSpec(
        code='000300.SH', source_id='fixture', source_version='v1')
    series = TotalReturnIndexSeries(
        spec=spec,
        scope=scope,
        base_observation_id='20231229',
        base_level=100.0,
        levels=(110.0, 120.0),
    )
    return PortfolioResult(
        start_date='20240102', end_date='20240103',
        initial_cash=100.0, final_value=130.0,
        total_return=0.30, annual_return=0.0, max_drawdown=0.0,
        trade_count=0, sharpe_ratio=None,
        daily_values=pd.DataFrame(
            {'total_value': [110.0, 130.0]},
            index=pd.to_datetime(['2024-01-02', '2024-01-03']),
        ),
        result_contract=_success_contract(),
        comparisons=ComparisonBundle(
            scope=scope,
            reference_index_total_return=ReferenceIndexResult.from_series(
                series),
        ),
    )


def test_comparison_view_only_accepts_engine_validated_bundle():
    view, error = _validated_comparison_view(
        _result_with_validated_comparison())

    assert error is None
    assert view['code'] == '000300.SH'
    assert view['values'] == pytest.approx((1.10, 1.20))
    assert view['reference_return'] == pytest.approx(0.20)
    assert view['excess_return'] == pytest.approx(0.10)

    unavailable, reason = _validated_comparison_view(SimpleNamespace(
        comparisons=None, reference_total_return_excess=None))
    assert unavailable is None
    assert 'ComparisonBundle' in reason


@pytest.mark.parametrize(
    'kind, code, base',
    [
        ('portfolio', 'def on_day(ctx, bars):\n    pass\n', PortfolioStrategy),
        ('single', 'def on_day(ctx, bar):\n    pass\n', Strategy),
    ],
)
def test_compile_strategy_uses_explicit_contract(kind, code, base):
    strategy_type = compile_strategy(code, strategy_kind=kind)
    assert issubclass(strategy_type, base)


def test_compile_strategy_rejects_wrong_class_for_single_contract():
    code = (
        'from diepi.backtest.strategy import PortfolioStrategy\n'
        'class Wrong(PortfolioStrategy):\n'
        '    pass\n'
    )
    with pytest.raises(ValueError, match="strategy_kind='single'"):
        compile_strategy(code, strategy_kind='single')


def test_worker_syntax_check_uses_mode_contract_without_engine_or_error():
    worker = BacktestWorker(
        'def on_day(ctx, bar):\n    pass\n',
        {'mode': 'independent', 'strategy_kind': 'single', '_syntax_only': True},
    )
    finished = []
    errors = []
    worker.finished.connect(finished.append)
    worker.error.connect(errors.append)

    worker.run()

    assert errors == []
    assert finished == [StrategyCheckResult('single', 'DynamicStrategy')]


def test_worker_routes_non_rankable_engine_terminal_result_to_finished(
    monkeypatch, tmp_path,
):
    captured = {}
    contract = ResultContract(
        status=ResultStatus.CANCELED,
        reason=ResultReason('USER_STOPPED', '用户请求停止'),
    )
    terminal = SimpleNamespace(
        result_contract=contract,
        is_rankable=False,
        error='USER_STOPPED',
        final_value=100_000.0,
        total_return=0.0,
    )

    class FakeEngine:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, _strategy):
            return terminal

    monkeypatch.setattr(
        'diepi.backtest.ui.worker.PortfolioEngine', FakeEngine)
    worker = BacktestWorker('def on_day(ctx, bars):\n    pass\n', {
        'start_date': '20240101', 'end_date': '20240105',
        'initial_cash': 100_000.0, 'freq': 'daily',
        'slippage': 0.0, 'commission': 0.0,
        'data_root': str(tmp_path),
        'price_mode': 'raw',
    })
    finished = []
    errors = []
    worker.finished.connect(finished.append)
    worker.error.connect(errors.append)

    worker._run_portfolio_mode(
        PortfolioStrategy, PoolSource.SPECIFIED, ['000001.SZ'], None)

    assert finished == [terminal]
    assert errors == []
    assert captured['strategy_price_mode'] == 'raw'
    assert captured['execution_price_mode'] == 'raw'


def test_gui_worker_runs_packaged_ma_example_on_raw_minimal_data(tmp_path):
    from diepi.demo import generate_synthetic_demo
    from diepi.examples import get_example

    demo = generate_synthetic_demo(tmp_path / 'gui-raw-minimal')
    shutil.rmtree(demo.data_root / 'parquet' / 'timeseries' / 'daily')
    shutil.rmtree(demo.data_root / 'parquet' / 'timeseries' / 'adj_factor')
    worker = BacktestWorker(get_example('ma-cross').read_source(), {
        'data_root': str(demo.data_root),
        'pool_source': 'specified',
        'symbols': list(demo.manifest.symbols),
        'industry': None,
        'start_date': demo.manifest.start_date,
        'end_date': demo.manifest.end_date,
        'initial_cash': 1_000_000.0,
        'slippage': 0.001,
        'commission': 0.00025,
        'stamp_duty': 0.001,
        'min_commission': 5.0,
        'freq': 'daily',
        'mode': 'portfolio',
        'strategy_kind': 'portfolio',
        'price_mode': 'raw',
        'daily_open_previous_day_ratio': 0.1,
        'daily_close_previous_day_ratio': 0.1,
    })
    finished = []
    errors = []
    worker.finished.connect(finished.append)
    worker.error.connect(errors.append)

    worker.run()

    assert errors == []
    assert len(finished) == 1
    assert finished[0].result_contract.status is ResultStatus.SUCCESS
    assumptions = {
        item.key: item.value
        for item in finished[0].result_contract.assumptions
    }
    assert assumptions['execution.strategy_price_mode'] == 'raw'
    assert assumptions['execution.execution_price_mode'] == 'raw'


def test_gui_all_market_missing_member_is_saved_as_verified_partial(tmp_path):
    from diepi.demo import generate_synthetic_demo

    demo = generate_synthetic_demo(tmp_path / 'gui-all-market-missing')
    basic_path = (
        demo.data_root / 'parquet' / 'metadata' / 'stock' / 'basic.parquet'
    )
    basic = pd.read_parquet(basic_path)
    missing = basic.iloc[[0]].copy()
    missing['ts_code'] = '000002.SZ'
    pd.concat([basic, missing], ignore_index=True).to_parquet(
        basic_path, index=False
    )
    source = demo.strategy_file.read_text(encoding='utf-8')
    worker = BacktestWorker(source, {
        'data_root': str(demo.data_root),
        'pool_source': 'all_market',
        'symbols': None,
        'industry': None,
        'start_date': demo.manifest.start_date,
        'end_date': demo.manifest.end_date,
        'initial_cash': 1_000_000.0,
        'slippage': 0.001,
        'commission': 0.00025,
        'stamp_duty': 0.001,
        'min_commission': 5.0,
        'freq': 'daily',
        'mode': 'portfolio',
        'strategy_kind': 'portfolio',
        'input_mode': 'strategy',
        'price_mode': 'dual',
        'daily_open_cap_yuan': 1_000_000_000.0,
        'daily_close_cap_yuan': 1_000_000_000.0,
    })
    finished = []
    errors = []
    worker.finished.connect(finished.append)
    worker.error.connect(errors.append)

    worker.run()

    assert errors == []
    assert len(finished) == 1
    result = finished[0]
    assert result.result_contract.status is ResultStatus.PARTIAL
    assert result.result_contract.reason.code == (
        'UNIVERSE_MARKET_DATA_INCOMPLETE'
    )
    assert result.is_rankable is False
    assert worker.config['realized_symbols'] == [
        '000001.SZ',
        '000002.SZ',
    ]

    artifact = save_gui_run(
        result,
        worker.config,
        worker.artifact_strategy_source,
        results_root=tmp_path / 'results',
        market_data_fingerprints=worker.market_data_fingerprints,
    )
    loaded = load_gui_run(artifact)
    assert loaded.artifact_verified is True
    assert loaded.is_rankable is False
    assert loaded.result.result_contract.reason.code == (
        'UNIVERSE_MARKET_DATA_INCOMPLETE'
    )


def test_result_contract_disclosure_includes_terminal_evidence():
    contract = ResultContract(
        status=ResultStatus.PARTIAL,
        reason=ResultReason('USER_STOPPED', '用户请求停止'),
        warnings=(ResultWarning('WINDOW_TRUNCATED', '结果区间已截断'),),
        actual_interval=ActualInterval('2024-01-02', '2024-01-03'),
        data_coverage=DataCoverage(3, 2, 2 / 3),
    )

    text = format_result_contract(contract)

    assert '状态: PARTIAL' in text
    assert '可排名: 否' in text
    assert 'USER_STOPPED' in text
    assert '2024-01-02 ~ 2024-01-03' in text
    assert '2/3' in text and '缺失 1' in text
    assert 'WINDOW_TRUNCATED' in text
    result = SimpleNamespace(result_contract=contract)
    assert _terminal_result_summary(result) == 'PARTIAL | 可排名: 否'


def test_editor_mode_switch_preserves_both_strategy_drafts():
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.screens.editor_screen import EditorScreen

    app = QApplication.instance() or QApplication([])
    editor = EditorScreen()
    editor.set_code('# portfolio draft')

    editor.config_panel.mode_independent.setChecked(True)
    assert '单标的策略模板' in editor.get_code()
    editor.set_code('# single draft')

    editor.config_panel.mode_portfolio.setChecked(True)
    assert editor.get_code() == '# portfolio draft'
    editor.config_panel.mode_independent.setChecked(True)
    assert editor.get_code() == '# single draft'
    assert app is not None


def test_history_dialog_exposes_status_and_rankability_columns(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.widgets.history_dialog import HistoryDialog

    app = QApplication.instance() or QApplication([])
    empty_root = resolve_gui_results_root(os.environ.get('DIEPI_RESULTS_DIR'))
    monkeypatch.setattr(
        'diepi.backtest.ui.widgets.history_dialog.discover_history_records',
        lambda _root: [],
    )
    dialog = HistoryDialog(results_root=empty_root)

    headers = [
        dialog.table.horizontalHeaderItem(index).text()
        for index in range(dialog.table.columnCount())
    ]
    assert dialog.table.columnCount() == 8
    assert '格式/验证' in headers
    assert '状态' in headers
    assert '可排名' in headers
    assert '暂无已保存' in dialog.contract_details.text()
    assert app is not None


def test_failed_artifact_shows_bounded_verified_diagnostics_offscreen(
    tmp_path
):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.main_window import MainWindow
    from diepi.backtest.ui.widgets.history_dialog import HistoryDialog

    app = QApplication.instance() or QApplication([])
    contract = ResultContract(
        status=ResultStatus.FAILED,
        reason=ResultReason('ENGINE_CRASH', '引擎执行失败'),
    )
    run_error = RunError(
        code='ENGINE_CRASH',
        category=ErrorCategory.ENGINE,
        phase='engine_run',
        exception_type='RuntimeError',
        message='boom',
        traceback_role='traceback',
    )
    traceback_text = (
        'Traceback (most recent call last):\n'
        + ('x' * GUI_TRACEBACK_MAX_BYTES)
        + '\nRuntimeError: private tail must not be displayed\n'
    )
    artifact = ArtifactStore.save(
        RunOutcome(
            engine_kind=EngineKind.INDEX_FUTURES,
            result_contract=contract,
            result_role=None,
            error=run_error,
        ),
        tmp_path / 'failed-futures',
        config={'command': 'future-engine'},
        traceback_text=traceback_text,
    )

    loaded = load_gui_run(artifact)

    assert loaded.artifact_verified is True
    assert loaded.result is None
    assert loaded.is_rankable is False
    assert loaded.engine_kind == EngineKind.INDEX_FUTURES.value
    assert loaded.run_error == run_error
    assert loaded.traceback_truncated is True
    assert len(loaded.traceback_text.encode('utf-8')) <= (
        GUI_TRACEBACK_MAX_BYTES
    )
    assert loaded.traceback_text.startswith('Traceback')
    assert 'private tail' not in loaded.traceback_text

    history = HistoryDialog(results_root=tmp_path)
    assert history.table.item(0, 2).text() == 'FAILED'
    assert history.table.item(0, 3).text() == '否'
    assert 'ENGINE_CRASH' in history.contract_details.text()
    assert 'index_futures' in history.contract_details.text()
    assert history.traceback_view.isReadOnly() is True
    assert history.traceback_view.toPlainText() == loaded.traceback_text
    assert '最多显示 262144' in history.traceback_label.text()
    assert '已验证前缀' in history.traceback_label.text()

    window = MainWindow(results_root=tmp_path, data_root=tmp_path)
    window._on_load_result_finished(loaded)
    dialog = window.failure_dialog
    assert dialog is not None
    assert dialog.isModal() is True
    assert dialog.error_summary.isReadOnly() is True
    assert 'ENGINE_CRASH' in dialog.error_summary.toPlainText()
    assert '可排名: 否' in dialog.error_summary.toPlainText()
    assert dialog.traceback_view.isReadOnly() is True
    assert dialog.traceback_view.toPlainText() == loaded.traceback_text
    assert window.result_screen.get_current_result() is None
    assert 'FAILED' in window.statusBar().currentMessage()

    dialog.copy_traceback_btn.click()
    assert QApplication.clipboard().text() == loaded.traceback_text
    dialog.close()
    history.close()
    window.close()
    assert app is not None


def test_gui_portfolio_artifact_round_trip_uses_explicit_root(tmp_path):
    root = tmp_path / 'explicit gui results'
    result = _artifact_portfolio_result()

    artifact = save_gui_run(
        result,
        {
            'mode': 'portfolio',
            'nested': {'value': 1},
            'data_root': str(tmp_path / 'private machine data'),
        },
        'def on_day(ctx, bars):\n    pass\n',
        results_root=root,
    )
    loaded = load_gui_run(artifact)

    assert artifact.parent == root.resolve()
    assert (artifact / 'manifest.json').is_file()
    assert loaded.artifact_format == 'RunArtifact v1'
    assert loaded.artifact_verified is True
    assert loaded.is_rankable is True
    assert type(loaded.result) is PortfolioResult
    assert loaded.config == {
        'mode': 'portfolio',
        'nested': {'value': 1},
        'data_source': 'external_local_data',
        'price_mode': 'dual',
    }
    assert str(tmp_path) not in (artifact / 'config.json').read_text(
        encoding='utf-8')
    assert loaded.strategy_source.startswith('def on_day')
    assert ArtifactStore.verify(artifact).run_id == artifact.name


def test_gui_combo_artifact_snapshots_inputs_and_reloads_portably(
    tmp_path, monkeypatch,
):
    from diepi.backtest.cli.combo_bundle import load_combo_bundle

    bundle = tmp_path / 'private combo'
    bundle.mkdir()
    pd.DataFrame([
        {
            'trade_date': '20240102',
            'symbol': '000001.SZ',
            'target_weight': 1.0,
        }
    ]).to_csv(bundle / 'targets.csv', index=False)
    pd.DataFrame([
        {
            'trade_date': '20240102',
            'symbol': '000001.SZ',
            'exit_price': 'close',
        }
    ]).to_csv(bundle / 'close_sells.csv', index=False)
    pd.DataFrame([
        {'date': '20240102', 'invested_weight': 1.0, 'cash_weight': 0.0}
    ]).to_csv(bundle / 'daily.csv', index=False)
    runtime_bundle = load_combo_bundle(bundle, tag='private-combo')
    runtime_inputs = runtime_bundle.artifact_inputs()
    executed_source = (
        Path(__file__).resolve().parents[2]
        / 'diepi' / 'backtest' / 'cli' / 'combo_replay_template.py'
    ).read_text(encoding='utf-8')

    # A later valid replacement must not change what the result artifact
    # records as the input generation used by the engine.
    pd.DataFrame([{
        'trade_date': '20240102',
        'symbol': '000001.SZ',
        'target_weight': 0.5,
    }]).to_csv(bundle / 'targets.csv', index=False)

    artifact = save_gui_run(
        _artifact_portfolio_result(),
        {
            'mode': 'portfolio',
            'start_date': '20240102',
            'end_date': '20240102',
            'symbols': ['000001.SZ'],
            'combo_bundle': str(bundle),
            'combo_tag': 'private-combo',
        },
        executed_source,
        results_root=tmp_path / 'gui combo results',
        combo_artifact_inputs=runtime_inputs,
    )
    loaded = load_gui_run(artifact)

    assert loaded.artifact_verified is True
    assert loaded.config['combo_bundle'] == str(
        (artifact / 'inputs' / 'combo').resolve()
    )
    assert str(bundle) not in (artifact / 'config.json').read_text(
        encoding='utf-8'
    )
    assert loaded.strategy_source == executed_source
    assert ArtifactStore.load(artifact).read_bytes('combo_targets_source') == (
        runtime_inputs[1][2]
    )
    assert ArtifactStore.load(artifact).read_bytes(
        'combo_targets_source'
    ) != (bundle / 'targets.csv').read_bytes()

    (artifact / 'inputs' / 'combo' / 'targets.csv').write_bytes(
        b'trade_date,symbol,target_weight\n20240102,000001.SZ,0.25\n'
    )
    loaded.config['data_root'] = str(tmp_path)
    worker = BacktestWorker(loaded.strategy_source, loaded.config)
    captured = {}

    def fake_compile(_code, *, strategy_kind, strategy_params):
        captured['kind'] = strategy_kind
        captured['bundle'] = strategy_params['COMBO_BUNDLE']
        return PortfolioStrategy

    monkeypatch.setattr(worker, '_compile_strategy', fake_compile)
    monkeypatch.setattr(
        worker, '_run_portfolio_mode', lambda *_args, **_kwargs: None
    )
    errors = []
    worker.error.connect(errors.append)
    worker.run()

    assert errors == []
    assert captured['kind'] == 'portfolio'
    assert captured['bundle'].targets_bytes == runtime_inputs[1][2]
    assert worker.combo_artifact_inputs == runtime_inputs


def test_gui_parallel_artifact_is_saved_and_loaded_as_v1(tmp_path):
    result = _artifact_parallel_result()

    artifact = save_gui_run(
        result,
        {'mode': 'independent', 'strategy_kind': 'single'},
        'def on_day(ctx, bar):\n    pass\n',
        results_root=tmp_path / 'parallel results',
    )
    loaded = load_gui_run(artifact)

    assert loaded.artifact_verified is True
    assert loaded.is_rankable is True
    assert type(loaded.result) is ParallelResult
    assert loaded.result.requested_symbols == ('600000.SH',)
    assert loaded.config['mode'] == 'independent'


def test_gui_save_and_load_workers_use_run_artifact_v1(tmp_path):
    result = _artifact_portfolio_result()
    market_source = SourceFingerprint.from_bytes(
        kind='market_data_file',
        logical_path='parquet/timeseries/daily_raw/000001.SZ.parquet',
        payload=b'run-generation',
    )
    saved = []
    save_errors = []
    saver = SaveWorker(
        result,
        {'mode': 'portfolio'},
        'pass\n',
        results_root=tmp_path / 'worker results',
        market_data_fingerprints=(market_source,),
    )
    saver.finished.connect(saved.append)
    saver.error.connect(save_errors.append)
    saver.run()

    assert save_errors == []
    assert len(saved) == 1
    assert os.path.isfile(os.path.join(saved[0], 'manifest.json'))

    loaded = []
    load_errors = []
    loader = LoadWorker(saved[0])
    loader.finished.connect(loaded.append)
    loader.error.connect(load_errors.append)
    loader.run()

    assert load_errors == []
    assert len(loaded) == 1
    assert loaded[0].artifact_verified is True
    assert loaded[0].strategy_source == 'pass\n'
    assert loaded[0].provenance.sources == (market_source,)


def test_history_lists_verified_v1_and_marks_legacy_unverified(
    tmp_path, monkeypatch,
):
    from diepi.backtest.data.result_storage import ResultStorage
    from diepi.backtest.data.report_generator import ReportGenerator
    from diepi.backtest.ui.widgets.history_dialog import (
        discover_history_records,
    )

    root = tmp_path / 'mixed results'
    v1 = save_gui_run(
        _artifact_portfolio_result(),
        {'mode': 'portfolio'},
        'pass\n',
        results_root=root,
    )
    monkeypatch.setattr(ResultStorage, 'RESULTS_DIR', str(root))
    monkeypatch.setattr(
        ReportGenerator, 'generate', staticmethod(lambda *args, **kwargs: None))
    legacy_result = _artifact_portfolio_result()
    legacy_result.position_history = []
    legacy = ResultStorage.save(
        legacy_result, {'mode': 'portfolio'}, 'pass\n')

    before = sorted(path.name for path in root.iterdir())
    records = discover_history_records(root)
    after = sorted(path.name for path in root.iterdir())

    by_path = {record['path']: record for record in records}
    assert by_path[str(v1)]['verified'] is True
    assert by_path[str(v1)]['rankable'] is True
    assert by_path[str(legacy)]['verified'] is False
    assert by_path[str(legacy)]['rankable'] is False
    assert by_path[str(legacy)]['result_status'] == 'LEGACY_UNVERIFIED'
    assert before == after  # discovery never auto-upgrades legacy


def test_main_window_opens_history_with_its_runtime_results_root(
    tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    captured = {}

    class FakeSignal:
        def connect(self, _callback):
            pass

    class FakeHistoryDialog:
        def __init__(self, parent, *, results_root):
            captured['parent'] = parent
            captured['results_root'] = results_root
            self.view_result = FakeSignal()
            self.view_code = FakeSignal()

        def exec(self):
            captured['executed'] = True

    monkeypatch.setattr(
        'diepi.backtest.ui.main_window.HistoryDialog', FakeHistoryDialog)
    data_root = tmp_path / 'market root'
    window = MainWindow(
        results_root=tmp_path / 'gui root', data_root=data_root
    )

    window._on_view_history()

    assert captured['parent'] is window
    assert captured['results_root'] == (tmp_path / 'gui root').resolve()
    assert window.editor_screen.get_config()['data_root'] == str(
        data_root.resolve()
    )
    assert captured['executed'] is True
    assert app is not None


def test_main_window_views_artifact_when_exact_gui_rerun_is_unavailable(
    tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QApplication
    import diepi.backtest.ui.main_window as main_window_module

    app = QApplication.instance() or QApplication([])
    result = _artifact_portfolio_result()
    artifact = ArtifactStore.save(
        RunOutcome.from_result(
            result,
            engine_kind=EngineKind.CASH_PORTFOLIO,
        ),
        tmp_path / 'cli-nonrepresentable-rate',
        config={
            'command': 'run',
            'input_mode': 'strategy',
            'requested_start_date': '20240102',
            'requested_end_date': '20240102',
            'parameters': {
                'initial_cash': 1_000.0,
                'slippage': 0.001,
                'commission': 0.00025,
                'stamp_duty': 'auto',
                'transfer_fee_rate': 1.3e-6,
                'min_commission': 5.0,
                'freq': 'daily',
                'price_mode': 'dual',
                'pool_symbols': ['600000.SH'],
                'strategy_params': {},
            },
        },
        strategy_source='def on_day(ctx, bars):\n    pass\n',
    )
    loaded = load_gui_run(artifact)
    warnings = []
    criticals = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        'warning',
        lambda _parent, title, message: warnings.append((title, message)),
    )
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        'critical',
        lambda _parent, title, message: criticals.append((title, message)),
    )
    window = main_window_module.MainWindow(
        results_root=tmp_path / 'results', data_root=tmp_path
    )

    window._on_load_result_finished(loaded)

    assert window.stack.currentWidget() is window.result_screen
    assert window.result_screen.get_current_result() is not None
    assert window.result_screen.get_current_result().final_value == (
        result.final_value
    )
    assert 'transfer_fee_rate' in window._artifact_rerun_block_reason
    assert window.editor_screen.run_btn.isEnabled() is False
    assert window.editor_screen.check_btn.isEnabled() is False
    assert warnings and '结果已载入' in warnings[0][1]
    assert criticals == []

    window._on_back_to_editor()
    assert window.stack.currentWidget() is window.editor_screen
    assert window.editor_screen.run_btn.isEnabled() is False
    assert '无法等价恢复' in window.editor_screen.run_btn.toolTip()

    code_window = main_window_module.MainWindow(
        results_root=tmp_path / 'code-results', data_root=tmp_path
    )
    code_window.editor_screen.set_code('original editor code\n')
    original_config = code_window.editor_screen.get_config()
    code_window._on_load_code_finished(loaded)

    assert code_window.editor_screen.get_code() == 'original editor code\n'
    assert code_window.editor_screen.get_config()['transfer_fee_rate'] == (
        original_config['transfer_fee_rate']
    )
    assert code_window.editor_screen.run_btn.isEnabled() is False
    assert 'transfer_fee_rate' in code_window._artifact_rerun_block_reason
    assert len(warnings) == 2
    assert criticals == []
    assert app is not None


def test_unknown_cli_execution_parameter_is_view_only_not_ignored(
    tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QApplication
    import diepi.backtest.ui.main_window as main_window_module

    app = QApplication.instance() or QApplication([])
    result = _artifact_portfolio_result()
    artifact = ArtifactStore.save(
        RunOutcome.from_result(
            result,
            engine_kind=EngineKind.CASH_PORTFOLIO,
        ),
        tmp_path / 'future-cli-parameter',
        config={
            'command': 'run',
            'input_mode': 'strategy',
            'requested_start_date': '20240102',
            'requested_end_date': '20240102',
            'parameters': {
                'initial_cash': 1_000.0,
                'freq': 'daily',
                'price_mode': 'dual',
                'pool_symbols': ['600000.SH'],
                'strategy_params': {},
                'future_execution_model': 'v2',
            },
        },
        strategy_source='def on_day(ctx, bars):\n    pass\n',
    )

    loaded = load_gui_run(artifact)

    assert loaded.result.final_value == result.final_value
    assert 'future_execution_model' in loaded.rerun_block_reason
    assert loaded.config['parameters']['future_execution_model'] == 'v2'
    warnings = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        'warning',
        lambda _parent, title, message: warnings.append((title, message)),
    )
    window = main_window_module.MainWindow(
        results_root=tmp_path / 'results', data_root=tmp_path
    )
    window._on_load_result_finished(loaded)

    assert window.stack.currentWidget() is window.result_screen
    assert window.result_screen.get_current_result().final_value == result.final_value
    assert window.editor_screen.run_btn.isEnabled() is False
    assert 'future_execution_model' in window._artifact_rerun_block_reason
    assert warnings and '只读查看' in warnings[0][1]
    assert app is not None


def test_futures_v1_history_uses_read_only_summary_instead_of_cash_view(
    tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QApplication
    import diepi.backtest.ui.main_window as main_window_module

    app = QApplication.instance() or QApplication([])
    result = _artifact_futures_result()
    artifact = ArtifactStore.save(
        build_futures_outcome(result),
        tmp_path / 'futures-history',
        config={'command': 'futures', 'product': 'IC'},
    )
    loaded = load_gui_run(artifact)

    assert type(loaded.result) is FuturesResult
    assert 'read-only experimental summary' in loaded.rerun_block_reason
    warnings = []
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        'warning',
        lambda _parent, title, message: warnings.append((title, message)),
    )
    window = main_window_module.MainWindow(
        results_root=tmp_path / 'results', data_root=tmp_path
    )
    window._on_load_result_finished(loaded)

    assert window.stack.currentWidget() is window.result_screen
    assert type(window.result_screen.get_current_result()) is FuturesResult
    assert '股指期货日线近似研究' in window.result_screen.contract_summary.text()
    assert window.result_screen.stat_labels['total_return'].text() == '1.00%'
    assert window.result_screen.stat_labels['final_value'].text() == '1,010'
    assert '现金订单事件' in window.result_screen.events_status_label.text()
    assert window.editor_screen.run_btn.isEnabled() is False
    assert warnings and '只读查看' in warnings[0][1]
    assert app is not None


def test_main_window_save_binds_evidence_to_displayed_run(
    tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QApplication
    import diepi.backtest.ui.main_window as main_window_module

    app = QApplication.instance() or QApplication([])
    captured = {}

    class FakeSignal:
        def connect(self, callback):
            self.callback = callback

    class FakeSaveWorker:
        def __init__(self, result, config, code, **kwargs):
            captured.update(
                result=result, config=config, code=code, kwargs=kwargs
            )
            self.finished = FakeSignal()
            self.error = FakeSignal()

        def start(self):
            captured['started'] = True

        def isRunning(self):
            return False

    monkeypatch.setattr(main_window_module, 'SaveWorker', FakeSaveWorker)
    window = main_window_module.MainWindow(
        results_root=tmp_path / 'results', data_root=tmp_path
    )
    result = _artifact_portfolio_result()
    source = SourceFingerprint.from_bytes(
        kind='market_data_file',
        logical_path='parquet/timeseries/daily_raw/000001.SZ.parquet',
        payload=b'used-by-this-result',
    )
    window.result_screen._current_result = result
    window._current_config = {'mode': 'portfolio', 'price_mode': 'raw'}
    window._current_code = 'editor draft'
    window._current_artifact_strategy_source = 'exact executed source'
    window._current_market_data_fingerprints = (source,)
    window._current_signals_artifact_inputs = ()
    window._current_combo_artifact_inputs = ()

    window._on_save_result()

    assert captured['result'] is result
    assert captured['code'] == 'exact executed source'
    assert captured['kwargs']['market_data_fingerprints'] == (source,)
    assert captured['kwargs']['signals_artifact_inputs'] == ()
    assert captured['kwargs']['combo_artifact_inputs'] == ()
    assert captured['started'] is True
    assert app is not None


def test_config_panel_data_root_round_trip(tmp_path):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.widgets.config_panel import ConfigPanel

    app = QApplication.instance() or QApplication([])
    first = tmp_path / 'dataset one'
    second = tmp_path / 'dataset two'
    first.mkdir()
    second.mkdir()
    panel = ConfigPanel()

    panel.set_config({'data_root': str(first), 'mode': 'portfolio'})
    assert panel.get_config()['data_root'] == str(first)
    panel.set_config({'data_root': str(second), 'mode': 'independent'})
    config = panel.get_config()

    assert config['data_root'] == str(second)
    assert config['mode'] == 'independent'
    assert config['strategy_kind'] == 'single'
    assert app is not None


def test_config_panel_price_mode_and_auction_policy_round_trip(tmp_path):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.widgets.config_panel import ConfigPanel

    app = QApplication.instance() or QApplication([])
    panel = ConfigPanel(data_root=tmp_path)
    panel.set_config({
        'price_mode': 'raw',
        'daily_open_cap_yuan': 2_000_000.0,
        'daily_open_previous_day_ratio': None,
        'daily_close_cap_yuan': None,
        'daily_close_previous_day_ratio': 0.25,
    })

    config = panel.get_config()
    assert config['price_mode'] == 'raw'
    assert config['daily_open_cap_yuan'] == 2_000_000.0
    assert config['daily_open_previous_day_ratio'] is None
    assert config['daily_close_cap_yuan'] is None
    assert config['daily_close_previous_day_ratio'] == pytest.approx(0.25)
    assert app is not None


def test_gui_price_mode_maps_to_explicit_engine_lanes():
    assert _price_mode_engine_kwargs('dual') == {
        'strategy_price_mode': 'hfq',
        'execution_price_mode': 'raw',
    }
    assert _price_mode_engine_kwargs('raw') == {
        'strategy_price_mode': 'raw',
        'execution_price_mode': 'raw',
    }
    assert _price_mode_engine_kwargs('hfq') == {
        'strategy_price_mode': 'hfq',
        'execution_price_mode': 'hfq',
    }
    with pytest.raises(ValueError, match='dual/hfq/raw'):
        _price_mode_engine_kwargs('guess')


def test_gui_worker_fails_closed_when_market_generation_changes(
    tmp_path, monkeypatch,
):
    before = (SourceFingerprint.from_bytes(
        kind='market_data_file',
        logical_path='parquet/timeseries/daily_raw/000001.SZ.parquet',
        payload=b'before',
    ),)
    after = (SourceFingerprint.from_bytes(
        kind='market_data_file',
        logical_path='parquet/timeseries/daily_raw/000001.SZ.parquet',
        payload=b'after',
    ),)
    result = SimpleNamespace(
        result_contract=ResultContract(
            status=ResultStatus.INVALID,
            reason=ResultReason('NO_DATA', 'test'),
        ),
        is_rankable=False,
        error='',
        final_value=1_000.0,
        total_return=0.0,
    )

    class FakeEngine:
        def __init__(self, **_kwargs):
            pass

        def run(self, _strategy):
            return result

    worker = BacktestWorker('', {
        'start_date': '20240101',
        'end_date': '20240102',
        'initial_cash': 1_000.0,
        'freq': 'daily',
        'pool_source': 'specified',
        'symbols': ['000001.SZ'],
        'slippage': 0.0,
        'commission': 0.0,
        'data_root': str(tmp_path),
        'price_mode': 'raw',
    })
    captures = iter((before, after))
    monkeypatch.setattr(
        worker, '_capture_market_data_fingerprints', lambda _symbols: next(captures)
    )
    monkeypatch.setattr('diepi.backtest.ui.worker.PortfolioEngine', FakeEngine)

    with pytest.raises(OSError, match='MARKET_DATA_CHANGED_DURING_RUN'):
        worker._run_portfolio_mode(
            PortfolioStrategy, PoolSource.SPECIFIED, ['000001.SZ'], None
        )

    assert worker.market_data_fingerprints == ()


def test_gui_combo_worker_keeps_exact_runtime_inputs_and_source(
    tmp_path, monkeypatch,
):
    bundle = tmp_path / 'combo'
    bundle.mkdir()
    pd.DataFrame([{
        'trade_date': '20240102',
        'symbol': '000001.SZ',
        'target_weight': 1.0,
    }]).to_csv(bundle / 'targets.csv', index=False)
    pd.DataFrame([{
        'trade_date': '20240102',
        'symbol': '000001.SZ',
        'exit_price': 'close',
    }]).to_csv(bundle / 'close_sells.csv', index=False)
    pd.DataFrame([{
        'date': '20240102',
        'invested_weight': 1.0,
        'cash_weight': 0.0,
    }]).to_csv(bundle / 'daily.csv', index=False)
    worker = BacktestWorker('editor draft', {
        'mode': 'portfolio',
        'strategy_kind': 'portfolio',
        'combo_bundle': str(bundle),
        'combo_tag': 'combo-v1',
        'start_date': '20240102',
        'end_date': '20240102',
        'pool_source': 'specified',
        'symbols': ['000001.SZ'],
    })
    monkeypatch.setattr(
        worker,
        '_compile_strategy',
        lambda *_args, **_kwargs: PortfolioStrategy,
    )
    monkeypatch.setattr(
        worker, '_run_portfolio_mode', lambda *_args, **_kwargs: None
    )

    worker.run()

    assert len(worker.combo_artifact_inputs) == 4
    assert 'def on_after_open' in worker.artifact_strategy_source
    assert worker.artifact_strategy_source != 'editor draft'


def test_market_file_fingerprint_verifies_and_rejects_changed_local_data(
    tmp_path,
):
    market = (
        tmp_path / 'parquet' / 'timeseries' / 'daily_raw'
        / '000001.SZ.parquet'
    )
    _write_daily_parquet(market, close=10.0)
    sources = collect_market_data_fingerprints(
        tmp_path, symbols=['000001.SZ'], price_mode='raw'
    )
    provenance = RunProvenance.build(sources=sources)

    matched = verify_display_daily_source(
        provenance,
        data_root=tmp_path,
        symbol='000001.SZ',
        price_mode='raw',
        scope_symbols=['000001.SZ'],
    )
    assert matched.verified is True
    assert matched.logical_path == (
        'parquet/timeseries/daily_raw/000001.SZ.parquet'
    )
    assert str(tmp_path) not in matched.logical_path

    _write_daily_parquet(market, close=11.0)
    changed = verify_display_daily_source(
        provenance,
        data_root=tmp_path,
        symbol='000001.SZ',
        price_mode='raw',
        scope_symbols=['000001.SZ'],
    )
    assert changed.verified is False
    assert 'SHA-256' in changed.message


def test_empty_etf_direct_file_never_claims_section_fallback_as_verified(
    tmp_path,
):
    symbol = '510300.SH'
    direct = (
        tmp_path / 'parquet' / 'timeseries' / 'etf_daily_raw'
        / f'{symbol}.parquet'
    )
    direct.parent.mkdir(parents=True)
    pd.DataFrame(columns=[
        'ts_code', 'trade_date', 'open', 'high', 'low', 'close',
        'pre_close', 'change', 'pct_chg', 'vol', 'amount',
    ]).to_parquet(direct, index=False)
    section = (
        tmp_path / 'parquet' / 'section' / 'etf_daily_raw'
        / '20240102.parquet'
    )
    section.parent.mkdir(parents=True)
    pd.DataFrame([{
        'ts_code': symbol,
        'trade_date': '20240102',
        'open': 3.5,
        'high': 3.6,
        'low': 3.4,
        'close': 3.55,
        'pre_close': 3.5,
        'change': 0.05,
        'pct_chg': 1.4,
        'vol': 1000.0,
        'amount': 3500.0,
    }]).to_parquet(section, index=False)

    sources = collect_market_data_fingerprints(
        tmp_path, symbols=[symbol], price_mode='raw'
    )
    verification = verify_display_daily_source(
        RunProvenance.build(sources=sources),
        data_root=tmp_path,
        symbol=symbol,
        price_mode='raw',
        scope_symbols=[symbol],
    )

    assert sources == ()
    assert verification.verified is False


def test_direct_parquet_directory_and_alternate_file_route_identically(
    tmp_path,
):
    from diepi.backtest.data.cache_manager import CacheConfig, ParquetReader

    root = tmp_path / 'parquet' / 'timeseries'
    canonical = root / 'daily_raw' / '000001.SZ.parquet'
    canonical.mkdir(parents=True)
    _write_daily_parquet(canonical / 'part.parquet', close=10.0)
    alternate = root / 'daily_raw' / '000001_SZ.parquet'
    _write_daily_parquet(alternate, close=20.0)
    reader = ParquetReader(CacheConfig(
        PARQUET_ROOT=root,
        METADATA_ROOT=tmp_path / 'parquet' / 'metadata',
        ONECSV_DIR=tmp_path / 'onecsv',
    ))

    engine_frame = reader.read('daily_data_raw', '000001.SZ')
    sources = collect_market_data_fingerprints(
        tmp_path, symbols=['000001.SZ'], price_mode='raw'
    )
    provenance = RunProvenance.build(sources=sources)
    display_frame, verification = load_verified_display_daily_source(
        provenance,
        data_root=tmp_path,
        symbol='000001.SZ',
        price_mode='raw',
        scope_symbols=['000001.SZ'],
    )

    assert engine_frame['close'].tolist() == [20.0]
    assert display_frame['close'].tolist() == [20.0]
    assert verification.logical_path.endswith(
        'daily_raw/000001_SZ.parquet'
    )


@pytest.mark.parametrize('unsupported_kind', ['symlink', 'reparse'])
def test_unsupported_direct_link_and_alternate_file_route_identically(
    tmp_path, monkeypatch, unsupported_kind,
):
    from diepi.backtest.data.cache_manager import CacheConfig, ParquetReader

    root = tmp_path / 'parquet' / 'timeseries'
    canonical = root / 'daily_raw' / '000001.SZ.parquet'
    _write_daily_parquet(canonical, close=10.0)
    alternate = root / 'daily_raw' / '000001_SZ.parquet'
    _write_daily_parquet(alternate, close=20.0)
    if unsupported_kind == 'symlink':
        real_is_symlink = Path.is_symlink

        def simulated_is_symlink(path):
            if path == canonical:
                return True
            return real_is_symlink(path)

        monkeypatch.setattr(Path, 'is_symlink', simulated_is_symlink)
    else:
        real_lstat = Path.lstat

        def simulated_reparse_lstat(path):
            value = real_lstat(path)
            if path == canonical:
                return SimpleNamespace(
                    st_mode=value.st_mode,
                    st_file_attributes=0x0400,
                )
            return value

        monkeypatch.setattr(Path, 'lstat', simulated_reparse_lstat)
    reader = ParquetReader(CacheConfig(
        PARQUET_ROOT=root,
        METADATA_ROOT=tmp_path / 'parquet' / 'metadata',
        ONECSV_DIR=tmp_path / 'onecsv',
    ))

    engine_frame = reader.read('daily_data_raw', '000001.SZ')
    sources = collect_market_data_fingerprints(
        tmp_path, symbols=['000001.SZ'], price_mode='raw'
    )
    provenance = RunProvenance.build(sources=sources)
    display_frame, verification = load_verified_display_daily_source(
        provenance,
        data_root=tmp_path,
        symbol='000001.SZ',
        price_mode='raw',
        scope_symbols=['000001.SZ'],
    )

    assert engine_frame['close'].tolist() == [20.0]
    assert display_frame['close'].tolist() == [20.0]
    assert verification.logical_path.endswith(
        'daily_raw/000001_SZ.parquet'
    )


def test_verified_daily_loader_rejects_replacement_between_verify_and_read(
    tmp_path, monkeypatch,
):
    import diepi.backtest.data.source_evidence as evidence_module

    market = (
        tmp_path / 'parquet' / 'timeseries' / 'daily_raw'
        / '000001.SZ.parquet'
    )
    _write_daily_parquet(market, close=10.0)
    provenance = RunProvenance.build(sources=collect_market_data_fingerprints(
        tmp_path, symbols=['000001.SZ'], price_mode='raw'
    ))
    real_verify = evidence_module.verify_display_daily_source
    calls = 0

    def replace_after_first_verification(*args, **kwargs):
        nonlocal calls
        result = real_verify(*args, **kwargs)
        calls += 1
        if calls == 1 and result.verified:
            _write_daily_parquet(market, close=11.0)
        return result

    monkeypatch.setattr(
        evidence_module,
        'verify_display_daily_source',
        replace_after_first_verification,
    )

    with pytest.raises(OSError, match='changed before it was read'):
        load_verified_display_daily_source(
            provenance,
            data_root=tmp_path,
            symbol='000001.SZ',
            price_mode='raw',
            scope_symbols=['000001.SZ'],
        )


def test_minute_run_binds_companion_daily_and_requested_year_partition(tmp_path):
    market = (
        tmp_path / 'parquet' / 'timeseries' / 'daily_raw'
        / '000001.SZ.parquet'
    )
    _write_daily_parquet(market)
    minute = (
        tmp_path / 'parquet' / 'timeseries' / 'minute_raw'
        / '000001.SZ' / '2024.parquet'
    )
    minute.parent.mkdir(parents=True)
    pd.DataFrame([{
        'trade_time': pd.Timestamp('2024-01-02 09:31:00'),
        'open': 10.0,
        'high': 10.1,
        'low': 9.9,
        'close': 10.0,
        'pre_close': 9.9,
        'amount': 100000.0,
    }]).to_parquet(minute, index=False)
    outside = minute.with_name('2023.parquet')
    pd.read_parquet(minute).assign(
        trade_time=pd.Timestamp('2023-01-03 09:31:00')
    ).to_parquet(outside, index=False)

    sources = collect_market_data_fingerprints(
        tmp_path,
        symbols=['000001.SZ'],
        price_mode='raw',
        frequency='minute',
        start_date='20240102',
        end_date='20240102',
    )

    assert {source.logical_path for source in sources} == {
        'parquet/timeseries/daily_raw/000001.SZ.parquet',
        'parquet/timeseries/minute_raw/000001.SZ/2024.parquet',
    }


def test_config_panel_combo_bundle_round_trip(tmp_path):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.widgets.config_panel import ConfigPanel

    app = QApplication.instance() or QApplication([])
    bundle = tmp_path / 'combo bundle'
    bundle.mkdir()
    panel = ConfigPanel()
    panel.set_config({
        'data_root': str(tmp_path),
        'combo_bundle': str(bundle),
        'combo_tag': 'frozen-v1',
        'mode': 'portfolio',
    })

    config = panel.get_config()
    assert config['combo_bundle'] == str(bundle)
    assert config['combo_tag'] == 'frozen-v1'
    assert config['mode'] == 'portfolio'
    assert app is not None


def test_config_panel_three_inputs_and_mixed_fee_controls_round_trip(tmp_path):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.widgets.config_panel import ConfigPanel

    app = QApplication.instance() or QApplication([])
    signals = tmp_path / 'signals.csv'
    combo = tmp_path / 'combo'
    combo.mkdir()
    panel = ConfigPanel(data_root=tmp_path)

    panel.set_config({
        'mode': 'portfolio',
        'input_mode': 'signals',
        'signals_file': str(signals),
        'signals_format': 'action',
        'combo_bundle': str(combo),  # inactive stale value must not leak
        'stamp_duty': 'auto',
        'transfer_fee_rate': 0.00001,
        'lot_size': 200,
        'liquidity_cap_ratio': 0.625,
        'open_buy_resize_mode': 'legacy',
        'open_buy_fill_mode': 'open',
        'open_buy_sizing': 'fill',
        'limit_pct_overrides': {'688001': 0.2, '159781.SZ': 0.1},
        't0_overrides': ['511', '159001.SZ'],
        'trading_days_per_year': 244,
        'risk_free_rate': 0.0175,
    })
    restored = panel.get_config()

    assert restored['input_mode'] == 'signals'
    assert restored['signals_file'] == str(signals)
    assert restored['signals_format'] == 'action'
    assert restored['combo_bundle'] is None
    assert restored['stamp_duty'] == 'auto'
    assert restored['transfer_fee_rate'] == pytest.approx(0.00001)
    assert restored['lot_size'] == 200
    assert restored['liquidity_cap_ratio'] == pytest.approx(0.625)
    assert restored['open_buy_resize_mode'] == 'legacy'
    assert restored['open_buy_fill_mode'] == 'open'
    assert restored['open_buy_sizing'] == 'fill'
    assert restored['limit_pct_overrides'] == {
        '159781.SZ': 0.1,
        '688001': 0.2,
    }
    assert restored['t0_overrides'] == ['511', '159001.SZ']
    assert restored['trading_days_per_year'] == 244
    assert restored['risk_free_rate'] == pytest.approx(0.0175)

    panel.set_config({
        'mode': 'portfolio',
        'input_mode': 'combo',
        'combo_bundle': str(combo),
        'stamp_duty': 0.0005,
        'transfer_fee_rate': 0.0,
    })
    restored = panel.get_config()
    assert restored['input_mode'] == 'combo'
    assert restored['signals_file'] is None
    assert restored['combo_bundle'] == str(combo)
    assert restored['stamp_duty'] == pytest.approx(0.0005)

    # Independent-per-symbol has only the source-code execution contract.
    panel.set_config({'mode': 'independent', 'input_mode': 'signals'})
    assert panel.get_config()['input_mode'] == 'strategy'
    assert app is not None


def test_gui_signals_worker_saves_exact_runtime_bytes_and_reloads(
    tmp_path, monkeypatch,
):
    original = (
        b'date,symbol,target_weight\n'
        b'20240102,000001.SZ,1.0\n'
    )
    signals = tmp_path / 'signals.csv'
    signals.write_bytes(original)
    config = {
        'mode': 'portfolio',
        'strategy_kind': 'portfolio',
        'input_mode': 'signals',
        'signals_file': str(signals),
        'signals_format': 'auto',
        'start_date': '20240102',
        'end_date': '20240102',
        'pool_source': 'all_market',
        'symbols': None,
        'stamp_duty': 'auto',
        'transfer_fee_rate': 0.00001,
    }
    captured = {}
    worker = BacktestWorker('editor draft is not executed', config)

    def fake_compile(code, *, strategy_kind, strategy_params):
        captured['code'] = code
        captured['kind'] = strategy_kind
        captured['params'] = strategy_params
        return PortfolioStrategy

    monkeypatch.setattr(worker, '_compile_strategy', fake_compile)
    monkeypatch.setattr(
        worker, '_run_portfolio_mode', lambda *_args, **_kwargs: None
    )
    errors = []
    worker.error.connect(errors.append)
    worker.run()

    assert errors == []
    assert worker.signals_artifact_inputs == (
        ('signals_source', 'inputs/signals.csv', original),
    )
    assert captured['kind'] == 'portfolio'
    frozen = captured['params']['SIGNALS_INPUT']
    assert frozen.source_bytes == original
    assert frozen.signal_format == 'target'
    assert worker.config['symbols'] == ['000001.SZ']
    assert 'SIGNALS_INPUT' in worker.artifact_strategy_source
    assert worker.artifact_strategy_source != 'editor draft is not executed'

    # A valid later replacement cannot alter the bytes that produced the run.
    signals.write_bytes(
        b'date,symbol,target_weight\n20240102,000001.SZ,0.5\n'
    )
    artifact = save_gui_run(
        _artifact_portfolio_result(),
        worker.config,
        worker.artifact_strategy_source,
        results_root=tmp_path / 'signals results',
        signals_artifact_inputs=worker.signals_artifact_inputs,
    )
    loaded = load_gui_run(artifact)

    assert ArtifactStore.load(artifact).read_bytes('signals_source') == original
    assert loaded.config['input_mode'] == 'signals'
    assert loaded.config['signals_format'] == 'target'
    assert loaded.config['signals_file'] == str(
        (artifact / 'inputs' / 'signals.csv').resolve()
    )
    assert loaded.config['stamp_duty'] == 'auto'
    assert loaded.config['transfer_fee_rate'] == pytest.approx(0.00001)
    assert str(signals) not in (artifact / 'config.json').read_text(
        encoding='utf-8'
    )


def test_gui_signals_worker_rejects_forged_runtime_snapshot(
    tmp_path, monkeypatch,
):
    from diepi.backtest.cli.signal_input import (
        SignalInstruction,
        SignalReplayInput,
    )

    payload = (
        b'date,symbol,target_weight\n'
        b'20240102,000001.SZ,0.1\n'
    )
    signals = tmp_path / 'signals.csv'
    signals.write_bytes(payload)
    forged = SignalReplayInput(
        source_name='signals.csv',
        source_bytes=payload,
        signal_format='target',
        instructions=(SignalInstruction(
            date='20240102', symbol='600000.SH', target_weight=0.9
        ),),
    )
    worker = BacktestWorker('not executed', {
        'mode': 'portfolio',
        'strategy_kind': 'portfolio',
        'input_mode': 'signals',
        'signals_file': str(signals),
        'signals_format': 'target',
        '_signals_replay_input': forged,
        'start_date': '20240102',
        'end_date': '20240102',
        'pool_source': 'specified',
        'symbols': ['600000.SH'],
    })
    compiled = []
    monkeypatch.setattr(
        worker,
        '_compile_strategy',
        lambda *_args, **_kwargs: compiled.append(True),
    )
    errors = []
    worker.error.connect(errors.append)

    worker.run()

    assert compiled == []
    assert len(errors) == 1
    assert 'do not match the frozen source bytes' in errors[0]


def test_editor_run_uses_verified_signal_snapshot_after_member_changes(
    tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.cli.signal_input import SignalReplayInput
    from diepi.backtest.ui.screens.editor_screen import EditorScreen

    app = QApplication.instance() or QApplication([])
    original = (
        b'date,symbol,target_weight\n'
        b'20240102,000001.SZ,0.1\n'
    )
    signals = tmp_path / 'signals.csv'
    signals.write_bytes(original)
    frozen = SignalReplayInput.from_bytes(
        original, signal_format='target', source_name='signals.csv'
    )
    editor = EditorScreen(data_root=tmp_path)
    editor.set_config({
        'mode': 'portfolio',
        'input_mode': 'signals',
        'signals_file': str(signals.resolve()),
        'signals_format': 'target',
        'start_date': '20240102',
        'end_date': '20240102',
        '_signals_replay_input': frozen,
    })
    signals.write_bytes(
        b'date,symbol,target_weight\n'
        b'20240102,600000.SH,0.9\n'
    )
    emitted = []
    editor.run_backtest.connect(
        lambda _code, config: emitted.append(config)
    )

    editor._on_run()

    assert len(emitted) == 1
    assert emitted[0]['_signals_replay_input'].source_bytes == original
    assert emitted[0]['symbols'] == ['000001.SZ']

    warnings = []
    monkeypatch.setattr(
        'diepi.backtest.ui.screens.editor_screen.QMessageBox.warning',
        lambda *_args: warnings.append(_args),
    )
    conflicting = editor.get_config()
    conflicting['signals_format'] = 'action'
    assert editor._validate_config(conflicting) is False
    assert warnings
    assert 'conflicts with the verified runtime snapshot' in warnings[0][-1]
    assert app is not None


def test_gui_loads_cli_signals_artifact_and_can_rerun_it(
    tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.screens.editor_screen import EditorScreen

    app = QApplication.instance() or QApplication([])
    payload = (
        b'date,symbol,action,percent\n'
        b'20240102,600000.SH,buy,0.5\n'
    )
    template = (
        Path(__file__).resolve().parents[2]
        / 'diepi' / 'backtest' / 'cli' / 'signal_replay_template.py'
    ).read_text(encoding='utf-8')
    cli_config = {
        'command': 'run',
        'input_mode': 'signals',
        'requested_start_date': '20240102',
        'requested_end_date': '20240102',
        'parameters': {
            'initial_cash': 50_000.0,
            'slippage': 0.0,
            'commission': 0.00025,
            'stamp_duty': 'auto',
            'transfer_fee_rate': 0.00001,
            'min_commission': 5.0,
            'freq': 'daily',
            'price_mode': 'dual',
            'pool_symbols': ['600000.SH'],
            'strategy_params': {
                'SIGNALS_INPUT': 'inputs/signals.csv',
                'SIGNALS_FORMAT': 'action',
            },
        },
    }
    artifact = ArtifactStore.save(
        RunOutcome.from_result(
            _artifact_portfolio_result(),
            engine_kind=EngineKind.CASH_PORTFOLIO,
        ),
        tmp_path / 'cli-signals-run',
        config=cli_config,
        strategy_source=template,
        signals_source=payload,
    )

    loaded = load_gui_run(artifact)
    assert loaded.config['input_mode'] == 'signals'
    assert loaded.config['signals_format'] == 'action'
    assert loaded.config['stamp_duty'] == 'auto'
    assert loaded.config['transfer_fee_rate'] == pytest.approx(0.00001)
    assert loaded.config['signals_file'] == str(
        (artifact / 'inputs' / 'signals.csv').resolve()
    )

    # The verified load owns an immutable replay object.  A later in-place
    # mutation of the Artifact member cannot change execution bytes.
    (artifact / 'inputs' / 'signals.csv').write_bytes(
        b'date,symbol,action,percent\n'
        b'20240102,600000.SH,buy,0.25\n'
    )
    editor = EditorScreen(data_root=tmp_path)
    editor.set_config(loaded.config)
    assert editor.get_config()['_signals_replay_input'] is (
        loaded.signals_replay_input
    )
    editor.config_panel.signals_file_edit.setText(str(tmp_path / 'new.csv'))
    assert '_signals_replay_input' not in editor.get_config()

    loaded.config['data_root'] = str(tmp_path)
    worker = BacktestWorker(loaded.strategy_source, loaded.config)
    captured = {}

    def fake_compile(_code, *, strategy_kind, strategy_params):
        captured['kind'] = strategy_kind
        captured['input'] = strategy_params['SIGNALS_INPUT']
        return PortfolioStrategy

    monkeypatch.setattr(worker, '_compile_strategy', fake_compile)
    monkeypatch.setattr(
        worker, '_run_portfolio_mode', lambda *_args, **_kwargs: None
    )
    errors = []
    worker.error.connect(errors.append)
    worker.run()

    assert errors == []
    assert captured['kind'] == 'portfolio'
    assert captured['input'].source_bytes == payload
    assert captured['input'].signal_format == 'action'
    assert worker.signals_artifact_inputs[0][2] == payload
    assert app is not None


def test_gui_views_but_will_not_rerun_mismatched_replay_template(
    tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QApplication
    import diepi.backtest.ui.main_window as main_window_module

    app = QApplication.instance() or QApplication([])
    template = (
        Path(__file__).resolve().parents[2]
        / 'diepi' / 'backtest' / 'cli' / 'signal_replay_template.py'
    ).read_text(encoding='utf-8')
    artifact = ArtifactStore.save(
        RunOutcome.from_result(
            _artifact_portfolio_result(),
            engine_kind=EngineKind.CASH_PORTFOLIO,
        ),
        tmp_path / 'old-signal-template',
        config={
            'command': 'run',
            'input_mode': 'signals',
            'requested_start_date': '20240102',
            'requested_end_date': '20240102',
            'parameters': {
                'freq': 'daily',
                'price_mode': 'dual',
                'pool_symbols': ['000001.SZ'],
                'strategy_params': {
                    'SIGNALS_INPUT': 'inputs/signals.csv',
                    'SIGNALS_FORMAT': 'target',
                },
            },
        },
        strategy_source=template + '\n# prior release variant\n',
        signals_source=(
            b'date,symbol,target_weight\n20240102,000001.SZ,1\n'
        ),
    )
    loaded = load_gui_run(artifact)
    assert 'differs from the installed diepi version' in (
        loaded.rerun_block_reason
    )
    monkeypatch.setattr(
        main_window_module.QMessageBox,
        'warning',
        lambda *_args, **_kwargs: None,
    )
    window = main_window_module.MainWindow(
        results_root=tmp_path / 'results', data_root=tmp_path
    )

    window._on_load_result_finished(loaded)

    assert window.result_screen.get_current_result() is not None
    assert window.editor_screen.run_btn.isEnabled() is False
    assert 'differs from the installed diepi version' in (
        window._artifact_rerun_block_reason
    )
    assert app is not None


def test_cli_artifact_execution_parameters_round_trip_through_gui_worker(
    tmp_path, monkeypatch,
):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.widgets.config_panel import ConfigPanel

    app = QApplication.instance() or QApplication([])
    parameters = {
        'initial_cash': 50_000.125,
        'slippage': 0.00025,
        'commission': 0.000255,
        'stamp_duty': 0.000555,
        'transfer_fee_rate': 0.0000025,
        'min_commission': 3.25,
        'lot_size': 200,
        'liquidity_cap_ratio': 0.123456,
        'open_buy_resize_mode': 'legacy',
        'open_buy_fill_mode': 'open',
        'open_buy_sizing': 'fill',
        'limit_pct_overrides': {'688001': 0.2, '159781.SZ': 0.1},
        't0_overrides': ['159001.SZ', '511'],
        'trading_days_per_year': 244,
        'risk_free_rate': 0.012345,
        'daily_open_cap_yuan': 123.45,
        'daily_close_cap_yuan': None,
        'daily_open_previous_day_ratio': None,
        'daily_close_previous_day_ratio': 0.012345,
        'freq': 'daily',
        'price_mode': 'raw',
        'pool_symbols': ['000001.SZ'],
        'strategy_params': {
            'LOOKBACK': 17,
            'THRESHOLD': 0.125,
            'USE_FILTER': True,
            'LABEL': '',
            'SIGNALS_FORMAT': 'ordinary strategy value',
            'SIGNALS_MANIFEST': 'ordinary strategy value',
            'SIGNALS_CUSTOM': 7,
            'COMBO_TAG': 'ordinary strategy value',
        },
    }
    artifact = ArtifactStore.save(
        RunOutcome.from_result(
            _artifact_portfolio_result(),
            engine_kind=EngineKind.CASH_PORTFOLIO,
        ),
        tmp_path / 'cli-advanced-run',
        config={
            'command': 'run',
            'input_mode': 'strategy',
            'requested_start_date': '20240102',
            'requested_end_date': '20240102',
            'parameters': parameters,
        },
        strategy_source='def on_day(ctx, bars):\n    pass\n',
    )
    loaded = load_gui_run(artifact)
    panel = ConfigPanel(data_root=tmp_path)
    panel.set_config(loaded.config)
    restored = panel.get_config()

    assert restored['strategy_params'] == parameters['strategy_params']

    for key in (
        'initial_cash',
        'slippage',
        'commission',
        'min_commission',
        'lot_size',
        'liquidity_cap_ratio',
        'open_buy_resize_mode',
        'open_buy_fill_mode',
        'open_buy_sizing',
        'limit_pct_overrides',
        't0_overrides',
        'trading_days_per_year',
        'risk_free_rate',
        'stamp_duty',
        'transfer_fee_rate',
        'daily_open_cap_yuan',
        'daily_close_cap_yuan',
        'daily_open_previous_day_ratio',
        'daily_close_previous_day_ratio',
    ):
        assert restored[key] == parameters[key]

    captured = {}
    compiled = {}

    class FakeEngine:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, _strategy):
            return _artifact_portfolio_result()

    monkeypatch.setattr('diepi.backtest.ui.worker.PortfolioEngine', FakeEngine)
    worker = BacktestWorker(loaded.strategy_source, restored)
    def fake_compile(_code, *, strategy_kind, strategy_params):
        compiled['kind'] = strategy_kind
        compiled['params'] = strategy_params
        return PortfolioStrategy

    monkeypatch.setattr(worker, '_compile_strategy', fake_compile)
    monkeypatch.setattr(
        worker, '_capture_market_data_fingerprints', lambda _symbols: ()
    )
    errors = []
    worker.error.connect(errors.append)
    worker.run()

    assert errors == []
    assert compiled == {
        'kind': 'portfolio',
        'params': parameters['strategy_params'],
    }
    for key in (
        'initial_cash',
        'slippage',
        'min_commission',
        'lot_size',
        'liquidity_cap_ratio',
        'open_buy_resize_mode',
        'open_buy_fill_mode',
        'open_buy_sizing',
        'limit_pct_overrides',
        't0_overrides',
        'trading_days_per_year',
        'risk_free_rate',
        'transfer_fee_rate',
    ):
        assert captured[key] == restored[key]
    assert captured['commission_rate'] == restored['commission']
    assert captured['stamp_duty'] == restored['stamp_duty']
    assert captured['daily_auction_liquidity'].open_cap.value == 123.45
    assert captured['daily_auction_liquidity'].close_cap.value == 0.012345
    assert app is not None


def test_gui_parallel_worker_forwards_advanced_execution_parameters(
    tmp_path, monkeypatch,
):
    captured = {}
    _write_daily_parquet(
        tmp_path
        / 'parquet'
        / 'timeseries'
        / 'daily_raw'
        / '600000.SH.parquet'
    )

    class FakeParallelRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self, _code):
            return _artifact_parallel_result()

    config = {
        'mode': 'independent',
        'strategy_kind': 'single',
        'input_mode': 'strategy',
        'start_date': '20240102',
        'end_date': '20240102',
        'initial_cash': 50_000.0,
        'slippage': 0.0003,
        'commission': 0.0002,
        'stamp_duty': 'auto',
        'transfer_fee_rate': 0.00001,
        'min_commission': 3.0,
        'lot_size': 200,
        'liquidity_cap_ratio': 0.625,
        'open_buy_resize_mode': 'legacy',
        'open_buy_fill_mode': 'open',
        'open_buy_sizing': 'fill',
        'limit_pct_overrides': {'688001': 0.2},
        't0_overrides': ['511'],
        'trading_days_per_year': 244,
        'risk_free_rate': 0.0175,
        'freq': 'daily',
        'price_mode': 'raw',
        'pool_source': 'specified',
        'symbols': ['600000.SH'],
        'data_root': str(tmp_path),
        'max_workers': 1,
    }
    monkeypatch.setattr(
        'diepi.backtest.ui.worker.ParallelRunner', FakeParallelRunner
    )
    worker = BacktestWorker('class Example: pass', config)
    monkeypatch.setattr(
        worker, '_compile_strategy', lambda *_args, **_kwargs: Strategy
    )
    monkeypatch.setattr(
        worker, '_capture_market_data_fingerprints', lambda _symbols: ()
    )
    errors = []
    worker.error.connect(errors.append)
    worker.run()

    assert errors == []
    for key in (
        'lot_size',
        'liquidity_cap_ratio',
        'open_buy_resize_mode',
        'open_buy_fill_mode',
        'open_buy_sizing',
        'limit_pct_overrides',
        't0_overrides',
        'trading_days_per_year',
        'risk_free_rate',
        'transfer_fee_rate',
    ):
        assert captured[key] == config[key]
    assert captured['stamp_duty'] == 'auto'


def test_gui_all_market_parallel_artifact_binds_resolved_market_sources(
    tmp_path, monkeypatch,
):
    market_path = (
        tmp_path
        / 'parquet'
        / 'timeseries'
        / 'daily_raw'
        / '600000.SH.parquet'
    )
    _write_daily_parquet(market_path)

    class FakeParallelRunner:
        def __init__(self, **_kwargs):
            pass

        def run(self, _code):
            return _artifact_parallel_result()

    monkeypatch.setattr(
        'diepi.backtest.ui.worker.ParallelRunner', FakeParallelRunner
    )
    monkeypatch.setattr(
        'diepi.backtest.ui.worker._resolve_independent_universe',
        lambda *_args, **_kwargs: (['600000.SH'], (), ()),
    )
    config = {
        'mode': 'independent',
        'strategy_kind': 'single',
        'input_mode': 'strategy',
        'start_date': '20240102',
        'end_date': '20240102',
        'initial_cash': 50_000.0,
        'slippage': 0.0003,
        'commission': 0.0002,
        'stamp_duty': 'auto',
        'transfer_fee_rate': 0.00001,
        'min_commission': 3.0,
        'lot_size': 100,
        'liquidity_cap_ratio': 0.8,
        'open_buy_resize_mode': 'auto',
        'open_buy_fill_mode': 'open+slip',
        'open_buy_sizing': 'limit_up',
        'limit_pct_overrides': None,
        't0_overrides': None,
        'trading_days_per_year': 252,
        'risk_free_rate': 0.03,
        'freq': 'daily',
        'price_mode': 'raw',
        'pool_source': 'all_market',
        'symbols': None,
        'data_root': str(tmp_path),
        'max_workers': 1,
    }
    worker = BacktestWorker('class Example: pass', config)
    monkeypatch.setattr(
        worker, '_compile_strategy', lambda *_args, **_kwargs: Strategy
    )
    errors = []
    results = []
    worker.error.connect(errors.append)
    worker.finished.connect(results.append)

    worker.run()

    assert errors == []
    assert len(results) == 1
    assert worker.config['realized_symbols'] == ['600000.SH']
    assert {
        source.logical_path for source in worker.market_data_fingerprints
    } == {'parquet/timeseries/daily_raw/600000.SH.parquet'}

    artifact = save_gui_run(
        results[0],
        worker.config,
        worker.artifact_strategy_source,
        results_root=tmp_path / 'results',
        market_data_fingerprints=worker.market_data_fingerprints,
    )
    loaded = load_gui_run(artifact)
    assert loaded.artifact_verified is True
    assert loaded.is_rankable is True
    assert loaded.config['realized_symbols'] == ['600000.SH']
    assert {
        source.logical_path
        for source in ArtifactStore.load(artifact).provenance.sources
        if source.kind == 'market_data_file'
    } == {'parquet/timeseries/daily_raw/600000.SH.parquet'}


@pytest.mark.parametrize(
    'params',
    (
        {'BAD': [1]},
        {'BAD': {'nested': 1}},
        {'BAD': None},
        {'not-an-identifier': 1},
        {'SIGNALS_INPUT': 'inputs/signals.csv'},
        {'SIGNALS_FILE': 'inputs/signals.csv'},
        {'COMBO_BUNDLE': 'inputs/combo/diepi_combo.json'},
        {'BAD': float('inf')},
    ),
)
def test_gui_custom_strategy_params_fail_closed_for_unsupported_json(params):
    with pytest.raises(ValueError):
        _normalize_custom_strategy_params(params)


def test_gui_custom_strategy_params_allow_non_mode_metadata_names():
    params = {
        'SIGNALS_FORMAT': 'target',
        'SIGNALS_MANIFEST': 'ordinary strategy value',
        'SIGNALS_CUSTOM': 7,
        'COMBO_TAG': 'ordinary strategy value',
    }

    assert _normalize_custom_strategy_params(params) == params


def test_config_panel_numeric_inputs_round_trip_exactly_without_old_grid_loss():
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.widgets.config_panel import ConfigPanel

    app = QApplication.instance() or QApplication([])
    expected = {
        'initial_cash': 10_000.9,
        'slippage': 0.00025,
        'commission': 0.000255,
        'stamp_duty': 0.000555,
        'transfer_fee_rate': 0.0000025,
        'min_commission': 5.25,
        'liquidity_cap_ratio': 0.123456,
        'risk_free_rate': 0.012345,
        'daily_open_cap_yuan': 123.45,
        'daily_open_previous_day_ratio': None,
        'daily_close_cap_yuan': None,
        'daily_close_previous_day_ratio': 0.012345,
    }
    panel = ConfigPanel()

    panel.set_config(expected)
    restored = panel.get_config()

    for key, value in expected.items():
        assert restored[key] == value
    assert app is not None


@pytest.mark.parametrize(
    ('override', 'message'),
    (
        ({'initial_cash': 0.0}, 'initial_cash'),
        ({'initial_cash': 1e-300}, 'initial_cash'),
        ({'slippage': 1.0}, 'slippage'),
        ({'commission': -0.1}, 'commission'),
        ({'commission': 1e300}, 'commission'),
        ({'stamp_duty': float('nan')}, 'stamp_duty'),
        ({'transfer_fee_rate': 2.55e-6}, 'transfer_fee_rate'),
        ({'min_commission': -0.01}, 'min_commission'),
        ({'liquidity_cap_ratio': 0.12345678901234566},
         'liquidity_cap_ratio'),
        ({'risk_free_rate': float('inf')}, 'risk_free_rate'),
        ({'daily_open_cap_yuan': 0.0}, 'daily_open_cap_yuan'),
        ({'daily_open_previous_day_ratio': 0.0},
         'daily_open_previous_day_ratio'),
        ({'daily_open_previous_day_ratio': 1.01},
         'daily_open_previous_day_ratio'),
        ({'freq': 'weekly'}, 'freq'),
        ({'price_mode': 'future-mode'}, 'price_mode'),
        ({'start_date': '20000101'}, 'start_date'),
        ({'end_date': '20990101'}, 'end_date'),
    ),
)
def test_config_panel_numeric_restore_fails_closed(override, message):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.widgets.config_panel import ConfigPanel

    app = QApplication.instance() or QApplication([])
    panel = ConfigPanel()

    with pytest.raises(ValueError, match=message):
        panel.set_config(override)
    assert app is not None


def test_cli_signal_mapper_keeps_only_template_params():
    mapped = _gui_config_from_artifact({
        'command': 'run',
        'input_mode': 'signals',
        'requested_start_date': '20240102',
        'requested_end_date': '20240102',
        'parameters': {
            'pool_symbols': ['000001.SZ'],
            'strategy_params': {
                'SIGNALS_INPUT': 'inputs/signals.csv',
                'SIGNALS_FORMAT': 'target',
                'SIGNALS_MANIFEST': {'schema': 'ignored-by-GUI-adapter'},
                'UNUSED_CUSTOM': 'must-not-leak-into-template',
            },
        },
    })

    assert mapped['input_mode'] == 'signals'
    assert mapped['signals_format'] == 'target'
    assert mapped['strategy_params'] is None

    with pytest.raises(ValueError, match='input_mode conflicts'):
        _gui_config_from_artifact({
            'command': 'run',
            'input_mode': 'strategy',
            'parameters': {
                'strategy_params': {
                    'SIGNALS_INPUT': 'inputs/signals.csv',
                },
            },
        })


def test_gui_independent_worker_rejects_custom_strategy_params():
    worker = BacktestWorker('ignored', {
        'mode': 'independent',
        'strategy_kind': 'single',
        'input_mode': 'strategy',
        'strategy_params': {'LOOKBACK': 20},
    })
    errors = []
    worker.error.connect(errors.append)

    worker.run()

    assert len(errors) == 1
    assert '独立测试模式不支持策略参数覆盖' in errors[0]


def test_gui_worker_rejects_non_strategy_input_in_independent_mode():
    worker = BacktestWorker('ignored', {
        'mode': 'independent',
        'strategy_kind': 'single',
        'input_mode': 'signals',
        'signals_file': 'does-not-need-to-exist.csv',
    })
    errors = []
    worker.error.connect(errors.append)

    worker.run()

    assert len(errors) == 1
    assert 'signals 输入只支持组合投资模式' in errors[0]


@pytest.mark.parametrize(
    'unsafe_path',
    (
        r'\outside\signals.csv',
        r'C:outside\signals.csv',
        r'\\server\share\signals.csv',
        'C:' + '\\' + r'outside\signals.csv',
        r'..\outside\signals.csv',
    ),
    ids=('root-relative', 'drive-relative', 'unc', 'absolute', 'parent'),
)
def test_gui_v1_signals_restore_rejects_noncanonical_windows_paths(
    tmp_path, unsafe_path,
):
    artifact = ArtifactStore.save(
        RunOutcome.from_result(
            _artifact_portfolio_result(),
            engine_kind=EngineKind.CASH_PORTFOLIO,
        ),
        tmp_path / 'unsafe-signals-artifact',
        config={
            'mode': 'portfolio',
            'strategy_kind': 'portfolio',
            'input_mode': 'signals',
            'signals_file': unsafe_path,
            'signals_format': 'target',
        },
        strategy_source='pass\n',
        signals_source=(
            b'date,symbol,target_weight\n20240102,000001.SZ,1\n'
        ),
    )

    with pytest.raises(ValueError, match='must be exactly'):
        load_gui_run(artifact)


@pytest.mark.parametrize(
    'unsafe_path',
    (
        r'\outside\combo',
        r'C:outside\combo',
        r'\\server\share\combo',
        'C:' + '\\' + r'outside\combo',
        r'..\outside\combo',
    ),
    ids=('root-relative', 'drive-relative', 'unc', 'absolute', 'parent'),
)
def test_gui_v1_combo_restore_rejects_noncanonical_windows_paths(
    tmp_path, unsafe_path,
):
    from diepi.backtest.cli.combo_bundle import load_combo_bundle

    source = tmp_path / 'combo-source'
    source.mkdir()
    pd.DataFrame([{
        'trade_date': '20240102',
        'symbol': '000001.SZ',
        'target_weight': 1.0,
    }]).to_csv(source / 'targets.csv', index=False)
    pd.DataFrame([{
        'trade_date': '20240102',
        'symbol': '000001.SZ',
        'exit_price': 'close',
    }]).to_csv(source / 'close_sells.csv', index=False)
    pd.DataFrame([{
        'date': '20240102',
        'invested_weight': 1.0,
        'cash_weight': 0.0,
    }]).to_csv(source / 'daily.csv', index=False)
    bundle = load_combo_bundle(source)
    payloads = tuple(
        ArtifactPayload(
            role=role,
            path=path,
            media_type=(
                'application/json'
                if path.endswith('.json')
                else 'text/csv; charset=utf-8'
            ),
            data=payload,
        )
        for role, path, payload in bundle.artifact_inputs()
    )
    artifact = ArtifactStore.save(
        RunOutcome.from_result(
            _artifact_portfolio_result(),
            engine_kind=EngineKind.CASH_PORTFOLIO,
        ),
        tmp_path / 'unsafe-combo-artifact',
        config={
            'mode': 'portfolio',
            'strategy_kind': 'portfolio',
            'input_mode': 'combo',
            'combo_bundle': unsafe_path,
        },
        strategy_source='pass\n',
        extra_payloads=payloads,
    )

    with pytest.raises(ValueError, match='must be exactly'):
        load_gui_run(artifact)


def test_gui_v1_restore_rejects_reparse_signal_member(
    tmp_path, monkeypatch,
):
    import stat
    import diepi.artifacts.storage as artifact_storage

    artifact = ArtifactStore.save(
        RunOutcome.from_result(
            _artifact_portfolio_result(),
            engine_kind=EngineKind.CASH_PORTFOLIO,
        ),
        tmp_path / 'linked-signals-artifact',
        config={
            'mode': 'portfolio',
            'strategy_kind': 'portfolio',
            'input_mode': 'signals',
            'signals_file': 'inputs/signals.csv',
            'signals_format': 'target',
        },
        strategy_source='pass\n',
        signals_source=(
            b'date,symbol,target_weight\n20240102,000001.SZ,1\n'
        ),
    )
    member = artifact / 'inputs' / 'signals.csv'
    real_lstat = artifact_storage._lstat
    reparse_flag = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
    monkeypatch.setattr(
        artifact_storage.stat,
        'FILE_ATTRIBUTE_REPARSE_POINT',
        reparse_flag,
        raising=False,
    )

    def simulated_reparse(candidate, label):
        info = real_lstat(candidate, label)
        if Path(candidate) == member:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=(
                    getattr(info, 'st_file_attributes', 0)
                    | reparse_flag
                ),
            )
        return info

    monkeypatch.setattr(artifact_storage, '_lstat', simulated_reparse)

    with pytest.raises(ValueError, match='link|reparse'):
        load_gui_run(artifact)


def test_portfolio_and_parallel_keep_explicit_data_roots_isolated(
    tmp_path, monkeypatch,
):
    first = tmp_path / 'data one'
    second = tmp_path / 'data two'
    first.mkdir()
    second.mkdir()
    captured = []

    class FakeEngine:
        def __init__(self, **kwargs):
            captured.append(kwargs['data_root'])

        def run(self, _strategy):
            return SimpleNamespace(
                result_contract=ResultContract(
                    status=ResultStatus.INVALID,
                    reason=ResultReason('NO_DATA', '测试终态'),
                ),
                is_rankable=False,
                error='', final_value=1_000.0, total_return=0.0,
            )

    monkeypatch.setattr(
        'diepi.backtest.ui.worker.PortfolioEngine', FakeEngine)
    base = {
        'start_date': '20240101', 'end_date': '20240102',
        'initial_cash': 1_000.0, 'freq': 'daily',
        'slippage': 0.0, 'commission': 0.0,
    }
    for root in (first, second):
        worker = BacktestWorker('', {**base, 'data_root': str(root)})
        worker._run_portfolio_mode(
            PortfolioStrategy, PoolSource.SPECIFIED, ['000001.SZ'], None)

    from diepi.backtest.engine.parallel_runner import ParallelRunner
    runner_one = ParallelRunner(
        ['000001.SZ'], '20240101', '20240102', data_root=str(first))
    runner_two = ParallelRunner(
        ['000001.SZ'], '20240101', '20240102', data_root=str(second))

    assert captured == [str(first), str(second)]
    assert runner_one._engine_kwargs()['data_root'] == str(first)
    assert runner_two._engine_kwargs()['data_root'] == str(second)


@pytest.mark.parametrize(
    'contract',
    [
        ResultContract(
            status=ResultStatus.PARTIAL,
            reason=ResultReason('WINDOW_TRUNCATED', '窗口被截断'),
            actual_interval=ActualInterval('2024-01-02', '2024-01-02'),
            data_coverage=DataCoverage(2, 1, 0.5),
        ),
        ResultContract(
            status=ResultStatus.INVALID,
            reason=ResultReason('NO_DATA', '没有可用数据'),
            data_coverage=DataCoverage(2, 0, 0.0),
        ),
        ResultContract(
            status=ResultStatus.CANCELED,
            reason=ResultReason('USER_STOPPED', '用户请求停止'),
        ),
    ],
)
def test_result_screen_displays_and_enables_save_for_non_success_contracts(
    contract,
):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.screens.result_screen import ResultScreen

    app = QApplication.instance() or QApplication([])
    result = PortfolioResult(
        start_date='20240101', end_date='20240105',
        initial_cash=100_000.0, final_value=100_000.0,
        total_return=0.0, annual_return=0.0, max_drawdown=0.0,
        trade_count=0, sharpe_ratio=None,
        result_contract=contract,
    )
    screen = ResultScreen()

    screen.display_result(result)
    screen.set_running(False)

    assert contract.status.value in screen.contract_summary.text()
    assert screen.get_current_result() is result
    assert screen.save_btn.isEnabled()
    assert screen.stat_labels['benchmark_return'].text() == '不可用'
    assert app is not None


def test_result_screen_historical_kline_requires_matching_artifact_source(
    tmp_path,
):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.screens.result_screen import ResultScreen

    app = QApplication.instance() or QApplication([])
    market = (
        tmp_path / 'parquet' / 'timeseries' / 'daily_raw'
        / '000001.SZ.parquet'
    )
    _write_daily_parquet(market, close=10.0)
    provenance = RunProvenance.build(sources=collect_market_data_fingerprints(
        tmp_path, symbols=['000001.SZ'], price_mode='raw'
    ))
    screen = ResultScreen()
    screen.set_market_data_context(
        config={'price_mode': 'raw', 'symbols': ['000001.SZ']},
        data_root=tmp_path,
        provenance=provenance,
        historical_artifact=True,
    )

    assert screen._kline_price_mode('000001.SZ') == 'raw'
    assert '已通过' in screen.kline_evidence_label.text()
    _write_daily_parquet(market, close=11.0)
    assert screen._kline_price_mode('000001.SZ') is None
    assert '已禁用' in screen.kline_evidence_label.text()
    assert app is not None


def test_trade_double_click_opens_symbol_date_and_populates_detail(monkeypatch):
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.screens.result_screen import ResultScreen

    app = QApplication.instance() or QApplication([])
    screen = ResultScreen()
    trades = [{
        'time': '20240103',
        'symbol': '000001.SZ',
        'direction': 'BUY',
        'shares': 100,
        'price': 10.0,
        'amount': 1000.0,
        'note': 'example',
    }]
    screen._current_result = SimpleNamespace(trades=trades)
    screen._fill_trades_table(trades)
    opened = []
    monkeypatch.setattr(
        screen,
        '_open_kline_dialog',
        lambda symbol, *, focus_date=None: opened.append(
            (symbol, focus_date)
        ),
    )

    screen._on_trade_double_clicked(
        screen.trades_table.model().index(0, 0)
    )

    assert opened == [('000001.SZ', '20240103')]
    assert screen.tabs.currentIndex() == screen._kline_tab_index
    assert screen.stock_trade_table.rowCount() == 1
    assert '000001.SZ' in screen.stock_trade_label.text()
    assert app is not None


@pytest.mark.skipif(not HAS_PYQTGRAPH, reason="pyqtgraph is unavailable")
def test_result_screen_uses_recorded_drawdown_and_execution_journal():
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.screens.result_screen import ResultScreen

    app = QApplication.instance() or QApplication([])
    result = _artifact_portfolio_result()
    result.daily_values['drawdown_close_nav'] = [0.125]
    screen = ResultScreen()
    screen.display_result(result)

    assert '引擎记录' in screen.drawdown_status_label.text()
    assert screen.events_table.rowCount() == 0
    assert '0 条' in screen.events_status_label.text()
    assert app is not None


def test_parallel_row_double_click_opens_complete_child_and_returns():
    from PySide6.QtWidgets import QApplication
    from diepi.backtest.ui.screens.result_screen import ResultScreen

    app = QApplication.instance() or QApplication([])
    aggregate = _artifact_parallel_result()
    screen = ResultScreen()
    screen.display_result(aggregate)
    screen.save_btn.setEnabled(True)

    screen._on_parallel_double_clicked(
        screen.parallel_table.model().index(0, 0)
    )
    assert screen.get_current_result() is aggregate.results['600000.SH']
    assert screen.back_parallel_btn.isHidden() is False
    assert screen.save_btn.isEnabled() is False
    assert screen.trades_table.rowCount() == 0

    screen._on_back_parallel_result()
    assert screen.get_current_result() is aggregate
    assert screen.back_parallel_btn.isHidden() is True
    assert screen.save_btn.isEnabled() is True
    assert app is not None


def test_editor_loads_packaged_ma_example_and_raw_defaults(monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from diepi.backtest.ui.screens.editor_screen import EditorScreen

    app = QApplication.instance() or QApplication([])
    editor = EditorScreen()
    monkeypatch.setattr(
        QMessageBox, 'question', staticmethod(lambda *args, **kwargs: QMessageBox.Yes)
    )

    editor._on_load_ma_example()

    assert 'SLOW_PERIOD = 20' in editor.get_code()
    config = editor.get_config()
    assert config['mode'] == 'portfolio'
    assert config['price_mode'] == 'raw'
    assert config['daily_open_previous_day_ratio'] == pytest.approx(0.1)
    assert config['daily_close_previous_day_ratio'] == pytest.approx(0.1)
    assert '不会配置数据根、标的或日期' in editor.example_btn.toolTip()
    assert app is not None


def test_editor_loads_complete_public_sample_preset(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox
    from diepi.backtest.ui.screens.editor_screen import EditorScreen

    sample_root = (tmp_path / 'public sample' / 'data').resolve()
    sample_root.mkdir(parents=True)
    monkeypatch.setattr(
        EditorScreen,
        '_find_public_sample_data_root',
        staticmethod(lambda: sample_root),
    )
    monkeypatch.setattr(
        QMessageBox, 'question', staticmethod(lambda *args, **kwargs: QMessageBox.Yes)
    )
    app = QApplication.instance() or QApplication([])
    editor = EditorScreen()

    editor._on_load_public_sample()

    config = editor.get_config()
    assert config['data_root'] == str(sample_root)
    assert config['input_mode'] == 'strategy'
    assert config['mode'] == 'portfolio'
    assert config['symbols'] == ['600000.SH', '510300.SH']
    assert config['start_date'] == '20260101'
    assert config['end_date'] == '20260630'
    assert config['freq'] == 'daily'
    assert config['price_mode'] == 'dual'
    assert config['stamp_duty'] == 'auto'
    assert config['daily_open_previous_day_ratio'] == pytest.approx(0.1)
    assert config['daily_close_previous_day_ratio'] == pytest.approx(0.1)
    assert 'SLOW_PERIOD = 20' in editor.get_code()
    assert editor.public_sample_btn.isEnabled()
    assert app is not None


class _FakeWorker:
    def __init__(self, *, exits=True, stoppable=True):
        self.running = True
        self.exits = exits
        self.stop_calls = 0
        if not stoppable:
            self.stop = None

    def isRunning(self):
        return self.running

    def stop(self):
        self.stop_calls += 1

    def wait(self, timeout_ms):
        if self.exits:
            self.running = False
        return self.exits


def test_worker_shutdown_never_claims_success_for_a_live_thread():
    cooperative = _FakeWorker(exits=True)
    stuck = _FakeWorker(exits=False, stoppable=False)

    assert _request_worker_shutdown((cooperative, stuck), timeout_ms=1) is False
    assert cooperative.stop_calls == 1
    assert cooperative.running is False
    assert stuck.running is True


@pytest.mark.skipif(not HAS_PYQTGRAPH, reason="pyqtgraph is unavailable")
def test_minute_drilldown_commits_state_only_after_data_arrives():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    chart = KLineChart()
    daily = pd.DataFrame(
        {
            "trade_date": ["20240102"],
            "open": [10.0],
            "high": [10.2],
            "low": [9.8],
            "close": [10.1],
            "vol": [1000.0],
        }
    )
    chart.set_data(daily, symbol="000001.SZ")

    class EmptyProvider:
        def __init__(self):
            self.calls = []

        def get_minute(self, symbol, trade_date, *, price_mode=None):
            self.calls.append((symbol, trade_date, price_mode))
            return pd.DataFrame()

    provider = EmptyProvider()
    chart.set_data_provider(provider, price_mode='raw')
    chart.expand_to_minute("20240102")

    assert app is not None
    assert chart.mode == "daily"
    assert chart._current_date == ""
    assert chart._daily_data is not None
    assert provider.calls == [('000001.SZ', '20240102', 'raw')]


@pytest.mark.skipif(not HAS_PYQTGRAPH, reason="pyqtgraph is unavailable")
def test_verified_kline_disables_unfingerprinted_minute_drilldown():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    chart = KLineChart()
    chart.set_data(pd.DataFrame({
        'trade_date': ['20240102'],
        'open': [10.0],
        'high': [10.0],
        'low': [10.0],
        'close': [10.0],
        'vol': [0.0],
    }), symbol='000001.SZ')
    requested = []
    chart.minute_data_requested.connect(
        lambda symbol, date: requested.append((symbol, date))
    )
    chart.set_data_provider(None)

    chart.expand_to_minute('20240102')

    assert requested == []
    assert chart.mode == 'daily'
    assert app is not None
