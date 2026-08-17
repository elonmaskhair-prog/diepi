"""BacktestEngine integration for causal cash-session window bounds."""

import pandas as pd

import diepi.backtest.engine.backtest_engine as backtest_engine_module
from diepi.backtest.broker import Account, Broker
from diepi.backtest.engine.backtest_engine import BacktestEngine
from diepi.backtest.engine.context import Context
from diepi.backtest.engine.window import WindowBound
from diepi.backtest.result_contract import ResultStatus
from diepi.backtest.strategy.base import Strategy


SYMBOL = "000001.SZ"


class _Provider:
    price_mode = "hfq"
    execution_price_mode = "raw"

    def __init__(self, trade_days):
        self.trade_days = list(trade_days)
        self.frame = pd.DataFrame(
            {
                "open": [10.0, 10.1],
                "high": [10.2, 10.3],
                "low": [9.8, 9.9],
                "close": [10.1, 10.2],
                "pre_close": [9.9, 10.1],
                "vol": [1000.0, 1000.0],
                "amount": [10000.0, 10000.0],
            },
            index=pd.to_datetime(["2026-08-05", "2026-08-07"]),
        )

    def get_trade_days_between(self, start, end):
        return list(self.trade_days)

    def get_daily(self, *args, **kwargs):
        return self.frame.copy()

    def set_context(self, context):
        self.context = context


def _engine(trade_days, start="20260807"):
    provider = _Provider(trade_days)
    account = Account(
        initial_cash=100.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    context = Context(
        broker=Broker(account, slippage=0.0), data_provider=provider
    )
    context.set_symbol(SYMBOL)
    engine = BacktestEngine(
        SYMBOL,
        start,
        "20260811",
        initial_cash=100.0,
        freq="daily",
        on_missing_data="ignore",
    )
    engine._data_provider = provider
    engine._context = context
    engine._init_engine = lambda: None
    return engine


def _append_flat_nav(engine, day, **kwargs):
    engine._current_day_high_market_value = 0.0
    engine._current_day_high_total_value = 100.0
    engine._current_day_low_market_value = 0.0
    engine._current_day_low_total_value = 100.0
    engine._daily_values.append(
        {
            "date": day,
            "cash": 100.0,
            "market_value": 0.0,
            "total_value": 100.0,
            "intraday_high_market_value": 0.0,
            "intraday_high_total_value": 100.0,
            "intraday_low_market_value": 0.0,
            "intraday_low_total_value": 100.0,
        }
    )


def test_int64_trade_date_probe_uses_yyyymmdd_calendar_semantics():
    frame = pd.DataFrame(
        {
            "trade_date": pd.Series(
                [20260805, 20260807], dtype="int64"
            ),
            "close": [10.0, 10.1],
        }
    )

    assert BacktestEngine._probe_daily_data_dates(frame) == ("20260807",)


def test_future_request_uses_probe_cutoff_and_one_contract_warning(
    monkeypatch,
):
    engine = _engine(["20260807", "20260810", "20260811"])
    engine._run_day = lambda day, **kwargs: _append_flat_nav(engine, day)
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
        backtest_engine_module,
        "bound_cash_trade_days",
        deterministic_bound,
    )

    result = engine.run(Strategy())

    assert captured == {
        "trade_days": ("20260807", "20260810", "20260811"),
        "requested_end": "20260811",
        "data_dates": ("20260807",),
    }
    assert result.result_contract.status is ResultStatus.SUCCESS
    assert result.result_contract.data_coverage.expected_observations == 1
    assert result.result_contract.data_coverage.actual_observations == 1
    assert len(result.daily_values) == 1
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
    engine = _engine(["20260810", "20260811"], start="20260810")
    expected_bound = WindowBound(
        trade_days=(),
        requested_end="20260811",
        cutoff="20260807",
        wall_clock_cutoff="20260807",
        data_cutoff="20260807",
        truncated=True,
    )
    monkeypatch.setattr(
        backtest_engine_module,
        "bound_cash_trade_days",
        lambda *args, **kwargs: expected_bound,
    )

    result = engine.run(Strategy())

    assert result.result_contract.status is ResultStatus.INVALID
    assert result.result_contract.reason.code == "NO_EXPECTED_OBSERVATIONS"
    assert result.daily_values.empty
    warnings = [
        warning
        for warning in result.result_contract.warnings
        if warning.code == "WINDOW_TRUNCATED"
    ]
    assert len(warnings) == 1
    assert warnings[0].message == expected_bound.warning_message
