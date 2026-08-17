"""Pure in-memory event-order tests for the approximate futures engine."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from diepi.backtest.result_contract import ResultStatus
from diepi.futures.combiner import FuturesCombiner
from diepi.futures.contract import ContractSelection
from diepi.futures.cost import CostModel
from diepi.futures.engine import FuturesEngine


CONTRACT = "IC9999.CFX"
D1 = "20240102"
D2 = "20240103"
D3 = "20240104"


@dataclass
class _SyntheticSelector:
    prices: dict[str, dict[str, float]]

    @property
    def calendar(self) -> list[str]:
        return sorted(self.prices)

    def select(self, trade_date: str, min_days_remaining: int = 7,
               method: str = "volume_t1") -> ContractSelection | None:
        if trade_date not in self.prices:
            return None
        return ContractSelection(
            ts_code=CONTRACT,
            trade_date=trade_date,
            method=method,
            expiry_date="20991231",
            days_to_expiry=99,
            volume_rank=1,
            reason="synthetic",
        )

    def get_price(self, ts_code: str, trade_date: str,
                  field: str) -> float | None:
        if ts_code != CONTRACT:
            return None
        return self.prices.get(trade_date, {}).get(field)

    def is_valid_on(self, ts_code: str, trade_date: str) -> bool:
        return ts_code == CONTRACT and trade_date in self.prices

    def is_expired_on(self, ts_code: str, trade_date: str) -> bool:
        return False

    def has_data_on(self, ts_code: str, trade_date: str) -> bool:
        return self.is_valid_on(ts_code, trade_date)


class _LiveContractGapSelector(_SyntheticSelector):
    @property
    def calendar(self) -> list[str]:
        return [D1, D2]


def _bar(open_: float, close: float, *, low: float | None = None,
         high: float | None = None) -> dict[str, float]:
    return {
        "open": open_,
        "high": high if high is not None else max(open_, close),
        "low": low if low is not None else min(open_, close),
        "close": close,
    }


def _engine(prices: dict[str, dict[str, float]], *,
            account_size: float = 30_000.0,
            start: str | None = None,
            end: str | None = None,
            trading_calendar: tuple[str, ...] | None = None) -> FuturesEngine:
    """Wire the public engine API without touching filesystem-backed data."""
    engine = object.__new__(FuturesEngine)
    engine._product = "IC"
    engine._start = start or (min(prices) if prices else D1)
    engine._end = end or (max(prices) if prices else D2)
    engine._account_size = account_size
    engine._lots = 1
    engine._contract_method = "volume_t1"
    engine._min_expiry = 7
    engine._multiplier = 200
    engine._margin_rate = 0.14
    engine._cost = CostModel(commission_rate=0.0, slippage_points=0.0)
    engine._selector = _SyntheticSelector(prices)
    engine._trading_calendar = tuple(
        trading_calendar if trading_calendar is not None else sorted(prices)
    )
    engine._trading_calendar_source = "explicit_test_calendar"
    engine._trading_calendar_hash = "synthetic"
    return engine


def _signals(*directions: str) -> pd.DataFrame:
    dates = [D1, D2][:len(directions)]
    return pd.DataFrame({"trade_date": dates, "direction": directions})


def test_new_position_is_margin_checked_at_its_entry_day_close():
    # 30k covers IC margin at the 1000 open (28k).  The adverse close leaves
    # NAV=26k, below close margin=27.44k, so liquidation belongs to D1 close.
    engine = _engine({
        D1: _bar(1_000.0, 980.0, low=970.0, high=1_005.0),
        D2: _bar(980.0, 980.0),
    })

    result = engine.run(_signals("LONG", "FLAT"))

    assert result.trade_count == 1
    trade = result.trades.iloc[0]
    assert trade["entry_date"] == D1
    assert trade["exit_date"] == D1
    assert trade["exit_reason"] == "margin_call"
    assert trade["hold_days"] == 0
    day_one = result.daily_nav.set_index("date").loc[D1]
    assert day_one["position"] == "FLAT"
    assert day_one["nav"] == pytest.approx(26_000.0)


def test_flat_signal_executes_at_open_before_that_days_close_margin_check():
    engine = _engine({
        D1: _bar(1_000.0, 1_000.0),
        # Holding through this close would breach maintenance margin, but the
        # FLAT signal has already closed the position at the day's open.
        D2: _bar(1_000.0, 900.0, low=890.0, high=1_005.0),
    })

    result = engine.run(_signals("LONG", "FLAT"))

    assert result.trade_count == 1
    trade = result.trades.iloc[0]
    assert trade["exit_date"] == D2
    assert trade["exit_price"] == pytest.approx(1_000.0)
    assert trade["exit_reason"] == "signal_change"
    assert result.final_nav == pytest.approx(30_000.0)


def test_reversal_executes_at_open_then_checks_new_direction_at_close():
    engine = _engine({
        D1: _bar(1_000.0, 1_000.0),
        # The old LONG would breach at close.  The open SHORT reversal instead
        # profits into the close and must survive the close-only margin check.
        D2: _bar(1_000.0, 900.0, low=890.0, high=1_005.0),
    })

    result = engine.run(_signals("LONG", "SHORT"))

    assert result.trade_count == 2
    old_position = result.trades.iloc[0]
    new_position = result.trades.iloc[1]
    assert old_position["exit_date"] == D2
    assert old_position["exit_reason"] == "signal_change"
    assert old_position["exit_price"] == pytest.approx(1_000.0)
    assert new_position["entry_date"] == D2
    assert new_position["direction"] == "SHORT"
    assert new_position["exit_reason"] == "end_of_backtest"
    assert result.final_nav == pytest.approx(50_000.0)


def test_failure_paths_raise_instead_of_returning_empty_success_results():
    engine = _engine({D1: _bar(1_000.0, 1_000.0)})
    with pytest.raises(ValueError, match="Invalid direction"):
        engine.run(_signals("NOT_A_SIGNAL"))
    assert engine.last_result_contract is None

    empty_engine = _engine({})
    with pytest.raises(ValueError, match="No trading days"):
        empty_engine.run(_signals("FLAT"))
    assert empty_engine.last_result_contract is None


def test_live_contract_data_gap_is_not_relabelled_as_expiry():
    engine = _engine({D1: _bar(1_000.0, 1_000.0)})
    engine._end = D2
    engine._selector = _LiveContractGapSelector(
        {D1: _bar(1_000.0, 1_000.0)})
    engine._trading_calendar = (D1, D2)

    with pytest.raises(ValueError, match="data gap as contract expiry"):
        engine.run(_signals("LONG", "LONG"))

    contract = engine.last_result_contract
    assert contract is not None
    assert contract.status is ResultStatus.FAILED
    assert contract.reason.code == "FUTURES_ENGINE_RUNTIME_ERROR"
    assert contract.data_coverage.expected_observations == 2
    assert contract.data_coverage.actual_observations == 1
    assert contract.actual_interval.start_date == "2024-01-02"
    assert contract.actual_interval.end_date == "2024-01-02"
    assert {
        item.key: item.value for item in contract.assumptions
    }["data_coverage.unit"] == "futures_trading_day"
    failure_assumptions = {
        item.key: item.value for item in contract.assumptions
    }
    assert failure_assumptions["engine_scope"] == (
        "approximate_index_futures_research"
    )
    assert failure_assumptions["settlement_model"] == (
        "cumulative_nav_close_mark_not_daily_settlement"
    )


def test_result_declares_approximate_scope_and_close_only_margin_boundary():
    engine = _engine({D1: _bar(1_000.0, 1_000.0)})

    result = engine.run(_signals("FLAT"))

    assert result.engine_scope == "approximate_index_futures_research"
    assert result.assumptions["margin_maintenance_checkpoint"] == "daily_close_only"
    assert result.assumptions["intraday_liquidation"] == "not_modeled"
    assert result.assumptions["margin_mark_price"] == (
        "close_price_not_exchange_settlement"
    )
    assert result.result_contract is not None
    assert result.result_contract.status is ResultStatus.SUCCESS
    assert result.result_contract is engine.last_result_contract
    assert result.result_contract.data_coverage.expected_observations == 1
    assert result.result_contract.data_coverage.actual_observations == 1
    contract_assumptions = {
        item.key: item.value for item in result.result_contract.assumptions
    }
    assert contract_assumptions["data_coverage.unit"] == (
        "futures_trading_day"
    )
    payload = result.to_dict()
    assert payload["engine_scope"] == result.engine_scope
    assert payload["assumptions"] == result.assumptions


def test_iso_and_compact_signal_dates_produce_identical_results():
    prices = {
        D1: _bar(1_000.0, 1_000.0),
        D2: _bar(1_000.0, 990.0, low=985.0, high=1_005.0),
    }
    compact = _engine(prices).run(_signals("LONG", "FLAT"))
    iso_signals = pd.DataFrame({
        "trade_date": ["2024-01-02", "2024-01-03"],
        "direction": ["LONG", "FLAT"],
    })

    iso = _engine(prices).run(iso_signals)

    pd.testing.assert_frame_equal(iso.daily_nav, compact.daily_nav)
    pd.testing.assert_frame_equal(iso.trades, compact.trades)


def test_zero_signal_calendar_overlap_fails_fast():
    engine = _engine({D1: _bar(1_000.0, 1_000.0)})
    signals = pd.DataFrame({
        "trade_date": ["2024-01-03"],
        "direction": ["LONG"],
    })

    with pytest.raises(ValueError, match="zero overlap"):
        engine.run(signals)
    assert engine.last_result_contract is None


def test_successful_engine_results_can_be_combined_without_contract_inference():
    prices = {
        D1: _bar(1_000.0, 1_000.0),
        D2: _bar(1_000.0, 1_000.0),
    }
    signals = _signals("FLAT", "FLAT")
    first = _engine(prices).run(signals)
    second = _engine(prices).run(signals)

    combined = FuturesCombiner.combine(
        [first, second], ["first", "second"]
    )

    assert combined.result_contract is not None
    assert combined.result_contract.status is ResultStatus.SUCCESS
    assert combined.result_contract.data_coverage.actual_observations == 2


def test_partial_daily_signal_coverage_raises_by_default():
    engine = _engine({
        D1: _bar(1_000.0, 1_000.0),
        D2: _bar(1_000.0, 1_000.0),
    })
    sparse = pd.DataFrame({"trade_date": [D1], "direction": ["LONG"]})

    with pytest.raises(ValueError, match="missing signals"):
        engine.run(sparse)


@pytest.mark.parametrize("policy", ["event", "ffill"])
def test_sparse_signals_require_and_record_explicit_policy(policy: str):
    prices = {
        # D1 LONG passes open margin but fails at close and is liquidated.
        D1: _bar(1_000.0, 980.0, low=970.0, high=1_005.0),
        # On D2, ffill retries LONG at a lower margin; event emits no new order.
        D2: _bar(900.0, 900.0, low=895.0, high=905.0),
    }
    sparse = pd.DataFrame({"trade_date": [D1], "direction": ["LONG"]})

    result = _engine(prices).run(sparse, policy=policy)

    assert result.assumptions["signal_policy"] == policy
    expected_trades = 1 if policy == "event" else 2
    assert result.trade_count == expected_trades


@pytest.mark.parametrize("policy", ["event", "ffill"])
def test_sparse_policy_seeds_target_from_last_pre_window_signal(policy: str):
    full_prices = {
        D1: _bar(1_000.0, 1_000.0),
        D2: _bar(1_000.0, 1_000.0),
        D3: _bar(1_000.0, 1_000.0),
    }
    calendar = (D1, D2, D3)
    history = pd.DataFrame({
        "trade_date": [D1],
        "direction": ["LONG"],
    })

    full = _engine(
        full_prices, start=D1, end=D3, trading_calendar=calendar
    ).run(history, policy=policy)
    window = _engine(
        {D2: full_prices[D2], D3: full_prices[D3]},
        start=D2,
        end=D3,
        trading_calendar=calendar,
    ).run(history, policy=policy)

    full_state = full.daily_nav.set_index("date").loc[
        [D2, D3], ["position", "contract"]
    ]
    window_state = window.daily_nav.set_index("date")[["position", "contract"]]
    pd.testing.assert_frame_equal(window_state, full_state)
    assert window.trades.iloc[0]["entry_date"] == D2
    assert window.trades.iloc[0]["direction"] == "LONG"
    assert window.assumptions["sparse_signal_window_seed"] == (
        "last_pre_window_signal_or_flat"
    )


def test_independent_calendar_detects_a_truncated_market_data_tail():
    engine = _engine(
        {
            D1: _bar(1_000.0, 1_000.0),
            D2: _bar(1_000.0, 1_000.0),
        },
        start=D1,
        end=D3,
        trading_calendar=(D1, D2, D3),
    )
    signals = pd.DataFrame({
        "trade_date": [D1, D2, D3],
        "direction": ["FLAT", "FLAT", "FLAT"],
    })

    with pytest.raises(ValueError, match="coverage mismatch"):
        engine.run(signals)

    contract = engine.last_result_contract
    assert contract is not None
    assert contract.status is ResultStatus.FAILED
    assert contract.data_coverage.expected_observations == 3
    assert contract.data_coverage.actual_observations == 2
    assert contract.actual_interval.start_date == "2024-01-02"
    assert contract.actual_interval.end_date == "2024-01-03"


def test_truncated_calendar_cannot_self_certify_requested_tail():
    engine = _engine(
        {D1: _bar(1_000.0, 1_000.0)},
        start=D1,
        end=D3,
        trading_calendar=(D1,),
    )

    with pytest.raises(ValueError, match="calendar coverage does not cover"):
        engine.run(_signals("FLAT"))

    assert engine.last_result_contract is None
