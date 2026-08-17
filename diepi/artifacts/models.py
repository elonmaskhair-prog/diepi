"""Versioned run-outcome and artifact-manifest contracts.

The artifact layer is deliberately independent from the execution engines.  It
wraps an engine result at an orchestration boundary without changing the
engine's public ``run()`` return type.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional

from diepi.backtest.result_contract import ResultContract, ResultStatus


RUN_OUTCOME_SCHEMA = "diepi.run_outcome"
RUN_OUTCOME_SCHEMA_VERSION = 1
RUN_ARTIFACT_SCHEMA = "diepi.run_artifact"
RUN_ARTIFACT_SCHEMA_VERSION = 1
# RunArtifact v1 is loaded into memory as one verified bundle.  These limits are
# part of the v1 acceptance contract, rather than caller-tunable policy, so a
# directory cannot be accepted by one consumer and rejected by another.
RUN_ARTIFACT_V1_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
RUN_ARTIFACT_V1_MAX_PAYLOAD_COUNT = 16_384
RUN_ARTIFACT_V1_MAX_PAYLOAD_BYTES = 128 * 1024 * 1024
RUN_ARTIFACT_V1_MAX_TOTAL_PAYLOAD_BYTES = 512 * 1024 * 1024

_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *("COM{}".format(index) for index in range(1, 10)),
        *("LPT{}".format(index) for index in range(1, 10)),
    }
)


def _exact_keys(value: Any, expected, label: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise TypeError("{} must be exactly an object".format(label))
    expected_keys = set(expected)
    actual_keys = set(value)
    if actual_keys != expected_keys:
        raise ValueError(
            "{} keys mismatch: missing={}, unknown={}".format(
                label,
                sorted(expected_keys - actual_keys),
                sorted(actual_keys - expected_keys),
            )
        )
    return value


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: {!r}".format(key))
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("non-finite JSON constant is forbidden: {}".format(value))


def load_json_bytes(payload: bytes, label: str = "JSON payload") -> Any:
    """Decode strict UTF-8 JSON while rejecting duplicate keys and NaN."""

    if type(payload) is not bytes:
        raise TypeError("{} must be exactly bytes".format(label))
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("{} must use UTF-8".format(label)) from exc
    try:
        return json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("{} is invalid JSON: {}".format(label, exc)) from exc


def normalize_json_value(value: Any, path: str = "value") -> Any:
    """Return a deterministic JSON value or fail instead of stringifying it.

    Configuration echoes may contain common immutable Python/numpy values.
    Unknown objects are rejected so an artifact never claims reproducibility
    after silently converting an input to an implementation-specific repr.
    """

    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("{} must be finite".format(path))
        return value
    if isinstance(value, Enum):
        return normalize_json_value(value.value, path)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()

    # numpy is a required project dependency, but importing it lazily keeps
    # this low-level schema module inexpensive for metadata-only consumers.
    try:
        import numpy as np

        if isinstance(value, np.bool_):
            return bool(value)
        if isinstance(value, np.integer):
            return int(value)
        if isinstance(value, np.floating):
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("{} must be finite".format(path))
            return number
        if isinstance(value, np.ndarray):
            return normalize_json_value(value.tolist(), path)
    except ImportError:  # pragma: no cover - numpy is a required dependency
        pass

    if type(value) is dict:
        normalized = {}
        for key, item in value.items():
            if type(key) is not str or not key:
                raise TypeError("{} keys must be non-empty strings".format(path))
            normalized[key] = normalize_json_value(
                item, "{}.{!s}".format(path, key)
            )
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            normalize_json_value(item, "{}[{}]".format(path, index))
            for index, item in enumerate(value)
        ]
    if isinstance(value, (set, frozenset)):
        items = [
            normalize_json_value(item, "{}[]".format(path)) for item in value
        ]
        return sorted(
            items,
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )
    raise TypeError(
        "{} contains unsupported JSON value of type {}".format(
            path, type(value).__name__
        )
    )


def canonical_json_bytes(value: Any) -> bytes:
    normalized = normalize_json_value(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_artifact_relative_path(value: Any) -> str:
    """Validate a portable, traversal-free artifact member path."""

    if type(value) is not str:
        raise TypeError("artifact path must be exactly str")
    if not value or value != value.strip():
        raise ValueError("artifact path must be non-empty and trimmed")
    if "\\" in value or "\x00" in value:
        raise ValueError("artifact path must use safe POSIX separators")
    raw_parts = value.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ValueError("artifact path must not contain empty/dot components")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or str(candidate) != value:
        raise ValueError("artifact path must be canonical and relative")
    for part in raw_parts:
        if ":" in part or part.endswith((" ", ".")):
            raise ValueError("artifact path is not portable across platforms")
        stem = part.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_NAMES:
            raise ValueError("artifact path uses a reserved Windows name")
    if value.casefold() == "manifest.json":
        raise ValueError("manifest.json cannot describe itself")
    return value


class EngineKind(str, Enum):
    CASH_SINGLE = "cash_single"
    CASH_PORTFOLIO = "cash_portfolio"
    CASH_PARALLEL = "cash_parallel"
    INDEX_FUTURES = "index_futures"


class ErrorCategory(str, Enum):
    INPUT = "INPUT"
    DATA = "DATA"
    STRATEGY = "STRATEGY"
    ENGINE = "ENGINE"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True)
class RunError:
    code: str
    category: ErrorCategory
    phase: str
    exception_type: str
    message: str
    traceback_role: Optional[str] = None

    def __post_init__(self) -> None:
        if type(self.code) is not str or not _CODE_RE.fullmatch(self.code):
            raise ValueError("run error code must be upper snake case")
        if type(self.category) is not ErrorCategory:
            raise TypeError("run error category must be exactly ErrorCategory")
        if type(self.phase) is not str or not _ROLE_RE.fullmatch(self.phase):
            raise ValueError("run error phase must be lower snake case")
        for name, value in (
            ("exception_type", self.exception_type),
            ("message", self.message),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ValueError("{} must be non-empty and trimmed".format(name))
            if "\x00" in value:
                raise ValueError("{} must not contain NUL".format(name))
        if self.traceback_role is not None:
            if (
                type(self.traceback_role) is not str
                or not _ROLE_RE.fullmatch(self.traceback_role)
            ):
                raise ValueError("traceback_role must be a lower snake-case role")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "category": self.category.value,
            "code": self.code,
            "exception_type": self.exception_type,
            "message": self.message,
            "phase": self.phase,
            "traceback_role": self.traceback_role,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "RunError":
        value = _exact_keys(
            payload,
            (
                "category",
                "code",
                "exception_type",
                "message",
                "phase",
                "traceback_role",
            ),
            "run error",
        )
        if type(value["category"]) is not str:
            raise TypeError("run error category must be exactly str")
        try:
            category = ErrorCategory(value["category"])
        except ValueError as exc:
            raise ValueError("unsupported run error category") from exc
        restored = cls(
            code=value["code"],
            category=category,
            phase=value["phase"],
            exception_type=value["exception_type"],
            message=value["message"],
            traceback_role=value["traceback_role"],
        )
        if restored.to_dict() != value:
            raise ValueError("run error payload is not canonical")
        return restored


@dataclass(frozen=True)
class RunOutcome:
    engine_kind: EngineKind
    result_contract: ResultContract
    result_role: Optional[str]
    error: Optional[RunError] = None
    result: Any = field(default=None, repr=False, compare=False)
    schema_version: int = RUN_OUTCOME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.engine_kind) is not EngineKind:
            raise TypeError("engine_kind must be exactly EngineKind")
        if type(self.result_contract) is not ResultContract:
            raise TypeError("result_contract must be exactly ResultContract")
        if type(self.schema_version) is not int:
            raise TypeError("run outcome schema_version must be exactly int")
        if self.schema_version != RUN_OUTCOME_SCHEMA_VERSION:
            raise ValueError("unsupported run outcome schema_version")
        if self.result_role is not None:
            if type(self.result_role) is not str or not _ROLE_RE.fullmatch(
                self.result_role
            ):
                raise ValueError("result_role must be a lower snake-case role")
        if self.error is not None and type(self.error) is not RunError:
            raise TypeError("error must be exactly RunError or None")
        if self.result is not None and self.result_role is None:
            raise ValueError("an in-memory result requires result_role")

        status = self.result_contract.status
        if status is ResultStatus.SUCCESS:
            if self.result_role != "result":
                raise ValueError("SUCCESS outcome requires result_role='result'")
            if self.error is not None:
                raise ValueError("SUCCESS outcome must not carry an error")
        if status is ResultStatus.FAILED and self.error is None:
            raise ValueError("FAILED outcome requires a structured error")

        result_contract = getattr(self.result, "result_contract", None)
        if self.result is not None:
            if self.engine_kind in (
                EngineKind.CASH_SINGLE,
                EngineKind.CASH_PORTFOLIO,
            ):
                if result_contract != self.result_contract:
                    raise ValueError(
                        "cash result contract must equal outcome result_contract"
                    )
            elif (
                result_contract is not None
                and result_contract != self.result_contract
            ):
                raise ValueError(
                    "embedded result contract must equal outcome result_contract"
                )

    @property
    def is_rankable(self) -> bool:
        return self.result_contract.is_rankable

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine_kind": self.engine_kind.value,
            "error": None if self.error is None else self.error.to_dict(),
            "result_contract": self.result_contract.to_dict(),
            "result_role": self.result_role,
            "schema": RUN_OUTCOME_SCHEMA,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "RunOutcome":
        value = _exact_keys(
            payload,
            (
                "engine_kind",
                "error",
                "result_contract",
                "result_role",
                "schema",
                "schema_version",
            ),
            "run outcome",
        )
        if value["schema"] != RUN_OUTCOME_SCHEMA:
            raise ValueError("unsupported run outcome schema")
        if type(value["schema_version"]) is not int:
            raise TypeError("run outcome schema_version must be exactly int")
        if type(value["engine_kind"]) is not str:
            raise TypeError("run outcome engine_kind must be exactly str")
        try:
            engine_kind = EngineKind(value["engine_kind"])
        except ValueError as exc:
            raise ValueError("unsupported run outcome engine_kind") from exc
        restored = cls(
            engine_kind=engine_kind,
            result_contract=ResultContract.from_dict(value["result_contract"]),
            result_role=value["result_role"],
            error=(
                None
                if value["error"] is None
                else RunError.from_dict(value["error"])
            ),
            schema_version=value["schema_version"],
        )
        if restored.to_dict() != value:
            raise ValueError("run outcome payload is not canonical")
        return restored

    @classmethod
    def from_result(
        cls, result: Any, *, engine_kind: Any
    ) -> "RunOutcome":
        try:
            kind = engine_kind if type(engine_kind) is EngineKind else EngineKind(engine_kind)
        except ValueError as exc:
            raise ValueError("unsupported engine_kind") from exc
        if kind in (EngineKind.CASH_PARALLEL, EngineKind.INDEX_FUTURES):
            raise ValueError(
                "{} requires its explicit orchestration outcome builder".format(
                    kind.value
                )
            )
        contract = getattr(result, "result_contract", None)
        if type(contract) is not ResultContract:
            raise TypeError("result must carry exactly one ResultContract")
        return cls(
            engine_kind=kind,
            result_contract=contract,
            result_role="result",
            result=result,
        )

    def with_result(self, result: Any) -> "RunOutcome":
        return replace(self, result=result)


@dataclass(frozen=True)
class ArtifactDescriptor:
    path: str
    media_type: str
    byte_length: int
    sha256: str

    def __post_init__(self) -> None:
        validate_artifact_relative_path(self.path)
        if (
            type(self.media_type) is not str
            or not self.media_type
            or self.media_type != self.media_type.strip()
            or any(ord(char) < 32 for char in self.media_type)
        ):
            raise ValueError("media_type must be non-empty canonical text")
        if type(self.byte_length) is not int or self.byte_length < 0:
            raise TypeError("byte_length must be a non-negative int")
        if self.byte_length > RUN_ARTIFACT_V1_MAX_PAYLOAD_BYTES:
            raise ValueError("artifact byte_length exceeds the RunArtifact v1 limit")
        if type(self.sha256) is not str or not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("sha256 must be canonical lowercase hex")

    @classmethod
    def from_payload(
        cls, *, path: str, media_type: str, payload: bytes
    ) -> "ArtifactDescriptor":
        if type(payload) is not bytes:
            raise TypeError("artifact payload must be exactly bytes")
        return cls(
            path=path,
            media_type=media_type,
            byte_length=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "byte_length": self.byte_length,
            "media_type": self.media_type,
            "path": self.path,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ArtifactDescriptor":
        value = _exact_keys(
            payload,
            ("byte_length", "media_type", "path", "sha256"),
            "artifact descriptor",
        )
        restored = cls(
            path=value["path"],
            media_type=value["media_type"],
            byte_length=value["byte_length"],
            sha256=value["sha256"],
        )
        if restored.to_dict() != value:
            raise ValueError("artifact descriptor is not canonical")
        return restored


@dataclass(frozen=True)
class ProducerInfo:
    diepi_version: str
    python_version: str
    platform: str
    dependencies: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("diepi_version", self.diepi_version),
            ("python_version", self.python_version),
            ("platform", self.platform),
        ):
            if type(value) is not str or not value or value != value.strip():
                raise ValueError("{} must be non-empty and trimmed".format(name))
        if type(self.dependencies) not in (dict, MappingProxyType):
            raise TypeError("dependencies must be exactly a mapping")
        copied = {}
        for key, value in self.dependencies.items():
            if type(key) is not str or not key or key != key.strip():
                raise ValueError("dependency names must be non-empty text")
            if type(value) is not str or not value or value != value.strip():
                raise ValueError("dependency versions must be non-empty text")
            copied[key] = value
        object.__setattr__(
            self, "dependencies", MappingProxyType(dict(sorted(copied.items())))
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dependencies": dict(self.dependencies),
            "diepi_version": self.diepi_version,
            "platform": self.platform,
            "python_version": self.python_version,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "ProducerInfo":
        value = _exact_keys(
            payload,
            ("dependencies", "diepi_version", "platform", "python_version"),
            "producer",
        )
        return cls(
            diepi_version=value["diepi_version"],
            python_version=value["python_version"],
            platform=value["platform"],
            dependencies=value["dependencies"],
        )


@dataclass(frozen=True)
class ArtifactManifest:
    run_id: str
    created_at_utc: str
    producer: ProducerInfo
    outcome: RunOutcome
    artifacts: Mapping[str, ArtifactDescriptor]
    schema_version: int = RUN_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not _RUN_ID_RE.fullmatch(self.run_id):
            raise ValueError("run_id must be a portable identifier")
        if self.run_id in (".", ".."):
            raise ValueError("run_id must not be a dot path")
        if self.run_id.endswith((" ", ".")):
            raise ValueError("run_id must be portable across platforms")
        if self.run_id.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
            raise ValueError("run_id must not use a reserved Windows name")
        if type(self.created_at_utc) is not str:
            raise TypeError("created_at_utc must be exactly str")
        try:
            parsed = datetime.fromisoformat(
                self.created_at_utc[:-1] + "+00:00"
            )
        except (ValueError, TypeError):
            raise ValueError(
                "created_at_utc must use canonical UTC microsecond format"
            ) from None
        canonical_time = parsed.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        if not self.created_at_utc.endswith("Z") or canonical_time != self.created_at_utc:
            raise ValueError(
                "created_at_utc must use canonical UTC microsecond format"
            )
        if type(self.producer) is not ProducerInfo:
            raise TypeError("producer must be exactly ProducerInfo")
        if type(self.outcome) is not RunOutcome:
            raise TypeError("outcome must be exactly RunOutcome")
        if type(self.schema_version) is not int:
            raise TypeError("artifact schema_version must be exactly int")
        if self.schema_version != RUN_ARTIFACT_SCHEMA_VERSION:
            raise ValueError("unsupported run artifact schema_version")
        if type(self.artifacts) not in (dict, MappingProxyType):
            raise TypeError("artifacts must be exactly a mapping")
        if len(self.artifacts) > RUN_ARTIFACT_V1_MAX_PAYLOAD_COUNT:
            raise ValueError("artifact count exceeds the RunArtifact v1 limit")

        copied = {}
        seen_paths = set()
        total_bytes = 0
        for role, descriptor in self.artifacts.items():
            if type(role) is not str or not _ROLE_RE.fullmatch(role):
                raise ValueError("artifact roles must be lower snake case")
            if type(descriptor) is not ArtifactDescriptor:
                raise TypeError("artifact descriptors must be exact objects")
            folded = descriptor.path.casefold()
            if folded in seen_paths:
                raise ValueError("artifact paths must be unique case-insensitively")
            seen_paths.add(folded)
            copied[role] = descriptor
            total_bytes += descriptor.byte_length
            if total_bytes > RUN_ARTIFACT_V1_MAX_TOTAL_PAYLOAD_BYTES:
                raise ValueError(
                    "artifact byte_length total exceeds the RunArtifact v1 limit"
                )
        for required in ("config", "provenance"):
            if required not in copied:
                raise ValueError("manifest is missing required role {!r}".format(required))
        if self.outcome.result_role is not None:
            if self.outcome.result_role not in copied:
                raise ValueError("outcome result_role is missing from artifacts")
        if self.outcome.error is not None:
            traceback_role = self.outcome.error.traceback_role
            if traceback_role is not None and traceback_role not in copied:
                raise ValueError("run error traceback_role is missing from artifacts")
        object.__setattr__(
            self, "artifacts", MappingProxyType(dict(sorted(copied.items())))
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "artifacts": {
                role: descriptor.to_dict()
                for role, descriptor in self.artifacts.items()
            },
            "created_at_utc": self.created_at_utc,
            "outcome": self.outcome.to_dict(),
            "producer": self.producer.to_dict(),
            "run_id": self.run_id,
            "schema": RUN_ARTIFACT_SCHEMA,
            "schema_version": self.schema_version,
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Any) -> "ArtifactManifest":
        value = _exact_keys(
            payload,
            (
                "artifacts",
                "created_at_utc",
                "outcome",
                "producer",
                "run_id",
                "schema",
                "schema_version",
            ),
            "artifact manifest",
        )
        if value["schema"] != RUN_ARTIFACT_SCHEMA:
            raise ValueError("unsupported run artifact schema")
        if type(value["schema_version"]) is not int:
            raise TypeError("artifact schema_version must be exactly int")
        if type(value["artifacts"]) is not dict:
            raise TypeError("manifest artifacts must be exactly an object")
        # Reject a member-count bomb before constructing one descriptor object
        # per untrusted manifest entry.
        if len(value["artifacts"]) > RUN_ARTIFACT_V1_MAX_PAYLOAD_COUNT:
            raise ValueError("artifact count exceeds the RunArtifact v1 limit")
        restored = cls(
            run_id=value["run_id"],
            created_at_utc=value["created_at_utc"],
            producer=ProducerInfo.from_dict(value["producer"]),
            outcome=RunOutcome.from_dict(value["outcome"]),
            artifacts={
                role: ArtifactDescriptor.from_dict(descriptor)
                for role, descriptor in value["artifacts"].items()
            },
            schema_version=value["schema_version"],
        )
        if restored.to_dict() != value:
            raise ValueError("artifact manifest is not canonical")
        return restored

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> "ArtifactManifest":
        return cls.from_dict(load_json_bytes(payload, "manifest.json"))


__all__ = [
    "ArtifactDescriptor",
    "ArtifactManifest",
    "EngineKind",
    "ErrorCategory",
    "ProducerInfo",
    "RUN_ARTIFACT_SCHEMA",
    "RUN_ARTIFACT_SCHEMA_VERSION",
    "RUN_ARTIFACT_V1_MAX_MANIFEST_BYTES",
    "RUN_ARTIFACT_V1_MAX_PAYLOAD_BYTES",
    "RUN_ARTIFACT_V1_MAX_PAYLOAD_COUNT",
    "RUN_ARTIFACT_V1_MAX_TOTAL_PAYLOAD_BYTES",
    "RUN_OUTCOME_SCHEMA",
    "RUN_OUTCOME_SCHEMA_VERSION",
    "RunError",
    "RunOutcome",
    "canonical_json_bytes",
    "load_json_bytes",
    "normalize_json_value",
    "validate_artifact_relative_path",
]
