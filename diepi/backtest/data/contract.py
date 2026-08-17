"""Deterministic market-data contract for paired backtest tracks.

This module is deliberately independent from providers, caches, and the
backtest engine.  It validates a strategy-price track and an execution-price
track as one atomic input.  Validation never drops, fills, sorts, or falls
back to the other track.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import json
import math
from numbers import Real
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Type, TypeVar

import numpy as np
import pandas as pd


CONTRACT_VERSION = "DC-1"
SCHEMA_VERSION = 4
MAX_ISSUE_SAMPLES = 5

ADJUSTMENT_FACTOR_IDENTITY_SCHEMA = "diepi.adjustment_factor_identity"
ADJUSTMENT_FACTOR_IDENTITY_SCHEMA_VERSION = 1
ADJUSTMENT_FACTOR_IDENTITY_VERSION = "AFI-1"
ADJUSTMENT_FACTOR_RELATION = (
    "hfq_close=raw_close*(adj_factor/base_factor)"
)
# The stored daily datasets are rounded to cents.  Half a cent is therefore
# the narrowest deterministic absolute tolerance that accepts a correctly
# rounded value.  The tiny relative term covers floating-point multiplication
# only; it is not a percentage-error allowance.
ADJUSTMENT_PRICE_ABS_TOLERANCE = 0.0050001
ADJUSTMENT_PRICE_REL_TOLERANCE = 1e-9


class TrackRole(str, Enum):
    """The semantic role of one price track."""

    STRATEGY = "strategy"
    EXECUTION = "execution"


class PriceSpace(str, Enum):
    """Supported price coordinate systems."""

    RAW = "raw"
    HFQ = "hfq"


class AmountUnit(str, Enum):
    """Source unit for the monetary ``amount`` column.

    The aligned output always uses yuan.  Requiring the source unit at the
    boundary avoids guessing from frequency and prevents the engine from
    applying the provider's daily ``x1000`` conversion twice.
    """

    YUAN = "yuan"
    THOUSAND_YUAN = "thousand_yuan"


class Frequency(str, Enum):
    """Supported bar frequencies for the minimal contract."""

    DAILY = "daily"
    MINUTE = "minute"


class PreCloseSource(str, Enum):
    """Audited provenance for each track's ``pre_close`` column."""

    ABSENT = "absent"
    NATIVE = "native"
    SAME_LANE_DAILY = "same_lane_daily"
    SAME_LANE_DAILY_WITH_SUSPENSION_FALLBACK = (
        "same_lane_daily_with_suspension_fallback"
    )


# Public, stable issue-code table.  Some codes are reserved for the provider
# integration layer; keeping them here prevents report-schema drift later.
ISSUE_MISSING_DATASET = "MISSING_DATASET"
ISSUE_READ_ERROR = "READ_ERROR"
ISSUE_MISSING_COLUMN = "MISSING_COLUMN"
ISSUE_DUPLICATE_COLUMN = "DUPLICATE_COLUMN"
ISSUE_DUPLICATE_KEY = "DUPLICATE_KEY"
ISSUE_NON_MONOTONIC_KEY = "NON_MONOTONIC_KEY"
ISSUE_KEY_MISMATCH = "KEY_MISMATCH"
ISSUE_PRICE_SPACE_MISSING = "PRICE_SPACE_MISSING"
ISSUE_NONFINITE_VALUE = "NONFINITE_VALUE"
ISSUE_NONPOSITIVE_PRICE = "NONPOSITIVE_PRICE"
ISSUE_OHLC_INCONSISTENT = "OHLC_INCONSISTENT"
ISSUE_INVALID_PRE_CLOSE = "INVALID_PRE_CLOSE"
ISSUE_INVALID_AMOUNT = "INVALID_AMOUNT"
ISSUE_FREQUENCY_DOWNGRADE = "FREQUENCY_DOWNGRADE"
ISSUE_UNSUPPORTED_INSTRUMENT = "UNSUPPORTED_INSTRUMENT"
ISSUE_MISSING_TIMESTAMP = "MISSING_TIMESTAMP"
ISSUE_INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
ISSUE_SYMBOL_MISMATCH = "SYMBOL_MISMATCH"
ISSUE_MISSING_ADJ_FACTOR = "MISSING_ADJ_FACTOR"
ISSUE_DUPLICATE_ADJ_FACTOR = "DUPLICATE_ADJ_FACTOR"
ISSUE_NON_MONOTONIC_ADJ_FACTOR = "NON_MONOTONIC_ADJ_FACTOR"
ISSUE_INVALID_ADJ_FACTOR = "INVALID_ADJ_FACTOR"
ISSUE_ADJ_FACTOR_SCOPE_MISMATCH = "ADJ_FACTOR_SCOPE_MISMATCH"
ISSUE_PRICE_IDENTITY_MISMATCH = "PRICE_IDENTITY_MISMATCH"

ISSUE_CODES: Tuple[str, ...] = (
    ISSUE_MISSING_DATASET,
    ISSUE_READ_ERROR,
    ISSUE_MISSING_COLUMN,
    ISSUE_DUPLICATE_COLUMN,
    ISSUE_DUPLICATE_KEY,
    ISSUE_NON_MONOTONIC_KEY,
    ISSUE_KEY_MISMATCH,
    ISSUE_PRICE_SPACE_MISSING,
    ISSUE_NONFINITE_VALUE,
    ISSUE_NONPOSITIVE_PRICE,
    ISSUE_OHLC_INCONSISTENT,
    ISSUE_INVALID_PRE_CLOSE,
    ISSUE_INVALID_AMOUNT,
    ISSUE_FREQUENCY_DOWNGRADE,
    ISSUE_UNSUPPORTED_INSTRUMENT,
    ISSUE_MISSING_TIMESTAMP,
    ISSUE_INVALID_TIMESTAMP,
    ISSUE_SYMBOL_MISMATCH,
    ISSUE_MISSING_ADJ_FACTOR,
    ISSUE_DUPLICATE_ADJ_FACTOR,
    ISSUE_NON_MONOTONIC_ADJ_FACTOR,
    ISSUE_INVALID_ADJ_FACTOR,
    ISSUE_ADJ_FACTOR_SCOPE_MISMATCH,
    ISSUE_PRICE_IDENTITY_MISMATCH,
)

_ISSUE_ORDER = {code: position for position, code in enumerate(ISSUE_CODES)}
_TRACK_ORDER = {None: 0, TrackRole.STRATEGY: 1, TrackRole.EXECUTION: 2}
_OHLC_COLUMNS = ("open", "high", "low", "close")
_EXECUTION_ONLY_COLUMNS = ("pre_close", "amount")


@dataclass(frozen=True)
class DataQualityIssue:
    """One aggregated, deterministic contract violation."""

    code: str
    track: Optional[TrackRole]
    count: int
    message: str
    field: Optional[str] = None
    sample_keys: Tuple[str, ...] = ()
    severity: str = "error"

    def __post_init__(self) -> None:
        if self.code not in ISSUE_CODES:
            raise ValueError("unknown data-quality issue code: %s" % self.code)
        if self.track is not None and not isinstance(self.track, TrackRole):
            raise TypeError("issue track must be a TrackRole or None")
        if self.count < 1:
            raise ValueError("issue count must be positive")
        if len(self.sample_keys) > MAX_ISSUE_SAMPLES:
            raise ValueError("issue sample exceeds MAX_ISSUE_SAMPLES")
        if tuple(sorted(set(self.sample_keys))) != self.sample_keys:
            raise ValueError("issue sample keys must be unique and sorted")

    def to_dict(self) -> Dict[str, Any]:
        """Return a stable, JSON-friendly representation."""

        return {
            "code": self.code,
            "severity": self.severity,
            "track": self.track.value if self.track is not None else None,
            "field": self.field,
            "count": self.count,
            "sample_keys": list(self.sample_keys),
            "message": self.message,
        }


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ADJUSTMENT_FACTOR_IDENTITY_KEYS = frozenset(
    {
        "base_date",
        "base_factor",
        "execution_price_space",
        "factor_values_sha256",
        "frequency",
        "identity_version",
        "price_abs_tolerance",
        "price_rel_tolerance",
        "relation",
        "schema",
        "schema_version",
        "scope_end",
        "scope_observation_keys_sha256",
        "scope_start",
        "scope_trade_dates_sha256",
        "scope_trade_days",
        "source",
        "strategy_price_space",
        "symbol",
        "validated_close_rows",
    }
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _strict_json_object_pairs(pairs):
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key: %s" % key)
        value[key] = item
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON constant is not allowed: %s" % value)


def _canonical_daily_date(value: Any, argument: str) -> str:
    try:
        timestamp = _parse_one_timestamp(value, Frequency.DAILY)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError("%s must be a valid daily date" % argument) from error
    return timestamp.strftime("%Y%m%d")


@dataclass(frozen=True)
class AdjustmentFactorIdentity:
    """Compact, immutable proof of one validated raw/HFQ mapping.

    The exact trade-day set and exact factor values are represented by
    separate SHA-256 identities.  Together with the inclusive endpoints and
    count this exposes the complete scope without embedding thousands of
    factor rows into every result assumption.
    """

    symbol: str
    frequency: Frequency
    strategy_price_space: PriceSpace
    execution_price_space: PriceSpace
    scope_start: str
    scope_end: str
    scope_trade_days: int
    scope_trade_dates_sha256: str
    scope_observation_keys_sha256: str
    validated_close_rows: int
    source: str
    base_date: str
    base_factor: float
    factor_values_sha256: str
    relation: str = ADJUSTMENT_FACTOR_RELATION
    price_abs_tolerance: float = ADJUSTMENT_PRICE_ABS_TOLERANCE
    price_rel_tolerance: float = ADJUSTMENT_PRICE_REL_TOLERANCE
    schema: str = ADJUSTMENT_FACTOR_IDENTITY_SCHEMA
    schema_version: int = ADJUSTMENT_FACTOR_IDENTITY_SCHEMA_VERSION
    identity_version: str = ADJUSTMENT_FACTOR_IDENTITY_VERSION

    def __post_init__(self) -> None:
        canonical_symbol = _normalize_symbol(self.symbol)
        if canonical_symbol != self.symbol:
            raise ValueError("adjustment identity symbol must be canonical")
        object.__setattr__(
            self,
            "frequency",
            _coerce_enum(self.frequency, Frequency, "frequency"),
        )
        object.__setattr__(
            self,
            "strategy_price_space",
            _coerce_enum(
                self.strategy_price_space,
                PriceSpace,
                "strategy_price_space",
            ),
        )
        object.__setattr__(
            self,
            "execution_price_space",
            _coerce_enum(
                self.execution_price_space,
                PriceSpace,
                "execution_price_space",
            ),
        )
        if self.strategy_price_space is self.execution_price_space:
            raise ValueError(
                "adjustment identity requires distinct raw and hfq spaces"
            )
        if {
            self.strategy_price_space,
            self.execution_price_space,
        } != {PriceSpace.RAW, PriceSpace.HFQ}:
            raise ValueError("adjustment identity supports only raw/hfq pairs")

        canonical_start = _canonical_daily_date(
            self.scope_start, "scope_start"
        )
        canonical_end = _canonical_daily_date(self.scope_end, "scope_end")
        canonical_base = _canonical_daily_date(self.base_date, "base_date")
        if canonical_start != self.scope_start or canonical_end != self.scope_end:
            raise ValueError("adjustment identity scope dates must use YYYYMMDD")
        if canonical_base != self.base_date:
            raise ValueError("adjustment identity base_date must use YYYYMMDD")
        if canonical_start > canonical_end:
            raise ValueError("adjustment identity scope_start exceeds scope_end")
        if canonical_base > canonical_start:
            raise ValueError("adjustment factor base_date exceeds scope_start")

        if type(self.scope_trade_days) is not int or self.scope_trade_days < 1:
            raise ValueError("scope_trade_days must be a positive exact int")
        if (
            type(self.validated_close_rows) is not int
            or self.validated_close_rows < self.scope_trade_days
        ):
            raise ValueError(
                "validated_close_rows must cover every scoped trade day"
            )
        if not isinstance(self.scope_trade_dates_sha256, str) or not _SHA256_RE.fullmatch(
            self.scope_trade_dates_sha256
        ):
            raise ValueError("scope_trade_dates_sha256 must be lowercase SHA-256")
        if not isinstance(self.scope_observation_keys_sha256, str) or not _SHA256_RE.fullmatch(
            self.scope_observation_keys_sha256
        ):
            raise ValueError(
                "scope_observation_keys_sha256 must be lowercase SHA-256"
            )
        if not isinstance(self.factor_values_sha256, str) or not _SHA256_RE.fullmatch(
            self.factor_values_sha256
        ):
            raise ValueError("factor_values_sha256 must be lowercase SHA-256")

        if type(self.source) is not str or not self.source.strip():
            raise ValueError("adjustment factor source must be non-empty")
        if self.source != self.source.strip() or any(
            character in self.source for character in "\r\n\t"
        ):
            raise ValueError("adjustment factor source must be canonical text")
        if type(self.base_factor) is not float:
            raise TypeError("base_factor must be exactly float")
        if not math.isfinite(self.base_factor) or self.base_factor <= 0.0:
            raise ValueError("base_factor must be finite and positive")
        if self.relation != ADJUSTMENT_FACTOR_RELATION:
            raise ValueError("unsupported adjustment price relation")
        if (
            type(self.price_abs_tolerance) is not float
            or self.price_abs_tolerance != ADJUSTMENT_PRICE_ABS_TOLERANCE
        ):
            raise ValueError("unsupported adjustment absolute tolerance")
        if (
            type(self.price_rel_tolerance) is not float
            or self.price_rel_tolerance != ADJUSTMENT_PRICE_REL_TOLERANCE
        ):
            raise ValueError("unsupported adjustment relative tolerance")
        if self.schema != ADJUSTMENT_FACTOR_IDENTITY_SCHEMA:
            raise ValueError("unsupported adjustment identity schema")
        if (
            type(self.schema_version) is not int
            or self.schema_version != ADJUSTMENT_FACTOR_IDENTITY_SCHEMA_VERSION
        ):
            raise ValueError("unsupported adjustment identity schema_version")
        if self.identity_version != ADJUSTMENT_FACTOR_IDENTITY_VERSION:
            raise ValueError("unsupported adjustment identity version")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "base_date": self.base_date,
            "base_factor": self.base_factor,
            "execution_price_space": self.execution_price_space.value,
            "factor_values_sha256": self.factor_values_sha256,
            "frequency": self.frequency.value,
            "identity_version": self.identity_version,
            "price_abs_tolerance": self.price_abs_tolerance,
            "price_rel_tolerance": self.price_rel_tolerance,
            "relation": self.relation,
            "schema": self.schema,
            "schema_version": self.schema_version,
            "scope_end": self.scope_end,
            "scope_observation_keys_sha256": (
                self.scope_observation_keys_sha256
            ),
            "scope_start": self.scope_start,
            "scope_trade_dates_sha256": self.scope_trade_dates_sha256,
            "scope_trade_days": self.scope_trade_days,
            "source": self.source,
            "strategy_price_space": self.strategy_price_space.value,
            "symbol": self.symbol,
            "validated_close_rows": self.validated_close_rows,
        }

    def to_json(self) -> str:
        """Canonical value suitable for one result assumption."""

        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> "AdjustmentFactorIdentity":
        if type(value) is not dict:
            raise TypeError("adjustment factor identity must be exactly dict")
        if set(value) != _ADJUSTMENT_FACTOR_IDENTITY_KEYS:
            missing = sorted(_ADJUSTMENT_FACTOR_IDENTITY_KEYS - set(value))
            unknown = sorted(set(value) - _ADJUSTMENT_FACTOR_IDENTITY_KEYS)
            raise ValueError(
                "adjustment factor identity keys mismatch; missing=%s unknown=%s"
                % (missing, unknown)
            )
        restored = cls(
            symbol=value["symbol"],
            frequency=value["frequency"],
            strategy_price_space=value["strategy_price_space"],
            execution_price_space=value["execution_price_space"],
            scope_start=value["scope_start"],
            scope_end=value["scope_end"],
            scope_trade_days=value["scope_trade_days"],
            scope_trade_dates_sha256=value["scope_trade_dates_sha256"],
            scope_observation_keys_sha256=(
                value["scope_observation_keys_sha256"]
            ),
            validated_close_rows=value["validated_close_rows"],
            source=value["source"],
            base_date=value["base_date"],
            base_factor=value["base_factor"],
            factor_values_sha256=value["factor_values_sha256"],
            relation=value["relation"],
            price_abs_tolerance=value["price_abs_tolerance"],
            price_rel_tolerance=value["price_rel_tolerance"],
            schema=value["schema"],
            schema_version=value["schema_version"],
            identity_version=value["identity_version"],
        )
        if restored.to_dict() != value:
            raise ValueError("adjustment factor identity is not canonical")
        return restored

    @classmethod
    def from_json(cls, value: Any) -> "AdjustmentFactorIdentity":
        if type(value) is not str:
            raise TypeError("adjustment factor identity JSON must be exactly str")
        payload = json.loads(
            value,
            object_pairs_hook=_strict_json_object_pairs,
            parse_constant=_reject_json_constant,
        )
        restored = cls.from_dict(payload)
        if restored.to_json() != value:
            raise ValueError("adjustment factor identity JSON must be canonical")
        return restored


def _ordered_adjustment_identities(
    identities: Iterable[AdjustmentFactorIdentity],
) -> Tuple[AdjustmentFactorIdentity, ...]:
    if isinstance(identities, (str, bytes)):
        raise TypeError("identities must be an iterable of identity objects")
    items = tuple(identities)
    if not items:
        raise ValueError("at least one adjustment factor identity is required")
    if any(not isinstance(item, AdjustmentFactorIdentity) for item in items):
        raise TypeError("every identity must be AdjustmentFactorIdentity")
    unique = {}
    for item in items:
        key = (
            item.symbol,
            item.frequency.value,
            item.scope_start,
            item.scope_end,
            item.scope_observation_keys_sha256,
        )
        previous = unique.get(key)
        if previous is not None and previous != item:
            raise ValueError("conflicting adjustment factor identity scope")
        unique[key] = item
    ordered = tuple(
        sorted(
            unique.values(),
            key=lambda item: (
                item.symbol,
                item.frequency.value,
                item.scope_start,
                item.scope_end,
                item.scope_observation_keys_sha256,
            ),
        )
    )
    return ordered


@dataclass(frozen=True)
class AdjustmentFactorRunIdentity:
    """Compact run-level commitment suitable for ``ResultAssumption``.

    The full per-symbol identity stream stays in data-quality reports or a
    separate artifact.  The result envelope carries only this fixed-size
    summary and its cryptographic commitment, so a large minute portfolio
    cannot inflate metadata in proportion to symbol-day count.
    """

    identity_count: int
    symbol_count: int
    frequencies: Tuple[str, ...]
    scope_start: str
    scope_end: str
    total_scope_trade_days: int
    total_validated_close_rows: int
    source_count: int
    sources_sha256: str
    identity_stream_sha256: str
    relation: str = ADJUSTMENT_FACTOR_RELATION
    identity_version: str = ADJUSTMENT_FACTOR_IDENTITY_VERSION
    schema: str = "diepi.adjustment_factor_run_identity"
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in (
            "identity_count",
            "symbol_count",
            "total_scope_trade_days",
            "total_validated_close_rows",
            "source_count",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError("%s must be a positive exact int" % name)
        if self.symbol_count > self.identity_count:
            raise ValueError("symbol_count cannot exceed identity_count")
        if self.source_count > self.identity_count:
            raise ValueError("source_count cannot exceed identity_count")
        if self.total_validated_close_rows < self.total_scope_trade_days:
            raise ValueError("validated rows must cover total scope trade days")
        if type(self.frequencies) is not tuple or not self.frequencies:
            raise TypeError("frequencies must be a non-empty tuple")
        if tuple(sorted(set(self.frequencies))) != self.frequencies:
            raise ValueError("frequencies must be unique and sorted")
        if any(value not in {item.value for item in Frequency} for value in self.frequencies):
            raise ValueError("run identity contains unsupported frequency")
        start = _canonical_daily_date(self.scope_start, "scope_start")
        end = _canonical_daily_date(self.scope_end, "scope_end")
        if start != self.scope_start or end != self.scope_end or start > end:
            raise ValueError("run identity scope dates must be canonical and ordered")
        for name in ("sources_sha256", "identity_stream_sha256"):
            value = getattr(self, name)
            if type(value) is not str or not _SHA256_RE.fullmatch(value):
                raise ValueError("%s must be lowercase SHA-256" % name)
        if self.relation != ADJUSTMENT_FACTOR_RELATION:
            raise ValueError("unsupported run identity relation")
        if self.identity_version != ADJUSTMENT_FACTOR_IDENTITY_VERSION:
            raise ValueError("unsupported run identity version")
        if self.schema != "diepi.adjustment_factor_run_identity":
            raise ValueError("unsupported run identity schema")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported run identity schema_version")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frequencies": list(self.frequencies),
            "identity_count": self.identity_count,
            "identity_stream_sha256": self.identity_stream_sha256,
            "identity_version": self.identity_version,
            "relation": self.relation,
            "schema": self.schema,
            "schema_version": self.schema_version,
            "scope_end": self.scope_end,
            "scope_start": self.scope_start,
            "source_count": self.source_count,
            "sources_sha256": self.sources_sha256,
            "symbol_count": self.symbol_count,
            "total_scope_trade_days": self.total_scope_trade_days,
            "total_validated_close_rows": self.total_validated_close_rows,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> "AdjustmentFactorRunIdentity":
        if type(value) is not dict:
            raise TypeError("run adjustment identity must be exactly dict")
        expected = {
            "frequencies",
            "identity_count",
            "identity_stream_sha256",
            "identity_version",
            "relation",
            "schema",
            "schema_version",
            "scope_end",
            "scope_start",
            "source_count",
            "sources_sha256",
            "symbol_count",
            "total_scope_trade_days",
            "total_validated_close_rows",
        }
        if set(value) != expected:
            raise ValueError("run adjustment identity keys mismatch")
        if type(value["frequencies"]) is not list:
            raise TypeError("run identity frequencies must be exactly list")
        restored = cls(
            identity_count=value["identity_count"],
            symbol_count=value["symbol_count"],
            frequencies=tuple(value["frequencies"]),
            scope_start=value["scope_start"],
            scope_end=value["scope_end"],
            total_scope_trade_days=value["total_scope_trade_days"],
            total_validated_close_rows=value["total_validated_close_rows"],
            source_count=value["source_count"],
            sources_sha256=value["sources_sha256"],
            identity_stream_sha256=value["identity_stream_sha256"],
            relation=value["relation"],
            identity_version=value["identity_version"],
            schema=value["schema"],
            schema_version=value["schema_version"],
        )
        if restored.to_dict() != value:
            raise ValueError("run adjustment identity is not canonical")
        return restored

    @classmethod
    def from_json(cls, value: Any) -> "AdjustmentFactorRunIdentity":
        if type(value) is not str:
            raise TypeError("run adjustment identity JSON must be exactly str")
        payload = json.loads(
            value,
            object_pairs_hook=_strict_json_object_pairs,
            parse_constant=_reject_json_constant,
        )
        restored = cls.from_dict(payload)
        if restored.to_json() != value:
            raise ValueError("run adjustment identity JSON must be canonical")
        return restored


def adjustment_factor_identity_artifact(
    identities: Iterable[AdjustmentFactorIdentity],
) -> str:
    """Canonical full identity stream for an optional standalone artifact."""

    ordered = _ordered_adjustment_identities(identities)
    return _canonical_json(
        {
            "identities": [item.to_dict() for item in ordered],
            "schema": "diepi.adjustment_factor_identity_artifact",
            "schema_version": 1,
        }
    )


def build_adjustment_factor_run_identity(
    identities: Iterable[AdjustmentFactorIdentity],
) -> AdjustmentFactorRunIdentity:
    """Build a fixed-size commitment over canonical per-symbol identities."""

    ordered = _ordered_adjustment_identities(identities)
    stream = adjustment_factor_identity_artifact(ordered)
    sources = tuple(sorted({item.source for item in ordered}))
    return AdjustmentFactorRunIdentity(
        identity_count=len(ordered),
        symbol_count=len({item.symbol for item in ordered}),
        frequencies=tuple(sorted({item.frequency.value for item in ordered})),
        scope_start=min(item.scope_start for item in ordered),
        scope_end=max(item.scope_end for item in ordered),
        total_scope_trade_days=sum(item.scope_trade_days for item in ordered),
        total_validated_close_rows=sum(
            item.validated_close_rows for item in ordered
        ),
        source_count=len(sources),
        sources_sha256=hashlib.sha256(
            _canonical_json(list(sources)).encode("utf-8")
        ).hexdigest(),
        identity_stream_sha256=hashlib.sha256(
            stream.encode("utf-8")
        ).hexdigest(),
    )


def adjustment_factor_assumption_value(
    identities: Iterable[AdjustmentFactorIdentity],
) -> str:
    """Return the compact run identity for one ResultAssumption value."""

    return build_adjustment_factor_run_identity(identities).to_json()


@dataclass(frozen=True)
class DataQualityReport:
    """Complete result of validating one strategy/execution pair."""

    symbol: str
    frequency: Frequency
    strategy_price_space: PriceSpace
    execution_price_space: PriceSpace
    strategy_amount_unit: AmountUnit
    execution_amount_unit: AmountUnit
    strategy_rows: int
    execution_rows: int
    aligned_rows: int
    issues: Tuple[DataQualityIssue, ...]
    pre_close_exempt_dates: Tuple[str, ...] = ()
    strategy_pre_close_source: PreCloseSource = PreCloseSource.ABSENT
    execution_pre_close_source: PreCloseSource = PreCloseSource.ABSENT
    adjustment_factor_identity: Optional[AdjustmentFactorIdentity] = None
    schema_version: int = SCHEMA_VERSION
    contract_version: str = CONTRACT_VERSION
    strict: bool = True

    def __post_init__(self) -> None:
        ordered = tuple(sorted(tuple(self.issues), key=_issue_sort_key))
        object.__setattr__(self, "issues", ordered)
        canonical_exempt_dates = _normalize_pre_close_exempt_dates(
            self.pre_close_exempt_dates
        )
        object.__setattr__(
            self, "pre_close_exempt_dates", canonical_exempt_dates
        )
        object.__setattr__(
            self,
            "strategy_pre_close_source",
            _coerce_enum(
                self.strategy_pre_close_source,
                PreCloseSource,
                "strategy_pre_close_source",
            ),
        )
        object.__setattr__(
            self,
            "execution_pre_close_source",
            _coerce_enum(
                self.execution_pre_close_source,
                PreCloseSource,
                "execution_pre_close_source",
            ),
        )
        if self.frequency is not Frequency.MINUTE and (
            self.strategy_pre_close_source in (
                PreCloseSource.SAME_LANE_DAILY,
                PreCloseSource.SAME_LANE_DAILY_WITH_SUSPENSION_FALLBACK,
            )
            or self.execution_pre_close_source in (
                PreCloseSource.SAME_LANE_DAILY,
                PreCloseSource.SAME_LANE_DAILY_WITH_SUSPENSION_FALLBACK,
            )
        ):
            raise ValueError(
                "same_lane_daily pre_close provenance requires minute frequency"
            )
        if (
            self.adjustment_factor_identity is not None
            and not isinstance(
                self.adjustment_factor_identity, AdjustmentFactorIdentity
            )
        ):
            raise TypeError(
                "adjustment_factor_identity must be AdjustmentFactorIdentity or None"
            )
        if self.adjustment_factor_identity is not None:
            identity = self.adjustment_factor_identity
            if (
                identity.symbol != self.symbol
                or identity.frequency is not self.frequency
                or identity.strategy_price_space is not self.strategy_price_space
                or identity.execution_price_space is not self.execution_price_space
            ):
                raise ValueError(
                    "adjustment factor identity dimensions must match report"
                )

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def status(self) -> str:
        return "pass" if self.ok else "fail"

    @property
    def issue_codes(self) -> Tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def to_dict(self) -> Dict[str, Any]:
        """Return a stable, JSON-friendly representation with no clock data."""

        return {
            "schema_version": self.schema_version,
            "contract_version": self.contract_version,
            "strict": self.strict,
            "status": self.status,
            "symbol": self.symbol,
            "frequency": self.frequency.value,
            "strategy_price_space": self.strategy_price_space.value,
            "execution_price_space": self.execution_price_space.value,
            "strategy_amount_unit": self.strategy_amount_unit.value,
            "execution_amount_unit": self.execution_amount_unit.value,
            "pre_close_exempt_dates": list(self.pre_close_exempt_dates),
            "strategy_pre_close_source": self.strategy_pre_close_source.value,
            "execution_pre_close_source": self.execution_pre_close_source.value,
            "adjustment_factor_identity": (
                self.adjustment_factor_identity.to_dict()
                if self.adjustment_factor_identity is not None
                else None
            ),
            "strategy_rows": self.strategy_rows,
            "execution_rows": self.execution_rows,
            "aligned_rows": self.aligned_rows,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class AlignedMarketData:
    """Validated copies with one canonical symbol and timestamp key space."""

    symbol: str
    frequency: Frequency
    strategy_price_space: PriceSpace
    execution_price_space: PriceSpace
    strategy_amount_unit: AmountUnit
    execution_amount_unit: AmountUnit
    strategy: pd.DataFrame
    execution: pd.DataFrame
    report: DataQualityReport

    @property
    def strategy_data(self) -> pd.DataFrame:
        """Explicit alias for callers that prefer the ``*_data`` spelling."""

        return self.strategy

    @property
    def execution_data(self) -> pd.DataFrame:
        """Explicit alias for callers that prefer the ``*_data`` spelling."""

        return self.execution

    @property
    def pre_close_exempt_dates(self) -> Tuple[str, ...]:
        """Canonical dates whose source ``pre_close`` was explicitly exempt."""

        return self.report.pre_close_exempt_dates

    @property
    def strategy_pre_close_source(self) -> PreCloseSource:
        return self.report.strategy_pre_close_source

    @property
    def execution_pre_close_source(self) -> PreCloseSource:
        return self.report.execution_pre_close_source

    @property
    def adjustment_factor_identity(
        self,
    ) -> Optional[AdjustmentFactorIdentity]:
        return self.report.adjustment_factor_identity

    @property
    def keys(self) -> pd.MultiIndex:
        """Canonical composite keys without changing the DatetimeIndex frames."""

        return pd.MultiIndex.from_arrays(
            [[self.symbol] * len(self.strategy), self.strategy.index],
            names=["symbol", "timestamp"],
        )


class DataContractError(ValueError):
    """Raised with the complete report when DC-1 validation fails."""

    def __init__(self, report: DataQualityReport):
        if not isinstance(report, DataQualityReport):
            raise TypeError("report must be a DataQualityReport")
        self.report = report
        summary = ",".join(
            "%s:%d" % (issue.code, issue.count) for issue in report.issues
        )
        super().__init__(
            "%s validation failed for %s (%s); issues=%s"
            % (CONTRACT_VERSION, report.symbol, report.frequency.value, summary)
        )


class AdjustmentFactorError(ValueError):
    """Strict direct-ratio failure outside a paired data report."""

    def __init__(
        self, symbol: str, issues: Iterable[DataQualityIssue]
    ) -> None:
        self.symbol = _normalize_symbol(symbol)
        self.issues = tuple(sorted(tuple(issues), key=_issue_sort_key))
        if not self.issues:
            raise ValueError("AdjustmentFactorError requires at least one issue")
        summary = ",".join(
            "%s:%d" % (issue.code, issue.count) for issue in self.issues
        )
        super().__init__(
            "%s validation failed for %s; issues=%s"
            % (ADJUSTMENT_FACTOR_IDENTITY_VERSION, self.symbol, summary)
        )


_EnumT = TypeVar("_EnumT", bound=Enum)


def _coerce_enum(value: Any, enum_type: Type[_EnumT], argument: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    try:
        normalized = str(value).strip().lower()
        return enum_type(normalized)
    except (TypeError, ValueError):
        allowed = ", ".join(member.value for member in enum_type)
        raise ValueError("%s must be one of: %s" % (argument, allowed))


def _normalize_symbol(symbol: Any) -> str:
    if not isinstance(symbol, str):
        raise ValueError("symbol must be a non-empty string")
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("symbol must be a non-empty string")
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,127}", normalized):
        raise ValueError("symbol contains unsupported path or identifier syntax")
    return normalized


def _stable_value(value: Any) -> str:
    if value is None or value is pd.NA:
        return "<NA>"
    try:
        if bool(pd.isna(value)):
            return "<NA>"
    except (TypeError, ValueError):
        pass
    return str(value)


def _sorted_sample(values: Iterable[str]) -> Tuple[str, ...]:
    # Keep only the lexicographically smallest unique values.  Memory remains
    # bounded even when a corrupt file contains millions of bad rows.
    sample: List[str] = []
    for value in values:
        if value in sample:
            continue
        if len(sample) < MAX_ISSUE_SAMPLES:
            sample.append(value)
            sample.sort()
        elif value < sample[-1]:
            sample[-1] = value
            sample.sort()
    return tuple(sample)


def _format_timestamp(timestamp: pd.Timestamp, frequency: Frequency) -> str:
    if frequency is Frequency.DAILY:
        return timestamp.strftime("%Y-%m-%d")
    return timestamp.isoformat()


def _row_keys(
    symbol: str,
    index: Optional[pd.DatetimeIndex],
    positions: Iterable[int],
    frequency: Frequency,
) -> Tuple[str, ...]:
    def generate() -> Iterable[str]:
        for position in positions:
            if (
                index is not None
                and position < len(index)
                and not pd.isna(index[position])
            ):
                value = _format_timestamp(pd.Timestamp(index[position]), frequency)
                yield "%s@%s" % (symbol, value)
            else:
                yield "%s@row:%d" % (symbol, position)

    return _sorted_sample(generate())


def _mask_positions(mask: np.ndarray) -> Iterable[int]:
    return (
        position
        for position, is_bad in enumerate(np.asarray(mask, dtype=bool))
        if is_bad
    )


def _make_issue(
    code: str,
    track: Optional[TrackRole],
    count: int,
    message: str,
    field: Optional[str] = None,
    sample_keys: Tuple[str, ...] = (),
) -> DataQualityIssue:
    return DataQualityIssue(
        code=code,
        track=track,
        field=field,
        count=int(count),
        sample_keys=sample_keys,
        message=message,
    )


def _prepare_track_frame(
    frame: pd.DataFrame,
    track: TrackRole,
    symbol: str,
    issues: List[DataQualityIssue],
) -> pd.DataFrame:
    """Copy a frame and quarantine ambiguous duplicate columns.

    Pandas returns a DataFrame rather than a Series for ``frame[field]`` when
    column labels are duplicated.  Letting validation continue on that object
    produces order-dependent exceptions in timestamp and numeric checks.  The
    pair is already invalid, so remove *all* ambiguous occurrences only from
    the private validation copy and keep collecting deterministic issues for
    the remaining unambiguous fields.  Input data is never mutated or repaired.
    """

    prepared = frame.copy(deep=True)
    duplicated = np.asarray(prepared.columns.duplicated(keep=False), dtype=bool)
    if not duplicated.any():
        return prepared

    duplicate_labels = [
        _stable_value(label)
        for label, is_duplicate in zip(prepared.columns, duplicated)
        if is_duplicate
    ]
    issues.append(
        _make_issue(
            ISSUE_DUPLICATE_COLUMN,
            track,
            int(duplicated.sum()),
            "column labels must be unique; ambiguous columns are not inspected",
            field="columns",
            sample_keys=_sorted_sample(
                "%s@column:%s" % (symbol, label)
                for label in duplicate_labels
            ),
        )
    )
    return prepared.loc[:, ~duplicated].copy(deep=True)


def _timestamp_sources(
    frame: pd.DataFrame, frequency: Frequency
) -> List[Tuple[str, Sequence[Any], Frequency, bool, bool]]:
    """Describe timestamp-bearing fields without conflating their granularity.

    The provider's minute frames commonly contain both a minute-level index and
    a date-only ``trade_date`` column.  The latter is an auxiliary consistency
    check, not an alternative minute timestamp.  Treating it as a full minute
    source would reject every otherwise valid row (``20240102`` is a date, not
    ``2024-01-02 09:30``).

    Tuple fields are ``(name, values, parse_frequency, date_only_compare,
    canonical_candidate)``.
    """
    sources: List[Tuple[str, Sequence[Any], Frequency, bool, bool]] = []
    timestamp_names = ("trade_date", "trade_time", "timestamp")
    expected = "trade_date" if frequency is Frequency.DAILY else "trade_time"
    other = "trade_time" if frequency is Frequency.DAILY else "trade_date"

    index_is_canonical = (
        isinstance(frame.index, pd.DatetimeIndex)
        or frame.index.name in (expected, "timestamp")
        or (frequency is Frequency.DAILY and frame.index.name == other)
    )
    if index_is_canonical:
        sources.append(("index", frame.index, frequency, False, True))

    for field in (expected, "timestamp"):
        if field in frame.columns:
            sources.append((field, frame[field], frequency, False, True))

    if other in frame.columns:
        if frequency is Frequency.MINUTE:
            sources.append(
                (other, frame[other], Frequency.DAILY, True, False)
            )
        else:
            sources.append((other, frame[other], frequency, False, True))
    return sources


_DAILY_TEXT_FORMATS = (
    (8, "%Y%m%d"),
    (10, "%Y-%m-%d"),
    (16, "%Y-%m-%d %H:%M"),
    (19, "%Y-%m-%d %H:%M:%S"),
    (16, "%Y-%m-%dT%H:%M"),
    (19, "%Y-%m-%dT%H:%M:%S"),
)
_MINUTE_TEXT_FORMATS = (
    (16, "%Y-%m-%d %H:%M"),
    (19, "%Y-%m-%d %H:%M:%S"),
    (16, "%Y-%m-%dT%H:%M"),
    (19, "%Y-%m-%dT%H:%M:%S"),
)


def _parse_text_timestamp(value: str, frequency: Frequency) -> pd.Timestamp:
    text_value = value.strip()
    formats = (
        _DAILY_TEXT_FORMATS
        if frequency is Frequency.DAILY
        else _MINUTE_TEXT_FORMATS
    )
    for expected_length, timestamp_format in formats:
        if len(text_value) != expected_length:
            continue
        try:
            return pd.Timestamp(datetime.strptime(text_value, timestamp_format))
        except ValueError:
            continue
    raise ValueError("timestamp text has no accepted format")


def _parse_one_timestamp(value: Any, frequency: Frequency) -> pd.Timestamp:
    if value is None or value is pd.NA:
        raise ValueError("timestamp is missing")
    try:
        if bool(pd.isna(value)):
            raise ValueError("timestamp is missing")
    except (TypeError, ValueError):
        raise ValueError("timestamp is not scalar")

    if isinstance(value, str):
        timestamp = _parse_text_timestamp(value, frequency)
    elif isinstance(value, (pd.Timestamp, datetime, date, np.datetime64)):
        timestamp = pd.Timestamp(value)
    elif (
        frequency is Frequency.DAILY
        and isinstance(value, Real)
        and not isinstance(value, (bool, np.bool_))
    ):
        numeric_text = str(value)
        if numeric_text.endswith(".0"):
            numeric_text = numeric_text[:-2]
        timestamp = _parse_text_timestamp(numeric_text, frequency)
    else:
        raise ValueError("timestamp has an unsupported type")

    if timestamp.tzinfo is not None:
        raise ValueError("timestamp must be timezone-naive")
    if frequency is Frequency.DAILY:
        if timestamp != timestamp.normalize():
            raise ValueError("daily timestamp must be at 00:00")
    elif timestamp.second or timestamp.microsecond or timestamp.nanosecond:
        raise ValueError("minute timestamp must have zero seconds")
    return timestamp


def _normalize_pre_close_exempt_dates(values: Any) -> Tuple[str, ...]:
    """Return strict, canonical, deterministic pre-close exemption dates.

    A date collection is required even for one date.  In particular, a bare
    string is rejected instead of being treated as an iterable of characters.
    Individual values follow the contract's daily timestamp grammar and are
    serialized as unique, sorted ``YYYYMMDD`` strings.
    """

    if isinstance(values, (str, bytes)) or not isinstance(
            values, (list, tuple, set, frozenset)):
        raise TypeError(
            "pre_close_exempt_dates must be a list, tuple, set, or frozenset"
        )
    canonical: List[str] = []
    for value in values:
        try:
            timestamp = _parse_one_timestamp(value, Frequency.DAILY)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError(
                "pre_close_exempt_dates entries must be valid daily dates"
            ) from error
        canonical.append(timestamp.strftime("%Y%m%d"))
    return tuple(sorted(set(canonical)))


def _resolve_pre_close_source(
    value: Any,
    frame: pd.DataFrame,
    frequency: Frequency,
    argument: str,
) -> PreCloseSource:
    has_column = "pre_close" in frame.columns
    if value is None:
        return PreCloseSource.NATIVE if has_column else PreCloseSource.ABSENT
    source = _coerce_enum(value, PreCloseSource, argument)
    if source is PreCloseSource.ABSENT and has_column:
        raise ValueError("%s cannot be absent when pre_close exists" % argument)
    if source is not PreCloseSource.ABSENT and not has_column:
        raise ValueError("%s requires a pre_close column" % argument)
    if (
        source in (
            PreCloseSource.SAME_LANE_DAILY,
            PreCloseSource.SAME_LANE_DAILY_WITH_SUSPENSION_FALLBACK,
        )
        and frequency is not Frequency.MINUTE
    ):
        raise ValueError("%s is only valid for minute data" % argument)
    return source


def _parse_timestamps(
    values: Sequence[Any], frequency: Frequency
) -> Tuple[pd.DatetimeIndex, np.ndarray]:
    parsed: List[Any] = []
    invalid = np.zeros(len(values), dtype=bool)
    for position, value in enumerate(values):
        try:
            parsed.append(_parse_one_timestamp(value, frequency))
        except (TypeError, ValueError, OverflowError):
            parsed.append(pd.NaT)
            invalid[position] = True
    try:
        index = pd.DatetimeIndex(parsed)
    except (TypeError, ValueError, OverflowError):
        index = pd.DatetimeIndex([pd.NaT] * len(parsed))
        invalid[:] = True
    return index, invalid


def _normalize_track_index(
    frame: pd.DataFrame,
    track: TrackRole,
    symbol: str,
    frequency: Frequency,
    issues: List[DataQualityIssue],
) -> Tuple[pd.DataFrame, Optional[pd.DatetimeIndex]]:
    normalized = frame.copy(deep=True)
    sources = _timestamp_sources(normalized, frequency)
    canonical_sources = [source for source in sources if source[4]]
    if not canonical_sources:
        issues.append(
            _make_issue(
                ISSUE_MISSING_TIMESTAMP,
                track,
                1,
                "no canonical timestamp source is present",
                field="timestamp",
            )
        )
        return normalized, None

    parsed_sources: List[
        Tuple[str, pd.DatetimeIndex, np.ndarray, bool, bool]
    ] = []
    for (source_name, source_values, parse_frequency,
         date_only_compare, canonical_candidate) in sources:
        index, invalid = _parse_timestamps(source_values, parse_frequency)
        parsed_sources.append(
            (source_name, index, invalid,
             date_only_compare, canonical_candidate)
        )
        if invalid.any():
            positions = _mask_positions(invalid)
            def source_value_at(position: int) -> Any:
                if isinstance(source_values, pd.Series):
                    return source_values.iloc[position]
                return source_values[position]

            samples = _sorted_sample(
                "%s@invalid:%s"
                % (symbol, _stable_value(source_value_at(position)))
                for position in positions
            )
            issues.append(
                _make_issue(
                    ISSUE_INVALID_TIMESTAMP,
                    track,
                    int(invalid.sum()),
                    "timestamp values violate parsing, timezone, or granularity rules",
                    field=source_name,
                    sample_keys=samples,
                )
            )

    primary = next(source for source in parsed_sources if source[4])
    primary_name, primary_index, primary_invalid, _, _ = primary
    for (source_name, source_index, source_invalid,
         date_only_compare, _) in parsed_sources:
        if source_name == primary_name and source_index is primary_index:
            continue
        comparable = ~(primary_invalid | source_invalid)
        comparison_index = (
            primary_index.normalize() if date_only_compare else primary_index
        )
        mismatch = comparable & np.asarray(
            comparison_index != source_index, dtype=bool
        )
        if mismatch.any():
            positions = _mask_positions(mismatch)
            issues.append(
                _make_issue(
                    ISSUE_KEY_MISMATCH,
                    track,
                    int(mismatch.sum()),
                    "timestamp sources disagree row by row with %s" % primary_name,
                    field=source_name,
                    sample_keys=_row_keys(
                        symbol, primary_index, positions, frequency
                    ),
                )
            )

    normalized.index = primary_index
    normalized.index.name = "timestamp"
    return normalized, primary_index


def _validate_symbol_columns(
    frame: pd.DataFrame,
    index: Optional[pd.DatetimeIndex],
    track: TrackRole,
    symbol: str,
    frequency: Frequency,
    issues: List[DataQualityIssue],
) -> None:
    for field in ("symbol", "ts_code"):
        if field not in frame.columns:
            continue
        values = frame[field]
        is_string = values.map(lambda value: isinstance(value, str))
        normalized = values.map(
            lambda value: _stable_value(value).strip().upper()
        )
        bad = (
            ~is_string.to_numpy(dtype=bool)
            | (normalized.to_numpy(dtype=object) != symbol)
        )
        if bad.any():
            positions = _mask_positions(bad)
            issues.append(
                _make_issue(
                    ISSUE_SYMBOL_MISMATCH,
                    track,
                    int(bad.sum()),
                    "row symbol does not match the requested symbol",
                    field=field,
                    sample_keys=_row_keys(symbol, index, positions, frequency),
                )
            )
        else:
            frame[field] = symbol


def _validate_index(
    index: Optional[pd.DatetimeIndex],
    track: TrackRole,
    symbol: str,
    frequency: Frequency,
    issues: List[DataQualityIssue],
) -> None:
    if index is None:
        return

    valid_positions = np.flatnonzero(~np.asarray(index.isna(), dtype=bool))
    valid_index = index.take(valid_positions)
    valid_duplicated = np.asarray(valid_index.duplicated(keep=False), dtype=bool)
    duplicated = np.zeros(len(index), dtype=bool)
    duplicated[valid_positions[valid_duplicated]] = True
    if duplicated.any():
        positions = _mask_positions(duplicated)
        issues.append(
            _make_issue(
                ISSUE_DUPLICATE_KEY,
                track,
                int(duplicated.sum()),
                "timestamp keys must be unique",
                field="timestamp",
                sample_keys=_row_keys(symbol, index, positions, frequency),
            )
        )

    if not valid_index.is_monotonic_increasing:
        descending_positions = [
            int(valid_positions[position])
            for position in range(1, len(valid_index))
            if valid_index[position] < valid_index[position - 1]
        ]
        issues.append(
            _make_issue(
                ISSUE_NON_MONOTONIC_KEY,
                track,
                len(descending_positions),
                "timestamp keys must be monotonically increasing",
                field="timestamp",
                sample_keys=_row_keys(
                    symbol, index, descending_positions, frequency
                ),
            )
        )


def _numeric_column(
    frame: pd.DataFrame,
    field: str,
) -> np.ndarray:
    raw = frame[field]
    if (
        pd.api.types.is_numeric_dtype(raw.dtype)
        and not pd.api.types.is_bool_dtype(raw.dtype)
        and not pd.api.types.is_complex_dtype(raw.dtype)
    ):
        values = raw.to_numpy(dtype="float64", na_value=np.nan)
    else:
        values = np.full(len(raw), np.nan, dtype="float64")
        for position, value in enumerate(raw.to_numpy(dtype=object)):
            if isinstance(value, (bool, np.bool_)):
                continue
            if isinstance(value, Real) and not isinstance(value, complex):
                try:
                    values[position] = float(value)
                except (TypeError, ValueError, OverflowError):
                    pass
    frame[field] = values
    return values


def _append_mask_issue(
    issues: List[DataQualityIssue],
    code: str,
    track: TrackRole,
    field: str,
    message: str,
    mask: np.ndarray,
    symbol: str,
    index: Optional[pd.DatetimeIndex],
    frequency: Frequency,
) -> None:
    if not mask.any():
        return
    positions = _mask_positions(mask)
    issues.append(
        _make_issue(
            code,
            track,
            int(mask.sum()),
            message,
            field=field,
            sample_keys=_row_keys(symbol, index, positions, frequency),
        )
    )


def _pre_close_exempt_mask(
    index: Optional[pd.DatetimeIndex],
    pre_close_exempt_dates: Tuple[str, ...],
    row_count: int,
) -> np.ndarray:
    mask = np.zeros(row_count, dtype=bool)
    if index is None or not pre_close_exempt_dates:
        return mask
    exempt = set(pre_close_exempt_dates)
    for position, timestamp in enumerate(index):
        if position >= row_count or pd.isna(timestamp):
            continue
        mask[position] = pd.Timestamp(timestamp).strftime("%Y%m%d") in exempt
    return mask


def _validate_optional_market_columns(
    frame: pd.DataFrame,
    index: Optional[pd.DatetimeIndex],
    track: TrackRole,
    symbol: str,
    frequency: Frequency,
    amount_unit: AmountUnit,
    pre_close_exempt_dates: Tuple[str, ...],
    issues: List[DataQualityIssue],
) -> None:
    if "pre_close" in frame.columns:
        pre_close = _numeric_column(frame, "pre_close")
        source_invalid = ~np.isfinite(pre_close) | (pre_close <= 0.0)
        exempt = _pre_close_exempt_mask(
            index, pre_close_exempt_dates, len(pre_close)
        )
        invalid = source_invalid & ~exempt
        _append_mask_issue(
            issues,
            ISSUE_INVALID_PRE_CLOSE,
            track,
            "pre_close",
            "pre_close values must be real numeric, finite, and greater than zero",
            invalid,
            symbol,
            index,
            frequency,
        )
        if frequency is Frequency.MINUTE and index is not None:
            inconsistent = np.zeros(len(pre_close), dtype=bool)
            # Exempt rows do not establish or violate the daily constant,
            # even when their pre_close happens to be positive and finite.
            valid = (
                ~source_invalid
                & ~exempt
                & ~np.asarray(index.isna(), dtype=bool)
            )
            normalized_dates = index.normalize()
            for trading_date in normalized_dates[valid].unique():
                positions = np.flatnonzero(
                    valid & np.asarray(normalized_dates == trading_date, dtype=bool)
                )
                if len(positions) < 2:
                    continue
                reference = pre_close[positions[0]]
                inconsistent[positions[pre_close[positions] != reference]] = True
            _append_mask_issue(
                issues,
                ISSUE_INVALID_PRE_CLOSE,
                track,
                "pre_close",
                "minute pre_close must be constant within each trading day",
                inconsistent,
                symbol,
                index,
                frequency,
            )

    if "amount" in frame.columns:
        amount = _numeric_column(frame, "amount")
        multiplier = (
            1000.0 if amount_unit is AmountUnit.THOUSAND_YUAN else 1.0
        )
        with np.errstate(over="ignore", invalid="ignore"):
            normalized_amount = amount * multiplier
        invalid = (
            ~np.isfinite(amount)
            | (amount < 0.0)
            | ~np.isfinite(normalized_amount)
        )
        _append_mask_issue(
            issues,
            ISSUE_INVALID_AMOUNT,
            track,
            "amount",
            "amount must be real numeric, finite, non-negative, and finite after frequency normalization",
            invalid,
            symbol,
            index,
            frequency,
        )


def _validate_required_columns(
    frame: pd.DataFrame,
    index: Optional[pd.DatetimeIndex],
    track: TrackRole,
    symbol: str,
    frequency: Frequency,
    amount_unit: AmountUnit,
    pre_close_exempt_dates: Tuple[str, ...],
    issues: List[DataQualityIssue],
) -> None:
    required = list(_OHLC_COLUMNS)
    if track is TrackRole.EXECUTION:
        required.extend(_EXECUTION_ONLY_COLUMNS)
    elif frequency is Frequency.MINUTE:
        # The provider adapter must expose explicit same-lane provenance when
        # it enriches legacy minute files.  A minute strategy lane without a
        # reference close is therefore not silently treated as optional.
        required.append("pre_close")

    for field in required:
        if field not in frame.columns:
            issues.append(
                _make_issue(
                    ISSUE_MISSING_COLUMN,
                    track,
                    1,
                    "required column is missing",
                    field=field,
                )
            )

    if all(field in frame.columns for field in _OHLC_COLUMNS):
        ohlc = {field: _numeric_column(frame, field) for field in _OHLC_COLUMNS}
        matrix = np.column_stack([ohlc[field] for field in _OHLC_COLUMNS])
        finite_rows = np.isfinite(matrix).all(axis=1)
        _append_mask_issue(
            issues,
            ISSUE_NONFINITE_VALUE,
            track,
            "ohlc",
            "OHLC values must all be real numeric and finite",
            ~finite_rows,
            symbol,
            index,
            frequency,
        )

        positive_rows = (matrix > 0.0).all(axis=1)
        nonpositive = finite_rows & ~positive_rows
        _append_mask_issue(
            issues,
            ISSUE_NONPOSITIVE_PRICE,
            track,
            "ohlc",
            "OHLC values must all be greater than zero",
            nonpositive,
            symbol,
            index,
            frequency,
        )

        comparable = finite_rows & positive_rows
        relation_bad = comparable & (
            (ohlc["high"] < ohlc["open"])
            | (ohlc["high"] < ohlc["low"])
            | (ohlc["high"] < ohlc["close"])
            | (ohlc["low"] > ohlc["open"])
            | (ohlc["low"] > ohlc["high"])
            | (ohlc["low"] > ohlc["close"])
        )
        _append_mask_issue(
            issues,
            ISSUE_OHLC_INCONSISTENT,
            track,
            "ohlc",
            "OHLC values violate high/low envelope relationships",
            relation_bad,
            symbol,
            index,
            frequency,
        )

    # Optional market columns are validated on both tracks.  They are required
    # only on execution, as recorded by the missing-column checks above.
    _validate_optional_market_columns(
        frame,
        index,
        track,
        symbol,
        frequency,
        amount_unit,
        pre_close_exempt_dates,
        issues,
    )


def _validate_key_sets(
    strategy_index: Optional[pd.DatetimeIndex],
    execution_index: Optional[pd.DatetimeIndex],
    symbol: str,
    frequency: Frequency,
    issues: List[DataQualityIssue],
) -> int:
    if strategy_index is None or execution_index is None:
        return 0
    if bool(strategy_index.isna().any()) or bool(execution_index.isna().any()):
        return 0

    strategy_keys = set(strategy_index.tolist())
    execution_keys = set(execution_index.tolist())
    missing_from_strategy = execution_keys - strategy_keys
    missing_from_execution = strategy_keys - execution_keys

    if missing_from_strategy:
        samples = _sorted_sample(
            "%s@%s" % (symbol, _format_timestamp(pd.Timestamp(key), frequency))
            for key in missing_from_strategy
        )
        issues.append(
            _make_issue(
                ISSUE_KEY_MISMATCH,
                TrackRole.STRATEGY,
                len(missing_from_strategy),
                "strategy track is missing keys present in execution track",
                field="timestamp",
                sample_keys=samples,
            )
        )

    if missing_from_execution:
        samples = _sorted_sample(
            "%s@%s" % (symbol, _format_timestamp(pd.Timestamp(key), frequency))
            for key in missing_from_execution
        )
        issues.append(
            _make_issue(
                ISSUE_KEY_MISMATCH,
                TrackRole.EXECUTION,
                len(missing_from_execution),
                "execution track is missing keys present in strategy track",
                field="timestamp",
                sample_keys=samples,
            )
        )

    return len(strategy_keys & execution_keys)


def _issue_sort_key(issue: DataQualityIssue) -> Tuple[Any, ...]:
    return (
        _ISSUE_ORDER[issue.code],
        _TRACK_ORDER[issue.track],
        issue.field or "",
        issue.sample_keys,
        issue.count,
        issue.message,
        issue.severity,
    )


@dataclass(frozen=True)
class _ValidatedAdjustmentFactorScope:
    symbol: str
    source: str
    base_date: str
    base_factor: float
    trade_dates: Tuple[str, ...]
    factors: Tuple[float, ...]
    ratios: Tuple[float, ...]
    scope_trade_dates_sha256: str
    factor_values_sha256: str

    def ratio_for(self, trade_date: str) -> float:
        try:
            position = self.trade_dates.index(trade_date)
        except ValueError as error:
            raise KeyError(trade_date) from error
        return self.ratios[position]


def _canonical_factor_source(value: Any) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError("adjustment_factor_source must be non-empty text")
    if value != value.strip() or any(character in value for character in "\r\n\t"):
        raise ValueError("adjustment_factor_source must be canonical text")
    return value


def _factor_sample(symbol: str, trade_date: str) -> str:
    return "%s|%s" % (symbol, trade_date)


def _inspect_adjustment_factor_scope(
    adjustment_factors: pd.DataFrame,
    *,
    symbol: str,
    required_trade_dates: Tuple[str, ...],
    source: str,
) -> Tuple[Optional[_ValidatedAdjustmentFactorScope], Tuple[DataQualityIssue, ...]]:
    """Inspect exact factor rows without sorting, filling, or carrying forward."""

    issues: List[DataQualityIssue] = []
    required_set = frozenset(required_trade_dates)
    missing_samples = _sorted_sample(
        _factor_sample(symbol, trade_date) for trade_date in required_trade_dates
    )
    if not isinstance(adjustment_factors, pd.DataFrame):
        issues.append(
            _make_issue(
                ISSUE_MISSING_ADJ_FACTOR,
                None,
                max(1, len(required_trade_dates)),
                "adjustment factor dataset is unavailable",
                field="adj_factor",
                sample_keys=missing_samples,
            )
        )
        return None, tuple(issues)
    frame = adjustment_factors.copy(deep=True)
    if len(frame) == 0:
        issues.append(
            _make_issue(
                ISSUE_MISSING_ADJ_FACTOR,
                None,
                max(1, len(required_trade_dates)),
                "adjustment factor dataset must contain scoped rows",
                field="adj_factor",
                sample_keys=missing_samples,
            )
        )
        return None, tuple(issues)

    duplicate_columns = tuple(
        sorted(
            {
                str(column)
                for column, duplicate in zip(
                    frame.columns, frame.columns.duplicated(keep=False)
                )
                if duplicate
            }
        )
    )
    if duplicate_columns:
        issues.append(
            _make_issue(
                ISSUE_DUPLICATE_ADJ_FACTOR,
                None,
                len(duplicate_columns),
                "adjustment factor columns must be unique",
                field="columns",
                sample_keys=_sorted_sample(duplicate_columns),
            )
        )
    missing_columns = tuple(
        field for field in ("trade_date", "adj_factor") if field not in frame.columns
    )
    if missing_columns:
        for field in missing_columns:
            issues.append(
                _make_issue(
                    ISSUE_MISSING_ADJ_FACTOR,
                    None,
                    1,
                    "adjustment factor dataset is missing required column",
                    field=field,
                )
            )
    if duplicate_columns or missing_columns:
        return None, tuple(issues)

    # Freeze the observable factor input at the first point where every
    # required date has been seen.  Only source row zero (the HFQ anchor) and
    # exact required-date rows enter validation/hashing.  Rows appended after
    # the requested interval -- even malformed rows -- cannot change an
    # earlier run's outcome or identity.
    selected_positions = [0]
    seen_required = set()
    scope_end = required_trade_dates[-1]
    for position, raw_date in enumerate(frame["trade_date"].tolist()):
        try:
            parsed = _parse_one_timestamp(raw_date, Frequency.DAILY)
        except (TypeError, ValueError, OverflowError):
            continue
        canonical = parsed.strftime("%Y%m%d")
        if canonical > scope_end and seen_required != required_set:
            break
        if canonical in required_set:
            selected_positions.append(position)
            seen_required.add(canonical)
        if seen_required == required_set:
            break
    selected_positions = list(dict.fromkeys(selected_positions))
    frame = frame.iloc[selected_positions].copy(deep=True)

    parsed_dates, invalid_dates = _parse_timestamps(
        frame["trade_date"].tolist(), Frequency.DAILY
    )
    canonical_dates = tuple(
        timestamp.strftime("%Y%m%d") if not pd.isna(timestamp) else "invalid"
        for timestamp in parsed_dates
    )
    if invalid_dates.any():
        positions = tuple(_mask_positions(invalid_dates))
        issues.append(
            _make_issue(
                ISSUE_INVALID_ADJ_FACTOR,
                None,
                int(invalid_dates.sum()),
                "adjustment factor trade_date values are invalid",
                field="trade_date",
                sample_keys=_sorted_sample(
                    "%s|invalid:%s"
                    % (symbol, _stable_value(frame["trade_date"].iloc[position]))
                    for position in positions
                ),
            )
        )

    if not invalid_dates.any():
        duplicated = parsed_dates.duplicated(keep=False)
        if bool(duplicated.any()):
            duplicate_dates = tuple(
                canonical_dates[position]
                for position in _mask_positions(np.asarray(duplicated, dtype=bool))
            )
            issues.append(
                _make_issue(
                    ISSUE_DUPLICATE_ADJ_FACTOR,
                    None,
                    len(duplicate_dates),
                    "adjustment factor trade dates must be unique",
                    field="trade_date",
                    sample_keys=_sorted_sample(
                        _factor_sample(symbol, trade_date)
                        for trade_date in duplicate_dates
                    ),
                )
            )
        if not parsed_dates.is_monotonic_increasing:
            bad_positions = tuple(
                position
                for position in range(1, len(parsed_dates))
                if parsed_dates[position] < parsed_dates[position - 1]
            )
            issues.append(
                _make_issue(
                    ISSUE_NON_MONOTONIC_ADJ_FACTOR,
                    None,
                    max(1, len(bad_positions)),
                    "adjustment factor rows must already be chronological",
                    field="trade_date",
                    sample_keys=_sorted_sample(
                        _factor_sample(symbol, canonical_dates[position])
                        for position in bad_positions
                    ),
                )
            )

    numeric = _numeric_column(frame, "adj_factor")
    invalid_values = ~np.isfinite(numeric) | (numeric <= 0.0)
    if invalid_values.any():
        positions = tuple(_mask_positions(invalid_values))
        issues.append(
            _make_issue(
                ISSUE_INVALID_ADJ_FACTOR,
                None,
                int(invalid_values.sum()),
                "adjustment factors must be finite and strictly positive",
                field="adj_factor",
                sample_keys=_sorted_sample(
                    _factor_sample(symbol, canonical_dates[position])
                    if canonical_dates[position] != "invalid"
                    else "%s|invalid-row:%d" % (symbol, position)
                    for position in positions
                ),
            )
        )

    for symbol_column in ("symbol", "ts_code"):
        if symbol_column not in frame.columns:
            continue
        normalized_symbols = frame[symbol_column].map(
            lambda value: value.strip().upper()
            if isinstance(value, str)
            else ""
        )
        mismatched = normalized_symbols.to_numpy(dtype=object) != symbol
        if mismatched.any():
            positions = tuple(_mask_positions(mismatched))
            issues.append(
                _make_issue(
                    ISSUE_ADJ_FACTOR_SCOPE_MISMATCH,
                    None,
                    int(mismatched.sum()),
                    "adjustment factor symbols must match the requested symbol",
                    field=symbol_column,
                    sample_keys=_sorted_sample(
                        _factor_sample(symbol, canonical_dates[position])
                        if canonical_dates[position] != "invalid"
                        else "%s|invalid-row:%d" % (symbol, position)
                        for position in positions
                    ),
                )
            )

    if invalid_dates.any():
        available_set = frozenset(
            trade_date for trade_date in canonical_dates if trade_date != "invalid"
        )
    else:
        available_set = frozenset(canonical_dates)
    missing_dates = tuple(sorted(required_set - available_set))
    if missing_dates:
        issues.append(
            _make_issue(
                ISSUE_MISSING_ADJ_FACTOR,
                None,
                len(missing_dates),
                "every scoped trade day requires one exact adjustment factor",
                field="adj_factor",
                sample_keys=_sorted_sample(
                    _factor_sample(symbol, trade_date)
                    for trade_date in missing_dates
                ),
            )
        )
        issues.append(
            _make_issue(
                ISSUE_ADJ_FACTOR_SCOPE_MISMATCH,
                None,
                len(missing_dates),
                "adjustment factor scope does not cover the paired bar scope",
                field="trade_date",
                sample_keys=_sorted_sample(
                    _factor_sample(symbol, trade_date)
                    for trade_date in missing_dates
                ),
            )
        )

    if issues:
        return None, tuple(issues)

    date_to_factor = {
        trade_date: float(numeric[position])
        for position, trade_date in enumerate(canonical_dates)
    }
    base_date = canonical_dates[0]
    base_factor = float(numeric[0])
    if base_date > required_trade_dates[0]:
        issues.append(
            _make_issue(
                ISSUE_ADJ_FACTOR_SCOPE_MISMATCH,
                None,
                1,
                "adjustment factor base must not be later than the scope",
                field="trade_date",
                sample_keys=(_factor_sample(symbol, base_date),),
            )
        )
        return None, tuple(issues)

    scoped_factors = tuple(date_to_factor[date] for date in required_trade_dates)
    scoped_ratios = tuple(value / base_factor for value in scoped_factors)
    if any(not math.isfinite(value) or value <= 0.0 for value in scoped_ratios):
        issues.append(
            _make_issue(
                ISSUE_INVALID_ADJ_FACTOR,
                None,
                1,
                "adjustment factor ratios must be finite and positive",
                field="adj_factor",
            )
        )
        return None, tuple(issues)

    scope_payload = [
        _factor_sample(symbol, trade_date) for trade_date in required_trade_dates
    ]
    factor_payload = {
        "base_date": base_date,
        "base_factor": format(base_factor, ".17g"),
        "rows": [
            {
                "adj_factor": format(factor, ".17g"),
                "trade_date": trade_date,
            }
            for trade_date, factor in zip(required_trade_dates, scoped_factors)
        ],
        "source": source,
        "symbol": symbol,
    }
    return (
        _ValidatedAdjustmentFactorScope(
            symbol=symbol,
            source=source,
            base_date=base_date,
            base_factor=base_factor,
            trade_dates=required_trade_dates,
            factors=scoped_factors,
            ratios=scoped_ratios,
            scope_trade_dates_sha256=hashlib.sha256(
                _canonical_json(scope_payload).encode("utf-8")
            ).hexdigest(),
            factor_values_sha256=hashlib.sha256(
                _canonical_json(factor_payload).encode("utf-8")
            ).hexdigest(),
        ),
        (),
    )


def validate_adjustment_factor_ratio(
    adjustment_factors: pd.DataFrame,
    *,
    symbol: str,
    trade_date: Any,
    source: str,
) -> float:
    """Return the exact-day ratio or raise; never fill or return ``1``."""

    canonical_symbol = _normalize_symbol(symbol)
    canonical_date = _canonical_daily_date(trade_date, "trade_date")
    canonical_source = _canonical_factor_source(source)
    scope, issues = _inspect_adjustment_factor_scope(
        adjustment_factors,
        symbol=canonical_symbol,
        required_trade_dates=(canonical_date,),
        source=canonical_source,
    )
    if issues or scope is None:
        raise AdjustmentFactorError(canonical_symbol, issues)
    return scope.ratio_for(canonical_date)


def _paired_trade_dates(
    index: Optional[pd.DatetimeIndex],
) -> Tuple[str, ...]:
    if index is None or len(index) == 0 or bool(index.isna().any()):
        return ()
    return tuple(sorted(set(index.strftime("%Y%m%d"))))


def _validate_pair_adjustment_identity(
    *,
    adjustment_factors: pd.DataFrame,
    adjustment_factor_source: str,
    symbol: str,
    frequency: Frequency,
    strategy_space: PriceSpace,
    execution_space: PriceSpace,
    strategy: pd.DataFrame,
    execution: pd.DataFrame,
    strategy_index: Optional[pd.DatetimeIndex],
    execution_index: Optional[pd.DatetimeIndex],
    issues: List[DataQualityIssue],
) -> Optional[AdjustmentFactorIdentity]:
    if (
        strategy_index is None
        or execution_index is None
        or len(strategy_index) == 0
        or not strategy_index.equals(execution_index)
    ):
        return None
    trade_dates = _paired_trade_dates(strategy_index)
    if not trade_dates:
        return None
    scope, factor_issues = _inspect_adjustment_factor_scope(
        adjustment_factors,
        symbol=symbol,
        required_trade_dates=trade_dates,
        source=adjustment_factor_source,
    )
    issues.extend(factor_issues)
    if scope is None:
        return None
    if "close" not in strategy.columns or "close" not in execution.columns:
        return None

    strategy_close = pd.to_numeric(strategy["close"], errors="coerce").to_numpy(
        dtype="float64", na_value=np.nan
    )
    execution_close = pd.to_numeric(execution["close"], errors="coerce").to_numpy(
        dtype="float64", na_value=np.nan
    )
    if (
        not np.isfinite(strategy_close).all()
        or not np.isfinite(execution_close).all()
        or (strategy_close <= 0.0).any()
        or (execution_close <= 0.0).any()
    ):
        return None

    if strategy_space is PriceSpace.HFQ:
        hfq_close = strategy_close
        raw_close = execution_close
    else:
        hfq_close = execution_close
        raw_close = strategy_close
    ratios = np.asarray(
        [scope.ratio_for(timestamp.strftime("%Y%m%d")) for timestamp in strategy_index],
        dtype="float64",
    )
    expected_hfq = raw_close * ratios
    matched = np.isclose(
        hfq_close,
        expected_hfq,
        rtol=ADJUSTMENT_PRICE_REL_TOLERANCE,
        atol=ADJUSTMENT_PRICE_ABS_TOLERANCE,
        equal_nan=False,
    )
    if not bool(matched.all()):
        mismatches = ~matched
        issues.append(
            _make_issue(
                ISSUE_PRICE_IDENTITY_MISMATCH,
                None,
                int(mismatches.sum()),
                "%s within rtol=%s atol=%s"
                % (
                    ADJUSTMENT_FACTOR_RELATION,
                    format(ADJUSTMENT_PRICE_REL_TOLERANCE, ".17g"),
                    format(ADJUSTMENT_PRICE_ABS_TOLERANCE, ".17g"),
                ),
                field="close",
                sample_keys=_row_keys(
                    symbol,
                    strategy_index,
                    _mask_positions(mismatches),
                    frequency,
                ),
            )
        )
        return None

    return AdjustmentFactorIdentity(
        symbol=symbol,
        frequency=frequency,
        strategy_price_space=strategy_space,
        execution_price_space=execution_space,
        scope_start=trade_dates[0],
        scope_end=trade_dates[-1],
        scope_trade_days=len(trade_dates),
        scope_trade_dates_sha256=scope.scope_trade_dates_sha256,
        scope_observation_keys_sha256=hashlib.sha256(
            _canonical_json(
                [
                    "%s@%s"
                    % (symbol, _format_timestamp(pd.Timestamp(timestamp), frequency))
                    for timestamp in strategy_index
                ]
            ).encode("utf-8")
        ).hexdigest(),
        validated_close_rows=len(strategy_index),
        source=scope.source,
        base_date=scope.base_date,
        base_factor=float(scope.base_factor),
        factor_values_sha256=scope.factor_values_sha256,
    )


def _normalize_amount_columns(
    frame: pd.DataFrame, amount_unit: AmountUnit
) -> None:
    if "amount" not in frame.columns:
        return
    amount = frame["amount"].to_numpy(dtype="float64", na_value=np.nan)
    multiplier = 1000.0 if amount_unit is AmountUnit.THOUSAND_YUAN else 1.0
    frame["amount"] = amount * multiplier


def validate_and_align_pair(
    strategy_data: pd.DataFrame,
    execution_data: pd.DataFrame,
    *,
    symbol: str,
    strategy_price_space: PriceSpace,
    execution_price_space: PriceSpace,
    strategy_amount_unit: AmountUnit,
    execution_amount_unit: AmountUnit,
    frequency: Frequency,
    pre_close_exempt_dates: Iterable[Any] = (),
    strategy_pre_close_source: Optional[PreCloseSource] = None,
    execution_pre_close_source: Optional[PreCloseSource] = None,
    adjustment_factors: Optional[pd.DataFrame] = None,
    adjustment_factor_source: Optional[str] = None,
    require_adjustment_factor_identity: bool = False,
) -> AlignedMarketData:
    """Validate and canonically align a strategy/execution market-data pair.

    The returned frames are deep copies with a ``DatetimeIndex`` named
    ``timestamp``.  The requested ``symbol`` supplies the other component of
    the canonical key.  Both key sets must already be equal, unique, and
    monotonically increasing; the function never sorts or intersects them.

    Each track's source ``amount`` unit must be declared explicitly.  Returned
    ``amount`` columns are normalized to yuan only after the entire pair has
    passed validation.  ``pre_close_exempt_dates`` is an explicit, audited
    exception only for non-positive/non-finite pre-close values on those
    dates; it never relaxes the required column or any other check.

    A provider that supplies distinct RAW/HFQ tracks sets
    ``require_adjustment_factor_identity=True`` and passes the exact factor
    dataset plus its source label.  In that mode every trade day must have one
    positive finite factor and every close must satisfy the frozen AFI-1
    relation.  No row is sorted, filled, carried forward, or defaulted to one.
    """

    if not isinstance(strategy_data, pd.DataFrame):
        raise TypeError("strategy_data must be a pandas DataFrame")
    if not isinstance(execution_data, pd.DataFrame):
        raise TypeError("execution_data must be a pandas DataFrame")

    canonical_symbol = _normalize_symbol(symbol)
    strategy_space = _coerce_enum(
        strategy_price_space, PriceSpace, "strategy_price_space"
    )
    execution_space = _coerce_enum(
        execution_price_space, PriceSpace, "execution_price_space"
    )
    strategy_unit = _coerce_enum(
        strategy_amount_unit, AmountUnit, "strategy_amount_unit"
    )
    execution_unit = _coerce_enum(
        execution_amount_unit, AmountUnit, "execution_amount_unit"
    )
    canonical_frequency = _coerce_enum(frequency, Frequency, "frequency")
    canonical_pre_close_exempt_dates = _normalize_pre_close_exempt_dates(
        pre_close_exempt_dates
    )
    canonical_strategy_pre_close_source = _resolve_pre_close_source(
        strategy_pre_close_source,
        strategy_data,
        canonical_frequency,
        "strategy_pre_close_source",
    )
    canonical_execution_pre_close_source = _resolve_pre_close_source(
        execution_pre_close_source,
        execution_data,
        canonical_frequency,
        "execution_pre_close_source",
    )
    if type(require_adjustment_factor_identity) is not bool:
        raise TypeError("require_adjustment_factor_identity must be exactly bool")
    distinct_price_spaces = strategy_space is not execution_space
    if require_adjustment_factor_identity and not distinct_price_spaces:
        raise ValueError(
            "adjustment factor identity cannot be required for one price space"
        )
    if adjustment_factors is not None and not distinct_price_spaces:
        raise ValueError(
            "adjustment factors are invalid when both tracks share a price space"
        )
    if adjustment_factor_source is not None:
        canonical_adjustment_source = _canonical_factor_source(
            adjustment_factor_source
        )
    else:
        canonical_adjustment_source = None
    if adjustment_factors is not None and canonical_adjustment_source is None:
        raise ValueError(
            "adjustment_factor_source is required with adjustment_factors"
        )

    issues: List[DataQualityIssue] = []
    if len(strategy_data) == 0:
        issues.append(
            _make_issue(
                ISSUE_MISSING_DATASET,
                TrackRole.STRATEGY,
                1,
                "strategy dataset must contain at least one row",
            )
        )
    if len(execution_data) == 0:
        issues.append(
            _make_issue(
                ISSUE_MISSING_DATASET,
                TrackRole.EXECUTION,
                1,
                "execution dataset must contain at least one row",
            )
        )

    strategy_prepared = _prepare_track_frame(
        strategy_data, TrackRole.STRATEGY, canonical_symbol, issues
    )
    execution_prepared = _prepare_track_frame(
        execution_data, TrackRole.EXECUTION, canonical_symbol, issues
    )

    strategy, strategy_index = _normalize_track_index(
        strategy_prepared,
        TrackRole.STRATEGY,
        canonical_symbol,
        canonical_frequency,
        issues,
    )
    execution, execution_index = _normalize_track_index(
        execution_prepared,
        TrackRole.EXECUTION,
        canonical_symbol,
        canonical_frequency,
        issues,
    )

    _validate_symbol_columns(
        strategy,
        strategy_index,
        TrackRole.STRATEGY,
        canonical_symbol,
        canonical_frequency,
        issues,
    )
    _validate_symbol_columns(
        execution,
        execution_index,
        TrackRole.EXECUTION,
        canonical_symbol,
        canonical_frequency,
        issues,
    )
    _validate_index(
        strategy_index,
        TrackRole.STRATEGY,
        canonical_symbol,
        canonical_frequency,
        issues,
    )
    _validate_index(
        execution_index,
        TrackRole.EXECUTION,
        canonical_symbol,
        canonical_frequency,
        issues,
    )
    _validate_required_columns(
        strategy,
        strategy_index,
        TrackRole.STRATEGY,
        canonical_symbol,
        canonical_frequency,
        strategy_unit,
        canonical_pre_close_exempt_dates,
        issues,
    )
    _validate_required_columns(
        execution,
        execution_index,
        TrackRole.EXECUTION,
        canonical_symbol,
        canonical_frequency,
        execution_unit,
        canonical_pre_close_exempt_dates,
        issues,
    )
    aligned_rows = _validate_key_sets(
        strategy_index,
        execution_index,
        canonical_symbol,
        canonical_frequency,
        issues,
    )
    adjustment_factor_identity = None
    if distinct_price_spaces and (
        require_adjustment_factor_identity or adjustment_factors is not None
    ):
        if adjustment_factors is None:
            trade_dates = _paired_trade_dates(strategy_index)
            issues.append(
                _make_issue(
                    ISSUE_MISSING_ADJ_FACTOR,
                    None,
                    max(1, len(trade_dates)),
                    "distinct raw/hfq tracks require adjustment factors",
                    field="adj_factor",
                    sample_keys=_sorted_sample(
                        _factor_sample(canonical_symbol, trade_date)
                        for trade_date in trade_dates
                    ),
                )
            )
        elif canonical_adjustment_source is not None:
            adjustment_factor_identity = _validate_pair_adjustment_identity(
                adjustment_factors=adjustment_factors,
                adjustment_factor_source=canonical_adjustment_source,
                symbol=canonical_symbol,
                frequency=canonical_frequency,
                strategy_space=strategy_space,
                execution_space=execution_space,
                strategy=strategy,
                execution=execution,
                strategy_index=strategy_index,
                execution_index=execution_index,
                issues=issues,
            )

    ordered_issues = tuple(sorted(issues, key=_issue_sort_key))
    report = DataQualityReport(
        symbol=canonical_symbol,
        frequency=canonical_frequency,
        strategy_price_space=strategy_space,
        execution_price_space=execution_space,
        strategy_amount_unit=strategy_unit,
        execution_amount_unit=execution_unit,
        strategy_rows=len(strategy_data),
        execution_rows=len(execution_data),
        aligned_rows=aligned_rows,
        issues=ordered_issues,
        pre_close_exempt_dates=canonical_pre_close_exempt_dates,
        strategy_pre_close_source=canonical_strategy_pre_close_source,
        execution_pre_close_source=canonical_execution_pre_close_source,
        adjustment_factor_identity=adjustment_factor_identity,
    )
    if ordered_issues:
        raise DataContractError(report)

    _normalize_amount_columns(strategy, strategy_unit)
    _normalize_amount_columns(execution, execution_unit)
    return AlignedMarketData(
        symbol=canonical_symbol,
        frequency=canonical_frequency,
        strategy_price_space=strategy_space,
        execution_price_space=execution_space,
        strategy_amount_unit=strategy_unit,
        execution_amount_unit=execution_unit,
        strategy=strategy,
        execution=execution,
        report=report,
    )


def inspect_pair(
    strategy_data: pd.DataFrame,
    execution_data: pd.DataFrame,
    *,
    symbol: str,
    strategy_price_space: PriceSpace,
    execution_price_space: PriceSpace,
    strategy_amount_unit: AmountUnit,
    execution_amount_unit: AmountUnit,
    frequency: Frequency,
    pre_close_exempt_dates: Iterable[Any] = (),
    strategy_pre_close_source: Optional[PreCloseSource] = None,
    execution_pre_close_source: Optional[PreCloseSource] = None,
    adjustment_factors: Optional[pd.DataFrame] = None,
    adjustment_factor_source: Optional[str] = None,
    require_adjustment_factor_identity: bool = False,
) -> DataQualityReport:
    """Return the complete strict DC-1 report without enforcing it.

    Configuration/type errors still raise because no meaningful report can be
    constructed for an unknown contract dimension.  Data-quality violations
    are returned to callers so providers can run the contract in shadow/audit
    mode before engine integration switches to fail-fast enforcement.
    """

    try:
        aligned = validate_and_align_pair(
            strategy_data,
            execution_data,
            symbol=symbol,
            strategy_price_space=strategy_price_space,
            execution_price_space=execution_price_space,
            strategy_amount_unit=strategy_amount_unit,
            execution_amount_unit=execution_amount_unit,
            frequency=frequency,
            pre_close_exempt_dates=pre_close_exempt_dates,
            strategy_pre_close_source=strategy_pre_close_source,
            execution_pre_close_source=execution_pre_close_source,
            adjustment_factors=adjustment_factors,
            adjustment_factor_source=adjustment_factor_source,
            require_adjustment_factor_identity=(
                require_adjustment_factor_identity
            ),
        )
    except DataContractError as error:
        return error.report
    return aligned.report


__all__ = [
    "ADJUSTMENT_FACTOR_IDENTITY_SCHEMA",
    "ADJUSTMENT_FACTOR_IDENTITY_SCHEMA_VERSION",
    "ADJUSTMENT_FACTOR_IDENTITY_VERSION",
    "ADJUSTMENT_FACTOR_RELATION",
    "ADJUSTMENT_PRICE_ABS_TOLERANCE",
    "ADJUSTMENT_PRICE_REL_TOLERANCE",
    "AdjustmentFactorError",
    "AdjustmentFactorIdentity",
    "AdjustmentFactorRunIdentity",
    "AlignedMarketData",
    "AmountUnit",
    "CONTRACT_VERSION",
    "DataContractError",
    "DataQualityIssue",
    "DataQualityReport",
    "Frequency",
    "ISSUE_CODES",
    "MAX_ISSUE_SAMPLES",
    "PriceSpace",
    "PreCloseSource",
    "SCHEMA_VERSION",
    "TrackRole",
    "adjustment_factor_assumption_value",
    "adjustment_factor_identity_artifact",
    "build_adjustment_factor_run_identity",
    "inspect_pair",
    "validate_adjustment_factor_ratio",
    "validate_and_align_pair",
]
