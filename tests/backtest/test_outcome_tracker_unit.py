"""Tests for runtime result evidence; no legacy inference is allowed."""

from datetime import datetime, timezone

import pytest

from diepi.backtest.outcome import OutcomeTracker
from diepi.backtest.result_contract import ResultStatus


def test_complete_nonempty_run_is_success_and_rankable():
    tracker = OutcomeTracker(
        expected_observation_ids=("A|2024-01-02", "A|2024-01-03"),
        coverage_unit="symbol_date",
    )
    tracker.observe("A|2024-01-02", "20240102")
    tracker.observe("A|2024-01-03", "2024-01-03")

    result = tracker.finalize_completed()

    assert result.status is ResultStatus.SUCCESS
    assert result.is_rankable
    assert result.actual_interval.start_date == "2024-01-02"
    assert result.data_coverage.ratio == 1.0


def test_incomplete_run_is_partial_not_fake_success():
    tracker = OutcomeTracker(
        expected_observations=3, coverage_unit="symbol_date"
    )
    tracker.observe("A|1", "2024-01-03")

    result = tracker.finalize_completed()

    assert result.status is ResultStatus.PARTIAL
    assert result.reason.code == "INCOMPLETE_DATA_COVERAGE"
    assert not result.is_rankable


def test_zero_expected_scope_is_invalid():
    result = OutcomeTracker(
        expected_observations=0, coverage_unit="symbol_date"
    ).finalize_completed()

    assert result.status is ResultStatus.INVALID
    assert result.reason.code == "NO_EXPECTED_OBSERVATIONS"


def test_zero_actual_market_data_is_invalid():
    result = OutcomeTracker(
        expected_observations=2, coverage_unit="symbol_date"
    ).finalize_completed()

    assert result.status is ResultStatus.INVALID
    assert result.reason.code == "MARKET_DATA_UNAVAILABLE"
    assert result.data_coverage.actual_observations == 0


def test_failed_run_retains_actual_interval_and_coverage():
    tracker = OutcomeTracker(
        expected_observations=5, coverage_unit="symbol_date"
    )
    tracker.observe("A|1", "2024-01-02")
    tracker.observe("A|2", "2024-01-04")

    result = tracker.finalize(
        ResultStatus.FAILED,
        reason_code="ENGINE_RUNTIME_ERROR",
        reason_message="Engine failed while processing a bar",
    )

    assert result.status is ResultStatus.FAILED
    assert result.actual_interval.end_date == "2024-01-04"
    assert result.data_coverage.ratio == pytest.approx(0.4)


def test_canceled_run_with_no_known_expectation_can_still_be_explicit():
    tracker = OutcomeTracker()
    result = tracker.finalize(
        ResultStatus.CANCELED,
        reason_code="STOP_REQUESTED",
        reason_message="The user requested cancellation",
    )

    assert result.status is ResultStatus.CANCELED
    assert result.data_coverage is None


def test_observed_data_requires_a_declared_scope_before_finalization():
    tracker = OutcomeTracker(coverage_unit="symbol_date")
    tracker.observe("A|1", "2024-01-02")

    with pytest.raises(RuntimeError, match="expected observations"):
        tracker.finalize(
            ResultStatus.FAILED,
            reason_code="ENGINE_RUNTIME_ERROR",
            reason_message="Engine failed",
        )


def test_duplicate_observation_never_inflates_coverage():
    tracker = OutcomeTracker(
        expected_observations=2, coverage_unit="symbol_date"
    )
    tracker.observe("same", "2024-01-02")

    with pytest.raises(ValueError, match="duplicate observation"):
        tracker.observe("same", "2024-01-02")
    assert tracker.actual_observations == 1


def test_observations_cannot_exceed_declared_scope():
    tracker = OutcomeTracker(
        expected_observations=1, coverage_unit="symbol_date"
    )
    tracker.observe("one", "2024-01-02")
    with pytest.raises(ValueError, match="exceed expected"):
        tracker.observe("two", "2024-01-03")


def test_warning_and_assumption_order_is_deterministic():
    tracker = OutcomeTracker(
        expected_observation_ids=("one",), coverage_unit="symbol_date"
    )
    tracker.add_warning("Z_WARNING", "Second alphabetically")
    tracker.add_warning("A_WARNING", "First alphabetically")
    tracker.add_assumption("z.value", "last")
    tracker.add_assumption("a.value", "first")
    tracker.observe("one", "2024-01-02")

    result = tracker.finalize_completed()

    assert [item.code for item in result.warnings] == ["A_WARNING", "Z_WARNING"]
    assert [item.key for item in result.assumptions] == [
        "a.value",
        "data_coverage.unit",
        "z.value",
    ]


def test_tracker_cannot_be_reused_after_finalization():
    tracker = OutcomeTracker(
        expected_observation_ids=("one",), coverage_unit="symbol_date"
    )
    tracker.observe("one", "2024-01-02")
    tracker.finalize_completed()

    with pytest.raises(RuntimeError, match="finalized"):
        tracker.observe("two", "2024-01-03")


def test_equal_count_without_exact_expected_ids_cannot_prove_success():
    tracker = OutcomeTracker(
        expected_observations=2, coverage_unit="symbol_date"
    )
    tracker.observe("wrong-a", "2024-01-02")
    tracker.observe("wrong-b", "2024-01-03")

    with pytest.raises(RuntimeError, match="explicit expected observation ID"):
        tracker.finalize_completed()


def test_wrong_equal_sized_observation_set_is_rejected():
    tracker = OutcomeTracker(coverage_unit="symbol_date")
    tracker.observe("wrong-a", "2024-01-02")
    tracker.observe("wrong-b", "2024-01-03")

    with pytest.raises(ValueError, match="outside the expected scope"):
        tracker.set_expected_scope(("expected-a", "expected-b"))


def test_expected_scope_is_canonical_and_exposed_deterministically():
    tracker = OutcomeTracker(
        expected_observation_ids=(" B ", "A"),
        coverage_unit="symbol_date",
    )

    assert tracker.expected_observation_ids == ("A", "B")
    assert tracker.expected_observations == 2


def test_unexpected_observation_is_rejected_at_collection_boundary():
    tracker = OutcomeTracker(
        expected_observation_ids=("expected",),
        coverage_unit="symbol_date",
    )

    with pytest.raises(ValueError, match="outside expected scope"):
        tracker.observe("wrong", "2024-01-02")
    assert tracker.actual_observations == 0


def test_observation_ids_are_stripped_before_uniqueness_check():
    tracker = OutcomeTracker(
        expected_observation_ids=("same", "other"),
        coverage_unit="symbol_date",
    )
    tracker.observe(" same ", "2024-01-02")

    with pytest.raises(ValueError, match="duplicate observation"):
        tracker.observe("same", "2024-01-02")
    assert tracker.actual_observations == 1


@pytest.mark.parametrize("observation_id", ["", " ", "\t\n"])
def test_blank_observation_ids_are_rejected_after_stripping(observation_id):
    tracker = OutcomeTracker(
        expected_observations=1, coverage_unit="symbol_date"
    )

    with pytest.raises(ValueError, match="after stripping"):
        tracker.observe(observation_id, "2024-01-02")


def test_expected_ids_are_unique_after_stripping():
    with pytest.raises(ValueError, match="duplicate expected"):
        OutcomeTracker(
            expected_observation_ids=("same", " same "),
            coverage_unit="symbol_date",
        )


@pytest.mark.parametrize(
    "value",
    [
        "2024-W01-1",
        "2024-1-02",
        "2024012",
        "2024-001",
        " 2024-01-02",
        "2024-01-02 ",
    ],
)
def test_observation_date_rejects_non_declared_string_forms(value):
    tracker = OutcomeTracker(
        expected_observation_ids=("one",), coverage_unit="symbol_date"
    )

    with pytest.raises(ValueError, match="YYYYMMDD or YYYY-MM-DD"):
        tracker.observe("one", value)


def test_timezone_aware_datetime_is_rejected_without_silent_date_loss():
    tracker = OutcomeTracker(
        expected_observation_ids=("one",), coverage_unit="symbol_date"
    )

    with pytest.raises(ValueError, match="timezone-naive"):
        tracker.observe(
            "one", datetime(2024, 1, 2, 23, tzinfo=timezone.utc)
        )


def test_naive_datetime_remains_supported_and_canonicalized():
    tracker = OutcomeTracker(
        expected_observation_ids=("one",), coverage_unit="symbol_date"
    )
    tracker.observe("one", datetime(2024, 1, 2, 23, 59))

    result = tracker.finalize_completed()

    assert result.actual_interval.start_date == "2024-01-02"


def test_coverage_requires_explicit_unit_assumption():
    tracker = OutcomeTracker(expected_observation_ids=("one",))
    tracker.observe("one", "2024-01-02")

    with pytest.raises(RuntimeError, match="coverage unit"):
        tracker.finalize_completed()


def test_coverage_unit_is_serialized_as_a_contract_assumption():
    tracker = OutcomeTracker(
        expected_observation_ids=("one",), coverage_unit="completed_bar"
    )
    tracker.observe("one", "2024-01-02")

    result = tracker.finalize_completed()

    assert ("data_coverage.unit", "completed_bar") in {
        (item.key, item.value) for item in result.assumptions
    }


def test_direct_success_requires_exact_expected_scope_not_only_counts():
    tracker = OutcomeTracker(
        expected_observations=1, coverage_unit="symbol_date"
    )
    tracker.observe("one", "2024-01-02")

    with pytest.raises(RuntimeError, match="explicit expected observation ID"):
        tracker.finalize(ResultStatus.SUCCESS)
