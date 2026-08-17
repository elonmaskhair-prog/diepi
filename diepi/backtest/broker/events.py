"""Deterministic execution-event envelopes and an append-only journal.

This module is deliberately a small logging primitive.  It records facts that
an execution system has already decided to emit; it does not update accounts,
provide transaction atomicity, or constitute a double-entry ledger.

Every event is keyed by ``(simulated_time, phase_seq, sequence)``.  Simulated
time is always supplied by the caller.  There is intentionally no wall-clock
fallback and no UUID generation, so identical inputs produce identical
snapshots.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from datetime import date, datetime
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Any, ClassVar, Iterable, Iterator, Optional, Tuple, Union


EXECUTION_EVENT_SCHEMA = "diepi.execution_event"
EXECUTION_EVENT_JOURNAL_SCHEMA = "diepi.execution_event_journal"
EXECUTION_EVENT_SCHEMA_VERSION = 1
_MAX_SAFE_JSON_INTEGER = (1 << 53) - 1


class ExecutionEventType(str, Enum):
    """Stable names for facts emitted by execution and accounting adapters."""

    ORDER_CREATED = "order_created"
    ORDER_ACCEPTED = "order_accepted"
    ORDER_REJECTED = "order_rejected"
    ORDER_CANCELED = "order_canceled"
    ORDER_EXPIRED = "order_expired"
    RESERVE = "reserve"
    RELEASE = "release"
    FILL = "fill"
    SETTLEMENT = "settlement"
    T1_UNLOCK = "t1_unlock"
    CORPORATE_ACTION = "corporate_action"
    MARK = "mark"
    MARGIN = "margin"
    ROLL = "roll"


def _nonempty_text(value: Any, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    normalised = value.strip()
    if not normalised:
        raise ValueError(f"{name} must be non-empty")
    return normalised


def _optional_text(value: Any, name: str) -> Optional[str]:
    if value is None:
        return None
    return _nonempty_text(value, name)


def _integer(value: Any, name: str, *, minimum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        relation = "positive" if minimum == 1 else f">= {minimum}"
        raise ValueError(f"{name} must be {relation}")
    if value > _MAX_SAFE_JSON_INTEGER:
        raise ValueError(f"{name} exceeds the JSON safe-integer range")
    return value


def _finite_number(value: Any, name: str, *, minimum: Optional[float] = None) -> float:
    # Exact built-in numbers keep the serialized representation JSON portable.
    # In particular, bool, Decimal, numpy scalars, and user numeric subclasses
    # are not silently coerced into a different schema value.
    if type(value) not in (int, float):
        raise TypeError(f"{name} must be an int or float")
    if type(value) is int and abs(value) > _MAX_SAFE_JSON_INTEGER:
        raise ValueError(f"{name} exceeds the JSON safe-integer range")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{name} must be finite") from None
    if not isfinite(number):
        raise ValueError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        if minimum == 0:
            raise ValueError(f"{name} must be non-negative")
        raise ValueError(f"{name} must be >= {minimum}")
    # JSON permits -0.0, but different canonicalization schemes disagree on
    # its wire spelling.  Numeric payload fields therefore store one zero.
    return 0.0 if number == 0 else number


def _positive_number(value: Any, name: str) -> float:
    number = _finite_number(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _iso_date(value: Any, name: str) -> str:
    text = _nonempty_text(value, name)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{name} must use YYYY-MM-DD") from None
    if parsed.isoformat() != text:
        raise ValueError(f"{name} must use YYYY-MM-DD")
    return text


def _normalise_json_value(value: Any, path: str) -> Any:
    """Validate and canonicalize the immutable JSON subset used by attributes."""

    if value is None or type(value) in (bool, str):
        return value
    if type(value) is int:
        if abs(value) > _MAX_SAFE_JSON_INTEGER:
            raise ValueError(f"{path} exceeds the JSON safe-integer range")
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError(f"{path} must not contain NaN or infinity")
        return 0.0 if value == 0 else value
    if type(value) is tuple:
        return tuple(
            _normalise_json_value(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if type(value) is EventAttributes:
        return value
    if isinstance(value, (dict, list, set, bytearray)):
        raise TypeError(f"{path} must not contain mutable values")
    raise TypeError(f"{path} contains unsupported value {type(value).__name__}")


def _json_value_to_python(value: Any) -> Any:
    if type(value) is tuple:
        return [_json_value_to_python(item) for item in value]
    if type(value) is EventAttributes:
        return value.to_dict()
    return value


def _unique_json_object(pairs) -> dict:
    """Build one JSON object while rejecting duplicate member names."""

    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _load_canonical_json(value: Any, name: str) -> Any:
    """Decode exactly the canonical JSON spelling produced by this module."""

    if type(value) is not str:
        raise TypeError(f"{name} must be a string")
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is invalid JSON: {exc}") from None
    canonical = json.dumps(
        decoded,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if canonical != value:
        raise ValueError(f"{name} must use canonical JSON encoding")
    return decoded


@dataclass(frozen=True)
class EventAttributes:
    """An immutable, canonically ordered JSON object for optional attributes.

    ``entries`` must be an immutable tuple of ``(str, value)`` tuples.  Nested
    JSON arrays use tuples and nested objects use :class:`EventAttributes`.
    Mutable mappings/lists are intentionally rejected instead of copied.
    """

    entries: Tuple[Tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if type(self.entries) is not tuple:
            raise TypeError("entries must be a tuple")
        normalised = []
        seen = set()
        for index, pair in enumerate(self.entries):
            if type(pair) is not tuple or len(pair) != 2:
                raise TypeError(f"entries[{index}] must be a (key, value) tuple")
            key, value = pair
            if type(key) is not str:
                raise TypeError(f"entries[{index}] key must be a string")
            if not key:
                raise ValueError(f"entries[{index}] key must be non-empty")
            if key in seen:
                raise ValueError(f"duplicate attribute key: {key!r}")
            value = _normalise_json_value(value, f"attributes.{key}")
            seen.add(key)
            normalised.append((key, value))
        object.__setattr__(self, "entries", tuple(sorted(normalised)))

    def to_dict(self) -> dict:
        return {
            key: _json_value_to_python(value)
            for key, value in self.entries
        }

    @classmethod
    def from_dict(cls, value: dict) -> "EventAttributes":
        """Restore the immutable JSON subset without accepting normalization."""

        if type(value) is not dict:
            raise TypeError("EventAttributes payload must be exactly a dict")
        entries = tuple(
            (key, _attribute_value_from_wire(item, f"attributes.{key}"))
            for key, item in value.items()
        )
        restored = cls(entries)
        if restored.to_dict() != value:
            raise ValueError("EventAttributes payload is not canonical")
        return restored


def _attribute_value_from_wire(value: Any, path: str) -> Any:
    """Convert decoded JSON containers into their immutable counterparts."""

    if value is None or type(value) in (bool, str):
        return value
    if type(value) is int:
        if abs(value) > _MAX_SAFE_JSON_INTEGER:
            raise ValueError(f"{path} exceeds the JSON safe-integer range")
        return value
    if type(value) is float:
        if not isfinite(value):
            raise ValueError(f"{path} must not contain NaN or infinity")
        if value == 0 and str(value).startswith("-"):
            raise ValueError(f"{path} must use canonical positive zero")
        return value
    if type(value) is list:
        return tuple(
            _attribute_value_from_wire(item, f"{path}[{index}]")
            for index, item in enumerate(value)
        )
    if type(value) is dict:
        return EventAttributes.from_dict(value)
    raise TypeError(f"{path} contains unsupported value {type(value).__name__}")


class EventPayload:
    """Marker and serializer for the closed set of typed event payloads."""

    __slots__ = ()

    def _validate_attributes(self) -> None:
        if type(self.attributes) is not EventAttributes:
            raise TypeError("attributes must be an EventAttributes instance")

    def to_dict(self) -> dict:
        result = {}
        for item in sorted(fields(self), key=lambda field_info: field_info.name):
            value = getattr(self, item.name)
            value = _normalise_json_value(value, f"payload.{item.name}")
            result[item.name] = _json_value_to_python(value)
        return result


@dataclass(frozen=True)
class OrderEventPayload(EventPayload):
    """Minimal correlation data for an order lifecycle event."""

    order_id: str
    symbol: str
    reason: Optional[str] = None
    attributes: EventAttributes = field(default_factory=EventAttributes)

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", _nonempty_text(self.order_id, "order_id"))
        object.__setattr__(self, "symbol", _nonempty_text(self.symbol, "symbol"))
        object.__setattr__(self, "reason", _optional_text(self.reason, "reason"))
        self._validate_attributes()


@dataclass(frozen=True)
class ReservationPayload(EventPayload):
    """A positive reservation or release of one asset."""

    reservation_id: str
    order_id: str
    asset: str
    amount: float
    attributes: EventAttributes = field(default_factory=EventAttributes)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reservation_id",
            _nonempty_text(self.reservation_id, "reservation_id"),
        )
        object.__setattr__(self, "order_id", _nonempty_text(self.order_id, "order_id"))
        object.__setattr__(self, "asset", _nonempty_text(self.asset, "asset"))
        object.__setattr__(self, "amount", _positive_number(self.amount, "amount"))
        self._validate_attributes()


@dataclass(frozen=True)
class FillPayload(EventPayload):
    """Execution details for one fill; no account mutation is implied."""

    fill_id: str
    order_id: str
    symbol: str
    quantity: int
    price: float
    amount: float
    fee: float = 0.0
    attributes: EventAttributes = field(default_factory=EventAttributes)

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_id", _nonempty_text(self.fill_id, "fill_id"))
        object.__setattr__(self, "order_id", _nonempty_text(self.order_id, "order_id"))
        object.__setattr__(self, "symbol", _nonempty_text(self.symbol, "symbol"))
        object.__setattr__(self, "quantity", _integer(self.quantity, "quantity", minimum=1))
        object.__setattr__(self, "price", _positive_number(self.price, "price"))
        object.__setattr__(self, "amount", _positive_number(self.amount, "amount"))
        object.__setattr__(self, "fee", _finite_number(self.fee, "fee", minimum=0))
        self._validate_attributes()


@dataclass(frozen=True)
class SettlementPayload(EventPayload):
    """A positive debit or credit settled for an account asset."""

    settlement_id: str
    account_id: str
    asset: str
    amount: float
    direction: str
    settlement_date: str
    attributes: EventAttributes = field(default_factory=EventAttributes)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "settlement_id",
            _nonempty_text(self.settlement_id, "settlement_id"),
        )
        object.__setattr__(
            self, "account_id", _nonempty_text(self.account_id, "account_id")
        )
        object.__setattr__(self, "asset", _nonempty_text(self.asset, "asset"))
        object.__setattr__(self, "amount", _positive_number(self.amount, "amount"))
        direction = _nonempty_text(self.direction, "direction").lower()
        if direction not in ("credit", "debit"):
            raise ValueError("direction must be 'credit' or 'debit'")
        object.__setattr__(self, "direction", direction)
        object.__setattr__(
            self,
            "settlement_date",
            _iso_date(self.settlement_date, "settlement_date"),
        )
        self._validate_attributes()


@dataclass(frozen=True)
class T1UnlockPayload(EventPayload):
    """Quantity made sellable on a trading date."""

    account_id: str
    symbol: str
    quantity: int
    trade_date: str
    attributes: EventAttributes = field(default_factory=EventAttributes)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "account_id", _nonempty_text(self.account_id, "account_id")
        )
        object.__setattr__(self, "symbol", _nonempty_text(self.symbol, "symbol"))
        object.__setattr__(self, "quantity", _integer(self.quantity, "quantity", minimum=1))
        object.__setattr__(self, "trade_date", _iso_date(self.trade_date, "trade_date"))
        self._validate_attributes()


@dataclass(frozen=True)
class CorporateActionPayload(EventPayload):
    """Identity and effective date of one externally computed action."""

    action_id: str
    symbol: str
    action_type: str
    effective_date: str
    attributes: EventAttributes = field(default_factory=EventAttributes)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action_id", _nonempty_text(self.action_id, "action_id"))
        object.__setattr__(self, "symbol", _nonempty_text(self.symbol, "symbol"))
        object.__setattr__(
            self, "action_type", _nonempty_text(self.action_type, "action_type")
        )
        object.__setattr__(
            self,
            "effective_date",
            _iso_date(self.effective_date, "effective_date"),
        )
        self._validate_attributes()


@dataclass(frozen=True)
class MarkPayload(EventPayload):
    """A finite positive valuation mark for an instrument."""

    symbol: str
    price: float
    attributes: EventAttributes = field(default_factory=EventAttributes)

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _nonempty_text(self.symbol, "symbol"))
        object.__setattr__(self, "price", _positive_number(self.price, "price"))
        self._validate_attributes()


@dataclass(frozen=True)
class MarginPayload(EventPayload):
    """A positive margin amount classified by a caller-defined kind."""

    account_id: str
    contract: str
    amount: float
    margin_kind: str
    attributes: EventAttributes = field(default_factory=EventAttributes)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "account_id", _nonempty_text(self.account_id, "account_id")
        )
        object.__setattr__(self, "contract", _nonempty_text(self.contract, "contract"))
        object.__setattr__(self, "amount", _positive_number(self.amount, "amount"))
        object.__setattr__(
            self, "margin_kind", _nonempty_text(self.margin_kind, "margin_kind")
        )
        self._validate_attributes()


@dataclass(frozen=True)
class RollPayload(EventPayload):
    """Contract transition metadata without implied fills or cash movement."""

    roll_id: str
    from_contract: str
    to_contract: str
    quantity: int
    attributes: EventAttributes = field(default_factory=EventAttributes)

    def __post_init__(self) -> None:
        object.__setattr__(self, "roll_id", _nonempty_text(self.roll_id, "roll_id"))
        object.__setattr__(
            self,
            "from_contract",
            _nonempty_text(self.from_contract, "from_contract"),
        )
        object.__setattr__(
            self,
            "to_contract",
            _nonempty_text(self.to_contract, "to_contract"),
        )
        if self.from_contract == self.to_contract:
            raise ValueError("from_contract and to_contract must differ")
        object.__setattr__(self, "quantity", _integer(self.quantity, "quantity", minimum=1))
        self._validate_attributes()


ExecutionPayload = Union[
    OrderEventPayload,
    ReservationPayload,
    FillPayload,
    SettlementPayload,
    T1UnlockPayload,
    CorporateActionPayload,
    MarkPayload,
    MarginPayload,
    RollPayload,
]


_ORDER_EVENTS = frozenset(
    {
        ExecutionEventType.ORDER_CREATED,
        ExecutionEventType.ORDER_ACCEPTED,
        ExecutionEventType.ORDER_REJECTED,
        ExecutionEventType.ORDER_CANCELED,
        ExecutionEventType.ORDER_EXPIRED,
    }
)
_PAYLOAD_TYPE_BY_EVENT = MappingProxyType({
    **{event_type: OrderEventPayload for event_type in _ORDER_EVENTS},
    ExecutionEventType.RESERVE: ReservationPayload,
    ExecutionEventType.RELEASE: ReservationPayload,
    ExecutionEventType.FILL: FillPayload,
    ExecutionEventType.SETTLEMENT: SettlementPayload,
    ExecutionEventType.T1_UNLOCK: T1UnlockPayload,
    ExecutionEventType.CORPORATE_ACTION: CorporateActionPayload,
    ExecutionEventType.MARK: MarkPayload,
    ExecutionEventType.MARGIN: MarginPayload,
    ExecutionEventType.ROLL: RollPayload,
})
if frozenset(_PAYLOAD_TYPE_BY_EVENT) != frozenset(ExecutionEventType):
    raise RuntimeError("every ExecutionEventType must have exactly one payload schema")


def _require_exact_keys(value: Any, expected: Iterable[str], name: str) -> dict:
    if type(value) is not dict:
        raise TypeError(f"{name} must be exactly a dict")
    expected_keys = frozenset(expected)
    actual_keys = frozenset(value)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        extra = sorted(actual_keys - expected_keys)
        raise ValueError(
            f"{name} keys mismatch: missing={missing}, extra={extra}"
        )
    return value


def _payload_from_dict(
    event_type: ExecutionEventType, value: Any
) -> ExecutionPayload:
    payload_type = _PAYLOAD_TYPE_BY_EVENT[event_type]
    payload_fields = tuple(item.name for item in fields(payload_type))
    payload = _require_exact_keys(
        value, payload_fields, f"{event_type.value} payload"
    )
    values = dict(payload)
    values["attributes"] = EventAttributes.from_dict(values["attributes"])
    restored = payload_type(**values)
    if restored.to_dict() != payload:
        raise ValueError(f"{event_type.value} payload is not canonical")
    return restored


@dataclass(frozen=True)
class ExecutionEvent:
    """Immutable deterministic envelope for one simulated execution fact.

    Schema v1 stores a timezone-naive exchange-local ``datetime`` at exact
    Python microsecond precision.  ``fold=1`` is rejected because ISO 8601 does
    not preserve that flag and would otherwise admit two objects with one wire
    representation.
    """

    SCHEMA: ClassVar[str] = EXECUTION_EVENT_SCHEMA
    SCHEMA_VERSION: ClassVar[int] = EXECUTION_EVENT_SCHEMA_VERSION

    event_id: str
    event_type: ExecutionEventType
    simulated_time: datetime
    phase_seq: int
    sequence: int
    payload: ExecutionPayload

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _nonempty_text(self.event_id, "event_id"))
        if type(self.event_type) is not ExecutionEventType:
            raise TypeError("event_type must be an ExecutionEventType")
        if type(self.simulated_time) is not datetime:
            raise TypeError("simulated_time must be a datetime supplied by the simulator")
        if self.simulated_time.tzinfo is not None:
            raise ValueError("simulated_time must be timezone-naive")
        if self.simulated_time.fold != 0:
            raise ValueError("simulated_time fold must be 0 in schema v1")
        object.__setattr__(
            self,
            "phase_seq",
            _integer(self.phase_seq, "phase_seq", minimum=0),
        )
        object.__setattr__(
            self,
            "sequence",
            _integer(self.sequence, "sequence", minimum=1),
        )
        expected_payload = _PAYLOAD_TYPE_BY_EVENT.get(self.event_type)
        if expected_payload is None:  # guarded at import; defensive for corrupt runtimes
            raise RuntimeError(
                f"no payload schema registered for {self.event_type.value}"
            )
        if type(self.payload) is not expected_payload:
            raise TypeError(
                f"event_type={self.event_type.value} requires "
                f"payload={expected_payload.__name__}"
            )

    @property
    def ordering_key(self) -> Tuple[datetime, int, int]:
        return self.simulated_time, self.phase_seq, self.sequence

    def to_dict(self) -> dict:
        values = {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "payload": self.payload.to_dict(),
            "phase_seq": self.phase_seq,
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "sequence": self.sequence,
            "simulated_time": self.simulated_time.isoformat(timespec="microseconds"),
        }
        return {key: values[key] for key in sorted(values)}

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, value: dict) -> "ExecutionEvent":
        payload = _require_exact_keys(
            value,
            (
                "event_id",
                "event_type",
                "payload",
                "phase_seq",
                "schema",
                "schema_version",
                "sequence",
                "simulated_time",
            ),
            "ExecutionEvent payload",
        )
        if payload["schema"] != EXECUTION_EVENT_SCHEMA:
            raise ValueError(
                f"ExecutionEvent schema must equal {EXECUTION_EVENT_SCHEMA!r}"
            )
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != EXECUTION_EVENT_SCHEMA_VERSION
        ):
            raise ValueError(
                "ExecutionEvent schema_version must equal "
                f"{EXECUTION_EVENT_SCHEMA_VERSION}"
            )
        if type(payload["event_type"]) is not str:
            raise TypeError("ExecutionEvent event_type must be a string")
        try:
            event_type = ExecutionEventType(payload["event_type"])
        except ValueError:
            raise ValueError(
                f"unknown ExecutionEvent event_type: {payload['event_type']!r}"
            ) from None
        simulated_text = payload["simulated_time"]
        if type(simulated_text) is not str:
            raise TypeError("ExecutionEvent simulated_time must be a string")
        try:
            simulated_time = datetime.strptime(
                simulated_text, "%Y-%m-%dT%H:%M:%S.%f"
            )
        except ValueError:
            raise ValueError(
                "ExecutionEvent simulated_time must use "
                "YYYY-MM-DDTHH:MM:SS.ffffff"
            ) from None
        if simulated_time.isoformat(timespec="microseconds") != simulated_text:
            raise ValueError(
                "ExecutionEvent simulated_time must use "
                "YYYY-MM-DDTHH:MM:SS.ffffff"
            )
        restored = cls(
            event_id=payload["event_id"],
            event_type=event_type,
            simulated_time=simulated_time,
            phase_seq=payload["phase_seq"],
            sequence=payload["sequence"],
            payload=_payload_from_dict(event_type, payload["payload"]),
        )
        if restored.to_dict() != payload:
            raise ValueError("ExecutionEvent payload is not canonical")
        return restored

    @classmethod
    def from_json(cls, value: str) -> "ExecutionEvent":
        decoded = _load_canonical_json(value, "ExecutionEvent JSON")
        restored = cls.from_dict(decoded)
        if restored.to_json() != value:
            raise ValueError("ExecutionEvent JSON is not canonical")
        return restored


@dataclass(frozen=True)
class FillJournalView:
    """Read-only fill projection copied verbatim from a ``FillPayload``."""

    event_id: str
    sequence: int
    simulated_time: datetime
    phase_seq: int
    fill_id: str
    order_id: str
    symbol: str
    quantity: int
    price: float
    amount: float
    fee: float


@dataclass(frozen=True)
class FeeJournalView:
    """Recorded fee projection; components are never recomputed from rates."""

    event_id: str
    sequence: int
    fill_id: str
    order_id: str
    total: float
    commission: Optional[float]
    stamp_duty: Optional[float]
    transfer_fee: Optional[float]


class ExecutionEventJournal:
    """Append-only, in-memory ordering and serialization boundary.

    Single and batch appends are atomic with respect to journal state: every
    event in the proposed append is validated against local copies before the
    event list and duplicate-id index are published.  This is not transaction
    atomicity for any account or broker state.
    """

    __slots__ = ("_events", "_event_ids")

    SCHEMA = EXECUTION_EVENT_JOURNAL_SCHEMA
    SCHEMA_VERSION = EXECUTION_EVENT_SCHEMA_VERSION

    def __init__(self, events: Iterable[ExecutionEvent] = ()) -> None:
        self._events = []
        self._event_ids = set()
        try:
            self.append_batch(events)
        except Exception:
            # Construction publishes no partially validated stream, even when
            # callers invoke __init__ manually on a preallocated instance.
            self._events.clear()
            self._event_ids.clear()
            raise

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[ExecutionEvent]:
        return iter(self.snapshot())

    def __getitem__(self, index):
        value = self._events[index]
        return tuple(value) if isinstance(index, slice) else value

    @property
    def next_sequence(self) -> int:
        return len(self._events) + 1

    @property
    def events(self) -> Tuple[ExecutionEvent, ...]:
        return self.snapshot()

    def append(self, event: ExecutionEvent) -> ExecutionEvent:
        """Atomically append one event while preserving the legacy return type."""

        return self.append_batch((event,))[0]

    def append_batch(
        self, events: Iterable[ExecutionEvent]
    ) -> Tuple[ExecutionEvent, ...]:
        """Atomically append an iterable of already sequenced events.

        The input is materialized without modification.  Type, identity,
        sequence, and ordering validation then runs entirely against local
        candidate state.  The journal publishes that state only after every
        event succeeds, so a failure at any position leaves both the event
        stream and duplicate-id index unchanged.  An empty iterable is a
        successful no-op.
        """

        try:
            iterator = iter(events)
        except TypeError:
            raise TypeError("events must be an iterable of ExecutionEvent instances") from None
        batch = tuple(iterator)

        if not batch:
            return ()

        candidate_events = list(self._events)
        candidate_ids = set(self._event_ids)
        expected_sequence = len(candidate_events) + 1
        previous = candidate_events[-1] if candidate_events else None

        for event in batch:
            if type(event) is not ExecutionEvent:
                raise TypeError("event must be an ExecutionEvent")
            if event.event_id in candidate_ids:
                raise ValueError(f"duplicate event_id: {event.event_id!r}")
            if event.sequence != expected_sequence:
                raise ValueError(
                    f"event sequence must be continuous: expected {expected_sequence}, "
                    f"got {event.sequence}"
                )
            if previous is not None and event.ordering_key < previous.ordering_key:
                raise ValueError(
                    "event simulated_time/phase_seq/sequence must be "
                    "monotonically non-decreasing"
                )

            candidate_events.append(event)
            candidate_ids.add(event.event_id)
            previous = event
            expected_sequence += 1

        # Publish only the fully validated candidate state.  No caller-owned
        # input container is retained or mutated.
        self._events, self._event_ids = candidate_events, candidate_ids
        return batch

    def record(
        self,
        *,
        event_id: str,
        event_type: ExecutionEventType,
        simulated_time: datetime,
        phase_seq: int,
        payload: ExecutionPayload,
    ) -> ExecutionEvent:
        """Build and append an event using the next deterministic sequence."""

        event = ExecutionEvent(
            event_id=event_id,
            event_type=event_type,
            simulated_time=simulated_time,
            phase_seq=phase_seq,
            sequence=self.next_sequence,
            payload=payload,
        )
        return self.append(event)

    def snapshot(self) -> Tuple[ExecutionEvent, ...]:
        """Return an immutable point-in-time view of the current events."""

        return tuple(self._events)

    def _restore_snapshot(self, events: Iterable[ExecutionEvent]) -> None:
        """Restore a previously captured snapshot for a broker transaction.

        This deliberately private hook is the journal participant used by
        :class:`SettlementUnitOfWork`.  Public callers still see an append-only
        API, while a failed settlement can roll the journal back in-place so
        references obtained before the transaction remain valid.
        """

        restored = ExecutionEventJournal(events)
        self._events[:] = restored._events
        self._event_ids.clear()
        self._event_ids.update(restored._event_ids)

    def filter_events(
        self,
        *,
        event_types: Optional[Iterable[ExecutionEventType]] = None,
        start_sequence: Optional[int] = None,
        end_sequence: Optional[int] = None,
    ) -> Tuple[ExecutionEvent, ...]:
        """Return a non-mutating filtered snapshot (sequence bounds inclusive)."""

        allowed = None
        if event_types is not None:
            if type(event_types) is ExecutionEventType:
                candidates = (event_types,)
            else:
                try:
                    candidates = tuple(event_types)
                except TypeError:
                    raise TypeError("event_types must be an iterable of event enums") from None
            if any(type(item) is not ExecutionEventType for item in candidates):
                raise TypeError("event_types must contain only ExecutionEventType members")
            allowed = frozenset(candidates)

        if start_sequence is not None:
            start_sequence = _integer(start_sequence, "start_sequence", minimum=1)
        if end_sequence is not None:
            end_sequence = _integer(end_sequence, "end_sequence", minimum=1)
        if (
            start_sequence is not None
            and end_sequence is not None
            and start_sequence > end_sequence
        ):
            raise ValueError("start_sequence must not exceed end_sequence")

        result = []
        for event in self.snapshot():
            if allowed is not None and event.event_type not in allowed:
                continue
            if start_sequence is not None and event.sequence < start_sequence:
                continue
            if end_sequence is not None and event.sequence > end_sequence:
                continue
            result.append(event)
        return tuple(result)

    def replay(
        self,
        *,
        start_sequence: int = 1,
        end_sequence: Optional[int] = None,
    ) -> Tuple[ExecutionEvent, ...]:
        """Return a deterministic replay view without applying side effects."""

        return self.filter_events(
            start_sequence=start_sequence,
            end_sequence=end_sequence,
        )

    def fill_view(self) -> Tuple[FillJournalView, ...]:
        """Return immutable fills using only values already stored in events."""

        result = []
        for event in self.filter_events(
                event_types=(ExecutionEventType.FILL,)):
            payload = event.payload
            result.append(FillJournalView(
                event_id=event.event_id,
                sequence=event.sequence,
                simulated_time=event.simulated_time,
                phase_seq=event.phase_seq,
                fill_id=payload.fill_id,
                order_id=payload.order_id,
                symbol=payload.symbol,
                quantity=payload.quantity,
                price=payload.price,
                amount=payload.amount,
                fee=payload.fee,
            ))
        return tuple(result)

    def fee_view(self) -> Tuple[FeeJournalView, ...]:
        """Return exact recorded fee totals/components without rate arithmetic."""

        result = []
        for event in self.filter_events(
                event_types=(ExecutionEventType.FILL,)):
            payload = event.payload
            attributes = dict(payload.attributes.entries)
            result.append(FeeJournalView(
                event_id=event.event_id,
                sequence=event.sequence,
                fill_id=payload.fill_id,
                order_id=payload.order_id,
                total=payload.fee,
                commission=attributes.get('commission'),
                stamp_duty=attributes.get('stamp_duty'),
                transfer_fee=attributes.get('transfer_fee'),
            ))
        return tuple(result)

    def to_dict(self) -> dict:
        values = {
            "events": [event.to_dict() for event in self.snapshot()],
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
        }
        return {key: values[key] for key in sorted(values)}

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, value: dict) -> "ExecutionEventJournal":
        payload = _require_exact_keys(
            value,
            ("events", "schema", "schema_version"),
            "ExecutionEventJournal payload",
        )
        if payload["schema"] != EXECUTION_EVENT_JOURNAL_SCHEMA:
            raise ValueError(
                "ExecutionEventJournal schema must equal "
                f"{EXECUTION_EVENT_JOURNAL_SCHEMA!r}"
            )
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != EXECUTION_EVENT_SCHEMA_VERSION
        ):
            raise ValueError(
                "ExecutionEventJournal schema_version must equal "
                f"{EXECUTION_EVENT_SCHEMA_VERSION}"
            )
        raw_events = payload["events"]
        if type(raw_events) is not list:
            raise TypeError("ExecutionEventJournal events must be exactly a list")
        restored = cls(ExecutionEvent.from_dict(event) for event in raw_events)
        if restored.to_dict() != payload:
            raise ValueError("ExecutionEventJournal payload is not canonical")
        return restored

    @classmethod
    def from_json(cls, value: str) -> "ExecutionEventJournal":
        decoded = _load_canonical_json(value, "ExecutionEventJournal JSON")
        restored = cls.from_dict(decoded)
        if restored.to_json() != value:
            raise ValueError("ExecutionEventJournal JSON is not canonical")
        return restored


__all__ = [
    "EXECUTION_EVENT_SCHEMA",
    "EXECUTION_EVENT_JOURNAL_SCHEMA",
    "EXECUTION_EVENT_SCHEMA_VERSION",
    "ExecutionEventType",
    "EventAttributes",
    "EventPayload",
    "OrderEventPayload",
    "ReservationPayload",
    "FillPayload",
    "SettlementPayload",
    "T1UnlockPayload",
    "CorporateActionPayload",
    "MarkPayload",
    "MarginPayload",
    "RollPayload",
    "ExecutionPayload",
    "ExecutionEvent",
    "FillJournalView",
    "FeeJournalView",
    "ExecutionEventJournal",
]
