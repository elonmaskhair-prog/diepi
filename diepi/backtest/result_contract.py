"""Stable, deterministic status envelope for backtest results.

This module intentionally contains no engine, metric, pandas, persistence, or
wall-clock dependency.  Engines can wrap their domain-specific payloads with
``ResultContract`` without changing how those payloads are calculated.

Ordering is part of the public contract: warnings and assumptions retain the
exact tuple order supplied by the producer.  Duplicate warning codes and
assumption keys are rejected so that consumers never have to choose between
conflicting entries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
import json
import math
import re
from typing import Any, Dict, Optional, Tuple


SCHEMA_VERSION = 1
SEMANTICS_VERSION = "v1"

_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_KEY_RE = re.compile(
    r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*(?:\."
    r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*)*$"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_RESULT_CONTRACT_KEYS = frozenset(
    {
        "schema_version",
        "semantics_version",
        "status",
        "rankable",
        "reason",
        "warnings",
        "assumptions",
        "actual_interval",
        "data_coverage",
    }
)
_DIAGNOSTIC_KEYS = frozenset({"code", "message"})
_ASSUMPTION_KEYS = frozenset({"key", "value"})
_INTERVAL_KEYS = frozenset({"start_date", "end_date"})
_COVERAGE_KEYS = frozenset(
    {
        "expected_observations",
        "actual_observations",
        "missing_observations",
        "ratio",
    }
)


def _require_object(value: Any, path: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{path} must be exactly dict")
    return value


def _require_exact_keys(
    value: Dict[str, Any], expected: frozenset, path: str
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted((actual - expected), key=repr)
    if missing:
        raise ValueError(f"{path} is missing required field(s): {missing}")
    if unknown:
        raise ValueError(f"{path} contains unknown field(s): {unknown}")


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _unique_json_object(pairs: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


class ResultStatus(str, Enum):
    """Stable terminal states exposed to result consumers."""

    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


def _validate_code(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be exactly str")
    if not _CODE_RE.fullmatch(value):
        raise ValueError(
            f"{field_name} must be a non-empty upper snake-case identifier"
        )


def _validate_text(value: object, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be exactly str")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be non-empty and trimmed")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{field_name} must not contain control characters")


def _parse_iso_date(value: object, field_name: str) -> date:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be exactly str")
    if not _DATE_RE.fullmatch(value):
        raise ValueError(f"{field_name} must use YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid calendar date") from exc


@dataclass(frozen=True)
class ResultReason:
    """Machine-readable terminal reason plus a stable human description."""

    code: str
    message: str

    def __post_init__(self) -> None:
        _validate_code(self.code, "reason code")
        _validate_text(self.message, "reason message")

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ResultWarning:
    """Non-terminal diagnostic emitted by a completed or attempted run."""

    code: str
    message: str

    def __post_init__(self) -> None:
        _validate_code(self.code, "warning code")
        _validate_text(self.message, "warning message")

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ResultAssumption:
    """One explicit model assumption, represented only by immutable strings."""

    key: str
    value: str

    def __post_init__(self) -> None:
        if type(self.key) is not str:
            raise TypeError("assumption key must be exactly str")
        if not _KEY_RE.fullmatch(self.key):
            raise ValueError(
                "assumption key must be a lower snake-case identifier"
            )
        _validate_text(self.value, "assumption value")

    def to_dict(self) -> Dict[str, str]:
        return {"key": self.key, "value": self.value}


@dataclass(frozen=True)
class ActualInterval:
    """Inclusive interval for which the engine actually produced observations."""

    start_date: str
    end_date: str

    def __post_init__(self) -> None:
        start = _parse_iso_date(self.start_date, "actual start_date")
        end = _parse_iso_date(self.end_date, "actual end_date")
        if start > end:
            raise ValueError("actual start_date must not be after end_date")

    def to_dict(self) -> Dict[str, str]:
        return {"start_date": self.start_date, "end_date": self.end_date}


@dataclass(frozen=True)
class DataCoverage:
    """Observed-versus-expected coverage for the declared result scope.

    ``ratio`` is stored as an explicit audit value, but it must agree with the
    two counts.  For an empty expected scope, the only valid representation is
    ``0 / 0`` with a ratio of ``0.0``.
    """

    expected_observations: int
    actual_observations: int
    ratio: float

    def __post_init__(self) -> None:
        if type(self.expected_observations) is not int:
            raise TypeError("expected_observations must be exactly int")
        if type(self.actual_observations) is not int:
            raise TypeError("actual_observations must be exactly int")
        if self.expected_observations < 0:
            raise ValueError("expected_observations must be non-negative")
        if self.actual_observations < 0:
            raise ValueError("actual_observations must be non-negative")
        if self.actual_observations > self.expected_observations:
            raise ValueError(
                "actual_observations must not exceed expected_observations"
            )
        if type(self.ratio) is not float:
            raise TypeError("coverage ratio must be exactly float")
        if not math.isfinite(self.ratio):
            raise ValueError("coverage ratio must be finite")
        if not 0.0 <= self.ratio <= 1.0:
            raise ValueError("coverage ratio must be within [0, 1]")

        if self.expected_observations == 0:
            expected_ratio = 0.0
        else:
            expected_ratio = (
                self.actual_observations / self.expected_observations
            )
        if not math.isclose(
            self.ratio, expected_ratio, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValueError(
                "coverage ratio must equal actual_observations / "
                "expected_observations"
            )

    @property
    def missing_observations(self) -> int:
        return self.expected_observations - self.actual_observations

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expected_observations": self.expected_observations,
            "actual_observations": self.actual_observations,
            "missing_observations": self.missing_observations,
            "ratio": self.ratio,
        }


@dataclass(frozen=True)
class ResultContract:
    """Immutable status and audit envelope for any result payload.

    Domain metrics and tables deliberately live outside this object.  A
    consumer may rank a result if and only if ``is_rankable`` is true.
    """

    status: ResultStatus
    reason: Optional[ResultReason] = None
    warnings: Tuple[ResultWarning, ...] = ()
    assumptions: Tuple[ResultAssumption, ...] = ()
    actual_interval: Optional[ActualInterval] = None
    data_coverage: Optional[DataCoverage] = None
    semantics_version: str = SEMANTICS_VERSION
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.status) is not ResultStatus:
            raise TypeError("status must be exactly ResultStatus")
        if self.reason is not None and type(self.reason) is not ResultReason:
            raise TypeError("reason must be exactly ResultReason or None")
        if type(self.warnings) is not tuple:
            raise TypeError("warnings must be exactly tuple")
        if any(type(item) is not ResultWarning for item in self.warnings):
            raise TypeError("each warning must be exactly ResultWarning")
        warning_codes = tuple(item.code for item in self.warnings)
        if len(warning_codes) != len(set(warning_codes)):
            raise ValueError("warning codes must be unique")

        if type(self.assumptions) is not tuple:
            raise TypeError("assumptions must be exactly tuple")
        if any(type(item) is not ResultAssumption for item in self.assumptions):
            raise TypeError("each assumption must be exactly ResultAssumption")
        assumption_keys = tuple(item.key for item in self.assumptions)
        if len(assumption_keys) != len(set(assumption_keys)):
            raise ValueError("assumption keys must be unique")

        if (
            self.actual_interval is not None
            and type(self.actual_interval) is not ActualInterval
        ):
            raise TypeError(
                "actual_interval must be exactly ActualInterval or None"
            )
        if (
            self.data_coverage is not None
            and type(self.data_coverage) is not DataCoverage
        ):
            raise TypeError("data_coverage must be exactly DataCoverage or None")
        if type(self.semantics_version) is not str:
            raise TypeError("semantics_version must be exactly str")
        if self.semantics_version != SEMANTICS_VERSION:
            raise ValueError(
                f"unsupported semantics_version: {self.semantics_version!r}"
            )
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be exactly int")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {self.schema_version!r}")

        self._validate_progress_shape()
        self._validate_status_invariants()

    def _validate_progress_shape(self) -> None:
        coverage = self.data_coverage
        interval = self.actual_interval
        if interval is not None:
            if coverage is None or coverage.actual_observations == 0:
                raise ValueError(
                    "actual_interval requires positive data_coverage"
                )
        if coverage is not None and coverage.actual_observations > 0:
            if interval is None:
                raise ValueError(
                    "positive data coverage requires an actual_interval"
                )

    def _validate_status_invariants(self) -> None:
        if self.status is ResultStatus.SUCCESS:
            if self.reason is not None:
                raise ValueError("SUCCESS must not carry a terminal reason")
            if self.actual_interval is None or self.data_coverage is None:
                raise ValueError(
                    "SUCCESS requires an actual_interval and data_coverage"
                )
            if self.data_coverage.actual_observations == 0:
                raise ValueError("SUCCESS requires at least one observation")
            if self.data_coverage.ratio != 1.0:
                raise ValueError("SUCCESS requires complete data coverage")
            return

        if self.reason is None:
            raise ValueError(f"{self.status.value} requires a terminal reason")

        if self.status is ResultStatus.PARTIAL:
            if self.actual_interval is None or self.data_coverage is None:
                raise ValueError(
                    "PARTIAL requires an actual_interval and data_coverage"
                )
            if self.data_coverage.actual_observations == 0:
                raise ValueError("PARTIAL requires at least one observation")

        if self.status is ResultStatus.INVALID:
            if self.actual_interval is not None:
                raise ValueError("INVALID must not claim an actual interval")
            if (
                self.data_coverage is not None
                and self.data_coverage.actual_observations != 0
            ):
                raise ValueError("INVALID must not claim observed data")

    @property
    def is_rankable(self) -> bool:
        """Only a fully successful result may enter comparisons or rankings."""

        return self.status is ResultStatus.SUCCESS

    def require_rankable(self) -> "ResultContract":
        """Return ``self`` or reject unsafe ranking at the consumer boundary."""

        if not self.is_rankable:
            raise ValueError(
                f"result status {self.status.value} is not rankable"
            )
        return self

    @classmethod
    def from_dict(cls, payload: Any) -> "ResultContract":
        """Restore a contract from its exact versioned wire representation.

        The decoder is intentionally closed-world.  Missing fields, unknown
        fields, redundant derived values that do not agree, and unsupported
        schema or semantics versions all fail instead of being guessed.
        """

        value = _require_object(payload, "result_contract")
        _require_exact_keys(value, _RESULT_CONTRACT_KEYS, "result_contract")

        schema_version = value["schema_version"]
        if type(schema_version) is not int:
            raise TypeError("result_contract.schema_version must be exactly int")
        if schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version: {schema_version!r}"
            )

        semantics_version = value["semantics_version"]
        if type(semantics_version) is not str:
            raise TypeError(
                "result_contract.semantics_version must be exactly str"
            )
        if semantics_version != SEMANTICS_VERSION:
            raise ValueError(
                f"unsupported semantics_version: {semantics_version!r}"
            )

        status_value = value["status"]
        if type(status_value) is not str:
            raise TypeError("result_contract.status must be exactly str")
        try:
            status = ResultStatus(status_value)
        except ValueError as exc:
            raise ValueError(
                f"unsupported result_contract.status: {status_value!r}"
            ) from exc

        rankable = value["rankable"]
        if type(rankable) is not bool:
            raise TypeError("result_contract.rankable must be exactly bool")

        reason_value = value["reason"]
        if reason_value is None:
            reason = None
        else:
            reason_object = _require_object(
                reason_value, "result_contract.reason"
            )
            _require_exact_keys(
                reason_object, _DIAGNOSTIC_KEYS, "result_contract.reason"
            )
            reason = ResultReason(
                code=reason_object["code"],
                message=reason_object["message"],
            )

        warnings_value = value["warnings"]
        if type(warnings_value) is not list:
            raise TypeError("result_contract.warnings must be exactly list")
        warnings = []
        for index, warning_value in enumerate(warnings_value):
            path = f"result_contract.warnings[{index}]"
            warning_object = _require_object(warning_value, path)
            _require_exact_keys(warning_object, _DIAGNOSTIC_KEYS, path)
            warnings.append(
                ResultWarning(
                    code=warning_object["code"],
                    message=warning_object["message"],
                )
            )

        assumptions_value = value["assumptions"]
        if type(assumptions_value) is not list:
            raise TypeError("result_contract.assumptions must be exactly list")
        assumptions = []
        for index, assumption_value in enumerate(assumptions_value):
            path = f"result_contract.assumptions[{index}]"
            assumption_object = _require_object(assumption_value, path)
            _require_exact_keys(assumption_object, _ASSUMPTION_KEYS, path)
            assumptions.append(
                ResultAssumption(
                    key=assumption_object["key"],
                    value=assumption_object["value"],
                )
            )

        interval_value = value["actual_interval"]
        if interval_value is None:
            actual_interval = None
        else:
            interval_object = _require_object(
                interval_value, "result_contract.actual_interval"
            )
            _require_exact_keys(
                interval_object,
                _INTERVAL_KEYS,
                "result_contract.actual_interval",
            )
            actual_interval = ActualInterval(
                start_date=interval_object["start_date"],
                end_date=interval_object["end_date"],
            )

        coverage_value = value["data_coverage"]
        if coverage_value is None:
            data_coverage = None
        else:
            coverage_object = _require_object(
                coverage_value, "result_contract.data_coverage"
            )
            _require_exact_keys(
                coverage_object,
                _COVERAGE_KEYS,
                "result_contract.data_coverage",
            )
            missing_observations = coverage_object["missing_observations"]
            if type(missing_observations) is not int:
                raise TypeError(
                    "result_contract.data_coverage.missing_observations "
                    "must be exactly int"
                )
            data_coverage = DataCoverage(
                expected_observations=coverage_object[
                    "expected_observations"
                ],
                actual_observations=coverage_object["actual_observations"],
                ratio=coverage_object["ratio"],
            )
            if missing_observations != data_coverage.missing_observations:
                raise ValueError(
                    "result_contract.data_coverage.missing_observations "
                    "does not agree with expected and actual observations"
                )

        contract = cls(
            status=status,
            reason=reason,
            warnings=tuple(warnings),
            assumptions=tuple(assumptions),
            actual_interval=actual_interval,
            data_coverage=data_coverage,
            semantics_version=semantics_version,
            schema_version=schema_version,
        )
        if rankable is not contract.is_rankable:
            raise ValueError(
                "result_contract.rankable does not agree with status"
            )
        return contract

    @classmethod
    def from_json(cls, payload: str) -> "ResultContract":
        """Restore a contract from strict JSON with duplicate-key checks."""

        if type(payload) is not str:
            raise TypeError("result contract JSON payload must be exactly str")
        value = json.loads(
            payload,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        )
        return cls.from_dict(value)

    def to_dict(self) -> Dict[str, Any]:
        """Return a new, deterministic, JSON-compatible value tree."""

        return {
            "schema_version": self.schema_version,
            "semantics_version": self.semantics_version,
            "status": self.status.value,
            "rankable": self.is_rankable,
            "reason": self.reason.to_dict() if self.reason is not None else None,
            "warnings": [item.to_dict() for item in self.warnings],
            "assumptions": [item.to_dict() for item in self.assumptions],
            "actual_interval": (
                self.actual_interval.to_dict()
                if self.actual_interval is not None
                else None
            ),
            "data_coverage": (
                self.data_coverage.to_dict()
                if self.data_coverage is not None
                else None
            ),
        }

    def to_json(self) -> str:
        """Return canonical UTF-8 JSON without timestamps or ambient state."""

        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


__all__ = [
    "SCHEMA_VERSION",
    "SEMANTICS_VERSION",
    "ResultStatus",
    "ResultReason",
    "ResultWarning",
    "ResultAssumption",
    "ActualInterval",
    "DataCoverage",
    "ResultContract",
]
