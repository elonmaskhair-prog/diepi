"""Run-to-run parity is separate from benchmark comparison."""

from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from diepi.artifacts import ArtifactStore, EngineKind, RunOutcome
from diepi.backtest.comparison import (
    RunParityPolicy,
    RunParityStatus,
    capture_cash_run,
    compare_cash_runs,
)
from diepi.commands import run_compare
from tests.backtest.test_run_artifacts import _portfolio_result


def _trade(**overrides):
    row = {
        "event_sequence": 1,
        "order_id": "volatile-order-id",
        "symbol": "510300.SH",
        "direction": "BUY",
        "shares": 100,
        "price": 4.0,
        "amount": 400.0,
        "time": "20240102",
        "simulated_time": "2024-01-02T09:30:00.000000",
        "fee": 5.0,
        "commission": 5.0,
        "stamp_duty": 0.0,
        "transfer_fee": 0.0,
        "cash_delta": -405.0,
        "realized_pnl": None,
        "t0": False,
        "order_type": "open",
        "note": "",
        "requested_shares": 100,
        "requested_amount": None,
        "requested_percent": None,
        "auto_resized": False,
        "resized_from_shares": None,
        "resized_to_shares": None,
        "resize_reason": "",
    }
    row.update(overrides)
    return row


def _result(
    *,
    second_total=1_010.0,
    dates=("20240102", "20240103"),
    origin_cash=1_000.0,
):
    frame = pd.DataFrame(
        {
            "cash": [600.0, 600.0],
            "market_value": [400.0, second_total - 600.0],
            "total_value": [1_000.0, second_total],
        },
        index=pd.to_datetime(dates, format="%Y%m%d"),
    )
    frame.index.name = "date"
    terminal_state = SimpleNamespace(
        cash=600.0,
        frozen_cash=0.0,
        last_sequence=4,
        orders=(),
        positions=(),
        reservations=(),
    )
    return SimpleNamespace(
        initial_cash=1_000.0,
        final_value=second_total,
        total_return=second_total / 1_000.0 - 1.0,
        annual_return=0.1,
        max_drawdown=0.0,
        trade_count=1,
        win_rate=None,
        sharpe_ratio=1.0,
        result_contract=SimpleNamespace(
            assumptions=(
                SimpleNamespace(key="cash.trade_count_unit", value="fill"),
                SimpleNamespace(
                    key="cash.win_rate_unit", value="closed_inventory_round"
                ),
                SimpleNamespace(
                    key="metrics.intraday_high_to_low",
                    value="pessimistic_same_day_upper_bound",
                ),
                SimpleNamespace(
                    key="metrics.intraday_low_path",
                    value="causal_prior_close_peak_to_intraday_low",
                ),
                SimpleNamespace(key="metrics.return_std_ddof", value="1"),
                SimpleNamespace(key="metrics.risk_free_rate", value="0.03"),
                SimpleNamespace(key="metrics.schema_version", value="1"),
                SimpleNamespace(
                    key="metrics.trading_days_per_year", value="252"
                ),
            )
        ),
        cash_audit=SimpleNamespace(
            journal_json=(
                '{"events":[],"schema":"diepi.execution_event_journal",'
                '"schema_version":1}'
            ),
            seed=SimpleNamespace(
                to_dict=lambda: {
                    "account_id": "cash-account",
                    "as_of": "2024-01-01T00:00:00.000000",
                    "initial_cash": origin_cash,
                    "initial_frozen_cash": 0.0,
                    "open_orders": [],
                    "positions": [],
                    "reservations": [],
                    "schema": "diepi.cash_replay_seed",
                    "schema_version": 1,
                }
            ),
            replay=lambda: SimpleNamespace(state=terminal_state),
        ),
        daily_values=frame,
        trades=[_trade()],
    )


def test_identical_economics_and_semantic_trades_are_exact():
    baseline = _result()
    candidate = deepcopy(baseline)
    candidate.trades[0]["order_id"] = "different-internal-id"

    first = compare_cash_runs(baseline, candidate)
    second = compare_cash_runs(baseline, candidate)

    assert first.status is RunParityStatus.UNATTESTED
    assert first.projection_status is RunParityStatus.EXACT
    assert not first.exact
    assert not first.within_tolerance
    assert first.report_sha256 == second.report_sha256
    assert first.baseline.snapshot_sha256 == first.candidate.snapshot_sha256
    assert "different-internal-id" not in first.to_json()
    assert "Projection status: `EXACT`" in first.to_markdown()


def test_small_float_difference_is_distinct_from_exact_parity():
    baseline = _result()
    candidate = _result(second_total=1_010.005)
    policy = RunParityPolicy(absolute_tolerance=0.01)

    report = compare_cash_runs(baseline, candidate, policy=policy)

    assert report.status is RunParityStatus.UNATTESTED
    assert report.projection_status is RunParityStatus.WITHIN_TOLERANCE
    assert not report.exact
    assert not report.within_tolerance
    assert report.projection_within_tolerance
    total_value = next(
        value for value in report.daily_summaries if value.field == "total_value"
    )
    assert total_value.different_values == 1
    assert total_value.outside_tolerance_values == 0
    assert total_value.first_difference.key == "20240103"


def test_material_difference_reports_first_observation_and_values():
    report = compare_cash_runs(
        _result(),
        _result(second_total=1_010.02),
        policy=RunParityPolicy(absolute_tolerance=0.01),
    )

    assert report.status is RunParityStatus.UNATTESTED
    assert report.projection_status is RunParityStatus.DIFFERENT
    total_value = next(
        value for value in report.daily_summaries if value.field == "total_value"
    )
    first = total_value.first_outside_tolerance
    assert first.key == "20240103"
    assert first.baseline == 1_010.0
    assert first.candidate == 1_010.02
    assert first.absolute_difference == pytest.approx(0.02)


def test_trade_difference_key_identifies_semantic_execution_coordinate():
    baseline = _result()
    candidate = deepcopy(baseline)
    candidate.trades[0]["shares"] = 200
    candidate.trades[0]["amount"] = 800.0

    report = compare_cash_runs(baseline, candidate)
    shares = next(
        value for value in report.trade_summaries if value.field == "shares"
    )

    assert shares.first_difference.key == (
        "1|2024-01-02T09:30:00.000000|510300.SH|BUY|1"
    )


def test_scope_mismatch_is_not_silently_intersected():
    baseline = _result()
    candidate = _result(dates=("20240102", "20240104"))

    report = compare_cash_runs(baseline, candidate)

    assert report.status is RunParityStatus.UNATTESTED
    assert report.projection_status is RunParityStatus.NOT_COMPARABLE
    assert not report.comparable
    assert report.baseline_only_observations == ("20240103",)
    assert report.candidate_only_observations == ("20240104",)
    assert report.daily_summaries == ()
    assert "not intersected" in report.reasons[0]


def test_trade_count_is_part_of_parity_even_when_common_rows_match():
    baseline = _result()
    candidate = _result()
    candidate.trades.append(
        _trade(
            event_sequence=2,
            time="20240103",
            simulated_time="2024-01-03T15:00:00.000000",
            direction="SELL",
        )
    )
    candidate.trade_count = 2

    report = compare_cash_runs(baseline, candidate)

    assert report.status is RunParityStatus.UNATTESTED
    assert report.projection_status is RunParityStatus.DIFFERENT
    assert not report.trade_count_equal
    assert not report.within_tolerance


def test_full_trade_projection_rejects_reordered_causal_records():
    baseline = _result()
    baseline.trades.append(
        _trade(
            event_sequence=2,
            time="20240102",
            simulated_time="2024-01-02T15:00:00.000000",
            direction="SELL",
            price=4.1,
            amount=410.0,
        )
    )
    baseline.trade_count = 2
    candidate = deepcopy(baseline)
    candidate.trades.reverse()
    candidate.trades[0]["order_id"] = "another-volatile-id"

    with pytest.raises(ValueError, match="event_sequence order"):
        compare_cash_runs(baseline, candidate)


def test_loaded_run_shape_is_unwrapped_and_metric_definitions_are_separate():
    baseline = _result()
    candidate = deepcopy(baseline)
    candidate.result_contract.assumptions[-1].value = "245"
    loaded_baseline = SimpleNamespace(result=baseline)
    loaded_candidate = SimpleNamespace(result=candidate)

    report = compare_cash_runs(loaded_baseline, loaded_candidate)

    assert report.ledger_status is RunParityStatus.EXACT
    assert report.metric_definition_status is RunParityStatus.DIFFERENT
    assert report.status is RunParityStatus.UNATTESTED
    assert report.projection_status is RunParityStatus.DIFFERENT


def test_verified_artifact_identity_survives_outer_directory_rename(tmp_path):
    outcome = RunOutcome.from_result(
        _portfolio_result(), engine_kind=EngineKind.CASH_PORTFOLIO
    )
    original = ArtifactStore.save(
        outcome, tmp_path / "manifest-run-id", config={}
    )
    renamed = tmp_path / "portable-copy"
    original.rename(renamed)

    snapshot = capture_cash_run(ArtifactStore.load(renamed), label="renamed")

    assert snapshot.input_artifact_verified is True
    assert snapshot.input_run_id == "manifest-run-id"
    assert snapshot.input_manifest_sha256 is not None


def test_fee_components_and_cash_delta_are_economic_trade_fields():
    baseline = _result()
    candidate = deepcopy(baseline)
    candidate.trades[0]["commission"] = 4.0
    candidate.trades[0]["stamp_duty"] = 1.0

    report = compare_cash_runs(baseline, candidate)

    assert report.ledger_status is RunParityStatus.DIFFERENT
    commission = next(
        value for value in report.trade_summaries if value.field == "commission"
    )
    assert commission.first_difference.key == (
        "1|2024-01-02T09:30:00.000000|510300.SH|BUY|1"
    )


def test_bool_is_not_equal_to_integer_in_semantic_trade_fields():
    baseline = _result()
    candidate = deepcopy(baseline)
    candidate.trades[0]["auto_resized"] = 1

    with pytest.raises(TypeError, match="auto_resized must be exactly bool"):
        compare_cash_runs(baseline, candidate)


def test_large_integer_share_difference_cannot_disappear_in_float_conversion():
    baseline = _result()
    candidate = deepcopy(baseline)
    baseline.trades[0]["shares"] = 2**60
    candidate.trades[0]["shares"] = 2**60 + 1

    report = compare_cash_runs(baseline, candidate)
    shares = next(
        value for value in report.trade_summaries if value.field == "shares"
    )

    assert report.ledger_status is RunParityStatus.DIFFERENT
    assert shares.first_difference.absolute_difference == 1.0
    assert shares.outside_tolerance_values == 1


def test_cash_metric_units_are_part_of_metric_definition():
    baseline = _result()
    candidate = deepcopy(baseline)
    candidate.result_contract.assumptions[1].value = "fill"

    report = compare_cash_runs(baseline, candidate)

    assert report.ledger_status is RunParityStatus.EXACT
    assert report.metric_definition_status is RunParityStatus.DIFFERENT
    assert report.status is RunParityStatus.UNATTESTED
    assert report.projection_status is RunParityStatus.DIFFERENT


def test_partial_metric_definition_is_not_comparable():
    baseline = _result()
    candidate = deepcopy(baseline)
    baseline.result_contract.assumptions = baseline.result_contract.assumptions[:-1]

    report = compare_cash_runs(baseline, candidate)

    assert report.ledger_status is RunParityStatus.EXACT
    assert report.metric_definition_status is RunParityStatus.NOT_COMPARABLE


def test_full_profile_cannot_silently_drop_required_dimensions():
    with pytest.raises(ValueError, match="fixed daily projection"):
        RunParityPolicy(daily_fields=("cash",))
    with pytest.raises(ValueError, match="fixed metric projection"):
        RunParityPolicy(metric_fields=("initial_cash",))
    with pytest.raises(ValueError, match="fixed trade projection"):
        RunParityPolicy(trade_fields=("symbol",))


def test_terminal_state_difference_prevents_false_ledger_parity():
    baseline = _result()
    candidate = deepcopy(baseline)
    candidate.cash_audit.replay = lambda: SimpleNamespace(
        state=SimpleNamespace(
            cash=601.0,
            frozen_cash=0.0,
            last_sequence=4,
            orders=(),
            positions=(),
            reservations=(),
        )
    )

    report = compare_cash_runs(baseline, candidate)

    assert report.post_seed_projection_status is RunParityStatus.EXACT
    assert report.ledger_status is RunParityStatus.DIFFERENT
    assert not report.terminal_states_equal


def test_cash_event_path_difference_prevents_false_ledger_parity():
    baseline = _result()
    candidate = deepcopy(baseline)
    candidate.cash_audit.journal_json = (
        '{"events":[{"amount":200}],'
        '"schema":"diepi.execution_event_journal","schema_version":1}'
    )

    report = compare_cash_runs(baseline, candidate)

    assert report.post_seed_projection_status is RunParityStatus.EXACT
    assert report.ledger_status is RunParityStatus.DIFFERENT
    assert not report.event_journals_equal


def test_signed_zero_is_canonicalized_in_snapshot_identity():
    baseline = _result()
    candidate = deepcopy(baseline)
    baseline.daily_values.iloc[0, baseline.daily_values.columns.get_loc("cash")] = 0.0
    candidate.daily_values.iloc[0, candidate.daily_values.columns.get_loc("cash")] = -0.0

    report = compare_cash_runs(baseline, candidate)

    assert report.baseline.snapshot_sha256 == report.candidate.snapshot_sha256
    cash = next(value for value in report.daily_summaries if value.field == "cash")
    assert cash.exact


def test_legacy_results_can_have_exact_ledger_without_full_metric_parity():
    baseline = _result()
    candidate = deepcopy(baseline)
    baseline.result_contract = None
    candidate.result_contract = None

    report = compare_cash_runs(baseline, candidate)

    assert report.ledger_status is RunParityStatus.EXACT
    assert report.metric_definition_status is RunParityStatus.NOT_COMPARABLE
    assert report.status is RunParityStatus.UNATTESTED
    assert report.projection_status is RunParityStatus.NOT_COMPARABLE


def test_report_write_refuses_overwrite_and_supports_both_formats(tmp_path):
    report = compare_cash_runs(_result(), _result(), atol=1e-9, rtol=1e-12)

    json_path = report.write(tmp_path / "parity.json")
    markdown_path = report.write(tmp_path / "parity.md")

    assert '"schema": "diepi.run_parity_report"' in json_path.read_text(
        encoding="utf-8"
    )
    assert "Ledger status: `EXACT`" in markdown_path.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        report.write(json_path)


def test_capture_rejects_incomplete_trade_projection():
    result = _result()
    del result.trades[0]["price"]

    with pytest.raises(ValueError, match="missing parity fields"):
        capture_cash_run(result, label="incomplete")


def test_unverified_legacy_command_cannot_report_overall_success(
    tmp_path, monkeypatch
):
    baseline_root = tmp_path / "legacy-baseline"
    candidate_root = tmp_path / "legacy-candidate"
    baseline_root.mkdir()
    candidate_root.mkdir()
    loaded = {
        str(baseline_root): SimpleNamespace(
            root=baseline_root, result=_result(), is_rankable=False
        ),
        str(candidate_root): SimpleNamespace(
            root=candidate_root, result=_result(), is_rankable=False
        ),
    }

    def fake_load(path, *, allow_unverified_legacy):
        assert allow_unverified_legacy is True
        return loaded[str(Path(path))], False

    monkeypatch.setattr(run_compare, "_load", fake_load)
    report_path = tmp_path / "legacy-comparison.json"
    stdout = StringIO()
    stderr = StringIO()
    args = SimpleNamespace(
        baseline=str(baseline_root),
        candidate=str(candidate_root),
        baseline_label="legacy-baseline",
        candidate_label="legacy-candidate",
        atol=0.0,
        rtol=0.0,
        report=str(report_path),
        overwrite=False,
        allow_unverified_legacy=True,
        json=True,
    )

    exit_code = run_compare.execute(args, stdout=stdout, stderr=stderr)
    envelope = json.loads(stdout.getvalue())
    payload = envelope["command_report"]
    persisted = json.loads(report_path.read_text(encoding="utf-8"))

    assert exit_code == run_compare.EXIT_DIFFERENT
    assert payload["status"] == "UNVERIFIED"
    assert payload["comparison"]["ledger"]["status"] == "NOT_COMPARABLE"
    assert (
        payload["comparison"]["ledger"]["post_seed_projection_status"]
        == "EXACT"
    )
    assert payload["artifact_trust"] == {
        "baseline_rankable": False,
        "baseline_verified": False,
        "candidate_rankable": False,
        "candidate_verified": False,
        "legacy_explicitly_allowed": True,
        "trusted_comparison": False,
    }
    assert persisted["status"] == "UNVERIFIED"
    assert persisted["artifact_trust"] == payload["artifact_trust"]
    assert envelope["report_path"] == str(report_path.resolve())
    assert stderr.getvalue() == ""

    markdown_path = tmp_path / "legacy-comparison.md"
    args.report = str(markdown_path)
    args.json = False
    assert (
        run_compare.execute(args, stdout=StringIO(), stderr=StringIO())
        == run_compare.EXIT_DIFFERENT
    )
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Overall status: `UNVERIFIED`" in markdown
    assert "Trusted comparison: `false`" in markdown


def test_duck_typed_nonrankable_object_cannot_claim_verified_artifact_trust(
    tmp_path, monkeypatch
):
    roots = (tmp_path / "partial-a", tmp_path / "partial-b")
    for root in roots:
        root.mkdir()
    loaded = {
        str(root): SimpleNamespace(root=root, result=_result(), is_rankable=False)
        for root in roots
    }

    monkeypatch.setattr(
        run_compare,
        "_load",
        lambda path, *, allow_unverified_legacy: (loaded[str(Path(path))], True),
    )
    stdout = StringIO()
    args = SimpleNamespace(
        baseline=str(roots[0]),
        candidate=str(roots[1]),
        baseline_label="partial-a",
        candidate_label="partial-b",
        atol=0.0,
        rtol=0.0,
        report=None,
        overwrite=False,
        allow_unverified_legacy=False,
        json=True,
    )

    exit_code = run_compare.execute(args, stdout=stdout, stderr=StringIO())
    envelope = json.loads(stdout.getvalue())
    payload = envelope["command_report"]

    assert exit_code == run_compare.EXIT_DIFFERENT
    assert payload["status"] == "UNVERIFIED"
    assert payload["comparison"]["ledger"]["status"] == "EXACT"
    assert payload["artifact_trust"]["trusted_comparison"] is False
    assert payload["artifact_trust"]["baseline_verified"] is False
    assert payload["artifact_trust"]["candidate_verified"] is False
    assert envelope["report_path"] is None
