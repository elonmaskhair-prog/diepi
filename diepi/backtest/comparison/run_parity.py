"""Deterministic run-to-run parity reports for cash backtests.

Reference-index comparison answers whether a strategy beat an external series.
Run parity answers a different question: whether two executions of a strategy
produced the same observable economic result.  The comparison is deliberately
strict about scope and field selection; it never intersects dates or silently
drops fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import hashlib
import json
import math
from numbers import Integral, Real
from pathlib import Path
import re
from typing import Any, Optional, Sequence, Tuple

import pandas as pd


RUN_PARITY_SCHEMA_VERSION = 2
RUN_PARITY_SNAPSHOT_SCHEMA = "diepi.run_parity_snapshot"
RUN_PARITY_REPORT_SCHEMA = "diepi.run_parity_report"

DEFAULT_PARITY_DAILY_FIELDS = (
    "cash",
    "market_value",
    "total_value",
)
DEFAULT_PARITY_TRADE_FIELDS = (
    "event_sequence",
    "symbol",
    "direction",
    "shares",
    "price",
    "amount",
    "time",
    "simulated_time",
    "fee",
    "commission",
    "stamp_duty",
    "transfer_fee",
    "cash_delta",
    "realized_pnl",
    "t0",
    "order_type",
    "note",
    "requested_shares",
    "requested_amount",
    "requested_percent",
    "auto_resized",
    "resized_from_shares",
    "resized_to_shares",
    "resize_reason",
)
LEGACY_COMMON_TRADE_FIELDS = (
    "symbol",
    "direction",
    "shares",
    "price",
    "amount",
    "time",
)
LEDGER_PARITY_METRIC_FIELDS = (
    "initial_cash",
    "final_value",
    "total_return",
    "trade_count",
)
DERIVED_PARITY_METRIC_FIELDS = (
    "annual_return",
    "max_drawdown",
    "win_rate",
    "sharpe_ratio",
)
DEFAULT_PARITY_METRIC_FIELDS = (
    LEDGER_PARITY_METRIC_FIELDS + DERIVED_PARITY_METRIC_FIELDS
)
REQUIRED_PARITY_METRIC_DEFINITION_KEYS = (
    "cash.trade_count_unit",
    "cash.win_rate_unit",
    "metrics.intraday_high_to_low",
    "metrics.intraday_low_path",
    "metrics.return_std_ddof",
    "metrics.risk_free_rate",
    "metrics.schema_version",
    "metrics.trading_days_per_year",
)
_EXACT_TEXT_FIELDS = frozenset(
    {
        "direction",
        "note",
        "order_type",
        "resize_reason",
        "simulated_time",
        "symbol",
        "time",
    }
)
_EXACT_BOOL_FIELDS = frozenset({"auto_resized", "t0"})
_EXACT_INT_FIELDS = frozenset(
    {
        "event_sequence",
        "requested_shares",
        "resized_from_shares",
        "resized_to_shares",
        "shares",
        "trade_count",
    }
)
_TOLERANT_FLOAT_FIELDS = frozenset(
    {
        "amount",
        "annual_return",
        "cash",
        "cash_delta",
        "commission",
        "fee",
        "final_value",
        "initial_cash",
        "market_value",
        "max_drawdown",
        "price",
        "realized_pnl",
        "requested_amount",
        "requested_percent",
        "sharpe_ratio",
        "stamp_duty",
        "total_return",
        "total_value",
        "transfer_fee",
        "win_rate",
    }
)
_NULLABLE_FIELDS = frozenset(
    {
        "realized_pnl",
        "requested_amount",
        "requested_percent",
        "requested_shares",
        "resized_from_shares",
        "resized_to_shares",
        "sharpe_ratio",
        "win_rate",
    }
)

_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_OBSERVATION_RE = re.compile(r"^\d{8}$")
_Primitive = Optional[object]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _text(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exactly str")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty and trimmed")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    return value


def _fields(value: Sequence[str], name: str) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be a sequence of field names")
    result = tuple(value)
    if not result:
        raise ValueError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    for item in result:
        if type(item) is not str or not _FIELD_RE.fullmatch(item):
            raise ValueError(f"{name} must contain lower snake-case names")
    return result


def _primitive(value: Any, name: str) -> _Primitive:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"{name} must be finite or null")
        return 0.0 if number == 0.0 else number
    if isinstance(value, (pd.Timestamp, datetime)):
        timestamp = pd.Timestamp(value)
        if timestamp.tz is not None:
            raise ValueError(f"{name} datetime must be timezone-naive")
        return timestamp.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        if any(ord(character) < 32 for character in value):
            raise ValueError(f"{name} must not contain control characters")
        return value
    raise TypeError(
        f"{name} must be a JSON scalar (str, bool, int, float, or null)"
    )


def _parity_primitive(value: Any, field: str, name: str) -> _Primitive:
    primitive = _primitive(value, name)
    if primitive is None:
        if field not in _NULLABLE_FIELDS:
            raise TypeError(f"{name} must not be null")
        return None
    if field in _EXACT_TEXT_FIELDS:
        if type(primitive) is not str:
            raise TypeError(f"{name} must be exactly text")
        return primitive
    if field in _EXACT_BOOL_FIELDS:
        if type(primitive) is not bool:
            raise TypeError(f"{name} must be exactly bool")
        return primitive
    if field in _EXACT_INT_FIELDS:
        if type(primitive) is not int:
            raise TypeError(f"{name} must be exactly int")
        return primitive
    if field in _TOLERANT_FLOAT_FIELDS:
        if type(primitive) not in (int, float):
            raise TypeError(f"{name} must be numeric")
        return float(primitive)
    return primitive


def _observation_id(value: Any, name: str) -> str:
    if isinstance(value, (pd.Timestamp, datetime, date)):
        timestamp = pd.Timestamp(value)
        if timestamp.tz is not None:
            raise ValueError(f"{name} must be timezone-naive")
        result = timestamp.strftime("%Y%m%d")
    elif isinstance(value, str):
        candidate = value.strip()
        if _OBSERVATION_RE.fullmatch(candidate):
            result = candidate
        else:
            try:
                timestamp = pd.Timestamp(candidate)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be a date") from exc
            if timestamp.tz is not None:
                raise ValueError(f"{name} must be timezone-naive")
            result = timestamp.strftime("%Y%m%d")
    else:
        raise TypeError(f"{name} must be a date or date string")
    try:
        parsed = datetime.strptime(result, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid date") from exc
    if parsed.strftime("%Y%m%d") != result:
        raise ValueError(f"{name} must be a valid date")
    return result


class RunParityStatus(str, Enum):
    EXACT = "EXACT"
    WITHIN_TOLERANCE = "WITHIN_TOLERANCE"
    DIFFERENT = "DIFFERENT"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    UNVERIFIED = "UNVERIFIED"
    NOT_RANKABLE = "NOT_RANKABLE"
    UNATTESTED = "UNATTESTED"


@dataclass(frozen=True)
class RunParityPolicy:
    """The explicit projection and numeric tolerance used by one report."""

    absolute_tolerance: float = 0.0
    relative_tolerance: float = 0.0
    daily_fields: Tuple[str, ...] = DEFAULT_PARITY_DAILY_FIELDS
    trade_fields: Tuple[str, ...] = DEFAULT_PARITY_TRADE_FIELDS
    metric_fields: Tuple[str, ...] = DEFAULT_PARITY_METRIC_FIELDS
    profile_name: str = "cash_full_v2"

    def __post_init__(self) -> None:
        for name in ("absolute_tolerance", "relative_tolerance"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Real):
                raise TypeError(f"{name} must be a real number")
            number = float(value)
            if not math.isfinite(number) or number < 0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, number)
        object.__setattr__(
            self, "daily_fields", _fields(self.daily_fields, "daily_fields")
        )
        object.__setattr__(
            self, "trade_fields", _fields(self.trade_fields, "trade_fields")
        )
        object.__setattr__(
            self, "metric_fields", _fields(self.metric_fields, "metric_fields")
        )
        if self.profile_name not in {"cash_full_v2", "legacy_common_v1"}:
            raise ValueError("unsupported run parity profile_name")
        expected_trade_fields = (
            DEFAULT_PARITY_TRADE_FIELDS
            if self.profile_name == "cash_full_v2"
            else LEGACY_COMMON_TRADE_FIELDS
        )
        if self.daily_fields != DEFAULT_PARITY_DAILY_FIELDS:
            raise ValueError(f"{self.profile_name} requires its fixed daily projection")
        if self.trade_fields != expected_trade_fields:
            raise ValueError(f"{self.profile_name} requires its fixed trade projection")
        if self.metric_fields != DEFAULT_PARITY_METRIC_FIELDS:
            raise ValueError(f"{self.profile_name} requires its fixed metric projection")

    @classmethod
    def legacy_common(
        cls, *, absolute_tolerance: float = 0.0, relative_tolerance: float = 0.0
    ) -> "RunParityPolicy":
        return cls(
            absolute_tolerance=absolute_tolerance,
            relative_tolerance=relative_tolerance,
            trade_fields=LEGACY_COMMON_TRADE_FIELDS,
            profile_name="legacy_common_v1",
        )

    @property
    def complete_ledger(self) -> bool:
        return self.profile_name == "cash_full_v2"

    def to_dict(self) -> dict:
        return {
            "absolute_tolerance": self.absolute_tolerance,
            "daily_fields": list(self.daily_fields),
            "metric_fields": list(self.metric_fields),
            "profile_name": self.profile_name,
            "complete_ledger": self.complete_ledger,
            "relative_tolerance": self.relative_tolerance,
            "trade_ordering": (
                "canonical_semantic_v2"
                if self.complete_ledger
                else "legacy_common_semantic_v1"
            ),
            "trade_fields": list(self.trade_fields),
        }


@dataclass(frozen=True)
class CashRunParitySnapshot:
    """A small, immutable projection of one cash result.

    Internal order/fill identifiers are excluded by the default policy.  This
    makes the trade comparison semantic while the daily cash/NAV series keeps
    the economic ledger observable.
    """

    label: str
    observation_ids: Tuple[str, ...]
    daily_fields: Tuple[str, ...]
    daily_rows: Tuple[Tuple[_Primitive, ...], ...]
    trade_fields: Tuple[str, ...]
    trade_rows: Tuple[Tuple[_Primitive, ...], ...]
    metric_fields: Tuple[str, ...]
    metric_values: Tuple[_Primitive, ...]
    metric_definition: Tuple[Tuple[str, str], ...] = ()
    economic_origin_sha256: Optional[str] = None
    event_journal_sha256: Optional[str] = None
    terminal_state_sha256: Optional[str] = None
    input_artifact_verified: Optional[bool] = None
    input_rankable: Optional[bool] = None
    input_run_id: Optional[str] = None
    input_manifest_sha256: Optional[str] = None
    schema_version: int = RUN_PARITY_SCHEMA_VERSION
    _source_kind: str = field(
        default="RAW", init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        _text(self.label, "label")
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be exactly int")
        if self.schema_version != RUN_PARITY_SCHEMA_VERSION:
            raise ValueError("unsupported run parity snapshot schema_version")
        object.__setattr__(
            self, "daily_fields", _fields(self.daily_fields, "daily_fields")
        )
        object.__setattr__(
            self, "trade_fields", _fields(self.trade_fields, "trade_fields")
        )
        object.__setattr__(
            self, "metric_fields", _fields(self.metric_fields, "metric_fields")
        )
        observations = tuple(
            _observation_id(value, f"observation_ids[{index}]")
            for index, value in enumerate(self.observation_ids)
        )
        if len(set(observations)) != len(observations):
            raise ValueError("observation_ids must be unique")
        if tuple(sorted(observations)) != observations:
            raise ValueError("observation_ids must be increasing")
        if len(self.daily_rows) != len(observations):
            raise ValueError("daily_rows must have one row per observation")
        object.__setattr__(self, "observation_ids", observations)
        object.__setattr__(
            self,
            "daily_rows",
            self._validated_rows(
                self.daily_rows, self.daily_fields, "daily_rows"
            ),
        )
        object.__setattr__(
            self,
            "trade_rows",
            self._validated_rows(
                self.trade_rows, self.trade_fields, "trade_rows"
            ),
        )
        if len(self.metric_values) != len(self.metric_fields):
            raise ValueError("metric_values must have one value per metric field")
        object.__setattr__(
            self,
            "metric_values",
            tuple(
                _parity_primitive(
                    value,
                    self.metric_fields[index],
                    f"metric_values[{index}]",
                )
                for index, value in enumerate(self.metric_values)
            ),
        )
        if type(self.metric_definition) is not tuple:
            raise TypeError("metric_definition must be exactly tuple")
        definition = []
        for index, item in enumerate(self.metric_definition):
            if type(item) is not tuple or len(item) != 2:
                raise TypeError(
                    f"metric_definition[{index}] must be a (key, value) tuple"
                )
            key = _text(item[0], f"metric_definition[{index}].key")
            value = _text(item[1], f"metric_definition[{index}].value")
            if not _is_metric_definition_key(key):
                raise ValueError(
                    "metric definition keys must describe a supported metric "
                    "or cash aggregation unit"
                )
            definition.append((key, value))
        if tuple(sorted(definition)) != tuple(definition):
            raise ValueError("metric_definition must be sorted by key")
        if len({key for key, _ in definition}) != len(definition):
            raise ValueError("metric_definition keys must be unique")
        object.__setattr__(self, "metric_definition", tuple(definition))
        for name in (
            "economic_origin_sha256",
            "event_journal_sha256",
            "terminal_state_sha256",
            "input_manifest_sha256",
        ):
            value = getattr(self, name)
            if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be lowercase SHA-256 or null")
        for name in ("input_artifact_verified", "input_rankable"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be bool or null")
        if self.input_artifact_verified is None:
            if self.input_rankable is not None:
                raise ValueError(
                    "input_rankable requires an artifact-backed input"
                )
            if self.input_run_id is not None or self.input_manifest_sha256 is not None:
                raise ValueError(
                    "run/manifest identity requires an artifact-backed input"
                )
        else:
            if self.input_rankable is None:
                raise ValueError(
                    "artifact-backed input must declare input_rankable"
                )
            if self.input_run_id is None:
                raise ValueError("artifact-backed input must declare input_run_id")
            _text(self.input_run_id, "input_run_id")
            if self.input_artifact_verified and self.input_manifest_sha256 is None:
                raise ValueError(
                    "verified input must declare input_manifest_sha256"
                )

    @staticmethod
    def _validated_rows(rows, fields, name):
        if type(rows) is not tuple:
            raise TypeError(f"{name} must be exactly tuple")
        result = []
        for row_index, row in enumerate(rows):
            if type(row) is not tuple:
                raise TypeError(f"{name}[{row_index}] must be exactly tuple")
            if len(row) != len(fields):
                raise ValueError(
                    f"{name}[{row_index}] must have one value per field"
                )
            result.append(
                tuple(
                    _parity_primitive(
                        value,
                        fields[column_index],
                        f"{name}[{row_index}][{column_index}]",
                    )
                    for column_index, value in enumerate(row)
                )
            )
        return tuple(result)

    def to_dict(self) -> dict:
        return {
            "daily_fields": list(self.daily_fields),
            "daily_rows": [list(row) for row in self.daily_rows],
            "economic_origin_sha256": self.economic_origin_sha256,
            "event_journal_sha256": self.event_journal_sha256,
            "input_artifact_verified": self.input_artifact_verified,
            "input_manifest_sha256": self.input_manifest_sha256,
            "input_rankable": self.input_rankable,
            "input_run_id": self.input_run_id,
            "label": self.label,
            "metric_fields": list(self.metric_fields),
            "metric_definition": [list(item) for item in self.metric_definition],
            "metric_values": list(self.metric_values),
            "observation_ids": list(self.observation_ids),
            "schema": RUN_PARITY_SNAPSHOT_SCHEMA,
            "schema_version": self.schema_version,
            "terminal_state_sha256": self.terminal_state_sha256,
            "trade_fields": list(self.trade_fields),
            "trade_rows": [list(row) for row in self.trade_rows],
        }

    @property
    def snapshot_sha256(self) -> str:
        payload = self.to_dict()
        # Labels belong to a comparison report, not to the economic identity
        # of a captured run.  Renaming baseline/candidate must not change the
        # snapshot digest.
        payload.pop("label")
        return hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest()


def _unwrap_cash_result(value: Any) -> Any:
    if hasattr(value, "daily_values") and hasattr(value, "trades"):
        return value
    nested = getattr(value, "result", None)
    if nested is not None and hasattr(nested, "daily_values") and hasattr(
        nested, "trades"
    ):
        return nested
    raise TypeError(
        "result must be a cash result or a loaded run containing one"
    )


def _prepare_source(value: Any) -> Tuple[Any, str]:
    """Reload exact artifact wrappers so cached mutable results are not trusted.

    Python objects are not an authentication boundary.  A result is therefore
    called verified only when this capture re-opens the exact ``LoadedRun``
    root, verifies its manifest and payload hashes, and reconstructs its result
    again.  Duck-typed objects remain raw diagnostic inputs.
    """

    from diepi.artifacts.storage import ArtifactStore, LoadedLegacyRun, LoadedRun

    if type(value) is LoadedRun:
        return ArtifactStore.load(value.root), "VERIFIED_ARTIFACT"
    if type(value) is LoadedLegacyRun:
        return ArtifactStore.load_legacy(value.root), "LEGACY"
    return value, "RAW"


def _source_identity(value: Any, source_kind: str) -> Tuple[
    Optional[bool], Optional[bool], Optional[str], Optional[str]
]:
    """Capture evidence only from an exact wrapper reloaded above."""

    if source_kind == "RAW":
        return None, None, None, None
    root = Path(value.root)
    if source_kind == "LEGACY":
        run_id = root.name
        _text(run_id, "loaded run_id")
        return False, False, run_id, None
    if source_kind != "VERIFIED_ARTIFACT":
        raise ValueError("unsupported run parity source kind")
    if value.artifact_verified is not True:
        raise ValueError("reloaded RunArtifact is not verified")
    # A v1 RunArtifact is a portable, manifest-identified directory.  Copying
    # or renaming its outer folder must not change the attested run identity.
    # ArtifactStore.load() has already verified the closed member set and all
    # payload hashes, so trust the signed-by-content manifest field instead of
    # treating the filesystem basename as an authentication boundary.
    run_id = value.manifest.run_id
    _text(run_id, "loaded run_id")
    manifest_bytes = value.manifest.to_json_bytes()
    return (
        True,
        value.is_rankable,
        run_id,
        hashlib.sha256(manifest_bytes).hexdigest(),
    )


def _economic_origin_sha256(result: Any) -> Optional[str]:
    """Hash the canonical cash replay seed, including initial positions."""

    bundle = getattr(result, "cash_audit", None)
    if bundle is None:
        return None
    seed = getattr(bundle, "seed", None)
    to_dict = getattr(seed, "to_dict", None)
    if seed is None or not callable(to_dict):
        raise TypeError("result.cash_audit.seed must expose to_dict()")
    payload = to_dict()
    if type(payload) is not dict:
        raise TypeError("cash replay seed to_dict() must return exactly dict")
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _event_journal_sha256(result: Any) -> Optional[str]:
    """Bind the full ordered cash-event path, including active-order facts."""

    bundle = getattr(result, "cash_audit", None)
    if bundle is None:
        return None
    journal_json = getattr(bundle, "journal_json", None)
    if type(journal_json) is not str:
        # Test doubles and compatible public objects may expose only journal.
        journal = getattr(bundle, "journal", None)
        to_json = getattr(journal, "to_json", None)
        if not callable(to_json):
            raise TypeError(
                "result.cash_audit must expose canonical journal_json or journal"
            )
        journal_json = to_json()
    return hashlib.sha256(journal_json.encode("utf-8")).hexdigest()


def _terminal_state_sha256(result: Any) -> Optional[str]:
    """Hash the replayed terminal account state without volatile identifiers."""

    bundle = getattr(result, "cash_audit", None)
    if bundle is None:
        return None
    replay_method = getattr(bundle, "replay", None)
    if not callable(replay_method):
        raise TypeError("result.cash_audit must expose replay()")
    replay = replay_method()
    state = getattr(replay, "state", None)
    if state is None:
        raise TypeError("cash audit replay must expose terminal state")
    payload = {
        "cash": _primitive(state.cash, "terminal cash"),
        "frozen_cash": _primitive(state.frozen_cash, "terminal frozen_cash"),
        "last_sequence": _primitive(state.last_sequence, "terminal last_sequence"),
        "orders": sorted(
            (
                {
                    "declared_quantity": item.declared_quantity,
                    "filled_quantity": item.filled_quantity,
                    "order_id": item.order_id,
                    "side": item.side,
                    "status": item.status,
                    "symbol": item.symbol,
                }
                for item in state.orders
            ),
            key=_canonical_json,
        ),
        "positions": sorted(
            (
                {
                    "available_shares": item.available_shares,
                    "avg_cost": _primitive(item.avg_cost, "terminal avg_cost"),
                    "cost_basis": _primitive(item.cost_basis, "terminal cost_basis"),
                    "current_price": _primitive(
                        item.current_price, "terminal current_price"
                    ),
                    "frozen_shares": item.frozen_shares,
                    "pending_t1": [
                        value.to_dict() for value in item.pending_t1
                    ],
                    "shares": item.shares,
                    "symbol": item.symbol,
                }
                for item in state.positions
            ),
            key=_canonical_json,
        ),
        "reservations": sorted(
            (
                {
                    "amount": _primitive(item.amount, "terminal reservation"),
                    "asset": item.asset,
                    "order_id": item.order_id,
                    "reservation_id": item.reservation_id,
                }
                for item in state.reservations
            ),
            key=_canonical_json,
        ),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _metric_definition(result: Any) -> Tuple[Tuple[str, str], ...]:
    contract = getattr(result, "result_contract", None)
    assumptions = getattr(contract, "assumptions", ())
    values = []
    for item in assumptions:
        key = getattr(item, "key", None)
        value = getattr(item, "value", None)
        if isinstance(key, str) and _is_metric_definition_key(key):
            if type(value) is not str:
                raise TypeError("metric assumption values must be strings")
            values.append((key, value))
    return tuple(sorted(values))


def _is_metric_definition_key(key: str) -> bool:
    return bool(
        key.startswith("metrics.")
        or key in {"cash.trade_count_unit", "cash.win_rate_unit"}
    )


def _canonical_trade_sort_key(row: dict, fields: Tuple[str, ...]):
    selected = {field: _primitive(row[field], f"trade.{field}") for field in fields}
    # Date/time is primary.  The remaining semantic execution fields break
    # ties without relying on process-local order_id/fill_id values or input
    # list order.  Identical duplicate fills are intentionally interchangeable.
    semantic_order = (
        "simulated_time",
        "time",
        "symbol",
        "direction",
        "price",
        "shares",
        "amount",
        "note",
    )
    prefix = tuple(
        _canonical_json(selected.get(field)) for field in semantic_order
    )
    return prefix + (_canonical_json(selected),)


def capture_cash_run(
    result: Any,
    *,
    label: str,
    policy: Optional[RunParityPolicy] = None,
) -> CashRunParitySnapshot:
    """Capture a result without mutating, sorting, or intersecting its data."""

    selected = RunParityPolicy() if policy is None else policy
    if type(selected) is not RunParityPolicy:
        raise TypeError("policy must be exactly RunParityPolicy or None")
    result, source_kind = _prepare_source(result)
    (
        input_artifact_verified,
        input_rankable,
        input_run_id,
        input_manifest_sha256,
    ) = _source_identity(result, source_kind)
    result = _unwrap_cash_result(result)
    frame = result.daily_values
    if type(frame) is not pd.DataFrame:
        raise TypeError("result.daily_values must be exactly pandas.DataFrame")
    missing_daily = tuple(
        field for field in selected.daily_fields if field not in frame.columns
    )
    if missing_daily:
        raise ValueError(
            f"result.daily_values is missing parity fields: {missing_daily}"
        )

    if "date" in frame.columns:
        raw_observations = tuple(frame["date"].tolist())
        if isinstance(frame.index, pd.DatetimeIndex):
            index_observations = tuple(
                _observation_id(value, f"daily_values.index[{index}]")
                for index, value in enumerate(frame.index)
            )
            column_observations = tuple(
                _observation_id(value, f"daily_values.date[{index}]")
                for index, value in enumerate(raw_observations)
            )
            if index_observations != column_observations:
                raise ValueError("daily_values date column and index disagree")
    elif isinstance(frame.index, pd.DatetimeIndex):
        if frame.index.tz is not None:
            raise ValueError("daily_values index must be timezone-naive")
        raw_observations = tuple(frame.index)
    else:
        raise ValueError(
            "daily_values must have a date column or a DatetimeIndex"
        )

    observations = tuple(
        _observation_id(value, f"daily_values observation[{index}]")
        for index, value in enumerate(raw_observations)
    )
    daily_rows = tuple(
        tuple(frame.iloc[row_index][field] for field in selected.daily_fields)
        for row_index in range(len(frame))
    )

    trades = result.trades
    if not isinstance(trades, (list, tuple)):
        raise TypeError("result.trades must be a list or tuple")
    normalized_trades = []
    for row_index, row in enumerate(trades):
        if not isinstance(row, dict):
            raise TypeError(f"result.trades[{row_index}] must be a dict")
        missing_trade = tuple(
            field for field in selected.trade_fields if field not in row
        )
        if missing_trade:
            raise ValueError(
                f"result.trades[{row_index}] is missing parity fields: "
                f"{missing_trade}"
            )
        normalized_trades.append(
            {
                field: _parity_primitive(
                    row[field], field, f"result.trades[{row_index}].{field}"
                )
                for field in selected.trade_fields
            }
        )
    if selected.complete_ledger:
        event_sequences = [row["event_sequence"] for row in normalized_trades]
        if event_sequences != sorted(event_sequences) or len(
            set(event_sequences)
        ) != len(event_sequences):
            raise ValueError(
                "cash_full_v2 trades must preserve strictly increasing "
                "event_sequence order"
            )
    else:
        normalized_trades.sort(
            key=lambda row: _canonical_trade_sort_key(row, selected.trade_fields)
        )
    trade_rows = [
        tuple(row[field] for field in selected.trade_fields)
        for row in normalized_trades
    ]

    missing_metrics = tuple(
        field for field in selected.metric_fields if not hasattr(result, field)
    )
    if missing_metrics:
        raise ValueError(f"result is missing parity metrics: {missing_metrics}")
    metric_values = tuple(
        getattr(result, field) for field in selected.metric_fields
    )
    snapshot = CashRunParitySnapshot(
        label=label,
        observation_ids=observations,
        daily_fields=selected.daily_fields,
        daily_rows=daily_rows,
        trade_fields=selected.trade_fields,
        trade_rows=tuple(trade_rows),
        metric_fields=selected.metric_fields,
        metric_values=metric_values,
        metric_definition=_metric_definition(result),
        economic_origin_sha256=(
            _economic_origin_sha256(result) if selected.complete_ledger else None
        ),
        event_journal_sha256=(
            _event_journal_sha256(result) if selected.complete_ledger else None
        ),
        terminal_state_sha256=(
            _terminal_state_sha256(result) if selected.complete_ledger else None
        ),
        input_artifact_verified=input_artifact_verified,
        input_rankable=input_rankable,
        input_run_id=input_run_id,
        input_manifest_sha256=input_manifest_sha256,
    )
    object.__setattr__(snapshot, "_source_kind", source_kind)
    return snapshot


@dataclass(frozen=True)
class ParityDifference:
    key: str
    baseline: _Primitive
    candidate: _Primitive
    absolute_difference: Optional[float]
    allowed_difference: Optional[float]

    def __post_init__(self) -> None:
        _text(self.key, "difference key")
        object.__setattr__(self, "baseline", _primitive(self.baseline, "baseline"))
        object.__setattr__(self, "candidate", _primitive(self.candidate, "candidate"))
        for name in ("absolute_difference", "allowed_difference"):
            value = getattr(self, name)
            if value is not None:
                number = float(value)
                if not math.isfinite(number) or number < 0:
                    raise ValueError(f"{name} must be finite and non-negative")
                object.__setattr__(self, name, number)

    def to_dict(self) -> dict:
        return {
            "absolute_difference": self.absolute_difference,
            "allowed_difference": self.allowed_difference,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "key": self.key,
        }


@dataclass(frozen=True)
class ParityFieldSummary:
    field: str
    compared_values: int
    different_values: int
    outside_tolerance_values: int
    max_absolute_difference: Optional[float]
    first_difference: Optional[ParityDifference]
    first_outside_tolerance: Optional[ParityDifference]

    def __post_init__(self) -> None:
        if type(self.field) is not str or not _FIELD_RE.fullmatch(self.field):
            raise ValueError("parity summary field must be lower snake-case")
        for name in (
            "compared_values",
            "different_values",
            "outside_tolerance_values",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        if self.different_values > self.compared_values:
            raise ValueError("different_values cannot exceed compared_values")
        if self.outside_tolerance_values > self.different_values:
            raise ValueError(
                "outside_tolerance_values cannot exceed different_values"
            )
        if self.max_absolute_difference is not None:
            value = float(self.max_absolute_difference)
            if not math.isfinite(value) or value < 0:
                raise ValueError(
                    "max_absolute_difference must be finite and non-negative"
                )
            object.__setattr__(self, "max_absolute_difference", value)
        if self.first_difference is not None and type(
            self.first_difference
        ) is not ParityDifference:
            raise TypeError("first_difference must be ParityDifference or None")
        if self.first_outside_tolerance is not None and type(
            self.first_outside_tolerance
        ) is not ParityDifference:
            raise TypeError(
                "first_outside_tolerance must be ParityDifference or None"
            )
        if (self.first_difference is None) != (self.different_values == 0):
            raise ValueError("first_difference must agree with different_values")
        if (self.first_outside_tolerance is None) != (
            self.outside_tolerance_values == 0
        ):
            raise ValueError(
                "first_outside_tolerance must agree with outside_tolerance_values"
            )

    @property
    def exact(self) -> bool:
        return self.different_values == 0

    @property
    def within_tolerance(self) -> bool:
        return self.outside_tolerance_values == 0

    def to_dict(self) -> dict:
        return {
            "compared_values": self.compared_values,
            "different_values": self.different_values,
            "exact": self.exact,
            "field": self.field,
            "first_difference": (
                None
                if self.first_difference is None
                else self.first_difference.to_dict()
            ),
            "first_outside_tolerance": (
                None
                if self.first_outside_tolerance is None
                else self.first_outside_tolerance.to_dict()
            ),
            "max_absolute_difference": self.max_absolute_difference,
            "outside_tolerance_values": self.outside_tolerance_values,
            "within_tolerance": self.within_tolerance,
        }


@dataclass(frozen=True)
class RunParityIdentity:
    label: str
    snapshot_sha256: str
    observation_count: int
    trade_count: int
    economic_origin_sha256: Optional[str]
    event_journal_sha256: Optional[str]
    terminal_state_sha256: Optional[str]
    source_kind: str
    artifact_verified: Optional[bool]
    rankable: Optional[bool]
    run_id: Optional[str]
    manifest_sha256: Optional[str]

    def __post_init__(self) -> None:
        _text(self.label, "run parity identity label")
        if not re.fullmatch(r"[0-9a-f]{64}", self.snapshot_sha256):
            raise ValueError("snapshot_sha256 must be lowercase SHA-256")
        for name in ("observation_count", "trade_count"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative int")
        for name in (
            "economic_origin_sha256",
            "event_journal_sha256",
            "terminal_state_sha256",
            "manifest_sha256",
        ):
            value = getattr(self, name)
            if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError(f"{name} must be lowercase SHA-256 or null")
        if self.source_kind not in {"RAW", "LEGACY", "VERIFIED_ARTIFACT"}:
            raise ValueError("unsupported run parity source_kind")
        for name in ("artifact_verified", "rankable"):
            value = getattr(self, name)
            if value is not None and type(value) is not bool:
                raise TypeError(f"{name} must be bool or null")
        if self.source_kind == "RAW":
            if self.artifact_verified is not None:
                raise ValueError("raw parity identity cannot claim verification")
            if any(
                value is not None
                for value in (self.rankable, self.run_id, self.manifest_sha256)
            ):
                raise ValueError("raw parity identity cannot claim artifact evidence")
        else:
            if self.rankable is None or self.run_id is None:
                raise ValueError(
                    "artifact-backed parity identity requires rankable and run_id"
                )
            _text(self.run_id, "run parity identity run_id")
            if self.artifact_verified and self.manifest_sha256 is None:
                raise ValueError("verified parity identity requires manifest_sha256")
            if self.source_kind == "LEGACY" and self.artifact_verified is not False:
                raise ValueError("legacy parity identity must remain unverified")
            if (
                self.source_kind == "VERIFIED_ARTIFACT"
                and self.artifact_verified is not True
            ):
                raise ValueError("verified artifact identity must be verified")

    def to_dict(self) -> dict:
        return {
            "artifact_verified": self.artifact_verified,
            "economic_origin_sha256": self.economic_origin_sha256,
            "event_journal_sha256": self.event_journal_sha256,
            "label": self.label,
            "manifest_sha256": self.manifest_sha256,
            "observation_count": self.observation_count,
            "rankable": self.rankable,
            "run_id": self.run_id,
            "snapshot_sha256": self.snapshot_sha256,
            "source_kind": self.source_kind,
            "terminal_state_sha256": self.terminal_state_sha256,
            "trade_count": self.trade_count,
        }


@dataclass(frozen=True)
class RunParityReport:
    baseline: RunParityIdentity
    candidate: RunParityIdentity
    policy: RunParityPolicy
    comparable: bool
    reasons: Tuple[str, ...]
    scope_equal: bool
    baseline_only_observations: Tuple[str, ...]
    candidate_only_observations: Tuple[str, ...]
    metric_summaries: Tuple[ParityFieldSummary, ...]
    daily_summaries: Tuple[ParityFieldSummary, ...]
    trade_summaries: Tuple[ParityFieldSummary, ...]
    trade_count_equal: bool
    baseline_metric_definition: Tuple[Tuple[str, str], ...]
    candidate_metric_definition: Tuple[Tuple[str, str], ...]
    schema_version: int = RUN_PARITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.baseline) is not RunParityIdentity:
            raise TypeError("baseline must be exactly RunParityIdentity")
        if type(self.candidate) is not RunParityIdentity:
            raise TypeError("candidate must be exactly RunParityIdentity")
        if type(self.policy) is not RunParityPolicy:
            raise TypeError("policy must be exactly RunParityPolicy")
        for name in ("comparable", "scope_equal", "trade_count_equal"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be exactly bool")
        if type(self.reasons) is not tuple or any(
            type(value) is not str or not value for value in self.reasons
        ):
            raise TypeError("reasons must be a tuple of non-empty strings")
        if self.comparable == bool(self.reasons):
            raise ValueError("comparable must be true exactly when reasons is empty")
        for name in (
            "baseline_only_observations",
            "candidate_only_observations",
        ):
            values = getattr(self, name)
            if type(values) is not tuple:
                raise TypeError(f"{name} must be exactly tuple")
            for index, value in enumerate(values):
                _observation_id(value, f"{name}[{index}]")
        for name in (
            "metric_summaries",
            "daily_summaries",
            "trade_summaries",
        ):
            values = getattr(self, name)
            if type(values) is not tuple or any(
                type(value) is not ParityFieldSummary for value in values
            ):
                raise TypeError(f"{name} must contain ParityFieldSummary values")
        if self.comparable:
            if tuple(value.field for value in self.metric_summaries) != (
                self.policy.metric_fields
            ):
                raise ValueError("metric summaries must match policy field order")
            if tuple(value.field for value in self.daily_summaries) != (
                self.policy.daily_fields
            ):
                raise ValueError("daily summaries must match policy field order")
            if tuple(value.field for value in self.trade_summaries) != (
                self.policy.trade_fields
            ):
                raise ValueError("trade summaries must match policy field order")
        elif self.metric_summaries or self.daily_summaries or self.trade_summaries:
            raise ValueError("a non-comparable report must not contain comparisons")
        for name in (
            "baseline_metric_definition",
            "candidate_metric_definition",
        ):
            values = getattr(self, name)
            if type(values) is not tuple:
                raise TypeError(f"{name} must be exactly tuple")
            keys = []
            for item in values:
                if type(item) is not tuple or len(item) != 2:
                    raise TypeError(f"{name} entries must be (key, value) tuples")
                key = _text(item[0], f"{name} key")
                _text(item[1], f"{name} value")
                if not _is_metric_definition_key(key):
                    raise ValueError(
                        f"{name} keys must describe a supported metric or "
                        "cash aggregation unit"
                    )
                keys.append(key)
            if keys != sorted(keys) or len(keys) != len(set(keys)):
                raise ValueError(f"{name} must have sorted unique keys")
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be exactly int")
        if self.schema_version != RUN_PARITY_SCHEMA_VERSION:
            raise ValueError("unsupported run parity report schema_version")

    @property
    def projection_status(self) -> RunParityStatus:
        ledger = self.ledger_status
        metrics = self.metric_definition_status
        if ledger is RunParityStatus.NOT_COMPARABLE:
            return RunParityStatus.NOT_COMPARABLE
        if ledger is RunParityStatus.DIFFERENT:
            return RunParityStatus.DIFFERENT
        if metrics is RunParityStatus.NOT_COMPARABLE:
            return RunParityStatus.NOT_COMPARABLE
        if metrics is RunParityStatus.DIFFERENT:
            return RunParityStatus.DIFFERENT
        if (
            ledger is RunParityStatus.EXACT
            and metrics is RunParityStatus.EXACT
        ):
            return RunParityStatus.EXACT
        return RunParityStatus.WITHIN_TOLERANCE

    @property
    def assurance_status(self) -> str:
        # A portable Python object/JSON document is not a signature or an
        # authentication boundary.  The CLI may record that it reverified two
        # roots during one command, but this core report remains diagnostic.
        return "PORTABLE_DIAGNOSTIC_ONLY"

    @property
    def status(self) -> RunParityStatus:
        return RunParityStatus.UNATTESTED

    @property
    def ledger_metric_summaries(self) -> Tuple[ParityFieldSummary, ...]:
        return tuple(
            value
            for value in self.metric_summaries
            if value.field in LEDGER_PARITY_METRIC_FIELDS
        )

    @property
    def derived_metric_summaries(self) -> Tuple[ParityFieldSummary, ...]:
        return tuple(
            value
            for value in self.metric_summaries
            if value.field not in LEDGER_PARITY_METRIC_FIELDS
        )

    @property
    def metric_definitions_available(self) -> bool:
        required = set(REQUIRED_PARITY_METRIC_DEFINITION_KEYS)
        baseline_keys = {key for key, _ in self.baseline_metric_definition}
        candidate_keys = {key for key, _ in self.candidate_metric_definition}
        return bool(
            required.issubset(baseline_keys)
            and required.issubset(candidate_keys)
        )

    @property
    def metric_definitions_equal(self) -> bool:
        return bool(
            self.metric_definitions_available
            and self.baseline_metric_definition
            == self.candidate_metric_definition
        )

    @property
    def ledger_exact(self) -> bool:
        summaries = (
            self.ledger_metric_summaries
            + self.daily_summaries
            + self.trade_summaries
        )
        return bool(
            self.comparable
            and self.scope_equal
            and self.policy.complete_ledger
            and self.economic_origins_equal
            and self.event_journals_equal
            and self.terminal_states_equal
            and self.trade_count_equal
            and all(summary.exact for summary in summaries)
        )

    @property
    def ledger_within_tolerance(self) -> bool:
        summaries = (
            self.ledger_metric_summaries
            + self.daily_summaries
            + self.trade_summaries
        )
        return bool(
            self.comparable
            and self.scope_equal
            and self.policy.complete_ledger
            and self.economic_origins_equal
            and self.event_journals_equal
            and self.terminal_states_equal
            and self.trade_count_equal
            and all(summary.within_tolerance for summary in summaries)
        )

    @property
    def ledger_status(self) -> RunParityStatus:
        if not self.comparable:
            return RunParityStatus.NOT_COMPARABLE
        if not self.policy.complete_ledger:
            return RunParityStatus.NOT_COMPARABLE
        if not self.economic_origins_available:
            return RunParityStatus.NOT_COMPARABLE
        if not self.economic_origins_equal:
            return RunParityStatus.DIFFERENT
        if not self.event_journals_available:
            return RunParityStatus.NOT_COMPARABLE
        if not self.event_journals_equal:
            return RunParityStatus.DIFFERENT
        if not self.terminal_states_available:
            return RunParityStatus.NOT_COMPARABLE
        if not self.terminal_states_equal:
            return RunParityStatus.DIFFERENT
        if self.ledger_exact:
            return RunParityStatus.EXACT
        if self.ledger_within_tolerance:
            return RunParityStatus.WITHIN_TOLERANCE
        return RunParityStatus.DIFFERENT

    @property
    def post_seed_projection_status(self) -> RunParityStatus:
        if not self.comparable:
            return RunParityStatus.NOT_COMPARABLE
        summaries = (
            self.ledger_metric_summaries
            + self.daily_summaries
            + self.trade_summaries
        )
        if self.scope_equal and self.trade_count_equal and all(
            value.exact for value in summaries
        ):
            return RunParityStatus.EXACT
        if self.scope_equal and self.trade_count_equal and all(
            value.within_tolerance for value in summaries
        ):
            return RunParityStatus.WITHIN_TOLERANCE
        return RunParityStatus.DIFFERENT

    @property
    def economic_origins_available(self) -> bool:
        return bool(
            self.baseline.economic_origin_sha256
            and self.candidate.economic_origin_sha256
        )

    @property
    def economic_origins_equal(self) -> bool:
        return bool(
            self.economic_origins_available
            and self.baseline.economic_origin_sha256
            == self.candidate.economic_origin_sha256
        )

    @property
    def terminal_states_available(self) -> bool:
        return bool(
            self.baseline.terminal_state_sha256
            and self.candidate.terminal_state_sha256
        )

    @property
    def event_journals_available(self) -> bool:
        return bool(
            self.baseline.event_journal_sha256
            and self.candidate.event_journal_sha256
        )

    @property
    def event_journals_equal(self) -> bool:
        return bool(
            self.event_journals_available
            and self.baseline.event_journal_sha256
            == self.candidate.event_journal_sha256
        )

    @property
    def terminal_states_equal(self) -> bool:
        return bool(
            self.terminal_states_available
            and self.baseline.terminal_state_sha256
            == self.candidate.terminal_state_sha256
        )

    @property
    def metric_definition_status(self) -> RunParityStatus:
        if not self.comparable or not self.metric_definitions_available:
            return RunParityStatus.NOT_COMPARABLE
        if not self.metric_definitions_equal:
            return RunParityStatus.DIFFERENT
        if all(value.exact for value in self.derived_metric_summaries):
            return RunParityStatus.EXACT
        if all(
            value.within_tolerance for value in self.derived_metric_summaries
        ):
            return RunParityStatus.WITHIN_TOLERANCE
        return RunParityStatus.DIFFERENT

    @property
    def exact(self) -> bool:
        return self.status is RunParityStatus.EXACT

    @property
    def within_tolerance(self) -> bool:
        return self.status in (
            RunParityStatus.EXACT,
            RunParityStatus.WITHIN_TOLERANCE,
        )

    @property
    def projection_exact(self) -> bool:
        return self.projection_status is RunParityStatus.EXACT

    @property
    def projection_within_tolerance(self) -> bool:
        return self.projection_status in (
            RunParityStatus.EXACT,
            RunParityStatus.WITHIN_TOLERANCE,
        )

    def _payload(self) -> dict:
        return {
            "baseline": self.baseline.to_dict(),
            "baseline_only_observations": list(self.baseline_only_observations),
            "candidate": self.candidate.to_dict(),
            "candidate_only_observations": list(self.candidate_only_observations),
            "comparable": self.comparable,
            "exact": self.exact,
            "assurance": {
                "certified": False,
                "status": self.assurance_status,
            },
            "ledger": {
                "complete": self.policy.complete_ledger,
                "daily": [value.to_dict() for value in self.daily_summaries],
                "exact": self.ledger_exact,
                "economic_origin": {
                    "available": self.economic_origins_available,
                    "baseline_sha256": self.baseline.economic_origin_sha256,
                    "candidate_sha256": self.candidate.economic_origin_sha256,
                    "equal": self.economic_origins_equal,
                },
                "event_journal": {
                    "available": self.event_journals_available,
                    "baseline_sha256": self.baseline.event_journal_sha256,
                    "candidate_sha256": self.candidate.event_journal_sha256,
                    "equal": self.event_journals_equal,
                },
                "terminal_state": {
                    "available": self.terminal_states_available,
                    "baseline_sha256": self.baseline.terminal_state_sha256,
                    "candidate_sha256": self.candidate.terminal_state_sha256,
                    "equal": self.terminal_states_equal,
                },
                "metrics": [
                    value.to_dict() for value in self.ledger_metric_summaries
                ],
                "status": self.ledger_status.value,
                "post_seed_projection_status": (
                    self.post_seed_projection_status.value
                ),
                "omitted_required_trade_fields": [
                    field
                    for field in DEFAULT_PARITY_TRADE_FIELDS
                    if field not in self.policy.trade_fields
                ],
                "trade_count_equal": self.trade_count_equal,
                "trades": [value.to_dict() for value in self.trade_summaries],
                "within_tolerance": self.ledger_within_tolerance,
            },
            "metric_definitions": {
                "available": self.metric_definitions_available,
                "baseline": [
                    list(value) for value in self.baseline_metric_definition
                ],
                "candidate": [
                    list(value) for value in self.candidate_metric_definition
                ],
                "equal": self.metric_definitions_equal,
                "metrics": [
                    value.to_dict() for value in self.derived_metric_summaries
                ],
                "status": self.metric_definition_status.value,
            },
            "policy": self.policy.to_dict(),
            "reasons": list(self.reasons),
            "schema": RUN_PARITY_REPORT_SCHEMA,
            "schema_version": self.schema_version,
            "scope_equal": self.scope_equal,
            "projection_status": self.projection_status.value,
            "projection_exact": self.projection_exact,
            "projection_within_tolerance": self.projection_within_tolerance,
            "status": self.status.value,
            "trade_count_equal": self.trade_count_equal,
            "within_tolerance": self.within_tolerance,
        }

    @property
    def report_sha256(self) -> str:
        return hashlib.sha256(
            _canonical_json(self._payload()).encode("utf-8")
        ).hexdigest()

    def to_dict(self) -> dict:
        payload = self._payload()
        payload["report_sha256"] = self.report_sha256
        return payload

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"

    def to_markdown(self) -> str:
        def identity_rows(prefix: str, value: RunParityIdentity) -> list:
            return [
                f"- {prefix} label: `{value.label}`",
                f"- {prefix} source kind: `{value.source_kind}`",
                f"- {prefix} run ID: `{value.run_id}`",
                f"- {prefix} manifest SHA-256: `{value.manifest_sha256}`",
                f"- {prefix} artifact verified: `{value.artifact_verified}`",
                f"- {prefix} rankable: `{value.rankable}`",
                f"- {prefix} snapshot SHA-256: `{value.snapshot_sha256}`",
                f"- {prefix} origin SHA-256: `{value.economic_origin_sha256}`",
                f"- {prefix} event journal SHA-256: `{value.event_journal_sha256}`",
                f"- {prefix} terminal SHA-256: `{value.terminal_state_sha256}`",
            ]

        rows = [
            "# Run parity report",
            "",
            f"- Status: `{self.status.value}`",
            f"- Assurance status: `{self.assurance_status}`",
            f"- Projection status: `{self.projection_status.value}`",
            f"- Scope equal: `{str(self.scope_equal).lower()}`",
            f"- Ledger status: `{self.ledger_status.value}`",
            f"- Post-seed projection: `{self.post_seed_projection_status.value}`",
            f"- Economic origin available: `{str(self.economic_origins_available).lower()}`",
            f"- Economic origin equal: `{str(self.economic_origins_equal).lower()}`",
            f"- Metric-definition status: `{self.metric_definition_status.value}`",
            f"- Trade count equal: `{str(self.trade_count_equal).lower()}`",
            f"- Exact: `{str(self.exact).lower()}`",
            f"- Within tolerance: `{str(self.within_tolerance).lower()}`",
            f"- Report SHA-256: `{self.report_sha256}`",
        ]
        rows.extend(identity_rows("Baseline", self.baseline))
        rows.extend(identity_rows("Candidate", self.candidate))
        if self.reasons:
            rows.extend(("", "## Comparability", ""))
            rows.extend(f"- {reason}" for reason in self.reasons)
        summaries = (
            (("ledger metric", value) for value in self.ledger_metric_summaries),
            (("daily", value) for value in self.daily_summaries),
            (("trade", value) for value in self.trade_summaries),
            (("derived metric", value) for value in self.derived_metric_summaries),
        )
        flattened = [item for group in summaries for item in group]
        if flattened:
            rows.extend(
                (
                    "",
                    "## Field comparison",
                    "",
                    "| Dimension | Field | Different | Outside tolerance | Max abs | First outside |",
                    "|---|---|---:|---:|---:|---|",
                )
            )
            for dimension, summary in flattened:
                first = summary.first_outside_tolerance
                first_text = "" if first is None else first.key.replace("|", "\\|")
                max_abs = (
                    ""
                    if summary.max_absolute_difference is None
                    else format(summary.max_absolute_difference, ".17g")
                )
                rows.append(
                    f"| {dimension} | {summary.field} | "
                    f"{summary.different_values} | "
                    f"{summary.outside_tolerance_values} | {max_abs} | "
                    f"{first_text} |"
                )
        return "\n".join(rows) + "\n"

    def write(self, path, *, overwrite: bool = False) -> Path:
        """Write JSON or Markdown by suffix, refusing accidental overwrite."""

        if type(overwrite) is not bool:
            raise TypeError("overwrite must be exactly bool")
        target = Path(path).expanduser().resolve()
        if target.suffix.lower() not in {".json", ".md"}:
            raise ValueError("run parity report path must end in .json or .md")
        if target.exists() and not overwrite:
            raise FileExistsError(f"run parity report already exists: {target}")
        text = self.to_markdown() if target.suffix.lower() == ".md" else self.to_json()
        target.write_text(text, encoding="utf-8")
        return target


def _identity(snapshot: CashRunParitySnapshot) -> RunParityIdentity:
    return RunParityIdentity(
        label=snapshot.label,
        snapshot_sha256=snapshot.snapshot_sha256,
        observation_count=len(snapshot.observation_ids),
        trade_count=len(snapshot.trade_rows),
        economic_origin_sha256=snapshot.economic_origin_sha256,
        event_journal_sha256=snapshot.event_journal_sha256,
        terminal_state_sha256=snapshot.terminal_state_sha256,
        source_kind=snapshot._source_kind,
        artifact_verified=snapshot.input_artifact_verified,
        rankable=snapshot.input_rankable,
        run_id=snapshot.input_run_id,
        manifest_sha256=snapshot.input_manifest_sha256,
    )


def _difference(
    baseline: _Primitive,
    candidate: _Primitive,
    *,
    key: str,
    policy: RunParityPolicy,
    tolerant: bool,
):
    exact = type(baseline) is type(candidate) and baseline == candidate
    absolute_difference = None
    allowed_difference = None
    within = exact
    if type(baseline) is int and type(candidate) is int:
        integer_difference = abs(baseline - candidate)
        try:
            absolute_difference = float(integer_difference)
        except OverflowError:
            absolute_difference = None
        allowed_difference = 0.0
        within = integer_difference == 0
    elif tolerant and type(baseline) is float and type(candidate) is float:
        try:
            absolute_difference = abs(float(baseline) - float(candidate))
        except OverflowError:
            absolute_difference = math.inf
        if math.isfinite(absolute_difference):
            allowed_difference = max(
                policy.absolute_tolerance,
                policy.relative_tolerance
                * max(abs(float(baseline)), abs(float(candidate))),
            )
            within = absolute_difference <= allowed_difference
        else:
            # Inputs are finite, but their subtraction can still overflow.
            absolute_difference = None
            allowed_difference = 0.0
            within = False
    elif {type(baseline), type(candidate)} == {int, float}:
        # JSON integers carry exact count/quantity semantics.  Never let a
        # lossy binary-float conversion certify a mixed representation.
        try:
            difference = abs(float(baseline) - float(candidate))
            absolute_difference = difference if math.isfinite(difference) else None
        except OverflowError:
            absolute_difference = None
        allowed_difference = 0.0
        within = False
    return (
        exact,
        within,
        ParityDifference(
            key=key,
            baseline=baseline,
            candidate=candidate,
            absolute_difference=absolute_difference,
            allowed_difference=allowed_difference,
        ),
    )


def _summarize_field(
    field: str,
    keys: Sequence[str],
    baseline_values: Sequence[_Primitive],
    candidate_values: Sequence[_Primitive],
    policy: RunParityPolicy,
) -> ParityFieldSummary:
    if not (len(keys) == len(baseline_values) == len(candidate_values)):
        raise ValueError("field comparison inputs must have equal lengths")
    different = 0
    outside = 0
    max_absolute = None
    first_difference = None
    first_outside = None
    for key, baseline, candidate in zip(
        keys, baseline_values, candidate_values
    ):
        exact, within, difference = _difference(
            baseline,
            candidate,
            key=key,
            policy=policy,
            tolerant=field in _TOLERANT_FLOAT_FIELDS,
        )
        if difference.absolute_difference is not None:
            max_absolute = max(
                0.0 if max_absolute is None else max_absolute,
                difference.absolute_difference,
            )
        if not exact:
            different += 1
            if first_difference is None:
                first_difference = difference
        if not within:
            outside += 1
            if first_outside is None:
                first_outside = difference
    return ParityFieldSummary(
        field=field,
        compared_values=len(keys),
        different_values=different,
        outside_tolerance_values=outside,
        max_absolute_difference=max_absolute,
        first_difference=first_difference,
        first_outside_tolerance=first_outside,
    )


def compare_cash_run_snapshots(
    baseline: CashRunParitySnapshot,
    candidate: CashRunParitySnapshot,
    *,
    policy: Optional[RunParityPolicy] = None,
) -> RunParityReport:
    """Compare two captured runs without changing their scope or ordering."""

    if type(baseline) is not CashRunParitySnapshot:
        raise TypeError("baseline must be exactly CashRunParitySnapshot")
    if type(candidate) is not CashRunParitySnapshot:
        raise TypeError("candidate must be exactly CashRunParitySnapshot")
    selected = RunParityPolicy() if policy is None else policy
    if type(selected) is not RunParityPolicy:
        raise TypeError("policy must be exactly RunParityPolicy or None")

    reasons = []
    for name in ("daily_fields", "trade_fields", "metric_fields"):
        expected = getattr(selected, name)
        if getattr(baseline, name) != expected:
            reasons.append(f"baseline {name} does not match the report policy")
        if getattr(candidate, name) != expected:
            reasons.append(f"candidate {name} does not match the report policy")

    scope_equal = baseline.observation_ids == candidate.observation_ids
    if not scope_equal:
        reasons.append(
            "observation scopes differ; dates were not intersected or filled"
        )
    comparable = not reasons
    baseline_set = set(baseline.observation_ids)
    candidate_set = set(candidate.observation_ids)
    baseline_only = tuple(
        value for value in baseline.observation_ids if value not in candidate_set
    )
    candidate_only = tuple(
        value for value in candidate.observation_ids if value not in baseline_set
    )
    metric_summaries = []
    daily_summaries = []
    trade_summaries = []
    if comparable:
        for column_index, field in enumerate(selected.metric_fields):
            metric_summaries.append(
                _summarize_field(
                    field,
                    ("summary",),
                    (baseline.metric_values[column_index],),
                    (candidate.metric_values[column_index],),
                    selected,
                )
            )
        for column_index, field in enumerate(selected.daily_fields):
            daily_summaries.append(
                _summarize_field(
                    field,
                    baseline.observation_ids,
                    tuple(row[column_index] for row in baseline.daily_rows),
                    tuple(row[column_index] for row in candidate.daily_rows),
                    selected,
                )
            )
        shared_trade_count = min(
            len(baseline.trade_rows), len(candidate.trade_rows)
        )
        coordinate_fields = (
            ("event_sequence", "simulated_time", "symbol", "direction")
            if selected.complete_ledger
            else ("time", "symbol", "direction")
        )
        coordinate_indexes = (
            tuple(selected.trade_fields.index(field) for field in coordinate_fields)
            if all(field in selected.trade_fields for field in coordinate_fields)
            else None
        )
        trade_keys = []
        occurrences = {}
        for index in range(shared_trade_count):
            baseline_row = baseline.trade_rows[index]
            candidate_row = candidate.trade_rows[index]
            baseline_coordinate = (
                tuple(baseline_row[item] for item in coordinate_indexes)
                if coordinate_indexes is not None
                else None
            )
            candidate_coordinate = (
                tuple(candidate_row[item] for item in coordinate_indexes)
                if coordinate_indexes is not None
                else None
            )
            if (
                baseline_coordinate is not None
                and baseline_coordinate == candidate_coordinate
            ):
                occurrences[baseline_coordinate] = (
                    occurrences.get(baseline_coordinate, 0) + 1
                )
                coordinate_text = "|".join(
                    str(value) for value in baseline_coordinate
                )
                trade_keys.append(
                    f"{coordinate_text}|{occurrences[baseline_coordinate]}"
                )
            else:
                trade_keys.append(f"trade[{index}]")
        trade_keys = tuple(trade_keys)
        for column_index, field in enumerate(selected.trade_fields):
            trade_summaries.append(
                _summarize_field(
                    field,
                    trade_keys,
                    tuple(
                        row[column_index]
                        for row in baseline.trade_rows[:shared_trade_count]
                    ),
                    tuple(
                        row[column_index]
                        for row in candidate.trade_rows[:shared_trade_count]
                    ),
                    selected,
                )
            )

    trade_count_equal = len(baseline.trade_rows) == len(candidate.trade_rows)
    return RunParityReport(
        baseline=_identity(baseline),
        candidate=_identity(candidate),
        policy=selected,
        comparable=comparable,
        reasons=tuple(reasons),
        scope_equal=scope_equal,
        baseline_only_observations=baseline_only,
        candidate_only_observations=candidate_only,
        metric_summaries=tuple(metric_summaries),
        daily_summaries=tuple(daily_summaries),
        trade_summaries=tuple(trade_summaries),
        trade_count_equal=trade_count_equal,
        baseline_metric_definition=baseline.metric_definition,
        candidate_metric_definition=candidate.metric_definition,
    )


def compare_cash_runs(
    baseline: Any,
    candidate: Any,
    *,
    baseline_label: str = "baseline",
    candidate_label: str = "candidate",
    policy: Optional[RunParityPolicy] = None,
    atol: Optional[float] = None,
    rtol: Optional[float] = None,
) -> RunParityReport:
    """Capture and compare cash results or verified ``LoadedRun`` objects.

    ``report.ledger_status`` is the economic cash/NAV/execution answer.
    Full ``report.status`` also requires both runs to declare the complete,
    identical metric and cash-aggregation definitions.  Legacy results without
    those assumptions remain metric-definition ``NOT_COMPARABLE`` instead of
    being given a false full parity claim.
    """

    if policy is not None and (atol is not None or rtol is not None):
        raise ValueError("pass either policy or atol/rtol, not both")
    if policy is None:
        selected = RunParityPolicy(
            absolute_tolerance=0.0 if atol is None else atol,
            relative_tolerance=0.0 if rtol is None else rtol,
        )
    else:
        selected = policy
    baseline_snapshot = capture_cash_run(
        baseline, label=baseline_label, policy=selected
    )
    candidate_snapshot = capture_cash_run(
        candidate, label=candidate_label, policy=selected
    )
    return compare_cash_run_snapshots(
        baseline_snapshot, candidate_snapshot, policy=selected
    )


__all__ = [
    "RUN_PARITY_SCHEMA_VERSION",
    "RUN_PARITY_SNAPSHOT_SCHEMA",
    "RUN_PARITY_REPORT_SCHEMA",
    "DEFAULT_PARITY_DAILY_FIELDS",
    "DEFAULT_PARITY_TRADE_FIELDS",
    "DEFAULT_PARITY_METRIC_FIELDS",
    "REQUIRED_PARITY_METRIC_DEFINITION_KEYS",
    "LEDGER_PARITY_METRIC_FIELDS",
    "DERIVED_PARITY_METRIC_FIELDS",
    "RunParityStatus",
    "RunParityPolicy",
    "CashRunParitySnapshot",
    "ParityDifference",
    "ParityFieldSummary",
    "RunParityIdentity",
    "RunParityReport",
    "capture_cash_run",
    "compare_cash_run_snapshots",
    "compare_cash_runs",
]
