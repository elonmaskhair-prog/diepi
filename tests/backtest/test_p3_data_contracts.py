"""P3 regressions for context, compatibility, symbols, and pre-close."""

from datetime import datetime

import pytest

from diepi.backtest.broker import Account, Broker
from diepi.backtest.broker.broker import BarData
from diepi.backtest.broker.order import OrderStatus
from diepi.backtest.data.data_provider import ParameterValidator
from diepi.backtest.data.stock_pool import PoolSource, StockPool
from diepi.backtest.engine.portfolio_context import PortfolioContext
from diepi.backtest.engine.portfolio_engine import PortfolioEngine
from diepi.backtest.result_contract import ResultStatus
from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy


SYMBOL = "000001.SZ"
TRADE_DAY = "20240102"


class _Provider:
    price_mode = "hfq"
    execution_price_mode = "hfq"

    def get_trade_days_between(self, start, end):
        return [TRADE_DAY]

    def get_prev_trade_day(self, date, n=1):
        return None

    def get_next_trade_day(self, date, n=1):
        return "20240103"

    def is_trade_day(self, date):
        return date == TRADE_DAY

    def set_context(self, context):
        self.context = context


class _CashOnly(PortfolioStrategy):
    def on_before_market_open(self, ctx):
        return []


def _portfolio_engine():
    provider = _Provider()
    context = PortfolioContext(
        data_provider=provider,
        initial_cash=100.0,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=[SYMBOL],
    )
    engine = PortfolioEngine(
        TRADE_DAY,
        TRADE_DAY,
        initial_cash=100.0,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=[SYMBOL],
        benchmark="",
    )
    engine._data_provider = provider
    engine._context = context
    engine._init_engine = lambda: None
    return engine


def _assumptions(contract):
    return {item.key: item.value for item in contract.assumptions}


def test_portfolio_on_init_history_reads_fail_with_explicit_causal_error():
    context = PortfolioContext(data_provider=_Provider(), initial_cash=100.0)

    with pytest.raises(RuntimeError, match="unavailable during on_init"):
        context.get_daily(SYMBOL, days=1)
    with pytest.raises(RuntimeError, match="unavailable during on_init"):
        context.get_minute(SYMBOL, days=1)
    for query in (
        context.is_trade_day,
        context.get_prev_trade_day,
        context.get_next_trade_day,
    ):
        with pytest.raises(RuntimeError, match="unavailable during on_init"):
            query()

    assert context.is_trade_day(TRADE_DAY) is True
    assert context.get_prev_trade_day(TRADE_DAY) is None
    assert context.get_next_trade_day(TRADE_DAY) == "20240103"


def test_portfolio_compatibility_symbols_are_visible_once_on_success():
    engine = _portfolio_engine()

    def compatibility_preload(_pool):
        engine._data_contract_compatibility_symbols.update(
            {"600000.SH", SYMBOL}
        )

    engine._preload_data = compatibility_preload
    result = engine.run(_CashOnly())

    contract = result.result_contract
    assumptions = _assumptions(contract)
    assert contract.status is ResultStatus.SUCCESS
    assert assumptions["data.contract_path"] == (
        "legacy_provider_compatibility"
    )
    assert assumptions["data.contract_compatibility_symbols"] == (
        "000001.SZ,600000.SH"
    )
    assert sum(
        item.key == "data.contract_compatibility_symbols"
        for item in contract.assumptions
    ) == 1
    assert sum(
        warning.code == "DATA_CONTRACT_COMPATIBILITY_PATH"
        for warning in contract.warnings
    ) == 1


def test_portfolio_compatibility_symbols_survive_preload_failure_once():
    engine = _portfolio_engine()
    failure = LookupError("compatibility preload failed")

    def failing_preload(_pool):
        engine._data_contract_compatibility_symbols.add(SYMBOL)
        raise failure

    engine._preload_data = failing_preload

    with pytest.raises(LookupError) as captured:
        engine.run(_CashOnly())

    assert captured.value is failure
    contract = engine.last_result_contract
    assert contract.status is ResultStatus.FAILED
    assert _assumptions(contract)[
        "data.contract_compatibility_symbols"
    ] == SYMBOL
    assert sum(
        item.key == "data.contract_compatibility_symbols"
        for item in contract.assumptions
    ) == 1
    assert sum(
        warning.code == "DATA_CONTRACT_COMPATIBILITY_PATH"
        for warning in contract.warnings
    ) == 1


def test_stock_pool_reuses_fund_and_bse_symbol_normalization():
    raw = [
        "563300",
        "160223",
        "180101",
        "920002",
        "870001",
        " 510300.sh ",
    ]

    normalized = StockPool().get_pool(
        PoolSource.SPECIFIED, symbols=raw
    )

    assert normalized == [
        ParameterValidator.normalize_symbol(symbol) for symbol in raw
    ]
    assert normalized == [
        "563300.SH",
        "160223.SZ",
        "180101.SZ",
        "920002.BJ",
        "870001.BJ",
        "510300.SH",
    ]


def _execution_bar(pre_close: float) -> BarData:
    return BarData(
        symbol=SYMBOL,
        trade_time=datetime(2024, 1, 2, 9, 31),
        open=10.0,
        high=10.0,
        low=10.0,
        close=10.0,
        vol=1_000_000.0,
        amount=10_000_000.0,
        pre_close=pre_close,
    )


def _execution_broker() -> Broker:
    account = Account(
        initial_cash=100_000.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    broker = Broker(account, slippage=0.0, liquidity_cap_ratio=1.0)
    broker.set_date(TRADE_DAY)
    broker.set_market_phase("trading")
    return broker


def test_direct_broker_nonpositive_pre_close_fails_before_fill():
    broker = _execution_broker()
    order = broker.buy_at_market(
        SYMBOL, shares=100, current_price=10.0
    )
    before = (
        broker.account.cash,
        broker.account.frozen_cash,
        order.status,
        order.filled_shares,
        len(broker.event_journal),
    )

    with pytest.raises(ValueError, match="pre_close must be positive"):
        broker.execute_market_orders(_execution_bar(0.0))

    assert (
        broker.account.cash,
        broker.account.frozen_cash,
        order.status,
        order.filled_shares,
        len(broker.event_journal),
    ) == before


def test_explicit_limit_exemption_preserves_zero_pre_close_execution():
    broker = _execution_broker()
    broker.set_limit_check_exempt({SYMBOL: {TRADE_DAY}})
    order = broker.buy_at_market(
        SYMBOL, shares=100, current_price=10.0
    )

    executed = broker.execute_market_orders(_execution_bar(0.0))

    assert executed == [order]
    assert order.status is OrderStatus.FILLED
    assert order.filled_shares == 100
