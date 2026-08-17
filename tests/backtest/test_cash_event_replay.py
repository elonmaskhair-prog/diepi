"""Strict cash-account replay from ExecutionEventJournal facts."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from datetime import datetime

import pytest

from diepi.backtest.broker import (
    Account,
    Broker,
    CashAuditBundle,
    CashReplayError,
    CashReplaySeed,
    cash_replay_trade_records,
    replay_cash_journal,
)
from diepi.backtest.broker.broker import BarData
from diepi.backtest.broker.events import (
    ExecutionEvent,
    ExecutionEventJournal,
    ExecutionEventType,
)
from diepi.backtest.broker.order import Order, OrderSide, OrderStatus, OrderType


SYMBOL = "600000.SH"
DAY_1 = "20240102"
DAY_2 = "20240103"
DAY_3 = "20240104"


def _bar(when: datetime, price: float, *, amount: float = 1_000_000.0):
    return BarData(
        symbol=SYMBOL,
        trade_time=when,
        open=price,
        high=price,
        low=price,
        close=price,
        amount=amount,
        pre_close=price,
    )


def _renumber(events):
    return ExecutionEventJournal(
        ExecutionEvent(
            event_id=event.event_id,
            event_type=event.event_type,
            simulated_time=event.simulated_time,
            phase_seq=event.phase_seq,
            sequence=index,
            payload=event.payload,
        )
        for index, event in enumerate(events, start=1)
    )


def _buy_only_run(*, t0=False):
    account = Account(initial_cash=50_000.0)
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 9, 0)
    )
    broker = Broker(
        account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
        t0_overrides={"600000"} if t0 else None,
        transfer_fee_rate=0.00002,
    )
    broker.set_date(DAY_1)
    broker.set_market_phase("trading")
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 2, 9, 31),
        eligible_from=datetime(2024, 1, 2, 9, 32),
        expire_date=DAY_1,
    ):
        order = broker.buy_at_market(
            SYMBOL, shares=100, current_price=10.0
        )
    broker.execute_market_orders(
        _bar(datetime(2024, 1, 2, 9, 32), 10.0),
        match_at=datetime(2024, 1, 2, 9, 32),
    )
    return account, broker, seed, order


def _full_round_trip_run():
    account = Account(initial_cash=50_000.0, stamp_duty_rate=0.001)
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 9, 0)
    )
    broker = Broker(
        account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
        commission_scope="parent_order",
        transfer_fee_rate=0.00002,
    )
    broker.set_date(DAY_1)
    broker.set_market_phase("trading")
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 2, 9, 31),
        eligible_from=datetime(2024, 1, 2, 9, 32),
        expire_date=DAY_1,
    ):
        buy = broker.buy_at_market(
            SYMBOL, shares=200, current_price=10.0
        )

    # Two fills on one parent order: minimum commission is charged once while
    # transfer fee remains a per-fill fact.
    broker.execute_market_orders(
        _bar(datetime(2024, 1, 2, 9, 32), 10.0, amount=1_000.0),
        match_at=datetime(2024, 1, 2, 9, 32),
    )
    assert buy.status is OrderStatus.PARTIAL
    broker.reset_bar_used_amount()
    broker.execute_market_orders(
        _bar(datetime(2024, 1, 2, 9, 33), 10.0, amount=1_000.0),
        match_at=datetime(2024, 1, 2, 9, 33),
    )
    assert buy.status is OrderStatus.FILLED
    broker.update_prices(
        {SYMBOL: 10.5},
        sim_time=datetime(2024, 1, 2, 15, 0),
        phase="closing_mark",
    )
    broker.on_day_start(DAY_2, sim_time=datetime(2024, 1, 3, 9, 0))

    broker.set_market_phase("trading")
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 3, 9, 31),
        eligible_from=datetime(2024, 1, 3, 9, 32),
        expire_date=DAY_2,
    ):
        sell = broker.sell_at_market(SYMBOL, shares=200)
    broker.reset_bar_used_amount()
    broker.execute_market_orders(
        _bar(datetime(2024, 1, 3, 9, 32), 11.0),
        match_at=datetime(2024, 1, 3, 9, 32),
    )
    assert sell.status is OrderStatus.FILLED

    # Non-fill lifecycle facts must not alter the final economic state.
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 3, 10, 0),
        expire_date=DAY_2,
    ):
        rejected = broker.buy_at_market(
            SYMBOL, shares=0, current_price=11.0
        )
    assert rejected.status is OrderStatus.REJECTED
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 3, 10, 1),
        expire_date=DAY_2,
    ):
        canceled = broker.buy_at_price(SYMBOL, 8.0, shares=100)
    assert broker.cancel_order(canceled.order_id)
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 3, 10, 2),
        expire_date=DAY_2,
    ):
        expiring = broker.buy_at_price(SYMBOL, 7.0, shares=100)
    broker.on_day_start(DAY_3, sim_time=datetime(2024, 1, 4, 9, 0))
    assert expiring.status is OrderStatus.CANCELLED
    return account, broker, seed


def test_seed_is_versioned_canonical_immutable_and_captures_nonempty_account():
    account = Account(initial_cash=10_000.0)
    account.buy(
        SYMBOL, 100, 10.0, "20240101", strict_settlement=False)
    account.settle_t1(DAY_1)
    account.buy(SYMBOL, 100, 10.0, DAY_1, strict_settlement=False)
    account.update_prices({SYMBOL: 12.0})
    # Attribute every initial freeze to an exact active order.
    assert account.freeze_cash(1_005.0)
    order = Order(
        order_id="initial-buy",
        symbol="000001.SZ",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        shares=100,
        price=10.0,
        frozen_amount=1_005.0,
        status=OrderStatus.SUBMITTED,
    )
    account.add_order(order)
    assert account.freeze_shares(SYMBOL, 50) == 50
    account.add_order(Order(
        order_id="initial-sell",
        symbol=SYMBOL,
        side=OrderSide.SELL,
        order_type=OrderType.LIMIT,
        shares=50,
        price=13.0,
        frozen_shares=50,
        status=OrderStatus.SUBMITTED,
    ))

    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 8, 0)
    )
    restored = CashReplaySeed.from_json(seed.to_json())
    assert restored == seed
    wire = json.loads(seed.to_json())
    assert wire["schema"] == "diepi.cash_replay_seed"
    assert wire["schema_version"] == 1
    assert wire["initial_cash"] == account.cash
    assert wire["initial_frozen_cash"] == account.frozen_cash
    assert wire["positions"][0]["shares"] == 200
    assert wire["positions"][0]["available_shares"] == 50
    assert wire["positions"][0]["frozen_shares"] == 50
    assert wire["positions"][0]["pending_t1"] == [
        {"quantity": 100, "trade_date": "2024-01-02"}
    ]
    assert wire["positions"][0]["current_price"] == 12.0
    assert wire["positions"][0]["avg_cost"] == account.positions[SYMBOL].avg_cost
    assert wire["reservations"][0]["order_id"] == "initial-buy"
    assert wire["reservations"][1]["order_id"] == "initial-sell"

    # No caller-owned account/list/mapping is retained.
    account.positions[SYMBOL]._pending_shares[DAY_1] = 100
    assert seed.positions[0].pending_t1[0].quantity == 100
    with pytest.raises(FrozenInstanceError):
        seed.initial_cash = 0.0


def test_seed_rejects_unattributed_frozen_cash_or_shares():
    cash_account = Account(initial_cash=1_000.0)
    assert cash_account.freeze_cash(100.0)
    with pytest.raises(ValueError, match="cash reservations"):
        CashReplaySeed.from_account(
            cash_account, as_of=datetime(2024, 1, 2)
        )

    share_account = Account(initial_cash=1_000.0)
    share_account.buy(
        SYMBOL, 100, 10.0, DAY_1, strict_settlement=False)
    share_account.settle_t1(DAY_2)
    assert share_account.freeze_shares(SYMBOL, 100) == 100
    with pytest.raises(ValueError, match="share reservations"):
        CashReplaySeed.from_account(
            share_account, as_of=datetime(2024, 1, 3)
        )


def test_seed_rejects_pending_order_instead_of_inferring_acceptance():
    account = Account(initial_cash=1_000.0)
    account.add_order(Order(
        order_id="pending-at-boundary",
        symbol=SYMBOL,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        shares=100,
        price=9.0,
        status=OrderStatus.PENDING,
    ))
    with pytest.raises(CashReplayError, match="not replay-seed eligible"):
        CashReplaySeed.from_account(
            account, as_of=datetime(2024, 1, 2)
        )


@pytest.mark.parametrize(
    "mutator, message",
    [
        (lambda text: json.dumps(json.loads(text), indent=2), "canonical"),
        (
            lambda text: text.replace(
                '"account_id":', '"account_id":"x","account_id":', 1
            ),
            "duplicate",
        ),
        (lambda text: text.replace('"initial_cash":8995.0', '"initial_cash":NaN'), "non-finite"),
    ],
)
def test_seed_json_rejects_noncanonical_duplicate_and_nonfinite(mutator, message):
    account = Account(initial_cash=10_000.0)
    assert account.freeze_cash(1_005.0)
    account.add_order(Order(
        order_id="initial-buy",
        symbol=SYMBOL,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        shares=100,
        price=10.0,
        frozen_amount=1_005.0,
        status=OrderStatus.SUBMITTED,
    ))
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2)
    )
    with pytest.raises(ValueError, match=message):
        CashReplaySeed.from_json(mutator(seed.to_json()))


def test_empty_journal_replays_initial_frozen_and_nonempty_state_exactly():
    account = Account(initial_cash=20_000.0)
    account.buy(SYMBOL, 100, 10.0, DAY_1, strict_settlement=False)
    account.update_prices({SYMBOL: 11.0})
    assert account.freeze_cash(1_005.0)
    account.add_order(Order(
        order_id="initial-buy",
        symbol="000001.SZ",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        shares=100,
        price=10.0,
        frozen_amount=1_005.0,
        status=OrderStatus.SUBMITTED,
    ))
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 8, 0)
    )
    result = replay_cash_journal(seed, ExecutionEventJournal())
    result.state.assert_matches_account(account)
    assert result.state.positions[0].shares == 100
    assert result.state.frozen_cash == 1_005.0
    assert result.win_rate is None
    assert result.closed_round_trips == ()


def test_real_broker_multifill_fees_cross_day_and_lifecycles_replay_exactly():
    account, broker, seed = _full_round_trip_run()
    result = replay_cash_journal(seed, broker.event_journal)

    result.state.assert_matches_account(account)
    assert result.state.positions == ()
    assert result.state.frozen_cash == 0.0
    assert result.state.nav == pytest.approx(account.total_asset)
    assert [item.commission for item in result.fees[:2]] == [5.0, 0.0]
    assert [item.transfer_fee for item in result.fees[:2]] == [0.02, 0.02]
    assert [item.total for item in result.fees] == [5.02, 0.02, 7.24]
    assert [item.simulated_time for item in result.fills] == [
        datetime(2024, 1, 2, 9, 32),
        datetime(2024, 1, 2, 9, 33),
        datetime(2024, 1, 3, 9, 32),
    ]
    assert result.trades[-1].realized_pnl == pytest.approx(187.72)
    assert len(result.closed_round_trips) == 1
    round_trip = result.closed_round_trips[0]
    assert round_trip.fill_count == 3
    assert round_trip.entry_cost == pytest.approx(2_005.04)
    assert round_trip.exit_proceeds == pytest.approx(2_192.76)
    assert round_trip.realized_pnl == pytest.approx(187.72)
    assert round_trip.is_win
    assert result.win_rate == 1.0
    event_types = tuple(event.event_type for event in broker.event_journal)
    assert ExecutionEventType.ORDER_REJECTED in event_types
    assert ExecutionEventType.ORDER_CANCELED in event_types
    assert ExecutionEventType.ORDER_EXPIRED in event_types


def test_large_notional_tamper_cannot_hide_behind_relative_tolerance():
    account = Account(initial_cash=20_000_000_000_000.0)
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 9, 0)
    )
    broker = Broker(account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.set_date(DAY_1)
    broker.set_market_phase("trading")
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 2, 9, 31),
        eligible_from=datetime(2024, 1, 2, 9, 32),
        expire_date=DAY_1,
    ):
        broker.buy_at_market(
            SYMBOL, shares=1_000_000_000_000, current_price=10.0
        )
    broker.execute_market_orders(
        _bar(
            datetime(2024, 1, 2, 9, 32),
            10.0,
            amount=100_000_000_000_000.0,
        ),
        match_at=datetime(2024, 1, 2, 9, 32),
    )
    wire = broker.event_journal.to_dict()
    settlement = next(
        event for event in wire["events"]
        if event["event_type"] == "settlement"
    )
    settlement["payload"]["amount"] += 5.0
    tampered = ExecutionEventJournal.from_dict(wire)
    with pytest.raises(CashReplayError, match="cash identity"):
        replay_cash_journal(seed, tampered)


@pytest.mark.parametrize(
    "field, replacement, message",
    [
        ("order_type", "banana", "unsupported order_type"),
        ("note", ["not", "text"], "note must be str"),
    ],
)
def test_trade_projection_rejects_untyped_or_unknown_ui_attributes(
    field, replacement, message
):
    _, broker, seed, _ = _buy_only_run()
    wire = broker.event_journal.to_dict()
    for event in wire["events"]:
        if event["event_type"] in ("order_created", "fill"):
            event["payload"]["attributes"][field] = replacement
    journal = ExecutionEventJournal.from_dict(wire)
    bundle = CashAuditBundle.from_parts(seed, journal)
    with pytest.raises(CashReplayError, match=message):
        cash_replay_trade_records(bundle)


def test_trade_projection_uses_final_fill_resize_facts():
    # The final sizing decision occurs after OrderCreated and before Fill.
    account = Account(initial_cash=50_000.0)
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 9, 0)
    )
    broker = Broker(account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.set_date(DAY_1)
    broker.set_market_phase("trading")
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 2, 9, 31),
        eligible_from=datetime(2024, 1, 2, 9, 32),
        expire_date=DAY_1,
    ):
        order = broker.buy_at_market(
            SYMBOL, shares=100, current_price=10.0
        )
    order.auto_resized = True
    order.resized_from_shares = 200
    order.resized_to_shares = 100
    order.resize_reason = "auction_cash_cap"
    order.sizing_price = 10.0
    broker.execute_market_orders(
        _bar(datetime(2024, 1, 2, 9, 32), 10.0),
        match_at=datetime(2024, 1, 2, 9, 32),
    )
    records = cash_replay_trade_records(
        CashAuditBundle.from_parts(seed, broker.event_journal)
    )
    assert records[0]["auto_resized"] is True
    assert records[0]["resized_from_shares"] == 200
    assert records[0]["resized_to_shares"] == 100
    assert records[0]["resize_reason"] == "auction_cash_cap"
    assert records[0]["sizing_price"] == 10.0


@pytest.mark.parametrize("t0, expected_available, expected_pending", [
    (False, 0, 100),
    (True, 100, 0),
])
def test_fill_uses_recorded_t0_decision_without_rulebook_lookup(
    t0, expected_available, expected_pending
):
    account, broker, seed, _ = _buy_only_run(t0=t0)
    fill_event = broker.event_journal.filter_events(
        event_types=ExecutionEventType.FILL
    )[0]
    assert dict(fill_event.payload.attributes.entries)["t0"] is t0
    result = replay_cash_journal(seed, broker.event_journal)
    position = result.state.positions[0]
    assert position.available_shares == expected_available
    assert sum(item.quantity for item in position.pending_t1) == expected_pending
    # No bar Mark event was emitted; Fill itself establishes the public mark.
    assert position.current_price == 10.0
    result.state.assert_matches_account(account)
    assert result.win_rate is None


def test_initial_holding_corporate_action_pending_t1_and_mark_replay():
    account = Account(initial_cash=100_000.0)
    account.buy(SYMBOL, 100, 10.0, DAY_1, strict_settlement=False)
    account.update_prices({SYMBOL: 10.0})
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 8, 0)
    )
    broker = Broker(account, slippage=0.0)
    broker.set_date(DAY_1)
    broker.set_market_phase("trading")
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 2, 10, 0),
        expire_date=DAY_2,
    ):
        open_buy = broker.buy_at_price(SYMBOL, 8.0, shares=100)
    assert open_buy.frozen_amount > 0
    broker.apply_corporate_action(
        SYMBOL,
        2.0,
        effective_date=DAY_2,
        sim_time=datetime(2024, 1, 3, 9, 0),
    )
    broker.update_prices(
        {SYMBOL: 6.0},
        sim_time=datetime(2024, 1, 3, 9, 30),
        phase="opening_mark",
    )

    result = replay_cash_journal(seed, broker.event_journal)
    result.state.assert_matches_account(account)
    position = result.state.positions[0]
    assert position.shares == 200
    assert position.available_shares == 0
    assert position.pending_t1 == (
        # The action scales exact pending entitlements instead of rounding.
        seed.positions[0].pending_t1[0].__class__("2024-01-02", 200),
    )
    # The pre-seed direct account purchase included its recorded minimum fee.
    assert position.avg_cost == pytest.approx(5.025)
    assert position.current_price == 6.0
    assert result.win_rate is None


def test_fractional_adjustment_factor_reinvests_whole_shares_and_replays_cash_in_lieu():
    """A cash-dividend factor must not enter strict split entitlement logic."""
    account = Account(initial_cash=100_000.0)
    account.buy(SYMBOL, 1_000, 10.8, DAY_1, strict_settlement=False)
    account.update_prices({SYMBOL: 10.8})
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 8, 0)
    )
    broker = Broker(account, slippage=0.0)
    ratio = 125.0496 / 116.7130
    before_nav = account.total_asset

    broker.apply_adjustment_factor_total_return(
        SYMBOL,
        ratio,
        effective_date=DAY_2,
        sim_time=datetime(2024, 1, 3, 9, 0),
    )

    assert account.positions[SYMBOL].shares == 1_071
    assert account.total_asset == pytest.approx(before_nav)
    action = broker.event_journal.snapshot()[-1]
    attrs = dict(action.payload.attributes.entries)
    assert action.payload.action_type == "adjustment_factor_total_return"
    assert attrs["fractional_policy"] == "cash_in_lieu"
    assert attrs["cash_amount"] > 0

    replayed = replay_cash_journal(seed, broker.event_journal)
    replayed.state.assert_matches_account(account)


def test_partial_sell_fill_marks_remaining_position_without_bar_mark():
    account = Account(initial_cash=50_000.0)
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 9, 0)
    )
    broker = Broker(
        account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
        t0_overrides={"600000"},
    )
    broker.set_date(DAY_1)
    broker.set_market_phase("trading")
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 2, 9, 30),
        eligible_from=datetime(2024, 1, 2, 9, 31),
        expire_date=DAY_1,
    ):
        broker.buy_at_market(SYMBOL, shares=200, current_price=10.0)
    broker.execute_market_orders(
        _bar(datetime(2024, 1, 2, 9, 31), 10.0),
        match_at=datetime(2024, 1, 2, 9, 31),
    )
    broker.reset_bar_used_amount()
    with broker.order_creation_scope(
        "on_minute",
        datetime(2024, 1, 2, 9, 32),
        eligible_from=datetime(2024, 1, 2, 9, 33),
        expire_date=DAY_1,
    ):
        broker.sell_at_market(SYMBOL, shares=100)
    broker.execute_market_orders(
        _bar(datetime(2024, 1, 2, 9, 33), 11.0),
        match_at=datetime(2024, 1, 2, 9, 33),
    )

    result = replay_cash_journal(seed, broker.event_journal)
    result.state.assert_matches_account(account)
    assert result.state.positions[0].shares == 100
    assert result.state.positions[0].current_price == 11.0
    assert result.closed_round_trips == ()
    assert result.win_rate is None


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda attrs: attrs.pop("t0"), "lacks required attribute 't0'"),
        (lambda attrs: attrs.__setitem__("t0", 1), "exact bool"),
        (lambda attrs: attrs.__setitem__("side", "sell"), "side differs"),
        (
            lambda attrs: attrs.__setitem__("trade_date", "20240101"),
            "trade_date differs",
        ),
        (lambda attrs: attrs.__setitem__("commission", attrs["commission"] + 1.0), "fee differs"),
    ],
)
def test_fill_attribute_missing_or_tampered_fails_strict_replay(mutation, message):
    _, broker, seed, _ = _buy_only_run()
    wire = broker.event_journal.to_dict()
    fill = next(
        event for event in wire["events"] if event["event_type"] == "fill"
    )
    mutation(fill["payload"]["attributes"])
    tampered = ExecutionEventJournal.from_dict(wire)
    with pytest.raises(CashReplayError, match=message):
        replay_cash_journal(seed, tampered)


@pytest.mark.parametrize(
    "event_type, field, replacement, message",
    [
        ("settlement", "account_id", "wrong-account", "account_id"),
        ("settlement", "amount", 999.0, "cash identity"),
        ("release", "amount", 99_999.0, "exceeds outstanding"),
    ],
)
def test_settlement_and_release_tampering_fails(
    event_type, field, replacement, message
):
    _, broker, seed, _ = _buy_only_run()
    wire = broker.event_journal.to_dict()
    event = next(
        item for item in wire["events"] if item["event_type"] == event_type
    )
    event["payload"][field] = replacement
    tampered = ExecutionEventJournal.from_dict(wire)
    with pytest.raises(CashReplayError, match=message):
        replay_cash_journal(seed, tampered)


def test_missing_or_orphan_settlement_and_release_fail():
    _, broker, seed, _ = _buy_only_run()
    events = broker.event_journal.snapshot()
    missing_settlement = _renumber(
        event
        for event in events
        if event.event_type is not ExecutionEventType.SETTLEMENT
    )
    with pytest.raises(CashReplayError, match="not immediately followed"):
        replay_cash_journal(seed, missing_settlement)

    missing_release = _renumber(
        event
        for event in events
        if event.event_type is not ExecutionEventType.RELEASE
    )
    with pytest.raises(CashReplayError, match="lacks Release"):
        replay_cash_journal(seed, missing_release)

    settlement = next(
        event for event in events
        if event.event_type is ExecutionEventType.SETTLEMENT
    )
    orphan = _renumber((settlement,))
    with pytest.raises(CashReplayError, match="orphan settlement"):
        replay_cash_journal(seed, orphan)


@pytest.mark.parametrize("duplicate_field, message", [
    ("fill_id", "duplicate fill_id"),
    ("settlement_id", "duplicate settlement_id"),
])
def test_duplicate_fill_or_settlement_identity_fails(duplicate_field, message):
    _, broker, seed = _full_round_trip_run()
    wire = broker.event_journal.to_dict()
    fills = [event for event in wire["events"] if event["event_type"] == "fill"]
    settlements = [
        event for event in wire["events"] if event["event_type"] == "settlement"
    ]
    releases = [
        event
        for event in wire["events"]
        if event["event_type"] == "release"
        and event["payload"]["attributes"].get("release_reason") == "fill_settlement"
    ]
    if duplicate_field == "fill_id":
        original = fills[0]["payload"]["fill_id"]
        fills[1]["payload"]["fill_id"] = original
        settlements[1]["payload"]["attributes"]["fill_id"] = original
        releases[1]["payload"]["attributes"]["fill_id"] = original
    else:
        settlements[1]["payload"]["settlement_id"] = settlements[0]["payload"][
            "settlement_id"
        ]
    tampered = ExecutionEventJournal.from_dict(wire)
    with pytest.raises(CashReplayError, match=message):
        replay_cash_journal(seed, tampered)


def test_cash_reducer_explicitly_rejects_futures_vocabulary():
    from diepi.backtest.broker.events import EventAttributes, MarginPayload

    seed = CashReplaySeed(
        as_of=datetime(2024, 1, 2),
        account_id="cash-account",
        initial_cash=100.0,
        initial_frozen_cash=0.0,
    )
    journal = ExecutionEventJournal((
        ExecutionEvent(
            event_id="margin-event",
            event_type=ExecutionEventType.MARGIN,
            simulated_time=datetime(2024, 1, 2, 15, 0),
            phase_seq=10,
            sequence=1,
            payload=MarginPayload(
                account_id="future-account",
                contract="IF2401",
                amount=1.0,
                margin_kind="diagnostic",
                attributes=EventAttributes(),
            ),
        ),
    ))
    with pytest.raises(CashReplayError, match="does not support margin"):
        replay_cash_journal(seed, journal)
