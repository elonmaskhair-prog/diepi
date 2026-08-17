"""Pure contract tests for delayed close target intents and achievements."""

import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from diepi.backtest.broker.target import (
    TARGET_ACHIEVEMENT_SCHEMA,
    TARGET_INTENT_SCHEMA,
    TargetAchievement,
    TargetAchievementStatus,
    TargetIntent,
    TargetPlanSide,
    TargetWindow,
)


CREATED = datetime(2024, 1, 3, 9, 31)
ELIGIBLE = datetime(2024, 1, 3, 15, 0)


def _intent(**overrides):
    values = {
        "intent_id": "intent-0001",
        "batch_id": "batch-0001",
        "symbol": "510300.SH",
        "target_weight": 0.5,
        "created_at": CREATED,
        "created_phase": "on_after_open",
        "eligible_from": ELIGIBLE,
        "expire_date": "20240103",
        "min_trade_amount": 1000.0,
        "window": TargetWindow.CLOSE,
        "note": "rebalance",
    }
    values.update(overrides)
    return TargetIntent(**values)


def _achievement(**overrides):
    values = {
        "intent_id": "intent-0001",
        "batch_id": "batch-0001",
        "trade_date": "20240103",
        "symbol": "510300.SH",
        "target_weight": 0.5,
        "reference_nav": 1_000_000.0,
        "reference_price": 4.0,
        "target_notional": 500_000.0,
        "planned_side": TargetPlanSide.BUY,
        "planned_shares": 125_000,
        "order_ids": ("order-0001",),
        "filled_shares": 125_000,
        "achieved_value": 500_000.0,
        "achieved_weight": 0.5,
        "gap_weight": 0.0,
        "cap_respected": True,
        "status": TargetAchievementStatus.ACHIEVED,
        "reasons": (),
    }
    values.update(overrides)
    return TargetAchievement(**values)


def test_intent_is_frozen_deterministic_and_contains_no_sizing_information():
    intent = _intent()

    assert intent.to_json() == _intent().to_json()
    payload = json.loads(intent.to_json())
    assert payload["schema"] == TARGET_INTENT_SCHEMA
    assert payload["window"] == "close"
    assert {
        "reference_nav",
        "reference_price",
        "target_notional",
        "planned_side",
        "planned_shares",
        "shares",
        "amount",
        "close",
    }.isdisjoint(payload)
    with pytest.raises(FrozenInstanceError):
        intent.target_weight = 0.25


def test_intent_strict_from_dict_roundtrip_and_input_mutation_isolated():
    payload = _intent().to_dict()
    restored = TargetIntent.from_dict(payload)
    assert restored == _intent()
    payload["note"] = "mutated"
    assert restored.note == "rebalance"


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda p: p.__setitem__("extra", 1), "keys mismatch"),
        (lambda p: p.pop("note"), "keys mismatch"),
        (lambda p: p.__setitem__("schema", "wrong"), "schema"),
        (lambda p: p.__setitem__("schema_version", 2), "schema_version"),
        (lambda p: p.__setitem__("schema_version", True), "schema_version"),
        (lambda p: p.__setitem__("window", "CLOSE"), "window"),
        (
            lambda p: p.__setitem__("symbol", f" {p['symbol']} "),
            "canonical wire",
        ),
        (
            lambda p: p.__setitem__("note", f" {p['note']} "),
            "canonical wire",
        ),
        (
            lambda p: p.__setitem__("created_at", "2024-01-03T09:31:00"),
            "created_at",
        ),
        (
            lambda p: p.__setitem__(
                "eligible_from", "2024-01-03T15:00:00.000000+00:00"),
            "eligible_from",
        ),
    ],
)
def test_intent_from_dict_rejects_schema_drift(mutate, match):
    payload = _intent().to_dict()
    mutate(payload)
    with pytest.raises(ValueError, match=match):
        TargetIntent.from_dict(payload)


@pytest.mark.parametrize("target_weight", [0.0, 1.0])
def test_intent_accepts_inclusive_unlevered_target_bounds(target_weight):
    assert _intent(target_weight=target_weight).target_weight == target_weight


@pytest.mark.parametrize(
    "field,value,match",
    [
        ("target_weight", -0.01, "target_weight"),
        ("target_weight", 1.01, "target_weight"),
        ("target_weight", float("nan"), "target_weight"),
        ("target_weight", True, "target_weight"),
        ("min_trade_amount", -1.0, "min_trade_amount"),
        ("min_trade_amount", float("inf"), "min_trade_amount"),
        ("created_phase", "on_day", "created_phase"),
        ("created_at", CREATED.replace(tzinfo=timezone.utc), "timezone-naive"),
        ("eligible_from", CREATED, "strictly after"),
        ("eligible_from", ELIGIBLE + timedelta(days=1), "creation date"),
        ("expire_date", "2024-01-03", "YYYYMMDD"),
        ("expire_date", "20240104", "eligible close"),
        ("window", "close", "TargetWindow.CLOSE"),
    ],
)
def test_intent_rejects_invalid_or_noncausal_state(field, value, match):
    with pytest.raises(ValueError, match=match):
        _intent(**{field: value})


def test_intent_orders_by_execution_then_decision_and_stable_ids():
    first = _intent(intent_id="intent-2", created_at=CREATED)
    second = _intent(intent_id="intent-1", created_at=CREATED + timedelta(minutes=1))
    assert sorted((second, first), key=lambda item: item.ordering_key) == [first, second]


def test_achievement_is_frozen_canonical_and_defines_signed_gap():
    result = _achievement()

    assert json.loads(result.to_json())["schema"] == TARGET_ACHIEVEMENT_SCHEMA
    assert result.gap_weight == result.achieved_weight - result.target_weight
    assert result.cap_respected is True
    with pytest.raises(FrozenInstanceError):
        result.status = TargetAchievementStatus.BLOCKED


def test_achievement_strict_from_dict_roundtrip_and_list_mutation_isolated():
    payload = _achievement().to_dict()
    restored = TargetAchievement.from_dict(payload)
    assert restored == _achievement()
    payload["order_ids"].append("order-mutated")
    payload["reasons"].append("mutated")
    assert restored.order_ids == ("order-0001",)
    assert restored.reasons == ()


def test_blocked_missing_bar_roundtrips_without_inventing_a_price():
    result = _achievement(
        reference_price=0.0,
        planned_side=TargetPlanSide.NONE,
        planned_shares=0,
        order_ids=(),
        filled_shares=0,
        achieved_value=0.0,
        achieved_weight=0.0,
        gap_weight=-0.5,
        cap_respected=True,
        status=TargetAchievementStatus.BLOCKED,
        reasons=("no_close_bar",),
    )

    assert result.reference_price == 0.0
    assert TargetAchievement.from_dict(result.to_dict()) == result


def test_zero_reference_price_is_rejected_outside_missing_bar_outcomes():
    with pytest.raises(ValueError, match="reference_price=0"):
        _achievement(reference_price=0.0)


@pytest.mark.parametrize(
    "mutate,match",
    [
        (lambda p: p.__setitem__("extra", 1), "keys mismatch"),
        (lambda p: p.pop("status"), "keys mismatch"),
        (lambda p: p.__setitem__("schema", "wrong"), "schema"),
        (lambda p: p.__setitem__("schema_version", 0), "schema_version"),
        (lambda p: p.__setitem__("planned_side", "BUY"), "planned_side"),
        (lambda p: p.__setitem__("status", "DONE"), "status"),
        (
            lambda p: p.__setitem__("symbol", f" {p['symbol']} "),
            "canonical wire",
        ),
        (
            lambda p: p["reasons"].append(" trailing "),
            "canonical wire",
        ),
        (lambda p: p.__setitem__("order_ids", tuple(p["order_ids"])), "list"),
        (lambda p: p.__setitem__("reasons", tuple(p["reasons"])), "list"),
    ],
)
def test_achievement_from_dict_rejects_schema_drift(mutate, match):
    payload = _achievement().to_dict()
    mutate(payload)
    with pytest.raises(ValueError, match=match):
        TargetAchievement.from_dict(payload)


def test_under_target_records_unfilled_outcome_without_breaking_the_cap():
    result = _achievement(
        planned_shares=125_000,
        filled_shares=100_000,
        achieved_value=400_000.0,
        achieved_weight=0.4,
        gap_weight=-0.1,
        cap_respected=True,
        status=TargetAchievementStatus.UNDER_TARGET,
        reasons=("auction_liquidity_cap",),
    )

    assert result.status is TargetAchievementStatus.UNDER_TARGET
    assert result.cap_respected


def test_t1_blocked_reduction_can_be_explicitly_over_target():
    result = _achievement(
        planned_side=TargetPlanSide.SELL,
        planned_shares=20_000,
        order_ids=(),
        filled_shares=0,
        achieved_value=600_000.0,
        achieved_weight=0.6,
        gap_weight=0.1,
        cap_respected=False,
        status=TargetAchievementStatus.OVER_TARGET,
        reasons=("t1_locked",),
    )

    assert not result.cap_respected


@pytest.mark.parametrize("side", [TargetPlanSide.BUY, TargetPlanSide.SELL])
def test_direction_is_preserved_when_constraints_reduce_plan_to_zero(side):
    result = _achievement(
        planned_side=side,
        planned_shares=0,
        order_ids=(),
        filled_shares=0,
        achieved_value=400_000.0,
        achieved_weight=0.4,
        gap_weight=-0.1,
        cap_respected=True,
        status=TargetAchievementStatus.UNDER_TARGET,
        reasons=("constraint_reduced_to_zero",),
    )
    assert result.planned_side is side
    assert result.planned_shares == 0


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"target_notional": 499_999.0}, "target_notional"),
        ({"achieved_weight": 0.4}, "achieved_weight"),
        ({"gap_weight": 0.1}, "gap_weight"),
        ({"planned_side": TargetPlanSide.NONE}, "planned_shares=0"),
        ({"planned_shares": 100_000, "filled_shares": 125_000}, "filled_shares"),
        ({"order_ids": (), "filled_shares": 1}, "order_id"),
        ({"order_ids": ("order-1", "order-1")}, "duplicates"),
        ({"cap_respected": False}, "cap_respected"),
        ({"status": TargetAchievementStatus.UNDER_TARGET}, "negative target gap"),
        (
            {
                "status": TargetAchievementStatus.BLOCKED,
                "planned_side": TargetPlanSide.NONE,
                "planned_shares": 0,
                "order_ids": (),
                "filled_shares": 0,
            },
            "reason",
        ),
    ],
)
def test_achievement_rejects_internally_inconsistent_state(overrides, match):
    with pytest.raises(ValueError, match=match):
        _achievement(**overrides)


def test_noop_is_terminal_and_requires_an_explicit_reason():
    result = _achievement(
        planned_side=TargetPlanSide.NONE,
        planned_shares=0,
        order_ids=(),
        filled_shares=0,
        status=TargetAchievementStatus.NOOP,
        reasons=("already_at_target",),
    )
    assert result.status is TargetAchievementStatus.NOOP
