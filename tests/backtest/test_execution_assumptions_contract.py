"""Result assumptions must identify the effective cash execution model."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from diepi.backtest.broker import Account, Broker
from diepi.backtest.broker.broker import BarData
from diepi.backtest.broker.order import OrderStatus
from diepi.backtest.engine.backtest_engine import BacktestEngine
from diepi.backtest.engine.portfolio_engine import PortfolioEngine


def _broker(*, slippage=0.001):
    account = Account(
        initial_cash=100_000.0,
        commission_rate=0.00031,
        min_commission=6.0,
        stamp_duty_rate=0.0007,
    )
    return Broker(
        account,
        slippage=slippage,
        liquidity_cap_ratio=0.37,
        limit_pct_overrides={"600000.SH": 0.12},
        open_buy_fill_mode="open+slip",
        open_buy_sizing="limit_up",
        bar_liquidity_model="any_touch",
        t0_overrides={"511"},
        limit_check_exempt={"600000.SH": {"20240102"}},
        transfer_fee_rate=0.00001,
    )


def _assumptions(engine, broker):
    engine._context = SimpleNamespace(_broker=broker)
    contract = engine._new_outcome_tracker(()).finalize_completed()
    return {item.key: item.value for item in contract.assumptions}


def _single():
    return BacktestEngine(
        symbol="600000.SH",
        start_date="20240102",
        end_date="20240103",
        freq="daily",
    )


def _portfolio():
    return PortfolioEngine(
        start_date="20240102",
        end_date="20240103",
        freq="daily",
    )


def test_both_cash_engines_publish_the_same_effective_execution_contract():
    single = _assumptions(_single(), _broker())
    portfolio = _assumptions(_portfolio(), _broker())

    execution_single = {
        key: value for key, value in single.items()
        if key.startswith("execution.")
    }
    execution_portfolio = {
        key: value for key, value in portfolio.items()
        if key.startswith("execution.")
    }
    assert execution_single == execution_portfolio
    assert execution_single["execution.market_order_model"] == "worst_bar_v1"
    assert execution_single["execution.continuous_market_price"] == (
        "buy_high_plus_slippage_sell_low_minus_slippage"
    )
    assert execution_single["execution.limit_order_model"] == (
        "touch_at_limit_or_better_open_no_queue_shared_bar_liquidity_cap"
    )
    assert execution_single["execution.stop_order_model"] == (
        "trigger_or_gap_open_with_directional_slippage_shared_bar_liquidity_cap"
    )
    assert execution_single["execution.opening_auction_sell_price"] == "open"
    assert execution_single["execution.opening_auction_buy_price"] == (
        "open_plus_slippage"
    )
    assert execution_single["execution.open_buy_resize_mode"] == "auto"
    assert execution_single["execution.closing_auction_price"] == (
        "close_with_directional_slippage"
    )
    assert execution_single["execution.daily_close_phase_model"] == (
        "bar_close_window_approximation_not_exchange_session_reconstruction"
    )
    assert execution_single["execution.modeled_fill_price_semantics"] == (
        "effective_price_may_not_be_legal_tick_trade_print"
    )
    assert execution_single["execution.price_band_fill_policy"] == (
        "adverse_direction_saturation_opposite_direction_fail_fast"
    )
    assert execution_single["execution.raw_bar_price_band_validation"] == (
        "fail_fast_before_execution_or_strategy_callback"
    )
    assert execution_single["execution.corporate_action_band_reference"] == (
        "source_pre_close_else_pre_close_div_audited_factor_change"
    )
    assert "execution.auction_price" not in execution_single
    assert execution_single["execution.slippage_rate"] == "0.001"
    assert execution_single["execution.bar_liquidity_model"] == "any_touch"
    assert execution_single["execution.liquidity_cap_ratio"] == "0.37"
    assert execution_single["execution.commission_scope"] == "parent_order"
    assert execution_single["execution.commission_rate"] == "0.00031"
    assert execution_single["execution.min_commission"] == "6.0"
    assert execution_single["execution.stamp_duty_policy"] == "0.0007"
    assert execution_single["execution.transfer_fee_rate"] == "1e-05"
    assert execution_single["execution.limit_pct_overrides"] == (
        '{"600000.SH":0.12}'
    )
    assert execution_single["execution.t0_overrides"] == '["511"]'
    assert len(
        execution_single["execution.limit_exempt_snapshot_sha256"]
    ) == 64
    assert single["corporate_action.adjustment_factor_model"] == (
        "immediate_total_return_reinvestment_with_fractional_cash_in_lieu"
    )
    assert single["corporate_action.adjustment_factor_materiality"] == "1e-05"
    assert single["corporate_action.dividend_tax_model"] == "none"
    assert portfolio["corporate_action.adjustment_factor_model"] == (
        single["corporate_action.adjustment_factor_model"]
    )
    assert portfolio["corporate_action.adjustment_factor_materiality"] == (
        single["corporate_action.adjustment_factor_materiality"]
    )
    assert portfolio["corporate_action.dividend_tax_model"] == (
        single["corporate_action.dividend_tax_model"]
    )


def test_effective_config_change_changes_result_assumption():
    first = _assumptions(_single(), _broker(slippage=0.001))
    second = _assumptions(_single(), _broker(slippage=0.002))

    assert first["execution.slippage_rate"] == "0.001"
    assert second["execution.slippage_rate"] == "0.002"
    assert first != second


def test_raw_same_price_space_discloses_disabled_factor_overlay():
    engine = BacktestEngine(
        symbol="600000.SH",
        start_date="20240102",
        end_date="20240103",
        freq="daily",
        strategy_price_mode="raw",
        execution_price_mode="raw",
    )
    assumptions = _assumptions(engine, _broker())

    assert assumptions["execution.strategy_price_mode"] == "raw"
    assert assumptions["execution.execution_price_mode"] == "raw"
    assert assumptions["corporate_action.adjustment_factor_model"] == (
        "disabled_same_price_space"
    )
    assert assumptions["corporate_action.adjustment_factor_materiality"] == (
        "not_applicable"
    )
    assert assumptions["corporate_action.dividend_tax_model"] == "not_applicable"
    assert assumptions["execution.corporate_action_band_reference"] == (
        "source_pre_close_only_no_factor_overlay"
    )


def test_open_buy_compatibility_mode_is_disclosed_dynamically():
    broker = _broker()
    broker.open_buy_fill_mode = "open"

    assumptions = _assumptions(_single(), broker)

    assert assumptions["execution.opening_auction_sell_price"] == "open"
    assert assumptions["execution.opening_auction_buy_price"] == "open"
    assert assumptions["execution.closing_auction_price"] == (
        "close_with_directional_slippage"
    )


@pytest.mark.parametrize("fill_mode", ["open+slip", "open"])
def test_legacy_open_buy_path_discloses_its_effective_raw_fill_price(
        fill_mode):
    broker = _broker()
    broker.open_buy_resize_mode = "legacy"
    broker.open_buy_fill_mode = fill_mode

    assumptions = _assumptions(_single(), broker)

    assert assumptions["execution.open_buy_resize_mode"] == "legacy"
    assert assumptions["execution.open_buy_fill_mode"] == fill_mode
    assert assumptions["execution.opening_auction_buy_price"] == "open"


def test_engine_transfer_fee_parameter_affects_fill_and_assumption():
    engine = BacktestEngine(
        symbol="600000.SH",
        start_date="20240102",
        end_date="20240102",
        freq="daily",
        initial_cash=100_000.0,
        slippage=0.0,
        commission_rate=0.0,
        stamp_duty=0.0,
        min_commission=0.0,
        transfer_fee_rate=0.00002,
    )
    engine._init_engine()
    broker = engine._context._broker
    broker.set_date("20240102")
    broker.set_market_phase("trading")
    order = broker.buy_at_market(
        "600000.SH", shares=100, current_price=10.0)
    broker.execute_market_orders(BarData(
        symbol="600000.SH",
        trade_time=datetime(2024, 1, 2, 15, 0),
        open=10.0,
        high=10.0,
        low=10.0,
        close=10.0,
        vol=1_000_000.0,
        amount=1_000_000.0,
        pre_close=10.0,
    ))

    assumptions = _assumptions(engine, broker)
    assert order.status is OrderStatus.FILLED
    assert order.transfer_fee_paid == pytest.approx(0.02)
    assert assumptions["execution.transfer_fee_rate"] == "2e-05"
