"""Session-aware aggregation of one-minute cash-market bars.

Strict mode is selected by passing ``symbol``.  Every timestamp is then routed
through the effective-dated :mod:`diepi.backtest.session_calendar`; unknown
instruments, uncovered dates, off-session timestamps, duplicates, and unsorted
input fail before aggregation.

Calls without ``symbol`` retain the legacy API as an explicit compatibility
mode.  Compatibility mode still isolates dates, the lunch break, and an exact
15:00 row, but it cannot make venue/instrument-specific historical claims.  The
returned frame records the selected mode in ``DataFrame.attrs``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from numbers import Integral
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..session_calendar import (
    DEFAULT_SESSION_CALENDAR,
    SessionCalendar,
    SessionKind,
    SessionWindow,
)


_INTERNAL_TIME = "__diepi_trade_time__"
_INTERNAL_GROUP = "__diepi_session_group__"
_COMPATIBILITY_VERSION = "legacy-session-compat-v1"


@dataclass(frozen=True)
class ResampleConfig:
    """Validated target bar frequency."""

    freq_minutes: int

    def __post_init__(self) -> None:
        _validate_frequency(self.freq_minutes)

    @classmethod
    def from_freq_str(cls, freq: str) -> "ResampleConfig":
        if type(freq) is not str:
            raise TypeError("freq must be exactly str")
        freq_map = {
            "1min": 1,
            "5min": 5,
            "15min": 15,
            "30min": 30,
            "60min": 60,
            "minute": 1,
        }
        minutes = freq_map.get(freq.lower())
        if minutes is None:
            raise ValueError(
                "unsupported frequency: %s; supported: %s"
                % (freq, list(freq_map.keys()))
            )
        return cls(freq_minutes=minutes)


def _validate_frequency(freq_minutes: Any) -> int:
    if isinstance(freq_minutes, bool) or not isinstance(freq_minutes, Integral):
        raise TypeError("freq_minutes must be an integer")
    value = int(freq_minutes)
    if value <= 0:
        raise ValueError("freq_minutes must be positive")
    return value


def _prepare_frame_and_times(
    df: pd.DataFrame, time_col: str
) -> Tuple[pd.DataFrame, pd.Series, bool]:
    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame")
    if type(time_col) is not str or not time_col:
        raise ValueError("time_col must be a non-empty string")
    if not df.columns.is_unique:
        raise ValueError("input columns must be unique")
    if _INTERNAL_TIME in df.columns or _INTERNAL_GROUP in df.columns:
        raise ValueError("input contains a reserved resampler column")

    time_in_column = time_col in df.columns
    raw_times = df[time_col] if time_in_column else pd.Series(df.index, index=df.index)
    try:
        converted = pd.to_datetime(raw_times, errors="raise")
    except Exception as exc:
        raise ValueError("minute timestamps are not parseable") from exc

    if isinstance(converted, pd.DatetimeIndex):
        timestamps = pd.Series(converted, index=df.index)
    else:
        timestamps = pd.Series(converted.array, index=df.index)
    if timestamps.isna().any():
        raise ValueError("minute timestamps must not contain NaT")
    try:
        timezone = timestamps.dt.tz
    except AttributeError as exc:
        raise ValueError("minute timestamps must share one datetime type") from exc
    if timezone is not None:
        raise ValueError("minute timestamps must be timezone-naive")
    if timestamps.duplicated().any():
        duplicates = timestamps[timestamps.duplicated(keep=False)]
        sample = [value.isoformat() for value in duplicates.iloc[:3]]
        raise ValueError(f"duplicate minute timestamps: {sample}")
    if not timestamps.is_monotonic_increasing:
        raise ValueError("minute timestamps must be strictly increasing")
    if ((timestamps.dt.second != 0) | (timestamps.dt.microsecond != 0)).any():
        raise ValueError("minute timestamps must be aligned to whole minutes")

    working = df.copy()
    if time_in_column:
        working = working.drop(columns=[time_col])
    else:
        working = working.reset_index(drop=True)
        timestamps = timestamps.reset_index(drop=True)
    working[_INTERNAL_TIME] = timestamps.to_numpy()
    return working, timestamps.reset_index(drop=True), time_in_column


def _minute_number(value: time) -> int:
    return value.hour * 60 + value.minute


def _continuous_bucket(
    timestamp: pd.Timestamp,
    session: SessionWindow,
    freq_minutes: int,
) -> int:
    offset = _minute_number(timestamp.time()) - _minute_number(session.start_time)
    # Keep a one-minute bar at an inclusive endpoint independent.  For wider
    # legacy buckets the endpoint fold remains harmless with the minute-end
    # anchors and preserves the established grouping contract.
    if (
        freq_minutes > 1
        and session.include_end
        and timestamp.time() == session.end_time
    ):
        offset -= 1
    if offset < 0:
        raise AssertionError("session membership produced a negative offset")
    return offset // freq_minutes


def _strict_group_keys(
    timestamps: pd.Series,
    freq_minutes: int,
    symbol: str,
    metadata: Optional[Any],
    calendar: SessionCalendar,
) -> Tuple[pd.Series, Tuple[str, ...], Tuple[str, ...]]:
    keys = []
    source_versions = set()
    snapshot_hashes = set()
    aggregate_groups = {}
    for raw_timestamp in timestamps:
        timestamp = pd.Timestamp(raw_timestamp)
        rule, session = calendar.session_for_timestamp(
            symbol, timestamp, metadata=metadata
        )
        source_versions.add(rule.source_version)
        snapshot_hashes.add(rule.snapshot_hash)
        bucket = (
            0
            if session.aggregate_as_single_bar
            else _continuous_bucket(timestamp, session, freq_minutes)
        )
        keys.append(
            (timestamp.date().isoformat(), session.sequence, session.name.value, bucket)
        )
        if session.aggregate_as_single_bar:
            aggregate_key = (
                timestamp.date().isoformat(),
                session.sequence,
                session.name.value,
            )
            entry = aggregate_groups.setdefault(
                aggregate_key, (session, []))
            entry[1].append(timestamp)

    for session, observations in aggregate_groups.values():
        if session.kind is SessionKind.CLOSING_AUCTION:
            terminal = max(observations).time()
            if terminal != session.end_time:
                raise ValueError(
                    "closing-auction observations are truncated: "
                    f"expected terminal timestamp {session.end}, "
                    f"got {terminal.strftime('%H:%M')}"
                )
    return (
        pd.Series(keys, index=timestamps.index, dtype=object),
        tuple(sorted(source_versions)),
        tuple(sorted(snapshot_hashes)),
    )


def _compatibility_session(timestamp: pd.Timestamp) -> Tuple[int, str, int, bool]:
    value = timestamp.time()
    opening_stamp = time(9, 30)
    morning_start = time(9, 31)
    morning_end = time(11, 30)
    afternoon_start = time(13, 0)
    close_stamp = time(15, 0)
    if value == opening_stamp:
        return 0, "compat_open_0930", _minute_number(opening_stamp), False
    if morning_start <= value <= morning_end:
        return 1, "compat_morning", _minute_number(morning_start), value == morning_end
    if afternoon_start <= value < close_stamp:
        return 2, "compat_afternoon", _minute_number(afternoon_start), False
    if value == close_stamp:
        return 3, "compat_close_1500", _minute_number(close_stamp), False
    raise ValueError(
        "timestamp %s is outside compatibility cash sessions"
        % timestamp.isoformat(sep=" ")
    )


def _compatibility_group_keys(
    timestamps: pd.Series, freq_minutes: int
) -> pd.Series:
    keys = []
    for raw_timestamp in timestamps:
        timestamp = pd.Timestamp(raw_timestamp)
        sequence, name, start_minute, inclusive_end = _compatibility_session(timestamp)
        if name in ("compat_open_0930", "compat_close_1500"):
            bucket = 0
        else:
            offset = _minute_number(timestamp.time()) - start_minute
            if inclusive_end:
                offset -= 1
            bucket = offset // freq_minutes
        keys.append((timestamp.date().isoformat(), sequence, name, bucket))
    return pd.Series(keys, index=timestamps.index, dtype=object)


def resample_minute_data(
    df: pd.DataFrame,
    freq_minutes: int,
    time_col: str = "trade_time",
    symbol: Optional[str] = None,
    *,
    metadata: Optional[Any] = None,
    session_calendar: Optional[SessionCalendar] = None,
) -> pd.DataFrame:
    """Aggregate minute bars without crossing a date or trading session.

    Passing ``symbol`` selects strict effective-dated behavior.  Omitting it is
    supported only for legacy callers and marks the output with
    ``session_resample_mode='compatibility'``.
    """

    frequency = _validate_frequency(freq_minutes)
    if not isinstance(df, pd.DataFrame):
        raise TypeError("df must be a pandas DataFrame")
    calendar = session_calendar or DEFAULT_SESSION_CALENDAR
    if not isinstance(calendar, SessionCalendar):
        raise TypeError("session_calendar must be a SessionCalendar")
    strict = symbol is not None
    if strict:
        if type(symbol) is not str or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        calendar.validate_symbol(symbol, metadata=metadata)

    if df.empty:
        result = df.copy()
        result.attrs.update(
            {
                "session_resample_mode": "strict" if strict else "compatibility",
                "session_source_version": (
                    None if strict else _COMPATIBILITY_VERSION
                ),
                "session_snapshot_hash": None,
            }
        )
        return result

    missing_ohlc = [
        name for name in ("open", "high", "low", "close") if name not in df.columns
    ]
    if missing_ohlc:
        raise ValueError(f"minute data missing OHLC columns: {missing_ohlc}")

    working, timestamps, time_in_column = _prepare_frame_and_times(df, time_col)
    if strict:
        groups, source_versions, snapshot_hashes = _strict_group_keys(
            timestamps,
            frequency,
            symbol,
            metadata,
            calendar,
        )
    else:
        groups = _compatibility_group_keys(timestamps, frequency)
        source_versions = (_COMPATIBILITY_VERSION,)
        snapshot_hashes = ()

    # The legacy 1-minute call returned the original shape.  Preserve that only
    # in compatibility mode after validating timestamps.  Strict mode still
    # combines all closing-auction observations into one independent bar.
    if frequency == 1 and not strict:
        result = df.copy()
        result.attrs.update(
            {
                "session_resample_mode": "compatibility",
                "session_source_version": _COMPATIBILITY_VERSION,
                "session_snapshot_hash": None,
            }
        )
        return result

    working[_INTERNAL_GROUP] = groups.to_numpy()
    aggregation = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
    }
    if "vol" in working.columns:
        aggregation["vol"] = "sum"
    if "volume" in working.columns:
        aggregation["volume"] = "sum"
    if "amount" in working.columns:
        aggregation["amount"] = "sum"
    if "pre_close" in working.columns:
        aggregation["pre_close"] = "first"
    aggregation[_INTERNAL_TIME] = "last"

    result = (
        working.groupby(_INTERNAL_GROUP, sort=False, dropna=False)
        .agg(aggregation)
        .reset_index(drop=True)
        .rename(columns={_INTERNAL_TIME: "trade_time"})
    )
    result.attrs.update(
        {
            "session_resample_mode": "strict" if strict else "compatibility",
            "session_source_version": ",".join(source_versions),
            "session_snapshot_hash": (
                ",".join(snapshot_hashes) if snapshot_hashes else None
            ),
            "session_time_source": "column" if time_in_column else "index",
        }
    )
    return result


def _compute_time_groups(time_series: pd.Series, freq_minutes: int) -> pd.Series:
    """Compatibility-only grouping helper retained for older imports."""

    frequency = _validate_frequency(freq_minutes)
    converted = pd.Series(pd.to_datetime(time_series, errors="raise"))
    if converted.duplicated().any():
        raise ValueError("duplicate minute timestamps")
    if not converted.is_monotonic_increasing:
        raise ValueError("minute timestamps must be strictly increasing")
    return _compatibility_group_keys(converted, frequency)


def build_time_index(df: pd.DataFrame, time_col: str = "trade_time") -> Dict:
    """Build a timestamp-to-row-position index."""

    if df.empty:
        return {}
    times = df[time_col].tolist() if time_col in df.columns else df.index.tolist()
    return {value: index for index, value in enumerate(times)}


def get_bars_at_times_batch(
    df: pd.DataFrame,
    time_index: Dict,
    times: List,
    time_col: str = "trade_time",
) -> Dict:
    """Fetch multiple rows by a pre-built positional time index."""

    result = {}
    for value in times:
        index = time_index.get(value)
        if index is not None:
            result[value] = df.iloc[index]
    return result


__all__ = [
    "ResampleConfig",
    "build_time_index",
    "get_bars_at_times_batch",
    "resample_minute_data",
]
