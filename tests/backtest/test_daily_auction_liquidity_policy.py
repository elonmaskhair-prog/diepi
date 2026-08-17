"""Pure tests for explicit, causal daily-auction liquidity caps."""

import math

import pytest

from diepi.backtest.liquidity import (
    AuctionCapMode,
    AuctionCapSpec,
    AuctionLiquidityUnavailable,
    AuctionWindow,
    DailyAuctionLiquidityPolicy,
    build_daily_auction_liquidity_policy,
)


def test_fixed_cap_resolves_without_market_data():
    policy = DailyAuctionLiquidityPolicy(
        open_cap=AuctionCapSpec.fixed_yuan(250_000.0),
    )

    assert policy.resolve(AuctionWindow.OPEN) == pytest.approx(250_000.0)


def test_previous_day_ratio_uses_only_explicit_prior_turnover():
    policy = DailyAuctionLiquidityPolicy(
        close_cap=AuctionCapSpec.previous_day_ratio(0.0125),
    )

    cap = policy.resolve(
        AuctionWindow.CLOSE,
        previous_day_amount_yuan=80_000_000.0,
    )
    assert cap == pytest.approx(1_000_000.0)


@pytest.mark.parametrize("window", [AuctionWindow.OPEN, AuctionWindow.CLOSE])
def test_unconfigured_window_fails_fast(window):
    with pytest.raises(AuctionLiquidityUnavailable, match="explicit liquidity cap"):
        DailyAuctionLiquidityPolicy().resolve(window)


@pytest.mark.parametrize("prior", [None, 0.0, -1.0, math.nan, math.inf])
def test_previous_day_rule_never_guesses_when_prior_turnover_is_unusable(prior):
    spec = AuctionCapSpec.previous_day_ratio(0.01)

    with pytest.raises((AuctionLiquidityUnavailable, ValueError)):
        spec.resolve(previous_day_amount_yuan=prior)


@pytest.mark.parametrize("value", [0.0, -1.0, math.nan, math.inf])
def test_cap_values_must_be_finite_and_positive(value):
    with pytest.raises(ValueError, match="finite and positive"):
        AuctionCapSpec.fixed_yuan(value)


@pytest.mark.parametrize("value", [True, "1", object()])
def test_cap_values_reject_coercible_non_real_inputs(value):
    with pytest.raises(TypeError, match="real number"):
        AuctionCapSpec.fixed_yuan(value)


@pytest.mark.parametrize("ratio", [1.0000001, 2.0])
def test_previous_day_ratio_cannot_exceed_full_prior_turnover(ratio):
    with pytest.raises(ValueError, match=r"\(0, 1\]"):
        AuctionCapSpec.previous_day_ratio(ratio)


def test_policy_rejects_dict_shortcuts_that_hide_schema_errors():
    with pytest.raises(TypeError, match="AuctionCapSpec"):
        DailyAuctionLiquidityPolicy(open_cap={"fixed_yuan": 10_000})


def test_mode_requires_enum_member_not_an_unversioned_string():
    with pytest.raises(TypeError, match="AuctionCapMode"):
        AuctionCapSpec("fixed_yuan", 10_000.0)


def test_audit_assumptions_are_stable_and_show_missing_window():
    policy = DailyAuctionLiquidityPolicy(
        open_cap=AuctionCapSpec(AuctionCapMode.FIXED_YUAN, 100_000.0),
        close_cap=AuctionCapSpec.previous_day_ratio(0.01),
    )

    assert policy.assumptions() == (
        ("liquidity.daily_open_cap", "fixed_yuan:100000"),
        ("liquidity.daily_close_cap", "previous_day_ratio:0.01"),
    )


def test_resolved_cap_at_float_boundary_stays_finite():
    spec = AuctionCapSpec.previous_day_ratio(1.0)
    largest = float.fromhex("0x1.fffffffffffffp+1023")

    assert spec.resolve(previous_day_amount_yuan=largest) == largest
    assert math.isfinite(spec.resolve(previous_day_amount_yuan=largest))


def test_window_requires_enum_member():
    policy = DailyAuctionLiquidityPolicy(
        open_cap=AuctionCapSpec.fixed_yuan(1.0),
    )
    with pytest.raises(TypeError, match="AuctionWindow"):
        policy.resolve("open")


def test_public_scalar_builder_keeps_windows_and_modes_explicit():
    policy = build_daily_auction_liquidity_policy(
        open_fixed_yuan=250_000.0,
        close_previous_day_ratio=0.02,
    )

    assert policy.open_cap == AuctionCapSpec.fixed_yuan(250_000.0)
    assert policy.close_cap == AuctionCapSpec.previous_day_ratio(0.02)


def test_public_scalar_builder_does_not_create_an_implicit_default():
    assert build_daily_auction_liquidity_policy() is None


@pytest.mark.parametrize("window", ["open", "close"])
def test_public_scalar_builder_rejects_two_rules_for_one_window(window):
    kwargs = {
        f"{window}_fixed_yuan": 100_000.0,
        f"{window}_previous_day_ratio": 0.01,
    }

    with pytest.raises(ValueError, match=f"daily {window} auction"):
        build_daily_auction_liquidity_policy(**kwargs)
