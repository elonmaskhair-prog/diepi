"""Versioned identities for local datasets and the built-in synthetic demo."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .plain_files import (
    DATASET_MANIFEST_MAX_BYTES,
    DATASET_MANIFEST_MAX_FILES,
    DATASET_PARQUET_MAX_BYTES,
    read_plain_bytes,
    read_plain_parquet,
)


DATASET_MANIFEST_FILENAME = "diepi_dataset.json"
DATASET_MANIFEST_SCHEMA_VERSION = 1
DATASET_KINDS = frozenset({"synthetic_demo", "user_supplied"})
DATASET_MANIFEST_MAX_PATH_BYTES = 1024
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_scalar(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (pd.Timestamp, datetime, date)):
        timestamp = pd.Timestamp(value)
        if pd.isna(timestamp):
            return None
        if timestamp.tzinfo is not None:
            timestamp = timestamp.tz_convert("UTC").tz_localize(None)
        if timestamp == timestamp.normalize():
            return timestamp.strftime("%Y%m%d")
        return timestamp.isoformat(timespec="microseconds")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("logical dataset hashes require finite numeric values")
        return format(value, ".17g")
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):
        pass
    return str(value)


def logical_frame_payload(frame: pd.DataFrame) -> Dict[str, Any]:
    """Return a PyArrow-version-independent logical table representation."""

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    if frame.columns.has_duplicates:
        raise ValueError("logical dataset hashes require unique columns")
    columns = tuple(sorted(str(column) for column in frame.columns))
    if len(columns) != len(frame.columns):
        raise ValueError("column names must have unique string representations")
    normalized = frame.copy(deep=False)
    normalized.columns = [str(column) for column in frame.columns]
    rows = [
        [_canonical_scalar(value) for value in row]
        for row in normalized.loc[:, list(columns)].itertuples(index=False, name=None)
    ]
    return {"columns": list(columns), "rows": rows}


def logical_frame_sha256(frame: pd.DataFrame) -> str:
    """Hash logical values, excluding Parquet writer metadata and byte layout."""

    payload = _canonical_json(logical_frame_payload(frame)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _relative_manifest_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("manifest file path must be a non-empty string")
    encoded = value.encode("utf-8")
    if (
        len(encoded) > DATASET_MANIFEST_MAX_PATH_BYTES
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("manifest file path exceeds its portable safety contract")
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("manifest file path must stay below data root")
    for part in path.parts:
        if len(part.encode("utf-8")) > 255:
            raise ValueError("manifest file path component exceeds its safety limit")
        if any(character in '<>:"|?*' for character in part) or part.endswith(
            (" ", ".")
        ):
            raise ValueError("manifest file path exceeds its portable safety contract")
        basename = part.split(".", 1)[0].upper()
        if basename in _WINDOWS_RESERVED_BASENAMES:
            raise ValueError("manifest file path exceeds its portable safety contract")
    return path.as_posix()


def _sha256_text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a 64-character SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be hexadecimal") from exc
    return value.lower()


@dataclass(frozen=True)
class DatasetFileIdentity:
    path: str
    rows: int
    columns: Tuple[str, ...]
    logical_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", _relative_manifest_path(self.path))
        if type(self.rows) is not int or self.rows < 0:
            raise ValueError("manifest file rows must be a non-negative integer")
        columns = tuple(self.columns)
        if not columns or any(not isinstance(value, str) or not value for value in columns):
            raise ValueError("manifest file columns must be non-empty strings")
        if len(set(columns)) != len(columns):
            raise ValueError("manifest file columns must be unique")
        object.__setattr__(self, "columns", columns)
        object.__setattr__(
            self,
            "logical_sha256",
            _sha256_text(self.logical_sha256, name="logical_sha256"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "rows": self.rows,
            "columns": list(self.columns),
            "logical_sha256": self.logical_sha256,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DatasetFileIdentity":
        expected = {"path", "rows", "columns", "logical_sha256"}
        if type(value) is not dict or set(value) != expected:
            raise ValueError("dataset manifest file entry has unexpected fields")
        return cls(
            path=value["path"],
            rows=value["rows"],
            columns=tuple(value["columns"]),
            logical_sha256=value["logical_sha256"],
        )


@dataclass(frozen=True)
class DatasetManifest:
    dataset_kind: str
    dataset_id: str
    symbols: Tuple[str, ...]
    start_date: str
    end_date: str
    generator: str
    generator_version: str
    files: Tuple[DatasetFileIdentity, ...]
    schema_version: int = DATASET_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported dataset manifest schema_version")
        if self.dataset_kind not in DATASET_KINDS:
            raise ValueError(f"unsupported dataset_kind: {self.dataset_kind}")
        for field_name in ("dataset_id", "generator", "generator_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        symbols = tuple(self.symbols)
        if not symbols or any(not isinstance(value, str) or not value for value in symbols):
            raise ValueError("symbols must contain non-empty strings")
        if len(set(symbols)) != len(symbols):
            raise ValueError("symbols must be unique")
        object.__setattr__(self, "symbols", symbols)
        for field_name in ("start_date", "end_date"):
            value = getattr(self, field_name)
            try:
                parsed = datetime.strptime(value, "%Y%m%d")
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{field_name} must use YYYYMMDD") from exc
            if parsed.strftime("%Y%m%d") != value:
                raise ValueError(f"{field_name} must use YYYYMMDD")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        files = tuple(self.files)
        if not files or any(not isinstance(value, DatasetFileIdentity) for value in files):
            raise ValueError("files must contain DatasetFileIdentity values")
        if len(files) > DATASET_MANIFEST_MAX_FILES:
            raise ValueError(
                "dataset manifest exceeds the reviewed file-count safety limit"
            )
        paths = [value.path for value in files]
        if len(set(paths)) != len(paths):
            raise ValueError("manifest file paths must be unique")
        if len({path.casefold() for path in paths}) != len(paths):
            raise ValueError("manifest file paths must be portable-case unique")
        object.__setattr__(self, "files", tuple(sorted(files, key=lambda item: item.path)))

    def _payload(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "dataset_kind": self.dataset_kind,
            "dataset_id": self.dataset_id,
            "symbols": list(self.symbols),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "generator": self.generator,
            "generator_version": self.generator_version,
            "files": [value.to_dict() for value in self.files],
        }

    @property
    def manifest_sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self._payload()).encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        payload = self._payload()
        payload["manifest_sha256"] = self.manifest_sha256
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DatasetManifest":
        expected = {
            "schema_version",
            "dataset_kind",
            "dataset_id",
            "symbols",
            "start_date",
            "end_date",
            "generator",
            "generator_version",
            "files",
            "manifest_sha256",
        }
        if type(value) is not dict or set(value) != expected:
            raise ValueError("dataset manifest has unexpected fields")
        manifest = cls(
            schema_version=value["schema_version"],
            dataset_kind=value["dataset_kind"],
            dataset_id=value["dataset_id"],
            symbols=tuple(value["symbols"]),
            start_date=value["start_date"],
            end_date=value["end_date"],
            generator=value["generator"],
            generator_version=value["generator_version"],
            files=tuple(DatasetFileIdentity.from_dict(item) for item in value["files"]),
        )
        supplied = _sha256_text(value["manifest_sha256"], name="manifest_sha256")
        if supplied != manifest.manifest_sha256:
            raise ValueError("dataset manifest_sha256 does not match its content")
        return manifest

    @classmethod
    def read(cls, path, *, root=None) -> "DatasetManifest":
        target = Path(path).expanduser().absolute()
        trusted_root = target.parent if root is None else root
        raw = read_plain_bytes(
            target,
            root=trusted_root,
            max_bytes=DATASET_MANIFEST_MAX_BYTES,
            label="dataset manifest",
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("dataset manifest must be valid UTF-8 JSON") from exc
        return cls.from_dict(payload)

    def write(self, path) -> Path:
        target = Path(path)
        with target.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(self.to_json())
        return target


def identify_parquet_file(data_root, relative_path: str) -> DatasetFileIdentity:
    """Read one Parquet file and return its stable logical identity."""

    canonical = _relative_manifest_path(relative_path)
    root = Path(data_root).expanduser().absolute()
    path = root.joinpath(*PurePosixPath(canonical).parts)
    frame = read_plain_parquet(
        path,
        root=root,
        max_bytes=DATASET_PARQUET_MAX_BYTES,
        label=f"dataset member {canonical}",
    )
    return DatasetFileIdentity(
        path=canonical,
        rows=len(frame),
        columns=tuple(sorted(str(column) for column in frame.columns)),
        logical_sha256=logical_frame_sha256(frame),
    )


def build_dataset_manifest(
    *,
    data_root,
    dataset_kind: str,
    dataset_id: str,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    generator: str,
    generator_version: str,
    relative_files: Iterable[str],
) -> DatasetManifest:
    identities = tuple(
        identify_parquet_file(data_root, path) for path in relative_files
    )
    return DatasetManifest(
        dataset_kind=dataset_kind,
        dataset_id=dataset_id,
        symbols=tuple(symbols),
        start_date=start_date,
        end_date=end_date,
        generator=generator,
        generator_version=generator_version,
        files=identities,
    )


__all__ = [
    "DATASET_KINDS",
    "DATASET_MANIFEST_FILENAME",
    "DATASET_MANIFEST_MAX_PATH_BYTES",
    "DATASET_MANIFEST_SCHEMA_VERSION",
    "DatasetFileIdentity",
    "DatasetManifest",
    "build_dataset_manifest",
    "identify_parquet_file",
    "logical_frame_payload",
    "logical_frame_sha256",
]
