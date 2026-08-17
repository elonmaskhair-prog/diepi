"""Cash-engine terminal contracts and unified metric surfaces."""

from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from diepi.backtest.broker import Account, Broker
from diepi.backtest.engine.backtest_engine import BacktestEngine
from diepi.backtest.engine.backtest_engine import (
    _add_adjustment_factor_identity_assumption,
)
from diepi.backtest.engine.context import Context
from diepi.backtest.engine.portfolio_context import PortfolioContext
from diepi.backtest.engine.portfolio_engine import PortfolioEngine
from diepi.backtest.data.stock_pool import PoolSource
from diepi.backtest.result_contract import ResultStatus
from diepi.backtest.comparison import ComparisonStatus
from diepi.backtest.data.contract import (
    AdjustmentFactorRunIdentity,
    Frequency,
    PriceSpace,
    validate_and_align_pair,
)
from diepi.backtest.outcome import OutcomeTracker
from diepi.backtest.strategy.base import Strategy
from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy


SYMBOL = "000001.SZ"
DAYS = ("20240102", "20240103")
AFI_KEY = "data.adjustment_factor_identity"


def _adjustment_report(symbol=SYMBOL):
    dates = ("20240102", "20240103", "20240104")
    raw_close = pd.Series([10.0, 5.0, 8.0], dtype="float64")
    ratios = pd.Series([1.0, 2.0, 1.5], dtype="float64")

    def frame(close):
        return pd.DataFrame(
            {
                "trade_date": dates,
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "pre_close": close - 0.1,
                "amount": [1.0, 2.0, 3.0],
            }
        ).set_index("trade_date")

    aligned = validate_and_align_pair(
        frame(raw_close * ratios),
        frame(raw_close),
        symbol=symbol,
        strategy_price_space=PriceSpace.HFQ,
        execution_price_space=PriceSpace.RAW,
        strategy_amount_unit="thousand_yuan",
        execution_amount_unit="thousand_yuan",
        frequency=Frequency.DAILY,
        adjustment_factors=pd.DataFrame(
            {
                "ts_code": [symbol] * 4,
                "trade_date": ["20231229", *dates],
                "adj_factor": [2.0, 2.0, 4.0, 3.0],
            }
        ),
        adjustment_factor_source="cash-engine-test.adj_factor",
        require_adjustment_factor_identity=True,
    )
    return aligned.report


def _contract_assumptions(contract):
    return {item.key: item.value for item in contract.assumptions}


class _Provider:
    price_mode = "hfq"
    execution_price_mode = "hfq"

    def __init__(self, trade_days=DAYS):
        self.trade_days = list(trade_days)
        self.frame = pd.DataFrame(
            {
                "open": [10.0] * max(1, len(self.trade_days)),
                "high": [10.2] * max(1, len(self.trade_days)),
                "low": [9.8] * max(1, len(self.trade_days)),
                "close": [10.1] * max(1, len(self.trade_days)),
                "pre_close": [10.0] * max(1, len(self.trade_days)),
                "vol": [1000.0] * max(1, len(self.trade_days)),
                "amount": [10000.0] * max(1, len(self.trade_days)),
            },
            index=pd.to_datetime(
                self.trade_days or ("20240102",)
            ),
        )

    def get_trade_days_between(self, start, end):
        return list(self.trade_days)

    def get_daily(self, *args, **kwargs):
        return self.frame.copy()

    def set_context(self, context):
        self.context = context


def _single_engine(provider):
    account = Account(
        initial_cash=100.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    broker = Broker(account, slippage=0.0)
    context = Context(broker=broker, data_provider=provider)
    context.set_symbol(SYMBOL)
    engine = BacktestEngine(
        SYMBOL,
        DAYS[0],
        DAYS[-1],
        initial_cash=100.0,
        freq="daily",
        on_missing_data="ignore",
    )
    engine._data_provider = provider
    engine._context = context
    engine._init_engine = lambda: None
    return engine


def _portfolio_engine(provider, *, stop_check=None):
    account = Account(
        initial_cash=100.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    broker = Broker(account, slippage=0.0)
    context = PortfolioContext(
        broker=broker,
        data_provider=provider,
        initial_cash=100.0,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=[SYMBOL],
    )
    engine = PortfolioEngine(
        DAYS[0],
        DAYS[-1],
        initial_cash=100.0,
        pool_source=PoolSource.SPECIFIED,
        pool_symbols=[SYMBOL],
        benchmark="",
        stop_check=stop_check,
    )
    engine._data_provider = provider
    engine._context = context
    engine._init_engine = lambda: None
    engine._preload_data = lambda pool: None
    return engine


def _append_nav(
    engine, trade_date, *, close=90.0, low=80.0, high=120.0
):
    engine._current_day_high_market_value = 0.0
    engine._current_day_high_total_value = high
    engine._current_day_low_market_value = 0.0
    engine._current_day_low_total_value = low
    engine._daily_values.append(
        {
            "date": trade_date,
            "cash": close,
            "market_value": 0.0,
            "total_value": close,
            "intraday_high_market_value": 0.0,
            "intraday_high_total_value": high,
            "intraday_low_market_value": 0.0,
            "intraday_low_total_value": low,
        }
    )


def test_single_run_success_has_exact_rankable_scope():
    engine = _single_engine(_Provider())
    # This test isolates terminal scope classification.  Keep the synthetic
    # daily NAV equal to the untouched Account/EventJournal state; fabricating
    # a different result-only NAV would now (correctly) fail cash audit.
    engine._run_day = lambda day, **kwargs: _append_nav(
        engine, day, close=100.0
    )

    result = engine.run(Strategy())

    assert result.result_contract.status is ResultStatus.SUCCESS
    assert result.result_contract.data_coverage.actual_observations == 2
    assert result.is_rankable
    assert result.result_contract is engine.last_result_contract


def test_single_legacy_provider_path_is_visible_in_result_contract():
    engine = _single_engine(_Provider())
    engine._run_day = lambda day, **kwargs: _append_nav(
        engine, day, close=100.0
    )

    result = engine.run(Strategy())

    assumptions = _contract_assumptions(result.result_contract)
    assert assumptions["data.contract_path"] == (
        "legacy_provider_compatibility"
    )
    warnings = {
        item.code: item.message for item in result.result_contract.warnings
    }
    assert "DATA_CONTRACT_COMPATIBILITY_PATH" in warnings


def test_single_missing_day_is_partial_and_not_rankable():
    engine = _single_engine(_Provider())

    def run_day(day, **kwargs):
        if day == DAYS[0]:
            _append_nav(engine, day, close=100.0)
        else:
            engine._current_day_low_total_value = None
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

    engine._run_day = run_day
    result = engine.run(Strategy())

    assert result.result_contract.status is ResultStatus.PARTIAL
    assert result.result_contract.data_coverage.actual_observations == 1
    assert result.result_contract.data_coverage.expected_observations == 2
    assert not result.is_rankable


def test_single_empty_calendar_is_invalid_not_empty_success():
    engine = _single_engine(_Provider(()))

    result = engine.run(Strategy())

    assert result.result_contract.status is ResultStatus.INVALID
    assert result.result_contract.reason.code == "NO_EXPECTED_OBSERVATIONS"
    assert not result.is_rankable


def test_single_runtime_error_is_rethrown_with_failed_contract():
    engine = _single_engine(_Provider())
    failure = LookupError("original failure")

    def run_day(day, **kwargs):
        if day == DAYS[0]:
            _append_nav(engine, day)
            return
        raise failure

    engine._run_day = run_day

    with pytest.raises(LookupError) as captured:
        engine.run(Strategy())

    assert captured.value is failure
    assert engine.last_result_contract.status is ResultStatus.FAILED
    assert engine.last_result_contract.data_coverage.actual_observations == 1
    assert engine.last_result_contract.data_coverage.expected_observations == 2


def test_single_pre_init_failure_has_explicit_empty_failed_scope():
    engine = _single_engine(_Provider())
    failure = RuntimeError("single init wiring failure")
    engine._init_engine = lambda: (_ for _ in ()).throw(failure)

    with pytest.raises(RuntimeError) as captured:
        engine.run(Strategy())

    assert captured.value is failure
    contract = engine.last_result_contract
    assert contract.status is ResultStatus.FAILED
    assert contract.data_coverage.expected_observations == 0
    assert contract.data_coverage.actual_observations == 0
    assert ("data_coverage.unit", "symbol_trade_day") in {
        (item.key, item.value) for item in contract.assumptions
    }


def test_single_metrics_include_first_day_loss_and_intraday_low():
    engine = BacktestEngine(SYMBOL, DAYS[0], DAYS[0], initial_cash=100.0)
    engine._context = SimpleNamespace(
        _broker=SimpleNamespace(
            account=SimpleNamespace(order_history=[])
        )
    )
    _append_nav(engine, DAYS[0], close=90.0, low=80.0)

    result = engine._create_result()

    assert result.total_return == pytest.approx(-0.1)
    assert result.daily_values.iloc[0].daily_return == pytest.approx(-0.1)
    assert result.max_drawdown_close_nav == pytest.approx(0.1)
    assert result.max_drawdown_intraday_low_nav == pytest.approx(0.2)
    assert result.max_drawdown_intraday_high_to_low == pytest.approx(1 / 3)
    assert result.win_rate is None
    assert result.sharpe_ratio is None


def test_portfolio_stop_is_canceled_and_not_rankable():
    engine = _portfolio_engine(_Provider(), stop_check=lambda: True)

    result = engine.run(PortfolioStrategy())

    assert result.result_contract.status is ResultStatus.CANCELED
    assert result.result_contract.reason.code == "STOP_REQUESTED"
    assert result.result_contract.data_coverage.actual_observations == 0
    assert not result.is_rankable


def test_portfolio_empty_pool_is_explicit_invalid_scope():
    engine = _portfolio_engine(_Provider())
    engine._context.get_stock_pool = lambda: []

    result = engine.run(PortfolioStrategy())

    contract = result.result_contract
    assert contract.status is ResultStatus.INVALID
    assert contract.reason.code == "EMPTY_STOCK_POOL"
    assert contract.data_coverage.expected_observations == 0
    assert contract.data_coverage.actual_observations == 0
    assert not result.is_rankable


def test_portfolio_intentionally_cash_only_days_are_complete_observations():
    engine = _portfolio_engine(_Provider())

    class CashOnly(PortfolioStrategy):
        def on_before_market_open(self, ctx):
            return []

    result = engine.run(CashOnly())

    assert result.result_contract.status is ResultStatus.SUCCESS
    assert result.result_contract.data_coverage.actual_observations == 2
    assert result.result_contract.data_coverage.expected_observations == 2
    assert result.is_rankable


def test_all_market_missing_member_is_partial_even_with_complete_nav_days():
    engine = _portfolio_engine(_Provider())
    engine.pool_source = PoolSource.ALL_MARKET

    def preload(_pool):
        engine._preload_missing = ["000002.SZ"]

    engine._preload_data = preload

    class CashOnly(PortfolioStrategy):
        def on_before_market_open(self, ctx):
            return []

    result = engine.run(CashOnly())

    contract = result.result_contract
    assert contract.status is ResultStatus.PARTIAL
    assert contract.reason.code == "UNIVERSE_MARKET_DATA_INCOMPLETE"
    assert contract.data_coverage.actual_observations == 2
    assert contract.data_coverage.expected_observations == 2
    assert "UNIVERSE_MARKET_DATA_INCOMPLETE" in {
        warning.code for warning in contract.warnings
    }
    assert not result.is_rankable


def test_portfolio_max_days_is_partial_against_full_requested_scope():
    engine = _portfolio_engine(_Provider())
    engine.max_days = 1

    class CashOnly(PortfolioStrategy):
        def on_before_market_open(self, ctx):
            return []

    result = engine.run(CashOnly())

    contract = result.result_contract
    assert contract.status is ResultStatus.PARTIAL
    assert contract.reason.code == "MAX_DAYS_TRUNCATED"
    assert contract.data_coverage.actual_observations == 1
    assert contract.data_coverage.expected_observations == 2
    assert "MAX_DAYS_TRUNCATED" in {
        warning.code for warning in contract.warnings
    }
    assert not result.is_rankable


def test_portfolio_missing_trade_day_is_partial():
    engine = _portfolio_engine(_Provider())

    def run_day(day, **kwargs):
        if day == DAYS[0]:
            _append_nav(engine, day, close=100.0)
        else:
            engine._current_day_low_total_value = None
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

    engine._run_day = run_day
    result = engine.run(PortfolioStrategy())

    assert result.result_contract.status is ResultStatus.PARTIAL
    assert result.result_contract.data_coverage.actual_observations == 1
    assert result.result_contract.data_coverage.expected_observations == 2
    assert not result.is_rankable


def test_portfolio_runtime_error_is_rethrown_with_failed_contract():
    engine = _portfolio_engine(_Provider())
    failure = RuntimeError("portfolio original failure")

    def run_day(day, **kwargs):
        if day == DAYS[0]:
            _append_nav(engine, day)
            return
        raise failure

    engine._run_day = run_day

    with pytest.raises(RuntimeError) as captured:
        engine.run(PortfolioStrategy())

    assert captured.value is failure
    assert engine.last_result_contract.status is ResultStatus.FAILED
    assert engine.last_result_contract.data_coverage.actual_observations == 1
    assert engine.last_result_contract.data_coverage.expected_observations == 2


def test_portfolio_pre_init_failure_has_explicit_empty_failed_scope():
    engine = _portfolio_engine(_Provider())
    failure = RuntimeError("portfolio init wiring failure")
    engine._init_engine = lambda: (_ for _ in ()).throw(failure)

    with pytest.raises(RuntimeError) as captured:
        engine.run(PortfolioStrategy())

    assert captured.value is failure
    contract = engine.last_result_contract
    assert contract.status is ResultStatus.FAILED
    assert contract.data_coverage.expected_observations == 0
    assert contract.data_coverage.actual_observations == 0
    assert ("data_coverage.unit", "portfolio_trade_day") in {
        (item.key, item.value) for item in contract.assumptions
    }


def test_portfolio_metrics_share_initial_nav_and_none_round_semantics():
    engine = PortfolioEngine(
        DAYS[0], DAYS[0], initial_cash=100.0, benchmark=""
    )
    engine._context = SimpleNamespace(
        _broker=SimpleNamespace(
            account=SimpleNamespace(order_history=[])
        )
    )
    _append_nav(engine, DAYS[0], close=90.0, low=80.0)

    result = engine._create_result()

    assert result.total_return == pytest.approx(-0.1)
    assert result.daily_values.iloc[0].daily_return == pytest.approx(-0.1)
    assert result.max_drawdown_close_nav == pytest.approx(0.1)
    assert result.max_drawdown_intraday_low_nav == pytest.approx(0.2)
    assert result.max_drawdown_intraday_high_to_low == pytest.approx(1 / 3)
    assert result.win_rate is None
    assert result.sharpe_ratio is None


def test_portfolio_comparison_without_source_identity_is_explicitly_unavailable(
    monkeypatch,
):
    engine = PortfolioEngine(
        DAYS[0], DAYS[0], initial_cash=100.0,
        benchmark="000300.SH", data_root="unused",
    )
    engine._context = SimpleNamespace(
        _broker=SimpleNamespace(account=SimpleNamespace(order_history=[]))
    )
    _append_nav(engine, DAYS[0], close=100.0, low=100.0, high=100.0)

    class _IndexProvider:
        def __init__(self, **kwargs):
            pass

        def get_total_return_source_identity(self, code):
            return None

    monkeypatch.setattr(
        "diepi.backtest.data.index_provider.IndexProvider", _IndexProvider
    )

    result = engine._create_result()

    reference = result.comparisons.reference_index_total_return
    assert reference.status is ComparisonStatus.UNAVAILABLE
    assert reference.reason.code == (
        "REFERENCE_TOTAL_RETURN_SOURCE_UNAVAILABLE"
    )
    assert reference.spec.source_id == (
        "unavailable:local_index_total_return"
    )
    assert result.benchmark_code == ""
    assert result.reference_total_return_excess is None


def test_portfolio_comparison_constructor_bug_is_not_disguised(
    monkeypatch,
):
    engine = PortfolioEngine(
        DAYS[0], DAYS[0], initial_cash=100.0,
        benchmark="000300.SH", data_root="unused",
    )
    engine._context = SimpleNamespace(
        _broker=SimpleNamespace(account=SimpleNamespace(order_history=[]))
    )
    _append_nav(engine, DAYS[0], close=100.0, low=100.0, high=100.0)

    class _IndexProvider:
        def __init__(self, **kwargs):
            pass

        def get_total_return_source_identity(self, code):
            raise TypeError("programming contract broken")

    monkeypatch.setattr(
        "diepi.backtest.data.index_provider.IndexProvider", _IndexProvider
    )

    with pytest.raises(TypeError, match="programming contract broken"):
        engine._create_result()


def test_portfolio_comparison_uses_provider_source_identity(monkeypatch):
    engine = PortfolioEngine(
        DAYS[0], DAYS[0], initial_cash=100.0,
        benchmark="000300.SH", data_root="unused",
    )
    engine._context = SimpleNamespace(
        _broker=SimpleNamespace(account=SimpleNamespace(order_history=[]))
    )
    _append_nav(engine, DAYS[0], close=100.0, low=100.0, high=100.0)
    captured = {}

    class _IndexProvider:
        def __init__(self, **kwargs):
            pass

        def get_total_return_source_identity(self, code):
            return "fixture:index-total-return", "sha256:" + "a" * 64

        def get_total_return_series(self, spec, scope):
            captured["spec"] = spec
            from diepi.backtest.comparison import TotalReturnIndexSeries

            return TotalReturnIndexSeries(
                spec=spec,
                scope=scope,
                base_observation_id="20240101",
                base_level=100.0,
                levels=(101.0,),
            )

    monkeypatch.setattr(
        "diepi.backtest.data.index_provider.IndexProvider", _IndexProvider
    )

    result = engine._create_result()

    assert captured["spec"].source_id == "fixture:index-total-return"
    assert captured["spec"].source_version == "sha256:" + "a" * 64
    reference = result.comparisons.reference_index_total_return
    assert reference.status is ComparisonStatus.SUCCESS
    assert result.benchmark_return == pytest.approx(0.01)


def test_single_bar_high_to_low_is_independent_of_close_metrics():
    engine = BacktestEngine(SYMBOL, DAYS[0], DAYS[0], initial_cash=100.0)
    engine._context = SimpleNamespace(
        _broker=SimpleNamespace(
            account=SimpleNamespace(order_history=[])
        )
    )
    _append_nav(engine, DAYS[0], close=90.0, low=80.0, high=120.0)
    baseline = engine._create_result()

    engine._daily_values[0]["intraday_high_total_value"] = 160.0
    changed = engine._create_result()

    assert changed.total_return == pytest.approx(baseline.total_return)
    assert changed.max_drawdown_close_nav == pytest.approx(
        baseline.max_drawdown_close_nav
    )
    assert changed.max_drawdown_intraday_low_nav == pytest.approx(
        baseline.max_drawdown_intraday_low_nav
    )
    assert baseline.max_drawdown_intraday_high_to_low == pytest.approx(1 / 3)
    assert changed.max_drawdown_intraday_high_to_low == pytest.approx(0.5)


def test_intraday_range_sampling_is_high_then_low_and_read_only():
    calls = []

    class _Account:
        cash = 0.0
        frozen_cash = 0.0

        def estimate_market_value(self, prices):
            calls.append(dict(prices))
            return float(prices[SYMBOL])

    engine = BacktestEngine(SYMBOL, DAYS[0], DAYS[0], initial_cash=100.0)
    engine._context = SimpleNamespace(
        _broker=SimpleNamespace(account=_Account())
    )

    engine._capture_intraday_range(
        {SYMBOL: 12.0},
        {SYMBOL: 8.0},
    )

    assert calls == [{SYMBOL: 12.0}, {SYMBOL: 8.0}]
    assert engine._current_day_high_total_value == pytest.approx(12.0)
    assert engine._current_day_low_total_value == pytest.approx(8.0)


def _run_with_late_adjustment_reports(
        engine_kind, reports, *, failure=None):
    if engine_kind == "single":
        engine = _single_engine(_Provider())
        strategy = Strategy()
    else:
        engine = _portfolio_engine(_Provider())
        strategy = PortfolioStrategy()

    injected = False

    def run_day(day, **kwargs):
        nonlocal injected
        if not injected:
            engine._data_quality_reports.extend(reports)
            injected = True
        if failure is not None:
            raise failure
        # AFI wiring is the subject of these tests.  Preserve an economically
        # self-consistent no-trade cash path so Result audit remains valid.
        _append_nav(engine, day, close=100.0)

    engine._run_day = run_day
    return engine, strategy


@pytest.mark.parametrize("engine_kind", ("single", "portfolio"))
def test_cash_engines_commit_late_adjustment_identity_on_success(engine_kind):
    engine, strategy = _run_with_late_adjustment_reports(
        engine_kind, [_adjustment_report()]
    )

    result = engine.run(strategy)

    assumptions = _contract_assumptions(result.result_contract)
    run_identity = AdjustmentFactorRunIdentity.from_json(assumptions[AFI_KEY])
    assert result.result_contract.status is ResultStatus.SUCCESS
    assert run_identity.identity_count == 1
    assert sum(item.key == AFI_KEY for item in result.result_contract.assumptions) == 1


@pytest.mark.parametrize("engine_kind", ("single", "portfolio"))
def test_cash_engines_commit_adjustment_identity_on_failed_terminal(engine_kind):
    failure = LookupError("primary engine failure")
    engine, strategy = _run_with_late_adjustment_reports(
        engine_kind, [_adjustment_report()], failure=failure
    )

    with pytest.raises(LookupError) as captured:
        engine.run(strategy)

    assert captured.value is failure
    assumptions = _contract_assumptions(engine.last_result_contract)
    assert engine.last_result_contract.status is ResultStatus.FAILED
    assert AdjustmentFactorRunIdentity.from_json(
        assumptions[AFI_KEY]
    ).identity_count == 1


@pytest.mark.parametrize("engine_kind", ("single", "portfolio"))
def test_cash_engines_do_not_fabricate_adjustment_identity(engine_kind):
    engine, strategy = _run_with_late_adjustment_reports(
        engine_kind, [SimpleNamespace(adjustment_factor_identity=None)]
    )

    result = engine.run(strategy)

    assert AFI_KEY not in _contract_assumptions(result.result_contract)


def _identity_contract(reports):
    tracker = OutcomeTracker(
        expected_observation_ids=(),
        coverage_unit="test_scope",
    )
    _add_adjustment_factor_identity_assumption(tracker, reports)
    return tracker.finalize_completed()


def test_adjustment_identity_commitment_is_order_independent_and_deduplicated():
    first = _adjustment_report("000001.SZ")
    second = _adjustment_report("600000.SH")

    left = _identity_contract([first, second, first])
    right = _identity_contract([second, first])

    left_value = _contract_assumptions(left)[AFI_KEY]
    right_value = _contract_assumptions(right)[AFI_KEY]
    assert left_value == right_value
    assert AdjustmentFactorRunIdentity.from_json(left_value).identity_count == 2


def test_minute_windows_with_distinct_observation_scopes_do_not_conflict():
    identity = _adjustment_report().adjustment_factor_identity
    first = replace(
        identity,
        frequency=Frequency.MINUTE,
        scope_observation_keys_sha256="a" * 64,
        validated_close_rows=5,
    )
    second = replace(
        first,
        scope_observation_keys_sha256="b" * 64,
        validated_close_rows=6,
    )

    contract = _identity_contract(
        [
            SimpleNamespace(adjustment_factor_identity=first),
            SimpleNamespace(adjustment_factor_identity=second),
        ]
    )

    value = _contract_assumptions(contract)[AFI_KEY]
    assert AdjustmentFactorRunIdentity.from_json(value).identity_count == 2


def test_same_observation_scope_with_conflicting_identity_fails_fast():
    report = _adjustment_report()
    conflict = SimpleNamespace(
        adjustment_factor_identity=replace(
            report.adjustment_factor_identity,
            source="conflicting-source.adj_factor",
        )
    )

    with pytest.raises(ValueError, match="conflicting adjustment factor identity"):
        _identity_contract([report, conflict])


@pytest.mark.parametrize("engine_kind", ("single", "portfolio"))
def test_terminal_identity_conflict_records_failed_contract(engine_kind):
    report = _adjustment_report()
    conflict = SimpleNamespace(
        adjustment_factor_identity=replace(
            report.adjustment_factor_identity,
            source="conflicting-source.adj_factor",
        )
    )
    engine, strategy = _run_with_late_adjustment_reports(
        engine_kind, [report, conflict]
    )

    with pytest.raises(
        ValueError, match="conflicting adjustment factor identity"
    ):
        engine.run(strategy)

    assert engine.last_result_contract.status is ResultStatus.FAILED
    assert AFI_KEY not in _contract_assumptions(engine.last_result_contract)


@pytest.mark.parametrize("engine_kind", ("single", "portfolio"))
def test_identity_conflict_does_not_mask_primary_engine_error(engine_kind):
    report = _adjustment_report()
    conflict = SimpleNamespace(
        adjustment_factor_identity=replace(
            report.adjustment_factor_identity,
            source="conflicting-source.adj_factor",
        )
    )
    failure = LookupError("must remain primary")
    engine, strategy = _run_with_late_adjustment_reports(
        engine_kind, [report, conflict], failure=failure
    )

    with pytest.raises(LookupError) as captured:
        engine.run(strategy)

    assert captured.value is failure
    assert engine.last_result_contract.status is ResultStatus.FAILED
    assert AFI_KEY not in _contract_assumptions(engine.last_result_contract)


@pytest.mark.parametrize("engine_kind", ("single", "portfolio"))
def test_late_failed_finalize_preserves_frozen_adjustment_assumption(engine_kind):
    report = _adjustment_report()
    tracker = OutcomeTracker(
        expected_observation_ids=(),
        coverage_unit="test_scope",
    )
    _add_adjustment_factor_identity_assumption(tracker, [report])
    previous = tracker.finalize_completed()
    if engine_kind == "single":
        engine = BacktestEngine(SYMBOL, DAYS[0], DAYS[-1])
    else:
        engine = PortfolioEngine(DAYS[0], DAYS[-1], benchmark="")
    engine._outcome_tracker = tracker
    engine._data_quality_reports = [report]
    engine.last_result_contract = previous

    failed = engine._set_failed_contract()

    assert failed.status is ResultStatus.FAILED
    assert _contract_assumptions(failed)[AFI_KEY] == (
        _contract_assumptions(previous)[AFI_KEY]
    )
