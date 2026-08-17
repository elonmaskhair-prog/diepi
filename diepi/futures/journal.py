"""Minimal deterministic diagnostics for the approximate futures engine.

This journal intentionally records only two facts produced by real engine
paths: a scheduled close roll and a close maintenance-margin check.  It is not
an order/fill ledger and cannot replay NAV or account cash.
"""
from __future__ import annotations

from datetime import datetime
import json
from math import copysign, isfinite
from typing import Any

from ..backtest.broker.events import (
    EXECUTION_EVENT_JOURNAL_SCHEMA,
    EXECUTION_EVENT_SCHEMA,
    EXECUTION_EVENT_SCHEMA_VERSION,
    EventAttributes,
    ExecutionEventJournal,
    ExecutionEventType,
    MarginPayload,
    RollPayload,
)


FUTURES_EVENT_JOURNAL_SCOPE = "futures_margin_roll_diagnostic_v1"
FUTURES_CLOSE_TIME = (15, 0, 0)
ROLL_PHASE_SEQ = 10
MARGIN_PHASE_SEQ = 20
_MAX_SAFE_JSON_INTEGER = (1 << 53) - 1


def _close_datetime(trade_date: str) -> datetime:
    if type(trade_date) is not str:
        raise TypeError("trade_date must be a string")
    try:
        parsed = datetime.strptime(trade_date, "%Y%m%d")
    except ValueError:
        raise ValueError("trade_date must use YYYYMMDD") from None
    return parsed.replace(
        hour=FUTURES_CLOSE_TIME[0],
        minute=FUTURES_CLOSE_TIME[1],
        second=FUTURES_CLOSE_TIME[2],
    )


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value!r} is forbidden")


def _validate_json_value(value: Any, path: str) -> None:
    if value is None or type(value) in (bool, str):
        return
    if type(value) is int:
        if abs(value) > _MAX_SAFE_JSON_INTEGER:
            raise ValueError(f"{path} exceeds the JSON safe-integer range")
        return
    if type(value) is float:
        if not isfinite(value):
            raise ValueError(f"{path} must be finite")
        if value == 0 and copysign(1.0, value) < 0:
            raise ValueError(f"{path} must use canonical positive zero")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str or not key:
                raise ValueError(f"{path} keys must be non-empty strings")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise TypeError(f"{path} contains unsupported JSON value")


def _nonempty_wire_text(value: object, path: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{path} must be canonical non-empty text")
    return value


def _positive_wire_number(value: object, path: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{path} must be a number")
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{path} must be finite and positive")
    return result


def _finite_wire_number(value: object, path: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{path} must be a number")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{path} must be finite")
    return result


def validate_futures_journal_json(value: object) -> tuple[dict, ...]:
    """Validate and return a detached projection of canonical event dicts."""
    if type(value) is not str:
        raise TypeError("event_journal_json must be exactly str")
    try:
        decoded = json.loads(value, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"event_journal_json is invalid JSON: {exc}") from None
    _validate_json_value(decoded, "journal")
    if _canonical_json(decoded) != value:
        raise ValueError("event_journal_json must use canonical JSON encoding")
    if type(decoded) is not dict or set(decoded) != {
        "events", "schema", "schema_version"
    }:
        raise ValueError("event journal envelope does not match schema v1")
    if decoded["schema"] != EXECUTION_EVENT_JOURNAL_SCHEMA:
        raise ValueError("event journal schema is invalid")
    if (
        type(decoded["schema_version"]) is not int
        or decoded["schema_version"] != EXECUTION_EVENT_SCHEMA_VERSION
    ):
        raise ValueError("event journal schema_version is invalid")
    events = decoded["events"]
    if type(events) is not list:
        raise ValueError("event journal events must be a list")

    prior_key = None
    event_ids: set[str] = set()
    immutable_events = []
    for index, event in enumerate(events, start=1):
        path = f"events[{index - 1}]"
        if type(event) is not dict or set(event) != {
            "event_id", "event_type", "payload", "phase_seq", "schema",
            "schema_version", "sequence", "simulated_time",
        }:
            raise ValueError(f"{path} does not match execution-event schema v1")
        if event["schema"] != EXECUTION_EVENT_SCHEMA:
            raise ValueError(f"{path}.schema is invalid")
        if (
            type(event["schema_version"]) is not int
            or event["schema_version"] != EXECUTION_EVENT_SCHEMA_VERSION
        ):
            raise ValueError(f"{path}.schema_version is invalid")
        event_id = _nonempty_wire_text(event["event_id"], f"{path}.event_id")
        if event_id in event_ids:
            raise ValueError(f"duplicate event_id {event_id!r}")
        event_ids.add(event_id)
        if type(event["sequence"]) is not int or event["sequence"] != index:
            raise ValueError(f"{path}.sequence must be continuous from 1")
        if type(event["phase_seq"]) is not int or event["phase_seq"] < 0:
            raise ValueError(f"{path}.phase_seq must be a non-negative integer")
        simulated_text = _nonempty_wire_text(
            event["simulated_time"], f"{path}.simulated_time")
        try:
            simulated_time = datetime.fromisoformat(simulated_text)
        except ValueError:
            raise ValueError(f"{path}.simulated_time is invalid") from None
        if (
            simulated_time.tzinfo is not None
            or simulated_time.isoformat(timespec="microseconds") != simulated_text
            or simulated_time.time() != _close_datetime(
                simulated_time.strftime("%Y%m%d")).time()
        ):
            raise ValueError(
                f"{path}.simulated_time must be a canonical 15:00 close time")
        ordering_key = (
            simulated_time, event["phase_seq"], event["sequence"])
        if prior_key is not None and ordering_key < prior_key:
            raise ValueError("event journal ordering must be monotonic")
        prior_key = ordering_key

        event_type = event["event_type"]
        payload = event["payload"]
        if type(payload) is not dict:
            raise ValueError(f"{path}.payload must be an object")
        if event_type == ExecutionEventType.ROLL.value:
            if event["phase_seq"] != ROLL_PHASE_SEQ:
                raise ValueError("roll events must use the close-roll phase")
            if set(payload) != {
                "attributes", "from_contract", "quantity", "roll_id",
                "to_contract",
            }:
                raise ValueError("roll payload does not match diagnostic v1")
            from_contract = _nonempty_wire_text(
                payload["from_contract"], f"{path}.payload.from_contract")
            to_contract = _nonempty_wire_text(
                payload["to_contract"], f"{path}.payload.to_contract")
            if from_contract == to_contract:
                raise ValueError("roll contracts must differ")
            _nonempty_wire_text(payload["roll_id"], f"{path}.payload.roll_id")
            if type(payload["quantity"]) is not int or payload["quantity"] <= 0:
                raise ValueError("roll quantity must be a positive integer")
        elif event_type == ExecutionEventType.MARGIN.value:
            if event["phase_seq"] != MARGIN_PHASE_SEQ:
                raise ValueError("margin events must use the close-margin phase")
            if set(payload) != {
                "account_id", "amount", "attributes", "contract",
                "margin_kind",
            }:
                raise ValueError("margin payload does not match diagnostic v1")
            _nonempty_wire_text(
                payload["account_id"], f"{path}.payload.account_id")
            _nonempty_wire_text(
                payload["contract"], f"{path}.payload.contract")
            _nonempty_wire_text(
                payload["margin_kind"], f"{path}.payload.margin_kind")
            _positive_wire_number(payload["amount"], f"{path}.payload.amount")
        else:
            raise ValueError(
                "futures diagnostic journal accepts only margin and roll events")
        attributes = payload.get("attributes")
        if type(attributes) is not dict:
            raise ValueError(f"{path}.payload.attributes must be an object")
        if event_type == ExecutionEventType.ROLL.value:
            if set(attributes) != {
                "checkpoint", "journal_scope", "new_close_price",
                "new_entry_price", "old_exit_price", "reason",
            }:
                raise ValueError("roll attributes do not match diagnostic v1")
            if attributes["checkpoint"] != "daily_close":
                raise ValueError("roll checkpoint must be daily_close")
            if attributes["journal_scope"] != FUTURES_EVENT_JOURNAL_SCOPE:
                raise ValueError("roll journal_scope is invalid")
            if attributes["reason"] not in {
                "contract_roll", "contract_expiry"
            }:
                raise ValueError("roll reason is invalid")
            for key in (
                "new_close_price", "new_entry_price", "old_exit_price"
            ):
                _positive_wire_number(attributes[key], f"{path}.{key}")
        else:
            if set(attributes) != {
                "check_passed", "checkpoint", "journal_scope",
                "mark_price", "nav",
            }:
                raise ValueError("margin attributes do not match diagnostic v1")
            if type(attributes["check_passed"]) is not bool:
                raise ValueError("margin check_passed must be boolean")
            if attributes["checkpoint"] != "daily_close":
                raise ValueError("margin checkpoint must be daily_close")
            if attributes["journal_scope"] != FUTURES_EVENT_JOURNAL_SCOPE:
                raise ValueError("margin journal_scope is invalid")
            _positive_wire_number(
                attributes["mark_price"], f"{path}.mark_price")
            _finite_wire_number(attributes["nav"], f"{path}.nav")
        immutable_events.append(event)
    return tuple(immutable_events)


class FuturesDiagnosticJournal:
    """Engine-owned adapter that emits only real margin and roll diagnostics."""

    __slots__ = ("_account_id", "_journal", "_lots", "_product")

    def __init__(self, *, product: str, lots: int) -> None:
        self._product = _nonempty_wire_text(product, "product")
        if type(lots) is not int or lots <= 0:
            raise ValueError("lots must be a positive integer")
        self._lots = lots
        self._account_id = f"futures:{self._product}:research_account"
        self._journal = ExecutionEventJournal()

    def record_roll(
        self,
        *,
        trade_date: str,
        from_contract: str,
        to_contract: str,
        reason: str,
        old_exit_price: float,
        new_entry_price: float,
        new_close_price: float,
    ) -> None:
        roll_id = (
            f"futures-roll:{self._product}:{trade_date}:"
            f"{from_contract}:{to_contract}"
        )
        self._journal.record(
            event_id=(
                f"futures-event:{self._product}:{trade_date}:close:roll:"
                f"{from_contract}:{to_contract}"
            ),
            event_type=ExecutionEventType.ROLL,
            simulated_time=_close_datetime(trade_date),
            phase_seq=ROLL_PHASE_SEQ,
            payload=RollPayload(
                roll_id=roll_id,
                from_contract=from_contract,
                to_contract=to_contract,
                quantity=self._lots,
                attributes=EventAttributes(entries=(
                    ("checkpoint", "daily_close"),
                    ("journal_scope", FUTURES_EVENT_JOURNAL_SCOPE),
                    ("new_close_price", float(new_close_price)),
                    ("new_entry_price", float(new_entry_price)),
                    ("old_exit_price", float(old_exit_price)),
                    ("reason", reason),
                )),
            ),
        )

    def record_margin(
        self,
        *,
        trade_date: str,
        contract: str,
        mark_price: float,
        nav: float,
        required_margin: float,
        passed: bool,
    ) -> None:
        if type(passed) is not bool:
            raise TypeError("passed must be exactly bool")
        self._journal.record(
            event_id=(
                f"futures-event:{self._product}:{trade_date}:close:margin:"
                f"{contract}"
            ),
            event_type=ExecutionEventType.MARGIN,
            simulated_time=_close_datetime(trade_date),
            phase_seq=MARGIN_PHASE_SEQ,
            payload=MarginPayload(
                account_id=self._account_id,
                contract=contract,
                amount=float(required_margin),
                margin_kind="maintenance_close_check",
                attributes=EventAttributes(entries=(
                    ("check_passed", passed),
                    ("checkpoint", "daily_close"),
                    ("journal_scope", FUTURES_EVENT_JOURNAL_SCOPE),
                    ("mark_price", float(mark_price)),
                    ("nav", float(nav)),
                )),
            ),
        )

    def to_json(self) -> str:
        value = self._journal.to_json()
        validate_futures_journal_json(value)
        return value


EMPTY_FUTURES_EVENT_JOURNAL_JSON = ExecutionEventJournal().to_json()


__all__ = [
    "EMPTY_FUTURES_EVENT_JOURNAL_JSON",
    "FUTURES_EVENT_JOURNAL_SCOPE",
    "FuturesDiagnosticJournal",
    "validate_futures_journal_json",
]
