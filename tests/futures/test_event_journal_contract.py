"""Engine-path coverage for the futures margin/roll diagnostic journal."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from diepi.backtest.result_contract import ResultStatus
from diepi.futures.combiner import FuturesCombiner
from diepi.futures.journal import (
    EMPTY_FUTURES_EVENT_JOURNAL_JSON,
    FUTURES_EVENT_JOURNAL_SCOPE,
)
from tests.futures.test_engine_synthetic import (
    D1,
    D2,
    _bar as _single_bar,
    _engine as _single_engine,
    _signals as _single_signals,
)
from tests.futures.test_roll_close_contract import (
    CONTRACT_A,
    CONTRACT_B,
    D0,
    D3,
    _bar,
    _engine,
    _signals,
)


def _events(result) -> list[dict]:
    return json.loads(result.event_journal_json)["events"]


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def test_mapping_roll_and_each_real_close_margin_check_are_engine_events(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        {
            CONTRACT_A: {
                D1: _bar(1000, 1000),
                D2: _bar(1000, 1010),
            },
            CONTRACT_B: {
                D2: _bar(1100, 1100),
                D3: _bar(1100, 1100),
            },
        },
        expiry={CONTRACT_A: "20240216", CONTRACT_B: "20240315"},
        roll_schedule={D1: CONTRACT_A, D2: CONTRACT_B},
        method="mapping",
    )

    result = engine.run(_signals("LONG", "LONG", "FLAT"))
    events = _events(result)

    assert [event["event_type"] for event in events] == [
        "margin", "roll", "margin"
    ]
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert [event["phase_seq"] for event in events] == [20, 10, 20]
    assert [event["simulated_time"] for event in events] == [
        "2024-01-02T15:00:00.000000",
        "2024-01-03T15:00:00.000000",
        "2024-01-03T15:00:00.000000",
    ]
    assert events[1]["event_id"] == (
        "futures-event:IC:20240103:close:roll:ICA.CFX:ICB.CFX"
    )
    assert events[1]["payload"]["roll_id"] == (
        "futures-roll:IC:20240103:ICA.CFX:ICB.CFX"
    )
    assert events[1]["payload"]["attributes"]["reason"] == "contract_roll"
    assert events[2]["payload"]["contract"] == CONTRACT_B
    assert result.assumptions["event_journal_scope"] == (
        FUTURES_EVENT_JOURNAL_SCOPE
    )
    contract_assumptions = {
        item.key: item.value for item in result.result_contract.assumptions
    }
    assert contract_assumptions["event_journal_scope"] == (
        FUTURES_EVENT_JOURNAL_SCOPE
    )
    assert "not_order_fill_or_nav_replay" in (
        result.assumptions["event_journal_completeness"]
    )


def test_expiry_replacement_emits_same_close_roll_before_margin(
    tmp_path: Path,
) -> None:
    engine = _engine(
        tmp_path,
        {
            CONTRACT_A: {
                D0: _bar(1000, 1000, vol=200),
                D1: _bar(1000, 1000, vol=100),
                D2: _bar(1000, 1010, vol=10),
            },
            CONTRACT_B: {
                D0: _bar(1100, 1100, vol=50),
                D1: _bar(1100, 1100, vol=300),
                D2: _bar(1100, 1100, vol=300),
                D3: _bar(1100, 1100, vol=300),
            },
        },
        expiry={CONTRACT_A: D2, CONTRACT_B: "20240315"},
    )

    result = engine.run(_signals("LONG", "LONG", "FLAT"))
    same_close = [
        event for event in _events(result)
        if event["simulated_time"].startswith("2024-01-03")
    ]

    assert [event["event_type"] for event in same_close] == ["roll", "margin"]
    assert same_close[0]["payload"]["attributes"]["reason"] == (
        "contract_expiry"
    )
    assert same_close[0]["payload"]["from_contract"] == CONTRACT_A
    assert same_close[0]["payload"]["to_contract"] == CONTRACT_B


@pytest.mark.parametrize(
    ("account_size", "close", "expected_passed", "exit_reason"),
    [
        (30_000.0, 1_000.0, True, "end_of_backtest"),
        (30_000.0, 980.0, False, "margin_call"),
    ],
)
def test_real_close_margin_outcome_is_recorded(
    account_size, close, expected_passed, exit_reason
) -> None:
    result = _single_engine(
        {D1: _single_bar(1_000.0, close, low=min(970.0, close))},
        account_size=account_size,
    ).run(_single_signals("LONG"))

    events = _events(result)
    assert len(events) == 1
    assert events[0]["event_type"] == "margin"
    assert events[0]["payload"]["attributes"]["check_passed"] is expected_passed
    assert result.trades.iloc[0]["exit_reason"] == exit_reason


def test_flat_days_do_not_emit_fake_margin_events() -> None:
    result = _single_engine({
        D1: _single_bar(1_000.0, 1_000.0),
        D2: _single_bar(1_000.0, 1_000.0),
    }).run(_single_signals("FLAT", "FLAT"))

    assert result.event_journal_json == EMPTY_FUTURES_EVENT_JOURNAL_JSON
    assert _events(result) == []


def test_journal_is_canonical_roundtrippable_and_combiner_uses_no_fake_events(
    tmp_path: Path,
) -> None:
    prices = {D1: _single_bar(1_000.0, 1_000.0)}
    first = _single_engine(prices).run(_single_signals("LONG"))
    second = _single_engine(prices).run(_single_signals("LONG"))

    assert _canonical(json.loads(first.event_journal_json)) == (
        first.event_journal_json
    )
    payload = json.loads(json.dumps(first.to_dict(), ensure_ascii=False))
    assert payload["event_journal_json"] == first.event_journal_json

    combined = FuturesCombiner.combine([first, second], ["first", "second"])
    assert combined.is_valid
    assert combined.event_journal_json == EMPTY_FUTURES_EVENT_JOURNAL_JSON
    assert combined.assumptions["event_journal_aggregation"] == (
        "none_no_synthetic_combined_margin_or_roll_events"
    )


@pytest.mark.parametrize("mutation", ["noncanonical", "sequence", "nan"])
def test_result_validation_rejects_corrupt_journal(mutation: str) -> None:
    result = _single_engine({
        D1: _single_bar(1_000.0, 1_000.0),
    }).run(_single_signals("LONG"))
    decoded = json.loads(result.event_journal_json)
    if mutation == "noncanonical":
        result.event_journal_json = json.dumps(decoded, indent=2)
    elif mutation == "sequence":
        decoded["events"][0]["sequence"] = 2
        result.event_journal_json = _canonical(decoded)
    else:
        decoded["events"][0]["payload"]["attributes"]["nav"] = float("nan")
        result.event_journal_json = json.dumps(
            decoded, ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    assert not result.validate()
    assert any("event_journal_json" in error
               for error in result.validation_errors)


def test_runtime_failure_does_not_publish_partial_journal() -> None:
    class GapSelector:
        calendar = [D1, D2]

        def select(self, trade_date, min_days_remaining=7, method="volume_t1"):
            from diepi.futures.contract import ContractSelection
            return ContractSelection(
                "IC9999.CFX", trade_date, method, "20991231", 99, 1,
                "gap fixture")

        def get_price(self, contract, trade_date, field):
            if trade_date != D1:
                return None
            return _single_bar(1_000.0, 1_000.0).get(field)

        def is_expired_on(self, contract, trade_date):
            return False

        def has_data_on(self, contract, trade_date):
            return trade_date == D1

        def is_valid_on(self, contract, trade_date):
            return self.has_data_on(contract, trade_date)

    engine = _single_engine({D1: _single_bar(1_000.0, 1_000.0)})
    engine._end = D2
    engine._selector = GapSelector()
    engine._trading_calendar = (D1, D2)

    with pytest.raises(ValueError, match="data gap as contract expiry"):
        engine.run(_single_signals("LONG", "LONG"))

    assert engine.last_result_contract is not None
    assert engine.last_result_contract.status is ResultStatus.FAILED
    assert not hasattr(engine, "last_event_journal_json")


def test_repeated_input_and_pythonhashseed_produce_identical_journal() -> None:
    prices = {
        D1: _single_bar(1_000.0, 1_000.0),
        D2: _single_bar(1_000.0, 1_000.0),
    }
    first = _single_engine(prices).run(
        _single_signals("LONG", "FLAT")).event_journal_json
    second = _single_engine(prices).run(
        _single_signals("LONG", "FLAT")).event_journal_json
    assert first == second

    code = (
        "from tests.futures.test_engine_synthetic import "
        "_engine,_bar,_signals,D1,D2;"
        "p={D1:_bar(1000.,1000.),D2:_bar(1000.,1000.)};"
        "print(_engine(p).run(_signals('LONG','FLAT')).event_journal_json)"
    )
    outputs = []
    for seed in ("1", "777"):
        environment = dict(os.environ)
        environment["PYTHONHASHSEED"] = seed
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(completed.stdout.strip())
    assert outputs == [first, first]
