"""Strict economic replay for the cash execution-event journal.

This module reconstructs the cash account state from an explicit seed and the
facts already recorded in :mod:`diepi.backtest.broker.events`.  It deliberately does
not recalculate fees, consult a RuleBook, mutate an :class:`Account`, replay
futures events, or claim double-entry accounting semantics.

The reducer is intentionally stricter than the generic event deserializer.  A
cash fill is only replayable when its persisted attributes contain every rule
decision needed for accounting (notably ``side`` and exact-bool ``t0``), and it
must be immediately paired with one cash settlement and one reservation
release.  Corrupt, incomplete, or economically inconsistent streams fail
before a result is returned.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from math import isfinite
from types import MappingProxyType
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from .events import (
    CorporateActionPayload,
    ExecutionEvent,
    ExecutionEventJournal,
    ExecutionEventType,
    FillPayload,
    MarkPayload,
    OrderEventPayload,
    ReservationPayload,
    SettlementPayload,
    T1UnlockPayload,
)


CASH_REPLAY_SEED_SCHEMA = "diepi.cash_replay_seed"
CASH_REPLAY_SEED_SCHEMA_VERSION = 1
CASH_AUDIT_BUNDLE_SCHEMA = "diepi.cash_audit_bundle"
CASH_AUDIT_BUNDLE_SCHEMA_VERSION = 1
_MAX_SAFE_JSON_INTEGER = (1 << 53) - 1
_ABS_TOLERANCE = 1e-7


class CashReplayError(ValueError):
    """Raised when a cash journal cannot be replayed without guessing."""


def _same_amount(left: float, right: float) -> bool:
    # Cash, fees, notionals and cost bases are economic facts.  A relative
    # tolerance silently grows with account size (at 1e13, 1e-12 permits a
    # multi-yuan mismatch), so it is inappropriate at this audit boundary.
    # The reducer repeats the broker's persisted arithmetic; only a fixed
    # sub-cent float-noise allowance is needed.
    return abs(left - right) <= _ABS_TOLERANCE


def _normal_zero(value: float) -> float:
    return 0.0 if value == 0 else value


def _text(value: Any, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    if value != value.strip():
        raise ValueError(f"{name} must not have surrounding whitespace")
    return value


def _side(value: Any, name: str = "side") -> str:
    result = _text(value, name).lower()
    if result not in ("buy", "sell"):
        raise ValueError(f"{name} must be 'buy' or 'sell'")
    if result != value:
        raise ValueError(f"{name} must use canonical lowercase spelling")
    return result


def _number(
    value: Any,
    name: str,
    *,
    nonnegative: bool = False,
    positive: bool = False,
) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite built-in number")
    if type(value) is int and abs(value) > _MAX_SAFE_JSON_INTEGER:
        raise ValueError(f"{name} exceeds the JSON safe-integer range")
    try:
        result = float(value)
    except (ValueError, OverflowError):
        raise ValueError(f"{name} must be finite") from None
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    if nonnegative and result < 0:
        raise ValueError(f"{name} must be non-negative")
    return _normal_zero(result)


def _integer(value: Any, name: str, *, positive: bool = False) -> int:
    if type(value) is not int:
        raise ValueError(f"{name} must be an exact integer")
    if abs(value) > _MAX_SAFE_JSON_INTEGER:
        raise ValueError(f"{name} exceeds the JSON safe-integer range")
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a {qualifier} integer")
    return value


def _naive_datetime(value: Any, name: str) -> datetime:
    if type(value) is not datetime:
        raise ValueError(f"{name} must be a datetime")
    if value.tzinfo is not None or value.fold != 0:
        raise ValueError(f"{name} must be timezone-naive with fold=0")
    return value


def _iso_date(value: Any, name: str) -> str:
    text = _text(value, name)
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{name} must use YYYY-MM-DD") from None
    if parsed.isoformat() != text:
        raise ValueError(f"{name} must use YYYY-MM-DD")
    return text


def _compact_to_iso(value: Any, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must use YYYYMMDD")
    try:
        parsed = datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise ValueError(f"{name} must use YYYYMMDD") from None
    return parsed.isoformat()


def _canonical_datetime(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _datetime_from_wire(value: Any, name: str) -> datetime:
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    try:
        result = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f")
    except ValueError:
        raise ValueError(
            f"{name} must use YYYY-MM-DDTHH:MM:SS.ffffff"
        ) from None
    if _canonical_datetime(result) != value:
        raise ValueError(
            f"{name} must use YYYY-MM-DDTHH:MM:SS.ffffff"
        )
    return result


def _exact_keys(value: Any, expected: Iterable[str], name: str) -> dict:
    if type(value) is not dict:
        raise ValueError(f"{name} must be exactly a dict")
    expected_set = frozenset(expected)
    actual_set = frozenset(value)
    if expected_set != actual_set:
        raise ValueError(
            f"{name} keys mismatch: "
            f"missing={sorted(expected_set - actual_set)}, "
            f"extra={sorted(actual_set - expected_set)}"
        )
    return value


def _unique_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _load_canonical_json(value: Any, name: str) -> Any:
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")
    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name} is invalid JSON: {exc}") from None
    if _canonical_json(decoded) != value:
        raise ValueError(f"{name} must use canonical JSON encoding")
    return decoded


@dataclass(frozen=True)
class CashReplayPendingT1:
    """One immutable pending T+1 quantity in the seed or replay state."""

    trade_date: str
    quantity: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "trade_date", _iso_date(self.trade_date, "trade_date")
        )
        object.__setattr__(
            self, "quantity", _integer(self.quantity, "quantity", positive=True)
        )

    def to_dict(self) -> dict:
        return {"quantity": self.quantity, "trade_date": self.trade_date}

    @classmethod
    def from_dict(cls, value: Any) -> "CashReplayPendingT1":
        payload = _exact_keys(value, ("quantity", "trade_date"), "pending_t1")
        restored = cls(
            trade_date=payload["trade_date"], quantity=payload["quantity"]
        )
        if _canonical_json(restored.to_dict()) != _canonical_json(payload):
            raise ValueError("pending_t1 payload is not canonical")
        return restored


@dataclass(frozen=True)
class CashReplayPositionSeed:
    """Complete economic state for one holding at the replay boundary."""

    symbol: str
    shares: int
    available_shares: int
    frozen_shares: int
    pending_t1: Tuple[CashReplayPendingT1, ...]
    current_price: float
    avg_cost: float
    cost_basis: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        shares = _integer(self.shares, "shares", positive=True)
        available = _integer(self.available_shares, "available_shares")
        frozen = _integer(self.frozen_shares, "frozen_shares")
        if type(self.pending_t1) is not tuple or any(
            type(item) is not CashReplayPendingT1 for item in self.pending_t1
        ):
            raise ValueError("pending_t1 must be a tuple of CashReplayPendingT1")
        dates = tuple(item.trade_date for item in self.pending_t1)
        if dates != tuple(sorted(dates)) or len(set(dates)) != len(dates):
            raise ValueError("pending_t1 dates must be unique and sorted")
        pending = sum(item.quantity for item in self.pending_t1)
        if available + frozen + pending != shares:
            raise ValueError(
                "available_shares + frozen_shares + pending_t1 must equal shares"
            )
        current_price = _number(
            self.current_price, "current_price", nonnegative=True
        )
        avg_cost = _number(self.avg_cost, "avg_cost", positive=True)
        cost_basis = _number(self.cost_basis, "cost_basis", positive=True)
        if not _same_amount(cost_basis / shares, avg_cost):
            raise ValueError("avg_cost must equal cost_basis / shares")
        object.__setattr__(self, "shares", shares)
        object.__setattr__(self, "available_shares", available)
        object.__setattr__(self, "frozen_shares", frozen)
        object.__setattr__(self, "current_price", current_price)
        object.__setattr__(self, "avg_cost", avg_cost)
        object.__setattr__(self, "cost_basis", cost_basis)

    def to_dict(self) -> dict:
        return {
            "available_shares": self.available_shares,
            "avg_cost": self.avg_cost,
            "cost_basis": self.cost_basis,
            "current_price": self.current_price,
            "frozen_shares": self.frozen_shares,
            "pending_t1": [item.to_dict() for item in self.pending_t1],
            "shares": self.shares,
            "symbol": self.symbol,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CashReplayPositionSeed":
        payload = _exact_keys(
            value,
            (
                "available_shares",
                "avg_cost",
                "cost_basis",
                "current_price",
                "frozen_shares",
                "pending_t1",
                "shares",
                "symbol",
            ),
            "position seed",
        )
        if type(payload["pending_t1"]) is not list:
            raise ValueError("position seed pending_t1 must be exactly a list")
        restored = cls(
            symbol=payload["symbol"],
            shares=payload["shares"],
            available_shares=payload["available_shares"],
            frozen_shares=payload["frozen_shares"],
            pending_t1=tuple(
                CashReplayPendingT1.from_dict(item)
                for item in payload["pending_t1"]
            ),
            current_price=payload["current_price"],
            avg_cost=payload["avg_cost"],
            cost_basis=payload["cost_basis"],
        )
        if _canonical_json(restored.to_dict()) != _canonical_json(payload):
            raise ValueError("position seed payload is not canonical")
        return restored


@dataclass(frozen=True)
class CashReplayOpenOrderSeed:
    """An active order that already exists at the replay boundary."""

    order_id: str
    symbol: str
    side: str
    declared_quantity: int
    filled_quantity: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", _text(self.order_id, "order_id"))
        object.__setattr__(self, "symbol", _text(self.symbol, "symbol"))
        object.__setattr__(self, "side", _side(self.side))
        declared = _integer(self.declared_quantity, "declared_quantity")
        filled = _integer(self.filled_quantity, "filled_quantity")
        if declared and filled > declared:
            raise ValueError("filled_quantity cannot exceed declared_quantity")
        object.__setattr__(self, "declared_quantity", declared)
        object.__setattr__(self, "filled_quantity", filled)

    def to_dict(self) -> dict:
        return {
            "declared_quantity": self.declared_quantity,
            "filled_quantity": self.filled_quantity,
            "order_id": self.order_id,
            "side": self.side,
            "symbol": self.symbol,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CashReplayOpenOrderSeed":
        payload = _exact_keys(
            value,
            (
                "declared_quantity",
                "filled_quantity",
                "order_id",
                "side",
                "symbol",
            ),
            "open order seed",
        )
        restored = cls(**payload)
        if _canonical_json(restored.to_dict()) != _canonical_json(payload):
            raise ValueError("open order seed payload is not canonical")
        return restored


@dataclass(frozen=True)
class CashReplayReservationSeed:
    """Identity and amount backing one initial cash/share freeze."""

    reservation_id: str
    order_id: str
    asset: str
    amount: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reservation_id",
            _text(self.reservation_id, "reservation_id"),
        )
        object.__setattr__(self, "order_id", _text(self.order_id, "order_id"))
        object.__setattr__(self, "asset", _text(self.asset, "asset"))
        object.__setattr__(
            self, "amount", _number(self.amount, "amount", positive=True)
        )

    def to_dict(self) -> dict:
        return {
            "amount": self.amount,
            "asset": self.asset,
            "order_id": self.order_id,
            "reservation_id": self.reservation_id,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "CashReplayReservationSeed":
        payload = _exact_keys(
            value,
            ("amount", "asset", "order_id", "reservation_id"),
            "reservation seed",
        )
        restored = cls(**payload)
        if _canonical_json(restored.to_dict()) != _canonical_json(payload):
            raise ValueError("reservation seed payload is not canonical")
        return restored


@dataclass(frozen=True)
class CashReplaySeed:
    """Versioned, canonical starting state for one cash-journal replay.

    ``initial_cash`` is the available cash at ``as_of`` (not the account's
    historical capital parameter).  ``initial_frozen_cash`` is stored
    separately.  Any initial freeze must be attributable to exactly one active
    order/reservation; otherwise :meth:`from_account` fails fast.
    """

    as_of: datetime
    account_id: str
    initial_cash: float
    initial_frozen_cash: float
    positions: Tuple[CashReplayPositionSeed, ...] = ()
    open_orders: Tuple[CashReplayOpenOrderSeed, ...] = ()
    reservations: Tuple[CashReplayReservationSeed, ...] = ()

    SCHEMA = CASH_REPLAY_SEED_SCHEMA
    SCHEMA_VERSION = CASH_REPLAY_SEED_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "as_of", _naive_datetime(self.as_of, "as_of"))
        object.__setattr__(
            self, "account_id", _text(self.account_id, "account_id")
        )
        object.__setattr__(
            self, "initial_cash", _number(self.initial_cash, "initial_cash")
        )
        object.__setattr__(
            self,
            "initial_frozen_cash",
            _number(
                self.initial_frozen_cash,
                "initial_frozen_cash",
                nonnegative=True,
            ),
        )
        for name, expected in (
            ("positions", CashReplayPositionSeed),
            ("open_orders", CashReplayOpenOrderSeed),
            ("reservations", CashReplayReservationSeed),
        ):
            value = getattr(self, name)
            if type(value) is not tuple or any(type(item) is not expected for item in value):
                raise ValueError(f"{name} must be a tuple of {expected.__name__}")

        positions = tuple(sorted(self.positions, key=lambda item: item.symbol))
        orders = tuple(sorted(self.open_orders, key=lambda item: item.order_id))
        reservations = tuple(
            sorted(
                self.reservations,
                key=lambda item: (item.order_id, item.asset, item.reservation_id),
            )
        )
        if len({item.symbol for item in positions}) != len(positions):
            raise ValueError("initial position symbols must be unique")
        if len({item.order_id for item in orders}) != len(orders):
            raise ValueError("initial order IDs must be unique")
        if len({item.reservation_id for item in reservations}) != len(reservations):
            raise ValueError("initial reservation IDs must be unique")
        if (
            positions != self.positions
            or orders != self.open_orders
            or reservations != self.reservations
        ):
            raise ValueError("seed collections must use canonical sorted order")

        order_map = {item.order_id: item for item in orders}
        position_map = {item.symbol: item for item in positions}
        cash_reserved = 0.0
        share_reserved: Dict[str, int] = {}
        seen_order_assets = set()
        for reservation in reservations:
            order = order_map.get(reservation.order_id)
            if order is None:
                raise ValueError("initial reservation references an unknown order")
            key = (reservation.order_id, reservation.asset)
            if key in seen_order_assets:
                raise ValueError("an initial order/asset may have only one reservation")
            seen_order_assets.add(key)
            if order.side == "buy":
                if reservation.asset != "CNY":
                    raise ValueError("initial BUY reservation asset must be CNY")
                cash_reserved += reservation.amount
            else:
                if reservation.asset != order.symbol:
                    raise ValueError("initial SELL reservation asset must equal symbol")
                quantity = int(reservation.amount)
                if float(quantity) != reservation.amount:
                    raise ValueError("initial share reservation amount must be integral")
                share_reserved[order.symbol] = (
                    share_reserved.get(order.symbol, 0) + quantity
                )
        if not _same_amount(cash_reserved, self.initial_frozen_cash):
            raise ValueError(
                "initial cash reservations must equal initial_frozen_cash"
            )
        for symbol, position in position_map.items():
            if share_reserved.get(symbol, 0) != position.frozen_shares:
                raise ValueError(
                    f"initial share reservations do not equal frozen_shares[{symbol}]"
                )
        unknown_frozen = set(share_reserved) - set(position_map)
        if unknown_frozen:
            raise ValueError(
                f"initial share reservations lack positions: {sorted(unknown_frozen)}"
            )

    @classmethod
    def from_account(
        cls,
        account,
        *,
        as_of: datetime,
        account_id: str = "cash-account",
    ) -> "CashReplaySeed":
        """Capture every replay-relevant field or reject the account state."""

        from .account import Account
        from .order import OrderStatus

        if type(account) is not Account:
            raise TypeError("account must be exactly an Account")
        as_of = _naive_datetime(as_of, "as_of")
        initial_cash = _number(account.cash, "account.cash")
        frozen_cash = _number(
            account.frozen_cash, "account.frozen_cash", nonnegative=True
        )

        positions = []
        for symbol in sorted(account.positions):
            position = account.positions[symbol]
            position._validate_state()
            if position.shares <= 0:
                raise CashReplayError(
                    f"account contains a non-positive position: {symbol}"
                )
            pending = tuple(
                CashReplayPendingT1(
                    trade_date=_compact_to_iso(trade_date, "pending trade_date"),
                    quantity=quantity,
                )
                for trade_date, quantity in sorted(position._pending_shares.items())
                if quantity > 0
            )
            positions.append(
                CashReplayPositionSeed(
                    symbol=symbol,
                    shares=position.shares,
                    available_shares=position.available_shares,
                    frozen_shares=position.frozen_shares,
                    pending_t1=pending,
                    current_price=position.current_price,
                    avg_cost=position.avg_cost,
                    cost_basis=position.cost_basis,
                )
            )

        orders = []
        reservations = []
        for order_id in sorted(account.orders):
            order = account.orders[order_id]
            if order.order_id != order_id:
                raise CashReplayError(
                    "account order mapping key does not match order_id"
                )
            if not order.is_open:
                raise CashReplayError(
                    f"account.orders contains a terminal order: {order_id}"
                )
            if order.status not in (
                OrderStatus.SUBMITTED,
                OrderStatus.PARTIAL,
            ):
                # A PENDING order has not crossed the broker acceptance
                # boundary.  The v1 seed schema represents only accepted
                # active orders, so inferring PENDING as accepted would be a
                # silent lifecycle mutation.
                raise CashReplayError(
                    f"account order is not replay-seed eligible: {order_id} "
                    f"({order.status.value})"
                )
            if (
                order.status is OrderStatus.SUBMITTED
                and order.filled_shares != 0
            ):
                raise CashReplayError(
                    f"submitted seed order has fills: {order_id}"
                )
            if (
                order.status is OrderStatus.PARTIAL
                and not (0 < order.filled_shares < order.shares)
            ):
                raise CashReplayError(
                    f"partial seed order has invalid filled quantity: {order_id}"
                )
            side = order.side.value
            orders.append(
                CashReplayOpenOrderSeed(
                    order_id=order_id,
                    symbol=order.symbol,
                    side=side,
                    declared_quantity=order.shares,
                    filled_quantity=order.filled_shares,
                )
            )
            if side == "buy" and order.frozen_amount > 0:
                reservations.append(
                    CashReplayReservationSeed(
                        reservation_id=f"reservation-{order_id}-cash",
                        order_id=order_id,
                        asset="CNY",
                        amount=order.frozen_amount,
                    )
                )
            if side == "sell" and order.frozen_shares > 0:
                reservations.append(
                    CashReplayReservationSeed(
                        reservation_id=f"reservation-{order_id}-shares",
                        order_id=order_id,
                        asset=order.symbol,
                        amount=float(order.frozen_shares),
                    )
                )

        return cls(
            as_of=as_of,
            account_id=account_id,
            initial_cash=initial_cash,
            initial_frozen_cash=frozen_cash,
            positions=tuple(positions),
            open_orders=tuple(orders),
            reservations=tuple(
                sorted(
                    reservations,
                    key=lambda item: (
                        item.order_id,
                        item.asset,
                        item.reservation_id,
                    ),
                )
            ),
        )

    @property
    def initial_market_value(self) -> float:
        value = sum(
            position.shares * position.current_price
            for position in self.positions
        )
        if not isfinite(value):
            raise CashReplayError("CashReplaySeed initial market value is not finite")
        return value

    @property
    def initial_nav(self) -> float:
        value = (
            self.initial_cash
            + self.initial_frozen_cash
            + self.initial_market_value
        )
        if not isfinite(value):
            raise CashReplayError("CashReplaySeed initial NAV is not finite")
        return value

    def to_dict(self) -> dict:
        return {
            "account_id": self.account_id,
            "as_of": _canonical_datetime(self.as_of),
            "initial_cash": self.initial_cash,
            "initial_frozen_cash": self.initial_frozen_cash,
            "open_orders": [item.to_dict() for item in self.open_orders],
            "positions": [item.to_dict() for item in self.positions],
            "reservations": [item.to_dict() for item in self.reservations],
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> "CashReplaySeed":
        payload = _exact_keys(
            value,
            (
                "account_id",
                "as_of",
                "initial_cash",
                "initial_frozen_cash",
                "open_orders",
                "positions",
                "reservations",
                "schema",
                "schema_version",
            ),
            "CashReplaySeed",
        )
        if payload["schema"] != CASH_REPLAY_SEED_SCHEMA:
            raise ValueError("CashReplaySeed schema mismatch")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != CASH_REPLAY_SEED_SCHEMA_VERSION
        ):
            raise ValueError("CashReplaySeed schema_version mismatch")
        for name in ("open_orders", "positions", "reservations"):
            if type(payload[name]) is not list:
                raise ValueError(f"CashReplaySeed {name} must be exactly a list")
        restored = cls(
            as_of=_datetime_from_wire(payload["as_of"], "CashReplaySeed as_of"),
            account_id=payload["account_id"],
            initial_cash=payload["initial_cash"],
            initial_frozen_cash=payload["initial_frozen_cash"],
            positions=tuple(
                CashReplayPositionSeed.from_dict(item)
                for item in payload["positions"]
            ),
            open_orders=tuple(
                CashReplayOpenOrderSeed.from_dict(item)
                for item in payload["open_orders"]
            ),
            reservations=tuple(
                CashReplayReservationSeed.from_dict(item)
                for item in payload["reservations"]
            ),
        )
        if restored.to_json() != _canonical_json(payload):
            raise ValueError("CashReplaySeed payload is not canonical")
        return restored

    @classmethod
    def from_json(cls, value: str) -> "CashReplaySeed":
        decoded = _load_canonical_json(value, "CashReplaySeed JSON")
        restored = cls.from_dict(decoded)
        if restored.to_json() != value:
            raise ValueError("CashReplaySeed JSON is not canonical")
        return restored


@dataclass(frozen=True)
class CashReplayPositionState:
    symbol: str
    shares: int
    available_shares: int
    frozen_shares: int
    pending_t1: Tuple[CashReplayPendingT1, ...]
    current_price: float
    avg_cost: float
    cost_basis: float

    @property
    def market_value(self) -> float:
        result = self.shares * self.current_price
        if not isfinite(result):
            raise CashReplayError(f"market value overflow for {self.symbol}")
        return result


@dataclass(frozen=True)
class CashReplayReservationState:
    reservation_id: str
    order_id: str
    asset: str
    amount: float


@dataclass(frozen=True)
class CashReplayOrderState:
    order_id: str
    symbol: str
    side: str
    status: str
    declared_quantity: int
    filled_quantity: int


@dataclass(frozen=True)
class CashReplayState:
    account_id: str
    cash: float
    frozen_cash: float
    positions: Tuple[CashReplayPositionState, ...]
    reservations: Tuple[CashReplayReservationState, ...]
    orders: Tuple[CashReplayOrderState, ...]
    last_sequence: int

    @property
    def market_value(self) -> float:
        value = sum(position.market_value for position in self.positions)
        if not isfinite(value):
            raise CashReplayError("replayed market value is not finite")
        return value

    @property
    def nav(self) -> float:
        value = self.cash + self.frozen_cash + self.market_value
        if not isfinite(value):
            raise CashReplayError("replayed NAV is not finite")
        return value

    def assert_matches_account(self, account) -> None:
        """Fail if this immutable replay state differs from a real Account."""

        from .account import Account
        from .order import OrderStatus

        if type(account) is not Account:
            raise TypeError("account must be exactly an Account")
        if not _same_amount(self.cash, _number(account.cash, "account.cash")):
            raise CashReplayError("replayed cash does not match Account.cash")
        if not _same_amount(
            self.frozen_cash,
            _number(account.frozen_cash, "account.frozen_cash", nonnegative=True),
        ):
            raise CashReplayError(
                "replayed frozen_cash does not match Account.frozen_cash"
            )
        actual_symbols = set(account.positions)
        replay_symbols = {position.symbol for position in self.positions}
        if actual_symbols != replay_symbols:
            raise CashReplayError(
                "replayed position symbols do not match Account.positions"
            )
        for state in self.positions:
            position = account.positions[state.symbol]
            actual_pending = tuple(
                CashReplayPendingT1(
                    _compact_to_iso(trade_date, "pending trade_date"), quantity
                )
                for trade_date, quantity in sorted(position._pending_shares.items())
                if quantity > 0
            )
            integer_fields = {
                "shares": position.shares,
                "available_shares": position.available_shares,
                "frozen_shares": position.frozen_shares,
            }
            for field_name, actual in integer_fields.items():
                if getattr(state, field_name) != actual:
                    raise CashReplayError(
                        f"replayed {field_name} differs for {state.symbol}"
                    )
            if state.pending_t1 != actual_pending:
                raise CashReplayError(
                    f"replayed pending_t1 differs for {state.symbol}"
                )
            for field_name in ("current_price", "avg_cost", "cost_basis"):
                if not _same_amount(
                    getattr(state, field_name),
                    _number(
                        getattr(position, field_name),
                        f"{field_name}[{state.symbol}]",
                        nonnegative=True,
                    ),
                ):
                    raise CashReplayError(
                        f"replayed {field_name} differs for {state.symbol}"
                    )

        replay_active_orders = {
            order.order_id: order
            for order in self.orders
            if order.status in ("accepted", "partial")
        }
        actual_active_orders = {
            order_id: order
            for order_id, order in account.orders.items()
            if order.is_open
        }
        if set(replay_active_orders) != set(actual_active_orders):
            raise CashReplayError(
                "replayed active order IDs do not match Account.orders"
            )
        expected_status = {
            OrderStatus.SUBMITTED: "accepted",
            OrderStatus.PARTIAL: "partial",
        }
        for order_id, state in replay_active_orders.items():
            order = actual_active_orders[order_id]
            actual_status = expected_status.get(order.status)
            if actual_status is None:
                raise CashReplayError(
                    f"Account.orders contains an unsupported active status: "
                    f"{order_id} ({order.status.value})"
                )
            if state.status != actual_status:
                raise CashReplayError(
                    f"replayed order status differs for {order_id}"
                )
            if state.symbol != order.symbol:
                raise CashReplayError(
                    f"replayed order symbol differs for {order_id}"
                )
            if state.side != order.side.value:
                raise CashReplayError(
                    f"replayed order side differs for {order_id}"
                )
            if state.declared_quantity != order.shares:
                raise CashReplayError(
                    f"replayed order quantity differs for {order_id}"
                )
            if state.filled_quantity != order.filled_shares:
                raise CashReplayError(
                    f"replayed filled quantity differs for {order_id}"
                )
        actual_nav = _number(account.total_asset, "account.total_asset")
        if not _same_amount(self.nav, actual_nav):
            raise CashReplayError("replayed NAV does not match Account.total_asset")


@dataclass(frozen=True)
class CashReplayFillView:
    event_id: str
    sequence: int
    simulated_time: datetime
    fill_id: str
    settlement_id: str
    order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    notional: float
    fee: float
    cash_delta: float
    t0: bool


@dataclass(frozen=True)
class CashReplayFeeView:
    fill_id: str
    order_id: str
    simulated_time: datetime
    commission: float
    stamp_duty: float
    transfer_fee: float
    total: float


@dataclass(frozen=True)
class CashReplayTradeView:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    simulated_time: datetime
    quantity: int
    price: float
    notional: float
    fee: float
    cash_delta: float
    realized_pnl: Optional[float]


@dataclass(frozen=True)
class CashClosedRoundTripView:
    symbol: str
    opened_at: Optional[datetime]
    closed_at: datetime
    first_event_sequence: int
    last_event_sequence: int
    fill_count: int
    entry_cost: float
    exit_proceeds: float
    recorded_fees: float
    realized_pnl: float
    is_win: bool


@dataclass(frozen=True)
class CashReplayResult:
    state: CashReplayState
    fills: Tuple[CashReplayFillView, ...]
    fees: Tuple[CashReplayFeeView, ...]
    trades: Tuple[CashReplayTradeView, ...]
    closed_round_trips: Tuple[CashClosedRoundTripView, ...]
    win_rate: Optional[float]


@dataclass
class _MutablePosition:
    symbol: str
    shares: int
    frozen_shares: int
    pending_t1: Dict[str, int]
    current_price: float
    avg_cost: float
    cost_basis: float

    @property
    def available_shares(self) -> int:
        return self.shares - self.frozen_shares - sum(self.pending_t1.values())


@dataclass
class _MutableOrder:
    order_id: str
    symbol: str
    side: str
    declared_quantity: int
    filled_quantity: int
    status: str
    accepted: bool


@dataclass
class _MutableReservation:
    reservation_id: str
    order_id: str
    asset: str
    amount: float


@dataclass
class _MutableRound:
    symbol: str
    opened_at: Optional[datetime]
    first_event_sequence: int
    fill_count: int
    entry_cost: float
    exit_proceeds: float
    recorded_fees: float
    realized_pnl: float


def _attributes(event: ExecutionEvent) -> Mapping[str, Any]:
    return MappingProxyType(dict(event.payload.attributes.entries))


def _required_attribute(
    attributes: Mapping[str, Any], key: str, event: ExecutionEvent
) -> Any:
    if key not in attributes:
        raise CashReplayError(
            f"{event.event_type.value} event {event.event_id} lacks required "
            f"attribute {key!r}"
        )
    return attributes[key]


def _event_amount(value: Any, name: str, event: ExecutionEvent, **kwargs) -> float:
    try:
        return _number(value, name, **kwargs)
    except ValueError as exc:
        raise CashReplayError(f"event {event.event_id}: {exc}") from None


class CashEventJournalReducer:
    """One-shot strict reducer for the cash event vocabulary."""

    def replay(
        self, seed: CashReplaySeed, journal: ExecutionEventJournal
    ) -> CashReplayResult:
        if type(seed) is not CashReplaySeed:
            raise TypeError("seed must be exactly a CashReplaySeed")
        if type(journal) is not ExecutionEventJournal:
            raise TypeError("journal must be exactly an ExecutionEventJournal")
        # Rebuild through the public validator so even a deliberately corrupted
        # in-memory journal cannot bypass sequence/order/ID checks.
        validated = ExecutionEventJournal(journal.snapshot())
        events = validated.snapshot()
        if events and events[0].simulated_time < seed.as_of:
            raise CashReplayError("journal begins before CashReplaySeed.as_of")

        cash = seed.initial_cash
        frozen_cash = seed.initial_frozen_cash
        positions: Dict[str, _MutablePosition] = {
            item.symbol: _MutablePosition(
                symbol=item.symbol,
                shares=item.shares,
                frozen_shares=item.frozen_shares,
                pending_t1={lot.trade_date: lot.quantity for lot in item.pending_t1},
                current_price=item.current_price,
                avg_cost=item.avg_cost,
                cost_basis=item.cost_basis,
            )
            for item in seed.positions
        }
        orders: Dict[str, _MutableOrder] = {
            item.order_id: _MutableOrder(
                order_id=item.order_id,
                symbol=item.symbol,
                side=item.side,
                declared_quantity=item.declared_quantity,
                filled_quantity=item.filled_quantity,
                status="partial" if item.filled_quantity else "accepted",
                accepted=True,
            )
            for item in seed.open_orders
        }
        reservations: Dict[str, _MutableReservation] = {
            item.reservation_id: _MutableReservation(
                item.reservation_id, item.order_id, item.asset, item.amount
            )
            for item in seed.reservations
        }
        rounds: Dict[str, _MutableRound] = {
            item.symbol: _MutableRound(
                symbol=item.symbol,
                opened_at=None,
                first_event_sequence=0,
                fill_count=0,
                entry_cost=item.cost_basis,
                exit_proceeds=0.0,
                recorded_fees=0.0,
                realized_pnl=0.0,
            )
            for item in seed.positions
        }

        fill_ids = set()
        settlement_ids = set()
        action_ids = set()
        fills = []
        fees = []
        trades = []
        closed_rounds = []

        index = 0
        while index < len(events):
            event = events[index]
            event_type = event.event_type
            if event_type in (
                ExecutionEventType.MARGIN,
                ExecutionEventType.ROLL,
            ):
                raise CashReplayError(
                    f"cash reducer does not support {event_type.value} events"
                )

            if event_type is ExecutionEventType.ORDER_CREATED:
                self._order_created(event, orders)
                index += 1
                continue
            if event_type is ExecutionEventType.ORDER_ACCEPTED:
                self._order_accepted(event, orders)
                index += 1
                continue
            if event_type in (
                ExecutionEventType.ORDER_REJECTED,
                ExecutionEventType.ORDER_CANCELED,
                ExecutionEventType.ORDER_EXPIRED,
            ):
                self._order_terminal(event, orders, reservations)
                index += 1
                continue
            if event_type is ExecutionEventType.RESERVE:
                cash, frozen_cash = self._reserve(
                    event,
                    cash,
                    frozen_cash,
                    positions,
                    orders,
                    reservations,
                )
                index += 1
                continue
            if event_type is ExecutionEventType.RELEASE:
                cash, frozen_cash = self._ordinary_release(
                    event,
                    cash,
                    frozen_cash,
                    positions,
                    orders,
                    reservations,
                )
                index += 1
                continue
            if event_type is ExecutionEventType.FILL:
                if index + 1 >= len(events):
                    raise CashReplayError(
                        f"fill {event.event_id} lacks Settlement event"
                    )
                settlement_event = events[index + 1]
                if settlement_event.event_type is not ExecutionEventType.SETTLEMENT:
                    raise CashReplayError(
                        "fill is not immediately followed by Settlement"
                    )
                if index + 2 >= len(events):
                    raise CashReplayError(
                        f"fill {event.event_id} lacks Release event"
                    )
                release_event = events[index + 2]
                (
                    cash,
                    frozen_cash,
                    fill_view,
                    fee_view,
                    trade_view,
                    closed,
                ) = self._fill_group(
                    event,
                    settlement_event,
                    release_event,
                    seed.account_id,
                    cash,
                    frozen_cash,
                    positions,
                    orders,
                    reservations,
                    rounds,
                    fill_ids,
                    settlement_ids,
                )
                fills.append(fill_view)
                fees.append(fee_view)
                trades.append(trade_view)
                if closed is not None:
                    closed_rounds.append(closed)
                index += 3
                continue
            if event_type is ExecutionEventType.SETTLEMENT:
                raise CashReplayError(
                    f"orphan settlement event: {event.event_id}"
                )
            if event_type is ExecutionEventType.T1_UNLOCK:
                self._t1_unlock(event, positions, seed.account_id)
                index += 1
                continue
            if event_type is ExecutionEventType.CORPORATE_ACTION:
                cash = self._corporate_action(
                    event,
                    cash,
                    positions,
                    orders,
                    reservations,
                    action_ids,
                )
                index += 1
                continue
            if event_type is ExecutionEventType.MARK:
                self._mark(event, positions)
                index += 1
                continue
            raise CashReplayError(f"unsupported cash event type: {event_type}")

        incomplete = sorted(
            order.order_id
            for order in orders.values()
            if order.status == "created"
        )
        if incomplete:
            raise CashReplayError(
                f"orders lack accepted/rejected fact: {incomplete}"
            )
        self._validate_all_state(cash, frozen_cash, positions, reservations)
        state = self._freeze_state(
            seed.account_id,
            cash,
            frozen_cash,
            positions,
            reservations,
            orders,
            len(events),
        )
        win_rate = None
        if closed_rounds:
            win_rate = sum(item.is_win for item in closed_rounds) / len(closed_rounds)
        return CashReplayResult(
            state=state,
            fills=tuple(fills),
            fees=tuple(fees),
            trades=tuple(trades),
            closed_round_trips=tuple(closed_rounds),
            win_rate=win_rate,
        )

    @staticmethod
    def _order_created(
        event: ExecutionEvent, orders: Dict[str, _MutableOrder]
    ) -> None:
        payload = event.payload
        assert type(payload) is OrderEventPayload
        if payload.order_id in orders:
            raise CashReplayError(f"duplicate order creation: {payload.order_id}")
        attrs = _attributes(event)
        side = _side(_required_attribute(attrs, "side", event))
        shares = _integer(
            _required_attribute(attrs, "shares", event), "order shares"
        )
        orders[payload.order_id] = _MutableOrder(
            order_id=payload.order_id,
            symbol=payload.symbol,
            side=side,
            declared_quantity=shares,
            filled_quantity=0,
            status="created",
            accepted=False,
        )

    @staticmethod
    def _order_accepted(
        event: ExecutionEvent, orders: Dict[str, _MutableOrder]
    ) -> None:
        payload = event.payload
        assert type(payload) is OrderEventPayload
        order = orders.get(payload.order_id)
        if order is None:
            raise CashReplayError("order accepted before creation")
        if order.symbol != payload.symbol or order.status != "created":
            raise CashReplayError("invalid or duplicate order acceptance")
        attrs = _attributes(event)
        if _side(_required_attribute(attrs, "side", event)) != order.side:
            raise CashReplayError("accepted order side differs from creation")
        order.accepted = True
        order.status = "accepted"

    @staticmethod
    def _order_terminal(
        event: ExecutionEvent,
        orders: Dict[str, _MutableOrder],
        reservations: Dict[str, _MutableReservation],
    ) -> None:
        payload = event.payload
        assert type(payload) is OrderEventPayload
        order = orders.get(payload.order_id)
        if order is None or order.symbol != payload.symbol:
            raise CashReplayError("terminal order fact lacks matching creation")
        if order.status in ("rejected", "canceled", "expired"):
            raise CashReplayError("duplicate terminal order fact")
        if event.event_type is not ExecutionEventType.ORDER_REJECTED and not order.accepted:
            raise CashReplayError("unaccepted order cannot be canceled or expired")
        if any(item.order_id == order.order_id for item in reservations.values()):
            raise CashReplayError("terminal order retains a reservation")
        terminal_status = {
            ExecutionEventType.ORDER_REJECTED: "rejected",
            ExecutionEventType.ORDER_CANCELED: "canceled",
            ExecutionEventType.ORDER_EXPIRED: "expired",
        }[event.event_type]
        attrs = _attributes(event)
        if "side" in attrs and _side(attrs["side"]) != order.side:
            raise CashReplayError("terminal order side differs from creation")
        if "filled_shares" in attrs:
            recorded = _integer(attrs["filled_shares"], "filled_shares")
            if recorded != order.filled_quantity:
                raise CashReplayError("terminal filled_shares differs from fills")
        order.status = terminal_status

    @staticmethod
    def _reservation_for_event(
        payload: ReservationPayload,
        reservations: Dict[str, _MutableReservation],
    ) -> _MutableReservation:
        reservation = reservations.get(payload.reservation_id)
        if reservation is None:
            raise CashReplayError(
                f"unknown reservation: {payload.reservation_id}"
            )
        if (
            reservation.order_id != payload.order_id
            or reservation.asset != payload.asset
        ):
            raise CashReplayError("reservation identity/correlation changed")
        return reservation

    def _reserve(
        self,
        event: ExecutionEvent,
        cash: float,
        frozen_cash: float,
        positions: Dict[str, _MutablePosition],
        orders: Dict[str, _MutableOrder],
        reservations: Dict[str, _MutableReservation],
    ) -> Tuple[float, float]:
        payload = event.payload
        assert type(payload) is ReservationPayload
        order = orders.get(payload.order_id)
        if order is None or not order.accepted or order.status in (
            "rejected",
            "canceled",
            "expired",
            "filled",
        ):
            raise CashReplayError("reservation requires an active accepted order")
        existing = reservations.get(payload.reservation_id)
        if existing is not None and (
            existing.order_id != payload.order_id or existing.asset != payload.asset
        ):
            raise CashReplayError("reservation ID was reused with different identity")
        amount = payload.amount
        if order.side == "buy":
            if payload.asset != "CNY":
                raise CashReplayError("BUY reservation asset must be CNY")
            if amount > cash + _ABS_TOLERANCE:
                raise CashReplayError("cash reservation overdraws available cash")
            cash = _normal_zero(cash - amount)
            frozen_cash += amount
        else:
            if payload.asset != order.symbol:
                raise CashReplayError("SELL reservation asset must equal symbol")
            quantity = int(amount)
            if float(quantity) != amount:
                raise CashReplayError("share reservation amount must be integral")
            position = positions.get(order.symbol)
            if position is None or quantity > position.available_shares:
                raise CashReplayError("share reservation overdraws available shares")
            position.frozen_shares += quantity
        if existing is None:
            reservations[payload.reservation_id] = _MutableReservation(
                payload.reservation_id, payload.order_id, payload.asset, amount
            )
        else:
            existing.amount += amount
        self._validate_all_state(cash, frozen_cash, positions, reservations)
        return cash, frozen_cash

    def _ordinary_release(
        self,
        event: ExecutionEvent,
        cash: float,
        frozen_cash: float,
        positions: Dict[str, _MutablePosition],
        orders: Dict[str, _MutableOrder],
        reservations: Dict[str, _MutableReservation],
    ) -> Tuple[float, float]:
        payload = event.payload
        assert type(payload) is ReservationPayload
        attrs = _attributes(event)
        if attrs.get("release_reason") == "fill_settlement" or "fill_id" in attrs:
            raise CashReplayError("fill-linked release is orphaned from its fill")
        order = orders.get(payload.order_id)
        if order is None:
            raise CashReplayError("release references an unknown order")
        reservation = self._reservation_for_event(payload, reservations)
        if payload.amount > reservation.amount + _ABS_TOLERANCE:
            raise CashReplayError("release exceeds outstanding reservation")
        if payload.asset == "CNY":
            if order.side != "buy" or payload.amount > frozen_cash + _ABS_TOLERANCE:
                raise CashReplayError("cash release exceeds frozen cash")
            frozen_cash = max(0.0, frozen_cash - payload.amount)
            cash += payload.amount
        else:
            if order.side != "sell" or payload.asset != order.symbol:
                raise CashReplayError("share release side/asset mismatch")
            quantity = int(payload.amount)
            if float(quantity) != payload.amount:
                raise CashReplayError("share release amount must be integral")
            position = positions.get(order.symbol)
            if position is None or quantity > position.frozen_shares:
                raise CashReplayError("share release exceeds frozen shares")
            position.frozen_shares -= quantity
        self._decrease_reservation(reservations, reservation, payload.amount)
        self._validate_all_state(cash, frozen_cash, positions, reservations)
        return cash, frozen_cash

    def _fill_group(
        self,
        fill_event: ExecutionEvent,
        settlement_event: ExecutionEvent,
        release_event: ExecutionEvent,
        account_id: str,
        cash: float,
        frozen_cash: float,
        positions: Dict[str, _MutablePosition],
        orders: Dict[str, _MutableOrder],
        reservations: Dict[str, _MutableReservation],
        rounds: Dict[str, _MutableRound],
        fill_ids: set,
        settlement_ids: set,
    ):
        fill = fill_event.payload
        assert type(fill) is FillPayload
        if settlement_event.event_type is not ExecutionEventType.SETTLEMENT:
            raise CashReplayError("fill is not immediately followed by Settlement")
        if release_event.event_type is not ExecutionEventType.RELEASE:
            raise CashReplayError("fill Settlement is not followed by Release")
        settlement = settlement_event.payload
        release = release_event.payload
        assert type(settlement) is SettlementPayload
        assert type(release) is ReservationPayload
        if not (
            fill_event.simulated_time == settlement_event.simulated_time == release_event.simulated_time
            and fill_event.phase_seq == settlement_event.phase_seq == release_event.phase_seq
        ):
            raise CashReplayError("Fill/Settlement/Release must share time and phase")
        if fill.fill_id in fill_ids:
            raise CashReplayError(f"duplicate fill_id: {fill.fill_id}")
        if settlement.settlement_id in settlement_ids:
            raise CashReplayError(
                f"duplicate settlement_id: {settlement.settlement_id}"
            )
        order = orders.get(fill.order_id)
        if order is None or not order.accepted or order.status in (
            "rejected",
            "canceled",
            "expired",
            "filled",
        ):
            raise CashReplayError("fill requires an active accepted order")
        if order.symbol != fill.symbol:
            raise CashReplayError("fill symbol differs from order")

        attrs = _attributes(fill_event)
        side = _side(_required_attribute(attrs, "side", fill_event))
        if side != order.side:
            raise CashReplayError("fill side differs from order")
        t0 = _required_attribute(attrs, "t0", fill_event)
        if type(t0) is not bool:
            raise CashReplayError("fill attribute t0 must be an exact bool")
        trade_date_raw = _required_attribute(attrs, "trade_date", fill_event)
        trade_date = _compact_to_iso(trade_date_raw, "fill trade_date")
        if trade_date != fill_event.simulated_time.date().isoformat():
            raise CashReplayError(
                "fill trade_date differs from simulated_time date"
            )
        components = {}
        for name in ("commission", "stamp_duty", "transfer_fee"):
            components[name] = _event_amount(
                _required_attribute(attrs, name, fill_event),
                name,
                fill_event,
                nonnegative=True,
            )
        expected_fee = round(sum(components.values()), 2)
        if not _same_amount(expected_fee, fill.fee):
            raise CashReplayError("fill fee differs from recorded fee components")
        expected_notional = fill.quantity * fill.price
        if not isfinite(expected_notional) or not _same_amount(
            expected_notional, fill.amount
        ):
            raise CashReplayError("fill amount does not equal quantity * price")

        settlement_attrs = _attributes(settlement_event)
        if settlement.account_id != account_id:
            raise CashReplayError("settlement account_id differs from seed")
        if settlement.asset != "CNY":
            raise CashReplayError("cash fill settlement asset must be CNY")
        if _required_attribute(settlement_attrs, "fill_id", settlement_event) != fill.fill_id:
            raise CashReplayError("settlement fill_id correlation mismatch")
        if _required_attribute(settlement_attrs, "order_id", settlement_event) != fill.order_id:
            raise CashReplayError("settlement order_id correlation mismatch")
        gross = _event_amount(
            _required_attribute(settlement_attrs, "gross_notional", settlement_event),
            "gross_notional",
            settlement_event,
            positive=True,
        )
        recorded_fee = _event_amount(
            _required_attribute(settlement_attrs, "fee", settlement_event),
            "settlement fee",
            settlement_event,
            nonnegative=True,
        )
        if not _same_amount(gross, fill.amount) or not _same_amount(recorded_fee, fill.fee):
            raise CashReplayError("settlement gross/fee differs from fill")
        if (
            settlement.settlement_date
            != fill_event.simulated_time.date().isoformat()
        ):
            raise CashReplayError("settlement_date differs from simulated fill date")

        release_attrs = _attributes(release_event)
        if release_attrs.get("release_reason") != "fill_settlement":
            raise CashReplayError("fill release must use release_reason=fill_settlement")
        if _required_attribute(release_attrs, "fill_id", release_event) != fill.fill_id:
            raise CashReplayError("release fill_id correlation mismatch")
        if release.order_id != fill.order_id:
            raise CashReplayError("release order_id correlation mismatch")
        reservation = self._reservation_for_event(release, reservations)
        if release.amount > reservation.amount + _ABS_TOLERANCE:
            raise CashReplayError("fill release exceeds outstanding reservation")

        realized_pnl = None
        closed = None
        if side == "buy":
            if settlement.direction != "debit":
                raise CashReplayError("BUY settlement must be a debit")
            expected_cash_amount = fill.amount + fill.fee
            if not _same_amount(settlement.amount, expected_cash_amount):
                raise CashReplayError("BUY settlement violates cash identity")
            if release.asset != "CNY":
                raise CashReplayError("BUY fill release asset must be CNY")
            if release.amount + _ABS_TOLERANCE < settlement.amount:
                raise CashReplayError("BUY fill consumes more than released reservation")
            known_post_fill = order.filled_quantity + fill.quantity
            if order.declared_quantity:
                if known_post_fill < order.declared_quantity and not _same_amount(
                    release.amount, settlement.amount
                ):
                    raise CashReplayError(
                        "partial BUY fill must consume exactly its settlement"
                    )
                if known_post_fill == order.declared_quantity and not _same_amount(
                    release.amount, reservation.amount
                ):
                    raise CashReplayError(
                        "completed BUY fill must close its reservation"
                    )
            if release.amount > frozen_cash + _ABS_TOLERANCE:
                raise CashReplayError("BUY fill release exceeds frozen cash")
            refund = release.amount - settlement.amount
            if refund < -_ABS_TOLERANCE:
                raise CashReplayError("BUY fill has a negative reservation refund")
            cash += max(0.0, refund)
            frozen_cash = max(0.0, frozen_cash - release.amount)
            old = positions.get(fill.symbol)
            if old is None:
                old = _MutablePosition(fill.symbol, 0, 0, {}, 0.0, 0.0, 0.0)
                positions[fill.symbol] = old
            was_flat = old.shares == 0
            cost = settlement.amount
            old.shares += fill.quantity
            old.cost_basis += cost
            old.avg_cost = old.cost_basis / old.shares
            # Cash Position marks immediately to the actual execution price;
            # a later bar Mark may replace it, but replay must not need one.
            old.current_price = fill.price
            if not t0:
                old.pending_t1[trade_date] = (
                    old.pending_t1.get(trade_date, 0) + fill.quantity
                )
            if was_flat:
                rounds[fill.symbol] = _MutableRound(
                    symbol=fill.symbol,
                    opened_at=fill_event.simulated_time,
                    first_event_sequence=fill_event.sequence,
                    fill_count=0,
                    entry_cost=0.0,
                    exit_proceeds=0.0,
                    recorded_fees=0.0,
                    realized_pnl=0.0,
                )
            round_state = rounds[fill.symbol]
            round_state.entry_cost += cost
            cash_delta = -settlement.amount
        else:
            if settlement.direction != "credit":
                raise CashReplayError("SELL settlement must be a credit")
            expected_cash_amount = fill.amount - fill.fee
            if expected_cash_amount <= 0 or not _same_amount(
                settlement.amount, expected_cash_amount
            ):
                raise CashReplayError("SELL settlement violates cash identity")
            if release.asset != fill.symbol:
                raise CashReplayError("SELL fill release asset must equal symbol")
            if not _same_amount(release.amount, float(fill.quantity)):
                raise CashReplayError("SELL fill release must equal fill quantity")
            position = positions.get(fill.symbol)
            if (
                position is None
                or fill.quantity > position.shares
                or fill.quantity > position.frozen_shares
            ):
                raise CashReplayError("SELL fill overdraws reserved position")
            sold_cost = position.avg_cost * fill.quantity
            realized_pnl = settlement.amount - sold_cost
            cash += settlement.amount
            position.frozen_shares -= fill.quantity
            position.shares -= fill.quantity
            position.cost_basis = max(0.0, position.cost_basis - sold_cost)
            if position.shares == 0:
                if position.frozen_shares or position.pending_t1:
                    raise CashReplayError("flat position retains frozen/pending shares")
                position.avg_cost = 0.0
                del positions[fill.symbol]
            else:
                position.avg_cost = position.cost_basis / position.shares
                position.current_price = fill.price
            round_state = rounds.get(fill.symbol)
            if round_state is None:
                raise CashReplayError("SELL fill lacks an open inventory round")
            round_state.exit_proceeds += settlement.amount
            round_state.realized_pnl += realized_pnl
            cash_delta = settlement.amount

        round_state.fill_count += 1
        round_state.recorded_fees += fill.fee
        self._decrease_reservation(reservations, reservation, release.amount)
        order.filled_quantity += fill.quantity
        if order.declared_quantity and order.filled_quantity > order.declared_quantity:
            raise CashReplayError("fills exceed order declared quantity")
        has_reservation = any(
            item.order_id == order.order_id
            for item in reservations.values()
        )
        if order.declared_quantity:
            if order.filled_quantity == order.declared_quantity:
                if has_reservation:
                    raise CashReplayError(
                        "fully filled declared order retains a reservation"
                    )
                order.status = "filled"
            else:
                if not has_reservation:
                    raise CashReplayError(
                        "declared order exhausted its reservation before its "
                        "declared quantity was filled"
                    )
                order.status = "partial"
        else:
            # Deferred/dynamic-size orders historically publish shares=0 at
            # creation. For that explicit schema case only, reservation
            # exhaustion remains the terminal fill fact.
            order.status = "partial" if has_reservation else "filled"
        fill_ids.add(fill.fill_id)
        settlement_ids.add(settlement.settlement_id)

        if side == "sell" and fill.symbol not in positions:
            reconciliation = round_state.exit_proceeds - round_state.entry_cost
            if not _same_amount(reconciliation, round_state.realized_pnl):
                raise CashReplayError("closed inventory round violates PnL identity")
            closed = CashClosedRoundTripView(
                symbol=fill.symbol,
                opened_at=round_state.opened_at,
                closed_at=fill_event.simulated_time,
                first_event_sequence=round_state.first_event_sequence,
                last_event_sequence=fill_event.sequence,
                fill_count=round_state.fill_count,
                entry_cost=round_state.entry_cost,
                exit_proceeds=round_state.exit_proceeds,
                recorded_fees=round_state.recorded_fees,
                realized_pnl=round_state.realized_pnl,
                is_win=round_state.realized_pnl > 0,
            )
            del rounds[fill.symbol]

        self._validate_all_state(cash, frozen_cash, positions, reservations)
        fill_view = CashReplayFillView(
            event_id=fill_event.event_id,
            sequence=fill_event.sequence,
            simulated_time=fill_event.simulated_time,
            fill_id=fill.fill_id,
            settlement_id=settlement.settlement_id,
            order_id=fill.order_id,
            symbol=fill.symbol,
            side=side,
            quantity=fill.quantity,
            price=fill.price,
            notional=fill.amount,
            fee=fill.fee,
            cash_delta=cash_delta,
            t0=t0,
        )
        fee_view = CashReplayFeeView(
            fill_id=fill.fill_id,
            order_id=fill.order_id,
            simulated_time=fill_event.simulated_time,
            commission=components["commission"],
            stamp_duty=components["stamp_duty"],
            transfer_fee=components["transfer_fee"],
            total=fill.fee,
        )
        trade_view = CashReplayTradeView(
            fill_id=fill.fill_id,
            order_id=fill.order_id,
            symbol=fill.symbol,
            side=side,
            simulated_time=fill_event.simulated_time,
            quantity=fill.quantity,
            price=fill.price,
            notional=fill.amount,
            fee=fill.fee,
            cash_delta=cash_delta,
            realized_pnl=realized_pnl,
        )
        return cash, frozen_cash, fill_view, fee_view, trade_view, closed

    @staticmethod
    def _decrease_reservation(
        reservations: Dict[str, _MutableReservation],
        reservation: _MutableReservation,
        amount: float,
    ) -> None:
        remaining = reservation.amount - amount
        if remaining < -_ABS_TOLERANCE:
            raise CashReplayError("reservation became negative")
        if remaining <= _ABS_TOLERANCE:
            reservations.pop(reservation.reservation_id, None)
        else:
            reservation.amount = remaining

    @staticmethod
    def _t1_unlock(
        event: ExecutionEvent,
        positions: Dict[str, _MutablePosition],
        account_id: str,
    ) -> None:
        payload = event.payload
        assert type(payload) is T1UnlockPayload
        if payload.account_id != account_id:
            raise CashReplayError("T1 unlock account_id differs from seed")
        position = positions.get(payload.symbol)
        if position is None:
            raise CashReplayError("T1 unlock references a missing position")
        unlock = sum(
            quantity
            for trade_date, quantity in position.pending_t1.items()
            if trade_date < payload.trade_date
        )
        if unlock != payload.quantity:
            raise CashReplayError("T1 unlock quantity differs from pending lots")
        position.pending_t1 = {
            trade_date: quantity
            for trade_date, quantity in position.pending_t1.items()
            if trade_date >= payload.trade_date
        }

    @staticmethod
    def _corporate_action(
        event: ExecutionEvent,
        cash: float,
        positions: Dict[str, _MutablePosition],
        orders: Dict[str, _MutableOrder],
        reservations: Dict[str, _MutableReservation],
        action_ids: set,
    ) -> float:
        payload = event.payload
        assert type(payload) is CorporateActionPayload
        if payload.action_id in action_ids:
            raise CashReplayError(f"duplicate action_id: {payload.action_id}")
        action_ids.add(payload.action_id)
        if payload.action_type not in (
            "split_ratio", "adjustment_factor_total_return"
        ):
            raise CashReplayError("unsupported cash corporate action type")
        if payload.effective_date != event.simulated_time.date().isoformat():
            raise CashReplayError("corporate action date differs from simulated_time")
        attrs = _attributes(event)
        fractional_policy = _required_attribute(
            attrs, "fractional_policy", event)
        expected_policy = (
            "reject" if payload.action_type == "split_ratio"
            else "cash_in_lieu"
        )
        if fractional_policy != expected_policy:
            raise CashReplayError("unsupported fractional corporate-action policy")
        ratio = _event_amount(
            _required_attribute(attrs, "ratio", event),
            "split ratio",
            event,
            positive=True,
        )
        old_shares = _integer(
            _required_attribute(attrs, "old_shares", event), "old_shares"
        )
        new_shares = _integer(
            _required_attribute(attrs, "new_shares", event), "new_shares"
        )
        old_price = _event_amount(
            _required_attribute(attrs, "old_price", event),
            "old_price",
            event,
            nonnegative=True,
        )
        new_price = _event_amount(
            _required_attribute(attrs, "new_price", event),
            "new_price",
            event,
            nonnegative=True,
        )
        if any(
            reservation.order_id in orders
            and orders[reservation.order_id].symbol == payload.symbol
            for reservation in reservations.values()
        ):
            raise CashReplayError("corporate action retained a symbol reservation")
        if any(
            order.symbol == payload.symbol
            and order.status not in ("rejected", "canceled", "expired", "filled")
            for order in orders.values()
        ):
            raise CashReplayError("corporate action retained an active symbol order")
        position = positions.get(payload.symbol)
        if position is None:
            if any((old_shares, new_shares, old_price, new_price)):
                raise CashReplayError("positionless corporate action has non-zero state")
            if payload.action_type == "adjustment_factor_total_return":
                cash_amount = _event_amount(
                    _required_attribute(attrs, "cash_amount", event),
                    "cash in lieu", event, nonnegative=True)
                if cash_amount != 0:
                    raise CashReplayError(
                        "positionless factor action has non-zero cash")
                if _required_attribute(
                    attrs, "valuation_model", event
                ) != "immediate_total_return_reinvestment":
                    raise CashReplayError(
                        "unsupported factor-action valuation model")
            return cash
        if position.shares != old_shares or not _same_amount(
            position.current_price, old_price
        ):
            raise CashReplayError("corporate action old state differs from replay")
        try:
            ratio_decimal = Decimal(str(ratio))
        except (InvalidOperation, ValueError):
            raise CashReplayError("corporate action ratio is not replayable") from None

        if payload.action_type == "split_ratio":
            def scaled(quantity: int, name: str) -> int:
                entitlement = Decimal(quantity) * ratio_decimal
                integral = entitlement.to_integral_value()
                if entitlement != integral:
                    raise CashReplayError(
                        f"fractional split entitlement while replaying {name}"
                    )
                return int(integral)

            expected_shares = scaled(position.shares, "shares")
            if expected_shares != new_shares or expected_shares <= 0:
                raise CashReplayError("corporate action new_shares mismatch")
            position.frozen_shares = scaled(
                position.frozen_shares, "frozen_shares")
            position.pending_t1 = {
                trade_date: scaled(quantity, f"pending_t1[{trade_date}]")
                for trade_date, quantity in position.pending_t1.items()
            }
            expected_price = (
                old_shares * old_price / expected_shares
                if old_price > 0 else 0.0
            )
        else:
            if _required_attribute(
                attrs, "valuation_model", event
            ) != "immediate_total_return_reinvestment":
                raise CashReplayError(
                    "unsupported factor-action valuation model")

            def scaled_down(quantity: int) -> int:
                return int(Decimal(quantity) * ratio_decimal)

            expected_shares = scaled_down(position.shares)
            if expected_shares != new_shares or expected_shares <= 0:
                raise CashReplayError("factor action new_shares mismatch")
            position.frozen_shares = scaled_down(position.frozen_shares)
            position.pending_t1 = {
                trade_date: scaled_down(quantity)
                for trade_date, quantity in position.pending_t1.items()
            }
            expected_price = old_price / ratio
            cash_amount = _event_amount(
                _required_attribute(attrs, "cash_amount", event),
                "cash in lieu", event, nonnegative=True)
            expected_cash = (
                float(Decimal(old_shares) * ratio_decimal - new_shares)
                * expected_price
            )
            if not _same_amount(expected_cash, cash_amount):
                raise CashReplayError("factor action cash-in-lieu mismatch")
            cash += cash_amount

        position.shares = expected_shares
        position.avg_cost = position.cost_basis / expected_shares
        if not _same_amount(expected_price, new_price):
            raise CashReplayError("corporate action new_price mismatch")
        position.current_price = new_price
        return cash

    @staticmethod
    def _mark(
        event: ExecutionEvent, positions: Dict[str, _MutablePosition]
    ) -> None:
        payload = event.payload
        assert type(payload) is MarkPayload
        position = positions.get(payload.symbol)
        if position is None:
            raise CashReplayError("mark references a missing position")
        position.current_price = payload.price

    @staticmethod
    def _validate_all_state(
        cash: float,
        frozen_cash: float,
        positions: Dict[str, _MutablePosition],
        reservations: Dict[str, _MutableReservation],
    ) -> None:
        if not isfinite(cash):
            raise CashReplayError("replayed cash is not finite")
        if not isfinite(frozen_cash) or frozen_cash < -_ABS_TOLERANCE:
            raise CashReplayError("replayed frozen cash is invalid")
        cash_reservations = sum(
            item.amount for item in reservations.values() if item.asset == "CNY"
        )
        if not _same_amount(cash_reservations, frozen_cash):
            raise CashReplayError("cash reservations do not equal frozen cash")
        share_reservations: Dict[str, float] = {}
        for reservation in reservations.values():
            if not isfinite(reservation.amount) or reservation.amount <= 0:
                raise CashReplayError("reservation amount is invalid")
            if reservation.asset != "CNY":
                share_reservations[reservation.asset] = (
                    share_reservations.get(reservation.asset, 0.0)
                    + reservation.amount
                )
        for symbol, position in positions.items():
            values = (
                position.current_price,
                position.avg_cost,
                position.cost_basis,
            )
            if any(not isfinite(value) for value in values):
                raise CashReplayError(f"non-finite position state: {symbol}")
            if position.shares <= 0 or position.frozen_shares < 0:
                raise CashReplayError(f"invalid position quantity: {symbol}")
            if position.avg_cost <= 0 or position.cost_basis <= 0:
                raise CashReplayError(f"invalid position cost state: {symbol}")
            if position.current_price < 0:
                raise CashReplayError(f"invalid position mark: {symbol}")
            if any(quantity <= 0 for quantity in position.pending_t1.values()):
                raise CashReplayError(f"invalid pending T1 quantity: {symbol}")
            if position.available_shares < 0:
                raise CashReplayError(f"negative available shares: {symbol}")
            if not _same_amount(position.cost_basis / position.shares, position.avg_cost):
                raise CashReplayError(f"position average cost identity failed: {symbol}")
            if not _same_amount(
                share_reservations.get(symbol, 0.0),
                float(position.frozen_shares),
            ):
                raise CashReplayError(
                    f"share reservations do not equal frozen shares: {symbol}"
                )
        unknown_share_assets = set(share_reservations) - set(positions)
        if unknown_share_assets:
            raise CashReplayError(
                f"share reservations lack positions: {sorted(unknown_share_assets)}"
            )
        nav = cash + frozen_cash + sum(
            position.shares * position.current_price
            for position in positions.values()
        )
        if not isfinite(nav):
            raise CashReplayError("replayed NAV is not finite")

    @staticmethod
    def _freeze_state(
        account_id: str,
        cash: float,
        frozen_cash: float,
        positions: Dict[str, _MutablePosition],
        reservations: Dict[str, _MutableReservation],
        orders: Dict[str, _MutableOrder],
        last_sequence: int,
    ) -> CashReplayState:
        frozen_positions = tuple(
            CashReplayPositionState(
                symbol=symbol,
                shares=position.shares,
                available_shares=position.available_shares,
                frozen_shares=position.frozen_shares,
                pending_t1=tuple(
                    CashReplayPendingT1(trade_date, quantity)
                    for trade_date, quantity in sorted(position.pending_t1.items())
                ),
                current_price=position.current_price,
                avg_cost=position.avg_cost,
                cost_basis=position.cost_basis,
            )
            for symbol, position in sorted(positions.items())
        )
        frozen_reservations = tuple(
            CashReplayReservationState(
                item.reservation_id, item.order_id, item.asset, item.amount
            )
            for item in sorted(
                reservations.values(),
                key=lambda item: (item.order_id, item.asset, item.reservation_id),
            )
        )
        frozen_orders = tuple(
            CashReplayOrderState(
                item.order_id,
                item.symbol,
                item.side,
                item.status,
                item.declared_quantity,
                item.filled_quantity,
            )
            for item in sorted(orders.values(), key=lambda item: item.order_id)
        )
        return CashReplayState(
            account_id=account_id,
            cash=_normal_zero(cash),
            frozen_cash=_normal_zero(frozen_cash),
            positions=frozen_positions,
            reservations=frozen_reservations,
            orders=frozen_orders,
            last_sequence=last_sequence,
        )


def replay_cash_journal(
    seed: CashReplaySeed, journal: ExecutionEventJournal
) -> CashReplayResult:
    """Convenience entry point for strict cash-economic replay."""

    return CashEventJournalReducer().replay(seed, journal)


@dataclass(frozen=True)
class CashAuditBundle:
    """Immutable, versioned cash replay evidence attached to a result.

    The seed itself is frozen and the journal is retained as canonical JSON,
    rather than as a caller-mutable :class:`ExecutionEventJournal` reference.
    Construction performs a strict economic replay, so a bundle can never
    represent a syntactically valid but economically incomplete event stream.
    """

    seed: CashReplaySeed
    journal_json: str

    SCHEMA = CASH_AUDIT_BUNDLE_SCHEMA
    SCHEMA_VERSION = CASH_AUDIT_BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.seed) is not CashReplaySeed:
            raise TypeError("seed must be exactly a CashReplaySeed")
        if type(self.journal_json) is not str:
            raise TypeError("journal_json must be exactly str")
        journal = ExecutionEventJournal.from_json(self.journal_json)
        # Validate the complete economic stream at the evidence boundary.
        replay_cash_journal(self.seed, journal)

    @classmethod
    def from_parts(
        cls,
        seed: CashReplaySeed,
        journal: ExecutionEventJournal,
    ) -> "CashAuditBundle":
        if type(seed) is not CashReplaySeed:
            raise TypeError("seed must be exactly a CashReplaySeed")
        if type(journal) is not ExecutionEventJournal:
            raise TypeError(
                "journal must be exactly an ExecutionEventJournal"
            )
        # to_json() is also a point-in-time immutable snapshot.
        return cls(seed=seed, journal_json=journal.to_json())

    @property
    def journal(self) -> ExecutionEventJournal:
        """Return a newly validated journal; callers cannot mutate the bundle."""

        return ExecutionEventJournal.from_json(self.journal_json)

    def replay(self) -> CashReplayResult:
        return replay_cash_journal(self.seed, self.journal)

    def to_dict(self) -> dict:
        return {
            "journal": self.journal.to_dict(),
            "schema": self.SCHEMA,
            "schema_version": self.SCHEMA_VERSION,
            "seed": self.seed.to_dict(),
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> "CashAuditBundle":
        payload = _exact_keys(
            value,
            ("journal", "schema", "schema_version", "seed"),
            "CashAuditBundle",
        )
        if payload["schema"] != CASH_AUDIT_BUNDLE_SCHEMA:
            raise ValueError("CashAuditBundle schema mismatch")
        if (
            type(payload["schema_version"]) is not int
            or payload["schema_version"] != CASH_AUDIT_BUNDLE_SCHEMA_VERSION
        ):
            raise ValueError("CashAuditBundle schema_version mismatch")
        seed = CashReplaySeed.from_dict(payload["seed"])
        journal = ExecutionEventJournal.from_dict(payload["journal"])
        restored = cls.from_parts(seed, journal)
        if restored.to_json() != _canonical_json(payload):
            raise ValueError("CashAuditBundle payload is not canonical")
        return restored

    @classmethod
    def from_json(cls, value: str) -> "CashAuditBundle":
        decoded = _load_canonical_json(value, "CashAuditBundle JSON")
        restored = cls.from_dict(decoded)
        if restored.to_json() != value:
            raise ValueError("CashAuditBundle JSON is not canonical")
        return restored


def cash_replay_trade_records(bundle: CashAuditBundle) -> list:
    """Project the strict fill ledger into the historical public trade schema.

    Every economic value comes from the replay result or the persisted event
    attributes.  No commission, tax, transfer fee, date, price, or PnL is
    recomputed from an Order request.  The compatibility row grain is now one
    actual fill, which keeps partial fills and cross-day fills observable.
    """

    if type(bundle) is not CashAuditBundle:
        raise TypeError("bundle must be exactly a CashAuditBundle")
    replay = bundle.replay()
    journal = bundle.journal
    current_order_ids = set()

    order_attributes = {
        item.order_id: {
            "created_phase": None,
            "eligible_from": None,
            "expire_date": None,
            "note": None,
            "order_type": None,
            "requested_amount": None,
            "requested_percent": None,
            "requested_shares": None,
            "shares": item.declared_quantity,
            "side": item.side,
        }
        for item in bundle.seed.open_orders
    }
    fill_attributes = {}
    for event in journal:
        if event.event_type is ExecutionEventType.ORDER_CREATED:
            payload = event.payload
            assert type(payload) is OrderEventPayload
            current_order_ids.add(payload.order_id)
            order_attributes[payload.order_id] = dict(
                payload.attributes.entries
            )
        elif event.event_type is ExecutionEventType.FILL:
            payload = event.payload
            assert type(payload) is FillPayload
            fill_attributes[payload.fill_id] = dict(
                payload.attributes.entries
            )

    fill_by_id = {item.fill_id: item for item in replay.fills}
    fee_by_id = {item.fill_id: item for item in replay.fees}
    records = []
    for trade in replay.trades:
        fill = fill_by_id.get(trade.fill_id)
        fee = fee_by_id.get(trade.fill_id)
        if fill is None or fee is None:
            raise CashReplayError(
                f"trade {trade.fill_id} lacks its replay fill/fee view"
            )
        created = order_attributes.get(trade.order_id)
        attrs = fill_attributes.get(trade.fill_id)
        if created is None or attrs is None:
            raise CashReplayError(
                f"trade {trade.fill_id} lacks persisted order/fill attributes"
            )

        is_current_order = trade.order_id in current_order_ids

        def _matching_text_attribute(name: str):
            created_has = name in created and created[name] is not None
            fill_has = name in attrs and attrs[name] is not None
            if is_current_order and (not created_has or not fill_has):
                raise CashReplayError(
                    f"trade {trade.fill_id} lacks current {name!r} evidence"
                )
            values = []
            if created_has:
                values.append(("OrderCreated", created[name]))
            if fill_has:
                values.append(("Fill", attrs[name]))
            for source, value in values:
                if type(value) is not str:
                    raise CashReplayError(
                        f"trade {trade.fill_id} {source} {name} must be str"
                    )
            if len(values) == 2 and values[0][1] != values[1][1]:
                raise CashReplayError(
                    f"trade {trade.fill_id} {name} changed after creation"
                )
            return None if not values else values[-1][1]

        order_type = _matching_text_attribute("order_type")
        if order_type is not None and order_type not in {
            "open", "close", "market", "limit", "stop", "stop_profit"
        }:
            raise CashReplayError(
                f"trade {trade.fill_id} has unsupported order_type"
            )
        note = _matching_text_attribute("note")

        def _final_attribute(name: str, default=None):
            if name in attrs:
                return attrs[name]
            if name in created:
                return created[name]
            return default

        auto_resized = _final_attribute("auto_resized")
        resize_reason = _final_attribute("resize_reason")
        resized_from_shares = _final_attribute("resized_from_shares")
        resized_to_shares = _final_attribute("resized_to_shares")
        sizing_price = _final_attribute("sizing_price")
        if is_current_order:
            if type(auto_resized) is not bool:
                raise CashReplayError(
                    f"trade {trade.fill_id} auto_resized must be bool"
                )
            if type(resize_reason) is not str:
                raise CashReplayError(
                    f"trade {trade.fill_id} resize_reason must be str"
                )
        elif auto_resized is not None and type(auto_resized) is not bool:
            raise CashReplayError(
                f"trade {trade.fill_id} auto_resized must be bool or null"
            )
        elif resize_reason is not None and type(resize_reason) is not str:
            raise CashReplayError(
                f"trade {trade.fill_id} resize_reason must be str or null"
            )
        for name, value in (
            ("resized_from_shares", resized_from_shares),
            ("resized_to_shares", resized_to_shares),
        ):
            if value is not None and (type(value) is not int or value <= 0):
                raise CashReplayError(
                    f"trade {trade.fill_id} {name} must be a positive int or null"
                )
        if sizing_price is not None and (
            type(sizing_price) not in (int, float)
            or not isfinite(float(sizing_price))
            or sizing_price <= 0
        ):
            raise CashReplayError(
                f"trade {trade.fill_id} sizing_price must be positive or null"
            )
        if auto_resized is True and (
            resized_from_shares is None
            or resized_to_shares is None
            or not resize_reason
        ):
            raise CashReplayError(
                f"trade {trade.fill_id} lacks final resize evidence"
            )

        profit = 0.0
        profit_pct = 0.0
        if trade.realized_pnl is not None:
            profit = trade.realized_pnl
            sold_cost = trade.cash_delta - trade.realized_pnl
            if not isfinite(sold_cost) or sold_cost <= 0:
                raise CashReplayError(
                    f"trade {trade.fill_id} has invalid realized cost basis"
                )
            profit_pct = trade.realized_pnl / sold_cost
            if not isfinite(profit_pct):
                raise CashReplayError(
                    f"trade {trade.fill_id} has non-finite profit_pct"
                )

        records.append({
            "fill_id": trade.fill_id,
            "settlement_id": fill.settlement_id,
            "event_sequence": fill.sequence,
            "order_id": trade.order_id,
            "symbol": trade.symbol,
            "direction": trade.side.upper(),
            "shares": trade.quantity,
            "price": trade.price,
            "amount": trade.notional,
            "time": trade.simulated_time.strftime("%Y%m%d"),
            "simulated_time": trade.simulated_time.isoformat(
                timespec="microseconds"
            ),
            "fee": trade.fee,
            "commission": fee.commission,
            "stamp_duty": fee.stamp_duty,
            "transfer_fee": fee.transfer_fee,
            "cash_delta": trade.cash_delta,
            "profit": _normal_zero(profit),
            "profit_pct": _normal_zero(profit_pct),
            "realized_pnl": trade.realized_pnl,
            "t0": fill.t0,
            "order_type": order_type,
            "note": note,
            "created_phase": created.get("created_phase"),
            "eligible_from": created.get("eligible_from"),
            "expire_date": created.get("expire_date"),
            "requested_shares": created.get("requested_shares"),
            "requested_amount": created.get("requested_amount"),
            "requested_percent": created.get("requested_percent"),
            "auto_resized": auto_resized,
            "resized_from_shares": resized_from_shares,
            "resized_to_shares": resized_to_shares,
            "resize_reason": resize_reason,
            "sizing_price": sizing_price,
        })
    return records


__all__ = [
    "CASH_AUDIT_BUNDLE_SCHEMA",
    "CASH_AUDIT_BUNDLE_SCHEMA_VERSION",
    "CASH_REPLAY_SEED_SCHEMA",
    "CASH_REPLAY_SEED_SCHEMA_VERSION",
    "CashReplayError",
    "CashReplayPendingT1",
    "CashReplayPositionSeed",
    "CashReplayOpenOrderSeed",
    "CashReplayReservationSeed",
    "CashReplaySeed",
    "CashReplayPositionState",
    "CashReplayReservationState",
    "CashReplayOrderState",
    "CashReplayState",
    "CashReplayFillView",
    "CashReplayFeeView",
    "CashReplayTradeView",
    "CashClosedRoundTripView",
    "CashReplayResult",
    "CashAuditBundle",
    "CashEventJournalReducer",
    "cash_replay_trade_records",
    "replay_cash_journal",
]
