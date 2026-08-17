"""Atomic storage and verified loading for RunArtifact v1 directories."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import os
from pathlib import Path, PurePosixPath
import platform as platform_module
import shutil
import stat
import tempfile
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple, Union

import diepi

from .adapters import ArtifactPayload, adapter_for_kind
from .models import (
    ArtifactDescriptor,
    ArtifactManifest,
    ProducerInfo,
    RUN_ARTIFACT_V1_MAX_MANIFEST_BYTES,
    RUN_ARTIFACT_V1_MAX_PAYLOAD_BYTES,
    RUN_ARTIFACT_V1_MAX_PAYLOAD_COUNT,
    RUN_ARTIFACT_V1_MAX_TOTAL_PAYLOAD_BYTES,
    RunOutcome,
    canonical_json_bytes,
    load_json_bytes,
    validate_artifact_relative_path,
)
from .provenance import RunProvenance


_MANIFEST_NAME = "manifest.json"
_STAGING_PREFIX = ".diepi-artifact-staging-"


def _is_link_or_reparse(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(reparse and attributes & reparse)


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise ValueError("cannot inspect {}: {}".format(label, exc)) from exc


def _ensure_plain_directory(path: Path, label: str) -> None:
    info = _lstat(path, label)
    if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("{} must be a plain directory".format(label))


def _inspect_plain_file(
    root: Path, relative_path: str, *, allow_manifest: bool = False
) -> Tuple[Path, os.stat_result]:
    if not (allow_manifest and relative_path == _MANIFEST_NAME):
        validate_artifact_relative_path(relative_path)
    current = root
    parts = PurePosixPath(relative_path).parts
    for index, part in enumerate(parts):
        current = current / part
        info = _lstat(current, "artifact member {!r}".format(relative_path))
        if _is_link_or_reparse(info):
            raise ValueError(
                "artifact member {!r} must not traverse a link or reparse point".format(
                    relative_path
                )
            )
        if index < len(parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(
                    "artifact member {!r} has a non-directory parent".format(
                        relative_path
                    )
                )
        elif not stat.S_ISREG(info.st_mode):
            raise ValueError(
                "artifact member {!r} must be a regular file".format(relative_path)
            )
    return current, info


def _read_plain_file(
    root: Path,
    relative_path: str,
    *,
    max_bytes: int,
    expected_bytes: Optional[int] = None,
    allow_manifest: bool = False,
) -> bytes:
    """Read one plain member with metadata and actual-byte bounds."""

    current, info = _inspect_plain_file(
        root, relative_path, allow_manifest=allow_manifest
    )
    if info.st_size > max_bytes:
        raise ValueError(
            "artifact member {!r} exceeds the RunArtifact v1 size limit".format(
                relative_path
            )
        )
    if expected_bytes is not None and info.st_size != expected_bytes:
        raise ValueError(
            "artifact length mismatch for member {!r}".format(relative_path)
        )
    try:
        with current.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or opened.st_size > max_bytes
                or (
                    expected_bytes is not None
                    and opened.st_size != expected_bytes
                )
            ):
                raise ValueError(
                    "artifact member {!r} changed or exceeds its size limit".format(
                        relative_path
                    )
                )
            read_limit = (
                expected_bytes if expected_bytes is not None else max_bytes
            )
            payload = stream.read(read_limit + 1)
            opened_after = os.fstat(stream.fileno())
    except OSError as exc:
        raise ValueError(
            "cannot read artifact member {!r}: {}".format(relative_path, exc)
        ) from exc
    if len(payload) > max_bytes:
        raise ValueError(
            "artifact member {!r} exceeds the RunArtifact v1 size limit".format(
                relative_path
            )
        )
    if expected_bytes is not None and len(payload) != expected_bytes:
        raise ValueError(
            "artifact length mismatch for member {!r}".format(relative_path)
        )
    after = _lstat(current, "artifact member {!r}".format(relative_path))
    if (
        _is_link_or_reparse(after)
        or not stat.S_ISREG(after.st_mode)
        or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)
        or after.st_size != info.st_size
        or after.st_mtime_ns != info.st_mtime_ns
        or opened_after.st_size != opened.st_size
        or opened_after.st_mtime_ns != opened.st_mtime_ns
    ):
        raise ValueError(
            "artifact member {!r} changed while it was being read".format(
                relative_path
            )
        )
    return payload


def _dependency_versions() -> Dict[str, str]:
    versions = {}
    for distribution in ("numpy", "pandas", "pyarrow"):
        try:
            versions[distribution] = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    return versions


def current_producer_info() -> ProducerInfo:
    """Return reproducibility metadata without host names or user identifiers."""

    system = platform_module.system() or "unknown"
    machine = platform_module.machine() or "unknown"
    return ProducerInfo(
        diepi_version=diepi.__version__,
        python_version=platform_module.python_version(),
        platform="{}-{}".format(system, machine),
        dependencies=_dependency_versions(),
    )


@dataclass(frozen=True)
class LoadedRun:
    """A fully verified artifact with its reconstructed engine result."""

    root: Path
    manifest: ArtifactManifest
    outcome: RunOutcome
    config: Mapping[str, Any]
    provenance: RunProvenance
    artifact_verified: bool = True
    _payloads: Mapping[str, bytes] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("root must be a pathlib.Path")
        if type(self.manifest) is not ArtifactManifest:
            raise TypeError("manifest must be exactly ArtifactManifest")
        if type(self.outcome) is not RunOutcome:
            raise TypeError("outcome must be exactly RunOutcome")
        if type(self.provenance) is not RunProvenance:
            raise TypeError("provenance must be exactly RunProvenance")
        if type(self.artifact_verified) is not bool:
            raise TypeError("artifact_verified must be exactly bool")
        if type(self.config) is not dict:
            raise TypeError("config must be exactly dict")
        if type(self._payloads) is not dict:
            raise TypeError("payloads must be exactly dict")
        config_copy = load_json_bytes(canonical_json_bytes(self.config), "config")
        object.__setattr__(self, "config", MappingProxyType(config_copy))
        object.__setattr__(self, "_payloads", MappingProxyType(dict(self._payloads)))

    @property
    def result(self) -> Any:
        return self.outcome.result

    @property
    def is_rankable(self) -> bool:
        return self.artifact_verified and self.outcome.is_rankable

    def read_bytes(self, role: str) -> bytes:
        """Return one already verified member by its manifest role."""

        if type(role) is not str:
            raise TypeError("role must be exactly str")
        try:
            return self._payloads[role]
        except KeyError:
            raise KeyError("unknown artifact role: {!r}".format(role)) from None


@dataclass(frozen=True)
class LoadedLegacyRun:
    """Read-only compatibility view of an old ResultStorage directory.

    Legacy folders have no complete RunArtifact manifest.  Their individual
    cash-audit files may carry hashes, but that cannot verify the directory as
    a whole; consequently this wrapper is unconditionally non-rankable.
    """

    root: Path
    result: Any
    config: Mapping[str, Any]
    strategy_source: str
    artifact_verified: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise TypeError("root must be a pathlib.Path")
        if type(self.config) is not dict:
            raise TypeError("config must be exactly dict")
        if type(self.strategy_source) is not str:
            raise TypeError("strategy_source must be exactly str")
        if self.artifact_verified is not False:
            raise ValueError("legacy results can never be artifact_verified")
        config_copy = load_json_bytes(canonical_json_bytes(self.config), "config")
        object.__setattr__(self, "config", MappingProxyType(config_copy))

    @property
    def is_rankable(self) -> bool:
        return False


class ArtifactStore:
    """Save immutable artifact directories and load them with full verification."""

    @classmethod
    def save(
        cls,
        outcome: RunOutcome,
        target: Union[str, os.PathLike],
        *,
        config: Dict[str, Any],
        provenance: Optional[RunProvenance] = None,
        strategy_source: Optional[str] = None,
        signals_source: Optional[Union[str, bytes]] = None,
        traceback_text: Optional[str] = None,
        producer: Optional[ProducerInfo] = None,
        extra_payloads: Tuple[ArtifactPayload, ...] = (),
    ) -> Path:
        """Persist a new run artifact; an existing target is never overwritten."""

        if type(outcome) is not RunOutcome:
            raise TypeError("outcome must be exactly RunOutcome")
        if type(config) is not dict:
            raise TypeError("config must be exactly dict")
        if provenance is None:
            provenance = RunProvenance.empty()
        if type(provenance) is not RunProvenance:
            raise TypeError("provenance must be exactly RunProvenance")
        if producer is None:
            producer = current_producer_info()
        if type(producer) is not ProducerInfo:
            raise TypeError("producer must be exactly ProducerInfo")
        if type(extra_payloads) is not tuple:
            raise TypeError("extra_payloads must be exactly tuple")
        if any(type(payload) is not ArtifactPayload for payload in extra_payloads):
            raise TypeError(
                "every extra_payloads item must be exactly ArtifactPayload"
            )

        requested = Path(target)
        if not requested.name or requested.name in (".", ".."):
            raise ValueError("artifact target must name a run directory")
        # ArtifactManifest performs the complete portable run-id validation.
        run_id = requested.name
        parent = requested.parent
        parent.mkdir(parents=True, exist_ok=True)
        parent = parent.resolve(strict=True)
        _ensure_plain_directory(parent, "artifact parent")
        destination = parent / run_id
        if os.path.lexists(str(destination)):
            raise FileExistsError(
                "artifact target already exists: {}".format(destination)
            )

        payloads = list(cls._base_payloads(config, provenance))
        payloads.extend(
            cls._optional_payloads(
                outcome=outcome,
                strategy_source=strategy_source,
                signals_source=signals_source,
                traceback_text=traceback_text,
            )
        )
        payloads.extend(extra_payloads)
        if outcome.result_role is not None:
            if outcome.result is None:
                raise ValueError("saving a result outcome requires its in-memory result")
            payloads.extend(
                adapter_for_kind(outcome.engine_kind).build_payloads(outcome)
            )
        elif outcome.result is not None:
            raise ValueError("outcome result has no result_role")
        payload_tuple = cls._validate_payloads(tuple(payloads))
        descriptors = {
            payload.role: ArtifactDescriptor.from_payload(
                path=payload.path,
                media_type=payload.media_type,
                payload=payload.data,
            )
            for payload in payload_tuple
        }
        manifest = ArtifactManifest(
            run_id=run_id,
            created_at_utc=datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            producer=producer,
            outcome=outcome,
            artifacts=descriptors,
        )

        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=str(parent)))
        try:
            cls._write_payloads(staging, payload_tuple)
            cls._write_manifest(staging, manifest)
            # Verify both hashes and adapter semantics before publication.
            cls._load_directory(staging)
            if os.path.lexists(str(destination)):
                raise FileExistsError(
                    "artifact target appeared during save: {}".format(destination)
                )
            cls._publish(staging, destination)
        except BaseException:
            cls._remove_staging(staging, parent)
            raise
        return destination

    @classmethod
    def load(cls, path: Union[str, os.PathLike]) -> LoadedRun:
        return cls._load_directory(Path(path))

    @classmethod
    def verify(cls, path: Union[str, os.PathLike]) -> ArtifactManifest:
        """Verify a complete artifact and return its manifest."""

        return cls.load(path).manifest

    @classmethod
    def load_legacy(
        cls, path: Union[str, os.PathLike]
    ) -> LoadedLegacyRun:
        """Load a ResultStorage folder without upgrading its trust status."""

        root = Path(path).absolute()
        _ensure_plain_directory(root, "legacy result root")
        # ResultStorage has strict parsers for its own schema.  Before calling
        # it, reject every link/reparse member so its ordinary open/read calls
        # cannot escape the selected directory.
        for current_text, directory_names, file_names in os.walk(
            str(root), topdown=True, followlinks=False
        ):
            current = Path(current_text)
            for name in tuple(directory_names) + tuple(file_names):
                member = current / name
                info = _lstat(member, "legacy result member")
                if _is_link_or_reparse(info):
                    raise ValueError(
                        "legacy result must not contain links or reparse points"
                    )
        from diepi.backtest.data.result_storage import ResultStorage

        result, config, strategy_source = ResultStorage.load(str(root))
        return LoadedLegacyRun(
            root=root,
            result=result,
            config=config,
            strategy_source=strategy_source,
            artifact_verified=False,
        )

    @staticmethod
    def _base_payloads(
        config: Dict[str, Any], provenance: RunProvenance
    ) -> Tuple[ArtifactPayload, ...]:
        config_bytes = canonical_json_bytes(config)
        restored = load_json_bytes(config_bytes, "config")
        if type(restored) is not dict:
            raise TypeError("config must encode a JSON object")
        return (
            ArtifactPayload(
                role="config",
                path="config.json",
                media_type="application/json",
                data=config_bytes,
            ),
            ArtifactPayload(
                role="provenance",
                path="provenance.json",
                media_type="application/json",
                data=provenance.to_json_bytes(),
            ),
        )

    @staticmethod
    def _optional_payloads(
        *,
        outcome: RunOutcome,
        strategy_source: Optional[str],
        signals_source: Optional[Union[str, bytes]],
        traceback_text: Optional[str],
    ) -> Tuple[ArtifactPayload, ...]:
        payloads = []
        if strategy_source is not None:
            if type(strategy_source) is not str:
                raise TypeError("strategy_source must be exactly str or None")
            payloads.append(
                ArtifactPayload(
                    role="strategy_source",
                    path="inputs/strategy.py",
                    media_type="text/x-python; charset=utf-8",
                    data=strategy_source.encode("utf-8"),
                )
            )
        if signals_source is not None:
            if type(signals_source) is str:
                signal_bytes = signals_source.encode("utf-8")
            elif type(signals_source) is bytes:
                signal_bytes = signals_source
            else:
                raise TypeError("signals_source must be exactly str, bytes, or None")
            payloads.append(
                ArtifactPayload(
                    role="signals_source",
                    path="inputs/signals.csv",
                    media_type="text/csv; charset=utf-8",
                    data=signal_bytes,
                )
            )

        expected_traceback_role = (
            outcome.error.traceback_role if outcome.error is not None else None
        )
        if traceback_text is None:
            if expected_traceback_role is not None:
                raise ValueError("outcome declares traceback evidence but none was supplied")
        else:
            if type(traceback_text) is not str:
                raise TypeError("traceback_text must be exactly str or None")
            if expected_traceback_role != "traceback":
                raise ValueError(
                    "traceback_text requires outcome.error.traceback_role='traceback'"
                )
            payloads.append(
                ArtifactPayload(
                    role="traceback",
                    path="diagnostics/traceback.txt",
                    media_type="text/plain; charset=utf-8",
                    data=traceback_text.encode("utf-8"),
                )
            )
        return tuple(payloads)

    @staticmethod
    def _validate_payloads(
        payloads: Tuple[ArtifactPayload, ...]
    ) -> Tuple[ArtifactPayload, ...]:
        if len(payloads) > RUN_ARTIFACT_V1_MAX_PAYLOAD_COUNT:
            raise ValueError("artifact count exceeds the RunArtifact v1 limit")
        roles = set()
        paths = set()
        total_bytes = 0
        for payload in payloads:
            if type(payload) is not ArtifactPayload:
                raise TypeError("payloads must be exactly ArtifactPayload")
            if payload.role in roles:
                raise ValueError("duplicate artifact role: {!r}".format(payload.role))
            if len(payload.data) > RUN_ARTIFACT_V1_MAX_PAYLOAD_BYTES:
                raise ValueError(
                    "artifact payload exceeds the RunArtifact v1 size limit"
                )
            total_bytes += len(payload.data)
            if total_bytes > RUN_ARTIFACT_V1_MAX_TOTAL_PAYLOAD_BYTES:
                raise ValueError(
                    "artifact payload total exceeds the RunArtifact v1 size limit"
                )
            folded = payload.path.casefold()
            if folded in paths:
                raise ValueError(
                    "duplicate artifact path (case-insensitive): {!r}".format(
                        payload.path
                    )
                )
            roles.add(payload.role)
            paths.add(folded)
        return tuple(sorted(payloads, key=lambda item: item.path))

    @staticmethod
    def _write_payloads(root: Path, payloads: Tuple[ArtifactPayload, ...]) -> None:
        for payload in payloads:
            destination = root.joinpath(*PurePosixPath(payload.path).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("xb") as stream:
                stream.write(payload.data)

    @staticmethod
    def _write_manifest(root: Path, manifest: ArtifactManifest) -> None:
        payload = manifest.to_json_bytes()
        if len(payload) > RUN_ARTIFACT_V1_MAX_MANIFEST_BYTES:
            raise ValueError("manifest.json exceeds the RunArtifact v1 size limit")
        with (root / _MANIFEST_NAME).open("xb") as stream:
            stream.write(payload)

    @staticmethod
    def _publish(staging: Path, destination: Path) -> None:
        # Same-parent staging gives rename atomicity on all supported platforms.
        os.rename(str(staging), str(destination))

    @staticmethod
    def _remove_staging(staging: Path, expected_parent: Path) -> None:
        if not os.path.lexists(str(staging)):
            return
        absolute = staging.absolute()
        if absolute.parent != expected_parent.absolute():
            raise RuntimeError("refusing to clean staging outside artifact parent")
        if not absolute.name.startswith(_STAGING_PREFIX):
            raise RuntimeError("refusing to clean an unexpected staging path")
        info = absolute.lstat()
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("refusing to recursively clean a linked staging path")
        shutil.rmtree(str(absolute))

    @classmethod
    def _load_directory(cls, path: Path) -> LoadedRun:
        root = path.absolute()
        _ensure_plain_directory(root, "artifact root")
        manifest_path = root / _MANIFEST_NAME
        info = _lstat(manifest_path, _MANIFEST_NAME)
        if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
            raise ValueError("manifest.json must be a plain regular file")
        if info.st_size > RUN_ARTIFACT_V1_MAX_MANIFEST_BYTES:
            raise ValueError("manifest.json exceeds the RunArtifact v1 size limit")
        manifest = ArtifactManifest.from_json_bytes(
            _read_plain_file(
                root,
                _MANIFEST_NAME,
                max_bytes=RUN_ARTIFACT_V1_MAX_MANIFEST_BYTES,
                allow_manifest=True,
            )
        )

        # Complete the count/declared-byte checks in ArtifactManifest first,
        # then inspect every member and the actual aggregate size before any
        # payload bytes are read.  A late oversized or mismatched member cannot
        # make the loader retain earlier payloads in memory.
        actual_total_bytes = 0
        for role, descriptor in manifest.artifacts.items():
            _, member_info = _inspect_plain_file(root, descriptor.path)
            if member_info.st_size != descriptor.byte_length:
                raise ValueError(
                    "artifact length mismatch for role {!r}".format(role)
                )
            if member_info.st_size > RUN_ARTIFACT_V1_MAX_PAYLOAD_BYTES:
                raise ValueError(
                    "artifact member exceeds the RunArtifact v1 size limit"
                )
            actual_total_bytes += member_info.st_size
            if actual_total_bytes > RUN_ARTIFACT_V1_MAX_TOTAL_PAYLOAD_BYTES:
                raise ValueError(
                    "artifact total exceeds the RunArtifact v1 size limit"
                )
        cls._reject_unlisted_members(root, manifest)

        payloads = {}
        for role, descriptor in manifest.artifacts.items():
            payload = _read_plain_file(
                root,
                descriptor.path,
                max_bytes=RUN_ARTIFACT_V1_MAX_PAYLOAD_BYTES,
                expected_bytes=descriptor.byte_length,
            )
            digest = hashlib.sha256(payload).hexdigest()
            if digest != descriptor.sha256:
                raise ValueError("artifact hash mismatch for role {!r}".format(role))
            payloads[role] = payload

        config = load_json_bytes(payloads["config"], "config.json")
        if type(config) is not dict:
            raise TypeError("config.json must contain exactly one object")
        provenance = RunProvenance.from_json_bytes(payloads["provenance"])
        outcome = manifest.outcome
        if outcome.result_role is not None:
            result = adapter_for_kind(outcome.engine_kind).load_result(
                outcome, payloads
            )
            outcome = outcome.with_result(result)
        return LoadedRun(
            root=root,
            manifest=manifest,
            outcome=outcome,
            config=config,
            provenance=provenance,
            artifact_verified=True,
            _payloads=payloads,
        )

    @staticmethod
    def _reject_unlisted_members(root: Path, manifest: ArtifactManifest) -> None:
        allowed_files = {_MANIFEST_NAME}
        allowed_dirs = set()
        for descriptor in manifest.artifacts.values():
            allowed_files.add(descriptor.path)
            path = PurePosixPath(descriptor.path)
            for parent in path.parents:
                if str(parent) != ".":
                    allowed_dirs.add(str(parent))

        for current_text, directory_names, file_names in os.walk(
            str(root), topdown=True, followlinks=False
        ):
            current = Path(current_text)
            for name in tuple(directory_names):
                member = current / name
                info = _lstat(member, "artifact directory")
                if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                    raise ValueError("artifact must not contain linked directories")
                relative = member.relative_to(root).as_posix()
                if relative not in allowed_dirs:
                    raise ValueError(
                        "artifact contains unlisted directory {!r}".format(relative)
                    )
            for name in file_names:
                member = current / name
                info = _lstat(member, "artifact file")
                if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
                    raise ValueError("artifact must contain only regular files")
                relative = member.relative_to(root).as_posix()
                if relative not in allowed_files:
                    raise ValueError(
                        "artifact contains unlisted file {!r}".format(relative)
                    )


def save_run_artifact(
    outcome: RunOutcome,
    target: Union[str, os.PathLike],
    *,
    config: Dict[str, Any],
    provenance: Optional[RunProvenance] = None,
    **kwargs: Any
) -> Path:
    """Functional convenience wrapper around :meth:`ArtifactStore.save`."""

    return ArtifactStore.save(
        outcome, target, config=config, provenance=provenance, **kwargs
    )


def load_run_artifact(path: Union[str, os.PathLike]) -> LoadedRun:
    """Functional convenience wrapper around :meth:`ArtifactStore.load`."""

    return ArtifactStore.load(path)


def load_legacy_result(path: Union[str, os.PathLike]) -> LoadedLegacyRun:
    """Load an old ResultStorage directory as explicitly unverified."""

    return ArtifactStore.load_legacy(path)


__all__ = [
    "ArtifactStore",
    "LoadedLegacyRun",
    "LoadedRun",
    "current_producer_info",
    "load_run_artifact",
    "load_legacy_result",
    "save_run_artifact",
]
