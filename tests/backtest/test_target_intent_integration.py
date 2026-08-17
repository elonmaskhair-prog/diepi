"""End-to-end contracts for delayed close target intents."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from diepi.backtest.broker import (
    Account,
    Broker,
    TargetAchievementStatus,
    TargetIntent,
    TargetPlanSide,
)
from diepi.backtest.broker.broker import BarData
from diepi.backtest.broker.settlement import (
    SettlementInvariantError,
)
from diepi.backtest.engine.portfolio_context import PortfolioContext


TRADE_DATE = "20240103"
DECISION = datetime(2024, 1, 3, 14, 55)
CLOSE = datetime(2024, 1, 3, 15, 0)


def _setup(cash=1_000_000.0, *, slippage=0.0, t0_overrides=None,
           allow_odd_lot_sell=True):
    account = Account(
        initial_cash=cash,
        commission_rate=0.0,
        stamp_duty_rate=0.0,
        min_commission=0.0,
    )
    broker = Broker(
        account=account,
        slippage=slippage,
        t0_overrides=t0_overrides,
        allow_odd_lot_sell=allow_odd_lot_sell,
    )
    broker.set_date(TRADE_DATE)
    broker.set_market_phase("trading")
    return PortfolioContext(broker=broker), broker, account


def _bar(symbol, close=10.0, *, high=None, low=None, pre_close=10.0,
         cap=1_000_000_000.0):
    return BarData(
        symbol=symbol,
        trade_time=CLOSE,
        open=close,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        amount=1_000_000_000.0,
        pre_close=pre_close,
        liquidity_cap_amount=cap,
        liquidity_window="close",
    )


def _submit(ctx, broker, targets, *, decision=DECISION,
            phase="on_before_close", rebalance=False):
    with broker.order_creation_scope(phase, decision):
        with broker.target_intent_scope(CLOSE):
            if rebalance:
                return ctx.rebalance(targets, when="close")
            symbol, weight = next(iter(targets.items()))
            return ctx.order_target_percent(symbol, weight, when="close")


def _hold(account, symbol, shares, price, *, date="20240102", settle=True):
    assert account.freeze_cash(shares * price)
    account.buy(symbol, shares, price, date, fee_total=0.0)
    if settle:
        account.settle_t1(TRADE_DATE)
    account.positions[symbol].update_price(price)


def _economic_snapshot(account, broker):
    return (
        account.cash,
        account.frozen_cash,
        tuple(sorted(
            (symbol, position.shares, position.available_shares,
             position.frozen_shares, position.current_price)
            for symbol, position in account.positions.items()
        )),
        tuple(account.orders),
        tuple(order.order_id for order in account.order_history),
        tuple(order.order_id for order in broker._pending_close_orders),
        tuple(sorted(broker._bar_used_amount.items())),
        broker.event_journal.snapshot(),
        broker._order_id_counter,
        broker._event_id_counter,
        broker._fill_id_counter,
        broker._settlement_id_counter,
        broker._corporate_action_id_counter,
        broker._target_intent_id_counter,
        broker._target_batch_id_counter,
        broker.target_achievements_snapshot(),
        tuple(sorted(broker._target_order_intents.items())),
    )


def test_close_decision_is_unsized_and_ohlc_metamorphic():
    json_decisions = []
    for high, low, close in ((10.0, 10.0, 10.0), (99.0, 1.0, 17.0)):
        ctx, broker, account = _setup()
        before = _economic_snapshot(account, broker)
        intent = _submit(
            ctx, broker, {"000001.SZ": 0.5}, phase="on_after_open",
            decision=datetime(2024, 1, 3, 9, 31),
        )
        assert isinstance(intent, TargetIntent)
        assert _economic_snapshot(account, broker)[:11] == before[:11]
        assert account.orders == {}
        assert not any(
            name in intent.to_dict()
            for name in ("close", "nav", "notional", "shares", "side")
        )
        json_decisions.append(intent.to_json())
        # The future bar is intentionally constructed only after the decision.
        _bar("000001.SZ", close, high=high, low=low, pre_close=10.0)
    assert json_decisions[0] == json_decisions[1]


def test_rebalance_shares_batch_id_but_all_calls_share_global_cash_scale():
    ctx, broker, account = _setup()
    first = _submit(ctx, broker, {"000001.SZ": 0.5})
    second = _submit(
        ctx, broker, {"600000.SH": 0.5},
        decision=DECISION + timedelta(seconds=1),
    )
    assert first.batch_id != second.batch_id

    broker.execute_close_orders(
        {
            "000001.SZ": _bar("000001.SZ"),
            "600000.SH": _bar("600000.SH"),
        },
        match_at=CLOSE,
    )

    results = {item.symbol: item for item in broker.target_achievements}
    assert results["000001.SZ"].planned_shares == results["600000.SH"].planned_shares
    assert all("cash_scaled" in item.reasons for item in results.values())

    ctx2, broker2, _ = _setup()
    intents = _submit(
        ctx2,
        broker2,
        {"600000.SH": 0.4, "000001.SZ": 0.4},
        rebalance=True,
    )
    assert len({intent.batch_id for intent in intents}) == 1
    assert [intent.symbol for intent in intents] == ["000001.SZ", "600000.SH"]


def test_unmentioned_retained_holding_makes_whole_set_infeasible():
    ctx, broker, account = _setup()
    _hold(account, "000001.SZ", 50_000, 10.0)
    intent = _submit(ctx, broker, {"600000.SH": 0.6})
    before = _economic_snapshot(account, broker)

    assert broker.execute_close_orders(
        {"600000.SH": _bar("600000.SH")}, match_at=CLOSE) == []

    assert _economic_snapshot(account, broker)[:7] == before[:7]
    achievement = broker.target_achievements[0]
    assert achievement.intent_id == intent.intent_id
    assert achievement.status is TargetAchievementStatus.INFEASIBLE
    assert "account_weight_cap" in achievement.reasons
    assert "stale_reference_mark" in achievement.reasons


def test_missing_target_close_bar_is_blocked_without_aborting_other_symbols():
    ctx, broker, account = _setup()
    intents = _submit(
        ctx,
        broker,
        {"000001.SZ": 0.4, "600000.SH": 0.4},
        rebalance=True,
    )

    executed = broker.execute_close_orders(
        {"000001.SZ": _bar("000001.SZ")}, match_at=CLOSE)

    assert len(executed) == 1
    by_symbol = {item.symbol: item for item in broker.target_achievements}
    assert by_symbol["000001.SZ"].status is TargetAchievementStatus.ACHIEVED
    missing = by_symbol["600000.SH"]
    assert missing.intent_id == next(
        item.intent_id for item in intents if item.symbol == "600000.SH")
    assert missing.status is TargetAchievementStatus.BLOCKED
    assert missing.planned_side is TargetPlanSide.NONE
    assert missing.planned_shares == 0
    assert missing.reference_price == 0.0
    assert missing.reasons == ("no_close_bar",)
    assert account.positions["000001.SZ"].shares == 40_000


def test_missing_held_target_uses_stale_mark_and_reports_blocked():
    ctx, broker, account = _setup()
    _hold(account, "600000.SH", 20_000, 10.0)
    _submit(
        ctx,
        broker,
        {"000001.SZ": 0.4, "600000.SH": 0.1},
        rebalance=True,
    )

    broker.execute_close_orders(
        {"000001.SZ": _bar("000001.SZ")}, match_at=CLOSE)

    missing = next(
        item for item in broker.target_achievements
        if item.symbol == "600000.SH"
    )
    assert missing.status is TargetAchievementStatus.BLOCKED
    assert missing.reference_price == pytest.approx(10.0)
    assert "no_close_bar" in missing.reasons
    assert "stale_reference_mark" in missing.reasons
    assert account.positions["600000.SH"].shares == 20_000


def test_retained_holding_without_market_mark_fails_atomically():
    ctx, broker, account = _setup()
    assert account.freeze_cash(500_000.0)
    account.buy("000001.SZ", 50_000, 10.0, "20240102", fee_total=0.0)
    account.settle_t1(TRADE_DATE)
    # Explicitly model an imported/legacy holding whose mark is unavailable.
    # Normal fills now mark immediately at their execution price (D-6).
    account.positions["000001.SZ"].current_price = 0.0
    assert account.positions["000001.SZ"].current_price == 0.0
    _submit(ctx, broker, {"600000.SH": 0.4})
    before = _economic_snapshot(account, broker)

    with pytest.raises(SettlementInvariantError, match="no close reference"):
        broker.execute_close_orders(
            {"600000.SH": _bar("600000.SH")}, match_at=CLOSE)

    assert _economic_snapshot(account, broker) == before


def test_sells_execute_first_and_fund_buys_with_upward_sell_rounding():
    ctx, broker, account = _setup()
    _hold(account, "000001.SZ", 90_000, 10.0)
    cash_before = account.available_cash
    _submit(
        ctx,
        broker,
        {"000001.SZ": 0.2, "600000.SH": 0.7},
        rebalance=True,
    )
    broker.execute_close_orders(
        {
            "000001.SZ": _bar("000001.SZ"),
            "600000.SH": _bar("600000.SH"),
        },
        match_at=CLOSE,
    )

    target_orders = [
        order for order in account.order_history
        if order.order_id in broker._target_order_intents
    ]
    assert [order.side.value for order in target_orders] == ["sell", "buy"]
    buy = target_orders[1]
    assert buy.shares * buy.sizing_price > cash_before
    sell_result = next(
        item for item in broker.target_achievements
        if item.symbol == "000001.SZ"
    )
    assert sell_result.cap_respected
    assert sell_result.achieved_weight <= sell_result.target_weight + 1e-12


def test_t1_and_minimum_lot_keep_direction_when_plan_is_zero():
    ctx, broker, account = _setup()
    assert account.freeze_cash(500_000.0)
    account.buy("000001.SZ", 50_000, 10.0, TRADE_DATE, fee_total=0.0)
    account.positions["000001.SZ"].update_price(10.0)
    _submit(ctx, broker, {"000001.SZ": 0.0})
    broker.execute_close_orders(
        {"000001.SZ": _bar("000001.SZ")}, match_at=CLOSE)
    locked = broker.target_achievements[0]
    assert locked.planned_side is TargetPlanSide.SELL
    assert locked.planned_shares == 0
    assert locked.status is TargetAchievementStatus.OVER_TARGET
    assert "t1_unavailable" in locked.reasons

    ctx2, broker2, _ = _setup()
    _submit(ctx2, broker2, {"600000.SH": 0.00001})
    broker2.execute_close_orders(
        {"600000.SH": _bar("600000.SH")}, match_at=CLOSE)
    tiny = broker2.target_achievements[0]
    assert tiny.planned_side is TargetPlanSide.BUY
    assert tiny.planned_shares == 0
    assert tiny.status is TargetAchievementStatus.UNDER_TARGET
    assert "minimum_lot" in tiny.reasons


def test_target_cash_scaling_uses_exact_freeze_cash_boundary(monkeypatch):
    _, broker, _ = _setup(cash=100.0)
    symbol = "920002.BJ"
    monkeypatch.setattr(
        broker, "_target_worst_buy_price", lambda _symbol, _bar: 1.0)
    monkeypatch.setattr(
        broker,
        "_target_buy_reserve_cost",
        lambda _symbol, shares, _price: (
            0.0 if shares == 0 else shares + 5e-10
        ),
    )

    allocation = broker._target_scaled_buy_shares(
        [{
            "intent": SimpleNamespace(symbol=symbol),
            "desired_shares": 100,
        }],
        {symbol: object()},
    )

    # The old +1e-9 planner tolerance returned 100 here even though the
    # downstream exact freeze_cash check could not reserve that order.
    assert allocation[symbol] == (0, 1.0, True)


def test_sell_plan_respects_disabled_odd_lot_configuration():
    ctx, broker, account = _setup(allow_odd_lot_sell=False)
    _hold(account, "000001.SZ", 150, 10.0)
    _submit(ctx, broker, {"000001.SZ": 0.0})
    broker.execute_close_orders(
        {"000001.SZ": _bar("000001.SZ")}, match_at=CLOSE)
    result = broker.target_achievements[0]
    assert result.planned_side is TargetPlanSide.SELL
    assert result.planned_shares == 100
    assert account.positions["000001.SZ"].shares == 50
    assert result.status is TargetAchievementStatus.OVER_TARGET
    assert "odd_lot_sell_disabled" in result.reasons


@pytest.mark.parametrize(
    ("kind", "expected_reason"),
    [
        ("limit", "limit_up_blocked_buy"),
        ("cap", "liquidity_cap"),
    ],
)
def test_limit_and_auction_cap_are_audited(kind, expected_reason):
    ctx, broker, _ = _setup()
    _submit(ctx, broker, {"000001.SZ": 0.5})
    bar = (
        _bar("000001.SZ", 11.0, pre_close=10.0)
        if kind == "limit"
        else _bar("000001.SZ", 10.0, cap=1_000.0)
    )
    broker.execute_close_orders({"000001.SZ": bar}, match_at=CLOSE)
    result = broker.target_achievements[0]
    assert result.status is TargetAchievementStatus.UNDER_TARGET
    assert expected_reason in result.reasons


def test_later_same_symbol_intent_supersedes_exactly_once():
    ctx, broker, account = _setup()
    older = _submit(ctx, broker, {"000001.SZ": 0.5})
    newer = _submit(
        ctx, broker, {"000001.SZ": 0.3},
        decision=DECISION + timedelta(seconds=1),
    )
    broker.execute_close_orders(
        {"000001.SZ": _bar("000001.SZ")}, match_at=CLOSE)

    by_id = {item.intent_id: item for item in broker.target_achievements}
    assert set(by_id) == {older.intent_id, newer.intent_id}
    assert by_id[older.intent_id].status is TargetAchievementStatus.SUPERSEDED
    assert by_id[newer.intent_id].status is TargetAchievementStatus.ACHIEVED
    assert account.positions["000001.SZ"].shares == 30_000
    # Re-entering the same window cannot publish a second terminal outcome.
    broker.execute_close_orders(
        {"000001.SZ": _bar("000001.SZ")}, match_at=CLOSE)
    assert len(broker.target_achievements) == 2


def test_manual_close_conflict_is_fail_fast_and_atomic():
    ctx, broker, account = _setup()
    with broker.order_creation_scope(
            "on_before_close", DECISION,
            eligible_from=CLOSE, expire_date=TRADE_DATE):
        with broker.target_intent_scope(CLOSE):
            manual = ctx.buy_at_close("000001.SZ", shares=100)
            ctx.order_target_percent(
                "000001.SZ", 0.3, when="close")
    before = _economic_snapshot(account, broker)

    with pytest.raises(SettlementInvariantError, match="manual CLOSE"):
        broker.execute_close_orders(
            {"000001.SZ": _bar("000001.SZ")}, match_at=CLOSE)

    assert _economic_snapshot(account, broker) == before
    assert manual.is_open


def test_entire_target_batch_rolls_back_on_second_order_fault(monkeypatch):
    ctx, broker, account = _setup()
    _submit(
        ctx,
        broker,
        {"000001.SZ": 0.4, "600000.SH": 0.4},
        rebalance=True,
    )
    bars = {
        "000001.SZ": _bar("000001.SZ"),
        "600000.SH": _bar("600000.SH"),
    }
    before = _economic_snapshot(account, broker)
    original = broker._execute_order
    calls = 0

    def fault(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected target fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(broker, "_execute_order", fault)
    with pytest.raises(RuntimeError, match="injected target fault"):
        broker.execute_close_orders(bars, match_at=CLOSE)

    assert _economic_snapshot(account, broker) == before


def test_system_orders_use_target_planning_phase_after_earlier_decision():
    ctx, broker, account = _setup()
    intent = _submit(ctx, broker, {"000001.SZ": 0.3})
    orders = broker.execute_close_orders(
        {"000001.SZ": _bar("000001.SZ")}, match_at=CLOSE)
    assert len(orders) == 1
    assert orders[0].created_phase == "target_planning"
    assert intent.created_at < orders[0].sim_created_at
    assert orders[0].sim_created_at == CLOSE


def test_single_daily_and_minute_engine_entries_materialize_at_exact_close():
    from diepi.backtest.liquidity import AuctionCapSpec, DailyAuctionLiquidityPolicy
    from diepi.backtest.strategy.base import Strategy
    from tests.backtest.test_single_engine_lifecycle_c0 import (
        SYMBOL,
        T,
        _Provider,
        _daily_frame,
        _minute_frame,
        _wire_engine,
    )

    class DailyTarget(Strategy):
        def on_after_open(self, ctx, bar):
            self.intent = ctx.order_target_percent(
                target_weight=0.5, when="close")

    daily_provider = _Provider(trade_days=[T], daily=_daily_frame())
    policy = DailyAuctionLiquidityPolicy(
        close_cap=AuctionCapSpec.fixed_yuan(1_000_000_000.0))
    daily_engine, daily_account = _wire_engine(
        daily_provider, freq="daily", daily_auction_liquidity=policy)
    daily_strategy = DailyTarget()
    daily_engine._strategy = daily_strategy
    daily_strategy.set_context(daily_engine._context)
    daily_engine._run_day(T, next_trade_date="20240103")
    assert daily_strategy.intent.eligible_from == datetime(2024, 1, 2, 15)
    assert daily_account.get_position(SYMBOL).shares > 0

    class MinuteTarget(Strategy):
        def on_before_close(self, ctx):
            self.intent = ctx.order_target_percent(
                target_weight=0.5, when="close")

    minute = _minute_frame(
        times=("09:30", "14:56", "14:57", "14:58", "15:00"))
    minute_provider = _Provider(
        trade_days=[T], daily=_daily_frame(),
        strategy_minute=minute, execution_minute=minute)
    minute_engine, minute_account = _wire_engine(
        minute_provider, freq="minute")
    minute_strategy = MinuteTarget()
    minute_engine._strategy = minute_strategy
    minute_strategy.set_context(minute_engine._context)
    minute_engine._run_day(T, next_trade_date="20240103")
    assert minute_strategy.intent.eligible_from == datetime(2024, 1, 2, 15)
    assert minute_account.get_position(SYMBOL).shares > 0


def test_portfolio_daily_and_minute_engine_entries_materialize_at_exact_close():
    from diepi.backtest.liquidity import AuctionCapSpec, DailyAuctionLiquidityPolicy
    from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy
    from tests.backtest.test_daily_auction_engine_contract import (
        SYMBOL,
        TRADE_DAY,
        _provider,
        _wire_engine,
    )
    from tests.backtest.test_c0_event_causality_synthetic import (
        SYMBOL_A,
        SyntheticDataProvider,
        T,
        _daily_frame,
        _minute_frame,
        _wire_portfolio_engine,
    )

    class DailyPortfolioTarget(PortfolioStrategy):
        def on_before_market_open(self, ctx):
            return [SYMBOL]

        def on_after_open(self, ctx, bars):
            self.intent = ctx.order_target_percent(
                SYMBOL, 0.5, when="close")

    daily_strategy = DailyPortfolioTarget()
    daily_harness = _wire_engine(
        "portfolio",
        _provider(),
        daily_strategy,
        policy=DailyAuctionLiquidityPolicy(
            close_cap=AuctionCapSpec.fixed_yuan(1_000_000_000.0)),
    )
    daily_harness.engine._run_day(TRADE_DAY)
    assert daily_strategy.intent.eligible_from == datetime(2024, 1, 2, 15)
    assert daily_harness.account.get_position(SYMBOL).shares > 0

    class MinutePortfolioTarget(PortfolioStrategy):
        def on_before_market_open(self, ctx):
            return [SYMBOL_A]

        def on_before_close(self, ctx):
            self.intent = ctx.order_target_percent(
                SYMBOL_A, 0.5, when="close")

    minute_strategy = MinutePortfolioTarget()
    daily = _daily_frame().iloc[[0]].copy()
    provider = SyntheticDataProvider(
        {SYMBOL_A: daily}, {SYMBOL_A: _minute_frame()})
    minute_engine = _wire_portfolio_engine(
        provider, minute_strategy, freq="minute")
    minute_engine._run_day(T)
    assert minute_strategy.intent.eligible_from == datetime(2024, 1, 2, 15)
    assert minute_engine._context.get_position(SYMBOL_A).shares > 0
