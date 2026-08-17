"""Result-contract integration tests for explicit reference comparisons."""

import json
from pathlib import Path

import pandas as pd
import pytest

from diepi.backtest.comparison import (
    ComparisonBundle,
    ComparisonReason,
    ComparisonScope,
    ComparisonStatus,
    ReferenceIndexInvalidError,
    ReferenceIndexPartialError,
    ReferenceIndexResult,
    ReferenceIndexSpec,
    ReferenceIndexUnavailableError,
    TotalReturnIndexSeries,
    build_reference_index_result,
    comparison_scope_from_daily_values,
)
from diepi.backtest.data.result_storage import ResultStorage
from diepi.backtest.engine.portfolio_engine import PortfolioResult
from diepi.backtest.result_contract import (
    ActualInterval,
    DataCoverage,
    ResultAssumption,
    ResultContract,
    ResultReason,
    ResultStatus,
)


def _scope():
    return ComparisonScope(("20240102", "20240103"))


def _spec():
    return ReferenceIndexSpec(
        code="000300.SH",
        source_id="fixture:index-total-return",
        source_version="snapshot-2024-01",
    )


def _series(scope=None, spec=None):
    scope = _scope() if scope is None else scope
    spec = _spec() if spec is None else spec
    return TotalReturnIndexSeries(
        spec=spec,
        scope=scope,
        base_observation_id="20240101",
        base_level=100.0,
        levels=(110.0, 121.0),
    )


def _success_bundle(scope=None):
    scope = _scope() if scope is None else scope
    result = ReferenceIndexResult.from_series(_series(scope=scope))
    return ComparisonBundle(
        scope=scope,
        reference_index_total_return=result,
    )


def _daily_values(observation_ids=("20240102", "20240103")):
    frame = pd.DataFrame(
        {
            "cash": [1_000_000.0] * len(observation_ids),
            "market_value": [0.0] * len(observation_ids),
            "total_value": [1_100_000.0, 1_300_000.0][
                : len(observation_ids)
            ],
        },
        index=pd.to_datetime(list(observation_ids), format="%Y%m%d"),
    )
    frame.index.name = "date"
    return frame


def _success_contract():
    return ResultContract(
        status=ResultStatus.SUCCESS,
        assumptions=(
            ResultAssumption("data_coverage.unit", "portfolio_trade_day"),
        ),
        actual_interval=ActualInterval("2024-01-02", "2024-01-03"),
        data_coverage=DataCoverage(2, 2, 1.0),
    )


def _portfolio_result(*, bundle=None, contract=None, daily_values=None):
    return PortfolioResult(
        start_date="20240102",
        end_date="20240103",
        initial_cash=1_000_000.0,
        final_value=1_300_000.0,
        total_return=0.30,
        annual_return=1.0,
        max_drawdown=0.02,
        trade_count=1,
        sharpe_ratio=1.5,
        benchmark_code="000300.SH",
        benchmark_return=0.99,
        excess_return=-0.69,
        daily_values=(
            _daily_values() if daily_values is None else daily_values
        ),
        result_contract=(
            _success_contract() if contract is None else contract
        ),
        comparisons=_success_bundle() if bundle is None else bundle,
    )


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(ResultStorage, "RESULTS_DIR", str(tmp_path))
    from diepi.backtest.data.report_generator import ReportGenerator

    monkeypatch.setattr(
        ReportGenerator,
        "generate",
        staticmethod(lambda *args, **kwargs: None),
    )
    return tmp_path


def _read_meta(folder):
    return json.loads((Path(folder) / "meta.json").read_text(encoding="utf-8"))


def _write_meta(folder, meta):
    (Path(folder) / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )


def test_scope_helper_uses_every_actual_index_observation_in_order():
    frame = pd.DataFrame(
        index=pd.to_datetime(["2024-01-02", "2024-01-04"])
    )

    scope = comparison_scope_from_daily_values(frame)

    assert scope.observation_ids == ("20240102", "20240104")


def test_scope_helper_accepts_only_exact_string_dates():
    frame = pd.DataFrame(index=pd.Index(["20240102", "20240103"]))

    assert comparison_scope_from_daily_values(frame) == _scope()

    frame.index = pd.Index(["2024-01-02", "2024-01-03"])
    with pytest.raises(ValueError, match="exact YYYYMMDD"):
        comparison_scope_from_daily_values(frame)


@pytest.mark.parametrize(
    "index, error, match",
    [
        (pd.RangeIndex(2), TypeError, "daily_values.index"),
        (pd.DatetimeIndex([]), ValueError, "completed observations"),
        (
            pd.to_datetime(["2024-01-02", "2024-01-02"]),
            ValueError,
            "unique",
        ),
        (
            pd.to_datetime(["2024-01-03", "2024-01-02"]),
            ValueError,
            "strictly increasing",
        ),
        (
            pd.DatetimeIndex(
                [pd.Timestamp("2024-01-02 10:00"), pd.Timestamp("2024-01-03")]
            ),
            ValueError,
            "whole day",
        ),
        (
            pd.DatetimeIndex(["2024-01-02", "2024-01-03"], tz="UTC"),
            ValueError,
            "timezone-naive",
        ),
        (
            pd.DatetimeIndex(["2024-01-02", pd.NaT]),
            ValueError,
            "NaT",
        ),
    ],
)
def test_scope_helper_rejects_ambiguous_or_damaged_indices(
    index, error, match
):
    frame = pd.DataFrame(index=index)

    with pytest.raises(error, match=match):
        comparison_scope_from_daily_values(frame)


class _ReturningProvider:
    def __init__(self, value):
        self.value = value

    def get_total_return_series(self, spec, scope):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


def test_reference_orchestration_preserves_prior_base_and_first_day_move():
    series = _series()

    result = build_reference_index_result(
        series.spec, _ReturningProvider(series), series.scope
    )

    assert result.status is ComparisonStatus.SUCCESS
    assert result.series.base_observation_id == "20240101"
    assert result.total_return == pytest.approx(0.21)


@pytest.mark.parametrize(
    "error, status",
    [
        (
            ReferenceIndexUnavailableError("NO_LANE", "lane unavailable"),
            ComparisonStatus.UNAVAILABLE,
        ),
        (
            ReferenceIndexPartialError("MISSING_DAY", "20240103 missing"),
            ComparisonStatus.PARTIAL,
        ),
        (
            ReferenceIndexInvalidError("BAD_LEVEL", "level is invalid"),
            ComparisonStatus.INVALID,
        ),
    ],
)
def test_reference_orchestration_maps_structured_provider_failures(
    error, status
):
    result = build_reference_index_result(
        _spec(), _ReturningProvider(error), _scope()
    )

    assert result.status is status
    assert result.reason == error.reason
    assert result.total_return is None


@pytest.mark.parametrize(
    "error",
    [RuntimeError("disk read failed"), ValueError("bad\x00provider value")],
)
def test_reference_orchestration_maps_unexpected_failures_without_raising(
    error,
):
    result = build_reference_index_result(
        _spec(), _ReturningProvider(error), _scope()
    )

    assert result.status is ComparisonStatus.FAILED
    assert result.reason.code == "REFERENCE_TOTAL_RETURN_PROVIDER_FAILED"
    assert type(error).__name__ in result.reason.message
    assert not any(ord(character) < 32 for character in result.reason.message)
    assert result.total_return is None


@pytest.mark.parametrize("provider_value", [object(), None])
def test_reference_orchestration_maps_invalid_provider_return_to_failed(
    provider_value,
):
    result = build_reference_index_result(
        _spec(), _ReturningProvider(provider_value), _scope()
    )

    assert result.status is ComparisonStatus.FAILED
    assert result.reason.code == "REFERENCE_TOTAL_RETURN_PROVIDER_FAILED"


def test_reference_orchestration_maps_wrong_series_scope_to_failed():
    wrong_scope = ComparisonScope(("20240102", "20240104"))
    result = build_reference_index_result(
        _spec(), _ReturningProvider(_series(scope=wrong_scope)), _scope()
    )

    assert result.status is ComparisonStatus.FAILED
    assert "different spec or scope" in result.reason.message


def test_reference_orchestration_maps_wrong_series_spec_to_failed():
    wrong_spec = ReferenceIndexSpec(
        code="000905.SH",
        source_id="fixture:other-total-return",
        source_version="snapshot-2024-01",
    )
    result = build_reference_index_result(
        _spec(), _ReturningProvider(_series(spec=wrong_spec)), _scope()
    )

    assert result.status is ComparisonStatus.FAILED
    assert "different spec or scope" in result.reason.message


def test_bundle_strict_decoder_round_trips_and_rejects_derived_corruption():
    bundle = _success_bundle()
    payload = json.loads(bundle.to_json())

    restored = ComparisonBundle.from_dict(payload)

    assert restored == bundle
    assert restored is not bundle

    payload["reference_index_total_return"]["series"]["normalized_nav"][0] = 0.0
    with pytest.raises(ValueError, match="redundant fields"):
        ComparisonBundle.from_dict(payload)


def test_reference_excess_requires_all_three_success_and_scope_gates():
    result = _portfolio_result()

    assert result.reference_total_return_excess == pytest.approx(0.09)
    assert result.to_dict()["reference_total_return_excess"] == pytest.approx(9.0)

    partial_contract = ResultContract(
        status=ResultStatus.PARTIAL,
        reason=ResultReason("MISSING_DAY", "one strategy day is missing"),
        actual_interval=ActualInterval("2024-01-02", "2024-01-03"),
        data_coverage=DataCoverage(3, 2, 2.0 / 3.0),
    )
    result.result_contract = partial_contract
    assert result.reference_total_return_excess is None

    result.result_contract = _success_contract()
    result.daily_values = _daily_values(("20240102",))
    assert result.reference_total_return_excess is None


def test_reference_excess_rejects_same_length_different_date_scope():
    result = _portfolio_result(
        daily_values=_daily_values(("20240102", "20240104")),
        contract=ResultContract(
            status=ResultStatus.SUCCESS,
            assumptions=(
                ResultAssumption(
                    "data_coverage.unit", "portfolio_trade_day"
                ),
            ),
            actual_interval=ActualInterval("2024-01-02", "2024-01-04"),
            data_coverage=DataCoverage(2, 2, 1.0),
        ),
    )

    assert result.reference_total_return_excess is None


def _non_success_strategy_contract(status):
    if status is ResultStatus.PARTIAL:
        return ResultContract(
            status=status,
            reason=ResultReason("NOT_COMPLETE", "strategy run is partial"),
            actual_interval=ActualInterval("2024-01-02", "2024-01-03"),
            data_coverage=DataCoverage(3, 2, 2.0 / 3.0),
        )
    return ResultContract(
        status=status,
        reason=ResultReason("NOT_SUCCESS", "strategy run did not succeed"),
    )


@pytest.mark.parametrize(
    "strategy_status",
    [
        ResultStatus.PARTIAL,
        ResultStatus.INVALID,
        ResultStatus.FAILED,
        ResultStatus.CANCELED,
    ],
)
@pytest.mark.parametrize(
    "reference_status",
    [
        ComparisonStatus.UNAVAILABLE,
        ComparisonStatus.PARTIAL,
        ComparisonStatus.INVALID,
        ComparisonStatus.FAILED,
    ],
)
def test_non_success_strategy_and_reference_status_cartesian_gate(
    strategy_status, reference_status
):
    scope = _scope()
    bundle = ComparisonBundle(
        scope=scope,
        reference_index_total_return=ReferenceIndexResult(
            status=reference_status,
            spec=_spec(),
            scope=scope,
            reason=ComparisonReason(
                "REFERENCE_NOT_SUCCESS",
                "reference leg did not succeed",
            ),
        ),
    )

    result = _portfolio_result(
        bundle=bundle,
        contract=_non_success_strategy_contract(strategy_status),
    )
    assert result.reference_total_return_excess is None


@pytest.mark.parametrize(
    "contract",
    [
        ResultContract(
            status=ResultStatus.SUCCESS,
            actual_interval=ActualInterval("2024-01-02", "2024-01-03"),
            data_coverage=DataCoverage(2, 2, 1.0),
        ),
        ResultContract(
            status=ResultStatus.SUCCESS,
            assumptions=(
                ResultAssumption("data_coverage.unit", "symbol_trade_day"),
            ),
            actual_interval=ActualInterval("2024-01-02", "2024-01-03"),
            data_coverage=DataCoverage(2, 2, 1.0),
        ),
        ResultContract(
            status=ResultStatus.SUCCESS,
            assumptions=(
                ResultAssumption(
                    "data_coverage.unit", "portfolio_trade_day"
                ),
            ),
            actual_interval=ActualInterval("2024-01-01", "2024-01-03"),
            data_coverage=DataCoverage(2, 2, 1.0),
        ),
        ResultContract(
            status=ResultStatus.SUCCESS,
            assumptions=(
                ResultAssumption(
                    "data_coverage.unit", "portfolio_trade_day"
                ),
            ),
            actual_interval=ActualInterval("2024-01-02", "2024-01-03"),
            data_coverage=DataCoverage(3, 3, 1.0),
        ),
    ],
    ids=[
        "missing-coverage-unit",
        "wrong-coverage-unit",
        "interval-mismatch",
        "observation-count-mismatch",
    ],
)
def test_success_contract_metadata_must_match_actual_daily_scope(contract):
    result = _portfolio_result(contract=contract)

    assert result.reference_total_return_excess is None


def test_failed_reference_leg_is_none_not_zero():
    scope = _scope()
    failed_leg = ReferenceIndexResult(
        status=ComparisonStatus.UNAVAILABLE,
        spec=_spec(),
        scope=scope,
        reason=ComparisonReason("NO_LANE", "total-return lane unavailable"),
    )
    bundle = ComparisonBundle(
        scope=scope,
        reference_index_total_return=failed_leg,
    )
    result = _portfolio_result(bundle=bundle)

    assert result.total_return == 0.30
    assert result.reference_total_return_excess is None
    assert result.to_dict()["reference_total_return_excess"] is None


def test_legacy_benchmark_fields_are_retained_but_never_inferred():
    result = _portfolio_result()

    assert result.benchmark_return == 0.99
    assert result.excess_return == -0.69
    assert result.reference_total_return_excess == pytest.approx(0.09)

    result.comparisons = None
    assert result.reference_total_return_excess is None
    assert result.to_dict()["benchmark_return"] == 99.0
    assert result.to_dict()["excess_return"] == -69.0


def test_new_comparisons_field_preserves_old_positional_error_slot():
    result = PortfolioResult(
        "20240102",
        "20240103",
        100.0,
        100.0,
        0.0,
        0.0,
        0.0,
        0,
        None,
        0.0,
        0.0,
        None,
        "",
        0.0,
        0.0,
        pd.DataFrame(),
        [],
        [],
        {},
        None,
        None,
        "legacy error",
    )

    assert result.error == "legacy error"
    assert result.comparisons is None


def test_result_storage_round_trips_comparisons_and_exact_excess(
    isolated_storage,
):
    original = _portfolio_result()

    folder = ResultStorage.save(original, {"frequency": "daily"}, "pass")
    meta = _read_meta(folder)
    restored, config, code = ResultStorage.load(folder)

    assert meta["comparisons"] == original.comparisons.to_dict()
    assert meta["reference_total_return_excess"] == pytest.approx(0.09)
    assert restored.comparisons == original.comparisons
    assert restored.comparisons is not original.comparisons
    assert restored.reference_total_return_excess == pytest.approx(0.09)
    assert restored.benchmark_return == 0.99
    assert restored.excess_return == -0.69
    assert config == {"frequency": "daily"}
    assert code == "pass"


def test_result_storage_persists_failed_reference_as_null_not_zero(
    isolated_storage,
):
    scope = _scope()
    bundle = ComparisonBundle(
        scope=scope,
        reference_index_total_return=ReferenceIndexResult(
            status=ComparisonStatus.PARTIAL,
            spec=_spec(),
            scope=scope,
            reason=ComparisonReason("MISSING_DAY", "20240103 is missing"),
        ),
    )

    folder = ResultStorage.save(_portfolio_result(bundle=bundle), {}, "")
    meta = _read_meta(folder)
    restored, _, _ = ResultStorage.load(folder)

    assert meta["reference_total_return_excess"] is None
    assert restored.reference_total_return_excess is None


def test_result_storage_loads_legacy_artifact_without_comparison_fields(
    isolated_storage,
):
    result = _portfolio_result()
    result.comparisons = None
    folder = ResultStorage.save(result, {}, "")
    meta = _read_meta(folder)
    del meta["comparisons"]
    del meta["reference_total_return_excess"]
    meta["version"] = "1.1"
    _write_meta(folder, meta)

    restored, _, _ = ResultStorage.load(folder)

    assert restored.comparisons is None
    assert restored.reference_total_return_excess is None


def test_result_storage_rejects_corrupt_redundant_excess(isolated_storage):
    folder = ResultStorage.save(_portfolio_result(), {}, "")
    meta = _read_meta(folder)
    meta["reference_total_return_excess"] = 0.0
    _write_meta(folder, meta)

    with pytest.raises(ValueError, match="does not agree"):
        ResultStorage.load(folder)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda contract: contract["actual_interval"].update(
            {"start_date": "2024-01-01"}
        ),
        lambda contract: contract["data_coverage"].update(
            {
                "expected_observations": 3,
                "actual_observations": 3,
                "missing_observations": 0,
                "ratio": 1.0,
            }
        ),
        lambda contract: contract["assumptions"][0].update(
            {"value": "symbol_trade_day"}
        ),
    ],
    ids=["interval", "coverage-count", "coverage-unit"],
)
def test_result_storage_rejects_success_contract_scope_corruption(
    isolated_storage, mutate
):
    folder = ResultStorage.save(_portfolio_result(), {}, "")
    meta = _read_meta(folder)
    mutate(meta["result_contract"])
    _write_meta(folder, meta)

    with pytest.raises(ValueError, match="does not agree"):
        ResultStorage.load(folder)


def test_result_storage_rejects_corrupt_comparison_scope_hash(
    isolated_storage,
):
    folder = ResultStorage.save(_portfolio_result(), {}, "")
    meta = _read_meta(folder)
    meta["comparisons"]["scope"]["scope_hash"] = "sha256:" + "0" * 64
    _write_meta(folder, meta)

    with pytest.raises(ValueError, match="derived fields"):
        ResultStorage.load(folder)


def test_result_storage_rejects_foreign_comparison_shape_and_cleans(
    isolated_storage,
):
    result = _portfolio_result()
    result.comparisons = result.comparisons.to_dict()

    with pytest.raises(TypeError, match="exactly ComparisonBundle"):
        ResultStorage.save(result, {}, "")

    assert list(isolated_storage.iterdir()) == []


def test_result_storage_rejects_nonfinite_json_constant_on_load(
    isolated_storage,
):
    folder = ResultStorage.save(_portfolio_result(), {}, "")
    meta = _read_meta(folder)
    meta["total_return"] = float("nan")
    (Path(folder) / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-finite JSON constant"):
        ResultStorage.load(folder)


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_result_storage_rejects_nonfinite_config_json_on_load(
    isolated_storage, constant
):
    folder = ResultStorage.save(_portfolio_result(), {}, "")
    (Path(folder) / "config.json").write_text(
        '{{"risk_free_rate": {}}}'.format(constant),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="non-finite JSON constant"):
        ResultStorage.load(folder)


def test_result_storage_rejects_non_object_config_root(isolated_storage):
    folder = ResultStorage.save(_portfolio_result(), {}, "")
    (Path(folder) / "config.json").write_text("[]", encoding="utf-8")

    with pytest.raises(TypeError, match="config.json root"):
        ResultStorage.load(folder)
