"""Strictly combine validated, successful futures result legs."""
from __future__ import annotations

from datetime import date
import json

import numpy as np
import pandas as pd

from ..backtest.metrics import MetricEngine, calculate_trade_round_metrics
from ..backtest.outcome import OutcomeTracker
from ..backtest.result_contract import ResultContract, ResultStatus

from .journal import EMPTY_FUTURES_EVENT_JOURNAL_JSON
from .result import (
    DEFAULT_ASSUMPTIONS,
    ENGINE_SCOPE,
    FuturesResult,
    FuturesValidationError,
    ValidationState,
)


def _canonical_date(value: object, *, leg: str, row: int) -> str:
    if type(value) is not str:
        raise ValueError(
            f"Leg {leg!r} daily_nav date at row {row} must be a string"
        )
    candidate = value
    if len(candidate) == 8 and candidate.isdigit():
        candidate = f"{candidate[:4]}-{candidate[4:6]}-{candidate[6:]}"
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        raise ValueError(
            f"Leg {leg!r} has invalid daily_nav date {value!r} at row {row}"
        ) from None


class FuturesCombiner:
    """Merge exact-calendar NAV series from completed futures runs."""

    @staticmethod
    def _validated_calendar(result: FuturesResult, label: str) -> tuple[list[str], list[str]]:
        if result.validation_state is not ValidationState.VALID or not result.is_valid:
            raise ValueError(
                f"Leg {label!r} has not passed validation: "
                f"state={result.validation_state.value}"
            )
        if result.validation_errors:
            raise ValueError(
                f"Leg {label!r} carries validation errors and cannot be combined"
            )
        if result.engine_scope != ENGINE_SCOPE:
            raise ValueError(
                f"Leg {label!r} has incompatible engine_scope "
                f"{result.engine_scope!r}"
            )
        if type(result.assumptions) is not dict or any(
            type(key) is not str or type(value) is not str
            for key, value in result.assumptions.items()
        ):
            raise ValueError(
                f"Leg {label!r} assumptions must be dict[str, str]"
            )

        contract = result.result_contract
        if contract is None:
            raise ValueError(f"Leg {label!r} is missing result_contract")
        if not isinstance(contract, ResultContract):
            raise TypeError(
                f"Leg {label!r} result_contract must be a ResultContract"
            )
        if contract.status is not ResultStatus.SUCCESS or not contract.is_rankable:
            raise ValueError(
                f"Leg {label!r} must have a SUCCESS result_contract; "
                f"got {contract.status.value}"
            )

        if not isinstance(result.daily_nav, pd.DataFrame):
            raise TypeError(f"Leg {label!r} daily_nav must be a DataFrame")
        required = {"date", "nav", "nav_worst"}
        missing = sorted(required - set(result.daily_nav.columns))
        if missing:
            raise ValueError(
                f"Leg {label!r} daily_nav is missing columns: {missing}"
            )

        raw_dates = result.daily_nav["date"].tolist()
        if not raw_dates:
            raise ValueError(f"Leg {label!r} daily_nav calendar must be non-empty")
        canonical_dates = [
            _canonical_date(value, leg=label, row=index)
            for index, value in enumerate(raw_dates)
        ]
        if len(set(canonical_dates)) != len(canonical_dates):
            raise ValueError(f"Leg {label!r} daily_nav calendar must be unique")
        if canonical_dates != sorted(canonical_dates):
            raise ValueError(
                f"Leg {label!r} daily_nav calendar must be strictly ordered"
            )

        coverage = contract.data_coverage
        interval = contract.actual_interval
        if coverage is None or coverage.actual_observations != len(raw_dates):
            raise ValueError(
                f"Leg {label!r} SUCCESS coverage does not match daily_nav rows"
            )
        if (
            interval is None
            or interval.start_date != canonical_dates[0]
            or interval.end_date != canonical_dates[-1]
        ):
            raise ValueError(
                f"Leg {label!r} SUCCESS interval does not match daily_nav calendar"
            )

        for column in ("nav", "nav_worst"):
            try:
                values = result.daily_nav[column].to_numpy(dtype=float)
            except (TypeError, ValueError):
                raise ValueError(
                    f"Leg {label!r} daily_nav.{column} must be numeric"
                ) from None
            if not np.isfinite(values).all():
                raise ValueError(
                    f"Leg {label!r} daily_nav.{column} must be finite"
                )
        close_nav = result.daily_nav["nav"].to_numpy(dtype=float)
        worst_nav = result.daily_nav["nav_worst"].to_numpy(dtype=float)
        if (close_nav <= 0).any():
            raise ValueError(f"Leg {label!r} daily_nav.nav must stay positive")
        if (worst_nav < 0).any():
            raise ValueError(
                f"Leg {label!r} daily_nav.nav_worst must be non-negative"
            )
        if (worst_nav > close_nav).any():
            raise ValueError(
                f"Leg {label!r} daily_nav.nav_worst cannot exceed nav"
            )

        return raw_dates, canonical_dates

    @staticmethod
    def combine(
        results: list[FuturesResult], labels: list[str] | None = None
    ) -> FuturesResult:
        """Combine legs only when validation, outcome, and calendars agree."""
        if not results:
            raise ValueError("No results to combine")

        if labels is None:
            labels = [f"leg_{index}" for index in range(len(results))]
        if len(labels) != len(results):
            raise ValueError(
                "labels length must exactly match the number of results"
            )
        if any(type(label) is not str or not label.strip() for label in labels):
            raise ValueError("Each label must be a non-empty string")
        if len(set(labels)) != len(labels):
            raise ValueError("labels must be unique")

        for index, result in enumerate(results):
            if not isinstance(result, FuturesResult):
                raise TypeError(f"results[{index}] must be a FuturesResult")

        calendars: list[list[str]] = []
        metric_calendars: list[list[str]] = []
        for result, label in zip(results, labels):
            raw_dates, canonical_dates = FuturesCombiner._validated_calendar(
                result, label
            )
            calendars.append(raw_dates)
            metric_calendars.append(canonical_dates)

        calendar = calendars[0]
        metric_calendar = metric_calendars[0]
        for label, leg_calendar, leg_metric_calendar in zip(
            labels[1:], calendars[1:], metric_calendars[1:]
        ):
            if leg_calendar != calendar or leg_metric_calendar != metric_calendar:
                raise ValueError(
                    "All leg daily_nav calendars must match exactly; "
                    f"leg {label!r} differs"
                )

        total_account = float(sum(result.account_size for result in results))
        close_matrix = np.vstack([
            result.daily_nav["nav"].to_numpy(dtype=float) for result in results
        ])
        worst_matrix = np.vstack([
            result.daily_nav["nav_worst"].to_numpy(dtype=float)
            for result in results
        ])
        close_nav = close_matrix.sum(axis=0)
        worst_nav = worst_matrix.sum(axis=0)

        daily_df = pd.DataFrame({
            "date": calendar,
            "nav": close_nav,
            "nav_worst": worst_nav,
            "position": "COMBINED",
            "contract": "",
        })

        trade_frames = []
        for result, label in zip(results, labels):
            if not result.trades.empty:
                leg_trades = result.trades.copy()
                leg_trades["leg"] = label
                trade_frames.append(leg_trades)
        trades_df = (
            pd.concat(trade_frames, ignore_index=True)
            if trade_frames else pd.DataFrame()
        )

        metric_engine = MetricEngine()
        combined_metric_assumptions = dict(
            metric_engine.config.assumptions())
        for result, label in zip(results, labels):
            mismatched = {
                key: result.assumptions.get(key)
                for key, expected in combined_metric_assumptions.items()
                if result.assumptions.get(key) != expected
            }
            if mismatched:
                raise ValueError(
                    f"Leg {label!r} metric assumptions do not match the "
                    f"combiner: {mismatched}"
                )
        metrics = metric_engine.calculate(
            initial_nav=total_account,
            dates=metric_calendar,
            close_nav=close_nav.tolist(),
            intraday_low_nav=worst_nav.tolist(),
        )
        worst_drawdown = metrics.max_drawdown_intraday_low_nav
        if worst_drawdown is None:  # Defensive: an intraday series was supplied.
            raise RuntimeError("MetricEngine did not return worst-NAV drawdown")
        calmar_close = (
            metrics.annual_return / metrics.max_drawdown_close_nav
            if metrics.max_drawdown_close_nav > 0 else 0.0
        )
        calmar_worst = (
            metrics.annual_return / worst_drawdown
            if worst_drawdown > 0 else 0.0
        )

        trade_metrics = calculate_trade_round_metrics(
            trades_df["pnl_yuan"].tolist() if not trades_df.empty else ()
        )
        trade_count = trade_metrics.closed_round_count

        component_signal_policies = [
            result.assumptions.get("signal_policy", "unknown")
            for result in results
        ]
        if any(type(policy) is not str or not policy.strip()
               for policy in component_signal_policies):
            raise ValueError("Each component signal_policy must be a non-empty string")
        signal_policies = sorted(set(component_signal_policies))

        assumptions = dict(DEFAULT_ASSUMPTIONS)
        assumptions.update(dict(metric_engine.config.assumptions()))
        assumptions.update({
            "signal_policy": (
                signal_policies[0]
                if len(signal_policies) == 1
                else f"mixed({','.join(signal_policies)})"
            ),
            "contract_expiry_source": "component_results",
            "roll_schedule_source": "component_results",
            "combiner_calendar_policy": "exact_match",
            "combiner_initial_nav_policy": "sum_component_account_size",
            "combiner_metric_policy": "exact_component_config_match",
            "combined_stress_aggregation": (
                "sum_same_day_component_nav_worst_conservative"
            ),
            "event_journal_aggregation": (
                "none_no_synthetic_combined_margin_or_roll_events"
            ),
            "component_count": str(len(results)),
            "component_labels": json.dumps(
                labels, ensure_ascii=False, separators=(",", ":")
            ),
            "component_assumptions": json.dumps(
                {
                    label: dict(sorted(result.assumptions.items()))
                    for result, label in zip(results, labels)
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        })
        # Fail at construction time if an assumption accidentally stops being
        # a stable JSON string value.
        json.dumps(assumptions, ensure_ascii=False, allow_nan=False)

        expected_observation_ids = tuple(
            f"COMBINED|{observation_date}"
            for observation_date in metric_calendar
        )
        outcome = OutcomeTracker(
            expected_observation_ids=expected_observation_ids,
            coverage_unit="futures_trading_day",
        )
        for key in sorted(assumptions):
            value = assumptions[key]
            if type(value) is not str:
                raise TypeError(f"Combined assumption {key!r} must be a string")
            outcome.add_assumption(key, value)
        for observation_id, observation_date in zip(
            expected_observation_ids, metric_calendar
        ):
            outcome.observe(observation_id, observation_date)
        result_contract = outcome.finalize_completed()
        if result_contract.status is not ResultStatus.SUCCESS:
            raise RuntimeError("Complete combined calendar did not produce SUCCESS")

        combined = FuturesResult(
            product="COMBINED",
            start_date=calendar[0],
            end_date=calendar[-1],
            account_size=total_account,
            lots=0,
            multiplier=0,
            final_nav=round(float(close_nav[-1]), 2),
            total_return=round(metrics.total_return, 4),
            cagr=round(metrics.annual_return, 4),
            sharpe=(
                None
                if metrics.sharpe_ratio is None
                else round(metrics.sharpe_ratio, 3)
            ),
            max_drawdown_close=round(metrics.max_drawdown_close_nav, 4),
            max_drawdown_worst=round(worst_drawdown, 4),
            calmar_close=round(calmar_close, 3),
            calmar_worst=round(calmar_worst, 3),
            trade_count=trade_count,
            win_rate=(
                None
                if trade_metrics.win_rate is None
                else round(trade_metrics.win_rate, 3)
            ),
            avg_trade_pnl=(
                round(float(trades_df["pnl_yuan"].mean()), 2)
                if trade_count > 0 else 0.0
            ),
            daily_nav=daily_df,
            trades=trades_df,
            expected_calendar_len=len(calendar),
            assumptions=assumptions,
            result_contract=result_contract,
            event_journal_json=EMPTY_FUTURES_EVENT_JOURNAL_JSON,
        )
        if not combined.validate():
            errors = "\n".join(combined.validation_errors)
            raise FuturesValidationError(
                f"Combined futures result validation failed:\n{errors}"
            )
        return combined


__all__ = ["FuturesCombiner"]
