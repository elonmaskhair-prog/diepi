"""Execution-event integration tests against real cash-broker mutations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from diepi.backtest.broker import Account, Broker
from diepi.backtest.broker.broker import BarData
from diepi.backtest.broker.events import (
    EventAttributes,
    ExecutionEventJournal,
    ExecutionEventType,
    FillPayload,
)
from diepi.backtest.broker.order import OrderStatus
from diepi.backtest.engine.context import Context
from diepi.backtest.engine.price_mode import PriceModeMixin


SYMBOL = "600000.SH"
T0 = datetime(2024, 1, 2, 9, 0)
T1 = datetime(2024, 1, 2, 10, 0)
T2 = datetime(2024, 1, 2, 10, 1)


def _bar(*, when=T2, amount=1_000_000.0):
    return BarData(
        symbol=SYMBOL,
        trade_time=when,
        open=10.0,
        high=10.0,
        low=10.0,
        close=10.0,
        amount=amount,
        pre_close=10.0,
    )


def _trading_broker(*, cash=100_000.0):
    broker = Broker(
        account=Account(initial_cash=cash),
        slippage=0.0,
        liquidity_cap_ratio=1.0,
    )
    broker.set_date("20240102")
    broker.set_market_phase("trading")
    return broker


def _event_types(broker):
    return tuple(event.event_type for event in broker.event_journal)


def test_public_order_fill_has_deterministic_identity_time_and_exact_views():
    broker = _trading_broker()
    with broker.order_creation_scope(
            "on_minute", pd.Timestamp(T1),
            eligible_from=pd.Timestamp(T1), expire_date="20240102"):
        order = broker.buy_at_market(
            SYMBOL, shares=100, current_price=10.0, note="audit")

    assert order.order_id == "order-000000000001"
    assert type(order.create_time) is datetime
    assert order.create_time == T1
    assert _event_types(broker) == (
        ExecutionEventType.ORDER_CREATED,
        ExecutionEventType.ORDER_ACCEPTED,
        ExecutionEventType.RESERVE,
    )

    broker.execute_market_orders(_bar(), match_at=T2)
    assert order.status is OrderStatus.FILLED
    assert order.update_time == T2
    assert _event_types(broker)[-3:] == (
        ExecutionEventType.FILL,
        ExecutionEventType.SETTLEMENT,
        ExecutionEventType.RELEASE,
    )
    assert [event.phase_seq for event in broker.event_journal] == [
        40, 40, 40, 45, 45, 45,
    ]

    fill = broker.event_journal.fill_view()[0]
    fee = broker.event_journal.fee_view()[0]
    fill_event = broker.event_journal.filter_events(
        event_types=(ExecutionEventType.FILL,))[0]
    assert fill.fill_id == "fill-000000000001"
    assert fill.amount == fill_event.payload.amount == 1_000.0
    assert fill.fee == fill_event.payload.fee == order.total_fees
    assert fee.fill_id == fill.fill_id
    assert fee.total == fill_event.payload.fee
    assert fee.commission == dict(
        fill_event.payload.attributes.entries)["commission"]
    assert fee.stamp_duty == dict(
        fill_event.payload.attributes.entries)["stamp_duty"]
    assert fee.transfer_fee == dict(
        fill_event.payload.attributes.entries)["transfer_fee"]
    settlement = broker.event_journal.filter_events(
        event_types=(ExecutionEventType.SETTLEMENT,))[0]
    assert settlement.payload.settlement_id == "settlement-000000000001"
    assert [event.event_id for event in broker.event_journal] == [
        f"event-{number:012d}" for number in range(1, 7)
    ]


def test_daily_bar_fill_uses_match_at_instead_of_midnight_bar_timestamp():
    previous_close = datetime(2024, 1, 2, 15, 0)
    next_midnight = datetime(2024, 1, 3)
    next_day_start = datetime(2024, 1, 3, 9, 0)
    next_close = datetime(2024, 1, 3, 15, 0)

    # The unrelated holding forces a 09:00 T+1 event into the journal.  A
    # date-only daily bar would therefore move event time backwards unless the
    # API-level match_at is propagated into nested fill settlement.
    account = Account(initial_cash=100_000.0)
    account.buy(
        SYMBOL, 100, 10.0, "20240102", strict_settlement=False)
    broker = Broker(
        account=account,
        slippage=0.0,
        liquidity_cap_ratio=1.0,
    )
    broker.set_date("20240102")
    broker.set_market_phase("trading")
    with broker.order_creation_scope(
            "on_day", previous_close,
            eligible_from=next_midnight, expire_date="20240103"):
        order = broker.buy_at_market(
            SYMBOL, shares=100, current_price=10.0)

    broker.on_day_start("20240103", sim_time=next_day_start)
    assert broker.event_journal[-1].simulated_time == next_day_start

    broker.execute_market_orders(
        _bar(when=next_midnight), match_at=next_close)

    assert order.status is OrderStatus.FILLED
    assert order.update_time == next_close
    terminal_events = broker.event_journal.filter_events(event_types=(
        ExecutionEventType.FILL,
        ExecutionEventType.SETTLEMENT,
        ExecutionEventType.RELEASE,
    ))
    assert terminal_events
    assert all(event.simulated_time == next_close for event in terminal_events)


def test_rejected_cancelled_and_expired_orders_emit_terminal_facts_and_release():
    broker = _trading_broker()
    rejected = broker.buy_at_market(
        SYMBOL, shares=0, current_price=10.0)
    assert rejected.status is OrderStatus.REJECTED
    assert _event_types(broker)[-2:] == (
        ExecutionEventType.ORDER_CREATED,
        ExecutionEventType.ORDER_REJECTED,
    )

    active = broker.buy_at_price(SYMBOL, 9.0, shares=100)
    assert broker.cancel_order(active.order_id)
    assert _event_types(broker)[-2:] == (
        ExecutionEventType.RELEASE,
        ExecutionEventType.ORDER_CANCELED,
    )

    expiring = broker.buy_at_price(SYMBOL, 8.0, shares=100)
    broker.on_day_start("20240103", sim_time=datetime(2024, 1, 3, 9, 0))
    assert expiring.status is OrderStatus.CANCELLED
    assert _event_types(broker)[-2:] == (
        ExecutionEventType.RELEASE,
        ExecutionEventType.ORDER_EXPIRED,
    )


def test_t1_unlock_and_context_mark_are_real_deterministic_paths():
    account = Account(initial_cash=100_000.0)
    account.buy(
        SYMBOL, 100, 10.0, "20240102", strict_settlement=False)
    broker = Broker(account=account, slippage=0.0)

    day_start = datetime(2024, 1, 3, 9, 0)
    broker.on_day_start("20240103", sim_time=day_start)
    unlock = broker.event_journal.filter_events(
        event_types=(ExecutionEventType.T1_UNLOCK,))[0]
    assert unlock.simulated_time == day_start
    assert unlock.payload.quantity == 100
    assert unlock.payload.trade_date == "2024-01-03"
    assert account.positions[SYMBOL].available_shares == 100

    context = object.__new__(Context)
    context._broker = broker
    context.current_time = pd.Timestamp("2024-01-03 09:30:00")
    context._update_prices(
        {SYMBOL: 12.5}, phase="opening_mark")
    mark = broker.event_journal.filter_events(
        event_types=(ExecutionEventType.MARK,))[0]
    assert type(mark.simulated_time) is datetime
    assert mark.simulated_time == datetime(2024, 1, 3, 9, 30)
    assert mark.phase_seq == 25
    assert mark.payload.price == 12.5
    assert account.positions[SYMBOL].last_update == mark.simulated_time


class _RatioData:
    @staticmethod
    def get_adj_ratio(symbol, trade_date):
        return 2.0 if trade_date == "20240103" else 1.0


class _PriceModeHarness(PriceModeMixin):
    def __init__(self, broker):
        self._broker = broker
        self._data = _RatioData()
        self._execution_price_mode = "raw"
        self.current_date = "20240103"
        self.current_time = datetime(2024, 1, 3, 9, 0)
        self._same_source_skip_adjust = frozenset()

    @staticmethod
    def get_prev_trade_day(current_date, count):
        assert current_date == "20240103" and count == 1
        return "20240102"


def test_price_mode_corporate_action_cancels_old_price_orders_then_records_ratio():
    account = Account(initial_cash=100_000.0)
    account.buy(
        SYMBOL, 100, 10.0, "20240101", strict_settlement=False)
    account.settle_t1("20240102")
    account.update_prices({SYMBOL: 10.0})
    broker = Broker(account=account, slippage=0.0)
    broker.set_date("20240102")
    broker.set_market_phase("trading")
    sell = broker.sell_at_price(SYMBOL, 11.0, shares=100)

    _PriceModeHarness(broker)._adjust_positions_for_corporate_actions()

    position = account.positions[SYMBOL]
    assert sell.status is OrderStatus.CANCELLED
    assert position.shares == 200
    assert position.frozen_shares == 0
    assert position.current_price == 5.0
    events = broker.event_journal.snapshot()
    action = events[-1]
    assert action.event_type is ExecutionEventType.CORPORATE_ACTION
    assert action.payload.action_type == "adjustment_factor_total_return"
    assert action.payload.action_id == "corporate-action-000000000001"
    assert action.payload.effective_date == "2024-01-03"
    attributes = dict(action.payload.attributes.entries)
    assert attributes["ratio"] == 2.0
    assert attributes["fractional_policy"] == "cash_in_lieu"
    assert attributes["cash_amount"] == 0.0
    assert events[-3].event_type is ExecutionEventType.RELEASE
    assert events[-2].event_type is ExecutionEventType.ORDER_CANCELED


def test_fractional_corporate_entitlement_fails_without_any_half_event_or_mutation():
    account = Account(initial_cash=100_000.0)
    account.buy(
        SYMBOL, 101, 10.0, "20240101", strict_settlement=False)
    account.settle_t1("20240102")
    broker = Broker(account=account)
    broker.set_date("20240102")
    broker.set_market_phase("trading")
    order = broker.sell_at_price(SYMBOL, 11.0, shares=100)
    before_position = deepcopy(account.positions[SYMBOL].__dict__)
    before_order = deepcopy(order.__dict__)
    before_events = broker.event_journal.snapshot()
    before_event_counter = broker._event_id_counter

    with pytest.raises(ValueError, match="fractional split entitlement"):
        broker.apply_corporate_action(
            SYMBOL,
            1.5,
            effective_date="20240102",
            sim_time=datetime(2024, 1, 2, 9, 0),
        )

    assert account.positions[SYMBOL].__dict__ == before_position
    assert order.__dict__ == before_order
    assert broker.account.orders[order.order_id] is order
    assert broker.event_journal.snapshot() == before_events
    assert broker._corporate_action_id_counter == 0
    assert broker._event_id_counter == before_event_counter


def test_append_then_raise_rolls_back_corporate_action_journal_and_ids(monkeypatch):
    account = Account(initial_cash=100_000.0)
    account.buy(
        SYMBOL, 100, 10.0, "20240101", strict_settlement=False)
    broker = Broker(account=account)
    before_position = deepcopy(account.positions[SYMBOL].__dict__)
    original = Broker._commit_prepared_events

    def append_then_raise(self, events):
        original(self, events)
        raise RuntimeError("injected journal failure")

    monkeypatch.setattr(Broker, "_commit_prepared_events", append_then_raise)
    with pytest.raises(RuntimeError, match="injected journal failure"):
        broker.apply_corporate_action(
            SYMBOL,
            2.0,
            effective_date="20240102",
            sim_time=datetime(2024, 1, 2, 9, 0),
        )

    assert account.positions[SYMBOL].__dict__ == before_position
    assert broker.event_journal.snapshot() == ()
    assert broker._event_id_counter == 0
    assert broker._corporate_action_id_counter == 0


def test_t1_and_mark_append_failures_restore_economic_state_and_journal(monkeypatch):
    account = Account(initial_cash=100_000.0)
    account.buy(
        SYMBOL, 100, 10.0, "20240102", strict_settlement=False)
    broker = Broker(account=account)
    before_position = deepcopy(account.positions[SYMBOL].__dict__)
    original = Broker._commit_prepared_events

    def append_then_raise(self, events):
        original(self, events)
        raise RuntimeError("injected event failure")

    monkeypatch.setattr(Broker, "_commit_prepared_events", append_then_raise)
    with pytest.raises(RuntimeError, match="event failure"):
        broker.on_day_start(
            "20240103", sim_time=datetime(2024, 1, 3, 9, 0))
    assert account.positions[SYMBOL].__dict__ == before_position
    assert broker.current_date is None
    assert broker.event_journal.snapshot() == ()
    assert broker._event_id_counter == 0

    monkeypatch.setattr(Broker, "_commit_prepared_events", original)
    account.settle_t1("20240103")
    before_position = deepcopy(account.positions[SYMBOL].__dict__)
    monkeypatch.setattr(Broker, "_commit_prepared_events", append_then_raise)
    with pytest.raises(RuntimeError, match="event failure"):
        broker.update_prices(
            {SYMBOL: 12.0}, sim_time=datetime(2024, 1, 3, 9, 30),
            phase="opening_mark")
    assert account.positions[SYMBOL].__dict__ == before_position
    assert broker.event_journal.snapshot() == ()
    assert broker._event_id_counter == 0


def test_fee_view_uses_recorded_total_without_recalculating_components():
    journal = ExecutionEventJournal()
    journal.record(
        event_id="event-1",
        event_type=ExecutionEventType.FILL,
        simulated_time=T1,
        phase_seq=45,
        payload=FillPayload(
            fill_id="fill-1",
            order_id="order-1",
            symbol=SYMBOL,
            quantity=100,
            price=10.0,
            amount=1_000.0,
            fee=7.0,
            attributes=EventAttributes((("commission", 1.0),)),
        ),
    )
    fee = journal.fee_view()[0]
    assert fee.total == 7.0
    assert fee.commission == 1.0
    assert fee.stamp_duty is None


def test_fill_failure_after_journal_append_restores_all_counters_and_retry_ids(
        monkeypatch):
    broker = _trading_broker()
    order = broker.buy_at_market(
        SYMBOL, shares=100, current_price=10.0)
    before_events = broker.event_journal.snapshot()
    before_cash = broker.account.cash
    before_frozen = broker.account.frozen_cash
    original = Broker._commit_fill_events

    def append_then_raise(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("injected post-journal failure")

    monkeypatch.setattr(Broker, "_commit_fill_events", append_then_raise)
    with pytest.raises(RuntimeError, match="post-journal"):
        broker.execute_market_orders(_bar(), match_at=T2)

    assert broker.event_journal.snapshot() == before_events
    assert broker._fill_id_counter == 0
    assert broker._settlement_id_counter == 0
    assert broker.account.cash == before_cash
    assert broker.account.frozen_cash == before_frozen
    assert order.status is OrderStatus.SUBMITTED

    monkeypatch.setattr(Broker, "_commit_fill_events", original)
    broker.execute_market_orders(_bar(), match_at=T2)
    assert broker.event_journal.fill_view()[0].fill_id == "fill-000000000001"


def test_submission_journal_failure_rolls_back_reservation_and_order_id(monkeypatch):
    broker = _trading_broker()
    original = Broker._commit_prepared_events

    def append_then_raise(self, events):
        original(self, events)
        raise RuntimeError("injected submission failure")

    monkeypatch.setattr(Broker, "_commit_prepared_events", append_then_raise)
    with pytest.raises(RuntimeError, match="submission failure"):
        broker.buy_at_price(SYMBOL, 9.0, shares=100)

    assert broker.account.orders == {}
    assert broker.account.frozen_cash == 0.0
    assert broker.event_journal.snapshot() == ()
    assert broker._order_id_counter == 0
    assert broker._event_id_counter == 0
    assert broker._journal_reservations == {}

    monkeypatch.setattr(Broker, "_commit_prepared_events", original)
    order = broker.buy_at_price(SYMBOL, 9.0, shares=100)
    assert order.order_id == "order-000000000001"
    assert broker.event_journal[0].event_id == "event-000000000001"


def test_simulated_time_normalizes_timestamp_and_rejects_nat_or_timezone():
    broker = _trading_broker()
    with broker.order_creation_scope(
            "on_minute", pd.Timestamp(T1), expire_date="20240102"):
        order = broker.buy_at_price(SYMBOL, 9.0, shares=100)
    assert type(order.sim_created_at) is datetime
    assert type(broker.event_journal[0].simulated_time) is datetime

    with pytest.raises(ValueError, match="Invalid simulated time"):
        with broker.order_creation_scope("on_minute", pd.NaT):
            pass
    with pytest.raises(ValueError, match="timezone-naive"):
        with broker.order_creation_scope(
                "on_minute", pd.Timestamp("2024-01-02", tz="UTC")):
            pass


def test_journal_json_is_identical_across_python_hash_seeds(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = """
from datetime import datetime
from diepi.backtest.broker import Account, Broker
b = Broker(Account(initial_cash=100000.0), slippage=0.0)
b.set_date('20240102')
b.set_market_phase('trading')
with b.order_creation_scope('on_minute', datetime(2024,1,2,10), expire_date='20240102'):
    o = b.buy_at_price('600000.SH', 9.0, shares=100, note='seed')
b.cancel_order(o.order_id)
print(b.event_journal.to_json())
"""
    outputs = []
    for seed in ("1", "7", "999"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        outputs.append(completed.stdout.strip())
    assert outputs[0] == outputs[1] == outputs[2]
    parsed = json.loads(outputs[0])
    assert [event["sequence"] for event in parsed["events"]] == [1, 2, 3, 4, 5]
