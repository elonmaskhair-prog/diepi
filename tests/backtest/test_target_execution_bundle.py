"""Stable result and persistence contracts for delayed target execution."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timedelta
import json
from pathlib import Path

import pandas as pd
import pytest

from diepi.backtest.broker import (
    TargetAchievement,
    TargetAchievementStatus,
    TargetExecutionBundle,
    TargetIntent,
    TargetPlanSide,
    TargetWindow,
)
from diepi.backtest.broker.target_execution import (
    TARGET_EXECUTION_BUNDLE_SCHEMA,
    TARGET_EXECUTION_BUNDLE_SCHEMA_VERSION,
)
from diepi.backtest.data.result_storage import ResultStorage
from diepi.backtest.engine.backtest_engine import BacktestResult
from diepi.backtest.engine.parallel_runner import (
    _parse_wire_target_execution,
    _restore_backtest_result,
    _serialize_backtest_result_wire,
)
from diepi.backtest.engine.portfolio_engine import PortfolioResult
from diepi.backtest.result_contract import (
    ActualInterval,
    DataCoverage,
    ResultContract,
    ResultReason,
    ResultStatus,
)


CREATED = datetime(2024, 1, 3, 9, 31)
ELIGIBLE = datetime(2024, 1, 3, 15, 0)


def _intent(
    intent_id: str,
    *,
    batch_id: str = "batch-1",
    symbol: str = "510300.SH",
    target_weight: float = 0.4,
    created_at: datetime = CREATED,
) -> TargetIntent:
    return TargetIntent(
        intent_id=intent_id,
        batch_id=batch_id,
        symbol=symbol,
        target_weight=target_weight,
        created_at=created_at,
        created_phase="on_after_open",
        eligible_from=ELIGIBLE,
        expire_date="20240103",
        min_trade_amount=0.0,
        window=TargetWindow.CLOSE,
        note="target decision",
    )


def _achievement(
    intent: TargetIntent,
    *,
    order_id: str,
    batch_id: str = None,
    symbol: str = None,
    target_weight: float = None,
    trade_date: str = None,
) -> TargetAchievement:
    weight = intent.target_weight if target_weight is None else target_weight
    reference_nav = 1_000_000.0
    reference_price = 10.0
    achieved_value = reference_nav * weight
    planned_shares = int(achieved_value / reference_price)
    return TargetAchievement(
        intent_id=intent.intent_id,
        batch_id=intent.batch_id if batch_id is None else batch_id,
        trade_date=intent.expire_date if trade_date is None else trade_date,
        symbol=intent.symbol if symbol is None else symbol,
        target_weight=weight,
        reference_nav=reference_nav,
        reference_price=reference_price,
        target_notional=reference_nav * weight,
        planned_side=TargetPlanSide.BUY,
        planned_shares=planned_shares,
        order_ids=(order_id,),
        filled_shares=planned_shares,
        achieved_value=achieved_value,
        achieved_weight=weight,
        gap_weight=0.0,
        cap_respected=True,
        status=TargetAchievementStatus.ACHIEVED,
        reasons=(),
    )


def _bundle() -> TargetExecutionBundle:
    first = _intent("intent-2", batch_id="batch-2")
    second = _intent(
        "intent-1",
        batch_id="batch-1",
        symbol="600000.SH",
        target_weight=0.3,
        created_at=CREATED + timedelta(minutes=1),
    )
    # Broker snapshots need not share an order: achievements are commonly
    # emitted symbol-first while intents are appended at decision time.
    return TargetExecutionBundle.from_snapshots(
        (second, first),
        (_achievement(second, order_id="order-2"),
         _achievement(first, order_id="order-1")),
    )


def _success_contract() -> ResultContract:
    return ResultContract(
        status=ResultStatus.SUCCESS,
        actual_interval=ActualInterval("2024-01-03", "2024-01-03"),
        data_coverage=DataCoverage(1, 1, 1.0),
    )


def _failed_contract() -> ResultContract:
    return ResultContract(
        status=ResultStatus.FAILED,
        reason=ResultReason("ENGINE_RUNTIME_ERROR", "The engine failed"),
    )


def _pending_bundle() -> TargetExecutionBundle:
    intent = _intent("pending-intent")
    return TargetExecutionBundle.from_snapshots((intent,), ())


def _backtest_result(
    bundle: TargetExecutionBundle,
    *,
    contract: ResultContract = None,
) -> BacktestResult:
    return BacktestResult(
        symbol="510300.SH",
        start_date="20240103",
        end_date="20240103",
        initial_cash=1_000_000.0,
        final_value=1_000_000.0,
        total_return=0.0,
        annual_return=0.0,
        max_drawdown=0.0,
        trade_count=2,
        win_rate=None,
        sharpe_ratio=None,
        daily_values=pd.DataFrame(
            index=pd.to_datetime(["2024-01-03"])
        ),
        result_contract=_success_contract() if contract is None else contract,
        target_execution=bundle,
    )


def _portfolio_result(bundle=None, *, contract=None) -> PortfolioResult:
    return PortfolioResult(
        start_date="20240103",
        end_date="20240103",
        initial_cash=1_000_000.0,
        final_value=1_000_000.0,
        total_return=0.0,
        annual_return=0.0,
        max_drawdown=0.0,
        trade_count=2,
        sharpe_ratio=None,
        result_contract=contract,
        target_execution=bundle,
    )


@pytest.fixture
def isolated_storage(tmp_path, monkeypatch):
    monkeypatch.setattr(ResultStorage, "RESULTS_DIR", str(tmp_path))
    from diepi.backtest.data.report_generator import ReportGenerator

    monkeypatch.setattr(
        ReportGenerator,
        "generate",
        staticmethod(lambda *args, **kwargs: None),
    )
    return tmp_path


def _read_meta(folder) -> dict:
    return json.loads((Path(folder) / "meta.json").read_text(encoding="utf-8"))


def _write_meta(folder, value, *, allow_nan=False) -> None:
    (Path(folder) / "meta.json").write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=allow_nan,
        ),
        encoding="utf-8",
    )


def test_empty_bundle_is_explicit_frozen_and_deterministic():
    bundle = TargetExecutionBundle.empty()

    assert bundle.to_dict() == {
        "achievements": [],
        "complete": True,
        "intents": [],
        "pending_intent_ids": [],
        "schema": TARGET_EXECUTION_BUNDLE_SCHEMA,
        "schema_version": TARGET_EXECUTION_BUNDLE_SCHEMA_VERSION,
    }
    assert TargetExecutionBundle.from_dict(bundle.to_dict()) == bundle
    assert bundle.to_json() == TargetExecutionBundle.empty().to_json()
    with pytest.raises(FrozenInstanceError):
        bundle.intents = ()


def test_result_fields_append_bundle_without_moving_legacy_positions():
    assert [field.name for field in fields(BacktestResult)][-3:] == [
        "result_contract",
        "target_execution",
        "cash_audit",
    ]
    assert [field.name for field in fields(PortfolioResult)][-4:] == [
        "error",
        "comparisons",
        "target_execution",
        "cash_audit",
    ]

    legacy_single = BacktestResult(
        "510300.SH",
        "20240103",
        "20240103",
        1_000_000.0,
        1_000_000.0,
        0.0,
        0.0,
        0.0,
        0,
        None,
        None,
    )
    legacy_portfolio = PortfolioResult(
        "20240103",
        "20240103",
        1_000_000.0,
        1_000_000.0,
        0.0,
        0.0,
        0.0,
        0,
        None,
    )
    assert legacy_single.target_execution is None
    assert legacy_portfolio.target_execution is None


def test_snapshot_factory_canonicalizes_and_aligns_by_intent_id():
    bundle = _bundle()

    assert [item.intent_id for item in bundle.intents] == [
        "intent-2",
        "intent-1",
    ]
    assert [item.intent_id for item in bundle.achievements] == [
        "intent-2",
        "intent-1",
    ]
    assert TargetExecutionBundle.from_dict(bundle.to_dict()) == bundle
    assert TargetExecutionBundle.from_dict(bundle.to_dict()).to_json() == (
        bundle.to_json()
    )


def test_snapshot_factory_preserves_pending_intent_evidence():
    first = _intent("intent-1")
    second = _intent(
        "intent-2", created_at=CREATED + timedelta(minutes=1)
    )
    bundle = TargetExecutionBundle.from_snapshots(
        (first, second),
        (_achievement(first, order_id="order-1"),),
    )

    assert not bundle.complete
    assert bundle.pending_intent_ids == (second.intent_id,)
    assert bundle.to_dict()["pending_intent_ids"] == [second.intent_id]
    assert TargetExecutionBundle.from_dict(bundle.to_dict()) == bundle


@pytest.mark.parametrize("field", ["complete", "pending_intent_ids"])
def test_deserialization_rejects_false_pending_redundancy(field):
    payload = _pending_bundle().to_dict()
    payload[field] = True if field == "complete" else []

    with pytest.raises(ValueError, match="canonical"):
        TargetExecutionBundle.from_dict(payload)


def test_success_result_requires_complete_bundle_but_failed_preserves_pending():
    pending = _pending_bundle()

    with pytest.raises(ValueError, match="SUCCESS BacktestResult"):
        _backtest_result(pending)

    failed = _backtest_result(pending, contract=_failed_contract())
    assert failed.target_execution.pending_intent_ids == ("pending-intent",)
    assert not failed.target_execution.complete
    assert failed.to_dict()["target_execution"] == pending.to_dict()


@pytest.mark.parametrize("case", ["orphan", "duplicate"])
def test_snapshot_factory_rejects_invalid_achievement_evidence(case):
    first = _intent("intent-1")
    second = _intent(
        "intent-2", created_at=CREATED + timedelta(minutes=1)
    )
    first_result = _achievement(first, order_id="order-1")
    second_result = _achievement(second, order_id="order-2")

    if case == "orphan":
        orphan = _achievement(
            _intent("orphan", created_at=CREATED + timedelta(minutes=2)),
            order_id="order-orphan",
        )
        achievements = (first_result, second_result, orphan)
    else:
        achievements = (first_result, first_result)

    with pytest.raises(ValueError):
        TargetExecutionBundle.from_snapshots(
            (first, second), achievements
        )


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("batch_id", "wrong-batch", "batch_id"),
        ("symbol", "000001.SZ", "symbol"),
        ("target_weight", 0.2, "target_weight"),
        ("trade_date", "20240104", "trade_date"),
    ],
)
def test_bundle_rejects_wrong_intent_achievement_association(
    field, value, match
):
    intent = _intent("intent-1")
    achievement = _achievement(
        intent, order_id="order-1", **{field: value}
    )

    with pytest.raises(ValueError, match=match):
        TargetExecutionBundle.from_snapshots((intent,), (achievement,))


def test_bundle_rejects_order_reuse_and_noncanonical_serialized_order():
    first = _intent("intent-1")
    second = _intent(
        "intent-2",
        symbol="600000.SH",
        created_at=CREATED + timedelta(minutes=1),
    )
    with pytest.raises(ValueError, match="order_id"):
        TargetExecutionBundle.from_snapshots(
            (first, second),
            (
                _achievement(first, order_id="shared-order"),
                _achievement(second, order_id="shared-order"),
            ),
        )

    payload = _bundle().to_dict()
    payload["intents"].reverse()
    payload["achievements"].reverse()
    with pytest.raises(ValueError, match="canonical decision order"):
        TargetExecutionBundle.from_dict(payload)


def test_result_dict_exposes_decisions_separately_from_final_outcomes():
    result = _backtest_result(_bundle())
    payload = result.to_dict()["target_execution"]
    intent = payload["intents"][0]
    achievement = payload["achievements"][0]

    assert {
        "reference_nav",
        "reference_price",
        "target_notional",
        "planned_side",
        "planned_shares",
        "shares",
        "side",
        "close",
        "nav",
    }.isdisjoint(intent)
    assert {
        "reference_nav",
        "reference_price",
        "target_notional",
        "planned_side",
        "planned_shares",
        "filled_shares",
        "achieved_weight",
        "status",
    }.issubset(achievement)

    portfolio = _portfolio_result(_bundle())
    assert portfolio.to_dict()["target_execution"] == payload


def test_real_cross_batch_same_symbol_supersede_bundle_is_aligned():
    from tests.backtest.test_target_intent_integration import (
        CLOSE,
        _bar,
        _setup,
        _submit,
    )

    ctx, broker, _ = _setup()
    older = _submit(ctx, broker, {"000001.SZ": 0.5})
    newer = _submit(
        ctx,
        broker,
        {"000001.SZ": 0.3},
        decision=CREATED.replace(hour=14, minute=55) + timedelta(seconds=1),
    )
    assert older.batch_id != newer.batch_id
    broker.execute_close_orders(
        {"000001.SZ": _bar("000001.SZ")}, match_at=CLOSE
    )

    bundle = TargetExecutionBundle.from_snapshots(
        broker.target_intents_snapshot(),
        broker.target_achievements_snapshot(),
    )

    assert [item.intent_id for item in bundle.intents] == [
        older.intent_id,
        newer.intent_id,
    ]
    assert [item.intent_id for item in bundle.achievements] == [
        older.intent_id,
        newer.intent_id,
    ]
    assert bundle.achievements[0].status is TargetAchievementStatus.SUPERSEDED
    assert bundle.achievements[1].status is TargetAchievementStatus.ACHIEVED


def test_parallel_wire_strictly_preserves_nonempty_target_bundle():
    bundle = _bundle()
    payload = _serialize_backtest_result_wire(_backtest_result(bundle))

    restored = _restore_backtest_result("510300.SH", payload)

    assert restored.target_execution == bundle
    assert restored.target_execution is not bundle
    assert restored.to_dict()["target_execution"] == bundle.to_dict()


@pytest.mark.parametrize(
    "case", ["duplicate", "wrong_link", "nan", "pending_success"]
)
def test_parallel_wire_rejects_corrupt_target_bundle(case):
    payload = _backtest_result(_bundle()).to_dict()
    target = payload["target_execution"]
    if case == "duplicate":
        target["intents"][1] = deepcopy(target["intents"][0])
    elif case == "wrong_link":
        target["achievements"][0]["symbol"] = "000001.SZ"
    elif case == "nan":
        target["intents"][0]["target_weight"] = float("nan")
    else:
        target["achievements"] = []
        target["complete"] = False
        target["pending_intent_ids"] = [
            item["intent_id"] for item in target["intents"]
        ]

    with pytest.raises((TypeError, ValueError)):
        _restore_backtest_result("510300.SH", payload)


@pytest.mark.parametrize("payload", [{}, {"target_execution": None}])
def test_current_worker_wire_requires_recorded_target_bundle(payload):
    with pytest.raises(ValueError, match="current result payload"):
        _parse_wire_target_execution(payload, require_recorded=True)


def test_result_storage_roundtrips_bundle_and_legacy_missing_is_none(
    isolated_storage,
):
    bundle = _bundle()
    folder = ResultStorage.save(
        _portfolio_result(bundle), {"frequency": "daily"}, "pass"
    )
    meta = _read_meta(folder)

    restored, config, code = ResultStorage.load(folder)

    assert meta["target_execution"] == bundle.to_dict()
    assert restored.target_execution == bundle
    assert restored.target_execution is not bundle
    assert config == {"frequency": "daily"}
    assert code == "pass"

    del meta["target_execution"]
    meta["version"] = "1.1-legacy-targetless"
    _write_meta(folder, meta)
    legacy, _, _ = ResultStorage.load(folder)
    assert legacy.target_execution is None


def test_result_storage_distinguishes_explicit_empty_from_legacy_none(
    isolated_storage,
):
    folder = ResultStorage.save(
        _portfolio_result(TargetExecutionBundle.empty()), {}, ""
    )

    restored, _, _ = ResultStorage.load(folder)

    assert restored.target_execution == TargetExecutionBundle.empty()
    assert _read_meta(folder)["target_execution"] == (
        TargetExecutionBundle.empty().to_dict()
    )


def test_result_storage_roundtrips_failed_pending_evidence(
    isolated_storage,
):
    pending = _pending_bundle()
    folder = ResultStorage.save(
        _portfolio_result(pending, contract=_failed_contract()), {}, ""
    )

    restored, _, _ = ResultStorage.load(folder)

    assert restored.result_contract.status is ResultStatus.FAILED
    assert restored.target_execution == pending
    assert restored.target_execution.pending_intent_ids == ("pending-intent",)


def test_result_storage_rejects_mutated_success_with_pending_evidence(
    isolated_storage,
):
    result = _portfolio_result(
        TargetExecutionBundle.empty(), contract=_success_contract()
    )
    result.target_execution = _pending_bundle()

    with pytest.raises(ValueError, match="SUCCESS PortfolioResult"):
        ResultStorage.save(result, {}, "")

    assert list(isolated_storage.iterdir()) == []


def test_result_storage_rejects_untyped_bundle_on_save(isolated_storage):
    result = _portfolio_result()
    result.target_execution = _bundle().to_dict()

    with pytest.raises(TypeError, match="exactly TargetExecutionBundle"):
        ResultStorage.save(result, {}, "")

    assert list(isolated_storage.iterdir()) == []


@pytest.mark.parametrize(
    "case", ["duplicate", "wrong_link", "nan", "unknown", "reordered"]
)
def test_result_storage_rejects_corrupt_target_bundle(
    isolated_storage, case
):
    folder = ResultStorage.save(_portfolio_result(_bundle()), {}, "")
    meta = _read_meta(folder)
    target = meta["target_execution"]
    if case == "duplicate":
        target["achievements"][1]["intent_id"] = (
            target["achievements"][0]["intent_id"]
        )
    elif case == "wrong_link":
        target["achievements"][0]["batch_id"] = "wrong-batch"
    elif case == "nan":
        target["achievements"][0]["reference_nav"] = float("nan")
    elif case == "unknown":
        target["unknown"] = True
    else:
        target["intents"].reverse()
        target["achievements"].reverse()
    _write_meta(folder, meta, allow_nan=(case == "nan"))

    with pytest.raises((TypeError, ValueError)):
        ResultStorage.load(folder)


def test_result_storage_listing_skips_corrupt_target_bundle(
    isolated_storage,
):
    folder = ResultStorage.save(_portfolio_result(_bundle()), {}, "")
    meta = _read_meta(folder)
    meta["target_execution"]["achievements"][0]["symbol"] = "000001.SZ"
    _write_meta(folder, meta)

    assert ResultStorage.list_records() == []


@pytest.mark.parametrize("freq", ["daily", "minute"])
def test_single_cash_engine_result_contains_target_and_achievement(freq):
    from diepi.backtest.liquidity import AuctionCapSpec, DailyAuctionLiquidityPolicy
    from diepi.backtest.strategy.base import Strategy
    from tests.backtest.test_single_engine_lifecycle_c0 import (
        T,
        _Provider,
        _daily_frame,
        _minute_frame,
        _wire_engine,
    )

    class TargetStrategy(Strategy):
        def on_after_open(self, ctx, bar):
            if freq == "daily":
                self.intent = ctx.order_target_percent(
                    target_weight=0.5, when="close"
                )

        def on_before_close(self, ctx):
            if freq == "minute":
                self.intent = ctx.order_target_percent(
                    target_weight=0.5, when="close"
                )

    minute = _minute_frame(
        times=("09:30", "14:56", "14:57", "14:58", "15:00")
    )
    provider = _Provider(
        trade_days=[T],
        daily=_daily_frame(),
        strategy_minute=minute,
        execution_minute=minute,
    )
    policy = DailyAuctionLiquidityPolicy(
        close_cap=AuctionCapSpec.fixed_yuan(1_000_000_000.0)
    )
    engine, _ = _wire_engine(
        provider,
        freq=freq,
        daily_auction_liquidity=policy if freq == "daily" else None,
    )

    result = engine.run(TargetStrategy())

    assert type(result.target_execution) is TargetExecutionBundle
    assert len(result.target_execution.intents) == 1
    assert len(result.target_execution.achievements) == 1
    assert result.target_execution.intents[0].intent_id == (
        result.target_execution.achievements[0].intent_id
    )


@pytest.mark.parametrize("freq", ["daily", "minute"])
def test_portfolio_cash_engine_result_contains_target_and_achievement(freq):
    from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy
    from tests.backtest.test_c0_event_causality_synthetic import (
        SYMBOL_A,
        SYMBOL_B,
        SyntheticDataProvider,
        T,
        _daily_frame,
        _minute_frame,
        _wire_portfolio_engine,
    )

    class TargetStrategy(PortfolioStrategy):
        def on_after_open(self, ctx, bars):
            if freq == "daily":
                self.intent = ctx.order_target_percent(
                    SYMBOL_A, 0.4, when="close"
                )

        def on_before_close(self, ctx):
            if freq == "minute":
                self.intent = ctx.order_target_percent(
                    SYMBOL_A, 0.4, when="close"
                )

    daily = {SYMBOL_A: _daily_frame(), SYMBOL_B: _daily_frame()}
    minute = (
        {SYMBOL_A: _minute_frame(), SYMBOL_B: _minute_frame()}
        if freq == "minute"
        else None
    )
    strategy = TargetStrategy()
    engine = _wire_portfolio_engine(
        SyntheticDataProvider(daily, minute), strategy, freq=freq
    )

    # This test drives the private day primitive directly.  Mirror the public
    # run lifecycle's audit boundaries so the result is backed by the same
    # seed+journal proof as an ordinary engine run.
    engine._ensure_cash_replay_seed()
    engine._run_day(T)
    engine._context._broker.cancel_all_orders()
    engine._snapshot_cash_audit()
    result = engine._create_result()

    assert type(result.target_execution) is TargetExecutionBundle
    assert result.target_execution.intents == (strategy.intent,)
    assert len(result.target_execution.achievements) == 1
    assert result.target_execution.achievements[0].intent_id == (
        strategy.intent.intent_id
    )


def test_single_engine_primary_failure_after_committed_intent_is_not_masked():
    from diepi.backtest.strategy.base import Strategy
    from tests.backtest.test_single_engine_lifecycle_c0 import (
        T,
        _Provider,
        _daily_frame,
        _wire_engine,
    )

    primary = LookupError("single failure after committed target intent")

    class FailingTarget(Strategy):
        def on_after_open(self, ctx, bar):
            self.intent = ctx.order_target_percent(
                target_weight=0.5, when="close"
            )
            raise primary

    engine, _ = _wire_engine(
        _Provider(trade_days=[T], daily=_daily_frame()), freq="daily"
    )

    with pytest.raises(LookupError) as captured:
        engine.run(FailingTarget())

    assert captured.value is primary
    assert engine.last_result_contract.status is ResultStatus.FAILED
    bundle = engine._target_execution_bundle()
    assert not bundle.complete
    assert bundle.achievements == ()
    assert bundle.pending_intent_ids == (bundle.intents[0].intent_id,)


def test_portfolio_engine_primary_failure_after_committed_intent_is_not_masked():
    from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy
    from tests.backtest.test_cash_engine_result_contract import (
        SYMBOL,
        _Provider,
        _portfolio_engine,
    )

    engine = _portfolio_engine(_Provider())
    primary = RuntimeError("portfolio failure after committed target intent")

    def fail_after_target(day, **kwargs):
        broker = engine._context._broker
        broker.set_date(day)
        broker.set_market_phase("trading")
        decision = datetime.strptime(day, "%Y%m%d").replace(
            hour=9, minute=31
        )
        close = decision.replace(hour=15, minute=0)
        with broker.order_creation_scope("on_after_open", decision):
            with broker.target_intent_scope(close):
                engine._context.order_target_percent(
                    SYMBOL, 0.5, when="close"
                )
        raise primary

    engine._run_day = fail_after_target

    with pytest.raises(RuntimeError) as captured:
        engine.run(PortfolioStrategy())

    assert captured.value is primary
    assert engine.last_result_contract.status is ResultStatus.FAILED
    bundle = engine._target_execution_bundle()
    assert not bundle.complete
    assert bundle.achievements == ()
    assert bundle.pending_intent_ids == (bundle.intents[0].intent_id,)


def test_initialized_empty_result_has_explicit_empty_bundle_but_legacy_does_not():
    from diepi.backtest.broker import Account, Broker
    from diepi.backtest.engine.context import Context

    legacy = _backtest_result(TargetExecutionBundle.empty())
    legacy.target_execution = None
    assert legacy.to_dict()["target_execution"] is None

    account = Account(initial_cash=1_000_000.0)
    broker = Broker(account=account)
    context = Context(broker=broker)
    from diepi.backtest.engine.backtest_engine import BacktestEngine

    engine = BacktestEngine(
        "510300.SH", "20240103", "20240103", initial_cash=1_000_000.0
    )
    engine._context = context
    engine._ensure_cash_replay_seed()
    engine._snapshot_cash_audit()
    current = engine._create_empty_result()
    assert current.target_execution == TargetExecutionBundle.empty()


def test_non_success_cash_result_paths_publish_proven_empty_bundle():
    from diepi.backtest.strategy.base import Strategy
    from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy
    from tests.backtest.test_cash_engine_result_contract import (
        DAYS,
        _Provider,
        _append_nav,
        _portfolio_engine,
        _single_engine,
    )

    single_invalid = _single_engine(_Provider(()))
    single_invalid_result = single_invalid.run(Strategy())
    assert single_invalid_result.result_contract.status is ResultStatus.INVALID
    assert single_invalid_result.target_execution == TargetExecutionBundle.empty()

    single_partial = _single_engine(_Provider())

    def partial_day(day, **kwargs):
        if day == DAYS[0]:
            _append_nav(single_partial, day, close=100.0)
        else:
            single_partial._current_day_low_total_value = None

    single_partial._run_day = partial_day
    single_partial_result = single_partial.run(Strategy())
    assert single_partial_result.result_contract.status is ResultStatus.PARTIAL
    assert single_partial_result.target_execution == TargetExecutionBundle.empty()

    portfolio_canceled = _portfolio_engine(
        _Provider(), stop_check=lambda: True
    )
    canceled_result = portfolio_canceled.run(PortfolioStrategy())
    assert canceled_result.result_contract.status is ResultStatus.CANCELED
    assert canceled_result.target_execution == TargetExecutionBundle.empty()

    portfolio_invalid = _portfolio_engine(_Provider())
    portfolio_invalid._context.get_stock_pool = lambda: []
    invalid_result = portfolio_invalid.run(PortfolioStrategy())
    assert invalid_result.result_contract.status is ResultStatus.INVALID
    assert invalid_result.target_execution == TargetExecutionBundle.empty()
