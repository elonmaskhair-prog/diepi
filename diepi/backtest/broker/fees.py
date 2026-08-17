"""Pure, auditable transaction-fee calculations.

``FeeEngine`` deliberately has no account or cash dependency.  A caller passes
the immutable state produced for the previous fill and receives an immutable
breakdown plus the next state.  This makes fee assessment safe to perform while
constructing a settlement event: no cash, order, or engine-owned accumulator is
mutated before the surrounding unit of work commits.

The default commission scope is ``parent_order``.  Commission for each fill is
the increment in the rounded commission due on the parent order's cumulative
notional.  Consequently, splitting one order into several fills does not apply
the minimum commission repeatedly.  ``fill`` is retained as an explicit stress
mode.  Stamp duty is charged on sells only; transfer fees are charged on both
sides.  Both are calculated independently for every fill.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from enum import Enum
from math import isfinite
from numbers import Integral
from sys import float_info
from typing import Optional, Union


class CommissionScope(str, Enum):
    """Minimum-commission aggregation boundary."""

    PARENT_ORDER = "parent_order"
    FILL = "fill"


class FeeSide(str, Enum):
    """Direction used by fee rules."""

    BUY = "buy"
    SELL = "sell"


_CENT = Decimal("0.01")
_MAX_FLOAT = Decimal(str(float_info.max))


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    """Convert a numeric public input to a finite non-negative ``Decimal``.

    Strings and booleans are rejected even when Python could coerce them.  The
    public result surface uses floats for compatibility with the existing cash
    engine, so values outside the finite float range are rejected as well.
    """

    if isinstance(value, (bool, str, bytes)):
        raise ValueError(f"{name} must be a finite non-negative number")
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        raise ValueError(
            f"{name} must be a finite non-negative number"
        ) from None
    if not isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return Decimal(str(number))


def _finite_float(value: Decimal, name: str) -> float:
    if not value.is_finite() or value < 0 or value > _MAX_FLOAT:
        raise ValueError(f"{name} must be finite and non-negative")
    number = float(value)
    if not isfinite(number):
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _round_money(value: Decimal, name: str) -> Decimal:
    """Round a non-negative amount to cents using commercial half-up rules."""

    if not value.is_finite() or value < 0 or value > _MAX_FLOAT:
        raise ValueError(f"{name} must be finite and non-negative")
    try:
        # Quantizing a value near the float limit requires more than Decimal's
        # default 28 digits of precision.
        with localcontext() as context:
            context.prec = 400
            rounded = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, OverflowError):
        raise ValueError(f"{name} must be finite and non-negative") from None
    if rounded > _MAX_FLOAT:
        raise ValueError(f"{name} must be finite and non-negative")
    return rounded


def _coerce_scope(value: Union[CommissionScope, str]) -> CommissionScope:
    if isinstance(value, CommissionScope):
        return value
    if isinstance(value, str):
        try:
            return CommissionScope(value)
        except ValueError:
            pass
    raise ValueError("commission_scope must be 'parent_order' or 'fill'")


def _coerce_side(value: Union[FeeSide, str]) -> FeeSide:
    if isinstance(value, FeeSide):
        return value
    if isinstance(value, str):
        try:
            return FeeSide(value)
        except ValueError:
            pass
    raise ValueError("side must be 'buy' or 'sell'")


def _validate_parent_order_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("parent_order_id must be a non-empty string")
    return value


def _validate_nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name} must be a non-negative integer")
    result = int(value)
    if result < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return result


@dataclass(frozen=True)
class FeeSchedule:
    """Immutable fee policy used by one ``FeeEngine``.

    All rates are proportions of fill notional.  A rate of zero disables that
    component.  No upper bound is imposed so deliberately punitive stress
    schedules remain possible, but every value must be finite and non-negative.
    """

    commission_rate: float = 0.00025
    min_commission: float = 5.0
    stamp_duty_rate: float = 0.001
    transfer_fee_rate: float = 0.0
    commission_scope: CommissionScope = CommissionScope.PARENT_ORDER

    def __post_init__(self) -> None:
        for name in (
            "commission_rate",
            "min_commission",
            "stamp_duty_rate",
            "transfer_fee_rate",
        ):
            value = _nonnegative_decimal(getattr(self, name), name)
            object.__setattr__(self, name, _finite_float(value, name))
        object.__setattr__(
            self, "commission_scope", _coerce_scope(self.commission_scope)
        )


@dataclass(frozen=True)
class ParentOrderFeeState:
    """Fee accumulation state after zero or more fills of one parent order."""

    parent_order_id: str
    side: FeeSide
    cumulative_notional: float
    commission_charged: float
    fill_count: int
    policy_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parent_order_id",
            _validate_parent_order_id(self.parent_order_id),
        )
        object.__setattr__(self, "side", _coerce_side(self.side))
        for name in ("cumulative_notional", "commission_charged"):
            value = _nonnegative_decimal(getattr(self, name), name)
            object.__setattr__(self, name, _finite_float(value, name))
        object.__setattr__(
            self, "fill_count", _validate_nonnegative_int(self.fill_count, "fill_count")
        )
        if not isinstance(self.policy_key, str) or not self.policy_key:
            raise ValueError("policy_key must be a non-empty string")
        if self.fill_count == 0 and (
            self.cumulative_notional != 0 or self.commission_charged != 0
        ):
            raise ValueError("empty fee state must have zero cumulative values")
        if self.fill_count > 0 and self.cumulative_notional <= 0:
            raise ValueError("non-empty fee state must have positive notional")


@dataclass(frozen=True)
class FeeBreakdown:
    """Immutable fee assessment for exactly one fill (or a zero no-op)."""

    parent_order_id: str
    side: FeeSide
    notional: float
    commission: float
    stamp_duty: float
    transfer_fee: float
    total: float
    next_state: ParentOrderFeeState

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parent_order_id",
            _validate_parent_order_id(self.parent_order_id),
        )
        object.__setattr__(self, "side", _coerce_side(self.side))
        notional = _nonnegative_decimal(self.notional, "notional")
        object.__setattr__(self, "notional", _finite_float(notional, "notional"))

        money = {}
        for name in ("commission", "stamp_duty", "transfer_fee", "total"):
            value = _nonnegative_decimal(getattr(self, name), name)
            if value != _round_money(value, name):
                raise ValueError(f"{name} must be rounded to cents")
            money[name] = value
            object.__setattr__(self, name, _finite_float(value, name))
        expected_total = (
            money["commission"] + money["stamp_duty"] + money["transfer_fee"]
        )
        if money["total"] != expected_total:
            raise ValueError("total must equal all fee components")
        if not isinstance(self.next_state, ParentOrderFeeState):
            raise TypeError("next_state must be a ParentOrderFeeState")
        if self.next_state.parent_order_id != self.parent_order_id:
            raise ValueError("next_state belongs to a different parent order")
        if self.next_state.side is not self.side:
            raise ValueError("next_state side does not match the fee result")

    @property
    def cumulative_notional(self) -> float:
        return self.next_state.cumulative_notional

    @property
    def cumulative_commission(self) -> float:
        return self.next_state.commission_charged

    @property
    def fill_count(self) -> int:
        return self.next_state.fill_count


@dataclass(frozen=True)
class FeeEngine:
    """Stateless fee calculator with immutable caller-owned order state."""

    schedule: FeeSchedule = FeeSchedule()

    def __post_init__(self) -> None:
        if not isinstance(self.schedule, FeeSchedule):
            raise TypeError("schedule must be a FeeSchedule")

    @classmethod
    def from_rates(
        cls,
        *,
        commission_rate: float = 0.00025,
        min_commission: float = 5.0,
        stamp_duty_rate: float = 0.001,
        transfer_fee_rate: float = 0.0,
        commission_scope: Union[CommissionScope, str] = CommissionScope.PARENT_ORDER,
    ) -> "FeeEngine":
        """Construct an engine from the existing cash-engine style parameters."""

        return cls(
            FeeSchedule(
                commission_rate=commission_rate,
                min_commission=min_commission,
                stamp_duty_rate=stamp_duty_rate,
                transfer_fee_rate=transfer_fee_rate,
                commission_scope=commission_scope,
            )
        )

    @property
    def commission_scope(self) -> CommissionScope:
        return self.schedule.commission_scope

    def _policy_key(self) -> str:
        """Fingerprint only the policy that accumulates across fills.

        Stamp duty and transfer-fee rates are intentionally absent: they are
        independent per-fill rules and may be resolved by symbol/effective date
        without invalidating the parent order's commission state.
        """

        values = (
            self.schedule.commission_scope.value,
            str(_nonnegative_decimal(self.schedule.commission_rate, "commission_rate")),
            str(_nonnegative_decimal(self.schedule.min_commission, "min_commission")),
        )
        return "|".join(values)

    def initial_state(
        self, parent_order_id: str, side: Union[FeeSide, str]
    ) -> ParentOrderFeeState:
        """Create the zero state for a parent order without retaining it."""

        return ParentOrderFeeState(
            parent_order_id=_validate_parent_order_id(parent_order_id),
            side=_coerce_side(side),
            cumulative_notional=0.0,
            commission_charged=0.0,
            fill_count=0,
            policy_key=self._policy_key(),
        )

    def calculate_fill(
        self,
        notional: float,
        *,
        parent_order_id: str,
        side: Union[FeeSide, str],
        prior_state: Optional[ParentOrderFeeState] = None,
        stamp_duty_exempt: bool = False,
        transfer_fee_exempt: bool = False,
        stamp_duty_rate: Optional[float] = None,
        transfer_fee_rate: Optional[float] = None,
    ) -> FeeBreakdown:
        """Calculate one fill's fees without modifying cash or retained state.

        ``prior_state`` must be the ``next_state`` returned for the preceding
        fill of the same parent order.  A zero notional is a no-op: it charges
        no minimum commission and does not increment ``fill_count``.  Optional
        tax-rate arguments let a RuleBook adapter supply symbol/date-resolved
        rates; these per-fill rules do not alter commission accumulation.
        """

        order_id = _validate_parent_order_id(parent_order_id)
        trade_side = _coerce_side(side)
        fill_notional = _nonnegative_decimal(notional, "notional")
        if type(stamp_duty_exempt) is not bool:
            raise ValueError("stamp_duty_exempt must be a bool")
        if type(transfer_fee_exempt) is not bool:
            raise ValueError("transfer_fee_exempt must be a bool")
        resolved_stamp_rate = _nonnegative_decimal(
            self.schedule.stamp_duty_rate
            if stamp_duty_rate is None
            else stamp_duty_rate,
            "stamp_duty_rate",
        )
        resolved_transfer_rate = _nonnegative_decimal(
            self.schedule.transfer_fee_rate
            if transfer_fee_rate is None
            else transfer_fee_rate,
            "transfer_fee_rate",
        )

        if prior_state is None:
            state = self.initial_state(order_id, trade_side)
        else:
            if not isinstance(prior_state, ParentOrderFeeState):
                raise TypeError("prior_state must be a ParentOrderFeeState")
            state = prior_state
            if state.parent_order_id != order_id:
                raise ValueError("prior_state belongs to a different parent order")
            if state.side is not trade_side:
                raise ValueError("prior_state side does not match this fill")
            if state.policy_key != self._policy_key():
                raise ValueError("prior_state was created by a different fee policy")

        if fill_notional == 0:
            return FeeBreakdown(
                parent_order_id=order_id,
                side=trade_side,
                notional=0.0,
                commission=0.0,
                stamp_duty=0.0,
                transfer_fee=0.0,
                total=0.0,
                next_state=state,
            )

        previous_notional = _nonnegative_decimal(
            state.cumulative_notional, "cumulative_notional"
        )
        previous_commission = _nonnegative_decimal(
            state.commission_charged, "commission_charged"
        )
        cumulative_notional = previous_notional + fill_notional
        if cumulative_notional > _MAX_FLOAT:
            raise ValueError("cumulative_notional must be finite and non-negative")

        commission_rate = _nonnegative_decimal(
            self.schedule.commission_rate, "commission_rate"
        )
        min_commission = _nonnegative_decimal(
            self.schedule.min_commission, "min_commission"
        )

        if self.commission_scope is CommissionScope.PARENT_ORDER:
            if state.fill_count:
                previous_due = _round_money(
                    max(previous_notional * commission_rate, min_commission),
                    "commission",
                )
                if previous_due != previous_commission:
                    raise ValueError(
                        "prior_state commission is inconsistent with this fee policy"
                    )
            cumulative_due = _round_money(
                max(cumulative_notional * commission_rate, min_commission),
                "commission",
            )
            commission = cumulative_due - previous_commission
            if commission < 0:
                raise ValueError("commission increment cannot be negative")
            cumulative_commission = cumulative_due
        else:
            commission = _round_money(
                max(fill_notional * commission_rate, min_commission),
                "commission",
            )
            cumulative_commission = previous_commission + commission
            if cumulative_commission > _MAX_FLOAT:
                raise ValueError("cumulative commission must be finite")

        stamp_duty = Decimal(0)
        if trade_side is FeeSide.SELL and not stamp_duty_exempt:
            stamp_duty = _round_money(
                fill_notional * resolved_stamp_rate, "stamp_duty"
            )
        transfer_fee = Decimal(0)
        if not transfer_fee_exempt:
            transfer_fee = _round_money(
                fill_notional * resolved_transfer_rate, "transfer_fee"
            )
        total = commission + stamp_duty + transfer_fee
        if total > _MAX_FLOAT:
            raise ValueError("total fee must be finite")

        next_state = ParentOrderFeeState(
            parent_order_id=order_id,
            side=trade_side,
            cumulative_notional=_finite_float(
                cumulative_notional, "cumulative_notional"
            ),
            commission_charged=_finite_float(
                cumulative_commission, "commission_charged"
            ),
            fill_count=state.fill_count + 1,
            policy_key=self._policy_key(),
        )
        return FeeBreakdown(
            parent_order_id=order_id,
            side=trade_side,
            notional=_finite_float(fill_notional, "notional"),
            commission=_finite_float(commission, "commission"),
            stamp_duty=_finite_float(stamp_duty, "stamp_duty"),
            transfer_fee=_finite_float(transfer_fee, "transfer_fee"),
            total=_finite_float(total, "total"),
            next_state=next_state,
        )


__all__ = [
    "CommissionScope",
    "FeeBreakdown",
    "FeeEngine",
    "FeeSchedule",
    "FeeSide",
    "ParentOrderFeeState",
]
