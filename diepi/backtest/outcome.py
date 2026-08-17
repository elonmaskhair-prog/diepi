"""Runtime evidence builder for :mod:`diepi.backtest.result_contract`.

This is intentionally not a legacy-result adapter.  It only records evidence
at the moment an engine observes it, so an empty payload can never be guessed
to mean either success or failure after the fact.
"""

from __future__ import annotations

from datetime import date, datetime
from math import isfinite
import re
from typing import Dict, Iterable, Optional, Set, Tuple

from .result_contract import (
    ActualInterval,
    DataCoverage,
    ResultAssumption,
    ResultContract,
    ResultReason,
    ResultStatus,
    ResultWarning,
)


_COMPACT_DATE_RE = re.compile(r"^\d{8}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COVERAGE_UNIT_KEY = "data_coverage.unit"


def _canonical_date(value: object, name: str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise ValueError(f"{name} must be timezone-naive")
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif type(value) is str:
        text = value
        if _COMPACT_DATE_RE.fullmatch(text):
            text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
        elif not _ISO_DATE_RE.fullmatch(text):
            raise ValueError(
                f"{name} must be a valid YYYYMMDD or YYYY-MM-DD"
            )
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            raise ValueError(
                f"{name} must be a valid YYYYMMDD or YYYY-MM-DD"
            ) from None
    else:
        raise TypeError(f"{name} must be str, date, or datetime")
    return parsed.isoformat()


def _canonical_observation_id(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exactly str")
    canonical = value.strip()
    if not canonical:
        raise ValueError(f"{name} must be non-empty after stripping")
    return canonical


class OutcomeTracker:
    """Mutable per-run evidence that freezes into a ``ResultContract``."""

    def __init__(
        self,
        *,
        expected_observations: Optional[int] = None,
        expected_observation_ids: Optional[Iterable[str]] = None,
        coverage_unit: Optional[str] = None,
    ) -> None:
        self._expected_observations: Optional[int] = None
        self._expected_observation_ids: Optional[frozenset[str]] = None
        self._observations: Set[str] = set()
        self._observation_dates: Dict[str, str] = {}
        self._warnings: Dict[str, ResultWarning] = {}
        self._assumptions: Dict[str, ResultAssumption] = {}
        self._built = False
        if (
            expected_observations is not None
            and expected_observation_ids is not None
        ):
            raise ValueError(
                "expected_observations and expected_observation_ids are "
                "mutually exclusive"
            )
        if coverage_unit is not None:
            self.set_coverage_unit(coverage_unit)
        if expected_observations is not None:
            self.set_expected_observations(expected_observations)
        if expected_observation_ids is not None:
            self.set_expected_scope(expected_observation_ids)

    @property
    def expected_observations(self) -> Optional[int]:
        return self._expected_observations

    @property
    def actual_observations(self) -> int:
        return len(self._observations)

    @property
    def expected_observation_ids(self) -> Optional[Tuple[str, ...]]:
        if self._expected_observation_ids is None:
            return None
        return tuple(sorted(self._expected_observation_ids))

    def _ensure_open(self) -> None:
        if self._built:
            raise RuntimeError("outcome tracker has already been finalized")

    def set_expected_observations(self, count: int) -> None:
        self._ensure_open()
        if type(count) is not int:
            raise TypeError("expected observations must be exactly int")
        if count < 0:
            raise ValueError("expected observations must be non-negative")
        if count < self.actual_observations:
            raise ValueError("expected observations cannot be below actual observations")
        if self._expected_observations is not None:
            raise RuntimeError("expected observations have already been set")
        self._expected_observations = count

    def set_expected_scope(self, observation_ids: Iterable[str]) -> None:
        """Declare the exact observation identities required for completion."""

        self._ensure_open()
        if self._expected_observations is not None:
            raise RuntimeError("expected observations have already been set")
        if isinstance(observation_ids, (str, bytes)):
            raise TypeError("expected observation IDs must be an iterable of str")
        try:
            iterator = iter(observation_ids)
        except TypeError:
            raise TypeError(
                "expected observation IDs must be an iterable of str"
            ) from None

        canonical_ids = []
        seen = set()
        for index, observation_id in enumerate(iterator):
            canonical = _canonical_observation_id(
                observation_id, f"expected_observation_ids[{index}]"
            )
            if canonical in seen:
                raise ValueError(
                    f"duplicate expected observation_id: {canonical!r}"
                )
            seen.add(canonical)
            canonical_ids.append(canonical)
        expected = frozenset(canonical_ids)
        unexpected_observed = self._observations - expected
        if unexpected_observed:
            raise ValueError(
                "observed IDs are outside the expected scope: "
                f"{sorted(unexpected_observed)!r}"
            )
        self._expected_observation_ids = expected
        self._expected_observations = len(expected)

    def set_coverage_unit(self, value: str) -> None:
        """Declare what one expected/actual observation represents."""

        self._ensure_open()
        self.add_assumption(_COVERAGE_UNIT_KEY, value)

    def observe(self, observation_id: str, observation_date: object) -> None:
        """Record one unique completed observation.

        ``observation_id`` should encode the coverage unit, for example
        ``symbol|timestamp``.  Duplicate IDs fail instead of inflating coverage.
        """

        self._ensure_open()
        canonical_id = _canonical_observation_id(
            observation_id, "observation_id"
        )
        if canonical_id in self._observations:
            raise ValueError(f"duplicate observation_id: {canonical_id!r}")
        if (
            self._expected_observation_ids is not None
            and canonical_id not in self._expected_observation_ids
        ):
            raise ValueError(
                f"observation_id is outside expected scope: {canonical_id!r}"
            )
        if (
            self._expected_observations is not None
            and self.actual_observations >= self._expected_observations
        ):
            raise ValueError("actual observations would exceed expected observations")
        canonical = _canonical_date(observation_date, "observation_date")
        self._observations.add(canonical_id)
        self._observation_dates[canonical_id] = canonical

    def add_warning(self, code: str, message: str) -> None:
        self._ensure_open()
        warning = ResultWarning(code, message)
        if warning.code in self._warnings:
            raise ValueError(f"duplicate warning code: {warning.code}")
        self._warnings[warning.code] = warning

    def add_assumption(self, key: str, value: str) -> None:
        self._ensure_open()
        assumption = ResultAssumption(key, value)
        if assumption.key in self._assumptions:
            raise ValueError(f"duplicate assumption key: {assumption.key}")
        self._assumptions[assumption.key] = assumption

    def _require_coverage_unit(self) -> None:
        if _COVERAGE_UNIT_KEY not in self._assumptions:
            raise RuntimeError(
                "data coverage requires an explicit coverage unit assumption"
            )

    def _progress(self) -> Tuple[Optional[ActualInterval], Optional[DataCoverage]]:
        actual = self.actual_observations
        if actual and self._expected_observations is None:
            raise RuntimeError(
                "expected observations must be set before finalizing observed data"
            )
        if actual or self._expected_observations is not None:
            self._require_coverage_unit()
        interval = None
        if actual:
            dates = tuple(self._observation_dates.values())
            interval = ActualInterval(min(dates), max(dates))
        coverage = None
        if self._expected_observations is not None:
            expected = self._expected_observations
            ratio = actual / expected if expected else 0.0
            if not isfinite(ratio):
                raise ValueError("coverage ratio must be finite")
            coverage = DataCoverage(expected, actual, float(ratio))
        return interval, coverage

    def finalize(
        self,
        status: ResultStatus,
        *,
        reason_code: Optional[str] = None,
        reason_message: Optional[str] = None,
    ) -> ResultContract:
        self._ensure_open()
        if type(status) is not ResultStatus:
            raise TypeError("status must be exactly ResultStatus")
        if status is ResultStatus.SUCCESS:
            if self._expected_observation_ids is None:
                raise RuntimeError(
                    "SUCCESS requires an explicit expected observation ID scope"
                )
            if self._observations != self._expected_observation_ids:
                missing = sorted(
                    self._expected_observation_ids - self._observations
                )
                unexpected = sorted(
                    self._observations - self._expected_observation_ids
                )
                raise ValueError(
                    "SUCCESS requires exact expected observation IDs; "
                    f"missing={missing!r}, unexpected={unexpected!r}"
                )
        if (reason_code is None) != (reason_message is None):
            raise ValueError("reason_code and reason_message must be supplied together")
        reason = (
            None
            if reason_code is None
            else ResultReason(reason_code, reason_message)
        )
        interval, coverage = self._progress()
        contract = ResultContract(
            status=status,
            reason=reason,
            warnings=tuple(self._warnings[key] for key in sorted(self._warnings)),
            assumptions=tuple(
                self._assumptions[key] for key in sorted(self._assumptions)
            ),
            actual_interval=interval,
            data_coverage=coverage,
        )
        self._built = True
        return contract

    def finalize_completed(
        self,
        *,
        incomplete_reason_code: str = "INCOMPLETE_DATA_COVERAGE",
        incomplete_reason_message: str = "Observed data coverage is incomplete",
    ) -> ResultContract:
        """Return SUCCESS only for non-empty complete coverage."""

        if self._expected_observations is None:
            raise RuntimeError("expected observations are required for completion")
        if self._expected_observations == 0:
            return self.finalize(
                ResultStatus.INVALID,
                reason_code="NO_EXPECTED_OBSERVATIONS",
                reason_message="The requested run has no expected observations",
            )
        if (
            self._expected_observation_ids is not None
            and self._observations == self._expected_observation_ids
        ):
            return self.finalize(ResultStatus.SUCCESS)
        if self.actual_observations == self._expected_observations:
            raise RuntimeError(
                "complete counts cannot prove SUCCESS without an explicit "
                "expected observation ID scope"
            )
        if self.actual_observations == 0:
            return self.finalize(
                ResultStatus.INVALID,
                reason_code="MARKET_DATA_UNAVAILABLE",
                reason_message="No valid market-data observations were completed",
            )
        return self.finalize(
            ResultStatus.PARTIAL,
            reason_code=incomplete_reason_code,
            reason_message=incomplete_reason_message,
        )


def add_trade_calendar_assumptions(
    tracker: OutcomeTracker, provider: object
) -> bool:
    """Attach the selected market-clock identity to reproducibility evidence.

    Custom legacy providers may not expose an identity; returning ``False``
    lets their existing compatibility path remain usable.  A real provider
    that exposes the property but cannot resolve it still raises, preserving
    fail-closed calendar semantics.
    """

    if not hasattr(type(provider), "trade_calendar_identity") and not hasattr(
        provider, "trade_calendar_identity"
    ):
        return False
    identity = provider.trade_calendar_identity
    assumptions = getattr(identity, "assumptions", None)
    if not callable(assumptions):
        raise TypeError(
            "trade_calendar_identity must expose assumptions()"
        )
    for key, value in assumptions():
        tracker.add_assumption(key, value)
    return True


__all__ = ["OutcomeTracker", "add_trade_calendar_assumptions"]
