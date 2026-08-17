"""Immutable comparison contracts for explicitly typed benchmark objects.

This module intentionally has no pandas, engine, broker, or filesystem
dependency.  It defines the JSON-safe value objects shared by comparison data
providers and, later, comparison runners.  A comparison scope is an exact set
of completed strategy observations; consumers must not silently intersect or
forward-fill it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any, Dict, Optional, Tuple


COMPARISON_SCHEMA_VERSION = 1
COMPARISON_SCOPE_SCHEMA = "diepi.comparison_scope"
COMPARISON_BUNDLE_SCHEMA = "diepi.comparison_bundle"

_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_INDEX_CODE_RE = re.compile(r"^\d{6}\.(?:SH|SZ)$")
_OBSERVATION_ID_RE = re.compile(r"^\d{8}$")
_COLUMN_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_REASON_KEYS = frozenset({"code", "message"})
_SCOPE_KEYS = frozenset(
    {
        "end_date",
        "observation_ids",
        "schema",
        "schema_version",
        "scope_hash",
        "start_date",
    }
)
_REFERENCE_SPEC_KEYS = frozenset(
    {"code", "source_id", "source_version", "value_column"}
)
_TOTAL_RETURN_SERIES_KEYS = frozenset(
    {
        "base_level",
        "base_observation_id",
        "levels",
        "normalized_nav",
        "observation_ids",
        "scope_hash",
        "spec",
        "total_return",
    }
)
_REFERENCE_RESULT_KEYS = frozenset(
    {"kind", "reason", "scope", "series", "spec", "status", "total_return"}
)
_BUNDLE_KEYS = frozenset(
    {"reference_index_total_return", "schema", "schema_version", "scope"}
)


class ComparisonKind(str, Enum):
    """Names of comparison objects that must never be conflated."""

    REFERENCE_INDEX_TOTAL_RETURN = "reference_index_total_return"
    EXECUTABLE_BENCHMARK = "executable_benchmark"
    EXECUTION_COUNTERFACTUAL = "execution_counterfactual"


class ComparisonStatus(str, Enum):
    """Terminal status of one comparison leg."""

    SUCCESS = "SUCCESS"
    UNAVAILABLE = "UNAVAILABLE"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"
    FAILED = "FAILED"


def _validate_trimmed_text(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError("{} must be exactly str".format(name))
    if not value or value != value.strip():
        raise ValueError("{} must be non-empty and trimmed".format(name))
    if any(ord(character) < 32 for character in value):
        raise ValueError("{} must not contain control characters".format(name))
    return value


def _validate_reason_code(value: object) -> str:
    text = _validate_trimmed_text(value, "reason code")
    if not _CODE_RE.fullmatch(text):
        raise ValueError(
            "reason code must be an upper snake-case identifier"
        )
    return text


def _parse_observation_id(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError("{} must be exactly str".format(name))
    if not _OBSERVATION_ID_RE.fullmatch(value):
        raise ValueError("{} must use YYYYMMDD".format(name))
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("{} is not a valid calendar date".format(name)) from exc
    if parsed.strftime("%Y%m%d") != value:
        raise ValueError("{} must use canonical YYYYMMDD".format(name))
    return value


def _positive_finite(value: object, name: str) -> float:
    if type(value) not in (int, float):
        raise TypeError("{} must be exactly int or float".format(name))
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError("{} must be finite and positive".format(name))
    return number


def _canonical_json(value: Dict[str, Any]) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _require_object(value: Any, path: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise TypeError("{} must be exactly dict".format(path))
    return value


def _require_exact_keys(
    value: Dict[str, Any], expected: frozenset, path: str
) -> None:
    actual = set(value)
    missing = sorted(expected - actual)
    unknown = sorted((actual - expected), key=repr)
    if missing:
        raise ValueError(
            "{} is missing required field(s): {}".format(path, missing)
        )
    if unknown:
        raise ValueError(
            "{} contains unknown field(s): {}".format(path, unknown)
        )


def _require_list(value: Any, path: str) -> list:
    if type(value) is not list:
        raise TypeError("{} must be exactly list".format(path))
    return value


def _finite_number(value: Any, path: str) -> float:
    if type(value) not in (int, float):
        raise TypeError("{} must be exactly int or float".format(path))
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("{} must be finite".format(path))
    return number


@dataclass(frozen=True)
class ComparisonReason:
    """Stable machine-readable reason for a non-success comparison leg."""

    code: str
    message: str

    def __post_init__(self) -> None:
        _validate_reason_code(self.code)
        _validate_trimmed_text(self.message, "reason message")

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, payload: Any) -> "ComparisonReason":
        value = _require_object(payload, "comparison reason")
        _require_exact_keys(value, _REASON_KEYS, "comparison reason")
        return cls(code=value["code"], message=value["message"])


@dataclass(frozen=True)
class ComparisonScope:
    """Exact, ordered strategy observations shared by every comparison leg."""

    observation_ids: Tuple[str, ...]
    scope_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.observation_ids) is not tuple:
            raise TypeError("observation_ids must be exactly tuple")
        if not self.observation_ids:
            raise ValueError("observation_ids must not be empty")
        canonical = tuple(
            _parse_observation_id(value, "observation_ids[{}]".format(index))
            for index, value in enumerate(self.observation_ids)
        )
        if len(set(canonical)) != len(canonical):
            raise ValueError("observation_ids must be unique")
        if tuple(sorted(canonical)) != canonical:
            raise ValueError("observation_ids must be strictly increasing")

        hash_payload = {
            "observation_ids": list(canonical),
            "schema": COMPARISON_SCOPE_SCHEMA,
            "schema_version": COMPARISON_SCHEMA_VERSION,
        }
        digest = hashlib.sha256(
            _canonical_json(hash_payload).encode("utf-8")
        ).hexdigest()
        object.__setattr__(self, "scope_hash", "sha256:" + digest)

    @property
    def start_date(self) -> str:
        return self.observation_ids[0]

    @property
    def end_date(self) -> str:
        return self.observation_ids[-1]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "end_date": self.end_date,
            "observation_ids": list(self.observation_ids),
            "schema": COMPARISON_SCOPE_SCHEMA,
            "schema_version": COMPARISON_SCHEMA_VERSION,
            "scope_hash": self.scope_hash,
            "start_date": self.start_date,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Any) -> "ComparisonScope":
        value = _require_object(payload, "comparison scope")
        _require_exact_keys(value, _SCOPE_KEYS, "comparison scope")
        if type(value["schema"]) is not str:
            raise TypeError("comparison scope.schema must be exactly str")
        if value["schema"] != COMPARISON_SCOPE_SCHEMA:
            raise ValueError("unsupported comparison scope schema")
        if type(value["schema_version"]) is not int:
            raise TypeError(
                "comparison scope.schema_version must be exactly int"
            )
        if value["schema_version"] != COMPARISON_SCHEMA_VERSION:
            raise ValueError("unsupported comparison scope schema_version")
        observations = _require_list(
            value["observation_ids"], "comparison scope.observation_ids"
        )
        restored = cls(observation_ids=tuple(observations))
        if restored.to_dict() != value:
            raise ValueError(
                "comparison scope derived fields do not agree with "
                "observation_ids"
            )
        return restored


@dataclass(frozen=True)
class ReferenceIndexSpec:
    """Explicit total-return-index lane and its declared provenance."""

    code: str
    source_id: str
    source_version: str
    value_column: str = "total_return_close"

    def __post_init__(self) -> None:
        code = _validate_trimmed_text(self.code, "reference index code")
        if not _INDEX_CODE_RE.fullmatch(code):
            raise ValueError(
                "reference index code must use canonical 000000.SH/SZ format"
            )
        _validate_trimmed_text(self.source_id, "source_id")
        _validate_trimmed_text(self.source_version, "source_version")
        column = _validate_trimmed_text(self.value_column, "value_column")
        if not _COLUMN_RE.fullmatch(column):
            raise ValueError("value_column must be a lower snake-case identifier")
        if column != "total_return_close":
            raise ValueError(
                "reference total return requires explicit "
                "value_column='total_return_close'"
            )

    def to_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "value_column": self.value_column,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ReferenceIndexSpec":
        value = _require_object(payload, "reference index spec")
        _require_exact_keys(
            value, _REFERENCE_SPEC_KEYS, "reference index spec"
        )
        restored = cls(
            code=value["code"],
            source_id=value["source_id"],
            source_version=value["source_version"],
            value_column=value["value_column"],
        )
        if restored.to_dict() != value:
            raise ValueError("reference index spec is not canonical")
        return restored


@dataclass(frozen=True)
class TotalReturnIndexSeries:
    """Validated total-return levels on exactly one comparison scope."""

    spec: ReferenceIndexSpec
    scope: ComparisonScope
    base_observation_id: str
    base_level: float
    levels: Tuple[float, ...]

    def __post_init__(self) -> None:
        if type(self.spec) is not ReferenceIndexSpec:
            raise TypeError("spec must be exactly ReferenceIndexSpec")
        if type(self.scope) is not ComparisonScope:
            raise TypeError("scope must be exactly ComparisonScope")
        base_id = _parse_observation_id(
            self.base_observation_id, "base_observation_id"
        )
        if base_id >= self.scope.start_date:
            raise ValueError(
                "base_observation_id must precede the comparison scope"
            )
        object.__setattr__(
            self, "base_level", _positive_finite(self.base_level, "base_level")
        )
        if type(self.levels) is not tuple:
            raise TypeError("levels must be exactly tuple")
        if len(self.levels) != len(self.scope.observation_ids):
            raise ValueError("levels must have one value per scope observation")
        validated_levels = tuple(
            _positive_finite(value, "levels[{}]".format(index))
            for index, value in enumerate(self.levels)
        )
        object.__setattr__(self, "levels", validated_levels)

    @property
    def normalized_nav(self) -> Tuple[float, ...]:
        return tuple(level / self.base_level for level in self.levels)

    @property
    def total_return(self) -> float:
        result = self.levels[-1] / self.base_level - 1.0
        if not math.isfinite(result):  # defensive; inputs are already finite
            raise ValueError("total return must be finite")
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_level": self.base_level,
            "base_observation_id": self.base_observation_id,
            "levels": list(self.levels),
            "normalized_nav": list(self.normalized_nav),
            "observation_ids": list(self.scope.observation_ids),
            "scope_hash": self.scope.scope_hash,
            "spec": self.spec.to_dict(),
            "total_return": self.total_return,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(
        cls,
        payload: Any,
        *,
        scope: ComparisonScope,
        spec: ReferenceIndexSpec,
    ) -> "TotalReturnIndexSeries":
        if type(scope) is not ComparisonScope:
            raise TypeError("scope must be exactly ComparisonScope")
        if type(spec) is not ReferenceIndexSpec:
            raise TypeError("spec must be exactly ReferenceIndexSpec")
        value = _require_object(payload, "total-return index series")
        _require_exact_keys(
            value, _TOTAL_RETURN_SERIES_KEYS, "total-return index series"
        )
        decoded_spec = ReferenceIndexSpec.from_dict(value["spec"])
        if decoded_spec != spec:
            raise ValueError(
                "total-return index series spec must match result spec"
            )
        levels = _require_list(
            value["levels"], "total-return index series.levels"
        )
        normalized_nav = _require_list(
            value["normalized_nav"],
            "total-return index series.normalized_nav",
        )
        observations = _require_list(
            value["observation_ids"],
            "total-return index series.observation_ids",
        )
        for index, item in enumerate(normalized_nav):
            _finite_number(
                item,
                "total-return index series.normalized_nav[{}]".format(index),
            )
        _finite_number(
            value["total_return"], "total-return index series.total_return"
        )
        if type(value["scope_hash"]) is not str:
            raise TypeError(
                "total-return index series.scope_hash must be exactly str"
            )
        restored = cls(
            spec=spec,
            scope=scope,
            base_observation_id=value["base_observation_id"],
            base_level=value["base_level"],
            levels=tuple(levels),
        )
        if restored.to_dict() != value:
            raise ValueError(
                "total-return index series redundant fields do not agree"
            )
        if tuple(observations) != scope.observation_ids:
            raise ValueError(
                "total-return index series observations must match scope"
            )
        return restored


class ReferenceIndexComparisonError(Exception):
    """Structured provider failure that maps to one comparison status."""

    status: ComparisonStatus

    def __init__(self, code: str, message: str) -> None:
        self.reason = ComparisonReason(code=code, message=message)
        super().__init__(message)


class ReferenceIndexUnavailableError(ReferenceIndexComparisonError):
    status = ComparisonStatus.UNAVAILABLE


class ReferenceIndexPartialError(ReferenceIndexComparisonError):
    status = ComparisonStatus.PARTIAL


class ReferenceIndexInvalidError(ReferenceIndexComparisonError):
    status = ComparisonStatus.INVALID


@dataclass(frozen=True)
class ReferenceIndexResult:
    """One explicitly typed reference-total-return comparison leg."""

    status: ComparisonStatus
    spec: ReferenceIndexSpec
    scope: ComparisonScope
    series: Optional[TotalReturnIndexSeries] = None
    reason: Optional[ComparisonReason] = None
    kind: ComparisonKind = field(
        default=ComparisonKind.REFERENCE_INDEX_TOTAL_RETURN, init=False
    )

    def __post_init__(self) -> None:
        if type(self.status) is not ComparisonStatus:
            raise TypeError("status must be exactly ComparisonStatus")
        if type(self.spec) is not ReferenceIndexSpec:
            raise TypeError("spec must be exactly ReferenceIndexSpec")
        if type(self.scope) is not ComparisonScope:
            raise TypeError("scope must be exactly ComparisonScope")
        if self.status is ComparisonStatus.SUCCESS:
            if type(self.series) is not TotalReturnIndexSeries:
                raise ValueError("SUCCESS reference result requires a series")
            if self.reason is not None:
                raise ValueError("SUCCESS reference result must not have a reason")
            if self.series.spec != self.spec or self.series.scope != self.scope:
                raise ValueError("reference series spec/scope must match the result")
        else:
            if self.series is not None:
                raise ValueError("non-success reference result must not have a series")
            if type(self.reason) is not ComparisonReason:
                raise ValueError("non-success reference result requires a reason")

    @classmethod
    def from_series(cls, series: TotalReturnIndexSeries) -> "ReferenceIndexResult":
        if type(series) is not TotalReturnIndexSeries:
            raise TypeError("series must be exactly TotalReturnIndexSeries")
        return cls(
            status=ComparisonStatus.SUCCESS,
            spec=series.spec,
            scope=series.scope,
            series=series,
        )

    @classmethod
    def from_error(
        cls,
        spec: ReferenceIndexSpec,
        scope: ComparisonScope,
        error: ReferenceIndexComparisonError,
    ) -> "ReferenceIndexResult":
        if not isinstance(error, ReferenceIndexComparisonError):
            raise TypeError(
                "error must be a ReferenceIndexComparisonError"
            )
        return cls(
            status=error.status,
            spec=spec,
            scope=scope,
            reason=error.reason,
        )

    @property
    def total_return(self) -> Optional[float]:
        return self.series.total_return if self.series is not None else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind.value,
            "reason": self.reason.to_dict() if self.reason is not None else None,
            "scope": self.scope.to_dict(),
            "series": self.series.to_dict() if self.series is not None else None,
            "spec": self.spec.to_dict(),
            "status": self.status.value,
            "total_return": self.total_return,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Any) -> "ReferenceIndexResult":
        value = _require_object(payload, "reference index result")
        _require_exact_keys(
            value, _REFERENCE_RESULT_KEYS, "reference index result"
        )
        if type(value["kind"]) is not str:
            raise TypeError("reference index result.kind must be exactly str")
        if value["kind"] != ComparisonKind.REFERENCE_INDEX_TOTAL_RETURN.value:
            raise ValueError("unsupported reference index result kind")
        if type(value["status"]) is not str:
            raise TypeError("reference index result.status must be exactly str")
        try:
            status = ComparisonStatus(value["status"])
        except ValueError:
            raise ValueError(
                "unsupported reference index result status: {!r}".format(
                    value["status"]
                )
            ) from None

        scope = ComparisonScope.from_dict(value["scope"])
        spec = ReferenceIndexSpec.from_dict(value["spec"])
        series = (
            None
            if value["series"] is None
            else TotalReturnIndexSeries.from_dict(
                value["series"], scope=scope, spec=spec
            )
        )
        reason = (
            None
            if value["reason"] is None
            else ComparisonReason.from_dict(value["reason"])
        )
        restored = cls(
            status=status,
            spec=spec,
            scope=scope,
            series=series,
            reason=reason,
        )
        if value["total_return"] is not None:
            _finite_number(
                value["total_return"], "reference index result.total_return"
            )
        if restored.to_dict() != value:
            raise ValueError(
                "reference index result redundant fields do not agree"
            )
        return restored


@dataclass(frozen=True)
class ComparisonBundle:
    """Versioned container; later phases add the other two typed legs."""

    scope: ComparisonScope
    reference_index_total_return: Optional[ReferenceIndexResult] = None
    schema_version: int = COMPARISON_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.scope) is not ComparisonScope:
            raise TypeError("scope must be exactly ComparisonScope")
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be exactly int")
        if self.schema_version != COMPARISON_SCHEMA_VERSION:
            raise ValueError("unsupported comparison schema_version")
        result = self.reference_index_total_return
        if result is not None:
            if type(result) is not ReferenceIndexResult:
                raise TypeError(
                    "reference_index_total_return must be exactly "
                    "ReferenceIndexResult or None"
                )
            if result.scope != self.scope:
                raise ValueError("comparison leg scope must match bundle scope")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "reference_index_total_return": (
                self.reference_index_total_return.to_dict()
                if self.reference_index_total_return is not None
                else None
            ),
            "schema": COMPARISON_BUNDLE_SCHEMA,
            "schema_version": self.schema_version,
            "scope": self.scope.to_dict(),
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Any) -> "ComparisonBundle":
        value = _require_object(payload, "comparison bundle")
        _require_exact_keys(value, _BUNDLE_KEYS, "comparison bundle")
        if type(value["schema"]) is not str:
            raise TypeError("comparison bundle.schema must be exactly str")
        if value["schema"] != COMPARISON_BUNDLE_SCHEMA:
            raise ValueError("unsupported comparison bundle schema")
        if type(value["schema_version"]) is not int:
            raise TypeError(
                "comparison bundle.schema_version must be exactly int"
            )
        scope = ComparisonScope.from_dict(value["scope"])
        reference_payload = value["reference_index_total_return"]
        reference_result = (
            None
            if reference_payload is None
            else ReferenceIndexResult.from_dict(reference_payload)
        )
        restored = cls(
            scope=scope,
            reference_index_total_return=reference_result,
            schema_version=value["schema_version"],
        )
        if restored.to_dict() != value:
            raise ValueError(
                "comparison bundle nested scope or derived fields do not agree"
            )
        return restored


__all__ = [
    "COMPARISON_SCHEMA_VERSION",
    "COMPARISON_SCOPE_SCHEMA",
    "COMPARISON_BUNDLE_SCHEMA",
    "ComparisonKind",
    "ComparisonStatus",
    "ComparisonReason",
    "ComparisonScope",
    "ReferenceIndexSpec",
    "TotalReturnIndexSeries",
    "ReferenceIndexResult",
    "ComparisonBundle",
    "ReferenceIndexComparisonError",
    "ReferenceIndexUnavailableError",
    "ReferenceIndexPartialError",
    "ReferenceIndexInvalidError",
]
