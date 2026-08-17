"""Pure unit tests for the deterministic execution event journal."""

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from diepi.backtest.broker.events import (
    EXECUTION_EVENT_JOURNAL_SCHEMA,
    EXECUTION_EVENT_SCHEMA,
    EXECUTION_EVENT_SCHEMA_VERSION,
    CorporateActionPayload,
    EventAttributes,
    ExecutionEvent,
    ExecutionEventJournal,
    ExecutionEventType,
    FillPayload,
    MarginPayload,
    MarkPayload,
    OrderEventPayload,
    ReservationPayload,
    RollPayload,
    SettlementPayload,
    T1UnlockPayload,
)


T0 = datetime(2024, 1, 2, 9, 30)
T1 = datetime(2024, 1, 2, 9, 31)


def _order_payload(order_id="order-1", **kwargs):
    return OrderEventPayload(order_id=order_id, symbol="600000.SH", **kwargs)


def _event(
    *,
    event_id="event-1",
    event_type=ExecutionEventType.ORDER_CREATED,
    simulated_time=T0,
    phase_seq=10,
    sequence=1,
    payload=None,
):
    return ExecutionEvent(
        event_id=event_id,
        event_type=event_type,
        simulated_time=simulated_time,
        phase_seq=phase_seq,
        sequence=sequence,
        payload=payload if payload is not None else _order_payload(),
    )


def _payload_for(event_type):
    if event_type in {
        ExecutionEventType.ORDER_CREATED,
        ExecutionEventType.ORDER_ACCEPTED,
        ExecutionEventType.ORDER_REJECTED,
        ExecutionEventType.ORDER_CANCELED,
        ExecutionEventType.ORDER_EXPIRED,
    }:
        return _order_payload()
    if event_type in {ExecutionEventType.RESERVE, ExecutionEventType.RELEASE}:
        return ReservationPayload("reserve-1", "order-1", "CNY", 100.0)
    if event_type is ExecutionEventType.FILL:
        return FillPayload("fill-1", "order-1", "600000.SH", 100, 10.0, 1000.0)
    if event_type is ExecutionEventType.SETTLEMENT:
        return SettlementPayload(
            "settle-1", "account-1", "CNY", 1000.0, "credit", "2024-01-03"
        )
    if event_type is ExecutionEventType.T1_UNLOCK:
        return T1UnlockPayload("account-1", "600000.SH", 100, "2024-01-03")
    if event_type is ExecutionEventType.CORPORATE_ACTION:
        return CorporateActionPayload(
            "action-1", "600000.SH", "cash_dividend", "2024-01-03"
        )
    if event_type is ExecutionEventType.MARK:
        return MarkPayload("600000.SH", 10.0)
    if event_type is ExecutionEventType.MARGIN:
        return MarginPayload("account-1", "IF2409", 12000.0, "initial")
    if event_type is ExecutionEventType.ROLL:
        return RollPayload("roll-1", "IF2409", "IF2410", 2)
    raise AssertionError(f"missing test payload for {event_type}")


def _record_order(journal, event_id, *, time=T0, phase=10, event_type=None):
    return journal.record(
        event_id=event_id,
        event_type=event_type or ExecutionEventType.ORDER_CREATED,
        simulated_time=time,
        phase_seq=phase,
        payload=_order_payload(order_id=f"order-{event_id}"),
    )


def test_event_type_values_are_a_stable_closed_schema():
    assert [event_type.value for event_type in ExecutionEventType] == [
        "order_created",
        "order_accepted",
        "order_rejected",
        "order_canceled",
        "order_expired",
        "reserve",
        "release",
        "fill",
        "settlement",
        "t1_unlock",
        "corporate_action",
        "mark",
        "margin",
        "roll",
    ]


def test_schema_names_and_version_are_explicit():
    assert EXECUTION_EVENT_SCHEMA == "diepi.execution_event"
    assert EXECUTION_EVENT_JOURNAL_SCHEMA == "diepi.execution_event_journal"
    assert EXECUTION_EVENT_SCHEMA_VERSION == 1


def test_attributes_sort_keys_recursively_and_use_json_arrays():
    nested = EventAttributes((('z', 2), ('a', (True, None, 3.5))))
    attributes = EventAttributes((('beta', nested), ('alpha', "x")))

    assert list(attributes.to_dict()) == ["alpha", "beta"]
    assert list(attributes.to_dict()["beta"]) == ["a", "z"]
    assert attributes.to_dict()["beta"]["a"] == [True, None, 3.5]


def test_attributes_to_dict_is_a_detached_copy():
    attributes = EventAttributes((('nested', (1, 2)),))
    exported = attributes.to_dict()
    exported["nested"].append(3)
    exported["new"] = "mutable output is harmless"

    assert attributes.to_dict() == {"nested": [1, 2]}


def test_attributes_canonicalize_negative_zero():
    attributes = EventAttributes((('zero', -0.0), ('nested', (-0.0,))))

    assert attributes.to_dict() == {"nested": [0.0], "zero": 0.0}
    assert "-0.0" not in json.dumps(attributes.to_dict(), sort_keys=True)


@pytest.mark.parametrize("value", [2**53, -(2**53)])
def test_attributes_reject_integers_outside_portable_json_range(value):
    with pytest.raises(ValueError, match="safe-integer"):
        EventAttributes((('too_large', value),))


def test_attributes_are_frozen():
    attributes = EventAttributes((('key', "value"),))

    with pytest.raises(FrozenInstanceError):
        attributes.entries = ()


def test_payloads_are_frozen():
    payload = _order_payload()

    with pytest.raises(FrozenInstanceError):
        payload.order_id = "changed"


def test_attributes_reject_mutable_entries_container():
    with pytest.raises(TypeError, match="entries must be a tuple"):
        EventAttributes([("key", "value")])


@pytest.mark.parametrize(
    "value",
    [
        {"key": "value"},
        [1, 2],
        {1, 2},
        bytearray(b"mutable"),
        (1, [2]),
    ],
)
def test_attributes_recursively_reject_mutable_values(value):
    with pytest.raises(TypeError, match="mutable"):
        EventAttributes((('bad', value),))


def test_attributes_reject_non_string_keys():
    with pytest.raises(TypeError, match="key must be a string"):
        EventAttributes(((1, "value"),))


def test_attributes_reject_duplicate_keys_before_canonicalization():
    with pytest.raises(ValueError, match="duplicate attribute key"):
        EventAttributes((('same', 1), ('same', 2)))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_attributes_recursively_reject_nonfinite_floats(value):
    with pytest.raises(ValueError, match="NaN or infinity"):
        EventAttributes((('outer', (0, value)),))


@pytest.mark.parametrize("value", [object(), Decimal("1.0"), datetime(2024, 1, 2)])
def test_attributes_reject_unsupported_json_values(value):
    with pytest.raises(TypeError, match="unsupported value"):
        EventAttributes((('bad', value),))


def test_payload_rejects_raw_mapping_as_attributes():
    with pytest.raises(TypeError, match="EventAttributes"):
        OrderEventPayload("order-1", "600000.SH", attributes={"desk": "alpha"})


def test_envelope_rejects_arbitrary_dict_payload():
    with pytest.raises(TypeError, match="requires payload=OrderEventPayload"):
        _event(payload={"order_id": "order-1"})


def test_payload_constructor_rejects_unknown_fields():
    with pytest.raises(TypeError, match="unexpected keyword"):
        OrderEventPayload("order-1", "600000.SH", amuont=100)


def test_envelope_constructor_rejects_wall_clock_or_unknown_fields():
    with pytest.raises(TypeError, match="unexpected keyword"):
        ExecutionEvent(
            event_id="event-1",
            event_type=ExecutionEventType.ORDER_CREATED,
            simulated_time=T0,
            phase_seq=10,
            sequence=1,
            payload=_order_payload(),
            recorded_at=datetime(2024, 1, 2, 9, 30),
        )


@pytest.mark.parametrize("event_type", list(ExecutionEventType))
def test_each_event_type_accepts_only_its_typed_payload(event_type):
    event = _event(event_type=event_type, payload=_payload_for(event_type))

    assert event.event_type is event_type


def test_event_type_payload_pairing_is_enforced():
    with pytest.raises(TypeError, match="requires payload=FillPayload"):
        _event(event_type=ExecutionEventType.FILL, payload=_order_payload())


def test_event_envelope_is_frozen():
    event = _event()

    with pytest.raises(FrozenInstanceError):
        event.sequence = 2


def test_event_ordering_key_contains_simulated_phase_and_journal_sequence():
    event = _event(phase_seq=7, sequence=3)

    assert event.ordering_key == (T0, 7, 3)


def test_event_to_dict_is_sorted_repeatable_and_versioned():
    attributes = EventAttributes((('z', 2), ('a', 1)))
    event = _event(payload=_order_payload(attributes=attributes))

    first = event.to_dict()
    second = event.to_dict()
    assert first == second
    assert first is not second
    assert list(first) == sorted(first)
    assert list(first["payload"]) == sorted(first["payload"])
    assert first["schema"] == EXECUTION_EVENT_SCHEMA
    assert first["schema_version"] == EXECUTION_EVENT_SCHEMA_VERSION
    assert first["simulated_time"] == "2024-01-02T09:30:00.000000"


def test_event_serialization_preserves_exact_microseconds():
    event = _event(simulated_time=datetime(2024, 1, 2, 9, 30, 0, 123456))

    assert event.to_dict()["simulated_time"] == "2024-01-02T09:30:00.123456"


def test_serialized_event_has_no_wall_clock_field():
    exported = _event().to_dict()

    assert not {"created_at", "recorded_at", "wall_time", "timestamp"}.intersection(exported)


def test_event_json_is_canonical_and_json_roundtrippable():
    event = _event(payload=_order_payload(reason="审计"))

    assert event.to_json() == event.to_json()
    assert json.loads(event.to_json()) == event.to_dict()
    assert " " not in event.to_json()


@pytest.mark.parametrize("event_id", ["", "   ", 1, None])
def test_event_id_must_be_nonempty_text(event_id):
    with pytest.raises((TypeError, ValueError)):
        _event(event_id=event_id)


@pytest.mark.parametrize("event_type", ["fill", 1, None])
def test_event_type_requires_the_enum(event_type):
    with pytest.raises(TypeError, match="ExecutionEventType"):
        _event(event_type=event_type)


@pytest.mark.parametrize("simulated_time", ["2024-01-02T09:30:00", None, 0])
def test_simulated_time_must_be_explicit_datetime(simulated_time):
    with pytest.raises(TypeError, match="simulated_time"):
        _event(simulated_time=simulated_time)


def test_timezone_aware_simulated_time_is_rejected():
    with pytest.raises(ValueError, match="timezone-naive"):
        _event(simulated_time=datetime(2024, 1, 2, 9, 30, tzinfo=timezone.utc))


def test_folded_simulated_time_is_rejected_because_wire_format_loses_fold():
    with pytest.raises(ValueError, match="fold must be 0"):
        _event(simulated_time=datetime(2024, 11, 3, 1, 30, fold=1))


@pytest.mark.parametrize("phase_seq", [-1, True, 1.0, "1", None])
def test_invalid_phase_sequence_is_rejected(phase_seq):
    with pytest.raises((TypeError, ValueError), match="phase_seq"):
        _event(phase_seq=phase_seq)


@pytest.mark.parametrize("sequence", [0, -1, True, 1.0, "1", None])
def test_invalid_journal_sequence_is_rejected(sequence):
    with pytest.raises((TypeError, ValueError), match="sequence"):
        _event(sequence=sequence)


@pytest.mark.parametrize("field", ["phase_seq", "sequence"])
def test_event_key_integers_must_fit_portable_json_range(field):
    kwargs = {field: 2**53}

    with pytest.raises(ValueError, match="safe-integer"):
        _event(**kwargs)


def test_record_assigns_a_continuous_one_based_sequence():
    journal = ExecutionEventJournal()

    first = _record_order(journal, "event-1")
    second = _record_order(journal, "event-2")

    assert (first.sequence, second.sequence, journal.next_sequence) == (1, 2, 3)


def test_append_rejects_a_sequence_gap():
    journal = ExecutionEventJournal()

    with pytest.raises(ValueError, match="continuous: expected 1, got 2"):
        journal.append(_event(sequence=2))


def test_append_rejects_duplicate_event_id():
    journal = ExecutionEventJournal()
    journal.append(_event())

    with pytest.raises(ValueError, match="duplicate event_id"):
        journal.append(_event(sequence=2))


def test_append_rejects_earlier_simulated_time():
    journal = ExecutionEventJournal()
    _record_order(journal, "event-1", time=T1, phase=0)

    with pytest.raises(ValueError, match="monotonically"):
        _record_order(journal, "event-2", time=T0, phase=99)


def test_append_rejects_a_lower_phase_at_the_same_simulated_time():
    journal = ExecutionEventJournal()
    _record_order(journal, "event-1", phase=20)

    with pytest.raises(ValueError, match="monotonically"):
        _record_order(journal, "event-2", phase=19)


def test_same_time_and_phase_are_stably_ordered_by_sequence():
    journal = ExecutionEventJournal()
    first = _record_order(journal, "event-1", phase=20)
    second = _record_order(journal, "event-2", phase=20)

    assert first.ordering_key < second.ordering_key


def test_phase_sequence_may_reset_after_simulated_time_advances():
    journal = ExecutionEventJournal()
    _record_order(journal, "event-1", time=T0, phase=20)
    event = _record_order(journal, "event-2", time=T1, phase=0)

    assert event.sequence == 2


def test_failed_append_is_atomic_for_journal_state_and_next_sequence():
    journal = ExecutionEventJournal()
    first = _record_order(journal, "event-1", time=T1)

    with pytest.raises(ValueError):
        _record_order(journal, "event-2", time=T0)

    assert journal.snapshot() == (first,)
    assert journal.next_sequence == 2
    recovered = _record_order(journal, "event-2", time=T1)
    assert recovered.sequence == 2


def test_append_batch_commits_all_events_and_returns_an_immutable_view():
    journal = ExecutionEventJournal()
    first = _event(event_id="event-1", sequence=1)
    second = _event(event_id="event-2", sequence=2)

    appended = journal.append_batch((first, second))

    assert appended == (first, second)
    assert isinstance(appended, tuple)
    assert journal.snapshot() == appended
    assert journal.next_sequence == 3


def test_append_batch_accepts_a_generator_and_consumes_it_once():
    journal = ExecutionEventJournal()
    yielded = []

    def events():
        for sequence in (1, 2, 3):
            yielded.append(sequence)
            yield _event(event_id=f"event-{sequence}", sequence=sequence)

    appended = journal.append_batch(events())

    assert yielded == [1, 2, 3]
    assert appended == journal.snapshot()
    assert [event.sequence for event in appended] == [1, 2, 3]


def test_append_batch_does_not_mutate_an_immutable_input():
    journal = ExecutionEventJournal()
    batch = (
        _event(event_id="event-1", sequence=1),
        _event(event_id="event-2", sequence=2),
    )
    before = tuple(batch)

    journal.append_batch(batch)

    assert batch == before
    assert batch is not journal.events


def test_append_batch_empty_iterable_is_an_atomic_noop():
    journal = ExecutionEventJournal()
    first = _record_order(journal, "event-1")
    before = journal.snapshot()

    result = journal.append_batch(iter(()))

    assert result == ()
    assert journal.snapshot() == before == (first,)
    assert journal.next_sequence == 2


@pytest.mark.parametrize(
    "batch, error, message",
    [
        (
            (
                _event(event_id="event-2", sequence=2),
                object(),
            ),
            TypeError,
            "ExecutionEvent",
        ),
        (
            (
                _event(event_id="event-2", sequence=2),
                _event(event_id="event-1", sequence=3),
            ),
            ValueError,
            "duplicate event_id",
        ),
        (
            (
                _event(event_id="event-2", sequence=2),
                _event(event_id="event-2", sequence=3),
            ),
            ValueError,
            "duplicate event_id",
        ),
        (
            (
                _event(event_id="event-2", sequence=2),
                _event(event_id="event-3", sequence=4),
            ),
            ValueError,
            "continuous: expected 3, got 4",
        ),
        (
            (
                _event(event_id="event-2", sequence=2, simulated_time=T1),
                _event(event_id="event-3", sequence=3, simulated_time=T0),
            ),
            ValueError,
            "monotonically",
        ),
        (
            (_event(event_id="event-2", sequence=2, phase_seq=9),),
            ValueError,
            "monotonically",
        ),
    ],
)
def test_append_batch_failure_at_any_position_leaves_journal_unchanged(
    batch, error, message
):
    journal = ExecutionEventJournal()
    first = _record_order(journal, "event-1")
    before_snapshot = journal.snapshot()
    before_json = journal.to_json()

    with pytest.raises(error, match=message):
        journal.append_batch(batch)

    assert journal.snapshot() == before_snapshot == (first,)
    assert journal.to_json() == before_json
    assert journal.next_sequence == 2
    recovered = journal.append(_event(event_id="event-2", sequence=2))
    assert recovered.sequence == 2


@pytest.mark.parametrize(
    "error",
    [RuntimeError("injected generator failure"), TypeError("injected generator failure")],
)
def test_append_batch_generator_failure_leaves_journal_unchanged(error):
    journal = ExecutionEventJournal()
    first = _record_order(journal, "event-1")
    before = journal.snapshot()

    def broken_generator():
        yield _event(event_id="event-2", sequence=2)
        raise error

    with pytest.raises(type(error), match="injected generator failure"):
        journal.append_batch(broken_generator())

    assert journal.snapshot() == before == (first,)
    assert journal.next_sequence == 2


def test_append_batch_rejects_a_non_iterable_without_mutation():
    journal = ExecutionEventJournal()
    first = _record_order(journal, "event-1")

    with pytest.raises(TypeError, match="iterable of ExecutionEvent"):
        journal.append_batch(None)

    assert journal.snapshot() == (first,)
    assert journal.next_sequence == 2


def test_snapshot_is_repeatable_and_does_not_change_after_later_append():
    journal = ExecutionEventJournal()
    first = _record_order(journal, "event-1")
    snapshot = journal.snapshot()
    _record_order(journal, "event-2")

    assert snapshot == (first,)
    assert journal.snapshot() == journal.snapshot()
    assert isinstance(snapshot, tuple)


def test_iterator_is_a_point_in_time_view():
    journal = ExecutionEventJournal()
    first = _record_order(journal, "event-1")
    iterator = iter(journal)
    _record_order(journal, "event-2")

    assert tuple(iterator) == (first,)


def test_filter_returns_an_immutable_non_mutating_view():
    journal = ExecutionEventJournal()
    created = _record_order(journal, "event-1")
    accepted = _record_order(
        journal,
        "event-2",
        event_type=ExecutionEventType.ORDER_ACCEPTED,
    )
    before = journal.snapshot()

    result = journal.filter_events(
        event_types=(ExecutionEventType.ORDER_ACCEPTED,),
        start_sequence=2,
        end_sequence=2,
    )

    assert result == (accepted,)
    assert isinstance(result, tuple)
    assert journal.snapshot() == before == (created, accepted)


def test_filter_accepts_one_event_enum_without_string_coercion():
    journal = ExecutionEventJournal()
    created = _record_order(journal, "event-1")

    assert journal.filter_events(event_types=ExecutionEventType.ORDER_CREATED) == (
        created,
    )


@pytest.mark.parametrize("event_types", ["order_created", ["order_created"], [1]])
def test_filter_rejects_non_enum_event_types(event_types):
    journal = ExecutionEventJournal()

    with pytest.raises(TypeError, match="event_types"):
        journal.filter_events(event_types=event_types)


def test_replay_returns_inclusive_sequence_window_without_mutation():
    journal = ExecutionEventJournal()
    first = _record_order(journal, "event-1")
    second = _record_order(journal, "event-2")
    third = _record_order(journal, "event-3")

    assert journal.replay(start_sequence=2, end_sequence=3) == (second, third)
    assert journal.snapshot() == (first, second, third)


def test_replay_rejects_reversed_or_invalid_sequence_bounds():
    journal = ExecutionEventJournal()

    with pytest.raises(ValueError, match="must not exceed"):
        journal.replay(start_sequence=2, end_sequence=1)
    with pytest.raises(TypeError, match="start_sequence"):
        journal.replay(start_sequence=True)


def test_journal_to_dict_is_repeatable_versioned_and_detached():
    journal = ExecutionEventJournal()
    _record_order(journal, "event-1")

    first = journal.to_dict()
    second = journal.to_dict()
    assert first == second
    assert list(first) == sorted(first)
    assert first["schema"] == EXECUTION_EVENT_JOURNAL_SCHEMA
    assert first["schema_version"] == EXECUTION_EVENT_SCHEMA_VERSION
    first["events"].clear()
    assert len(journal) == 1
    assert len(journal.to_dict()["events"]) == 1


def test_journal_json_is_canonical_and_contains_no_wall_clock():
    journal = ExecutionEventJournal()
    _record_order(journal, "event-1")

    assert journal.to_json() == journal.to_json()
    exported = json.loads(journal.to_json())
    assert exported == journal.to_dict()
    assert "recorded_at" not in journal.to_json()


def test_constructor_revalidates_preexisting_events_in_order():
    first = _event(event_id="event-1", sequence=1, simulated_time=T1)
    second = _event(event_id="event-2", sequence=2, simulated_time=T0)

    with pytest.raises(ValueError, match="monotonically"):
        ExecutionEventJournal((first, second))


def test_constructor_failure_clears_a_preallocated_instance():
    first = _event(event_id="event-1", sequence=1, simulated_time=T1)
    second = _event(event_id="event-2", sequence=2, simulated_time=T0)
    journal = object.__new__(ExecutionEventJournal)

    with pytest.raises(ValueError, match="monotonically"):
        journal.__init__((first, second))

    assert journal.snapshot() == ()
    assert journal.next_sequence == 1


@pytest.mark.parametrize(
    ("amount", "error"),
    [
        (0, ValueError),
        (-1, ValueError),
        (float("nan"), ValueError),
        (float("inf"), ValueError),
        (True, TypeError),
        ("100", TypeError),
    ],
)
def test_reservation_rejects_invalid_amounts(amount, error):
    with pytest.raises(error, match="amount"):
        ReservationPayload("reserve-1", "order-1", "CNY", amount)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"quantity": 0},
        {"quantity": 1.0},
        {"price": 0},
        {"price": float("nan")},
        {"amount": float("inf")},
        {"fee": -0.01},
    ],
)
def test_fill_rejects_invalid_numeric_fields(kwargs):
    values = {
        "fill_id": "fill-1",
        "order_id": "order-1",
        "symbol": "600000.SH",
        "quantity": 100,
        "price": 10.0,
        "amount": 1000.0,
        "fee": 0.0,
    }
    values.update(kwargs)

    with pytest.raises((TypeError, ValueError)):
        FillPayload(**values)


def test_nonnegative_payload_number_canonicalizes_negative_zero():
    payload = FillPayload(
        "fill-1",
        "order-1",
        "600000.SH",
        100,
        10.0,
        1000.0,
        fee=-0.0,
    )

    assert payload.to_dict()["fee"] == 0.0
    assert "-0.0" not in json.dumps(payload.to_dict(), sort_keys=True)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"direction": "sideways"},
        {"amount": 0},
        {"settlement_date": "20240103"},
        {"settlement_date": "2024-02-30"},
    ],
)
def test_settlement_rejects_invalid_fields(kwargs):
    values = {
        "settlement_id": "settle-1",
        "account_id": "account-1",
        "asset": "CNY",
        "amount": 100.0,
        "direction": "credit",
        "settlement_date": "2024-01-03",
    }
    values.update(kwargs)

    with pytest.raises((TypeError, ValueError)):
        SettlementPayload(**values)


@pytest.mark.parametrize(
    "quantity, trade_date",
    [(0, "2024-01-03"), (True, "2024-01-03"), (100, "20240103")],
)
def test_t1_unlock_rejects_invalid_quantity_or_date(quantity, trade_date):
    with pytest.raises((TypeError, ValueError)):
        T1UnlockPayload("account-1", "600000.SH", quantity, trade_date)


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf"), True])
def test_mark_rejects_invalid_price(price):
    with pytest.raises((TypeError, ValueError), match="price"):
        MarkPayload("600000.SH", price)


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf"), False])
def test_margin_rejects_invalid_amount(amount):
    with pytest.raises((TypeError, ValueError), match="amount"):
        MarginPayload("account-1", "IF2409", amount, "initial")


def test_roll_requires_distinct_contracts_and_positive_integer_quantity():
    with pytest.raises(ValueError, match="must differ"):
        RollPayload("roll-1", "IF2409", "IF2409", 1)
    with pytest.raises(TypeError, match="quantity"):
        RollPayload("roll-1", "IF2409", "IF2410", 1.0)
    with pytest.raises(ValueError, match="quantity"):
        RollPayload("roll-1", "IF2409", "IF2410", 0)


def test_text_fields_are_canonically_trimmed():
    payload = OrderEventPayload(" order-1 ", " 600000.SH ", reason=" audit ")
    event = _event(event_id=" event-1 ", payload=payload)

    assert event.event_id == "event-1"
    assert payload.to_dict()["order_id"] == "order-1"
    assert payload.to_dict()["symbol"] == "600000.SH"
    assert payload.to_dict()["reason"] == "audit"


def test_corporate_action_terms_require_immutable_attributes():
    terms = EventAttributes(
        (
            ("cash_per_share", 0.25),
            ("ratio", EventAttributes((('denominator', 10), ('numerator', 3)))),
        )
    )
    payload = CorporateActionPayload(
        "action-1",
        "600000.SH",
        "cash_and_stock_dividend",
        "2024-01-03",
        attributes=terms,
    )

    assert payload.to_dict()["attributes"] == {
        "cash_per_share": 0.25,
        "ratio": {"denominator": 10, "numerator": 3},
    }


@pytest.mark.parametrize("event_type", list(ExecutionEventType))
def test_event_from_dict_roundtrips_every_closed_payload_type(event_type):
    original = _event(
        event_id=f"event-{event_type.value}",
        event_type=event_type,
        payload=_payload_for(event_type),
    )

    restored = ExecutionEvent.from_dict(original.to_dict())

    assert restored == original
    assert restored.to_dict() == original.to_dict()


def test_event_from_dict_restores_nested_attributes_without_retaining_input():
    original = _event(payload=_order_payload(attributes=EventAttributes((
        ("array", (1, EventAttributes((("nested", "value"),)))),
    ))))
    wire = original.to_dict()

    restored = ExecutionEvent.from_dict(wire)
    wire["payload"]["attributes"]["array"].append("mutated")
    wire["payload"]["attributes"]["array"][1]["nested"] = "changed"

    assert restored.payload.attributes.to_dict() == {
        "array": [1, {"nested": "value"}]
    }


def test_event_from_dict_rejects_noncanonical_text_and_payload_pairing():
    whitespace = _event().to_dict()
    whitespace["payload"]["order_id"] = " order-1 "
    with pytest.raises(ValueError, match="not canonical"):
        ExecutionEvent.from_dict(whitespace)

    wrong_payload = _event().to_dict()
    wrong_payload["event_type"] = ExecutionEventType.FILL.value
    with pytest.raises(ValueError, match="keys mismatch"):
        ExecutionEvent.from_dict(wrong_payload)


def test_event_from_json_requires_exact_canonical_encoding():
    original = _event(payload=_order_payload(reason="审计"))
    assert ExecutionEvent.from_json(original.to_json()) == original

    pretty = json.dumps(original.to_dict(), ensure_ascii=False, indent=2)
    with pytest.raises(ValueError, match="canonical JSON"):
        ExecutionEvent.from_json(pretty)

    duplicate = original.to_json().replace(
        '{"event_id":', '{"event_id":"duplicate","event_id":', 1
    )
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        ExecutionEvent.from_json(duplicate)


def test_event_from_json_rejects_nonfinite_and_negative_zero_wire_values():
    fill = _event(
        event_type=ExecutionEventType.FILL,
        payload=_payload_for(ExecutionEventType.FILL),
    ).to_dict()
    fill["payload"]["fee"] = float("nan")
    with pytest.raises(ValueError, match="non-finite JSON constant"):
        ExecutionEvent.from_json(json.dumps(
            fill, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ))

    fill["payload"]["fee"] = -0.0
    with pytest.raises(ValueError, match="not canonical"):
        ExecutionEvent.from_json(json.dumps(
            fill, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ))


def test_journal_from_dict_and_json_roundtrip_the_complete_event_vocabulary():
    events = tuple(
        _event(
            event_id=f"event-{index:02d}",
            event_type=event_type,
            simulated_time=T0,
            phase_seq=10,
            sequence=index,
            payload=_payload_for(event_type),
        )
        for index, event_type in enumerate(ExecutionEventType, start=1)
    )
    original = ExecutionEventJournal(events)

    from_dict = ExecutionEventJournal.from_dict(original.to_dict())
    from_json = ExecutionEventJournal.from_json(original.to_json())

    assert from_dict.snapshot() == original.snapshot()
    assert from_json.snapshot() == original.snapshot()
    assert from_json.to_json() == original.to_json()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.__setitem__("schema_version", True),
         "schema_version"),
        (lambda payload: payload.__setitem__("extra", 1), "keys mismatch"),
        (lambda payload: payload.__setitem__("events", {}), "events"),
        (lambda payload: payload["events"][0].__setitem__("sequence", 2),
         "continuous"),
    ],
)
def test_journal_from_dict_rejects_schema_and_stream_mutations(
    mutation, message
):
    journal = ExecutionEventJournal((_event(),))
    payload = journal.to_dict()
    mutation(payload)

    with pytest.raises((TypeError, ValueError), match=message):
        ExecutionEventJournal.from_dict(payload)


def test_journal_from_json_rejects_duplicate_keys_and_pretty_encoding():
    journal = ExecutionEventJournal((_event(),))
    pretty = json.dumps(journal.to_dict(), ensure_ascii=False, indent=2)
    with pytest.raises(ValueError, match="canonical JSON"):
        ExecutionEventJournal.from_json(pretty)

    duplicate = journal.to_json().replace(
        '{"events":', '{"events":[],"events":', 1
    )
    with pytest.raises(ValueError, match="duplicate JSON object key"):
        ExecutionEventJournal.from_json(duplicate)
