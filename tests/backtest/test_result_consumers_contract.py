"""Consumer gates for nullable metrics and rankable result catalogues."""

import pandas as pd
import pytest

from diepi.backtest.cli.runner import (
    _build_equity_curve_export,
    _update_index,
    _validate_run_name,
)
from diepi.backtest.data.report_generator import ReportGenerator
from diepi.backtest.engine.attribution import calculate_attribution
from diepi.backtest.engine.portfolio_engine import PortfolioResult
from diepi.backtest.result_contract import (
    ActualInterval,
    DataCoverage,
    ResultContract,
    ResultStatus,
)


def _success_contract() -> ResultContract:
    return ResultContract(
        status=ResultStatus.SUCCESS,
        actual_interval=ActualInterval("2024-01-02", "2024-01-02"),
        data_coverage=DataCoverage(1, 1, 1.0),
    )


def _metrics(*, sharpe_ratio=None):
    return {
        "annual_return": 0.0,
        "sharpe_ratio": sharpe_ratio,
        "max_drawdown": 0.0,
        "max_drawdown_intraday_low_nav": 0.0,
        "calmar_ratio": 0.0,
        "trade_count": 0,
    }


def _meta():
    return {
        "strategy_name": "nullable",
        "start_date": "20240102",
        "end_date": "20240102",
        "run_time": "2024-01-02T15:00:00",
    }


def _portfolio_result(*, trades=None) -> PortfolioResult:
    return PortfolioResult(
        start_date="20240102",
        end_date="20240102",
        initial_cash=100_000.0,
        final_value=100_000.0,
        total_return=0.0,
        annual_return=0.0,
        max_drawdown=0.0,
        trade_count=0,
        sharpe_ratio=None,
        trades=[] if trades is None else trades,
        result_contract=_success_contract(),
    )


def test_nonrankable_run_never_enters_index(tmp_path):
    _update_index(
        tmp_path,
        "partial-run",
        _metrics(),
        _meta(),
        rankable=False,
        result_contract=None,
    )

    assert not (tmp_path / "index.csv").exists()


def test_rankable_index_preserves_nullable_sharpe_and_status(tmp_path):
    contract = _success_contract().to_dict()
    _update_index(
        tmp_path,
        "success-run",
        _metrics(),
        _meta(),
        rankable=True,
        result_contract=contract,
    )

    row = pd.read_csv(tmp_path / "index.csv").iloc[0]
    assert pd.isna(row["sharpe_ratio"])
    assert row["result_status"] == "SUCCESS"
    assert bool(row["rankable"])


def test_rankable_index_rejects_incoherent_contract(tmp_path):
    contract = _success_contract().to_dict()
    contract["status"] = "PARTIAL"

    with pytest.raises(ValueError, match="must be SUCCESS"):
        _update_index(
            tmp_path,
            "bad-run",
            _metrics(),
            _meta(),
            rankable=True,
            result_contract=contract,
        )


def test_index_preserves_numeric_run_ids_and_rejects_duplicates(tmp_path):
    contract = _success_contract().to_dict()
    for run_id in ('000123', '000124'):
        _update_index(
            tmp_path,
            run_id,
            _metrics(),
            _meta(),
            rankable=True,
            result_contract=contract,
        )

    frame = pd.read_csv(tmp_path / 'index.csv', dtype={'run_id': str})
    assert frame['run_id'].tolist() == ['000123', '000124']
    with pytest.raises(FileExistsError, match='拒绝覆盖'):
        _update_index(
            tmp_path,
            '000123',
            _metrics(),
            _meta(),
            rankable=True,
            result_contract=contract,
        )


@pytest.mark.parametrize(
    'bad',
    ['../escape', r'..\\escape', 'C:escape', '.', 'NUL',
     'index.csv', 'INDEX.CSV', 'Index.Csv', '含中文', 'x' * 129],
)
def test_run_name_must_be_one_portable_component(bad):
    with pytest.raises(ValueError, match='run_name'):
        _validate_run_name(bad)


def test_cli_equity_export_preserves_engine_return_and_drawdown_vectors():
    daily = pd.DataFrame({
        'date': ['2024-01-02', '2024-01-03'],
        'total_value': [90.0, 99.0],
        'cash': [90.0, 99.0],
        'market_value': [0.0, 0.0],
        'intraday_low_total_value': [80.0, 85.0],
        'daily_return': [-0.1, 0.1],
        'drawdown_close_nav': [0.1, 0.01],
        'drawdown_intraday_low_nav': [0.2, 0.15],
        'drawdown_intraday_high_to_low': [0.2, 0.055],
    })

    exported = _build_equity_curve_export(daily, initial_cash=100.0)

    assert exported['daily_return'].tolist() == [-0.1, 0.1]
    assert exported['drawdown'].tolist() == [0.1, 0.01]
    assert exported['intraday_low_drawdown'].tolist() == [0.2, 0.15]
    assert exported['drawdown_intraday_high_to_low'].tolist() == [0.2, 0.055]


def test_html_report_renders_nullable_sharpe_and_explicit_status():
    rendered = ReportGenerator._build_html(_portfolio_result(), {}, "pass")

    assert "N/A" in rendered
    assert "SUCCESS" in rendered


def test_attribution_and_html_do_not_invent_zero_win_rate(monkeypatch):
    trades = [{
        "symbol": "510300.SH",
        "direction": "BUY",
        "time": "20240102",
        "amount": 10_000.0,
    }]
    attribution = calculate_attribution(trades, 100_000.0)
    assert attribution["summary"]["win_rate"] is None
    assert attribution["by_stock"].iloc[0]["win_rate"] is None

    monkeypatch.setattr(
        ReportGenerator,
        "_get_stock_name",
        classmethod(lambda cls, symbol: "ETF"),
    )
    table = ReportGenerator._generate_stock_table(
        _portfolio_result(trades=trades)
    )
    assert "N/A" in table
