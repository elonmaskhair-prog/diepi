"""Bounded, link-free reads for local data-contract files.

The data root is user supplied, so a path that merely resolves below it is
not sufficient: a symbolic link or Windows reparse point can still redirect a
read after lexical validation.  This module centralises the small-file trust
boundary used by dataset manifests, security metadata, and local calendar
overrides.  It deliberately does not claim to sandbox Parquet decoding.
"""

from __future__ import annotations

from io import BytesIO
import os
from pathlib import Path
import stat
from typing import Optional, Sequence

import pandas as pd


DATASET_MANIFEST_MAX_BYTES = 4 * 1024 * 1024
DATASET_MANIFEST_MAX_FILES = 16_384
DATASET_PARQUET_MAX_BYTES = 512 * 1024 * 1024
METADATA_PARQUET_MAX_BYTES = 256 * 1024 * 1024
TRADE_CALENDAR_PARQUET_MAX_BYTES = 16 * 1024 * 1024


def _absolute_lexical(path) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _is_link_or_reparse(info: os.stat_result) -> bool:
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return bool(attributes & reparse)


def _has_single_link(info: os.stat_result) -> bool:
    """Treat an unavailable link count as one, but reject known hard links."""

    return getattr(info, "st_nlink", 1) == 1


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare filesystem objects without relying on a path resolution."""

    left_inode = getattr(left, "st_ino", 0)
    right_inode = getattr(right, "st_ino", 0)
    if left_inode and right_inode:
        return (
            getattr(left, "st_dev", 0),
            left_inode,
        ) == (
            getattr(right, "st_dev", 0),
            right_inode,
        )
    return (
        getattr(left, "st_dev", 0),
        stat.S_IFMT(left.st_mode),
        getattr(left, "st_ctime_ns", None),
    ) == (
        getattr(right, "st_dev", 0),
        stat.S_IFMT(right.st_mode),
        getattr(right, "st_ctime_ns", None),
    )


def _lstat(path: Path, *, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise ValueError(f"cannot inspect {label}: {exc}") from exc


def _inspect_plain_file(
    path,
    *,
    root,
    max_bytes: int,
    label: str,
) -> tuple[Path, os.stat_result, tuple[tuple[Path, os.stat_result], ...]]:
    """Return one regular file proven to stay below a plain trusted root."""

    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("label must be a non-empty string")

    trusted_root = _absolute_lexical(root)
    root_info = _lstat(trusted_root, label="data root")
    if _is_link_or_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("data root must be a plain directory")

    candidate = _absolute_lexical(path)
    try:
        relative = candidate.relative_to(trusted_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the data root") from exc
    if not relative.parts:
        raise ValueError(f"{label} must be a regular file")

    current = trusted_root
    info = root_info
    directories = [(trusted_root, root_info)]
    for index, part in enumerate(relative.parts):
        current = current / part
        info = _lstat(current, label=label)
        if _is_link_or_reparse(info):
            raise ValueError(f"{label} must not traverse a link or reparse point")
        if index < len(relative.parts) - 1:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError(f"{label} has a non-directory parent")
            directories.append((current, info))
        elif not stat.S_ISREG(info.st_mode) or not _has_single_link(info):
            raise ValueError(f"{label} must be a single-link regular file")

    if info.st_size > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte safety limit")
    return current, info, tuple(directories)


def plain_file_exists(path, *, root, label: str) -> bool:
    """Distinguish an absent optional file from an unsafe path to that file.

    Missing components return ``False``.  A link, reparse point, non-directory
    ancestor, non-regular leaf, or hard link raises instead of being treated as
    absence and triggering a semantic fallback.
    """

    if not isinstance(label, str) or not label.strip():
        raise ValueError("label must be a non-empty string")
    trusted_root = _absolute_lexical(root)
    root_info = _lstat(trusted_root, label="data root")
    if _is_link_or_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
        raise ValueError("data root must be a plain directory")

    candidate = _absolute_lexical(path)
    try:
        relative = candidate.relative_to(trusted_root)
    except ValueError as exc:
        raise ValueError(f"{label} escapes the data root") from exc
    if not relative.parts:
        raise ValueError(f"{label} must be a regular file")

    current = trusted_root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            info = _lstat(current, label=label)
        except ValueError as exc:
            if isinstance(exc.__cause__, FileNotFoundError):
                return False
            raise
        if _is_link_or_reparse(info):
            raise ValueError(f"{label} must not traverse a link or reparse point")
        leaf = index == len(relative.parts) - 1
        if not leaf and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{label} has a non-directory parent")
        if leaf and (
            not stat.S_ISREG(info.st_mode) or not _has_single_link(info)
        ):
            raise ValueError(f"{label} must be a single-link regular file")
    return True


def _recheck_plain_directories(
    directories: tuple[tuple[Path, os.stat_result], ...],
    *,
    label: str,
) -> None:
    for path, before in directories:
        after = _lstat(path, label=label)
        if (
            _is_link_or_reparse(after)
            or not stat.S_ISDIR(after.st_mode)
            or not _same_object(before, after)
        ):
            raise ValueError(f"{label} parent changed while it was being read")


def read_plain_bytes(
    path,
    *,
    root,
    max_bytes: int,
    label: str,
) -> bytes:
    """Read exact bytes from a stable regular-file handle with a hard cap."""

    current, before, directories = _inspect_plain_file(
        path,
        root=root,
        max_bytes=max_bytes,
        label=label,
    )
    try:
        with current.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or not _has_single_link(opened)
                or not _same_object(before, opened)
                or opened.st_size > max_bytes
            ):
                raise ValueError(f"{label} changed or exceeds its safety limit")
            payload = stream.read(max_bytes + 1)
            opened_after = os.fstat(stream.fileno())
    except OSError as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc

    if len(payload) > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte safety limit")
    _recheck_plain_directories(directories, label=label)
    after = _lstat(current, label=label)
    if (
        _is_link_or_reparse(after)
        or not stat.S_ISREG(after.st_mode)
        or not _has_single_link(after)
        or not _has_single_link(opened_after)
        or not _same_object(after, before)
        or after.st_size != before.st_size
        or after.st_mtime_ns != before.st_mtime_ns
        or opened_after.st_size != opened.st_size
        or opened_after.st_mtime_ns != opened.st_mtime_ns
    ):
        raise ValueError(f"{label} changed while it was being read")
    return payload


def read_plain_parquet(
    path,
    *,
    root,
    max_bytes: int,
    label: str,
    columns: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """Decode only the exact bounded bytes proven by :func:`read_plain_bytes`."""

    payload = read_plain_bytes(
        path,
        root=root,
        max_bytes=max_bytes,
        label=label,
    )
    try:
        return pd.read_parquet(BytesIO(payload), columns=columns)
    except Exception as exc:
        raise ValueError(
            f"{label} could not be decoded ({type(exc).__name__})"
        ) from exc


__all__ = [
    "DATASET_MANIFEST_MAX_BYTES",
    "DATASET_MANIFEST_MAX_FILES",
    "DATASET_PARQUET_MAX_BYTES",
    "METADATA_PARQUET_MAX_BYTES",
    "TRADE_CALENDAR_PARQUET_MAX_BYTES",
    "plain_file_exists",
    "read_plain_bytes",
    "read_plain_parquet",
]
