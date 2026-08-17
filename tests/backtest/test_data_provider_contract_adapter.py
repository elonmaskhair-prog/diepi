"""Pure synthetic tests for DataProvider's strict paired-data adapter."""

from types import SimpleNamespace

import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from diepi.backtest.data.contract import (
    AlignedMarketData,
    AmountUnit,
    DataContractError,
    Frequency,
    PreCloseSource,
    PriceSpace,
    TrackRole,
)
from diepi.backtest.data.data_provider import DataProvider
from diepi.backtest.data.exceptions import ParameterError


SYMBOL = "000001.SZ"


class _MemoryCache:
    def __init__(self, frames):
        self.frames = frames
        self.calls = []
        self.factor_calls = []

    def get_data(self, category, symbol):
        self.calls.append((category, symbol))
        frame = self.frames.get(category)
        if frame is None:
            return pd.DataFrame()
        return frame.copy(deep=True)

    def get_adj_factor(self, symbol):
        self.factor_calls.append(symbol)
        frame = self.frames.get("adj_factor")
        if frame is None:
            return pd.DataFrame()
        return frame.copy(deep=True)


class _DateHelper:
    def get_prev_trade_day(self, _date):
        return "20991231"

    def get_yesterday(self):
        return "20991231"


def _daily_frame(price_offset=0.0, dates=None):
    dates = dates or ["20240102", "20240103", "20240104"]
    base = pd.Series(
        [10.0 + price_offset + position for position in range(len(dates))],
        dtype="float64",
    )
    return pd.DataFrame(
        {
            "trade_date": dates,
            "open": base,
            "high": base + 0.5,
            "low": base - 0.5,
            "close": base + 0.2,
            "pre_close": base - 0.1,
            "amount": [1.0 + position for position in range(len(dates))],
        }
    )


def _minute_frame(price_offset=0.0, times=None):
    times = times or [
        "2024-01-02 09:30",
        "2024-01-02 09:31",
        "2024-01-03 09:30",
        "2024-01-03 09:31",
    ]
    timestamps = pd.to_datetime(times)
    day_numbers = pd.Series(
        [timestamp.day - 2 for timestamp in timestamps], dtype="float64"
    )
    base = 10.0 + price_offset + day_numbers
    pre_close = [9.9 + price_offset + value for value in day_numbers]
    return pd.DataFrame(
        {
            "trade_time": timestamps,
            "open": base,
            "high": base + 0.5,
            "low": base - 0.5,
            "close": base + 0.2,
            "pre_close": pre_close,
            "amount": [100.0 + position for position in range(len(times))],
        }
    )


def _factor_frame():
    rows = [{"ts_code": SYMBOL, "trade_date": "20240101", "adj_factor": 1.0}]
    for day in range(2, 10):
        raw_close = 10.2 + (day - 2)
        rows.append(
            {
                "ts_code": SYMBOL,
                "trade_date": "202401%02d" % day,
                "adj_factor": (raw_close + 20.0) / raw_close,
            }
        )
    return pd.DataFrame(rows)


_AUTO_FACTOR = object()


def _provider(
        *, strategy_mode="hfq", execution_mode="raw", frames=None,
        context=None, factor_frame=_AUTO_FACTOR):
    if frames is None:
        frames = {
            "daily_data": _daily_frame(20.0),
            "daily_data_raw": _daily_frame(),
            "minute_data": _minute_frame(20.0),
            "minute_data_raw": _minute_frame(),
        }
    else:
        frames = dict(frames)
    if factor_frame is _AUTO_FACTOR:
        frames.setdefault("adj_factor", _factor_frame())
    elif factor_frame is not None:
        frames["adj_factor"] = factor_frame
    provider = object.__new__(DataProvider)
    provider._cache = _MemoryCache(frames)
    provider._date_helper = _DateHelper()
    provider._context = context
    provider._price_mode = strategy_mode
    provider._execution_price_mode = execution_mode
    provider._adj_factor_cache = {}
    provider._adj_factor_ratio_cache = {}
    return provider


def _issues(error):
    return {(issue.code, issue.track, issue.field) for issue in error.report.issues}


def test_daily_pair_returns_both_tracks_report_and_yuan_amounts():
    provider = _provider()

    aligned = provider.get_aligned_pair(SYMBOL, frequency="daily")

    assert isinstance(aligned, AlignedMarketData)
    assert aligned.report.ok
    assert aligned.frequency is Frequency.DAILY
    assert aligned.strategy_price_space is PriceSpace.HFQ
    assert aligned.execution_price_space is PriceSpace.RAW
    assert aligned.strategy_amount_unit is AmountUnit.THOUSAND_YUAN
    assert aligned.execution_amount_unit is AmountUnit.THOUSAND_YUAN
    assert aligned.strategy["amount"].tolist() == [1000.0, 2000.0, 3000.0]
    assert aligned.execution["amount"].tolist() == [1000.0, 2000.0, 3000.0]
    assert aligned.report is not None
    assert aligned.report.pre_close_exempt_dates == ()


def test_adapter_forwards_only_explicit_pre_close_exemption_dates():
    frames = {
        "daily_data": _daily_frame(20.0),
        "daily_data_raw": _daily_frame(),
    }
    frames["daily_data"].loc[0, "pre_close"] = float("nan")
    frames["daily_data_raw"].loc[0, "pre_close"] = 0.0
    provider = _provider(frames=frames)

    aligned = provider.get_aligned_pair(
        SYMBOL,
        frequency="daily",
        pre_close_exempt_dates=["2024-01-02", "20240102"],
    )

    assert aligned.report.ok
    assert aligned.pre_close_exempt_dates == ("20240102",)
    assert aligned.report.to_dict()["pre_close_exempt_dates"] == ["20240102"]


def test_adapter_does_not_consult_implicit_provider_pre_close_exempt_state():
    frames = {
        "daily_data": _daily_frame(20.0),
        "daily_data_raw": _daily_frame(),
    }
    frames["daily_data_raw"].loc[0, "pre_close"] = 0.0
    provider = _provider(frames=frames)
    provider.pre_close_exempt_dates = {"20240102"}

    with pytest.raises(DataContractError) as caught:
        provider.get_aligned_pair(SYMBOL, frequency="daily")

    assert caught.value.report.pre_close_exempt_dates == ()
    assert (
        "INVALID_PRE_CLOSE",
        TrackRole.EXECUTION,
        "pre_close",
    ) in _issues(caught.value)


def test_adapter_rejects_bare_string_pre_close_exemption_collection():
    provider = _provider()

    with pytest.raises(TypeError, match="pre_close_exempt_dates"):
        provider.get_aligned_pair(
            SYMBOL,
            frequency="daily",
            pre_close_exempt_dates="20240102",
        )


def test_daily_pair_uses_resolved_provider_modes_not_hard_coded_roles():
    provider = _provider(strategy_mode="raw", execution_mode="hfq")

    aligned = provider.get_aligned_pair(SYMBOL, frequency=Frequency.DAILY)

    assert aligned.strategy_price_space is PriceSpace.RAW
    assert aligned.execution_price_space is PriceSpace.HFQ
    assert provider._cache.calls == [
        ("daily_data_raw", SYMBOL),
        ("daily_data", SYMBOL),
    ]
    assert aligned.strategy.iloc[0]["open"] == 10.0
    assert aligned.execution.iloc[0]["open"] == 30.0


def test_daily_explicit_date_range_is_applied_to_both_tracks():
    provider = _provider()

    aligned = provider.get_aligned_pair(
        SYMBOL, frequency="daily", start="2024-01-03", end="2024/01/04"
    )

    assert aligned.strategy.index.tolist() == [
        pd.Timestamp("2024-01-03"),
        pd.Timestamp("2024-01-04"),
    ]
    assert aligned.execution.index.equals(aligned.strategy.index)


def test_daily_start_only_is_applied_to_both_tracks():
    provider = _provider()

    aligned = provider.get_aligned_pair(
        SYMBOL, frequency="daily", start="20240103"
    )

    assert aligned.report.aligned_rows == 2
    assert aligned.strategy.index[0] == pd.Timestamp("2024-01-03")


def test_daily_end_and_count_are_applied_to_both_tracks():
    provider = _provider()

    aligned = provider.get_aligned_pair(
        SYMBOL, frequency="daily", end="20240103", count=1
    )

    assert aligned.report.aligned_rows == 1
    assert aligned.strategy.index[0] == pd.Timestamp("2024-01-03")


def test_minute_pair_keeps_yuan_amounts_without_scaling():
    provider = _provider()

    aligned = provider.get_aligned_pair(
        SYMBOL, frequency="minute", trade_date="20240102"
    )

    assert aligned.frequency is Frequency.MINUTE
    assert aligned.strategy_amount_unit is AmountUnit.YUAN
    assert aligned.execution_amount_unit is AmountUnit.YUAN
    assert aligned.strategy["amount"].tolist() == [100.0, 101.0]
    assert aligned.execution["amount"].tolist() == [100.0, 101.0]
    assert aligned.strategy_pre_close_source is PreCloseSource.NATIVE
    assert aligned.execution_pre_close_source is PreCloseSource.NATIVE


def test_minute_missing_pre_close_is_enriched_from_each_same_price_lane():
    frames = {
        "daily_data": _daily_frame(20.0),
        "daily_data_raw": _daily_frame(),
        "minute_data": _minute_frame(20.0).drop(columns=["pre_close"]),
        "minute_data_raw": _minute_frame().drop(columns=["pre_close"]),
    }
    provider = _provider(frames=frames)

    aligned = provider.get_aligned_pair(
        SYMBOL, frequency="minute", trade_date="20240102"
    )

    assert aligned.strategy["pre_close"].tolist() == [29.9, 29.9]
    assert aligned.execution["pre_close"].tolist() == [9.9, 9.9]
    assert aligned.strategy_pre_close_source is PreCloseSource.SAME_LANE_DAILY
    assert aligned.execution_pre_close_source is PreCloseSource.SAME_LANE_DAILY
    assert aligned.report.to_dict()["strategy_pre_close_source"] == (
        "same_lane_daily"
    )
    assert aligned.report.to_dict()["execution_pre_close_source"] == (
        "same_lane_daily"
    )
    assert provider._cache.calls == [
        ("minute_data", SYMBOL),
        ("minute_data_raw", SYMBOL),
        ("daily_data", SYMBOL),
        ("daily_data_raw", SYMBOL),
    ]


def test_synthetic_suspension_minutes_supply_audited_pre_close_fallback():
    minute = pd.DataFrame({
        "trade_time": pd.to_datetime([
            "2024-01-03 09:30", "2024-01-03 09:31"
        ]),
        "open": [10.0, 10.0],
        "high": [10.0, 10.0],
        "low": [10.0, 10.0],
        "close": [10.0, 10.0],
        "vol": [0.0, 0.0],
        "amount": [0.0, 0.0],
    })
    provider = _provider(
        strategy_mode="raw",
        execution_mode="raw",
        frames={
            "minute_data_raw": minute,
            "minute_data": minute,
        },
        factor_frame=None,
    )

    aligned = provider.get_aligned_pair(
        SYMBOL, frequency="minute", trade_date="20240103"
    )

    assert aligned.strategy["pre_close"].tolist() == [10.0, 10.0]
    assert aligned.execution["pre_close"].tolist() == [10.0, 10.0]
    assert aligned.strategy_pre_close_source is (
        PreCloseSource.SAME_LANE_DAILY_WITH_SUSPENSION_FALLBACK
    )
    assert aligned.report.ok


def test_multi_day_minute_enrichment_maps_each_date_without_resampling():
    frames = {
        "daily_data": _daily_frame(20.0),
        "daily_data_raw": _daily_frame(),
        "minute_data": _minute_frame(20.0).drop(columns=["pre_close"]),
        "minute_data_raw": _minute_frame().drop(columns=["pre_close"]),
    }
    provider = _provider(frames=frames)

    aligned = provider.get_aligned_pair(
        SYMBOL, frequency="minute", start="20240102", end="20240103"
    )

    assert aligned.strategy["pre_close"].tolist() == [
        29.9, 29.9, 30.9, 30.9
    ]
    assert aligned.execution["pre_close"].tolist() == [
        9.9, 9.9, 10.9, 10.9
    ]
    assert aligned.strategy.index.tolist() == pd.to_datetime(
        [
            "2024-01-02 09:30",
            "2024-01-02 09:31",
            "2024-01-03 09:30",
            "2024-01-03 09:31",
        ]
    ).tolist()


def test_minute_enrichment_never_uses_other_price_lane_daily_reference():
    frames = {
        "daily_data": _daily_frame(20.0),
        "minute_data": _minute_frame(20.0).drop(columns=["pre_close"]),
        "minute_data_raw": _minute_frame().drop(columns=["pre_close"]),
    }
    provider = _provider(frames=frames)

    with pytest.raises(DataContractError) as caught:
        provider.get_aligned_pair(
            SYMBOL, frequency="minute", trade_date="20240102"
        )

    assert caught.value.report.strategy_pre_close_source is (
        PreCloseSource.SAME_LANE_DAILY
    )
    assert caught.value.report.execution_pre_close_source is PreCloseSource.ABSENT
    assert (
        "MISSING_COLUMN",
        TrackRole.EXECUTION,
        "pre_close",
    ) in _issues(caught.value)
    assert ("daily_data_raw", SYMBOL) in provider._cache.calls


def test_minute_enrichment_requires_complete_same_lane_daily_date_mapping():
    frames = {
        "daily_data": _daily_frame(20.0, dates=["20240102"]),
        "daily_data_raw": _daily_frame(dates=["20240102"]),
        "minute_data": _minute_frame(20.0).drop(columns=["pre_close"]),
        "minute_data_raw": _minute_frame().drop(columns=["pre_close"]),
    }
    provider = _provider(frames=frames)

    with pytest.raises(DataContractError) as caught:
        provider.get_aligned_pair(
            SYMBOL, frequency="minute", start="20240102", end="20240103"
        )

    missing_pre_close_tracks = {
        issue.track
        for issue in caught.value.report.issues
        if issue.code == "MISSING_COLUMN" and issue.field == "pre_close"
    }
    assert missing_pre_close_tracks == {
        TrackRole.STRATEGY,
        TrackRole.EXECUTION,
    }


def test_minute_enrichment_rejects_duplicate_same_lane_daily_keys():
    duplicate_daily = pd.concat([_daily_frame(), _daily_frame().iloc[[0]]])
    frames = {
        "daily_data": duplicate_daily.copy(),
        "daily_data_raw": duplicate_daily.copy(),
        "minute_data": _minute_frame(20.0).drop(columns=["pre_close"]),
        "minute_data_raw": _minute_frame().drop(columns=["pre_close"]),
    }
    provider = _provider(frames=frames)

    with pytest.raises(DataContractError) as caught:
        provider.get_aligned_pair(
            SYMBOL, frequency="minute", trade_date="20240102"
        )

    assert caught.value.report.strategy_pre_close_source is PreCloseSource.ABSENT
    assert caught.value.report.execution_pre_close_source is PreCloseSource.ABSENT


def test_enriched_invalid_pre_close_requires_explicit_date_exemption():
    frames = {
        "daily_data": _daily_frame(20.0),
        "daily_data_raw": _daily_frame(),
        "minute_data": _minute_frame(20.0).drop(columns=["pre_close"]),
        "minute_data_raw": _minute_frame().drop(columns=["pre_close"]),
    }
    frames["daily_data"].loc[0, "pre_close"] = float("nan")
    frames["daily_data_raw"].loc[0, "pre_close"] = 0.0
    provider = _provider(frames=frames)

    with pytest.raises(DataContractError) as caught:
        provider.get_aligned_pair(
            SYMBOL, frequency="minute", trade_date="20240102"
        )
    assert any(
        issue.code == "INVALID_PRE_CLOSE"
        for issue in caught.value.report.issues
    )

    aligned = provider.get_aligned_pair(
        SYMBOL,
        frequency="minute",
        trade_date="20240102",
        pre_close_exempt_dates=["20240102"],
    )
    assert aligned.report.ok
    assert aligned.strategy_pre_close_source is PreCloseSource.SAME_LANE_DAILY
    assert aligned.execution_pre_close_source is PreCloseSource.SAME_LANE_DAILY


def test_minute_explicit_trade_date_is_applied_to_both_tracks():
    provider = _provider()

    aligned = provider.get_aligned_pair(
        SYMBOL, frequency="minute", trade_date="2024-01-03"
    )

    assert aligned.report.aligned_rows == 2
    assert {timestamp.strftime("%Y%m%d") for timestamp in aligned.strategy.index} == {
        "20240103"
    }
    assert provider._cache.calls == [
        ("minute_data", SYMBOL),
        ("minute_data_raw", SYMBOL),
    ]


def test_current_day_minute_request_preserves_provider_visibility_boundary():
    context = SimpleNamespace(
        current_date="20240102",
        current_time=pd.Timestamp("2024-01-02 09:31"),
    )
    provider = _provider(context=context)

    aligned = provider.get_aligned_pair(SYMBOL, frequency="minute")

    assert aligned.strategy.index.tolist() == [
        pd.Timestamp("2024-01-02 09:30"),
        pd.Timestamp("2024-01-02 09:31"),
    ]
    assert aligned.execution.index.equals(aligned.strategy.index)


def test_minute_explicit_multi_day_range_stays_at_minute_frequency():
    provider = _provider()

    aligned = provider.get_aligned_pair(
        SYMBOL, frequency="minute", start="20240102", end="20240103"
    )

    assert aligned.frequency is Frequency.MINUTE
    assert aligned.report.aligned_rows == 4
    assert all(timestamp.hour == 9 for timestamp in aligned.strategy.index)


def test_minute_range_time_window_is_applied_per_day():
    provider = _provider()

    aligned = provider.get_aligned_pair(
        SYMBOL,
        frequency="minute",
        start="20240102",
        end="20240103",
        start_time="09:31",
        end_time="09:31",
    )

    assert aligned.report.aligned_rows == 2
    assert [timestamp.strftime("%H:%M") for timestamp in aligned.strategy.index] == [
        "09:31",
        "09:31",
    ]


@pytest.mark.parametrize(
    ("missing_category", "track"),
    [
        ("daily_data", TrackRole.STRATEGY),
        ("daily_data_raw", TrackRole.EXECUTION),
    ],
)
def test_missing_daily_leg_raises_data_contract_error(missing_category, track):
    frames = {
        "daily_data": _daily_frame(20.0),
        "daily_data_raw": _daily_frame(),
    }
    del frames[missing_category]
    provider = _provider(frames=frames)

    with pytest.raises(DataContractError) as caught:
        provider.get_aligned_pair(SYMBOL, frequency="daily")

    assert ("MISSING_DATASET", track, None) in _issues(caught.value)
    assert caught.value.report.status == "fail"


def test_missing_minute_execution_leg_does_not_fall_back_to_strategy():
    frames = {"minute_data": _minute_frame(20.0)}
    provider = _provider(frames=frames)

    with pytest.raises(DataContractError) as caught:
        provider.get_aligned_pair(
            SYMBOL, frequency="minute", trade_date="20240102"
        )

    assert ("MISSING_DATASET", TrackRole.EXECUTION, None) in _issues(caught.value)
    assert provider._cache.calls == [
        ("minute_data", SYMBOL),
        ("minute_data_raw", SYMBOL),
    ]


def test_missing_required_execution_column_raises_data_contract_error():
    frames = {
        "daily_data": _daily_frame(20.0),
        "daily_data_raw": _daily_frame().drop(columns=["pre_close"]),
    }
    provider = _provider(frames=frames)

    with pytest.raises(DataContractError) as caught:
        provider.get_aligned_pair(SYMBOL, frequency="daily")

    assert (
        "MISSING_COLUMN",
        TrackRole.EXECUTION,
        "pre_close",
    ) in _issues(caught.value)


def test_fields_are_not_silently_completed_for_contract_validation():
    provider = _provider()

    with pytest.raises(DataContractError) as caught:
        provider.get_aligned_pair(
            SYMBOL,
            frequency="daily",
            fields=["open", "high", "low", "close"],
        )

    assert ("MISSING_COLUMN", TrackRole.EXECUTION, "amount") in _issues(
        caught.value
    )
    assert ("MISSING_COLUMN", TrackRole.EXECUTION, "pre_close") in _issues(
        caught.value
    )


def test_key_mismatch_is_rejected_without_intersection():
    frames = {
        "daily_data": _daily_frame(20.0),
        "daily_data_raw": _daily_frame(dates=[
            "20240102", "20240103", "20240105"
        ]),
    }
    provider = _provider(frames=frames)

    with pytest.raises(DataContractError) as caught:
        provider.get_aligned_pair(SYMBOL, frequency="daily")

    assert caught.value.report.aligned_rows == 2
    mismatch_issues = [
        issue for issue in caught.value.report.issues
        if issue.code == "KEY_MISMATCH"
    ]
    assert {issue.track for issue in mismatch_issues} == {
        TrackRole.STRATEGY,
        TrackRole.EXECUTION,
    }


def test_adapter_does_not_sort_reader_outputs_before_validation():
    provider = _provider()
    strategy = _daily_frame(20.0).set_index("trade_date").iloc[[1, 0, 2]]
    execution = _daily_frame().set_index("trade_date")

    def read_daily(**kwargs):
        return strategy if kwargs["price_mode"] == "hfq" else execution

    provider.get_daily = read_daily

    with pytest.raises(DataContractError) as caught:
        provider.get_aligned_pair(SYMBOL, frequency="daily")

    assert (
        "NON_MONOTONIC_KEY",
        TrackRole.STRATEGY,
        "timestamp",
    ) in _issues(caught.value)


def test_source_frames_are_not_modified_by_successful_reads():
    provider = _provider()
    before = {
        category: frame.copy(deep=True)
        for category, frame in provider._cache.frames.items()
    }

    provider.get_aligned_pair(SYMBOL, frequency="daily")
    provider.get_aligned_pair(
        SYMBOL, frequency="minute", start="20240102", end="20240103"
    )

    for category, expected in before.items():
        assert_frame_equal(provider._cache.frames[category], expected)


def test_reader_return_frames_are_not_modified_by_contract_normalization():
    provider = _provider()
    strategy = _daily_frame(20.0).set_index("trade_date")
    execution = _daily_frame().set_index("trade_date")
    strategy_before = strategy.copy(deep=True)
    execution_before = execution.copy(deep=True)

    def read_daily(**kwargs):
        return strategy if kwargs["price_mode"] == "hfq" else execution

    provider.get_daily = read_daily
    aligned = provider.get_aligned_pair(SYMBOL, frequency="daily")

    assert_frame_equal(strategy, strategy_before)
    assert_frame_equal(execution, execution_before)
    assert aligned.strategy is not strategy
    assert aligned.execution is not execution


def test_failure_report_is_deterministic_across_identical_reads():
    frames = {
        "daily_data": _daily_frame(20.0),
        "daily_data_raw": _daily_frame(dates=[
            "20240102", "20240103", "20240105"
        ]).drop(columns=["amount"]),
    }
    provider = _provider(frames=frames)

    reports = []
    for _ in range(2):
        with pytest.raises(DataContractError) as caught:
            provider.get_aligned_pair(SYMBOL, frequency="daily")
        reports.append(caught.value.report.to_dict())

    assert reports[0] == reports[1]


def test_same_resolved_mode_is_reported_and_read_twice_without_alias_fallback():
    provider = _provider(strategy_mode="raw", execution_mode="raw")

    aligned = provider.get_aligned_pair(SYMBOL, frequency="daily")

    assert aligned.strategy_price_space is PriceSpace.RAW
    assert aligned.execution_price_space is PriceSpace.RAW
    assert provider._cache.calls == [
        ("daily_data_raw", SYMBOL),
        ("daily_data_raw", SYMBOL),
    ]


@pytest.mark.parametrize("argument", ["trade_date", "start_time", "end_time"])
def test_daily_rejects_minute_only_arguments(argument):
    provider = _provider()
    kwargs = {argument: "20240102" if argument == "trade_date" else "09:30"}

    with pytest.raises(ParameterError):
        provider.get_aligned_pair(SYMBOL, frequency="daily", **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": "20240102"},
        {"end": "20240103"},
        {"start": "20240102", "end": "20240103", "trade_date": "20240102"},
        {"start": "20240102", "end": "20240103", "count": 1},
    ],
)
def test_minute_rejects_ambiguous_range_combinations(kwargs):
    provider = _provider()

    with pytest.raises(ParameterError):
        provider.get_aligned_pair(SYMBOL, frequency="minute", **kwargs)


def test_daily_rejects_start_end_and_count_together():
    provider = _provider()

    with pytest.raises(ParameterError):
        provider.get_aligned_pair(
            SYMBOL,
            frequency="daily",
            start="20240102",
            end="20240103",
            count=1,
        )


@pytest.mark.parametrize("frequency", ["hour", "", None, 1, False])
def test_invalid_frequency_is_rejected(frequency):
    provider = _provider()

    with pytest.raises(ParameterError):
        provider.get_aligned_pair(SYMBOL, frequency=frequency)


@pytest.mark.parametrize("count", [0, -1, 1.0, True, "1"])
def test_invalid_count_is_rejected(count):
    provider = _provider()

    with pytest.raises(ParameterError):
        provider.get_aligned_pair(SYMBOL, frequency="daily", count=count)


@pytest.mark.parametrize(
    "date_value",
    ["", "20240230", "202401", "20240102junk", "2024-1-02", 20240102],
)
def test_invalid_date_is_rejected(date_value):
    provider = _provider()

    with pytest.raises(ParameterError):
        provider.get_aligned_pair(
            SYMBOL, frequency="daily", start=date_value
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": "20240103", "end": "20240102"},
        {"start_time": "09:31", "end_time": "09:30"},
        {"start_time": "25:00"},
        {"start_time": "9:30"},
    ],
)
def test_reversed_or_invalid_ranges_are_rejected(kwargs):
    provider = _provider()

    with pytest.raises(ParameterError):
        provider.get_aligned_pair(SYMBOL, frequency="daily", **kwargs)


@pytest.mark.parametrize("fields", ["open", ["open", "open"], ["open", ""]])
def test_invalid_fields_are_rejected(fields):
    provider = _provider()

    with pytest.raises(ParameterError):
        provider.get_aligned_pair(
            SYMBOL, frequency="daily", fields=fields
        )
