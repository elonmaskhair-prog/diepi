"""Contract and metric boundaries for futures result combination."""
from __future__ import annotations

import json

import pandas as pd
import pytest

from diepi.backtest.outcome import OutcomeTracker
from diepi.backtest.result_contract import ResultStatus
from diepi.futures.combiner import FuturesCombiner
from diepi.futures.result import FuturesResult, ValidationState


D1 = "20240102"
D2 = "20240103"
D3 = "20240104"
_AUTO_CONTRACT = object()


def _completed_contract(dates: list[str]):
    expected_ids = tuple(f"leg|{value}" for value in dates)
    tracker = OutcomeTracker(
        expected_observation_ids=expected_ids,
        coverage_unit="futures_trading_day",
    )
    for observation_id, value in zip(expected_ids, dates):
        tracker.observe(observation_id, value)
    return tracker.finalize_completed()


def _partial_contract(dates: list[str]):
    observed_ids = tuple(f"leg|{value}" for value in dates)
    tracker = OutcomeTracker(
        expected_observation_ids=observed_ids + ("leg|missing",),
        coverage_unit="futures_trading_day",
    )
    for observation_id, value in zip(observed_ids, dates):
        tracker.observe(observation_id, value)
    return tracker.finalize_completed()


def _trade(entry: str, exit_: str, pnl: float) -> dict:
    return {
        "entry_date": entry,
        "exit_date": exit_,
        "pnl_yuan": pnl,
    }


def _result(
    *,
    dates: list[str] | None = None,
    nav: list[float] | None = None,
    nav_worst: list[float] | None = None,
    account_size: float = 100.0,
    trades: list[dict] | None = None,
    result_contract: object = _AUTO_CONTRACT,
    validate: bool = True,
    max_drawdown_close: float = 0.0,
    max_drawdown_worst: float = 0.0,
) -> FuturesResult:
    dates = list(dates or [D1, D2])
    nav = list(nav or [account_size, account_size])
    nav_worst = list(nav_worst or nav)
    trade_rows = list(trades or [])
    contract = (
        _completed_contract(dates)
        if result_contract is _AUTO_CONTRACT else result_contract
    )
    wins = sum(float(row["pnl_yuan"]) > 0 for row in trade_rows)
    result = FuturesResult(
        product="IC",
        start_date=dates[0],
        end_date=dates[-1],
        account_size=account_size,
        lots=1,
        multiplier=200,
        final_nav=float(nav[-1]),
        total_return=float(nav[-1]) / account_size - 1.0,
        trade_count=len(trade_rows),
        win_rate=(wins / len(trade_rows)) if trade_rows else None,
        avg_trade_pnl=(
            sum(float(row["pnl_yuan"]) for row in trade_rows)
            / len(trade_rows)
            if trade_rows else 0.0
        ),
        max_drawdown_close=max_drawdown_close,
        max_drawdown_worst=max_drawdown_worst,
        daily_nav=pd.DataFrame({
            "date": dates,
            "nav": nav,
            "nav_worst": nav_worst,
            "position": ["FLAT"] * len(dates),
            "contract": [""] * len(dates),
        }),
        trades=pd.DataFrame(trade_rows),
        expected_calendar_len=len(dates),
        result_contract=contract,
    )
    if validate:
        result.validate()
    return result


def test_new_result_is_unvalidated_and_never_reports_ok():
    result = _result(validate=False)

    assert result.validation_state is ValidationState.UNVALIDATED
    assert result.is_valid is False
    assert "Valid:           UNVALIDATED" in result.summary()
    assert result.to_dict()["validation_state"] == "UNVALIDATED"


def test_result_rejects_scope_or_non_string_assumptions():
    wrong_scope = _result(validate=False)
    wrong_scope.engine_scope = "strict_daily_settlement_futures"
    assert not wrong_scope.validate()
    assert any("engine_scope must be" in error
               for error in wrong_scope.validation_errors)

    wrong_assumption = _result(validate=False)
    wrong_assumption.assumptions["product_margin_rate"] = 0.14
    assert not wrong_assumption.validate()
    assert any("keys and values must be strings" in error
               for error in wrong_assumption.validation_errors)


def test_combiner_rejects_unvalidated_leg():
    result = _result(validate=False)

    with pytest.raises(ValueError, match="has not passed validation.*UNVALIDATED"):
        FuturesCombiner.combine([result])


def test_combiner_rejects_validation_failed_leg():
    result = _result(
        max_drawdown_close=0.2,
        max_drawdown_worst=0.1,
    )
    assert result.validation_state is ValidationState.INVALID

    with pytest.raises(ValueError, match="has not passed validation.*INVALID"):
        FuturesCombiner.combine([result])


@pytest.mark.parametrize(
    ("contract_factory", "message"),
    [
        (lambda dates: None, "missing result_contract"),
        (lambda dates: _partial_contract(dates), "SUCCESS result_contract.*PARTIAL"),
    ],
)
def test_combiner_rejects_missing_or_partial_contract(contract_factory, message):
    dates = [D1, D2]
    result = _result(
        dates=dates,
        result_contract=contract_factory(dates),
    )
    assert result.validation_state is ValidationState.VALID

    with pytest.raises(ValueError, match=message):
        FuturesCombiner.combine([result])


def test_combiner_rejects_calendar_mismatch_instead_of_filling():
    first = _result(dates=[D1, D2])
    second = _result(dates=[D1, D3])

    with pytest.raises(ValueError, match="calendars must match exactly"):
        FuturesCombiner.combine([first, second], ["first", "second"])


def test_combiner_rejects_component_metric_config_mismatch():
    result = _result(validate=False)
    result.assumptions["metrics.risk_free_rate"] = "0"
    assert result.validate()

    with pytest.raises(ValueError, match="metric assumptions do not match"):
        FuturesCombiner.combine([result])


@pytest.mark.parametrize(
    ("mutated_dates", "message"),
    [
        ([], "calendar must be non-empty"),
        ([D1, D1], "calendar must be unique"),
        ([D2, D1], "calendar must be strictly ordered"),
    ],
)
def test_combiner_rechecks_nonempty_unique_ordered_calendar(
    mutated_dates, message
):
    result = _result(dates=[D1, D2])
    if mutated_dates:
        result.daily_nav["date"] = mutated_dates
    else:
        result.daily_nav = result.daily_nav.iloc[0:0]

    with pytest.raises(ValueError, match=message):
        FuturesCombiner.combine([result])


@pytest.mark.parametrize("labels", [[], ["one", "two"]])
def test_labels_length_must_match_exactly(labels):
    with pytest.raises(ValueError, match="labels length must exactly match"):
        FuturesCombiner.combine([_result()], labels)


def test_metric_baseline_includes_first_day_loss():
    leg = _result(
        account_size=100.0,
        nav=[90.0, 95.0],
        nav_worst=[90.0, 94.0],
    )

    combined = FuturesCombiner.combine([leg], ["loss_leg"])

    assert combined.total_return == pytest.approx(-0.05)
    assert combined.max_drawdown_close == pytest.approx(0.10)
    assert combined.max_drawdown_worst == pytest.approx(0.10)


def test_zero_trade_combination_has_null_win_rate():
    combined = FuturesCombiner.combine([_result()], ["quiet"])

    assert combined.trade_count == 0
    assert combined.win_rate is None
    assert combined.to_dict()["win_rate"] is None
    assert "Win rate:        N/A" in combined.summary()


def test_successful_combination_returns_valid_success_contract():
    first = _result(
        account_size=1_000.0,
        nav=[990.0, 1_010.0],
        nav_worst=[980.0, 1_000.0],
        trades=[_trade(D1, D2, 10.0)],
    )
    second = _result(
        account_size=2_000.0,
        nav=[2_010.0, 2_020.0],
        nav_worst=[1_990.0, 2_000.0],
        trades=[_trade(D1, D2, 20.0)],
    )

    combined = FuturesCombiner.combine(
        [first, second], ["first", "second"]
    )

    assert combined.validation_state is ValidationState.VALID
    assert combined.is_valid
    assert combined.validation_errors == []
    assert combined.result_contract is not None
    assert combined.result_contract.status is ResultStatus.SUCCESS
    assert combined.result_contract.is_rankable
    assert combined.result_contract.data_coverage.actual_observations == 2
    assert {
        item.key: item.value for item in combined.result_contract.assumptions
    }["data_coverage.unit"] == "futures_trading_day"
    assert combined.daily_nav["nav"].tolist() == [3_000.0, 3_030.0]
    assert combined.trades["leg"].tolist() == ["first", "second"]
    assert combined.win_rate == 1.0
    component_assumptions = json.loads(
        combined.assumptions["component_assumptions"])
    assert component_assumptions["first"] == first.assumptions
    assert component_assumptions["second"] == second.assumptions
    json.dumps(combined.assumptions, ensure_ascii=False, allow_nan=False)
    json.dumps(combined.to_dict(), ensure_ascii=False, allow_nan=False)
