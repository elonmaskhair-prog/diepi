import pickle
from datetime import date, datetime

import pandas as pd
import pytest

from diepi.backtest.comparison import (
    ComparisonBundle,
    ComparisonScope,
    ReferenceIndexResult,
    ReferenceIndexSpec,
    TotalReturnIndexSeries,
)
from diepi.backtest.broker.events import ExecutionEventJournal
from diepi.backtest.broker.replay import CashAuditBundle, CashReplaySeed
from diepi.backtest.engine.backtest_engine import BacktestResult
from diepi.backtest.engine.parallel_runner import (
    ParallelResult,
    ParallelRunner,
    _restore_backtest_result,
    _serialize_backtest_result_wire,
)
from diepi.backtest.liquidity import AuctionCapSpec, DailyAuctionLiquidityPolicy
from diepi.backtest.result_contract import (
    ActualInterval,
    DataCoverage,
    ResultContract,
    ResultReason,
    ResultStatus,
    ResultWarning,
)
from diepi.backtest.strategy.base import Strategy


def test_parallel_result_preserves_legacy_positional_field_order():
    top = [{"symbol": "000001.SZ"}]
    result = ParallelResult(
        "20240101",
        "20240131",
        1_000_000.0,
        1,
        1,
        0,
        0.1,
        0.1,
        -0.05,
        1.0,
        0.5,
        top,
    )

    assert result.top_performers is top
    assert result.universe_assumptions == {}
    assert result.universe_warnings == []


def _contract(status: ResultStatus = ResultStatus.SUCCESS) -> ResultContract:
    if status is ResultStatus.SUCCESS:
        return ResultContract(
            status=status,
            actual_interval=ActualInterval("2024-01-02", "2024-01-03"),
            data_coverage=DataCoverage(2, 2, 1.0),
        )
    reason = ResultReason(
        f"{status.value}_TEST",
        f"Synthetic {status.value.lower()} result",
    )
    if status is ResultStatus.PARTIAL:
        return ResultContract(
            status=status,
            reason=reason,
            actual_interval=ActualInterval("2024-01-02", "2024-01-02"),
            data_coverage=DataCoverage(2, 1, 0.5),
        )
    return ResultContract(status=status, reason=reason)


def _daily_scope(*dates: str) -> pd.DataFrame:
    return pd.DataFrame(index=pd.to_datetime(list(dates)))


def _payload(
    symbol: str,
    *,
    status: ResultStatus = ResultStatus.SUCCESS,
    total_return: float = 0.10,
    annual_return: float = 0.20,
    max_drawdown: float = 0.31,
    win_rate=None,
    sharpe_ratio=None,
) -> dict:
    result = BacktestResult(
        symbol=symbol,
        start_date="20240102",
        end_date="20240103",
        initial_cash=100_000.0,
        final_value=100_000.0 * (1 + total_return),
        total_return=total_return,
        annual_return=annual_return,
        max_drawdown=max_drawdown,
        trade_count=0 if win_rate is None else 2,
        win_rate=win_rate,
        sharpe_ratio=sharpe_ratio,
        max_drawdown_close_nav=0.11,
        max_drawdown_intraday_low_nav=0.22,
        max_drawdown_intraday_high_to_low=0.33,
        daily_values=(
            _daily_scope("2024-01-02", "2024-01-03")
            if status is ResultStatus.SUCCESS
            else (
                _daily_scope("2024-01-02")
                if status is ResultStatus.PARTIAL
                else _daily_scope()
            )
        ),
        result_contract=_contract(status),
    )
    return _serialize_backtest_result_wire(result)


def test_parallel_wire_preserves_raw_fraction_and_sub_cent_nav():
    result = BacktestResult(
        symbol="EXACT",
        start_date="20240102",
        end_date="20240103",
        initial_cash=100_000.0,
        final_value=100_000.001234567,
        total_return=0.00000001234567,
        annual_return=0.1234567890123,
        max_drawdown=0.0000009876543,
        trade_count=2,
        win_rate=1 / 3,
        sharpe_ratio=1.234567890123,
        max_drawdown_close_nav=0.0000001234567,
        max_drawdown_intraday_low_nav=0.0000002234567,
        max_drawdown_intraday_high_to_low=0.0000003234567,
        daily_values=_daily_scope("2024-01-02", "2024-01-03"),
        result_contract=_contract(),
    )

    payload = _serialize_backtest_result_wire(result)
    restored = _restore_backtest_result("EXACT", payload)

    assert payload["final_value"] == result.final_value
    assert payload["win_rate"] == 1 / 3
    assert restored.final_value == result.final_value
    assert restored.win_rate == 1 / 3
    assert restored.annual_return == result.annual_return


def test_parallel_wire_restores_cash_audit_and_rejects_metric_tamper():
    seed = CashReplaySeed(
        as_of=datetime(2024, 1, 2),
        account_id="cash-account",
        initial_cash=100_000.0,
        initial_frozen_cash=0.0,
    )
    audit = CashAuditBundle.from_parts(seed, ExecutionEventJournal())
    result = BacktestResult(
        symbol="AUDITED",
        start_date="20240102",
        end_date="20240103",
        initial_cash=100_000.0,
        final_value=100_000.0,
        total_return=0.0,
        annual_return=0.0,
        max_drawdown=0.0,
        trade_count=0,
        win_rate=None,
        sharpe_ratio=None,
        daily_values=_daily_scope("2024-01-02", "2024-01-03"),
        trades=[],
        cash_audit=audit,
        result_contract=_contract(),
    )
    payload = _serialize_backtest_result_wire(result)

    restored = _restore_backtest_result("AUDITED", payload)

    assert restored.cash_audit == audit
    assert restored.trades == []
    payload["final_value"] += 1.0
    with pytest.raises(ValueError, match="final_value"):
        _restore_backtest_result("AUDITED", payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update({"unknown": 1}),
        lambda payload: payload.update({"schema": "wrong"}),
        lambda payload: payload.update({"schema_version": True}),
        lambda payload: payload.update({"total_return": float("nan")}),
    ],
)
def test_parallel_wire_rejects_unknown_or_invalid_envelope(mutate):
    payload = _payload("A")
    mutate(payload)

    with pytest.raises((TypeError, ValueError)):
        _restore_backtest_result("A", payload)


@pytest.mark.parametrize(
    "observation_ids",
    [
        ("2024-01-02", "2024-01-03"),
        [datetime(2024, 1, 2), datetime(2024, 1, 3)],
        ["20240102", "20240103"],
        ["2024-01-03", "2024-01-02"],
        ["2024-01-02"],
    ],
)
def test_parallel_wire_rejects_noncanonical_observation_scope(observation_ids):
    payload = _payload("A")
    payload["observation_ids"] = observation_ids

    with pytest.raises((TypeError, ValueError)):
        _restore_backtest_result("A", payload)


def _runner(symbols, **kwargs) -> ParallelRunner:
    return ParallelRunner(
        symbols=symbols,
        start_date="20240102",
        end_date="20240103",
        **kwargs,
    )


def test_constructor_rejects_duplicate_symbols_before_task_submission():
    with pytest.raises(ValueError, match="symbols must be unique"):
        _runner(["DUPLICATE", "OTHER", "DUPLICATE"])


def test_constructor_snapshots_the_caller_owned_symbol_list():
    symbols = ["A", "B"]
    runner = _runner(symbols)

    symbols.append("A")

    assert runner.symbols == ["A", "B"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_date": "20240104", "end_date": "20240103"},
        {"initial_cash": True},
        {"initial_cash": float("nan")},
        {"initial_cash": 0},
    ],
)
def test_constructor_rejects_invalid_run_envelope(kwargs):
    defaults = {
        "symbols": ["A"],
        "start_date": "20240102",
        "end_date": "20240103",
    }
    defaults.update(kwargs)

    with pytest.raises((TypeError, ValueError)):
        ParallelRunner(**defaults)


def test_constructor_canonicalizes_date_objects_before_child_dispatch():
    runner = ParallelRunner(
        symbols=["A"],
        start_date=date(2024, 1, 2),
        end_date=datetime(2024, 1, 3, 12, 0),
    )

    assert runner.start_date == "20240102"
    assert runner.end_date == "20240103"
    assert runner._engine_kwargs()["start_date"] == "20240102"
    assert runner._engine_kwargs()["end_date"] == "20240103"


def test_constructor_forwards_exact_picklable_daily_auction_policy():
    policy = DailyAuctionLiquidityPolicy(
        open_cap=AuctionCapSpec.fixed_yuan(10_000.0),
        close_cap=AuctionCapSpec.previous_day_ratio(0.05),
    )
    runner = _runner(["GOOD"], daily_auction_liquidity=policy)

    kwargs = runner._engine_kwargs()
    assert kwargs["daily_auction_liquidity"] is policy
    assert pickle.loads(pickle.dumps(kwargs))["daily_auction_liquidity"] == policy

    class DerivedPolicy(DailyAuctionLiquidityPolicy):
        pass

    with pytest.raises(TypeError, match="must be exactly"):
        _runner(["GOOD"], daily_auction_liquidity=DerivedPolicy())


def test_aggregate_accepts_only_strict_success_and_preserves_contract_and_mdd():
    symbols = [
        "GOOD_A",
        "GOOD_B",
        "PARTIAL",
        "INVALID",
        "FAILED",
        "CANCELED",
        "MISSING_CONTRACT",
        "DAMAGED_CONTRACT",
        "NO_RESULT",
    ]
    runner = _runner(symbols)
    payloads = {
        "GOOD_A": _payload("GOOD_A"),
        "GOOD_B": _payload(
            "GOOD_B",
            total_return=-0.02,
            annual_return=-0.04,
            max_drawdown=0.21,
            win_rate=0.5,
            sharpe_ratio=2.0,
        ),
        "PARTIAL": _payload("PARTIAL", status=ResultStatus.PARTIAL),
        "INVALID": _payload("INVALID", status=ResultStatus.INVALID),
        "FAILED": _payload("FAILED", status=ResultStatus.FAILED),
        "CANCELED": _payload("CANCELED", status=ResultStatus.CANCELED),
        "MISSING_CONTRACT": _payload("MISSING_CONTRACT"),
        "DAMAGED_CONTRACT": _payload("DAMAGED_CONTRACT"),
    }
    payloads["MISSING_CONTRACT"].pop("result_contract")
    payloads["DAMAGED_CONTRACT"]["result_contract"]["rankable"] = False

    aggregate = runner._aggregate_results(payloads, {})

    assert aggregate.total_symbols == 9
    assert aggregate.success_count == 2
    assert aggregate.failed_count == 7
    assert set(aggregate.results) == {"GOOD_A", "GOOD_B"}
    assert set(aggregate.errors) == set(symbols) - set(aggregate.results)
    assert "status=PARTIAL is not rankable" in aggregate.errors["PARTIAL"]
    assert "status=INVALID is not rankable" in aggregate.errors["INVALID"]
    assert "status=FAILED is not rankable" in aggregate.errors["FAILED"]
    assert "status=CANCELED is not rankable" in aggregate.errors["CANCELED"]
    assert "missing result_contract" in aggregate.errors["MISSING_CONTRACT"]
    assert "rankable does not agree" in aggregate.errors["DAMAGED_CONTRACT"]
    assert aggregate.errors["NO_RESULT"].startswith("NO_RESULT:")

    assert aggregate.is_rankable is False
    assert aggregate.ranking_scope is None
    assert aggregate.ranking_error.startswith("INCOMPLETE_SYMBOL_COVERAGE")
    assert aggregate.avg_return == 0.0
    assert aggregate.avg_annual_return == 0.0
    assert aggregate.avg_max_drawdown == 0.0
    assert aggregate.avg_sharpe is None
    assert aggregate.avg_win_rate is None
    assert aggregate.top_performers == []
    assert aggregate.worst_performers == []

    restored = aggregate.results["GOOD_A"]
    assert restored.result_contract.status is ResultStatus.SUCCESS
    assert restored.result_contract.actual_interval == _contract().actual_interval
    assert restored.result_contract.data_coverage == _contract().data_coverage
    assert restored.result_contract.is_rankable is True
    assert {
        warning.code for warning in restored.result_contract.warnings
    } == {"PARALLEL_COMPARISON_NOT_GENERATED"}
    assert {
        assumption.key: assumption.value
        for assumption in restored.result_contract.assumptions
    }["comparison.parallel_aggregation"] == (
        "unsupported_without_child_daily_nav_series"
    )
    assert restored.max_drawdown_close_nav == pytest.approx(0.11)
    assert restored.max_drawdown_intraday_low_nav == pytest.approx(0.22)
    assert restored.max_drawdown_intraday_high_to_low == pytest.approx(0.33)
    assert aggregate.to_dict()["errors"] == aggregate.errors
    assert aggregate.comparisons is None
    disclosure = aggregate.to_dict()["comparison_disclosure"]
    assert disclosure["status"] == "UNAVAILABLE"
    assert disclosure["reason"]["code"] == (
        "PARALLEL_COMPARISON_SERIES_UNAVAILABLE"
    )
    assert disclosure["warnings"][0]["code"] == (
        "PARALLEL_COMPARISON_NOT_GENERATED"
    )
    assert disclosure["assumptions"] == [
        {
            "key": "comparison.parallel_aggregation",
            "value": "unsupported_without_child_daily_nav_series",
        }
    ]


def test_optional_metrics_average_only_present_samples_and_serialize_null():
    runner = _runner(["A", "B"])
    aggregate = runner._aggregate_results(
        {"A": _payload("A"), "B": _payload("B")},
        {},
    )

    assert aggregate.avg_sharpe is None
    assert aggregate.avg_win_rate is None
    assert aggregate.to_dict()["avg_sharpe"] is None
    assert aggregate.to_dict()["avg_win_rate"] is None
    assert aggregate.is_rankable is True
    assert aggregate.ranking_scope == ("2024-01-02", "2024-01-03", 2, 2)
    assert all(item["sharpe"] is None for item in aggregate.top_performers)


def test_success_children_with_different_exact_day_sets_are_not_ranked():
    runner = _runner(["LEFT", "RIGHT"])
    left = _payload("LEFT")
    right = _payload("RIGHT")
    shared_contract = ResultContract(
        status=ResultStatus.SUCCESS,
        actual_interval=ActualInterval("2024-01-02", "2024-01-05"),
        data_coverage=DataCoverage(3, 3, 1.0),
    ).to_dict()
    left["result_contract"] = shared_contract
    right["result_contract"] = shared_contract
    left["observation_ids"] = [
        "2024-01-02", "2024-01-03", "2024-01-05"
    ]
    right["observation_ids"] = [
        "2024-01-02", "2024-01-04", "2024-01-05"
    ]

    aggregate = runner._aggregate_results(
        {"LEFT": left, "RIGHT": right},
        {},
    )

    assert aggregate.success_count == 2
    assert aggregate.failed_count == 0
    assert aggregate.is_rankable is False
    assert aggregate.ranking_scope is None
    assert aggregate.ranking_error.startswith("INCONSISTENT_CHILD_SCOPE")
    assert aggregate.top_performers == []
    assert aggregate.worst_performers == []
    assert aggregate.to_dict()["ranking_disclosure"]["scope"] is None


def test_window_truncated_success_is_not_ranked():
    runner = _runner(["TRUNCATED"])
    payload = _payload("TRUNCATED")
    payload["result_contract"] = ResultContract(
        status=ResultStatus.SUCCESS,
        warnings=(ResultWarning(
            "WINDOW_TRUNCATED",
            "requested tail exceeds the available completed-data window",
        ),),
        actual_interval=ActualInterval("2024-01-02", "2024-01-03"),
        data_coverage=DataCoverage(2, 2, 1.0),
    ).to_dict()

    aggregate = runner._aggregate_results({"TRUNCATED": payload}, {})

    assert aggregate.success_count == 1
    assert aggregate.failed_count == 0
    assert aggregate.is_rankable is False
    assert aggregate.ranking_error.startswith("REQUESTED_WINDOW_TRUNCATED")
    assert aggregate.top_performers == []


def test_empty_success_set_has_visible_failures_and_null_optional_averages():
    runner = _runner(["BROKEN", "ABSENT"])
    damaged = _payload("BROKEN")
    damaged["result_contract"]["schema_version"] = 0

    aggregate = runner._aggregate_results({"BROKEN": damaged}, {})

    assert aggregate.success_count == 0
    assert aggregate.failed_count == aggregate.total_symbols == 2
    assert set(aggregate.errors) == {"BROKEN", "ABSENT"}
    assert aggregate.avg_sharpe is None
    assert aggregate.avg_win_rate is None
    assert aggregate.to_dict()["avg_sharpe"] is None
    assert aggregate.to_dict()["avg_win_rate"] is None
    assert aggregate.to_dict()["comparisons"] is None
    assert aggregate.to_dict()["comparison_disclosure"]["status"] == (
        "UNAVAILABLE"
    )


def _reference_bundle_payload():
    scope = ComparisonScope(("20240102", "20240103"))
    spec = ReferenceIndexSpec(
        code="000300.SH",
        source_id="fixture:index-total-return",
        source_version="snapshot-2024-01",
    )
    series = TotalReturnIndexSeries(
        spec=spec,
        scope=scope,
        base_observation_id="20240101",
        base_level=100.0,
        levels=(101.0, 102.0),
    )
    return ComparisonBundle(
        scope=scope,
        reference_index_total_return=ReferenceIndexResult.from_series(series),
    ).to_dict()


def test_parallel_aggregate_rejects_non_null_child_comparison_instead_of_dropping():
    runner = _runner(["A"])
    payload = _payload("A")
    payload["comparisons"] = _reference_bundle_payload()

    aggregate = runner._aggregate_results({"A": payload}, {})

    assert aggregate.success_count == 0
    assert aggregate.failed_count == 1
    assert "PARALLEL_COMPARISON_SERIES_UNAVAILABLE" in aggregate.errors["A"]
    assert "cannot be preserved" in aggregate.errors["A"]


def test_parallel_aggregate_rejects_unscoped_numeric_reference_excess():
    runner = _runner(["A"])
    payload = _payload("A")
    payload["comparisons"] = None
    payload["reference_total_return_excess"] = 0.05

    aggregate = runner._aggregate_results({"A": payload}, {})

    assert aggregate.success_count == 0
    assert "PARALLEL_COMPARISON_SERIES_UNAVAILABLE" in aggregate.errors["A"]
    assert "numeric reference excess" in aggregate.errors["A"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("start_date", "19990101"),
        ("end_date", "19990131"),
        ("initial_cash", 1.0),
    ],
)
def test_parallel_aggregate_binds_child_to_requested_run_envelope(field, value):
    runner = _runner(["A"])
    payload = _payload("A")
    payload[field] = value

    aggregate = runner._aggregate_results({"A": payload}, {})

    assert aggregate.success_count == 0
    assert aggregate.failed_count == 1
    assert "RUN_ENVELOPE_MISMATCH" in aggregate.errors["A"]
    assert aggregate.is_rankable is False


def test_sequential_stop_marks_every_unstarted_symbol_canceled_without_engine():
    class NeverRunStrategy(Strategy):
        pass

    runner = _runner(["A", "B", "C"], stop_check=lambda: True)
    aggregate = runner.run_sequential(NeverRunStrategy)

    assert aggregate.success_count == 0
    assert aggregate.failed_count == aggregate.total_symbols == 3
    assert set(aggregate.errors) == {"A", "B", "C"}
    assert all(
        message.startswith("CANCELED:")
        for message in aggregate.errors.values()
    )
