"""Read-only orchestration for validating a scoped local market dataset."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

import pandas as pd

from ...runtime import RuntimePaths
from .cache_manager import CacheManager
from .calendar import identify_trade_calendar, load_builtin_trade_calendar
from .contract import DataContractError, DataQualityReport
from .data_provider import DataProvider, ParameterValidator
from .dataset_manifest import (
    DATASET_MANIFEST_FILENAME,
    DatasetManifest,
    logical_frame_sha256,
)
from .plain_files import (
    DATASET_PARQUET_MAX_BYTES,
    METADATA_PARQUET_MAX_BYTES,
    TRADE_CALENDAR_PARQUET_MAX_BYTES,
    plain_file_exists,
    read_plain_parquet,
)
from ..instruments import is_exchange_fund


DATA_VALIDATION_SCHEMA_VERSION = 1
SUPPORTED_VALIDATION_FREQUENCIES = frozenset({"daily", "minute"})
SUPPORTED_VALIDATION_PRICE_MODES = frozenset({"dual", "hfq", "raw"})
VALIDATION_LIMITATIONS = (
    "source_authorization_and_license_not_checked",
    "vendor_authenticity_and_economic_meaning_not_certified",
    "coverage_outside_requested_scope_not_checked",
    "missing_symbol_dates_may_be_suspensions_and_are_not_auto_repaired",
    "validation_never_downloads_repairs_sorts_intersects_or_fills_market_data",
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _canonical_date(value: str, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a YYYYMMDD string")
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{name} must use YYYYMMDD") from exc
    if parsed.strftime("%Y%m%d") != value:
        raise ValueError(f"{name} must use YYYYMMDD")
    return value


@dataclass(frozen=True)
class DataValidationScope:
    symbols: Tuple[str, ...]
    start_date: str
    end_date: str
    frequency: str = "daily"
    price_mode: str = "dual"

    def __post_init__(self) -> None:
        canonical_symbols = tuple(
            sorted(
                {
                    ParameterValidator.normalize_symbol(symbol)
                    for symbol in tuple(self.symbols)
                    if isinstance(symbol, str) and symbol.strip()
                }
            )
        )
        if not canonical_symbols:
            raise ValueError("symbols must contain at least one symbol")
        object.__setattr__(self, "symbols", canonical_symbols)
        object.__setattr__(
            self, "start_date", _canonical_date(self.start_date, name="start_date")
        )
        object.__setattr__(
            self, "end_date", _canonical_date(self.end_date, name="end_date")
        )
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if self.frequency not in SUPPORTED_VALIDATION_FREQUENCIES:
            raise ValueError(
                "frequency must be one of: daily, minute"
            )
        if self.price_mode not in SUPPORTED_VALIDATION_PRICE_MODES:
            raise ValueError(
                "price_mode must be one of: "
                + ", ".join(sorted(SUPPORTED_VALIDATION_PRICE_MODES))
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "start_date": self.start_date,
            "end_date": self.end_date,
            "frequency": self.frequency,
            "price_mode": self.price_mode,
        }


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    message: str
    symbol: Optional[str] = None
    sample_keys: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.severity not in {"error", "warning", "info"}:
            raise ValueError("validation issue severity is invalid")
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("validation issue code must be a non-empty string")
        if not isinstance(self.message, str) or not self.message:
            raise ValueError("validation issue message must be a non-empty string")
        samples = tuple(str(value) for value in self.sample_keys[:5])
        object.__setattr__(self, "sample_keys", samples)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
            "symbol": self.symbol,
            "sample_keys": list(self.sample_keys),
        }


@dataclass(frozen=True)
class DataValidationReport:
    scope: DataValidationScope
    dataset_kind: str
    manifest_status: str
    manifest_sha256: Optional[str]
    calendar: Mapping[str, Any]
    pair_reports: Tuple[DataQualityReport, ...]
    issues: Tuple[ValidationIssue, ...]
    limitations: Tuple[str, ...] = VALIDATION_LIMITATIONS
    schema_version: int = DATA_VALIDATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported validation report schema_version")
        if self.manifest_status not in {"absent", "verified", "failed", "not_checked"}:
            raise ValueError("manifest_status is invalid")
        if any(not isinstance(value, DataQualityReport) for value in self.pair_reports):
            raise TypeError("pair_reports must contain DataQualityReport values")
        if any(not isinstance(value, ValidationIssue) for value in self.issues):
            raise TypeError("issues must contain ValidationIssue values")

    @property
    def errors(self) -> Tuple[ValidationIssue, ...]:
        return tuple(value for value in self.issues if value.severity == "error")

    @property
    def warnings(self) -> Tuple[ValidationIssue, ...]:
        return tuple(value for value in self.issues if value.severity == "warning")

    @property
    def contract_ready(self) -> bool:
        return (
            not self.errors
            and len(self.pair_reports) == len(self.scope.symbols)
            and all(value.ok for value in self.pair_reports)
        )

    @property
    def status(self) -> str:
        return "pass" if self.contract_ready else "fail"

    def _payload(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "contract_ready": self.contract_ready,
            "scope": self.scope.to_dict(),
            "dataset_kind": self.dataset_kind,
            "manifest_status": self.manifest_status,
            "manifest_sha256": self.manifest_sha256,
            "calendar": dict(self.calendar),
            "pair_reports": [value.to_dict() for value in self.pair_reports],
            "issues": [value.to_dict() for value in self.issues],
            "limitations": list(self.limitations),
        }

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(_canonical_json(self._payload()).encode("utf-8")).hexdigest()

    def to_dict(self) -> Dict[str, Any]:
        payload = self._payload()
        payload["report_sha256"] = self.report_sha256
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _issue_sort_key(issue: ValidationIssue):
    severity_order = {"error": 0, "warning": 1, "info": 2}
    return (
        severity_order[issue.severity],
        issue.code,
        issue.symbol or "",
        issue.message,
        issue.sample_keys,
    )


def _validate_calendar(
    path: Path,
    scope: DataValidationScope,
    *,
    data_root: Path,
):
    issues = []
    presence_error = None
    try:
        local_override_exists = plain_file_exists(
            path,
            root=data_root,
            label="local trade-calendar override",
        )
    except Exception as exc:
        local_override_exists = True
        presence_error = exc
    source = "local_override" if local_override_exists else "bundled"
    result: Dict[str, Any] = {
        "status": "fail",
        "source": source,
        "calendar_id": None,
        "version": None,
        "content_sha256": None,
        "rows": 0,
        "first_date": None,
        "last_date": None,
        "open_days_in_scope": 0,
    }
    if presence_error is not None:
        issues.append(
            ValidationIssue(
                "error",
                "TRADE_CALENDAR_READ_ERROR",
                "local trade-calendar override could not be read: "
                f"{type(presence_error).__name__}: {presence_error}",
            )
        )
        return result, frozenset(), issues
    if local_override_exists:
        try:
            frame = read_plain_parquet(
                path,
                root=data_root,
                max_bytes=TRADE_CALENDAR_PARQUET_MAX_BYTES,
                label="local trade-calendar override",
            )
        except Exception as exc:
            issues.append(
                ValidationIssue(
                    "error",
                    "TRADE_CALENDAR_READ_ERROR",
                    "local trade-calendar override could not be read: "
                    f"{type(exc).__name__}: {exc}",
                )
            )
            return result, frozenset(), issues
    else:
        try:
            frame = load_builtin_trade_calendar()
        except Exception as exc:
            issues.append(
                ValidationIssue(
                    "error",
                    "BUILTIN_TRADE_CALENDAR_ERROR",
                    "bundled trade calendar failed its identity check: "
                    f"{type(exc).__name__}: {exc}",
                )
            )
            return result, frozenset(), issues
    result["rows"] = len(frame)
    required = {"cal_date", "is_open"}
    missing = sorted(required - set(frame.columns))
    for field in missing:
        issues.append(
            ValidationIssue(
                "error",
                "TRADE_CALENDAR_MISSING_COLUMN",
                "trade calendar is missing a required column",
                sample_keys=(field,),
            )
        )
    if missing or frame.empty:
        if frame.empty:
            issues.append(
                ValidationIssue(
                    "error", "EMPTY_TRADE_CALENDAR", "trade calendar has no rows"
                )
            )
        return result, frozenset(), issues

    raw_dates = (
        frame["cal_date"]
        .astype("string")
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )
    valid_dates = raw_dates.str.fullmatch(r"\d{8}", na=False)
    parsed = pd.to_datetime(
        raw_dates.where(valid_dates), format="%Y%m%d", errors="coerce"
    )
    invalid_dates = ~valid_dates | parsed.isna()
    if invalid_dates.any():
        samples = tuple(raw_dates[invalid_dates].astype(str).head(5))
        issues.append(
            ValidationIssue(
                "error",
                "TRADE_CALENDAR_INVALID_DATE",
                "cal_date must use valid YYYYMMDD values",
                sample_keys=samples,
            )
        )
    numeric_open = pd.to_numeric(frame["is_open"], errors="coerce")
    invalid_open = numeric_open.isna() | ~numeric_open.isin([0, 1])
    if invalid_open.any():
        samples = tuple(frame.loc[invalid_open, "is_open"].astype(str).head(5))
        issues.append(
            ValidationIssue(
                "error",
                "TRADE_CALENDAR_INVALID_IS_OPEN",
                "is_open must contain only 0 or 1",
                sample_keys=samples,
            )
        )

    canonical_dates = tuple(sorted(set(raw_dates[~invalid_dates].tolist())))
    if canonical_dates:
        result["first_date"] = canonical_dates[0]
        result["last_date"] = canonical_dates[-1]
        if canonical_dates[0] > scope.start_date or canonical_dates[-1] < scope.end_date:
            issues.append(
                ValidationIssue(
                    "error",
                    "TRADE_CALENDAR_SCOPE_UNPROVEN",
                    "trade calendar does not prove the complete requested interval",
                    sample_keys=(scope.start_date, scope.end_date),
                )
            )
        else:
            expected_scope_dates = set(
                pd.date_range(
                    scope.start_date, scope.end_date, freq="D"
                ).strftime("%Y%m%d")
            )
            missing_scope_dates = tuple(
                sorted(expected_scope_dates - set(canonical_dates))
            )
            if missing_scope_dates:
                issues.append(
                    ValidationIssue(
                        "error",
                        "TRADE_CALENDAR_SCOPE_UNPROVEN",
                        "trade calendar is missing natural dates in the requested interval",
                        sample_keys=missing_scope_dates,
                    )
                )
    open_days = frozenset(
        raw_dates[
            (~invalid_dates)
            & (~invalid_open)
            & numeric_open.eq(1)
            & raw_dates.ge(scope.start_date)
            & raw_dates.le(scope.end_date)
        ].tolist()
    )
    result["open_days_in_scope"] = len(open_days)
    if not open_days:
        issues.append(
            ValidationIssue(
                "error",
                "TRADE_CALENDAR_NO_OPEN_DAYS",
                "trade calendar contains no open days in the requested interval",
            )
        )
    if not any(value.severity == "error" for value in issues):
        try:
            identity = identify_trade_calendar(frame, source=source)
        except Exception as exc:
            issues.append(
                ValidationIssue(
                    "error",
                    "TRADE_CALENDAR_INVALID_OVERRIDE",
                    "trade calendar cannot prove a complete independent clock: "
                    f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            result.update(identity.to_dict())
            # Retain the established report names alongside the identity names.
            result["first_date"] = identity.coverage_start
            result["last_date"] = identity.coverage_end
    if not any(value.severity == "error" for value in issues):
        result["status"] = "pass"
    return result, open_days, issues


def _validate_basic_metadata_file(
    *,
    root: Path,
    symbols: Sequence[str],
    family: str,
):
    """Validate one instrument family's security-master slice.

    Market bars already route exchange funds to the ``etf_*`` stores.  The
    onboarding validator must use the same classification when it checks
    security-master coverage; otherwise a valid ETF-only dataset is reported
    as missing ``stock/basic.parquet``.
    """

    if family == "etf":
        relative_path = "etf/basic.parquet"
        issue_prefix = "ETF_BASIC"
        point_in_time_fields = ("list_date",)
    elif family == "stock":
        relative_path = "stock/basic.parquet"
        issue_prefix = "STOCK_BASIC"
        point_in_time_fields = ("list_date", "delist_date")
    else:  # defensive: this is an internal, closed set
        raise ValueError("unsupported basic metadata family")

    issues = []
    path = root / "parquet" / "metadata" / relative_path
    try:
        metadata_exists = plain_file_exists(
            path,
            root=root,
            label=f"{relative_path} metadata",
        )
    except Exception as exc:
        return [
            ValidationIssue(
                "warning",
                f"{issue_prefix}_READ_ERROR",
                f"{relative_path} could not be inspected: {type(exc).__name__}: {exc}",
            )
        ]
    if not metadata_exists:
        return [
            ValidationIssue(
                "warning",
                f"{issue_prefix}_NOT_CHECKED",
                f"{relative_path} is absent; listing and instrument metadata remain unverified",
            )
        ]
    try:
        frame = read_plain_parquet(
            path,
            root=root,
            max_bytes=METADATA_PARQUET_MAX_BYTES,
            label=f"{relative_path} metadata",
        )
    except Exception as exc:
        return [
            ValidationIssue(
                "warning",
                f"{issue_prefix}_READ_ERROR",
                f"{relative_path} could not be inspected: {type(exc).__name__}: {exc}",
            )
        ]
    code_column = "ts_code" if "ts_code" in frame.columns else (
        "symbol" if "symbol" in frame.columns else None
    )
    if code_column is None:
        return [
            ValidationIssue(
                "warning",
                f"{issue_prefix}_CODE_COLUMN_MISSING",
                f"{relative_path} has neither ts_code nor symbol",
            )
        ]
    present = set(frame[code_column].astype("string").str.strip().dropna())
    missing_symbols = tuple(symbol for symbol in symbols if symbol not in present)
    if missing_symbols:
        issues.append(
            ValidationIssue(
                "warning",
                f"{issue_prefix}_SYMBOL_MISSING",
                f"requested symbols are missing from {relative_path}",
                sample_keys=missing_symbols,
            )
        )
    missing_point_in_time = tuple(
        field for field in point_in_time_fields if field not in frame.columns
    )
    if missing_point_in_time:
        issues.append(
            ValidationIssue(
                "warning",
                f"{issue_prefix}_POINT_IN_TIME_FIELDS_MISSING",
                "listing interval metadata is incomplete",
                sample_keys=missing_point_in_time,
            )
        )
    return issues


def _validate_instrument_basic(root: Path, scope: DataValidationScope):
    fund_symbols = tuple(
        symbol for symbol in scope.symbols if is_exchange_fund(symbol)
    )
    stock_symbols = tuple(
        symbol for symbol in scope.symbols if not is_exchange_fund(symbol)
    )
    issues = []
    if stock_symbols:
        issues.extend(
            _validate_basic_metadata_file(
                root=root,
                symbols=stock_symbols,
                family="stock",
            )
        )
    if fund_symbols:
        issues.extend(
            _validate_basic_metadata_file(
                root=root,
                symbols=fund_symbols,
                family="etf",
            )
        )
    return issues


def _verify_manifest(root: Path):
    path = root / DATASET_MANIFEST_FILENAME
    if not os.path.lexists(path):
        return "absent", None, "user_supplied_unmanifested", [], None
    issues = []
    try:
        manifest = DatasetManifest.read(path, root=root)
    except Exception as exc:
        issues.append(
            ValidationIssue(
                "error",
                "DATASET_MANIFEST_INVALID",
                f"dataset manifest is invalid: {type(exc).__name__}: {exc}",
            )
        )
        return "failed", None, "unknown", issues, None

    for identity in manifest.files:
        file_path = root.joinpath(*Path(identity.path).parts)
        if not os.path.lexists(file_path):
            issues.append(
                ValidationIssue(
                    "error",
                    "DATASET_MANIFEST_FILE_MISSING",
                    "a dataset manifest file is missing",
                    sample_keys=(identity.path,),
                )
            )
            continue
        try:
            frame = read_plain_parquet(
                file_path,
                root=root,
                max_bytes=DATASET_PARQUET_MAX_BYTES,
                label=f"dataset member {identity.path}",
            )
            logical_hash = logical_frame_sha256(frame)
        except Exception as exc:
            issues.append(
                ValidationIssue(
                    "error",
                    "DATASET_MANIFEST_FILE_READ_ERROR",
                    f"a manifest file could not be read: {type(exc).__name__}: {exc}",
                    sample_keys=(identity.path,),
                )
            )
            continue
        actual_columns = tuple(sorted(str(column) for column in frame.columns))
        if (
            len(frame) != identity.rows
            or actual_columns != identity.columns
            or logical_hash != identity.logical_sha256
        ):
            issues.append(
                ValidationIssue(
                    "error",
                    "DATASET_MANIFEST_IDENTITY_MISMATCH",
                    "a manifest file no longer matches its logical identity",
                    sample_keys=(identity.path,),
                )
            )
    status = "failed" if issues else "verified"
    return status, manifest.manifest_sha256, manifest.dataset_kind, issues, manifest


def _price_spaces(price_mode: str):
    if price_mode == "dual":
        return "hfq", "raw"
    return price_mode, price_mode


def validate_local_data(
    *,
    data_root,
    symbols: Sequence[str],
    start_date: str,
    end_date: str,
    frequency: str = "daily",
    price_mode: str = "dual",
    verify_manifest: bool = True,
) -> DataValidationReport:
    """Validate one explicit scope without changing files or environment."""

    scope = DataValidationScope(
        symbols=tuple(symbols),
        start_date=start_date,
        end_date=end_date,
        frequency=frequency,
        price_mode=price_mode,
    )
    paths = RuntimePaths.resolve(data_root=data_root, require_data_root=False)
    root = paths.data_root
    issues = []
    pair_reports = []
    calendar: Mapping[str, Any] = {
        "status": "fail",
        "source": None,
        "calendar_id": None,
        "version": None,
        "content_sha256": None,
        "rows": 0,
        "first_date": None,
        "last_date": None,
        "open_days_in_scope": 0,
    }
    open_days = frozenset()
    if not root.is_dir():
        issues.append(
            ValidationIssue(
                "error",
                "DATA_ROOT_MISSING",
                "the explicit data root does not exist or is not a directory",
            )
        )
        return DataValidationReport(
            scope=scope,
            dataset_kind="unknown",
            manifest_status="not_checked",
            manifest_sha256=None,
            calendar=calendar,
            pair_reports=(),
            issues=tuple(issues),
        )

    if verify_manifest:
        (
            manifest_status,
            manifest_sha256,
            dataset_kind,
            manifest_issues,
            manifest,
        ) = _verify_manifest(root)
        issues.extend(manifest_issues)
        if manifest is not None:
            outside_manifest = tuple(
                symbol for symbol in scope.symbols if symbol not in manifest.symbols
            )
            if outside_manifest:
                issues.append(
                    ValidationIssue(
                        "warning",
                        "SCOPE_OUTSIDE_DATASET_MANIFEST",
                        "requested symbols are not declared by the dataset manifest",
                        sample_keys=outside_manifest,
                    )
                )
    else:
        manifest_status = "not_checked"
        manifest_sha256 = None
        dataset_kind = "unknown"

    calendar, open_days, calendar_issues = _validate_calendar(
        root / "parquet" / "metadata" / "common" / "trade_cal.parquet",
        scope,
        data_root=root,
    )
    issues.extend(calendar_issues)
    issues.extend(_validate_instrument_basic(root, scope))

    strategy_space, execution_space = _price_spaces(scope.price_mode)
    cache = CacheManager(data_root=root)
    provider = DataProvider(
        price_mode=strategy_space,
        execution_price_mode=execution_space,
        cache_manager=cache,
    )
    for symbol in scope.symbols:
        if scope.frequency == "minute":
            try:
                # Both cash engines use the daily pair to bind the completed
                # data window and to provide causal pre-open daily history.
                provider.get_aligned_pair(
                    symbol,
                    frequency="daily",
                    start=scope.start_date,
                    end=scope.end_date,
                )
            except DataContractError as exc:
                issues.append(
                    ValidationIssue(
                        "error",
                        "MINUTE_COMPANION_DAILY_REQUIRED",
                        "minute v1 requires a valid same-symbol, same-price-"
                        "lane daily companion: "
                        + ", ".join(sorted(set(exc.report.issue_codes))),
                        symbol=symbol,
                    )
                )
            except Exception as exc:
                issues.append(
                    ValidationIssue(
                        "error",
                        "MINUTE_COMPANION_DAILY_REQUIRED",
                        "minute v1 daily companion could not be inspected: "
                        f"{type(exc).__name__}: {exc}",
                        symbol=symbol,
                    )
                )
        try:
            aligned = provider.get_aligned_pair(
                symbol,
                frequency=scope.frequency,
                start=scope.start_date,
                end=scope.end_date,
            )
            pair_reports.append(aligned.report)
            observed = frozenset(
                pd.DatetimeIndex(aligned.strategy.index).strftime("%Y%m%d")
            )
            missing_open_days = tuple(sorted(open_days - observed))
            if missing_open_days:
                issues.append(
                    ValidationIssue(
                        "warning",
                        "UNEXPLAINED_BAR_GAPS",
                        "calendar open days without bars were observed; they may be suspensions and were not repaired",
                        symbol=symbol,
                        sample_keys=missing_open_days,
                    )
                )
        except DataContractError as exc:
            pair_reports.append(exc.report)
            issues.append(
                ValidationIssue(
                    "error",
                    "MARKET_PAIR_CONTRACT_FAILED",
                    "strict market-data pair validation failed: "
                    + ", ".join(sorted(set(exc.report.issue_codes))),
                    symbol=symbol,
                )
            )
        except Exception as exc:
            issues.append(
                ValidationIssue(
                    "error",
                    "MARKET_PAIR_VALIDATION_ERROR",
                    f"market-data pair could not be inspected: {type(exc).__name__}: {exc}",
                    symbol=symbol,
                )
            )

    ordered_reports = tuple(sorted(pair_reports, key=lambda value: value.symbol))
    ordered_issues = tuple(sorted(issues, key=_issue_sort_key))
    return DataValidationReport(
        scope=scope,
        dataset_kind=dataset_kind,
        manifest_status=manifest_status,
        manifest_sha256=manifest_sha256,
        calendar=calendar,
        pair_reports=ordered_reports,
        issues=ordered_issues,
    )


def write_validation_report(report: DataValidationReport, path) -> Path:
    if not isinstance(report, DataValidationReport):
        raise TypeError("report must be a DataValidationReport")
    target = Path(path)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(report.to_json())
    return target


__all__ = [
    "DATA_VALIDATION_SCHEMA_VERSION",
    "DataValidationReport",
    "DataValidationScope",
    "SUPPORTED_VALIDATION_FREQUENCIES",
    "SUPPORTED_VALIDATION_PRICE_MODES",
    "VALIDATION_LIMITATIONS",
    "ValidationIssue",
    "validate_local_data",
    "write_validation_report",
]
