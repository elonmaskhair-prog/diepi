"""Pure synthetic tests for the DC-1 paired market-data contract."""

import copy
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from diepi.backtest.data.contract import (
    AmountUnit,
    CONTRACT_VERSION,
    ISSUE_CODES,
    MAX_ISSUE_SAMPLES,
    SCHEMA_VERSION,
    AlignedMarketData,
    DataContractError,
    DataQualityIssue,
    DataQualityReport,
    Frequency,
    PreCloseSource,
    PriceSpace,
    TrackRole,
    inspect_pair,
    validate_and_align_pair,
)


SYMBOL = "000001.SZ"


def _daily_pair(periods=3):
    dates = pd.Index(
        pd.date_range("2024-01-02", periods=periods, freq="D").strftime("%Y%m%d"),
        name="trade_date",
    )
    base = np.arange(periods, dtype=float) * 0.1 + 10.0
    strategy = pd.DataFrame(
        {
            "open": base,
            "high": base + 0.5,
            "low": base - 0.5,
            "close": base + 0.2,
            "amount": np.arange(periods, dtype=float) + 1.0,
        },
        index=dates,
    )
    execution = pd.DataFrame(
        {
            "open": base,
            "high": base + 0.4,
            "low": base - 0.4,
            "close": base + 0.1,
            "pre_close": base - 0.1,
            "amount": np.arange(periods, dtype=float) + 10.0,
        },
        index=dates.copy(),
    )
    return strategy, execution


def _minute_pair(periods=3):
    times = pd.date_range("2024-01-02 09:30", periods=periods, freq="min")
    times.name = "trade_time"
    strategy, execution = _daily_pair(periods)
    strategy.index = times
    execution.index = times.copy()
    # A minute bar carries the previous *daily* close; it must not drift
    # between rows within one trading day.
    execution["pre_close"] = float(execution["pre_close"].iloc[0])
    strategy["pre_close"] = float(execution["pre_close"].iloc[0])
    return strategy, execution


def _validate(
        strategy, execution, frequency=Frequency.DAILY,
        pre_close_exempt_dates=()):
    amount_unit = (
        AmountUnit.THOUSAND_YUAN
        if frequency is Frequency.DAILY
        else AmountUnit.YUAN
    )
    return validate_and_align_pair(
        strategy,
        execution,
        symbol=SYMBOL,
        strategy_price_space=PriceSpace.HFQ,
        execution_price_space=PriceSpace.RAW,
        strategy_amount_unit=amount_unit,
        execution_amount_unit=amount_unit,
        frequency=frequency,
        pre_close_exempt_dates=pre_close_exempt_dates,
    )


def _capture_report(
        strategy, execution, frequency=Frequency.DAILY,
        pre_close_exempt_dates=()):
    with pytest.raises(DataContractError) as caught:
        _validate(
            strategy,
            execution,
            frequency,
            pre_close_exempt_dates=pre_close_exempt_dates,
        )
    return caught.value.report


def test_contract_enums_have_explicit_stable_values():
    assert TrackRole.STRATEGY.value == "strategy"
    assert TrackRole.EXECUTION.value == "execution"
    assert PriceSpace.RAW.value == "raw"
    assert PriceSpace.HFQ.value == "hfq"
    assert AmountUnit.YUAN.value == "yuan"
    assert AmountUnit.THOUSAND_YUAN.value == "thousand_yuan"
    assert Frequency.DAILY.value == "daily"
    assert Frequency.MINUTE.value == "minute"
    assert PreCloseSource.ABSENT.value == "absent"
    assert PreCloseSource.NATIVE.value == "native"
    assert PreCloseSource.SAME_LANE_DAILY.value == "same_lane_daily"
    assert (
        PreCloseSource.SAME_LANE_DAILY_WITH_SUSPENSION_FALLBACK.value
        == "same_lane_daily_with_suspension_fallback"
    )
    assert ISSUE_CODES == (
        "MISSING_DATASET",
        "READ_ERROR",
        "MISSING_COLUMN",
        "DUPLICATE_COLUMN",
        "DUPLICATE_KEY",
        "NON_MONOTONIC_KEY",
        "KEY_MISMATCH",
        "PRICE_SPACE_MISSING",
        "NONFINITE_VALUE",
        "NONPOSITIVE_PRICE",
        "OHLC_INCONSISTENT",
        "INVALID_PRE_CLOSE",
        "INVALID_AMOUNT",
        "FREQUENCY_DOWNGRADE",
        "UNSUPPORTED_INSTRUMENT",
        "MISSING_TIMESTAMP",
        "INVALID_TIMESTAMP",
        "SYMBOL_MISMATCH",
        "MISSING_ADJ_FACTOR",
        "DUPLICATE_ADJ_FACTOR",
        "NON_MONOTONIC_ADJ_FACTOR",
        "INVALID_ADJ_FACTOR",
        "ADJ_FACTOR_SCOPE_MISMATCH",
        "PRICE_IDENTITY_MISMATCH",
    )


def test_daily_success_returns_canonical_copies_and_scales_amount():
    strategy, execution = _daily_pair()
    strategy_before = strategy.copy(deep=True)
    execution_before = execution.copy(deep=True)

    aligned = _validate(strategy, execution)

    assert isinstance(aligned, AlignedMarketData)
    assert aligned.symbol == SYMBOL
    assert aligned.frequency is Frequency.DAILY
    assert aligned.strategy_price_space is PriceSpace.HFQ
    assert aligned.execution_price_space is PriceSpace.RAW
    assert aligned.strategy_amount_unit is AmountUnit.THOUSAND_YUAN
    assert aligned.execution_amount_unit is AmountUnit.THOUSAND_YUAN
    assert isinstance(aligned.strategy.index, pd.DatetimeIndex)
    assert isinstance(aligned.execution.index, pd.DatetimeIndex)
    assert aligned.strategy.index.name == "timestamp"
    assert aligned.execution.index.name == "timestamp"
    assert aligned.strategy.index.equals(aligned.execution.index)
    assert aligned.keys.names == ["symbol", "timestamp"]
    assert set(aligned.keys.get_level_values("symbol")) == {SYMBOL}
    assert aligned.strategy["amount"].tolist() == [1000.0, 2000.0, 3000.0]
    assert aligned.execution["amount"].tolist() == [10000.0, 11000.0, 12000.0]
    assert aligned.report.ok
    assert aligned.report.schema_version == SCHEMA_VERSION == 4
    assert aligned.report.status == "pass"
    assert aligned.report.aligned_rows == 3
    assert aligned.report.issues == ()
    assert aligned.strategy_pre_close_source is PreCloseSource.ABSENT
    assert aligned.execution_pre_close_source is PreCloseSource.NATIVE

    assert_frame_equal(strategy, strategy_before)
    assert_frame_equal(execution, execution_before)
    assert aligned.strategy is not strategy
    assert aligned.execution is not execution


def test_minute_success_uses_amount_multiplier_one_and_accepts_enum_strings():
    strategy, execution = _minute_pair()

    aligned = validate_and_align_pair(
        strategy,
        execution,
        symbol=" 000001.sz ",
        strategy_price_space="hfq",
        execution_price_space="raw",
        strategy_amount_unit="yuan",
        execution_amount_unit="yuan",
        frequency="minute",
    )

    assert aligned.symbol == SYMBOL
    assert aligned.frequency is Frequency.MINUTE
    assert aligned.strategy_amount_unit is AmountUnit.YUAN
    assert aligned.execution_amount_unit is AmountUnit.YUAN
    assert aligned.strategy["amount"].tolist() == [1.0, 2.0, 3.0]
    assert aligned.execution["amount"].tolist() == [10.0, 11.0, 12.0]
    assert aligned.strategy_pre_close_source is PreCloseSource.NATIVE
    assert aligned.execution_pre_close_source is PreCloseSource.NATIVE


def test_same_lane_daily_pre_close_provenance_is_explicit_and_serialized():
    strategy, execution = _minute_pair()

    aligned = validate_and_align_pair(
        strategy,
        execution,
        symbol=SYMBOL,
        strategy_price_space=PriceSpace.HFQ,
        execution_price_space=PriceSpace.RAW,
        strategy_amount_unit=AmountUnit.YUAN,
        execution_amount_unit=AmountUnit.YUAN,
        frequency=Frequency.MINUTE,
        strategy_pre_close_source=PreCloseSource.SAME_LANE_DAILY,
        execution_pre_close_source="same_lane_daily",
    )

    assert aligned.strategy_pre_close_source is PreCloseSource.SAME_LANE_DAILY
    assert aligned.execution_pre_close_source is PreCloseSource.SAME_LANE_DAILY
    payload = aligned.report.to_dict()
    assert payload["strategy_pre_close_source"] == "same_lane_daily"
    assert payload["execution_pre_close_source"] == "same_lane_daily"


@pytest.mark.parametrize(
    ("frequency", "source", "drop_column", "message"),
    [
        (Frequency.DAILY, "same_lane_daily", False, "only valid for minute"),
        (Frequency.MINUTE, "native", True, "requires a pre_close column"),
        (Frequency.MINUTE, "absent", False, "cannot be absent"),
        (Frequency.MINUTE, "other_lane", False, "strategy_pre_close_source"),
    ],
)
def test_pre_close_provenance_rejects_impossible_or_unknown_claims(
        frequency, source, drop_column, message):
    if frequency is Frequency.MINUTE:
        strategy, execution = _minute_pair()
    else:
        strategy, execution = _daily_pair()
        strategy["pre_close"] = [10.0] * len(strategy)
    if drop_column:
        strategy = strategy.drop(columns=["pre_close"])

    with pytest.raises(ValueError, match=message):
        validate_and_align_pair(
            strategy,
            execution,
            symbol=SYMBOL,
            strategy_price_space=PriceSpace.HFQ,
            execution_price_space=PriceSpace.RAW,
            strategy_amount_unit=(
                AmountUnit.YUAN
                if frequency is Frequency.MINUTE
                else AmountUnit.THOUSAND_YUAN
            ),
            execution_amount_unit=(
                AmountUnit.YUAN
                if frequency is Frequency.MINUTE
                else AmountUnit.THOUSAND_YUAN
            ),
            frequency=frequency,
            strategy_pre_close_source=source,
        )


def test_minute_date_column_is_auxiliary_and_matches_by_calendar_date():
    strategy, execution = _minute_pair()
    strategy["trade_date"] = [20240102] * len(strategy)
    execution["trade_date"] = ["20240102"] * len(execution)

    aligned = _validate(strategy, execution, Frequency.MINUTE)

    assert aligned.report.ok
    assert aligned.strategy.index[0] == pd.Timestamp("2024-01-02 09:30")
    assert aligned.execution["trade_date"].tolist() == ["20240102"] * 3


def test_amount_units_are_explicit_and_each_track_is_normalized_to_yuan():
    strategy, execution = _daily_pair()

    aligned = validate_and_align_pair(
        strategy,
        execution,
        symbol=SYMBOL,
        strategy_price_space=PriceSpace.HFQ,
        execution_price_space=PriceSpace.RAW,
        strategy_amount_unit=AmountUnit.YUAN,
        execution_amount_unit=AmountUnit.THOUSAND_YUAN,
        frequency=Frequency.DAILY,
    )

    assert aligned.strategy["amount"].tolist() == [1.0, 2.0, 3.0]
    assert aligned.execution["amount"].tolist() == [10000.0, 11000.0, 12000.0]
    assert aligned.report.strategy_amount_unit is AmountUnit.YUAN
    assert aligned.report.execution_amount_unit is AmountUnit.THOUSAND_YUAN


def test_minute_pre_close_must_be_constant_within_each_trading_day():
    strategy, execution = _minute_pair()
    execution.loc[execution.index[1], "pre_close"] += 0.01

    report = _capture_report(strategy, execution, Frequency.MINUTE)

    issue = next(
        issue
        for issue in report.issues
        if issue.code == "INVALID_PRE_CLOSE"
        and issue.track is TrackRole.EXECUTION
        and issue.field == "pre_close"
        and "constant" in issue.message
    )
    assert issue.count == 1
    assert issue.sample_keys == ("000001.SZ@2024-01-02T09:31:00",)


def test_explicit_daily_pre_close_exemptions_apply_to_both_tracks_and_are_audited():
    strategy, execution = _daily_pair()
    strategy["pre_close"] = [np.nan, 0.0, 10.0]
    execution["pre_close"] = [float("inf"), -1.0, 10.0]
    supplied = [
        "2024-01-03",
        datetime(2024, 1, 2),
        "20240103",
        date(2024, 1, 2),
    ]
    supplied_before = list(supplied)

    aligned = _validate(
        strategy,
        execution,
        pre_close_exempt_dates=supplied,
    )

    assert aligned.report.ok
    assert aligned.pre_close_exempt_dates == ("20240102", "20240103")
    assert aligned.report.pre_close_exempt_dates == ("20240102", "20240103")
    assert aligned.report.to_dict()["pre_close_exempt_dates"] == [
        "20240102",
        "20240103",
    ]
    assert pd.isna(aligned.strategy.iloc[0]["pre_close"])
    assert aligned.strategy.iloc[1]["pre_close"] == 0.0
    assert np.isinf(aligned.execution.iloc[0]["pre_close"])
    assert aligned.execution.iloc[1]["pre_close"] == -1.0
    assert supplied == supplied_before


@pytest.mark.parametrize("track", [TrackRole.STRATEGY, TrackRole.EXECUTION])
def test_pre_close_exemption_does_not_relax_non_exempt_dates(track):
    strategy, execution = _daily_pair()
    strategy["pre_close"] = [10.0, 10.0, 10.0]
    target = strategy if track is TrackRole.STRATEGY else execution
    target.loc[target.index[1], "pre_close"] = 0.0

    report = _capture_report(
        strategy,
        execution,
        pre_close_exempt_dates=["20240102"],
    )

    issue = next(
        issue for issue in report.issues
        if issue.code == "INVALID_PRE_CLOSE" and issue.track is track
    )
    assert issue.sample_keys == ("000001.SZ@2024-01-03",)
    assert report.pre_close_exempt_dates == ("20240102",)


def test_pre_close_exemption_never_substitutes_a_missing_required_column():
    strategy, execution = _daily_pair()
    execution = execution.drop(columns=["pre_close"])

    report = _capture_report(
        strategy,
        execution,
        pre_close_exempt_dates=["20240102", "20240103", "20240104"],
    )

    assert any(
        issue.code == "MISSING_COLUMN"
        and issue.track is TrackRole.EXECUTION
        and issue.field == "pre_close"
        for issue in report.issues
    )


def test_minute_pre_close_constant_check_ignores_every_exempt_date_row():
    strategy, execution = _minute_pair(periods=4)
    index = pd.DatetimeIndex(
        [
            "2024-01-02 09:30",
            "2024-01-02 09:31",
            "2024-01-03 09:30",
            "2024-01-03 09:31",
        ],
        name="trade_time",
    )
    strategy.index = index
    execution.index = index.copy()
    strategy["pre_close"] = [np.nan, 999.0, 10.0, 10.0]
    execution["pre_close"] = [0.0, 999.0, 10.0, 10.0]

    aligned = _validate(
        strategy,
        execution,
        Frequency.MINUTE,
        pre_close_exempt_dates=["2024-01-02"],
    )

    assert aligned.report.ok
    assert aligned.report.pre_close_exempt_dates == ("20240102",)


def test_minute_pre_close_constant_check_remains_strict_on_other_dates():
    strategy, execution = _minute_pair(periods=4)
    index = pd.DatetimeIndex(
        [
            "2024-01-02 09:30",
            "2024-01-02 09:31",
            "2024-01-03 09:30",
            "2024-01-03 09:31",
        ],
        name="trade_time",
    )
    strategy.index = index
    execution.index = index.copy()
    execution["pre_close"] = [0.0, 999.0, 10.0, 11.0]

    report = _capture_report(
        strategy,
        execution,
        Frequency.MINUTE,
        pre_close_exempt_dates=["20240102"],
    )

    issue = next(
        issue for issue in report.issues
        if issue.code == "INVALID_PRE_CLOSE"
        and issue.track is TrackRole.EXECUTION
        and "constant" in issue.message
    )
    assert issue.sample_keys == ("000001.SZ@2024-01-03T09:31:00",)


@pytest.mark.parametrize(
    "dates",
    [
        "20240102",
        b"20240102",
        None,
        {"date": "20240102"},
        (value for value in ["20240102"]),
    ],
)
def test_pre_close_exemption_requires_an_explicit_date_collection(dates):
    strategy, execution = _daily_pair()

    with pytest.raises(TypeError, match="pre_close_exempt_dates"):
        _validate(strategy, execution, pre_close_exempt_dates=dates)


@pytest.mark.parametrize(
    "value",
    [
        "20240230",
        "2024/01/02",
        "20240102junk",
        datetime(2024, 1, 2, 12, 0),
        pd.Timestamp("2024-01-02", tz="UTC"),
        True,
    ],
)
def test_pre_close_exemption_rejects_non_daily_values(value):
    strategy, execution = _daily_pair()

    with pytest.raises(ValueError, match="pre_close_exempt_dates"):
        _validate(strategy, execution, pre_close_exempt_dates=[value])


def test_inspect_pair_preserves_canonical_pre_close_exempt_dates_on_failure():
    strategy, execution = _daily_pair()
    execution.loc[execution.index[2], "amount"] = -1.0

    report = inspect_pair(
        strategy,
        execution,
        symbol=SYMBOL,
        strategy_price_space=PriceSpace.HFQ,
        execution_price_space=PriceSpace.RAW,
        strategy_amount_unit=AmountUnit.THOUSAND_YUAN,
        execution_amount_unit=AmountUnit.THOUSAND_YUAN,
        frequency=Frequency.DAILY,
        pre_close_exempt_dates={"2024-01-03", "20240102"},
    )

    assert not report.ok
    assert report.pre_close_exempt_dates == ("20240102", "20240103")


def test_duplicate_columns_fail_deterministically_without_crashing():
    strategy, execution = _daily_pair()
    strategy = pd.concat([strategy, strategy[["open"]]], axis=1)
    strategy.columns = ["open", "high", "low", "close", "amount", "open"]
    strategy_before = strategy.copy(deep=True)

    first = _capture_report(strategy, execution)
    second = _capture_report(strategy.copy(deep=True), execution.copy(deep=True))

    duplicate = next(
        issue for issue in first.issues if issue.code == "DUPLICATE_COLUMN"
    )
    assert duplicate.track is TrackRole.STRATEGY
    assert duplicate.count == 2
    assert duplicate.sample_keys == ("000001.SZ@column:open",)
    assert first.to_dict() == second.to_dict()
    assert_frame_equal(strategy, strategy_before)


def test_duplicate_timestamp_columns_report_instead_of_raising_pandas_error():
    strategy, execution = _daily_pair()
    strategy = strategy.reset_index()
    strategy.insert(1, "trade_date_copy", strategy["trade_date"])
    strategy.columns = [
        "trade_date",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "amount",
    ]

    report = _capture_report(strategy, execution)

    assert "DUPLICATE_COLUMN" in report.issue_codes
    assert "MISSING_TIMESTAMP" in report.issue_codes


def test_inspect_pair_returns_the_same_strict_report_without_enforcing():
    strategy, execution = _daily_pair()
    execution.loc[execution.index[0], "amount"] = -1.0

    inspected = inspect_pair(
        strategy,
        execution,
        symbol=SYMBOL,
        strategy_price_space=PriceSpace.HFQ,
        execution_price_space=PriceSpace.RAW,
        strategy_amount_unit=AmountUnit.THOUSAND_YUAN,
        execution_amount_unit=AmountUnit.THOUSAND_YUAN,
        frequency=Frequency.DAILY,
    )
    enforced = _capture_report(strategy, execution)

    assert inspected == enforced
    assert inspected.status == "fail"
    assert inspected.strict is True


def test_minute_date_column_must_match_the_minute_timestamp_date():
    strategy, execution = _minute_pair()
    execution["trade_date"] = ["20240102", "20240103", "20240102"]

    report = _capture_report(strategy, execution, Frequency.MINUTE)

    conflict = next(
        issue
        for issue in report.issues
        if issue.code == "KEY_MISMATCH"
        and issue.track is TrackRole.EXECUTION
        and issue.field == "trade_date"
    )
    assert conflict.count == 1
    assert conflict.sample_keys == ("000001.SZ@2024-01-02T09:31:00",)


def test_minute_date_column_alone_is_not_guessed_as_a_minute_timestamp():
    strategy, execution = _minute_pair()
    strategy = strategy.reset_index(drop=True)
    strategy["trade_date"] = "20240102"

    report = _capture_report(strategy, execution, Frequency.MINUTE)

    issue = next(
        issue for issue in report.issues if issue.code == "MISSING_TIMESTAMP"
    )
    assert issue.track is TrackRole.STRATEGY


def test_timestamp_columns_are_kept_while_index_is_canonicalized():
    strategy, execution = _daily_pair()
    strategy = strategy.reset_index()
    execution = execution.reset_index()

    aligned = _validate(strategy, execution)

    assert isinstance(aligned.strategy.index, pd.DatetimeIndex)
    assert aligned.strategy.index.name == "timestamp"
    assert "trade_date" in aligned.strategy.columns
    assert "trade_date" in aligned.execution.columns
    assert len(aligned.strategy) == len(strategy)
    assert len(aligned.execution) == len(execution)


def test_matching_symbol_columns_are_canonicalized_only_in_returned_copies():
    strategy, execution = _daily_pair()
    strategy["symbol"] = " 000001.sz "
    execution["ts_code"] = "000001.sz"
    strategy_before = strategy.copy(deep=True)
    execution_before = execution.copy(deep=True)

    aligned = _validate(strategy, execution)

    assert aligned.strategy["symbol"].unique().tolist() == [SYMBOL]
    assert aligned.execution["ts_code"].unique().tolist() == [SYMBOL]
    assert_frame_equal(strategy, strategy_before)
    assert_frame_equal(execution, execution_before)


def test_missing_columns_are_aggregated_for_both_tracks():
    strategy, execution = _daily_pair()
    strategy = strategy.drop(columns=["high"])
    execution = execution.drop(columns=["low", "pre_close", "amount"])

    report = _capture_report(strategy, execution)

    missing = {
        (issue.track, issue.field)
        for issue in report.issues
        if issue.code == "MISSING_COLUMN"
    }
    assert missing == {
        (TrackRole.STRATEGY, "high"),
        (TrackRole.EXECUTION, "low"),
        (TrackRole.EXECUTION, "pre_close"),
        (TrackRole.EXECUTION, "amount"),
    }


def test_ohlc_finite_positive_and_envelope_rules_are_all_reported():
    strategy, execution = _daily_pair()
    strategy.iloc[0, strategy.columns.get_loc("open")] = np.nan
    strategy.iloc[1, strategy.columns.get_loc("low")] = 0.0
    strategy.iloc[2, strategy.columns.get_loc("high")] = 9.0

    report = _capture_report(strategy, execution)

    strategy_codes = {
        issue.code
        for issue in report.issues
        if issue.track is TrackRole.STRATEGY
    }
    assert {
        "NONFINITE_VALUE",
        "NONPOSITIVE_PRICE",
        "OHLC_INCONSISTENT",
    }.issubset(strategy_codes)


def test_execution_pre_close_and_amount_rules_are_all_reported():
    strategy, execution = _daily_pair(periods=4)
    execution["pre_close"] = [np.inf, 0.0, 10.0, 10.0]
    execution["amount"] = [10.0, 11.0, np.nan, -1.0]

    report = _capture_report(strategy, execution)

    codes = {
        issue.code
        for issue in report.issues
        if issue.track is TrackRole.EXECUTION
    }
    assert {
        "INVALID_PRE_CLOSE",
        "INVALID_AMOUNT",
    }.issubset(codes)


def test_optional_strategy_pre_close_and_amount_are_strictly_validated():
    strategy, execution = _daily_pair()
    strategy["pre_close"] = [10.0, 0.0, 10.0]
    strategy["amount"] = [1.0, -1.0, 3.0]

    report = _capture_report(strategy, execution)

    fields = {
        (issue.code, issue.field)
        for issue in report.issues
        if issue.track is TrackRole.STRATEGY
    }
    assert ("INVALID_PRE_CLOSE", "pre_close") in fields
    assert ("INVALID_AMOUNT", "amount") in fields


def test_daily_amount_overflow_is_rejected_on_both_tracks():
    strategy, execution = _daily_pair()
    huge = np.finfo(np.float64).max
    strategy.loc[strategy.index[0], "amount"] = huge
    execution.loc[execution.index[1], "amount"] = huge

    report = _capture_report(strategy, execution)

    invalid_amount_tracks = {
        issue.track
        for issue in report.issues
        if issue.code == "INVALID_AMOUNT"
    }
    assert invalid_amount_tracks == {TrackRole.STRATEGY, TrackRole.EXECUTION}


@pytest.mark.parametrize(
    "bad_value",
    [True, 10 + 5j, pd.Timestamp("2024-01-02"), pd.Timedelta(days=10), "10.0"],
)
def test_price_values_reject_bool_complex_time_and_numeric_strings(bad_value):
    strategy, execution = _daily_pair()
    strategy["open"] = strategy["open"].astype(object)
    strategy.iloc[0, strategy.columns.get_loc("open")] = bad_value
    strategy_before = strategy.copy(deep=True)

    report = _capture_report(strategy, execution)

    issue = next(
        issue
        for issue in report.issues
        if issue.code == "NONFINITE_VALUE"
        and issue.track is TrackRole.STRATEGY
    )
    assert issue.field == "ohlc"
    assert issue.count == 1
    assert_frame_equal(strategy, strategy_before)


def test_duplicate_and_non_monotonic_keys_fail_without_sorting():
    strategy, execution = _daily_pair()
    bad_index = pd.Index(
        ["20240103", "20240103", "20240102"], name="trade_date"
    )
    strategy.index = bad_index
    execution.index = bad_index.copy()
    strategy_before = strategy.copy(deep=True)
    execution_before = execution.copy(deep=True)

    report = _capture_report(strategy, execution)

    by_track = {
        track: {
            issue.code for issue in report.issues if issue.track is track
        }
        for track in TrackRole
    }
    for track in TrackRole:
        assert "DUPLICATE_KEY" in by_track[track]
        assert "NON_MONOTONIC_KEY" in by_track[track]
    assert_frame_equal(strategy, strategy_before)
    assert_frame_equal(execution, execution_before)


def test_key_sets_must_be_equal_and_are_never_intersected_or_filled():
    strategy, execution = _daily_pair()
    execution.index = pd.Index(
        ["20240102", "20240103", "20240109"], name="trade_date"
    )

    report = _capture_report(strategy, execution)

    mismatches = [
        issue for issue in report.issues if issue.code == "KEY_MISMATCH"
    ]
    assert [(issue.track, issue.count) for issue in mismatches] == [
        (TrackRole.STRATEGY, 1),
        (TrackRole.EXECUTION, 1),
    ]
    assert report.strategy_rows == 3
    assert report.execution_rows == 3
    assert report.aligned_rows == 2


def test_timestamp_and_symbol_keys_are_validated_explicitly():
    strategy, execution = _daily_pair()
    strategy.index = pd.Index(
        ["20240102", "not-a-date", "20240104"], name="trade_date"
    )
    execution["ts_code"] = [SYMBOL, "000002.SZ", SYMBOL]

    report = _capture_report(strategy, execution)

    assert "INVALID_TIMESTAMP" in report.issue_codes
    mismatch = next(
        issue for issue in report.issues if issue.code == "SYMBOL_MISMATCH"
    )
    assert mismatch.track is TrackRole.EXECUTION
    assert mismatch.field == "ts_code"
    assert mismatch.sample_keys == ("000001.SZ@2024-01-03",)


def test_conflicting_timestamp_sources_are_never_ignored():
    strategy, execution = _daily_pair()
    strategy["timestamp"] = strategy.index.tolist()
    strategy.loc[strategy.index[1], "timestamp"] = "20240109"

    report = _capture_report(strategy, execution)

    conflict = next(
        issue
        for issue in report.issues
        if issue.code == "KEY_MISMATCH"
        and issue.track is TrackRole.STRATEGY
        and issue.field == "timestamp"
    )
    assert conflict.count == 1
    assert conflict.sample_keys == ("000001.SZ@2024-01-03",)


@pytest.mark.parametrize(
    ("frequency", "bad_index"),
    [
        (
            Frequency.DAILY,
            pd.DatetimeIndex(
                [
                    "2024-01-02 09:30",
                    "2024-01-03 00:00",
                    "2024-01-04 00:00",
                ],
                name="trade_date",
            ),
        ),
        (
            Frequency.MINUTE,
            pd.DatetimeIndex(
                [
                    "2024-01-02 09:30:01",
                    "2024-01-02 09:31:00",
                    "2024-01-02 09:32:00",
                ],
                name="trade_time",
            ),
        ),
        (
            Frequency.MINUTE,
            pd.date_range(
                "2024-01-02 09:30", periods=3, freq="min", tz="Asia/Shanghai"
            ).rename("trade_time"),
        ),
    ],
)
def test_timestamp_normalization_rejects_lossy_granularity_or_timezone(
    frequency, bad_index
):
    if frequency is Frequency.DAILY:
        strategy, execution = _daily_pair()
    else:
        strategy, execution = _minute_pair()
    strategy.index = bad_index
    strategy_before = strategy.copy(deep=True)

    report = _capture_report(strategy, execution, frequency)

    issue = next(
        issue
        for issue in report.issues
        if issue.code == "INVALID_TIMESTAMP"
        and issue.track is TrackRole.STRATEGY
    )
    assert issue.field == "index"
    assert issue.count >= 1
    assert_frame_equal(strategy, strategy_before)


@pytest.mark.parametrize("bad_symbol", [None, pd.NA, np.nan, 1])
def test_symbol_argument_must_be_a_nonmissing_string(bad_symbol):
    strategy, execution = _daily_pair()

    with pytest.raises(ValueError, match="symbol"):
        validate_and_align_pair(
            strategy,
            execution,
            symbol=bad_symbol,
            strategy_price_space=PriceSpace.HFQ,
            execution_price_space=PriceSpace.RAW,
            strategy_amount_unit=AmountUnit.THOUSAND_YUAN,
            execution_amount_unit=AmountUnit.THOUSAND_YUAN,
            frequency=Frequency.DAILY,
        )


def test_range_index_without_frequency_key_is_not_guessed():
    strategy, execution = _daily_pair()
    strategy = strategy.reset_index(drop=True)

    report = _capture_report(strategy, execution)

    issue = next(
        issue for issue in report.issues if issue.code == "MISSING_TIMESTAMP"
    )
    assert issue.track is TrackRole.STRATEGY


def test_report_is_repeatable_clock_free_and_samples_are_sorted_and_capped():
    strategy, execution = _daily_pair(periods=9)
    execution.index = pd.Index(
        pd.date_range("2024-02-01", periods=9, freq="D").strftime("%Y%m%d"),
        name="trade_date",
    )

    first = _capture_report(strategy, execution)
    second = _capture_report(copy.deepcopy(strategy), copy.deepcopy(execution))

    assert first == second
    assert first.to_dict() == second.to_dict()
    assert first.schema_version == 4
    assert first.contract_version == CONTRACT_VERSION
    assert tuple(first.to_dict()) == (
        "schema_version",
        "contract_version",
        "strict",
        "status",
        "symbol",
        "frequency",
        "strategy_price_space",
        "execution_price_space",
        "strategy_amount_unit",
        "execution_amount_unit",
        "pre_close_exempt_dates",
        "strategy_pre_close_source",
        "execution_pre_close_source",
        "adjustment_factor_identity",
        "strategy_rows",
        "execution_rows",
        "aligned_rows",
        "issues",
    )
    for issue in first.issues:
        assert len(issue.sample_keys) <= MAX_ISSUE_SAMPLES
        assert issue.sample_keys == tuple(sorted(set(issue.sample_keys)))


def test_global_issue_track_serializes_as_null():
    global_issue = DataQualityIssue(
        code="READ_ERROR",
        track=None,
        count=1,
        field=None,
        sample_keys=(),
        message="provider read failed",
    )
    track_issue = DataQualityIssue(
        code="MISSING_COLUMN",
        track=TrackRole.STRATEGY,
        count=1,
        field="open",
        sample_keys=(),
        message="required column is missing",
    )
    report = DataQualityReport(
        symbol=SYMBOL,
        frequency=Frequency.DAILY,
        strategy_price_space=PriceSpace.HFQ,
        execution_price_space=PriceSpace.RAW,
        strategy_amount_unit=AmountUnit.THOUSAND_YUAN,
        execution_amount_unit=AmountUnit.THOUSAND_YUAN,
        strategy_rows=0,
        execution_rows=0,
        aligned_rows=0,
        issues=(track_issue, global_issue),
    )

    assert global_issue.to_dict()["track"] is None
    assert report.issues[0] is global_issue
    assert report.to_dict()["issues"][0]["track"] is None


def test_error_carries_exact_report_and_has_deterministic_message():
    strategy, execution = _daily_pair()
    execution.loc[execution.index[0], "amount"] = -1.0

    with pytest.raises(DataContractError) as first:
        _validate(strategy, execution)
    with pytest.raises(DataContractError) as second:
        _validate(strategy, execution)

    assert first.value.report.status == "fail"
    assert str(first.value) == str(second.value)
    assert "INVALID_AMOUNT:1" in str(first.value)


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("strategy_price_space", "qfq"),
        ("execution_price_space", "adjusted"),
        ("strategy_amount_unit", "lots"),
        ("execution_amount_unit", "thousands"),
        ("frequency", "weekly"),
    ],
)
def test_unknown_contract_dimensions_are_rejected(argument, value):
    strategy, execution = _daily_pair()
    kwargs = {
        "symbol": SYMBOL,
        "strategy_price_space": PriceSpace.HFQ,
        "execution_price_space": PriceSpace.RAW,
        "strategy_amount_unit": AmountUnit.THOUSAND_YUAN,
        "execution_amount_unit": AmountUnit.THOUSAND_YUAN,
        "frequency": Frequency.DAILY,
    }
    kwargs[argument] = value

    with pytest.raises(ValueError, match=argument):
        validate_and_align_pair(strategy, execution, **kwargs)
