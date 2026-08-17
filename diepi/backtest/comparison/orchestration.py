"""Strict orchestration helpers for result-level comparisons.

The helpers in this module deliberately do not infer a comparison window from
requested dates.  The strategy's completed ``daily_values`` observations are
the only source of truth for scope, and no failed comparison is converted to a
numeric zero.
"""

from __future__ import annotations

from datetime import date, datetime
import math
import re
from typing import Any, Optional, TYPE_CHECKING

import pandas as pd

from .models import (
    ComparisonBundle,
    ComparisonReason,
    ComparisonScope,
    ComparisonStatus,
    ReferenceIndexComparisonError,
    ReferenceIndexResult,
    ReferenceIndexSpec,
    TotalReturnIndexSeries,
)
from ..result_contract import ResultContract, ResultStatus

if TYPE_CHECKING:  # Avoid importing the data package during engine imports.
    from ..data.index_provider import IndexProvider


_YYYYMMDD_RE = re.compile(r"^\d{8}$")
_COVERAGE_UNIT_KEY = "data_coverage.unit"
_PORTFOLIO_DAILY_COVERAGE_UNIT = "portfolio_trade_day"


def _canonical_daily_observation(value: Any, position: int) -> str:
    path = "daily_values.index[{}]".format(position)

    if value is pd.NaT or value is pd.NA:
        raise ValueError("{} must not be NaT".format(path))

    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            raise ValueError("{} must not be NaT".format(path))
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise ValueError("{} must be timezone-naive".format(path))
        if any(
            (
                value.hour,
                value.minute,
                value.second,
                value.microsecond,
                value.nanosecond,
            )
        ):
            raise ValueError("{} must identify a completed whole day".format(path))
        return value.strftime("%Y%m%d")

    if type(value) is datetime:
        if value.tzinfo is not None and value.utcoffset() is not None:
            raise ValueError("{} must be timezone-naive".format(path))
        if any((value.hour, value.minute, value.second, value.microsecond)):
            raise ValueError("{} must identify a completed whole day".format(path))
        return value.strftime("%Y%m%d")

    if type(value) is date:
        return value.strftime("%Y%m%d")

    if type(value) is str:
        if not _YYYYMMDD_RE.fullmatch(value):
            raise ValueError("{} must use exact YYYYMMDD".format(path))
        try:
            parsed = datetime.strptime(value, "%Y%m%d")
        except ValueError:
            raise ValueError("{} is not a valid calendar date".format(path)) from None
        if parsed.strftime("%Y%m%d") != value:
            raise ValueError("{} must use canonical YYYYMMDD".format(path))
        return value

    raise TypeError(
        "{} must be a date, datetime, pandas Timestamp, or exact "
        "YYYYMMDD str".format(path)
    )


def comparison_scope_from_daily_values(
    daily_values: pd.DataFrame,
) -> ComparisonScope:
    """Build the one exact comparison scope from completed strategy NAV rows.

    Every index entry is consumed in its original order.  The function never
    sorts, de-duplicates, intersects, forward-fills, or derives observations
    from the requested start/end dates.
    """

    if type(daily_values) is not pd.DataFrame:
        raise TypeError("daily_values must be exactly pandas.DataFrame")
    if len(daily_values.index) == 0:
        raise ValueError("daily_values must contain completed observations")
    observation_ids = tuple(
        _canonical_daily_observation(value, position)
        for position, value in enumerate(daily_values.index)
    )
    return ComparisonScope(observation_ids=observation_ids)


def _unexpected_provider_reason(error: Exception) -> ComparisonReason:
    error_type = type(error).__name__
    safe_text = "".join(
        (
            character
            if ord(character) >= 32
            and not 0xD800 <= ord(character) <= 0xDFFF
            else " "
        )
        for character in str(error)
    )
    detail = " ".join(safe_text.split())
    message = "reference total-return provider raised {}".format(error_type)
    if detail:
        message = "{}: {}".format(message, detail)
    return ComparisonReason(
        code="REFERENCE_TOTAL_RETURN_PROVIDER_FAILED",
        message=message,
    )


def build_reference_index_result(
    spec: ReferenceIndexSpec,
    provider: "IndexProvider",
    scope: ComparisonScope,
) -> ReferenceIndexResult:
    """Resolve one explicit total-return reference leg on ``scope``.

    Structured availability/coverage/data errors retain their declared
    UNAVAILABLE/PARTIAL/INVALID status.  Unexpected provider failures become a
    FAILED leg with an explicit reason; they are never represented as zero
    return.
    """

    if type(spec) is not ReferenceIndexSpec:
        raise TypeError("spec must be exactly ReferenceIndexSpec")
    if type(scope) is not ComparisonScope:
        raise TypeError("scope must be exactly ComparisonScope")
    method = getattr(provider, "get_total_return_series", None)
    if not callable(method):
        raise TypeError(
            "provider must expose callable get_total_return_series"
        )

    try:
        series = method(spec, scope)
        if type(series) is not TotalReturnIndexSeries:
            raise TypeError(
                "provider.get_total_return_series must return exactly "
                "TotalReturnIndexSeries"
            )
        if series.spec != spec or series.scope != scope:
            raise ValueError(
                "provider returned a total-return series for a different "
                "spec or scope"
            )
        return ReferenceIndexResult.from_series(series)
    except ReferenceIndexComparisonError as error:
        return ReferenceIndexResult.from_error(spec, scope, error)
    except Exception as error:
        return ReferenceIndexResult(
            status=ComparisonStatus.FAILED,
            spec=spec,
            scope=scope,
            reason=_unexpected_provider_reason(error),
        )


def reference_total_return_excess(
    *,
    strategy_total_return: Any,
    result_contract: Optional[ResultContract],
    daily_values: pd.DataFrame,
    comparisons: Optional[ComparisonBundle],
) -> Optional[float]:
    """Return strategy minus reference total return only when comparable.

    Comparability requires a successful strategy contract, a successful
    explicit total-return reference leg, and byte-for-byte-equivalent logical
    scopes derived from the actual strategy NAV observations.
    """

    if (
        type(result_contract) is not ResultContract
        or result_contract.status is not ResultStatus.SUCCESS
    ):
        return None
    if type(comparisons) is not ComparisonBundle:
        return None
    if type(strategy_total_return) not in (int, float):
        return None
    strategy_return = float(strategy_total_return)
    if not math.isfinite(strategy_return):
        return None

    try:
        actual_scope = comparison_scope_from_daily_values(daily_values)
    except (TypeError, ValueError):
        return None
    interval = result_contract.actual_interval
    coverage = result_contract.data_coverage
    if interval is None or coverage is None:
        return None
    expected_start = "{}-{}-{}".format(
        actual_scope.start_date[:4],
        actual_scope.start_date[4:6],
        actual_scope.start_date[6:],
    )
    expected_end = "{}-{}-{}".format(
        actual_scope.end_date[:4],
        actual_scope.end_date[4:6],
        actual_scope.end_date[6:],
    )
    if (
        interval.start_date != expected_start
        or interval.end_date != expected_end
    ):
        return None
    coverage_units = tuple(
        assumption.value
        for assumption in result_contract.assumptions
        if assumption.key == _COVERAGE_UNIT_KEY
    )
    if coverage_units != (_PORTFOLIO_DAILY_COVERAGE_UNIT,):
        return None
    if (
        coverage.actual_observations != len(actual_scope.observation_ids)
        or coverage.expected_observations != len(actual_scope.observation_ids)
    ):
        return None
    if comparisons.scope != actual_scope:
        return None

    reference = comparisons.reference_index_total_return
    if (
        type(reference) is not ReferenceIndexResult
        or reference.status is not ComparisonStatus.SUCCESS
        or reference.scope != actual_scope
    ):
        return None
    reference_return = reference.total_return
    if reference_return is None or not math.isfinite(reference_return):
        return None
    result = strategy_return - reference_return
    return result if math.isfinite(result) else None


__all__ = [
    "build_reference_index_result",
    "comparison_scope_from_daily_values",
    "reference_total_return_excess",
]
