"""Pure session-boundary tests for minute-bar resampling."""

import pandas as pd
import pytest

from diepi.backtest.engine.minute_resampler import ResampleConfig, resample_minute_data
from diepi.backtest.session_calendar import (
    SESSION_SNAPSHOT_HASH,
    SESSION_SOURCE_VERSION,
    SessionRuleNotFoundError,
    TimestampOutsideSessionError,
)


def _frame(times, *, as_index=False):
    rows = []
    for index, stamp in enumerate(times):
        base = float(index + 1)
        rows.append(
            {
                "trade_time": stamp,
                "open": base,
                "high": base + 1.0,
                "low": base - 0.5,
                "close": base + 0.25,
                "vol": index + 1,
                "amount": (index + 1) * 10.0,
                "pre_close": 9.0,
            }
        )
    result = pd.DataFrame(rows)
    if as_index:
        result["trade_time"] = pd.to_datetime(result["trade_time"])
        result = result.set_index("trade_time")
    return result


def test_strict_resampling_never_crosses_lunch_break():
    source = _frame(
        [
            "2024-01-02 11:29:00",
            "2024-01-02 11:30:00",
            "2024-01-02 13:01:00",
            "2024-01-02 13:02:00",
        ]
    )
    result = resample_minute_data(source, 60, symbol="000001.SZ")
    assert list(result["trade_time"].dt.strftime("%H:%M")) == ["11:30", "13:02"]
    assert result["vol"].tolist() == [3, 7]


def test_strict_resampling_never_groups_opening_auction_with_future_minutes():
    source = _frame(
        [
            "2024-01-02 09:30:00",
            "2024-01-02 09:31:00",
            "2024-01-02 09:32:00",
        ]
    )

    result = resample_minute_data(source, 60, symbol="000001.SZ")

    assert result["trade_time"].dt.strftime("%H:%M").tolist() == [
        "09:30",
        "09:32",
    ]
    assert result["vol"].tolist() == [1, 2 + 3]


def test_opening_auction_ohlc_is_invariant_to_future_continuous_rows():
    base = _frame(["2024-01-02 09:30:00"])
    extended = _frame(
        [
            "2024-01-02 09:30:00",
            "2024-01-02 09:31:00",
            "2024-01-02 09:32:00",
        ]
    )
    extended.loc[1:, ["open", "high", "low", "close"]] = [
        [1000.0, 2000.0, 500.0, 1500.0],
        [3000.0, 4000.0, 2500.0, 3500.0],
    ]

    base_open = resample_minute_data(base, 60, symbol="000001.SZ").iloc[0]
    extended_open = resample_minute_data(
        extended, 60, symbol="000001.SZ"
    ).iloc[0]

    for column in ("open", "high", "low", "close", "vol", "amount"):
        assert extended_open[column] == base_open[column]


def test_strict_resampling_never_crosses_trade_dates():
    source = _frame(
        [
            "2024-01-02 14:55:00",
            "2024-01-02 14:56:00",
            "2024-01-03 09:30:00",
            "2024-01-03 09:31:00",
        ]
    )
    result = resample_minute_data(source, 60, symbol="000001.SZ")
    assert len(result) == 3
    assert result["trade_time"].dt.strftime("%Y-%m-%d %H:%M").tolist() == [
        "2024-01-02 14:56",
        "2024-01-03 09:30",
        "2024-01-03 09:31",
    ]


@pytest.mark.parametrize(
    ("symbol", "trade_date"),
    [
        ("600000.SH", "2018-08-20"),
        ("510300.SH", "2026-07-06"),
        ("501018.SH", "2026-07-06"),
        ("000001.SZ", "2024-01-02"),
        ("830001.BJ", "2024-01-02"),
    ],
)
def test_closing_auction_is_one_independent_bar(symbol, trade_date):
    source = _frame(
        [
            f"{trade_date} 14:55:00",
            f"{trade_date} 14:56:00",
            f"{trade_date} 14:57:00",
            f"{trade_date} 14:58:00",
            f"{trade_date} 14:59:00",
            f"{trade_date} 15:00:00",
        ]
    )
    result = resample_minute_data(source, 60, symbol=symbol)
    assert len(result) == 2
    continuous, auction = result.iloc[0], result.iloc[1]
    assert continuous["trade_time"].strftime("%H:%M") == "14:57"
    assert auction["trade_time"].strftime("%H:%M") == "15:00"
    assert continuous["open"] == 1.0
    assert continuous["close"] == 3.25
    assert continuous["vol"] == 1 + 2 + 3
    assert auction["open"] == 4.0
    assert auction["high"] == 7.0
    assert auction["low"] == 3.5
    assert auction["close"] == 6.25
    assert auction["vol"] == 4 + 5 + 6
    assert auction["amount"] == 40 + 50 + 60
    assert auction["pre_close"] == 9.0


@pytest.mark.parametrize(
    ("symbol", "trade_date"),
    [
        ("600000.SH", "2018-08-19"),
        ("510300.SH", "2026-07-05"),
        ("501018.SH", "2026-07-05"),
    ],
)
def test_pre_change_sse_afternoon_remains_one_continuous_session(
    symbol, trade_date
):
    source = _frame(
        [
            f"{trade_date} 14:55:00",
            f"{trade_date} 14:57:00",
            f"{trade_date} 14:59:00",
            f"{trade_date} 15:00:00",
        ]
    )
    result = resample_minute_data(source, 60, symbol=symbol)
    assert len(result) == 1
    assert result.iloc[0]["trade_time"].strftime("%H:%M") == "15:00"
    assert result.iloc[0]["vol"] == 10


def test_strict_one_minute_still_combines_the_closing_auction():
    source = _frame(
        [
            "2024-01-02 14:56:00",
            "2024-01-02 14:57:00",
            "2024-01-02 14:58:00",
            "2024-01-02 15:00:00",
        ]
    )
    result = resample_minute_data(source, 1, symbol="000001.SZ")
    assert len(result) == 3
    assert result["trade_time"].dt.strftime("%H:%M").tolist() == [
        "14:56",
        "14:57",
        "15:00",
    ]
    assert result["vol"].tolist() == [1, 2, 3 + 4]


@pytest.mark.parametrize("terminal", ["14:58", "14:59"])
def test_strict_closing_auction_requires_terminal_1500_observation(terminal):
    source = _frame(
        [
            "2024-01-02 14:56:00",
            f"2024-01-02 {terminal}:00",
        ]
    )

    with pytest.raises(
            ValueError,
            match="closing-auction observations are truncated.*15:00"):
        resample_minute_data(source, 1, symbol="000001.SZ")


def test_1457_without_an_auction_observation_remains_continuous():
    source = _frame(
        [
            "2024-01-02 14:56:00",
            "2024-01-02 14:57:00",
        ]
    )

    result = resample_minute_data(source, 1, symbol="000001.SZ")

    assert result["trade_time"].dt.strftime("%H:%M").tolist() == [
        "14:56",
        "14:57",
    ]
    assert result["vol"].tolist() == [1, 2]


def test_one_minute_keeps_inclusive_continuous_endpoints_separate():
    morning = resample_minute_data(
        _frame(["2024-01-02 11:29:00", "2024-01-02 11:30:00"]),
        1,
        symbol="000001.SZ",
    )
    pre_auction_sse = resample_minute_data(
        _frame(["2018-08-19 14:59:00", "2018-08-19 15:00:00"]),
        1,
        symbol="600000.SH",
    )

    assert morning["trade_time"].dt.strftime("%H:%M").tolist() == [
        "11:29",
        "11:30",
    ]
    assert pre_auction_sse["trade_time"].dt.strftime("%H:%M").tolist() == [
        "14:59",
        "15:00",
    ]


def _full_minute_day(trade_date="2024-01-02"):
    morning = pd.date_range(
        f"{trade_date} 09:30:00", f"{trade_date} 11:30:00", freq="min"
    )
    afternoon = pd.date_range(
        f"{trade_date} 13:01:00", f"{trade_date} 15:00:00", freq="min"
    )
    times = [stamp.strftime("%Y-%m-%d %H:%M:%S") for stamp in (*morning, *afternoon)]
    assert len(times) == 241
    return _frame(times)


def test_real_shape_one_minute_day_preserves_each_continuous_bar_and_volume():
    source = _full_minute_day()

    result = resample_minute_data(source, 1, symbol="000001.SZ")

    # Three source auction observations become one audited auction bar.  No
    # continuous observation is folded into a neighbour or into the auction.
    assert len(result) == 239
    labels = result["trade_time"].dt.strftime("%H:%M").tolist()
    assert "11:29" in labels and "11:30" in labels
    assert "14:57" in labels
    assert "14:58" not in labels and "14:59" not in labels
    assert result["vol"].sum() == source["vol"].sum()
    assert result["amount"].sum() == source["amount"].sum()


@pytest.mark.parametrize(
    ("frequency", "first_afternoon_end"),
    [(5, "13:05"), (15, "13:15"), (30, "13:30"), (60, "14:00")],
)
def test_afternoon_buckets_anchor_on_first_real_endpoint(
    frequency, first_afternoon_end
):
    source = _full_minute_day()

    result = resample_minute_data(source, frequency, symbol="000001.SZ")

    afternoon = result[
        (result["trade_time"].dt.time >= pd.Timestamp("13:01").time())
        & (result["trade_time"].dt.time <= pd.Timestamp("14:57").time())
    ]
    assert afternoon.iloc[0]["trade_time"].strftime("%H:%M") == first_afternoon_end
    assert result["vol"].sum() == source["vol"].sum()
    assert result["amount"].sum() == source["amount"].sum()


def test_strict_result_records_reproducible_rule_metadata():
    result = resample_minute_data(
        _frame(["2024-01-02 09:30:00", "2024-01-02 09:31:00"]),
        5,
        symbol="000001.SZ",
    )
    assert result.attrs["session_resample_mode"] == "strict"
    assert result.attrs["session_source_version"] == SESSION_SOURCE_VERSION
    assert result.attrs["session_snapshot_hash"] == SESSION_SNAPSHOT_HASH


def test_compatibility_mode_isolates_lunch_date_and_exact_1500_row():
    source = _frame(
        [
            "2024-01-02 11:29:00",
            "2024-01-02 11:30:00",
            "2024-01-02 13:00:00",
            "2024-01-02 14:59:00",
            "2024-01-02 15:00:00",
            "2024-01-03 09:30:00",
        ]
    )
    result = resample_minute_data(source, 240)
    assert len(result) == 4
    assert result["trade_time"].dt.strftime("%Y-%m-%d %H:%M").tolist() == [
        "2024-01-02 11:30",
        "2024-01-02 14:59",
        "2024-01-02 15:00",
        "2024-01-03 09:30",
    ]
    assert result.attrs["session_resample_mode"] == "compatibility"
    assert result.attrs["session_source_version"] == "legacy-session-compat-v1"
    assert result.attrs["session_snapshot_hash"] is None


def test_compatibility_mode_also_isolates_exact_0930_row():
    source = _frame(
        ["2024-01-02 09:30:00", "2024-01-02 09:31:00"]
    )
    result = resample_minute_data(source, 60)
    assert result["trade_time"].dt.strftime("%H:%M").tolist() == [
        "09:30",
        "09:31",
    ]


def test_compatibility_one_minute_preserves_legacy_shape_and_values():
    source = _frame(["2024-01-02 09:30:00", "2024-01-02 15:00:00"])
    original = source.copy(deep=True)
    result = resample_minute_data(source, 1)
    pd.testing.assert_frame_equal(result, original)
    assert result is not source
    assert result.attrs["session_resample_mode"] == "compatibility"


def test_time_index_input_is_supported_without_cross_session_grouping():
    source = _frame(
        [
            "2024-01-02 11:30:00",
            "2024-01-02 13:01:00",
        ],
        as_index=True,
    )
    result = resample_minute_data(source, 60, symbol="000001.SZ")
    assert "trade_time" in result.columns
    assert len(result) == 2
    assert result.attrs["session_time_source"] == "index"


@pytest.mark.parametrize(
    "times",
    [
        ["2024-01-02 09:30:00", "2024-01-02 09:30:00"],
        ["2024-01-02 09:31:00", "2024-01-02 09:30:00"],
    ],
)
def test_duplicate_and_unsorted_timestamps_fail_before_aggregation(times):
    with pytest.raises(ValueError, match="duplicate|strictly increasing"):
        resample_minute_data(_frame(times), 5, symbol="000001.SZ")


@pytest.mark.parametrize(
    "stamp",
    [
        "2024-01-02 09:29:00",
        "2024-01-02 11:31:00",
        "2024-01-02 12:59:00",
        "2024-01-02 15:01:00",
    ],
)
def test_strict_off_session_rows_fail_before_aggregation(stamp):
    with pytest.raises(TimestampOutsideSessionError):
        resample_minute_data(_frame([stamp]), 5, symbol="000001.SZ")


def test_non_minute_aligned_and_timezone_aware_rows_fail():
    with pytest.raises(ValueError, match="whole minutes"):
        resample_minute_data(
            _frame(["2024-01-02 09:30:01"]), 5, symbol="000001.SZ"
        )
    aware = _frame([pd.Timestamp("2024-01-02 09:30:00", tz="Asia/Shanghai")])
    with pytest.raises(ValueError, match="timezone-naive"):
        resample_minute_data(aware, 5, symbol="000001.SZ")


@pytest.mark.parametrize(
    "symbol",
    ["000300.SH", "508000.SH", "IF2406.CFFEX", "not-a-symbol"],
)
def test_unknown_or_unsupported_symbols_fail_even_for_empty_frames(symbol):
    empty = pd.DataFrame(columns=["trade_time", "open", "high", "low", "close"])
    with pytest.raises(SessionRuleNotFoundError):
        resample_minute_data(empty, 5, symbol=symbol)


@pytest.mark.parametrize(
    ("symbol", "stamp"),
    [
        ("000001.SZ", "2006-06-30 09:30:00"),
        ("600000.SH", "2006-06-30 09:30:00"),
        ("830001.BJ", "2021-11-14 09:30:00"),
    ],
)
def test_uncovered_historical_dates_fail_fast(symbol, stamp):
    with pytest.raises(SessionRuleNotFoundError, match="no effective"):
        resample_minute_data(_frame([stamp]), 5, symbol=symbol)


@pytest.mark.parametrize("frequency", [0, -1, 1.0, True, "5"])
def test_invalid_frequency_is_rejected(frequency):
    with pytest.raises((TypeError, ValueError)):
        resample_minute_data(
            _frame(["2024-01-02 09:30:00"]),
            frequency,
            symbol="000001.SZ",
        )


@pytest.mark.parametrize(
    ("label", "minutes"),
    [
        ("minute", 1),
        ("1min", 1),
        ("5min", 5),
        ("15min", 15),
        ("30min", 30),
        ("60min", 60),
    ],
)
def test_resample_config_keeps_legacy_frequency_names(label, minutes):
    assert ResampleConfig.from_freq_str(label).freq_minutes == minutes


def test_source_frame_is_not_mutated_by_aggregation():
    source = _frame(["2024-01-02 09:30:00", "2024-01-02 09:31:00"])
    original = source.copy(deep=True)
    resample_minute_data(source, 5, symbol="000001.SZ")
    pd.testing.assert_frame_equal(source, original)
