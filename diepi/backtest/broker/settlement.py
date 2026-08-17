"""Broker/account transaction boundary.

The matcher coordinates several mutable objects for one fill.  This module
provides a deliberately small snapshot/rollback unit of work so an exception
from any participant cannot publish a partial economic result.  Fee events are
not emitted here; the journal requires a complete simulated event context that
the broker does not yet own.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


class SettlementInvariantError(ValueError):
    """Raised before commit when settlement-owned state is inconsistent."""


@dataclass
class _SettlementSnapshot:
    account_cash: float
    account_frozen_cash: float
    positions: Dict[str, Any]
    position_states: List[Any]
    order_states: List[Any]
    orders: Dict[str, Any]
    order_history: List[Any]
    bar_used_amount: Dict[str, float]
    order_fee_states: Dict[str, Any]
    pending_open_orders: List[Any]
    pending_close_orders: List[Any]
    event_journal_events: tuple
    order_id_counter: int
    event_id_counter: int
    fill_id_counter: int
    settlement_id_counter: int
    corporate_action_id_counter: int
    target_intent_id_counter: int
    target_batch_id_counter: int
    target_intents: List[Any]
    target_achievements: List[Any]
    target_order_intents: Dict[str, str]
    stamped_orders: Dict[int, Any]
    journal_reservations: Dict[Any, float]
    terminal_event_orders: set
    broker_current_date: Optional[str]
    broker_market_phase: str
    open_buy_stats: Dict[str, Any]

    @classmethod
    def capture(cls, broker: Any, order: Any = None) -> "_SettlementSnapshot":
        account = broker.account
        positions = tuple(account.positions.values())
        orders = []
        seen_orders = set()
        for candidate in (
            tuple(account.orders.values())
            + tuple(account.order_history)
            + tuple(broker._pending_open_orders)
            + tuple(broker._pending_close_orders)
            + ((order,) if order is not None else ())
        ):
            identity = id(candidate)
            if identity not in seen_orders:
                seen_orders.add(identity)
                orders.append(candidate)
        return cls(
            account_cash=account.cash,
            account_frozen_cash=account.frozen_cash,
            positions=dict(account.positions),
            position_states=[
                (position, deepcopy(position.__dict__))
                for position in positions
            ],
            order_states=[
                (candidate, deepcopy(candidate.__dict__))
                for candidate in orders
            ],
            orders=dict(account.orders),
            order_history=list(account.order_history),
            bar_used_amount=dict(broker._bar_used_amount),
            order_fee_states=dict(broker._order_fee_states),
            pending_open_orders=list(broker._pending_open_orders),
            pending_close_orders=list(broker._pending_close_orders),
            event_journal_events=broker._event_journal.snapshot(),
            order_id_counter=broker._order_id_counter,
            event_id_counter=broker._event_id_counter,
            fill_id_counter=broker._fill_id_counter,
            settlement_id_counter=broker._settlement_id_counter,
            corporate_action_id_counter=getattr(
                broker, '_corporate_action_id_counter', 0),
            target_intent_id_counter=getattr(
                broker, '_target_intent_id_counter', 0),
            target_batch_id_counter=getattr(
                broker, '_target_batch_id_counter', 0),
            target_intents=list(getattr(broker, '_target_intents', ())),
            target_achievements=list(
                getattr(broker, '_target_achievements', ())),
            target_order_intents=dict(
                getattr(broker, '_target_order_intents', {})),
            stamped_orders=dict(broker._stamped_orders),
            journal_reservations=dict(broker._journal_reservations),
            terminal_event_orders=set(broker._terminal_event_orders),
            broker_current_date=broker.current_date,
            broker_market_phase=broker._market_phase,
            open_buy_stats={
                name: getattr(broker, name)
                for name in (
                    '_open_buy_total',
                    '_open_buy_auto_resized_count',
                    '_open_buy_reject_insuff_cash_count',
                    '_open_buy_reject_invalid_count',
                    '_open_buy_resize_ratio_sum',
                )
            },
        )

    def restore(self, broker: Any, order: Any = None) -> None:
        account = broker.account

        # Restore objects before their owning mappings.  A position deleted by
        # a full sell is reinserted with its original identity; a position
        # created by a failed buy disappears when the mapping is restored.
        for position, state in self.position_states:
            position.__dict__.clear()
            position.__dict__.update(deepcopy(state))
        for candidate, state in self.order_states:
            candidate.__dict__.clear()
            candidate.__dict__.update(deepcopy(state))

        account.cash = self.account_cash
        account.frozen_cash = self.account_frozen_cash
        account.positions.clear()
        account.positions.update(self.positions)
        account.orders.clear()
        account.orders.update(self.orders)
        account.order_history[:] = self.order_history
        broker._bar_used_amount.clear()
        broker._bar_used_amount.update(self.bar_used_amount)
        broker._order_fee_states.clear()
        broker._order_fee_states.update(self.order_fee_states)
        broker._pending_open_orders[:] = self.pending_open_orders
        broker._pending_close_orders[:] = self.pending_close_orders
        broker._event_journal._restore_snapshot(self.event_journal_events)
        broker._order_id_counter = self.order_id_counter
        broker._event_id_counter = self.event_id_counter
        broker._fill_id_counter = self.fill_id_counter
        broker._settlement_id_counter = self.settlement_id_counter
        broker._corporate_action_id_counter = self.corporate_action_id_counter
        broker._target_intent_id_counter = self.target_intent_id_counter
        broker._target_batch_id_counter = self.target_batch_id_counter
        broker._target_intents[:] = self.target_intents
        broker._target_achievements[:] = self.target_achievements
        broker._target_order_intents.clear()
        broker._target_order_intents.update(self.target_order_intents)
        broker._stamped_orders.clear()
        broker._stamped_orders.update(self.stamped_orders)
        broker._journal_reservations.clear()
        broker._journal_reservations.update(self.journal_reservations)
        broker._terminal_event_orders.clear()
        broker._terminal_event_orders.update(self.terminal_event_orders)
        broker.current_date = self.broker_current_date
        broker._market_phase = self.broker_market_phase
        for name, value in self.open_buy_stats.items():
            setattr(broker, name, value)


class SettlementUnitOfWork:
    """Rollback every settlement-owned mutation unless explicitly committed."""

    def __init__(self, broker: Any, order: Any = None) -> None:
        self._broker = broker
        self._order = order
        self._snapshot = _SettlementSnapshot.capture(broker, order)
        self._committed = False

    def __enter__(self) -> "SettlementUnitOfWork":
        return self

    def commit(self) -> None:
        self._committed = True

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        if exc_type is not None or not self._committed:
            self._snapshot.restore(self._broker, self._order)
        # Never suppress an exception, including BaseException subclasses.
        return False


__all__ = ["SettlementInvariantError", "SettlementUnitOfWork"]
