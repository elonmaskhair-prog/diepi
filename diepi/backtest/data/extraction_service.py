"""Create a private, scoped dataset from user-supplied local Parquet data.

The extractor is intentionally a copier, not a downloader.  It reads only the
explicit source Parquet tree, writes into a private sibling staging directory,
validates the completed slice, and publishes the workspace with one directory
rename.  Existing source files are never changed.  It never records the source
path or a hash of the complete source dataset, and it never discovers or copies
strategy signal files.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
from datetime import datetime
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import sys
from typing import Any, Dict, Iterable, Mapping, Sequence, Tuple
import uuid

import pandas as pd

from ..instruments import is_exchange_fund
from .dataset_manifest import (
    DATASET_MANIFEST_FILENAME,
    DatasetManifest,
    identify_parquet_file,
)
from .calendar import (
    BUILTIN_CALENDAR_CONTENT_SHA256,
    BUILTIN_CALENDAR_END,
    BUILTIN_CALENDAR_ID,
    BUILTIN_CALENDAR_START,
    BUILTIN_CALENDAR_VERSION,
    load_builtin_trade_calendar,
)
from .validation_service import (
    DataValidationReport,
    validate_local_data,
    write_validation_report,
)


LOCAL_EXTRACTION_SCHEMA_VERSION = 2
LOCAL_EXTRACTOR_VERSION = "2"
EXTRACTION_SCOPE_FILENAME = "extraction_scope.json"
EXTRACTION_VALIDATION_FILENAME = "validation.json"

_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_STAGING_PREFIX = ".diepi-extract-"

# Extraction is a privacy boundary: only documented market-data fields may
# leave the user-owned source tree.  Unknown columns fail closed instead of
# being copied accidentally as strategy or research annotations.
_MARKET_COLUMN_ALLOWLIST = frozenset(
    {
        "ts_code",
        "symbol",
        "trade_date",
        "pre_close",
        "open",
        "high",
        "low",
        "close",
        "change",
        "pct_chg",
        "vol",
        "amount",
        "ah_vol",
        "ah_amount",
        "adj_factor",
    }
)
_FACTOR_COLUMN_ALLOWLIST = frozenset(
    {"ts_code", "symbol", "trade_date", "adj_factor"}
)
_BASIC_COLUMN_ALLOWLIST = {
    "stock": frozenset(
        {
            "ts_code",
            "symbol",
            "name",
            "area",
            "industry",
            "fullname",
            "enname",
            "cnspell",
            "market",
            "exchange",
            "curr_type",
            "list_status",
            "list_date",
            "delist_date",
            "is_hs",
        }
    ),
    "etf": frozenset(
        {
            "ts_code",
            "symbol",
            "name",
            "csname",
            "extname",
            "cname",
            "index_code",
            "index_name",
            "setup_date",
            "list_date",
            "delist_date",
            "list_status",
            "exchange",
            "mgr_name",
            "custod_name",
            "mgt_fee",
            "etf_type",
        }
    ),
}


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_date(value: str, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be text in YYYYMMDD form")
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{name} must use a valid YYYYMMDD date") from exc
    if parsed.strftime("%Y%m%d") != value:
        raise ValueError(f"{name} must use a valid YYYYMMDD date")
    return value


def _normalize_symbols(symbols: Sequence[str]) -> Tuple[str, ...]:
    if isinstance(symbols, (str, bytes)):
        raise TypeError("symbols must be a sequence, not one string")
    normalized = []
    for value in symbols:
        if not isinstance(value, str):
            raise TypeError("every symbol must be text")
        symbol = value.strip().upper()
        if not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError(
                "symbols must use canonical six-digit exchange form, "
                f"for example 000001.SZ or 510300.SH: {value!r}"
            )
        normalized.append(symbol)
    result = tuple(sorted(set(normalized)))
    if not result:
        raise ValueError("at least one symbol is required")
    return result


def _is_below(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _lexical_absolute(path: Path) -> Path:
    """Return an absolute path without following its final link target."""

    value = path.expanduser().absolute()
    if any(component in (".", "..") for component in value.parts):
        raise ValueError("local extraction paths must not contain dot segments")
    return value


def _plain_existing_path(path: Path, *, label: str, directory: bool) -> Path:
    """Reject links/reparse points in every existing path component."""

    value = _lexical_absolute(path)
    parts = value.parts
    if not parts or not value.anchor:
        raise ValueError(f"{label} must be an absolute path")
    current = Path(parts[0])
    components = parts[1:]
    if not components:
        components = ()
        current = value
    for component in components:
        current /= component
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"{label} does not exist: {value}") from exc
        if _is_link_or_reparse(info):
            raise ValueError(f"{label} contains a link or reparse point")
        if current != value and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{label} has a non-directory parent component")
    try:
        leaf = value.lstat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"{label} does not exist: {value}") from exc
    if _is_link_or_reparse(leaf):
        raise ValueError(f"{label} contains a link or reparse point")
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected(leaf.st_mode):
        kind = "directory" if directory else "regular file"
        raise ValueError(f"{label} is not a plain {kind}")
    return value


@dataclass(frozen=True)
class _PathIdentity:
    st_dev: int
    st_ino: int
    st_mode: int
    st_size: int
    st_mtime_ns: int
    st_file_attributes: int


def _path_identity(path: Path) -> _PathIdentity:
    info = path.lstat()
    return _PathIdentity(
        st_dev=int(info.st_dev),
        st_ino=int(info.st_ino),
        st_mode=int(info.st_mode),
        st_size=int(info.st_size),
        st_mtime_ns=int(info.st_mtime_ns),
        st_file_attributes=int(getattr(info, "st_file_attributes", 0)),
    )


def _assert_same_identity(
    path: Path, expected: _PathIdentity, *, label: str
) -> None:
    try:
        observed = _path_identity(path)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} disappeared during extraction") from exc
    if observed != expected:
        raise RuntimeError(f"{label} changed during extraction")


def _resolved_source_file(root: Path, relative_candidates: Iterable[str]) -> Path:
    attempted = []
    for relative in relative_candidates:
        canonical = PurePosixPath(relative)
        candidate = _lexical_absolute(root / canonical)
        if not _is_below(candidate, root):
            raise ValueError(f"source file escapes source data root: {relative}")
        attempted.append(canonical.as_posix())
        if os.path.lexists(candidate):
            return _plain_existing_path(
                candidate,
                label=f"source Parquet member {canonical.as_posix()}",
                directory=False,
            )
    raise FileNotFoundError(
        "required source Parquet file is missing: " + ", ".join(attempted)
    )


def _symbol_file_candidates(relative_dir: str, symbol: str) -> Tuple[str, ...]:
    alternate = symbol.replace(".", "_")
    return (
        f"parquet/timeseries/{relative_dir}/{symbol}.parquet",
        f"parquet/timeseries/{relative_dir}/{alternate}.parquet",
    )


def _read_frame(path: Path, *, description: str) -> pd.DataFrame:
    _plain_existing_path(path, label=description, directory=False)
    source_identity = _path_identity(path)
    try:
        frame = pd.read_parquet(path)
    except Exception as exc:
        raise ValueError(
            f"{description} could not be read: {type(exc).__name__}: {exc}"
        ) from exc
    _plain_existing_path(path, label=description, directory=False)
    _assert_same_identity(path, source_identity, label=description)
    if frame.columns.has_duplicates:
        raise ValueError(f"{description} contains duplicate columns")
    # pandas/pyarrow round-trips DataFrame.attrs into Parquet key/value
    # metadata.  Those opaque values are outside the public data contract and
    # may contain research annotations or strategy signals, so never copy them.
    frame.attrs.clear()
    return frame


def _require_allowed_columns(
    frame: pd.DataFrame,
    *,
    allowed: frozenset[str],
    description: str,
) -> None:
    invalid_types = [column for column in frame.columns if type(column) is not str]
    unknown = sorted(
        column
        for column in frame.columns
        if type(column) is str and column not in allowed
    )
    if invalid_types or unknown:
        rendered = [repr(value) for value in invalid_types]
        rendered.extend(unknown)
        raise ValueError(
            f"{description} contains unsupported columns; extraction "
            "fails closed: " + ",".join(rendered)
        )


def _canonical_date_series(
    frame: pd.DataFrame, *, column: str, description: str
) -> pd.Series:
    if column not in frame.columns:
        raise ValueError(f"{description} is missing {column}")
    values = frame[column]
    if pd.api.types.is_datetime64_any_dtype(values):
        parsed = pd.to_datetime(values, errors="coerce")
    else:
        text = values.astype("string").str.strip().str.replace(
            r"\.0$", "", regex=True
        )
        parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    return parsed.dt.strftime("%Y%m%d").astype("string")


def _instrument_mask(frame: pd.DataFrame, symbol: str) -> pd.Series:
    if "ts_code" in frame.columns:
        values = frame["ts_code"].astype("string").str.strip().str.upper()
        return values.eq(symbol).fillna(False)
    if "symbol" in frame.columns:
        values = frame["symbol"].astype("string").str.strip().str.upper()
        return values.isin((symbol, symbol.split(".", 1)[0])).fillna(False)
    return pd.Series(True, index=frame.index, dtype=bool)


def _slice_market_frame(
    frame: pd.DataFrame,
    *,
    symbol: str,
    storage_start_date: str,
    end_date: str,
    description: str,
) -> pd.DataFrame:
    dates = _canonical_date_series(
        frame, column="trade_date", description=description
    )
    mask = (
        _instrument_mask(frame, symbol)
        & dates.ge(storage_start_date).fillna(False)
        & dates.le(end_date).fillna(False)
    )
    selected = frame.loc[mask].copy(deep=True)
    selected_dates = dates.loc[mask]
    if selected.empty:
        raise ValueError(f"{description} has no rows in the storage interval")
    if storage_start_date not in set(selected_dates.dropna().tolist()):
        raise ValueError(
            f"{description} has no previous-trading-day row "
            f"({storage_start_date})"
        )
    selected.loc[:, "trade_date"] = selected_dates.astype(str).to_numpy()
    selected = selected.assign(_diepi_sort_date=selected["trade_date"])
    selected = selected.sort_values("_diepi_sort_date", kind="mergesort")
    return selected.drop(columns=["_diepi_sort_date"]).reset_index(drop=True)


def _slice_factor_frame(
    frame: pd.DataFrame,
    *,
    symbol: str,
    storage_start_date: str,
    end_date: str,
    description: str,
) -> pd.DataFrame:
    if frame.empty:
        raise ValueError(f"{description} is empty")
    dates = _canonical_date_series(
        frame, column="trade_date", description=description
    )
    if pd.isna(dates.iloc[0]):
        raise ValueError(f"{description} first source row has an invalid trade_date")
    symbol_mask = _instrument_mask(frame, symbol)
    if not bool(symbol_mask.iloc[0]):
        raise ValueError(f"{description} first source row belongs to another symbol")
    window_mask = (
        symbol_mask
        & dates.ge(storage_start_date).fillna(False)
        & dates.le(end_date).fillna(False)
    )
    positions = [0]
    positions.extend(
        position
        for position, included in enumerate(window_mask.tolist())
        if included and position != 0
    )
    selected = frame.iloc[positions].copy(deep=True)
    selected_dates = dates.iloc[positions]
    if storage_start_date not in set(selected_dates.dropna().tolist()):
        raise ValueError(
            f"{description} has no previous-trading-day factor "
            f"({storage_start_date})"
        )
    # AFI-1 depends on source row zero and source order.  Never sort this slice.
    selected.loc[:, "trade_date"] = selected_dates.astype(str).to_numpy()
    return selected.reset_index(drop=True)


def _calendar_slice(
    source_root: Path, *, start_date: str, end_date: str
) -> Tuple[pd.DataFrame, str]:
    """Select the bundled clock used to determine the storage warm-up day.

    ``source_root`` remains in the private helper signature for compatibility
    with callers that patch this boundary, but the extractor intentionally
    does not read or copy a source calendar.  Extracted workspaces therefore
    resolve the same bundled market clock as any other calendar-less dataset.
    """

    del source_root
    if start_date < BUILTIN_CALENDAR_START or end_date > BUILTIN_CALENDAR_END:
        raise ValueError(
            "extraction scope is outside bundled trade-calendar coverage: "
            f"requested={start_date}..{end_date}, "
            f"available={BUILTIN_CALENDAR_START}..{BUILTIN_CALENDAR_END}, "
            f"calendar_id={BUILTIN_CALENDAR_ID}"
        )
    frame = load_builtin_trade_calendar()
    dates = _canonical_date_series(
        frame, column="cal_date", description="bundled trade calendar"
    )
    is_open = pd.to_numeric(frame["is_open"], errors="coerce")
    previous = dates[dates.lt(start_date).fillna(False) & is_open.eq(1)]
    if previous.empty:
        raise ValueError(
            "trade calendar has no open day before the requested start date"
        )
    storage_start_date = str(previous.max())
    mask = (
        dates.ge(storage_start_date).fillna(False)
        & dates.le(end_date).fillna(False)
    )
    selected = frame.loc[mask].copy(deep=True)
    selected_dates = dates.loc[mask]
    selected.loc[:, "cal_date"] = selected_dates.astype(str).to_numpy()
    selected = selected.assign(_diepi_sort_date=selected["cal_date"])
    selected = selected.sort_values("_diepi_sort_date", kind="mergesort")
    return (
        selected.drop(columns=["_diepi_sort_date"]).reset_index(drop=True),
        storage_start_date,
    )


def _write_parquet(data_root: Path, relative_path: str, frame: pd.DataFrame) -> None:
    target = data_root / PurePosixPath(relative_path)
    _ensure_private_directory(data_root, target.parent)
    frame.to_parquet(target, index=False)
    _secure_private_file(target)


def _ensure_private_directory(root: Path, target: Path) -> None:
    if target != root and not _is_below(target, root):
        raise RuntimeError("private extraction directory escapes staging")
    relative = target.relative_to(root)
    current = root
    for component in relative.parts:
        current /= component
        current.mkdir(mode=0o700, exist_ok=True)
        info = current.lstat()
        if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("private extraction path is not a plain directory")
        _secure_private_directory(current)


def _secure_windows_acl(path: Path, *, directory: bool) -> None:
    if os.name != "nt":
        return
    # Protected DACL: the object owner and LocalSystem retain full access;
    # directory ACEs inherit to children.  This avoids relying on a permissive
    # parent-directory ACL while keeping recovery possible for the OS.
    sddl = (
        "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)"
        if directory
        else "D:P(A;;FA;;;OW)(A;;FA;;;SY)"
    )
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_ulong),
    )
    convert.restype = ctypes.c_int
    set_security = advapi32.SetFileSecurityW
    set_security.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_void_p,
    )
    set_security.restype = ctypes.c_int
    descriptor = ctypes.c_void_p()
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not set_security(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(descriptor)


def _secure_private_directory(path: Path) -> None:
    info = path.lstat()
    if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("private extraction output is not a plain directory")
    os.chmod(path, 0o700)
    _secure_windows_acl(path, directory=True)


def _secure_private_file(path: Path) -> None:
    info = path.lstat()
    if _is_link_or_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise RuntimeError("private extraction output is not a plain file")
    os.chmod(path, 0o600)
    _secure_windows_acl(path, directory=False)


def _extract_basic_metadata(
    *, source_root: Path, data_root: Path, symbols: Tuple[str, ...]
) -> Tuple[str, ...]:
    written = []
    families = {
        "stock": tuple(symbol for symbol in symbols if not is_exchange_fund(symbol)),
        "etf": tuple(symbol for symbol in symbols if is_exchange_fund(symbol)),
    }
    for family, family_symbols in families.items():
        if not family_symbols:
            continue
        relative = f"parquet/metadata/{family}/basic.parquet"
        source = _resolved_source_file(source_root, (relative,))
        frame = _read_frame(source, description=f"{family} basic metadata")
        _require_allowed_columns(
            frame,
            allowed=_BASIC_COLUMN_ALLOWLIST[family],
            description=f"{family} basic metadata",
        )
        if not {"ts_code", "symbol"} & set(frame.columns):
            raise ValueError(
                f"{family} basic metadata must contain ts_code or symbol"
            )
        masks = [_instrument_mask(frame, symbol) for symbol in family_symbols]
        included = masks[0].copy()
        for mask in masks[1:]:
            included |= mask
        selected = frame.loc[included].copy(deep=True)
        missing = tuple(
            symbol
            for symbol, mask in zip(family_symbols, masks)
            if not bool(mask.any())
        )
        if missing:
            raise ValueError(
                f"{family} basic metadata is missing requested symbols: "
                + ",".join(missing)
            )
        requested_full = set(family_symbols)
        requested_short = {
            symbol.split(".", 1)[0] for symbol in family_symbols
        }
        for column in ("ts_code", "symbol"):
            if column not in selected.columns:
                continue
            observed = {
                str(value).strip().upper()
                for value in selected[column].dropna().tolist()
                if str(value).strip()
            }
            allowed = requested_full if column == "ts_code" else (
                requested_full | requested_short
            )
            unexpected = sorted(observed - allowed)
            if unexpected:
                raise ValueError(
                    f"{family} basic metadata {column} contains identity "
                    "values outside the requested scope: "
                    + ",".join(unexpected)
                )
        _write_parquet(data_root, relative, selected.reset_index(drop=True))
        written.append(relative)
    return tuple(written)


def _build_manifest(
    *,
    data_root: Path,
    symbols: Tuple[str, ...],
    start_date: str,
    end_date: str,
    relative_files: Iterable[str],
) -> DatasetManifest:
    identities = tuple(
        identify_parquet_file(data_root, relative)
        for relative in sorted(set(relative_files))
    )
    identity_payload = {
        "schema_version": LOCAL_EXTRACTION_SCHEMA_VERSION,
        "symbols": list(symbols),
        "start_date": start_date,
        "end_date": end_date,
        "files": [identity.to_dict() for identity in identities],
    }
    digest = hashlib.sha256(
        _canonical_json(identity_payload).encode("utf-8")
    ).hexdigest()
    return DatasetManifest(
        dataset_kind="user_supplied",
        dataset_id=f"diepi-private-extract-{digest[:20]}",
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
        generator="diepi.backtest.data.extraction_service",
        generator_version=LOCAL_EXTRACTOR_VERSION,
        files=identities,
    )


@dataclass(frozen=True)
class ExtractionScope:
    """Redacted, deterministic description of one extracted private slice."""

    symbols: Tuple[str, ...]
    requested_start_date: str
    requested_end_date: str
    storage_start_date: str
    storage_end_date: str
    metadata_included: bool
    manifest_sha256: str
    dataset_id: str
    schema_version: int = LOCAL_EXTRACTION_SCHEMA_VERSION

    def _payload(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "data_origin": "user-supplied",
            "privacy": "private",
            "redistribution_policy": "non-redistributable-by-default",
            "redistribution_authorized": False,
            "source_location_recorded": False,
            "whole_source_hash_recorded": False,
            "strategy_signals_included": False,
            "frequency": "daily",
            "price_mode": "dual",
            "symbols": list(self.symbols),
            "requested_start_date": self.requested_start_date,
            "requested_end_date": self.requested_end_date,
            "storage_start_date": self.storage_start_date,
            "storage_end_date": self.storage_end_date,
            "previous_trading_day_included": True,
            "natural_day_calendar_included": False,
            "calendar_source": "bundled",
            "calendar_id": BUILTIN_CALENDAR_ID,
            "calendar_version": BUILTIN_CALENDAR_VERSION,
            "calendar_content_sha256": BUILTIN_CALENDAR_CONTENT_SHA256,
            "calendar_coverage_start": BUILTIN_CALENDAR_START,
            "calendar_coverage_end": BUILTIN_CALENDAR_END,
            "metadata_included": self.metadata_included,
            "dataset_id": self.dataset_id,
            "manifest_sha256": self.manifest_sha256,
        }

    @property
    def scope_sha256(self) -> str:
        return hashlib.sha256(
            _canonical_json(self._payload()).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        payload = self._payload()
        payload["scope_sha256"] = self.scope_sha256
        return payload

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"


@dataclass(frozen=True)
class ExtractedWorkspace:
    workspace: Path
    data_root: Path
    manifest_file: Path
    scope_file: Path
    validation_file: Path
    manifest: DatasetManifest
    scope: ExtractionScope
    validation_report: DataValidationReport

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "data_root": str(self.data_root),
            "manifest_file": str(self.manifest_file),
            "scope_file": str(self.scope_file),
            "validation_file": str(self.validation_file),
            "dataset_kind": self.manifest.dataset_kind,
            "dataset_id": self.manifest.dataset_id,
            "manifest_sha256": self.manifest.manifest_sha256,
            "scope_sha256": self.scope.scope_sha256,
            "validation_status": self.validation_report.status,
            "validation_report_sha256": self.validation_report.report_sha256,
            "symbols": list(self.scope.symbols),
            "requested_start_date": self.scope.requested_start_date,
            "requested_end_date": self.scope.requested_end_date,
            "storage_start_date": self.scope.storage_start_date,
            "storage_end_date": self.scope.storage_end_date,
            "private": True,
            "redistribution_authorized": False,
            "strategy_signals_included": False,
        }


@dataclass(frozen=True)
class _StagingIdentity:
    st_dev: int
    st_ino: int


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _capture_staging_identity(staging: Path) -> _StagingIdentity:
    info = staging.lstat()
    if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("new extraction staging path is not a plain directory")
    return _StagingIdentity(st_dev=int(info.st_dev), st_ino=int(info.st_ino))


def _safe_cleanup_staging(
    staging: Path,
    parent: Path,
    identity: _StagingIdentity,
    parent_identity: _StagingIdentity | None = None,
) -> None:
    """Remove only the exact plain staging directory created by this call."""

    lexical = staging.absolute()
    expected_parent = _lexical_absolute(parent)
    if (
        lexical.parent != expected_parent
        or not lexical.name.startswith(_STAGING_PREFIX)
    ):
        raise RuntimeError("refusing to clean an unowned extraction staging path")
    parent_info = expected_parent.lstat()
    if _is_link_or_reparse(parent_info) or not stat.S_ISDIR(parent_info.st_mode):
        raise RuntimeError("refusing cleanup through a changed parent directory")
    if parent_identity is not None and (
        int(parent_info.st_dev), int(parent_info.st_ino)
    ) != (parent_identity.st_dev, parent_identity.st_ino):
        raise RuntimeError("refusing cleanup through a replaced parent directory")
    try:
        info = lexical.lstat()
    except FileNotFoundError:
        return
    if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError(
            "refusing to recursively clean a linked or non-directory "
            "extraction staging path"
        )
    if (int(info.st_dev), int(info.st_ino)) != (
        identity.st_dev,
        identity.st_ino,
    ):
        raise RuntimeError(
            "refusing to clean an extraction staging path whose identity changed"
        )
    shutil.rmtree(lexical)


def _write_text(path: Path, value: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
    _secure_private_file(path)


def _publish_no_replace(staging: Path, target: Path) -> None:
    """Atomically publish one directory without replacing an existing path."""

    source_bytes = os.fsencode(staging)
    target_bytes = os.fsencode(target)
    if os.name == "nt":
        # Windows MoveFile semantics used by os.rename do not replace an
        # existing destination.
        os.rename(staging, target)
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise RuntimeError(
                "atomic no-replace publication is unavailable on this Linux"
            )
        renameat2.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renameat2.restype = ctypes.c_int
        ctypes.set_errno(0)
        result = renameat2(-100, source_bytes, -100, target_bytes, 1)
    elif sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise RuntimeError(
                "atomic no-replace publication is unavailable on this macOS"
            )
        renamex_np.argtypes = (
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        renamex_np.restype = ctypes.c_int
        ctypes.set_errno(0)
        # Darwin RENAME_EXCL: fail when the destination already exists.
        result = renamex_np(source_bytes, target_bytes, 0x00000004)
    else:
        raise RuntimeError(
            "atomic no-replace extraction publication is unsupported on "
            f"{sys.platform}"
        )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise FileExistsError(
            error_number,
            "extraction workspace appeared during publication",
            str(target),
        )
    raise OSError(error_number, os.strerror(error_number), str(target))


def extract_local_data(
    *,
    source_data_root,
    workspace,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    include_metadata: bool = False,
) -> ExtractedWorkspace:
    """Atomically build a validated private dataset from an explicit source.

    Existing source files are never modified.  The destination must not exist
    and must not overlap the source ``parquet`` tree.  It may be elsewhere
    below a broad source root (for example ``SOURCE_ROOT/diepi_samples`` when
    the source root is ``SOURCE_ROOT``).  Only daily dual-price market data, required
    factor anchors, the scoped calendar, and optionally selected
    security-master rows are copied.  The source calendar is neither required
    nor copied; the bundled A-share market clock supplies the warm-up date.
    """

    canonical_symbols = _normalize_symbols(symbols)
    requested_start = _validate_date(start_date, name="start_date")
    requested_end = _validate_date(end_date, name="end_date")
    if requested_start > requested_end:
        raise ValueError("start_date must not be after end_date")
    if type(include_metadata) is not bool:
        raise TypeError("include_metadata must be bool")

    source_root = _plain_existing_path(
        Path(source_data_root), label="source data root", directory=True
    )
    target = _lexical_absolute(Path(workspace))
    if os.path.lexists(target):
        target_info = target.lstat()
        if _is_link_or_reparse(target_info):
            raise ValueError(
                "extraction workspace must not be a link or reparse point"
            )
        raise FileExistsError(f"extraction workspace already exists: {target}")
    parent = _plain_existing_path(
        target.parent,
        label="extraction workspace parent",
        directory=True,
    )
    source_parquet_root = source_root / "parquet"
    if target == source_root or _is_below(target, source_parquet_root):
        raise ValueError(
            "extraction workspace must not overlap the read-only source Parquet tree"
        )

    staging = parent / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
    staging_identity = None
    parent_identity = None
    try:
        parent_identity = _capture_staging_identity(parent)
        staging.mkdir(mode=0o700)
        staging_identity = _capture_staging_identity(staging)
        _secure_private_directory(staging)
        data_root = staging / "market-data"
        data_root.mkdir(mode=0o700)
        _secure_private_directory(data_root)
        relative_files = []

        _calendar, storage_start = _calendar_slice(
            source_root,
            start_date=requested_start,
            end_date=requested_end,
        )

        for symbol in canonical_symbols:
            prefix = "etf_" if is_exchange_fund(symbol) else ""
            directories = {
                "hfq": f"{prefix}daily",
                "raw": f"{prefix}daily_raw",
                "factor": f"{prefix}adj_factor",
            }
            for lane in ("hfq", "raw"):
                directory = directories[lane]
                source = _resolved_source_file(
                    source_root, _symbol_file_candidates(directory, symbol)
                )
                frame = _read_frame(
                    source, description=f"{symbol} {lane} daily data"
                )
                _require_allowed_columns(
                    frame,
                    allowed=_MARKET_COLUMN_ALLOWLIST,
                    description=f"{symbol} {lane} daily data",
                )
                selected = _slice_market_frame(
                    frame,
                    symbol=symbol,
                    storage_start_date=storage_start,
                    end_date=requested_end,
                    description=f"{symbol} {lane} daily data",
                )
                relative = (
                    f"parquet/timeseries/{directory}/{symbol}.parquet"
                )
                _write_parquet(data_root, relative, selected)
                relative_files.append(relative)

            factor_directory = directories["factor"]
            factor_source = _resolved_source_file(
                source_root,
                _symbol_file_candidates(factor_directory, symbol),
            )
            factor_frame = _read_frame(
                factor_source, description=f"{symbol} adjustment factors"
            )
            _require_allowed_columns(
                factor_frame,
                allowed=_FACTOR_COLUMN_ALLOWLIST,
                description=f"{symbol} adjustment factors",
            )
            factor_selected = _slice_factor_frame(
                factor_frame,
                symbol=symbol,
                storage_start_date=storage_start,
                end_date=requested_end,
                description=f"{symbol} adjustment factors",
            )
            factor_relative = (
                f"parquet/timeseries/{factor_directory}/{symbol}.parquet"
            )
            _write_parquet(data_root, factor_relative, factor_selected)
            relative_files.append(factor_relative)

        if include_metadata:
            relative_files.extend(
                _extract_basic_metadata(
                    source_root=source_root,
                    data_root=data_root,
                    symbols=canonical_symbols,
                )
            )

        manifest = _build_manifest(
            data_root=data_root,
            symbols=canonical_symbols,
            start_date=requested_start,
            end_date=requested_end,
            relative_files=relative_files,
        )
        manifest.write(data_root / DATASET_MANIFEST_FILENAME)
        _secure_private_file(data_root / DATASET_MANIFEST_FILENAME)
        scope = ExtractionScope(
            symbols=canonical_symbols,
            requested_start_date=requested_start,
            requested_end_date=requested_end,
            storage_start_date=storage_start,
            storage_end_date=requested_end,
            metadata_included=include_metadata,
            manifest_sha256=manifest.manifest_sha256,
            dataset_id=manifest.dataset_id,
        )
        _write_text(staging / EXTRACTION_SCOPE_FILENAME, scope.to_json())

        report = validate_local_data(
            data_root=data_root,
            symbols=canonical_symbols,
            start_date=requested_start,
            end_date=requested_end,
            frequency="daily",
            price_mode="dual",
            verify_manifest=True,
        )
        write_validation_report(
            report, staging / EXTRACTION_VALIDATION_FILENAME
        )
        _secure_private_file(staging / EXTRACTION_VALIDATION_FILENAME)
        if not report.contract_ready or report.manifest_status != "verified":
            codes = ", ".join(issue.code for issue in report.errors)
            raise RuntimeError(
                "extracted data failed strict validation: "
                + (codes or report.manifest_status)
            )

        if os.path.lexists(target):
            raise FileExistsError(
                f"extraction workspace appeared during publication: {target}"
            )
        parent_info = parent.lstat()
        if (
            _is_link_or_reparse(parent_info)
            or not stat.S_ISDIR(parent_info.st_mode)
            or (int(parent_info.st_dev), int(parent_info.st_ino))
            != (parent_identity.st_dev, parent_identity.st_ino)
        ):
            raise RuntimeError(
                "extraction workspace parent changed during publication"
            )
        _publish_no_replace(staging, target)
    except BaseException:
        if staging_identity is not None:
            _safe_cleanup_staging(
                staging,
                parent,
                staging_identity,
                parent_identity,
            )
        raise

    return ExtractedWorkspace(
        workspace=target,
        data_root=target / "market-data",
        manifest_file=target / "market-data" / DATASET_MANIFEST_FILENAME,
        scope_file=target / EXTRACTION_SCOPE_FILENAME,
        validation_file=target / EXTRACTION_VALIDATION_FILENAME,
        manifest=manifest,
        scope=scope,
        validation_report=report,
    )


__all__ = [
    "EXTRACTION_SCOPE_FILENAME",
    "EXTRACTION_VALIDATION_FILENAME",
    "LOCAL_EXTRACTION_SCHEMA_VERSION",
    "LOCAL_EXTRACTOR_VERSION",
    "ExtractedWorkspace",
    "ExtractionScope",
    "extract_local_data",
]
