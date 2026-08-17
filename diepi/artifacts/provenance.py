"""Small, privacy-preserving provenance records for run artifacts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Optional, Tuple

from .models import (
    canonical_json_bytes,
    load_json_bytes,
    normalize_json_value,
    validate_artifact_relative_path,
)


RUN_PROVENANCE_SCHEMA = "diepi.run_provenance"
RUN_PROVENANCE_SCHEMA_VERSION = 1
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY_LEVELS = frozenset(
    {"not_recorded", "contract_reports_only", "content_sha256"}
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


@dataclass(frozen=True)
class SourceFingerprint:
    kind: str
    logical_path: str
    byte_length: int
    sha256: str

    def __post_init__(self) -> None:
        if type(self.kind) is not str or not _KIND_RE.fullmatch(self.kind):
            raise ValueError("source kind must be lower snake case")
        validate_artifact_relative_path(self.logical_path)
        if type(self.byte_length) is not int or self.byte_length < 0:
            raise TypeError("source byte_length must be a non-negative int")
        if type(self.sha256) is not str or not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("source sha256 must be canonical lowercase hex")

    @classmethod
    def from_bytes(
        cls, *, kind: str, logical_path: str, payload: bytes
    ) -> "SourceFingerprint":
        if type(payload) is not bytes:
            raise TypeError("source payload must be exactly bytes")
        return cls(
            kind=kind,
            logical_path=logical_path,
            byte_length=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

    @classmethod
    def from_file(
        cls, path: Any, *, root: Any, kind: str = "parquet"
    ) -> "SourceFingerprint":
        """Hash one stable file and record only its path relative to ``root``.

        Path and descriptor identities are checked before and after streaming.
        A concurrent replacement, resize, or metadata-changing write therefore
        fails instead of publishing a digest assembled across generations.
        """

        requested = Path(path)
        link_before = requested.lstat()
        source = requested.resolve(strict=True)
        root_path = Path(root).resolve(strict=True)
        try:
            relative = source.relative_to(root_path)
        except ValueError as exc:
            raise ValueError("source file must stay within provenance root") from exc
        if not source.is_file():
            raise ValueError("source path must identify a regular file")

        def stable_snapshot(value):
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
            )

        def same_object(left, right):
            # ``st_mode``/``st_ctime_ns`` may legitimately differ between
            # Windows path and descriptor stat views.  File identity uses the
            # stable volume/file index pair there (st_dev/st_ino); on a
            # platform that reports zero inode values, the surrounding path
            # and handle before/after snapshots still protect size/mtime.
            if left.st_ino and right.st_ino:
                return (
                    left.st_dev == right.st_dev
                    and left.st_ino == right.st_ino
                )
            return True

        path_before = source.stat()
        digest = hashlib.sha256()
        length = 0
        with source.open("rb") as stream:
            handle_before = os.fstat(stream.fileno())
            if (
                not same_object(path_before, handle_before)
                or path_before.st_size != handle_before.st_size
            ):
                raise OSError(
                    "source file identity changed before fingerprinting"
                )
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                length += len(block)
                digest.update(block)
            handle_after = os.fstat(stream.fileno())
        path_after = source.stat()
        link_after = requested.lstat()
        if (
            stable_snapshot(link_before) != stable_snapshot(link_after)
            or stable_snapshot(handle_before) != stable_snapshot(handle_after)
            or stable_snapshot(path_before) != stable_snapshot(path_after)
            or not same_object(handle_after, path_after)
            or handle_after.st_size != path_after.st_size
            or length != handle_after.st_size
        ):
            raise OSError("source file changed while fingerprinting")
        return cls(
            kind=kind,
            logical_path=relative.as_posix(),
            byte_length=length,
            sha256=digest.hexdigest(),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "byte_length": self.byte_length,
            "kind": self.kind,
            "logical_path": self.logical_path,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "SourceFingerprint":
        value = _exact_keys(
            payload,
            ("byte_length", "kind", "logical_path", "sha256"),
            "source fingerprint",
        )
        restored = cls(
            kind=value["kind"],
            logical_path=value["logical_path"],
            byte_length=value["byte_length"],
            sha256=value["sha256"],
        )
        if restored.to_dict() != value:
            raise ValueError("source fingerprint payload is not canonical")
        return restored


@dataclass(frozen=True)
class RunProvenance:
    sources: Tuple[SourceFingerprint, ...] = ()
    data_contract_reports: Tuple[Dict[str, Any], ...] = ()
    data_identity_level: str = "not_recorded"
    schema_version: int = RUN_PROVENANCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.sources) is not tuple:
            raise TypeError("sources must be exactly tuple")
        if any(type(item) is not SourceFingerprint for item in self.sources):
            raise TypeError("each source must be exactly SourceFingerprint")
        canonical_sources = tuple(
            sorted(self.sources, key=lambda item: (item.kind, item.logical_path))
        )
        if canonical_sources != self.sources:
            raise ValueError("sources must use canonical kind/path order")
        source_keys = tuple(
            (item.kind, item.logical_path.casefold()) for item in self.sources
        )
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("source kind/logical_path pairs must be unique")

        if type(self.data_contract_reports) is not tuple:
            raise TypeError("data_contract_reports must be exactly tuple")
        copied_reports = []
        report_payloads = []
        for index, report in enumerate(self.data_contract_reports):
            if type(report) is not dict:
                raise TypeError(
                    "data_contract_reports[{}] must be exactly object".format(index)
                )
            normalized = normalize_json_value(
                report, "data_contract_reports[{}]".format(index)
            )
            encoded = canonical_json_bytes(normalized)
            copied_reports.append(load_json_bytes(encoded, "data contract report"))
            report_payloads.append(encoded)
        if report_payloads != sorted(report_payloads):
            raise ValueError("data_contract_reports must use canonical order")
        object.__setattr__(self, "data_contract_reports", tuple(copied_reports))

        if (
            type(self.data_identity_level) is not str
            or self.data_identity_level not in _IDENTITY_LEVELS
        ):
            raise ValueError("unsupported data_identity_level")
        expected_level = (
            "content_sha256"
            if self.sources
            else (
                "contract_reports_only"
                if self.data_contract_reports
                else "not_recorded"
            )
        )
        if self.data_identity_level != expected_level:
            raise ValueError(
                "data_identity_level does not match recorded provenance"
            )
        if type(self.schema_version) is not int:
            raise TypeError("provenance schema_version must be exactly int")
        if self.schema_version != RUN_PROVENANCE_SCHEMA_VERSION:
            raise ValueError("unsupported provenance schema_version")

    @classmethod
    def empty(cls) -> "RunProvenance":
        return cls()

    @classmethod
    def build(
        cls,
        *,
        sources: Iterable[SourceFingerprint] = (),
        data_contract_reports: Iterable[Dict[str, Any]] = (),
    ) -> "RunProvenance":
        source_tuple = tuple(sorted(tuple(sources), key=lambda item: (
            item.kind, item.logical_path
        )))
        normalized_reports = [
            normalize_json_value(report, "data_contract_report")
            for report in data_contract_reports
        ]
        report_tuple = tuple(
            sorted(
                normalized_reports,
                key=lambda report: canonical_json_bytes(report),
            )
        )
        identity_level = (
            "content_sha256"
            if source_tuple
            else "contract_reports_only" if report_tuple else "not_recorded"
        )
        return cls(
            sources=source_tuple,
            data_contract_reports=report_tuple,
            data_identity_level=identity_level,
        )

    @classmethod
    def from_engine(
        cls,
        engine: Any,
        *,
        sources: Iterable[SourceFingerprint] = (),
    ) -> "RunProvenance":
        """Collect public, immutable evidence exposed by a cash engine."""

        reports = getattr(engine, "data_quality_reports", ())
        serialized = []
        for report in reports:
            to_dict = getattr(report, "to_dict", None)
            if not callable(to_dict):
                raise TypeError("engine data_quality_reports must be serializable")
            serialized.append(to_dict())
        return cls.build(sources=sources, data_contract_reports=serialized)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "data_contract_reports": [
                normalize_json_value(report) for report in self.data_contract_reports
            ],
            "data_identity_level": self.data_identity_level,
            "schema": RUN_PROVENANCE_SCHEMA,
            "schema_version": self.schema_version,
            "sources": [source.to_dict() for source in self.sources],
        }

    def to_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, payload: Any) -> "RunProvenance":
        value = _exact_keys(
            payload,
            (
                "data_contract_reports",
                "data_identity_level",
                "schema",
                "schema_version",
                "sources",
            ),
            "run provenance",
        )
        if value["schema"] != RUN_PROVENANCE_SCHEMA:
            raise ValueError("unsupported run provenance schema")
        if type(value["schema_version"]) is not int:
            raise TypeError("provenance schema_version must be exactly int")
        if type(value["sources"]) is not list:
            raise TypeError("provenance sources must be exactly list")
        if type(value["data_contract_reports"]) is not list:
            raise TypeError("data_contract_reports must be exactly list")
        restored = cls(
            sources=tuple(
                SourceFingerprint.from_dict(item) for item in value["sources"]
            ),
            data_contract_reports=tuple(value["data_contract_reports"]),
            data_identity_level=value["data_identity_level"],
            schema_version=value["schema_version"],
        )
        if restored.to_dict() != value:
            raise ValueError("run provenance payload is not canonical")
        return restored

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> "RunProvenance":
        return cls.from_dict(load_json_bytes(payload, "provenance.json"))


__all__ = [
    "RUN_PROVENANCE_SCHEMA",
    "RUN_PROVENANCE_SCHEMA_VERSION",
    "RunProvenance",
    "SourceFingerprint",
]
