"""Pure unit tests for the shared metric implementation."""

from dataclasses import replace
import math

import pytest

from diepi.backtest.metrics import (
    MetricConfig,
    MetricEngine,
    MetricResult,
    calculate_trade_round_metrics,
)


def test_first_day_loss_is_measured_from_explicit_initial_nav():
    result = MetricEngine().calculate(
        initial_nav=100.0,
        dates=("2024-01-02",),
        close_nav=(50.0,),
        intraday_low_nav=(40.0,),
    )

    assert result.daily_returns == pytest.approx((-0.5,))
    assert result.total_return == pytest.approx(-0.5)
    assert result.max_drawdown_close_nav == pytest.approx(0.5)
    assert result.max_drawdown_intraday_low_nav == pytest.approx(0.6)


def test_causal_low_drawdown_uses_only_prior_close_peak():
    result = MetricEngine().calculate(
        initial_nav=100.0,
        dates=("2024-01-02", "2024-01-03"),
        close_nav=(120.0, 110.0),
        intraday_low_nav=(90.0, 80.0),
    )

    # Day one low cannot use its later 120 close as a peak.
    assert result.causal_intraday_low_drawdowns == pytest.approx((0.1, 1 / 3))
    assert result.max_drawdown_intraday_low_nav == pytest.approx(1 / 3)


def test_same_day_high_to_low_is_labeled_as_separate_pessimistic_bound():
    result = MetricEngine().calculate(
        initial_nav=100.0,
        dates=("2024-01-02",),
        close_nav=(100.0,),
        intraday_low_nav=(75.0,),
        intraday_high_nav=(125.0,),
    )

    assert result.max_drawdown_close_nav == 0.0
    assert result.max_drawdown_intraday_low_nav == pytest.approx(0.25)
    assert result.max_drawdown_intraday_high_to_low == pytest.approx(0.4)


def test_daily_returns_include_t0_baseline_for_sharpe():
    config = MetricConfig(
        trading_days_per_year=4,
        risk_free_rate=0.0,
        return_std_ddof=0,
    )
    result = MetricEngine(config).calculate(
        initial_nav=100.0,
        dates=("2024-01-02", "2024-01-03"),
        close_nav=(110.0, 110.0),
    )

    # Returns are +10%, 0%; omitting t0 would leave only a zero return.
    assert result.daily_returns == pytest.approx((0.1, 0.0))
    assert result.sharpe_ratio == pytest.approx(2.0)


def test_zero_dispersion_sharpe_is_unknown_not_synthetic_zero():
    result = MetricEngine(MetricConfig(risk_free_rate=0.0)).calculate(
        initial_nav=100.0,
        dates=("2024-01-02", "2024-01-03"),
        close_nav=(100.0, 100.0),
    )

    assert result.sharpe_ratio is None


def test_no_closed_rounds_has_null_win_rate():
    result = calculate_trade_round_metrics((), open_round_count=3)

    assert result.open_round_count == 3
    assert result.closed_round_count == 0
    assert result.win_rate is None


def test_closed_round_win_rate_counts_only_positive_realized_pnl():
    result = calculate_trade_round_metrics((1.0, 0.0, -2.0, 3.0))

    assert result.winning_round_count == 2
    assert result.closed_round_count == 4
    assert result.win_rate == pytest.approx(0.5)


@pytest.mark.parametrize(
    "dates",
    [
        ("2024-01-03", "2024-01-02"),
        ("2024-01-02", "2024-01-02"),
    ],
)
def test_dates_must_be_strictly_increasing_and_unique(dates):
    with pytest.raises(ValueError, match="increasing|unique"):
        MetricEngine().calculate(
            initial_nav=100.0,
            dates=dates,
            close_nav=(100.0, 100.0),
        )


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, -1.0])
def test_nav_boundaries_reject_invalid_values(value):
    with pytest.raises(ValueError):
        MetricEngine().calculate(
            initial_nav=100.0,
            dates=("2024-01-02",),
            close_nav=(value,),
        )


def test_intraday_vectors_must_be_coherent_with_close():
    engine = MetricEngine()
    with pytest.raises(ValueError, match="cannot exceed close"):
        engine.calculate(
            initial_nav=100.0,
            dates=("2024-01-02",),
            close_nav=(100.0,),
            intraday_low_nav=(101.0,),
        )
    with pytest.raises(ValueError, match="cannot be below close"):
        engine.calculate(
            initial_nav=100.0,
            dates=("2024-01-02",),
            close_nav=(100.0,),
            intraday_low_nav=(90.0,),
            intraday_high_nav=(99.0,),
        )


def test_metric_config_assumptions_are_deterministic():
    assert MetricConfig(244, 0.01, 1).assumptions() == (
        ("metrics.schema_version", "1"),
        ("metrics.trading_days_per_year", "244"),
        ("metrics.risk_free_rate", "0.01"),
        ("metrics.return_std_ddof", "1"),
    )


def _audited_result(*, with_intraday=True):
    kwargs = {}
    if with_intraday:
        kwargs = {
            "intraday_low_nav": (90.0, 80.0),
            "intraday_high_nav": (125.0, 120.0),
        }
    return MetricEngine(
        MetricConfig(
            trading_days_per_year=4,
            risk_free_rate=0.0,
            return_std_ddof=0,
        )
    ).calculate(
        initial_nav=100.0,
        dates=("2024-01-02", "2024-01-03"),
        close_nav=(120.0, 100.0),
        **kwargs,
    )


def test_engine_result_carries_canonical_dates_and_metric_config():
    result = _audited_result()

    assert result.dates == ("2024-01-02", "2024-01-03")
    assert result.config == MetricConfig(4, 0.0, 0)


def test_original_manual_metric_result_shape_remains_constructible():
    result = MetricResult(
        total_return=0,
        annual_return=0,
        sharpe_ratio=None,
        max_drawdown_close_nav=0,
        max_drawdown_intraday_low_nav=None,
        max_drawdown_intraday_high_to_low=None,
        daily_returns=(0,),
        close_drawdowns=(0,),
        causal_intraday_low_drawdowns=None,
        intraday_high_to_low_drawdowns=None,
    )

    assert result.total_return == 0.0
    assert result.dates is None
    assert result.config is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("close_drawdowns", (0.0,)),
        ("causal_intraday_low_drawdowns", (0.0,)),
        ("intraday_high_to_low_drawdowns", (0.0,)),
        ("dates", ("2024-01-02",)),
    ],
)
def test_metric_audit_vector_lengths_must_match(field, value):
    with pytest.raises(ValueError, match="length"):
        replace(_audited_result(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_drawdown_close_nav", -0.1),
        ("max_drawdown_close_nav", 1.1),
        ("max_drawdown_intraday_low_nav", -0.1),
        ("max_drawdown_intraday_high_to_low", 1.1),
        ("close_drawdowns", (0.0, 1.1)),
        ("causal_intraday_low_drawdowns", (-0.1, 0.1)),
        ("intraday_high_to_low_drawdowns", (0.1, 1.1)),
    ],
)
def test_metric_drawdowns_are_strictly_bounded(field, value):
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        replace(_audited_result(), **{field: value})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("total_return", 0.123, "total_return must match"),
        ("annual_return", 0.123, "annual_return must match"),
        ("max_drawdown_close_nav", 0.123, "must equal max"),
        ("max_drawdown_intraday_low_nav", 0.123, "must equal max"),
        (
            "max_drawdown_intraday_high_to_low",
            0.123,
            "must equal max",
        ),
        ("sharpe_ratio", None, "nullability"),
    ],
)
def test_metric_scalars_must_recompute_from_audit_vectors_and_config(
    field, value, message
):
    with pytest.raises(ValueError, match=message):
        replace(_audited_result(), **{field: value})


def test_optional_intraday_max_and_vector_must_have_matching_presence():
    result = _audited_result()

    with pytest.raises(ValueError, match="both be present or None"):
        replace(result, max_drawdown_intraday_low_nav=None)
    with pytest.raises(ValueError, match="both be present or None"):
        replace(result, causal_intraday_low_drawdowns=None)


@pytest.mark.parametrize(
    "dates",
    [
        ("2024-W01-1", "2024-01-03"),
        ("2024-01-02", "2024-01-02"),
        ("2024-01-03", "2024-01-02"),
    ],
)
def test_metric_audit_dates_are_canonical_unique_and_increasing(dates):
    with pytest.raises(ValueError):
        replace(_audited_result(), dates=dates)


@pytest.mark.parametrize(
    "field",
    ["close_nav", "intraday_low_nav", "intraday_high_nav"],
)
def test_all_nav_audit_inputs_must_be_strictly_positive(field):
    kwargs = {
        "initial_nav": 100.0,
        "dates": ("2024-01-02",),
        "close_nav": (100.0,),
        "intraday_low_nav": (90.0,),
        "intraday_high_nav": (110.0,),
    }
    kwargs[field] = (0.0,)

    with pytest.raises(ValueError, match="positive"):
        MetricEngine().calculate(**kwargs)


def test_metric_audit_vectors_reject_non_finite_values():
    with pytest.raises(ValueError, match="finite"):
        replace(_audited_result(), daily_returns=(math.nan, 0.0))


def test_extreme_annualization_uses_stable_value_error_not_overflow_error():
    with pytest.raises(ValueError, match="annual_return overflowed"):
        MetricEngine().calculate(
            initial_nav=1.0,
            dates=("2024-01-02",),
            close_nav=(1e10,),
        )


def test_empty_metric_audit_vectors_are_rejected():
    with pytest.raises(ValueError, match="must not be empty"):
        MetricResult(
            total_return=0.0,
            annual_return=0.0,
            sharpe_ratio=None,
            max_drawdown_close_nav=0.0,
            max_drawdown_intraday_low_nav=None,
            max_drawdown_intraday_high_to_low=None,
            daily_returns=(),
            close_drawdowns=(),
            causal_intraday_low_drawdowns=None,
            intraday_high_to_low_drawdowns=None,
        )
