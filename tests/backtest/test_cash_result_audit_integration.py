"""Strict binding between cash replay evidence and public result objects."""

from datetime import datetime

import pytest

from diepi.backtest.broker import (
    Account,
    CashAuditBundle,
    CashReplaySeed,
)
from diepi.backtest.broker.events import ExecutionEventJournal
from diepi.backtest.engine.backtest_engine import BacktestResult
from diepi.backtest.engine.portfolio_engine import PortfolioResult


SYMBOL = "600000.SH"


def _nonempty_audit():
    account = Account(initial_cash=1_000.0)
    account.buy(
        SYMBOL, 100, 5.0, "20240102", strict_settlement=False)
    seed = CashReplaySeed.from_account(
        account, as_of=datetime(2024, 1, 2, 15, 0)
    )
    return CashAuditBundle.from_parts(seed, ExecutionEventJournal())


def _result(result_type, **overrides):
    audit = overrides.pop("cash_audit", _nonempty_audit())
    common = dict(
        start_date="20240102",
        end_date="20240102",
        initial_cash=audit.seed.initial_nav,
        final_value=audit.replay().state.nav,
        total_return=0.0,
        annual_return=0.0,
        max_drawdown=0.0,
        trade_count=0,
        sharpe_ratio=None,
        win_rate=None,
        trades=[],
        cash_audit=audit,
    )
    common.update(overrides)
    if result_type is BacktestResult:
        common["symbol"] = SYMBOL
    return result_type(**common)


@pytest.mark.parametrize("result_type", (BacktestResult, PortfolioResult))
@pytest.mark.parametrize(
    "override, message",
    [
        ({"initial_cash": 999.0}, "initial_cash"),
        ({"final_value": 123_456.0}, "final_value"),
        ({"total_return": 0.25}, "total_return"),
    ],
)
def test_result_rejects_audit_nav_or_return_mismatch(
    result_type, override, message
):
    with pytest.raises(ValueError, match=message):
        _result(result_type, **override)


@pytest.mark.parametrize("result_type", (BacktestResult, PortfolioResult))
def test_audit_derived_properties_revalidate_mutable_result(result_type):
    result = _result(result_type)
    assert result.fills == ()

    result.trades = [{"tampered": True}]
    with pytest.raises(ValueError, match="trades"):
        _ = result.fees

    result.trades = []
    result.final_value += 1.0
    with pytest.raises(ValueError, match="final_value"):
        _ = result.total_fees

    result.cash_audit = None
    assert result.fills == ()


def test_nonempty_seed_initial_nav_includes_marked_positions():
    audit = _nonempty_audit()
    seed = audit.seed
    assert seed.positions
    assert seed.initial_nav == pytest.approx(
        seed.initial_cash
        + seed.initial_frozen_cash
        + sum(
            position.shares * position.current_price
            for position in seed.positions
        )
    )
