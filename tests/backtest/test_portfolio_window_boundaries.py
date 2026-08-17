"""PortfolioEngine integration for causal cash-session window bounds."""

import pandas as pd

import diepi.backtest.engine.portfolio_engine as portfolio_engine_module
from diepi.backtest.data.stock_pool import PoolSource
from diepi.backtest.engine.portfolio_context import PortfolioContext
from diepi.backtest.engine.portfolio_engine import PortfolioEngine
from diepi.backtest.engine.window import WindowBound
from diepi.backtest.result_contract import ResultStatus
from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy


SYMBOL = "000001.SZ"


class _Provider:
    price_mode = "hfq"
    execution_price_mode = "raw"

    def __init__(self):
        self.trade_days = ["20260807", "20260810", "20260811"]

    def get_trade_days_between(self, start, end):
        return list(self.trade_days)

    def get_prev_trade_day(self, date, n=1):
        return None

    def set_context(self, context):
        self.context = context


class _CashOnly(PortfolioStrategy):
    def __init__(self):
        self.init_calls = 0
        self.finish_calls = 0

    def on_init(self, ctx):
        self.init_calls += 1

    def on_before_market_open(self, ctx):
        return []

    def on_finish(self, ctx):
        self.finish_calls += 1


def test_future_request_uses_loaded_data_cutoff_and_one_contract_warning(
    monkeypatch,
):
    provider = _Provider()
    context = PortfolioContext(
        data_provider=provider,
        initial_cash=100.0,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=[SYMBOL],
    )
    engine = PortfolioEngine(
        "20260807",
        "20260811",
        initial_cash=100.0,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=[SYMBOL],
        benchmark="",
    )
    engine._data_provider = provider
    engine._context = context
    engine._init_engine = lambda: None

    strategy_frame = pd.DataFrame(
        {"close": [10.0, 10.1]},
        index=pd.to_datetime(["2026-08-05", "2026-08-06"]),
    )
    execution_frame = pd.DataFrame(
        {"close": [10.0, 10.1]},
        index=pd.to_datetime(["2026-08-05", "2026-08-07"]),
    )

    def preload(_pool):
        engine._daily_data_cache = {SYMBOL: strategy_frame}
        engine._daily_data_raw_cache = {SYMBOL: execution_frame}

    engine._preload_data = preload
    expected_bound = WindowBound(
        trade_days=("20260807",),
        requested_end="20260811",
        cutoff="20260807",
        wall_clock_cutoff="20260807",
        data_cutoff="20260807",
        truncated=True,
    )
    captured = {}

    def deterministic_bound(
        trade_days, *, requested_end, data_dates=(), now=None
    ):
        captured["trade_days"] = tuple(trade_days)
        captured["requested_end"] = requested_end
        captured["data_dates"] = tuple(data_dates)
        return expected_bound

    monkeypatch.setattr(
        portfolio_engine_module,
        "bound_cash_trade_days",
        deterministic_bound,
    )

    result = engine.run(_CashOnly())

    assert captured == {
        "trade_days": ("20260807", "20260810", "20260811"),
        "requested_end": "20260811",
        "data_dates": ("20260807",),
    }
    assert result.result_contract.status is ResultStatus.SUCCESS
    assert result.result_contract.data_coverage.expected_observations == 1
    assert result.result_contract.data_coverage.actual_observations == 1
    assert len(result.daily_values) == 1
    assert pd.Timestamp(result.daily_values.index[0]).strftime("%Y%m%d") == (
        "20260807"
    )
    warnings = [
        warning
        for warning in result.result_contract.warnings
        if warning.code == "WINDOW_TRUNCATED"
    ]
    assert len(warnings) == 1
    assert warnings[0].message == expected_bound.warning_message


def test_entirely_future_request_is_invalid_instead_of_flat_cash_success(
    monkeypatch,
):
    provider = _Provider()
    provider.trade_days = ["20260810", "20260811"]
    context = PortfolioContext(
        data_provider=provider,
        initial_cash=100.0,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=[SYMBOL],
    )
    engine = PortfolioEngine(
        "20260810",
        "20260811",
        initial_cash=100.0,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=[SYMBOL],
        benchmark="",
    )
    engine._data_provider = provider
    engine._context = context
    engine._init_engine = lambda: None
    frame = pd.DataFrame(
        {"close": [10.0]}, index=pd.to_datetime(["2026-08-07"])
    )

    def preload(_pool):
        engine._daily_data_cache = {SYMBOL: frame}
        engine._daily_data_raw_cache = {SYMBOL: frame.copy()}

    engine._preload_data = preload
    expected_bound = WindowBound(
        trade_days=(),
        requested_end="20260811",
        cutoff="20260807",
        wall_clock_cutoff="20260807",
        data_cutoff="20260807",
        truncated=True,
    )
    monkeypatch.setattr(
        portfolio_engine_module,
        "bound_cash_trade_days",
        lambda *args, **kwargs: expected_bound,
    )

    strategy = _CashOnly()
    result = engine.run(strategy)

    assert result.result_contract.status is ResultStatus.INVALID
    assert result.result_contract.reason.code == "NO_EXPECTED_OBSERVATIONS"
    assert result.daily_values.empty
    assert strategy.init_calls == 1
    assert strategy.finish_calls == 1
    assert context._broker.get_open_orders() == []
    warnings = [
        warning
        for warning in result.result_contract.warnings
        if warning.code == "WINDOW_TRUNCATED"
    ]
    assert len(warnings) == 1
    assert warnings[0].message == expected_bound.warning_message
