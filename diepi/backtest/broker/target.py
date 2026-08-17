"""Immutable contracts for delayed target-weight execution.

``TargetIntent`` records only what the strategy knew at its decision point.
It deliberately contains no price, NAV, notional, share quantity, or side.
Those values are derived by the broker at the eligible closing match and are
reported separately in ``TargetAchievement``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isclose
from typing import ClassVar, Tuple

from .validation import (
    finite_float,
    nonnegative_float,
    nonnegative_int,
    positive_float,
    unit_interval,
)


TARGET_INTENT_SCHEMA = "diepi.target_intent"
TARGET_ACHIEVEMENT_SCHEMA = "diepi.target_achievement"
TARGET_SCHEMA_VERSION = 1

_TARGET_INTENT_KEYS = frozenset({
    "batch_id",
    "created_at",
    "created_phase",
    "eligible_from",
    "expire_date",
    "intent_id",
    "min_trade_amount",
    "note",
    "schema",
    "schema_version",
    "symbol",
    "target_weight",
    "window",
})
_TARGET_ACHIEVEMENT_KEYS = frozenset({
    "achieved_value",
    "achieved_weight",
    "batch_id",
    "cap_respected",
    "filled_shares",
    "gap_weight",
    "intent_id",
    "order_ids",
    "planned_shares",
    "planned_side",
    "reasons",
    "reference_nav",
    "reference_price",
    "schema",
    "schema_version",
    "status",
    "symbol",
    "target_notional",
    "target_weight",
    "trade_date",
})


class TargetWindow(str, Enum):
    """Execution window supported by delayed target intents in schema v1."""

    CLOSE = "close"


class TargetPlanSide(str, Enum):
    """Direction derived by the broker at the execution window."""

    BUY = "buy"
    SELL = "sell"
    NONE = "none"


class TargetAchievementStatus(str, Enum):
    """Terminal relationship between achieved and requested exposure."""

    ACHIEVED = "achieved"
    UNDER_TARGET = "under_target"
    OVER_TARGET = "over_target"
    NOOP = "noop"
    SUPERSEDED = "superseded"
    INFEASIBLE = "infeasible"
    BLOCKED = "blocked"


def _text(value, name: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    value = value.strip()
    if not value and not allow_empty:
        raise ValueError(f"{name} must be non-empty")
    return value


def _simulated_datetime(value, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{name} must be a datetime")
    if value.tzinfo is not None:
        raise ValueError(f"{name} must be timezone-naive")
    if value.fold != 0:
        raise ValueError(f"{name} fold must be 0")
    # pandas.Timestamp can carry nanoseconds that cannot survive the public
    # schema.  Reject that precision instead of silently rounding it away.
    if getattr(value, "nanosecond", 0):
        raise ValueError(f"{name} must have at most microsecond precision")
    return datetime(
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,
        fold=0,
    )


def _trade_date(value, name: str = "trade_date") -> str:
    value = _text(value, name)
    try:
        parsed = datetime.strptime(value, "%Y%m%d")
    except ValueError:
        raise ValueError(f"{name} must use YYYYMMDD") from None
    if parsed.strftime("%Y%m%d") != value:
        raise ValueError(f"{name} must use YYYYMMDD")
    return value


def _text_tuple(values, name: str, *, unique: bool) -> Tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError(f"{name} must be a tuple")
    result = tuple(_text(value, f"{name}[{index}]") for index, value in enumerate(values))
    if unique and len(set(result)) != len(result):
        raise ValueError(f"{name} must not contain duplicates")
    return result


def _strict_payload(value, *, name: str, keys, schema: str) -> dict:
    if type(value) is not dict:
        raise ValueError(f"{name} must be exactly a dict")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise ValueError(
            f"{name} keys mismatch: missing={missing}, extra={extra}")
    if value["schema"] != schema:
        raise ValueError(f"{name} schema must equal {schema!r}")
    if (type(value["schema_version"]) is not int
            or value["schema_version"] != TARGET_SCHEMA_VERSION):
        raise ValueError(
            f"{name} schema_version must equal {TARGET_SCHEMA_VERSION}")
    return value


def _serialized_datetime(value, name: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"{name} must be a serialized datetime string")
    try:
        result = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        raise ValueError(
            f"{name} must use YYYY-MM-DDTHH:MM:SS.ffffff") from None
    if result.isoformat(timespec="microseconds") != value:
        raise ValueError(
            f"{name} must use YYYY-MM-DDTHH:MM:SS.ffffff")
    return result


def _serialized_enum(value, enum_type, name: str):
    if type(value) is not str:
        raise ValueError(f"{name} must be a string enum value")
    try:
        return enum_type(value)
    except ValueError:
        allowed = ", ".join(item.value for item in enum_type)
        raise ValueError(f"{name} must be one of: {allowed}") from None


@dataclass(frozen=True)
class TargetIntent:
    """A same-day close target recorded before its sizing information exists."""

    SCHEMA: ClassVar[str] = TARGET_INTENT_SCHEMA
    SCHEMA_VERSION: ClassVar[int] = TARGET_SCHEMA_VERSION

    intent_id: str
    batch_id: str
    symbol: str
    target_weight: float
    created_at: datetime
    created_phase: str
    eligible_from: datetime
    expire_date: str
    min_trade_amount: float = 0.0
    window: TargetWindow = TargetWindow.CLOSE
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _text(self.intent_id, "intent_id"))
        object.__setattr__(self, "batch_id", _text(self.batch_id, "batch_id"))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        object.__setattr__(
            self,
            "target_weight",
            unit_interval(self.target_weight, "target_weight"),
        )
        created_at = _simulated_datetime(self.created_at, "created_at")
        eligible_from = _simulated_datetime(self.eligible_from, "eligible_from")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "eligible_from", eligible_from)

        created_phase = _text(self.created_phase, "created_phase")
        if created_phase not in {"on_after_open", "on_before_close"}:
            raise ValueError(
                "created_phase must be 'on_after_open' or 'on_before_close'"
            )
        object.__setattr__(self, "created_phase", created_phase)

        if type(self.window) is not TargetWindow or self.window is not TargetWindow.CLOSE:
            raise ValueError("schema v1 TargetIntent window must be TargetWindow.CLOSE")
        if eligible_from <= created_at:
            raise ValueError("eligible_from must be strictly after created_at")
        if eligible_from.date() != created_at.date():
            raise ValueError("schema v1 TargetIntent must execute on its creation date")

        expire_date = _trade_date(self.expire_date, "expire_date")
        if expire_date != eligible_from.strftime("%Y%m%d"):
            raise ValueError("expire_date must equal the eligible close trade date")
        object.__setattr__(self, "expire_date", expire_date)
        object.__setattr__(
            self,
            "min_trade_amount",
            nonnegative_float(self.min_trade_amount, "min_trade_amount"),
        )
        object.__setattr__(self, "note", _text(self.note, "note", allow_empty=True))

    @property
    def ordering_key(self):
        return self.eligible_from, self.created_at, self.batch_id, self.intent_id

    def to_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "created_at": self.created_at.isoformat(timespec="microseconds"),
            "created_phase": self.created_phase,
            "eligible_from": self.eligible_from.isoformat(timespec="microseconds"),
            "expire_date": self.expire_date,
            "intent_id": self.intent_id,
            "min_trade_amount": self.min_trade_amount,
            "note": self.note,
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "symbol": self.symbol,
            "target_weight": self.target_weight,
            "window": self.window.value,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, value: dict) -> "TargetIntent":
        payload = _strict_payload(
            value,
            name="TargetIntent payload",
            keys=_TARGET_INTENT_KEYS,
            schema=TARGET_INTENT_SCHEMA,
        )
        restored = cls(
            intent_id=payload["intent_id"],
            batch_id=payload["batch_id"],
            symbol=payload["symbol"],
            target_weight=payload["target_weight"],
            created_at=_serialized_datetime(
                payload["created_at"], "created_at"),
            created_phase=payload["created_phase"],
            eligible_from=_serialized_datetime(
                payload["eligible_from"], "eligible_from"),
            expire_date=payload["expire_date"],
            min_trade_amount=payload["min_trade_amount"],
            window=_serialized_enum(
                payload["window"], TargetWindow, "window"),
            note=payload["note"],
        )
        if restored.to_dict() != payload:
            raise ValueError(
                "TargetIntent payload must use the canonical wire representation"
            )
        return restored


@dataclass(frozen=True)
class TargetAchievement:
    """Terminal, auditable close-window outcome for exactly one intent.

    ``gap_weight`` is defined as ``achieved_weight - target_weight``.  A
    positive gap therefore means over-target exposure.  ``cap_respected`` is
    true exactly when that gap is not positive beyond numeric tolerance.
    """

    SCHEMA: ClassVar[str] = TARGET_ACHIEVEMENT_SCHEMA
    SCHEMA_VERSION: ClassVar[int] = TARGET_SCHEMA_VERSION

    intent_id: str
    batch_id: str
    trade_date: str
    symbol: str
    target_weight: float
    reference_nav: float
    reference_price: float
    target_notional: float
    planned_side: TargetPlanSide
    planned_shares: int
    order_ids: Tuple[str, ...]
    filled_shares: int
    achieved_value: float
    achieved_weight: float
    gap_weight: float
    cap_respected: bool
    status: TargetAchievementStatus
    reasons: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent_id", _text(self.intent_id, "intent_id"))
        object.__setattr__(self, "batch_id", _text(self.batch_id, "batch_id"))
        object.__setattr__(self, "trade_date", _trade_date(self.trade_date))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        target_weight = unit_interval(self.target_weight, "target_weight")
        reference_nav = positive_float(self.reference_nav, "reference_nav")
        # A terminal BLOCKED/SUPERSEDED outcome may have no observable close
        # at all.  Keep that fact explicit as ``0.0`` instead of inventing a
        # tradable price; the narrow exception is validated below after the
        # status/reason fields have been normalized.
        reference_price = nonnegative_float(
            self.reference_price, "reference_price")
        target_notional = nonnegative_float(self.target_notional, "target_notional")
        achieved_value = nonnegative_float(self.achieved_value, "achieved_value")
        achieved_weight = nonnegative_float(self.achieved_weight, "achieved_weight")
        gap_weight = finite_float(self.gap_weight, "gap_weight")
        object.__setattr__(self, "target_weight", target_weight)
        object.__setattr__(self, "reference_nav", reference_nav)
        object.__setattr__(self, "reference_price", reference_price)
        object.__setattr__(self, "target_notional", target_notional)
        object.__setattr__(self, "achieved_value", achieved_value)
        object.__setattr__(self, "achieved_weight", achieved_weight)
        object.__setattr__(self, "gap_weight", gap_weight)

        expected_notional = reference_nav * target_weight
        if not isclose(target_notional, expected_notional, rel_tol=1e-12, abs_tol=1e-8):
            raise ValueError("target_notional must equal reference_nav * target_weight")
        if not isclose(
            achieved_weight,
            achieved_value / reference_nav,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError("achieved_weight must equal achieved_value / reference_nav")
        expected_gap = achieved_weight - target_weight
        if not isclose(gap_weight, expected_gap, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("gap_weight must equal achieved_weight - target_weight")

        if type(self.planned_side) is not TargetPlanSide:
            raise ValueError("planned_side must be a TargetPlanSide")
        planned_shares = nonnegative_int(self.planned_shares, "planned_shares")
        filled_shares = nonnegative_int(self.filled_shares, "filled_shares")
        object.__setattr__(self, "planned_shares", planned_shares)
        object.__setattr__(self, "filled_shares", filled_shares)
        if self.planned_side is TargetPlanSide.NONE and planned_shares != 0:
            raise ValueError("planned_side NONE requires planned_shares=0")
        # A directional plan can legitimately materialize to zero shares: the
        # target delta can be below the minimum lot, T+1 can leave no sellable
        # inventory, or proportional cash scaling can floor every buy to zero.
        # Keeping BUY/SELL is more informative than erasing the direction and
        # pretending that no target delta existed.
        if filled_shares > planned_shares:
            raise ValueError("filled_shares cannot exceed planned_shares")

        order_ids = _text_tuple(self.order_ids, "order_ids", unique=True)
        reasons = _text_tuple(self.reasons, "reasons", unique=False)
        object.__setattr__(self, "order_ids", order_ids)
        object.__setattr__(self, "reasons", reasons)
        if filled_shares and not order_ids:
            raise ValueError("filled_shares requires at least one order_id")

        if type(self.cap_respected) is not bool:
            raise ValueError("cap_respected must be a bool")
        expected_cap_respected = gap_weight <= 1e-12
        if self.cap_respected is not expected_cap_respected:
            raise ValueError("cap_respected must agree with the achieved target gap")
        if type(self.status) is not TargetAchievementStatus:
            raise ValueError("status must be a TargetAchievementStatus")
        if reference_price == 0.0:
            if not (
                self.status in {
                    TargetAchievementStatus.BLOCKED,
                    TargetAchievementStatus.SUPERSEDED,
                }
                and self.planned_side is TargetPlanSide.NONE
                and planned_shares == 0
                and filled_shares == 0
                and not order_ids
                and "no_close_bar" in reasons
            ):
                raise ValueError(
                    "reference_price=0 requires an unplanned no_close_bar "
                    "BLOCKED/SUPERSEDED outcome"
                )
        if self.status is TargetAchievementStatus.ACHIEVED:
            if not isclose(gap_weight, 0.0, abs_tol=1e-12):
                raise ValueError("ACHIEVED status requires zero target gap")
        elif self.status is TargetAchievementStatus.UNDER_TARGET:
            if gap_weight >= -1e-12:
                raise ValueError("UNDER_TARGET status requires a negative target gap")
        elif self.status is TargetAchievementStatus.OVER_TARGET:
            if gap_weight <= 1e-12:
                raise ValueError("OVER_TARGET status requires a positive target gap")
        if self.status is not TargetAchievementStatus.ACHIEVED and not reasons:
            raise ValueError("non-ACHIEVED target outcomes require at least one reason")

    def to_dict(self) -> dict:
        return {
            "achieved_value": self.achieved_value,
            "achieved_weight": self.achieved_weight,
            "batch_id": self.batch_id,
            "cap_respected": self.cap_respected,
            "filled_shares": self.filled_shares,
            "gap_weight": self.gap_weight,
            "intent_id": self.intent_id,
            "order_ids": list(self.order_ids),
            "planned_shares": self.planned_shares,
            "planned_side": self.planned_side.value,
            "reasons": list(self.reasons),
            "reference_nav": self.reference_nav,
            "reference_price": self.reference_price,
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "status": self.status.value,
            "symbol": self.symbol,
            "target_notional": self.target_notional,
            "target_weight": self.target_weight,
            "trade_date": self.trade_date,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_dict(cls, value: dict) -> "TargetAchievement":
        payload = _strict_payload(
            value,
            name="TargetAchievement payload",
            keys=_TARGET_ACHIEVEMENT_KEYS,
            schema=TARGET_ACHIEVEMENT_SCHEMA,
        )
        if type(payload["order_ids"]) is not list:
            raise ValueError("order_ids must be exactly a list")
        if type(payload["reasons"]) is not list:
            raise ValueError("reasons must be exactly a list")
        restored = cls(
            intent_id=payload["intent_id"],
            batch_id=payload["batch_id"],
            trade_date=payload["trade_date"],
            symbol=payload["symbol"],
            target_weight=payload["target_weight"],
            reference_nav=payload["reference_nav"],
            reference_price=payload["reference_price"],
            target_notional=payload["target_notional"],
            planned_side=_serialized_enum(
                payload["planned_side"], TargetPlanSide, "planned_side"),
            planned_shares=payload["planned_shares"],
            order_ids=tuple(payload["order_ids"]),
            filled_shares=payload["filled_shares"],
            achieved_value=payload["achieved_value"],
            achieved_weight=payload["achieved_weight"],
            gap_weight=payload["gap_weight"],
            cap_respected=payload["cap_respected"],
            status=_serialized_enum(
                payload["status"], TargetAchievementStatus, "status"),
            reasons=tuple(payload["reasons"]),
        )
        if restored.to_dict() != payload:
            raise ValueError(
                "TargetAchievement payload must use the canonical wire representation"
            )
        return restored


__all__ = [
    "TARGET_ACHIEVEMENT_SCHEMA",
    "TARGET_INTENT_SCHEMA",
    "TARGET_SCHEMA_VERSION",
    "TargetAchievement",
    "TargetAchievementStatus",
    "TargetIntent",
    "TargetPlanSide",
    "TargetWindow",
]
