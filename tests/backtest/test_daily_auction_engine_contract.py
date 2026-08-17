# -*- coding: utf-8 -*-
"""Engine-level contracts for causal daily-auction liquidity.

These tests deliberately exercise both daily event loops with in-memory,
dual-lane data.  Daily source ``amount`` is expressed in thousand yuan; the
auction policy always resolves to a final executable-notional cap in yuan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import pandas as pd
import pytest

from diepi.backtest.broker.account import Account
from diepi.backtest.broker.broker import Broker
from diepi.backtest.broker.events import ExecutionEventType
from diepi.backtest.data.stock_pool import PoolSource
from diepi.backtest.engine.backtest_engine import BacktestEngine
from diepi.backtest.engine.context import Context
from diepi.backtest.engine.portfolio_context import PortfolioContext
from diepi.backtest.engine.portfolio_engine import PortfolioEngine
from diepi.backtest.liquidity import (
    AuctionCapSpec,
    AuctionLiquidityUnavailable,
    DailyAuctionLiquidityPolicy,
)
from diepi.backtest.strategy.base import Strategy
from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy


PRIOR_DAY = "20240101"
TRADE_DAY = "20240102"
SYMBOL = "000001.SZ"


def _daily_lane(
    *,
    prior_amount: float,
    current_amount: float,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "open": [10.0, 10.0],
            "high": [10.0, 10.0],
            "low": [10.0, 10.0],
            "close": [10.0, 10.0],
            "pre_close": [10.0, 10.0],
            "vol": [1_000_000.0, 1_000_000.0],
            "amount": [prior_amount, current_amount],
        },
        index=pd.to_datetime([PRIOR_DAY, TRADE_DAY]),
    )


class _DualLaneProvider:
    """Minimal provider with independently observable strategy/execution lanes."""

    price_mode = "hfq"
    execution_price_mode = "hfq"

    def __init__(
        self,
        execution: Dict[str, pd.DataFrame],
        strategy: Optional[Dict[str, pd.DataFrame]] = None,
    ) -> None:
        self._execution = {
            symbol: frame.copy() for symbol, frame in execution.items()
        }
        source = execution if strategy is None else strategy
        self._strategy = {
            symbol: frame.copy() for symbol, frame in source.items()
        }
        self.context = None

    @property
    def _trade_days(self):
        return sorted(
            {
                timestamp.strftime("%Y%m%d")
                for frame in self._execution.values()
                for timestamp in pd.DatetimeIndex(frame.index)
            }
        )

    def set_context(self, context) -> None:
        self.context = context

    def set_price_modes(self, strategy=None, execution=None) -> None:
        if strategy is not None:
            self.price_mode = strategy
        if execution is not None:
            self.execution_price_mode = execution

    def get_trade_days_between(self, start: str, end: str):
        return [day for day in self._trade_days if start <= day <= end]

    def get_prev_trade_day(self, date: str, n: int = 1):
        prior = [day for day in self._trade_days if day < date]
        return prior[-n] if len(prior) >= n else None

    def get_next_trade_day(self, date: str, n: int = 1):
        later = [day for day in self._trade_days if day > date]
        return later[n - 1] if len(later) >= n else None

    def get_adj_ratio(self, symbol: str, date: str) -> float:
        return 1.0

    def get_stock_info(self, symbol=None):
        return pd.DataFrame()

    def get_daily(
        self,
        symbol: str,
        start: str = None,
        end: str = None,
        fields=None,
        price_mode: str = None,
        **kwargs,
    ) -> pd.DataFrame:
        source = (
            self._execution
            if price_mode == "execution"
            else self._strategy
        )
        frame = source.get(symbol)
        if frame is None:
            return pd.DataFrame()
        result = frame
        if start is not None:
            result = result[result.index >= pd.to_datetime(start)]
        if end is not None:
            result = result[result.index <= pd.to_datetime(end)]
        if fields is not None:
            result = result[list(fields)]
        return result.copy()


def _new_account() -> Account:
    return Account(
        initial_cash=1_000_000.0,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )


def _new_broker(account: Account, liquidity_cap_ratio: float) -> Broker:
    return Broker(
        account=account,
        slippage=0.0,
        liquidity_cap_ratio=liquidity_cap_ratio,
    )


@dataclass
class _Harness:
    engine: object
    account: Account
    broker: Broker


def _wire_engine(
    engine_kind: str,
    provider: _DualLaneProvider,
    strategy,
    *,
    policy: Optional[DailyAuctionLiquidityPolicy],
    liquidity_cap_ratio: float = 0.8,
) -> _Harness:
    account = _new_account()
    broker = _new_broker(account, liquidity_cap_ratio)

    if engine_kind == "single":
        context = Context(broker=broker, data_provider=provider)
        context.set_symbol(SYMBOL)
        engine = BacktestEngine(
            symbol=SYMBOL,
            start_date=TRADE_DAY,
            end_date=TRADE_DAY,
            initial_cash=account.initial_cash,
            freq="daily",
            slippage=0.0,
            commission_rate=0.0,
            stamp_duty=0.0,
            min_commission=0.0,
            liquidity_cap_ratio=liquidity_cap_ratio,
            daily_auction_liquidity=policy,
        )
        engine._data_provider = provider
        engine._context = context
        engine._strategy = strategy
        engine._daily_values = []
    else:
        context = PortfolioContext(
            broker=broker,
            data_provider=provider,
            initial_cash=account.initial_cash,
            pool_source=PoolSource.SPECIFIED,
            pool_symbols=[SYMBOL],
        )
        engine = PortfolioEngine(
            start_date=TRADE_DAY,
            end_date=TRADE_DAY,
            initial_cash=account.initial_cash,
            freq="daily",
            pool_source=PoolSource.SPECIFIED,
            pool_symbols=[SYMBOL],
            benchmark="",
            slippage=0.0,
            commission_rate=0.0,
            stamp_duty=0.0,
            min_commission=0.0,
            liquidity_cap_ratio=liquidity_cap_ratio,
            daily_auction_liquidity=policy,
        )
        strategy_frame = provider.get_daily(SYMBOL, price_mode="strategy")
        execution_frame = provider.get_daily(SYMBOL, price_mode="execution")
        engine._data_provider = provider
        engine._context = context
        engine._strategy = strategy
        engine._daily_values = []
        engine._position_history = []
        engine._daily_data_cache = {SYMBOL: strategy_frame}
        engine._daily_data_raw_cache = {SYMBOL: execution_frame}
        engine._date_index_cache = {
            SYMBOL: engine._build_date_index(strategy_frame)
        }
        engine._date_index_raw_cache = {
            SYMBOL: engine._build_date_index(execution_frame)
        }

    strategy.set_context(context)
    strategy.on_init(context)
    return _Harness(engine=engine, account=account, broker=broker)


def _provider(
    *,
    prior_execution_amount: float = 100.0,
    current_execution_amount: float = 100.0,
    prior_strategy_amount: float = 999_999.0,
    current_strategy_amount: float = 999_999.0,
) -> _DualLaneProvider:
    return _DualLaneProvider(
        execution={
            SYMBOL: _daily_lane(
                prior_amount=prior_execution_amount,
                current_amount=current_execution_amount,
            )
        },
        strategy={
            SYMBOL: _daily_lane(
                prior_amount=prior_strategy_amount,
                current_amount=current_strategy_amount,
            )
        },
    )


def _account_snapshot(account: Account, broker: Broker, order) -> tuple:
    positions = tuple(
        sorted(
            (
                symbol,
                position.shares,
                position.frozen_shares,
                position.cost_basis,
                position.current_price,
            )
            for symbol, position in account.positions.items()
        )
    )
    return (
        account.cash,
        account.frozen_cash,
        positions,
        tuple(account.orders),
        tuple(item.order_id for item in account.order_history),
        order.status,
        order.shares,
        order.filled_shares,
        order.filled_amount,
        order.avg_price,
        order.frozen_amount,
        order.frozen_shares,
        tuple(item.order_id for item in broker._pending_open_orders),
        tuple(item.order_id for item in broker._pending_close_orders),
        tuple(sorted(broker._bar_used_amount.items())),
    )


class _SingleAuctionBuy(Strategy):
    def __init__(self, window: str, shares: int = 10_000) -> None:
        super().__init__()
        self.window = window
        self.shares = shares
        self.order = None
        self.before_match = None

    def on_before_market_open(self, ctx):
        if self.window == "open":
            self.order = ctx.buy_at_open(shares=self.shares)
            self.before_match = _account_snapshot(
                ctx._broker.account, ctx._broker, self.order
            )

    def on_after_open(self, ctx, bar):
        if self.window == "close":
            self.order = ctx.schedule_at_close(
                "buy",
                shares=self.shares,
                current_price=float(bar.open),
            )
            self.before_match = _account_snapshot(
                ctx._broker.account, ctx._broker, self.order
            )


class _PortfolioAuctionBuy(PortfolioStrategy):
    def __init__(self, window: str, shares: int = 10_000) -> None:
        super().__init__()
        self.window = window
        self.shares = shares
        self.order = None
        self.before_match = None

    def on_before_market_open(self, ctx):
        if self.window == "open":
            self.order = ctx.buy_at_open(SYMBOL, shares=self.shares)
            self.before_match = _account_snapshot(
                ctx._broker.account, ctx._broker, self.order
            )
        return [SYMBOL]

    def on_after_open(self, ctx, bars):
        if self.window == "close":
            self.order = ctx.schedule_at_close(
                "buy",
                SYMBOL,
                shares=self.shares,
                current_price=float(bars.get(SYMBOL).open),
            )
            self.before_match = _account_snapshot(
                ctx._broker.account, ctx._broker, self.order
            )


class _NoAuctionSingle(Strategy):
    pass


class _NoAuctionPortfolio(PortfolioStrategy):
    def on_before_market_open(self, ctx):
        return [SYMBOL]


def _strategy(engine_kind: str, window: str):
    if engine_kind == "single":
        return _SingleAuctionBuy(window)
    return _PortfolioAuctionBuy(window)


def _created_attributes(broker: Broker, order) -> dict:
    created = [
        event for event in broker.event_journal.snapshot()
        if event.event_type is ExecutionEventType.ORDER_CREATED
        and event.payload.order_id == order.order_id
    ]
    assert len(created) == 1
    return dict(created[0].payload.attributes.entries)


@pytest.mark.parametrize("engine_kind", ["single", "portfolio"])
def test_close_order_event_records_same_day_window_at_creation(engine_kind):
    strategy = _strategy(engine_kind, "close")
    harness = _wire_engine(
        engine_kind,
        _provider(),
        strategy,
        policy=DailyAuctionLiquidityPolicy(
            close_cap=AuctionCapSpec.fixed_yuan(1_000_000.0),
        ),
    )

    harness.engine._run_day(TRADE_DAY)

    attrs = _created_attributes(harness.broker, strategy.order)
    assert attrs["eligible_from"] == "2024-01-02T15:00:00.000000"
    assert attrs["expire_date"] == TRADE_DAY


def _run_filled_shares(
    engine_kind: str,
    window: str,
    *,
    policy: DailyAuctionLiquidityPolicy,
    prior_execution_amount: float,
    current_execution_amount: float,
    prior_strategy_amount: float = 999_999.0,
    liquidity_cap_ratio: float = 0.8,
) -> int:
    provider = _provider(
        prior_execution_amount=prior_execution_amount,
        current_execution_amount=current_execution_amount,
        prior_strategy_amount=prior_strategy_amount,
    )
    strategy = _strategy(engine_kind, window)
    harness = _wire_engine(
        engine_kind,
        provider,
        strategy,
        policy=policy,
        liquidity_cap_ratio=liquidity_cap_ratio,
    )

    harness.engine._run_day(TRADE_DAY)

    return strategy.order.filled_shares


@pytest.mark.parametrize("engine_kind", ["single", "portfolio"])
@pytest.mark.parametrize("window", ["open", "close"])
def test_missing_cap_fails_only_when_relevant_auction_order_reaches_matcher_and_is_atomic(
    engine_kind: str,
    window: str,
):
    strategy = _strategy(engine_kind, window)
    harness = _wire_engine(
        engine_kind,
        _provider(),
        strategy,
        policy=None,
    )

    with pytest.raises(
        AuctionLiquidityUnavailable,
        match=rf"{window} requires an explicit liquidity cap",
    ) as exc_info:
        harness.engine._run_day(TRADE_DAY)

    message = str(exc_info.value)
    assert f"--daily-{window}-cap-yuan" in message
    assert f"--daily-{window}-previous-day-ratio" in message

    assert strategy.order is not None
    assert _account_snapshot(
        harness.account, harness.broker, strategy.order
    ) == strategy.before_match


@pytest.mark.parametrize("engine_kind", ["single", "portfolio"])
def test_missing_policy_is_not_resolved_when_no_auction_order_exists(
    engine_kind: str,
):
    strategy = (
        _NoAuctionSingle()
        if engine_kind == "single"
        else _NoAuctionPortfolio()
    )
    harness = _wire_engine(
        engine_kind,
        _provider(),
        strategy,
        policy=None,
    )

    harness.engine._run_day(TRADE_DAY)

    assert harness.account.cash == pytest.approx(
        harness.account.initial_cash
    )
    assert harness.account.frozen_cash == pytest.approx(0.0)
    assert harness.account.positions == {}
    assert harness.account.orders == {}
    assert harness.account.order_history == []


@pytest.mark.parametrize("engine_kind", ["single", "portfolio"])
@pytest.mark.parametrize("window", ["open", "close"])
def test_fixed_yuan_auction_fill_is_invariant_to_current_day_turnover(
    engine_kind: str,
    window: str,
):
    cap = AuctionCapSpec.fixed_yuan(3_000.0)
    policy = DailyAuctionLiquidityPolicy(
        open_cap=cap if window == "open" else None,
        close_cap=cap if window == "close" else None,
    )

    low_current_fill = _run_filled_shares(
        engine_kind,
        window,
        policy=policy,
        prior_execution_amount=100.0,
        current_execution_amount=0.001,
    )
    high_current_fill = _run_filled_shares(
        engine_kind,
        window,
        policy=policy,
        prior_execution_amount=100.0,
        current_execution_amount=1_000_000_000.0,
    )

    assert low_current_fill == high_current_fill == 300


@pytest.mark.parametrize("engine_kind", ["single", "portfolio"])
@pytest.mark.parametrize("window", ["open", "close"])
def test_previous_day_ratio_uses_only_prior_execution_lane_turnover(
    engine_kind: str,
    window: str,
):
    cap = AuctionCapSpec.previous_day_ratio(0.10)
    policy = DailyAuctionLiquidityPolicy(
        open_cap=cap if window == "open" else None,
        close_cap=cap if window == "close" else None,
    )

    baseline = _run_filled_shares(
        engine_kind,
        window,
        policy=policy,
        prior_execution_amount=10.0,
        current_execution_amount=1.0,
        prior_strategy_amount=999_999.0,
    )
    changed_current = _run_filled_shares(
        engine_kind,
        window,
        policy=policy,
        prior_execution_amount=10.0,
        current_execution_amount=1_000_000.0,
        prior_strategy_amount=999_999.0,
    )
    changed_prior_execution = _run_filled_shares(
        engine_kind,
        window,
        policy=policy,
        prior_execution_amount=20.0,
        current_execution_amount=1.0,
        prior_strategy_amount=999_999.0,
    )

    # Source amount is thousand yuan.  10 * 1000 * 10% / 10 yuan = 100 shares.
    assert baseline == changed_current == 100
    assert changed_prior_execution == 200


@pytest.mark.parametrize("engine_kind", ["single", "portfolio"])
def test_open_and_close_have_independent_final_caps_not_broker_scaled(
    engine_kind: str,
):
    policy = DailyAuctionLiquidityPolicy(
        open_cap=AuctionCapSpec.fixed_yuan(5_000.0),
        close_cap=AuctionCapSpec.fixed_yuan(2_000.0),
    )
    strategy = _strategy(engine_kind, "open")

    class _SingleBoth(_SingleAuctionBuy):
        def __init__(self):
            super().__init__("open")
            self.close_order = None

        def on_after_open(self, ctx, bar):
            self.close_order = ctx.schedule_at_close(
                "buy", shares=10_000, current_price=float(bar.open)
            )

    class _PortfolioBoth(_PortfolioAuctionBuy):
        def __init__(self):
            super().__init__("open")
            self.close_order = None

        def on_after_open(self, ctx, bars):
            self.close_order = ctx.schedule_at_close(
                "buy",
                SYMBOL,
                shares=10_000,
                current_price=float(bars.get(SYMBOL).open),
            )

    strategy = _SingleBoth() if engine_kind == "single" else _PortfolioBoth()
    harness = _wire_engine(
        engine_kind,
        _provider(current_execution_amount=1_000_000_000.0),
        strategy,
        policy=policy,
        # If the broker ratio were applied again, these fills would round to
        # zero lots (50/20 yuan of effective cap instead of 5,000/2,000).
        liquidity_cap_ratio=0.01,
    )

    harness.engine._run_day(TRADE_DAY)

    assert strategy.order.filled_shares == 500
    assert strategy.close_order.filled_shares == 200
