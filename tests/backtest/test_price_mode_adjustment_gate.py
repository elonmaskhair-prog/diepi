"""No ratio=1 or exception-swallowing bypass around AFI-1."""

import copy

import pytest

from diepi.backtest.broker import Account, Broker
from diepi.backtest.engine.price_mode import PriceModeMixin


SYMBOL = "600000.SH"


class _Harness(PriceModeMixin):
    def __init__(
        self, *, data=None, broker=None, strategy_mode="hfq", execution_mode="raw"
    ):
        self._data = data
        self._broker = broker
        self._strategy_price_mode = strategy_mode
        self._execution_price_mode = execution_mode
        self.current_date = "20240103"
        self.current_time = None
        self._same_source_symbols = frozenset()
        self._same_source_skip_adjust = frozenset()

    @staticmethod
    def get_prev_trade_day(_date, _count):
        return "20240102"


class _Ratios:
    def __init__(self, values):
        self.values = values

    def get_adj_ratio(self, symbol, trade_date):
        value = self.values[trade_date]
        if isinstance(value, BaseException):
            raise value
        return value


def _holding_broker():
    account = Account(initial_cash=100_000.0)
    account.buy(
        SYMBOL, 100, 10.0, "20240101", strict_settlement=False)
    account.settle_t1("20240102")
    return Broker(account=account, slippage=0.0)


def test_distinct_price_conversion_requires_factor_provider():
    with pytest.raises(RuntimeError, match="strict adjustment-factor"):
        _Harness(data=None)._convert_price_for_execution(SYMBOL, 10.0)


@pytest.mark.parametrize("ratio", (True, 0.0, -1.0, float("nan"), float("inf")))
def test_distinct_price_conversion_rejects_invalid_ratio(ratio):
    data = _Ratios({"20240103": ratio})
    with pytest.raises(ValueError, match="finite and positive"):
        _Harness(data=data)._convert_price_for_execution(SYMBOL, 10.0)


def test_explicit_same_source_short_circuit_does_not_require_factor():
    harness = _Harness(data=None)
    harness.set_same_source_symbols({SYMBOL})

    assert harness._convert_price_for_execution(SYMBOL, 10.0) == 10.0


def test_raw_same_price_space_does_not_require_or_apply_factor_data():
    broker = _holding_broker()

    class _MustNotRead:
        def get_adj_ratio(self, _symbol, _trade_date):
            raise AssertionError("raw/raw must not read adjustment factors")

    harness = _Harness(
        data=_MustNotRead(),
        broker=broker,
        strategy_mode="raw",
        execution_mode="raw",
    )
    before_account = copy.deepcopy(broker.account)
    before_events = broker.event_journal.snapshot()

    harness._adjust_positions_for_corporate_actions()

    assert broker.account.cash == before_account.cash
    assert broker.account.positions[SYMBOL].shares == (
        before_account.positions[SYMBOL].shares
    )
    assert broker.event_journal.snapshot() == before_events


def test_corporate_action_factor_failure_is_not_swallowed_or_mutating():
    broker = _holding_broker()
    harness = _Harness(
        broker=broker,
        data=_Ratios({
            "20240102": 1.0,
            "20240103": RuntimeError("missing exact factor"),
        }),
    )
    before_account = copy.deepcopy(broker.account)
    before_events = broker.event_journal.snapshot()

    with pytest.raises(RuntimeError, match="missing exact factor"):
        harness._adjust_positions_for_corporate_actions()

    position = broker.account.positions[SYMBOL]
    expected = before_account.positions[SYMBOL]
    assert position.shares == expected.shares
    assert position.current_price == expected.current_price
    assert broker.account.cash == before_account.cash
    assert broker.event_journal.snapshot() == before_events


@pytest.mark.parametrize("bad", (True, 0.0, -1.0, float("nan"), float("inf")))
def test_corporate_action_rejects_invalid_factor_without_continue(bad):
    broker = _holding_broker()
    harness = _Harness(
        broker=broker,
        data=_Ratios({"20240102": 1.0, "20240103": bad}),
    )

    with pytest.raises(ValueError, match="finite and positive"):
        harness._adjust_positions_for_corporate_actions()


def test_parts_per_million_factor_drift_does_not_leak_integer_shares():
    broker = _holding_broker()
    position = broker.account.positions[SYMBOL]
    before = (position.shares, position.current_price, broker.account.cash)
    harness = _Harness(
        broker=broker,
        data=_Ratios({
            "20240102": 125.0496,
            "20240103": 125.0493,
        }),
    )

    harness._adjust_positions_for_corporate_actions()

    position = broker.account.positions[SYMBOL]
    assert (position.shares, position.current_price, broker.account.cash) == before
    assert not any(
        event.event_type.value == 'corporate_action'
        for event in broker.event_journal
    )
