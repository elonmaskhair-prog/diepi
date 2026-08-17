"""Pure contract tests for explicit reference total-return comparisons."""

import json
import os
from pathlib import Path
import subprocess
import sys
from dataclasses import FrozenInstanceError

import pandas as pd
import pytest

from diepi.backtest.comparison import (
    ComparisonBundle,
    ComparisonScope,
    ComparisonStatus,
    ReferenceIndexInvalidError,
    ReferenceIndexPartialError,
    ReferenceIndexResult,
    ReferenceIndexSpec,
    ReferenceIndexUnavailableError,
    TotalReturnIndexSeries,
)
from diepi.backtest.data.index_provider import IndexProvider


@pytest.fixture
def provider():
    IndexProvider.reset_instance()
    result = IndexProvider()
    yield result
    IndexProvider.reset_instance()


@pytest.fixture
def scope():
    return ComparisonScope(("20240102", "20240103"))


@pytest.fixture
def spec():
    return ReferenceIndexSpec(
        code="000300.SH",
        source_id="fixture:index-total-return",
        source_version="snapshot-2024-01",
    )


def _frame(
    *,
    dates=("20240101", "20240102", "20240103"),
    levels=(100.0, 110.0, 121.0),
    closes=(10.0, 10.1, 10.2),
    include_total_return=True,
):
    values = {
        "trade_date": list(dates),
        "close": list(closes),
    }
    if include_total_return:
        values["total_return_close"] = list(levels)
    return pd.DataFrame(values).set_index("trade_date")


def _install_frame(monkeypatch, provider, frame):
    monkeypatch.setattr(
        provider,
        "get_index_daily",
        lambda code, start=None, end=None: frame.copy(),
    )


def test_scope_hash_is_canonical_and_json_safe(scope):
    same = ComparisonScope(("20240102", "20240103"))

    assert scope.scope_hash == same.scope_hash
    assert scope.scope_hash.startswith("sha256:")
    assert len(scope.scope_hash) == len("sha256:") + 64
    assert json.loads(scope.to_json()) == scope.to_dict()
    assert scope.start_date == "20240102"
    assert scope.end_date == "20240103"


@pytest.mark.parametrize(
    "observation_ids, match",
    [
        (("20240102", "20240102"), "unique"),
        (("20240103", "20240102"), "strictly increasing"),
        (("20240230",), "valid calendar date"),
        ((), "must not be empty"),
    ],
)
def test_scope_rejects_noncanonical_observation_sets(observation_ids, match):
    with pytest.raises(ValueError, match=match):
        ComparisonScope(observation_ids)


def test_scope_hash_is_stable_across_python_hash_seeds():
    root = Path(__file__).resolve().parents[2]
    script = (
        "from diepi.backtest.comparison import ComparisonScope; "
        "print(ComparisonScope(('20240102','20240103')).scope_hash)"
    )
    outputs = []
    for seed in ("1", "987654"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(root),
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        outputs.append(result.stdout.strip())

    assert outputs[0] == outputs[1]


def test_reference_spec_forbids_price_close_impersonation():
    with pytest.raises(ValueError, match="total_return_close"):
        ReferenceIndexSpec(
            code="000300.SH",
            source_id="fixture",
            source_version="v1",
            value_column="close",
        )


def test_first_in_scope_move_is_retained(
    provider, monkeypatch, spec, scope
):
    _install_frame(monkeypatch, provider, _frame())

    series = provider.get_total_return_series(spec, scope)

    assert series.base_observation_id == "20240101"
    assert series.base_level == pytest.approx(100.0)
    assert series.levels == pytest.approx((110.0, 121.0))
    assert series.normalized_nav == pytest.approx((1.10, 1.21))
    assert series.total_return == pytest.approx(0.21)


def test_datetime_index_is_canonicalized_without_losing_exact_scope(
    provider, monkeypatch, spec, scope
):
    frame = pd.DataFrame(
        {
            "close": [3000.0, 1.0, 2.0],
            "total_return_close": [100.0, 110.0, 121.0],
        },
        index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"]),
    )
    _install_frame(monkeypatch, provider, frame)

    series = provider.get_total_return_series(spec, scope)

    assert series.base_observation_id == "20240101"
    assert series.scope.observation_ids == ("20240102", "20240103")
    assert series.total_return == pytest.approx(0.21)


def test_price_close_changes_do_not_affect_total_return_series(
    provider, monkeypatch, spec, scope
):
    first = _frame(closes=(10.0, 20.0, 30.0))
    _install_frame(monkeypatch, provider, first)
    left = provider.get_total_return_series(spec, scope)

    second = _frame(closes=(900.0, 100.0, 1.0))
    _install_frame(monkeypatch, provider, second)
    right = provider.get_total_return_series(spec, scope)

    assert left == right
    assert left.total_return == pytest.approx(0.21)


def test_price_only_data_never_falls_back_to_close(
    provider, monkeypatch, spec, scope
):
    _install_frame(
        monkeypatch, provider, _frame(include_total_return=False)
    )

    with pytest.raises(ReferenceIndexUnavailableError) as captured:
        provider.get_total_return_series(spec, scope)

    result = ReferenceIndexResult.from_error(
        spec, scope, captured.value
    )
    assert result.status is ComparisonStatus.UNAVAILABLE
    assert result.total_return is None
    assert result.reason.code == "REFERENCE_TOTAL_RETURN_COLUMN_MISSING"


def test_missing_scope_date_is_partial(
    provider, monkeypatch, spec, scope
):
    frame = _frame(
        dates=("20240101", "20240103"),
        levels=(100.0, 121.0),
        closes=(10.0, 10.2),
    )
    _install_frame(monkeypatch, provider, frame)

    with pytest.raises(ReferenceIndexPartialError) as captured:
        provider.get_total_return_series(spec, scope)

    result = ReferenceIndexResult.from_error(spec, scope, captured.value)
    assert result.status is ComparisonStatus.PARTIAL
    assert "20240102" in result.reason.message
    assert result.to_dict()["series"] is None


def test_missing_prior_session_base_is_partial(
    provider, monkeypatch, spec, scope
):
    frame = _frame(
        dates=("20240102", "20240103"),
        levels=(110.0, 121.0),
        closes=(10.1, 10.2),
    )
    _install_frame(monkeypatch, provider, frame)

    with pytest.raises(
        ReferenceIndexPartialError, match="prior-session"
    ):
        provider.get_total_return_series(spec, scope)


def test_duplicate_dates_are_invalid(
    provider, monkeypatch, spec, scope
):
    frame = _frame(
        dates=("20240101", "20240102", "20240102", "20240103"),
        levels=(100.0, 109.0, 110.0, 121.0),
        closes=(10.0, 10.1, 10.1, 10.2),
    )
    _install_frame(monkeypatch, provider, frame)

    with pytest.raises(ReferenceIndexInvalidError, match="duplicate"):
        provider.get_total_return_series(spec, scope)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
def test_nonfinite_or_nonpositive_levels_are_invalid(
    provider, monkeypatch, spec, scope, bad
):
    frame = _frame()
    frame.loc["20240102", "total_return_close"] = bad
    _install_frame(monkeypatch, provider, frame)

    with pytest.raises(
        ReferenceIndexInvalidError, match="finite and positive"
    ) as captured:
        provider.get_total_return_series(spec, scope)

    result = ReferenceIndexResult.from_error(spec, scope, captured.value)
    assert result.status is ComparisonStatus.INVALID
    assert result.total_return is None


def test_success_result_and_bundle_are_immutable_json_safe(
    provider, monkeypatch, spec, scope
):
    _install_frame(monkeypatch, provider, _frame())
    series = provider.get_total_return_series(spec, scope)
    result = ReferenceIndexResult.from_series(series)
    bundle = ComparisonBundle(
        scope=scope,
        reference_index_total_return=result,
    )

    assert result.status is ComparisonStatus.SUCCESS
    assert result.total_return == pytest.approx(0.21)
    assert json.loads(result.to_json())["status"] == "SUCCESS"
    assert json.loads(bundle.to_json())["scope"]["scope_hash"] == (
        scope.scope_hash
    )
    with pytest.raises(FrozenInstanceError):
        series.levels = (999.0,)


def test_series_constructor_rejects_json_unsafe_values(spec, scope):
    with pytest.raises(ValueError, match="finite and positive"):
        TotalReturnIndexSeries(
            spec=spec,
            scope=scope,
            base_observation_id="20240101",
            base_level=100.0,
            levels=(110.0, float("nan")),
        )
