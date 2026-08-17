"""Acceptance tests for the public onboarding command surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from diepi import cli
from diepi.artifacts import ArtifactStore, EngineKind
from diepi.backtest.comparison import RunParityStatus, compare_cash_runs
from diepi.demo import DEMO_END_DATE, DEMO_START_DATE, DEMO_SYMBOL


def test_root_help_presents_the_complete_user_journey(capsys):
    assert cli.dispatch([]) == cli.EXIT_OK

    output = capsys.readouterr().out
    for command in (
        "diepi doctor",
        "diepi data validate",
        "diepi data extract",
        "diepi combo validate",
        "diepi demo",
        "diepi examples",
        "diepi run",
        "diepi compare runs",
        "diepi gui",
    ):
        assert command in output
    assert "不下载行情" in output


@pytest.mark.parametrize(
    "arguments",
    (
        ("doctor", "--help"),
        ("data", "validate", "--help"),
        ("data", "extract", "--help"),
        ("combo", "validate", "--help"),
        ("demo", "--help"),
        ("examples", "--help"),
        ("run", "--help"),
        ("compare", "runs", "--help"),
        ("gui", "--help"),
    ),
)
def test_each_public_command_has_local_help(arguments):
    with pytest.raises(SystemExit) as stopped:
        cli.dispatch(arguments)

    assert stopped.value.code == 0


def test_legacy_strategy_form_is_only_a_run_alias(monkeypatch):
    observed = {}

    def fake_execute(arguments, *, prog="diepi run"):
        observed["arguments"] = tuple(arguments)
        observed["prog"] = prog
        return 17

    monkeypatch.setattr(cli, "_execute_run", fake_execute)

    assert cli.dispatch(("strategy.py", "--quiet")) == 17
    assert observed == {
        "arguments": ("strategy.py", "--quiet"),
        "prog": "diepi",
    }


def test_run_uses_explicit_data_and_results_roots(tmp_path, monkeypatch):
    strategy = tmp_path / "strategy.py"
    strategy.write_text("def on_day(ctx, bars):\n    pass\n", encoding="utf-8")
    data_root = tmp_path / "market data"
    data_root.mkdir()
    results_root = tmp_path / "results"
    captured = {}

    def fake_run_backtest(**kwargs):
        captured.update(kwargs)
        return {"rankable": True, "result_contract": {"status": "SUCCESS"}}

    monkeypatch.setattr(cli, "run_backtest", fake_run_backtest)

    exit_code = cli.dispatch(
        (
            "run",
            str(strategy),
            "--data-root",
            str(data_root),
            "--results-root",
            str(results_root),
            "--symbols",
            DEMO_SYMBOL,
            "--daily-open-previous-day-ratio",
            "0.1",
        )
    )

    assert exit_code == cli.EXIT_OK
    assert captured["data_root"] == data_root.resolve()
    assert captured["output_dir"] == results_root.resolve()


def test_doctor_reports_a_missing_explicit_root_without_writing(tmp_path, capsys):
    missing_data = tmp_path / "missing data"
    missing_results = tmp_path / "missing results"

    exit_code = cli.dispatch(
        (
            "doctor",
            "--data-root",
            str(missing_data),
            "--results-root",
            str(missing_results),
            "--no-gui-check",
            "--json",
        )
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == cli.EXIT_VALIDATION
    assert payload["status"] == "fail"
    assert not missing_data.exists()
    assert not missing_results.exists()


def test_demo_command_generates_validates_and_runs_synthetic_data(tmp_path, capsys):
    workspace = tmp_path / "含 空格 synthetic demo"

    exit_code = cli.dispatch(("demo", str(workspace), "--json"))
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == cli.EXIT_OK
    assert payload["synthetic"] is True
    assert payload["dataset_kind"] == "synthetic_demo"
    assert payload["validation_status"] == "pass"
    assert payload["run"]["artifact_verified"] is True
    assert Path(payload["run"]["artifact_dir"]) == (
        workspace / "results" / "synthetic_demo"
    ).resolve()
    assert payload["run"]["rankable"] is True
    assert payload["run"]["result_contract"]["status"] == "SUCCESS"
    assert (workspace / "market-data" / "diepi_dataset.json").is_file()
    assert (workspace / "validation.json").is_file()
    run_dir = workspace / "results" / "synthetic_demo"
    expected_members = {
        "manifest.json",
        "config.json",
        "provenance.json",
        "result.json",
        "inputs/strategy.py",
        "tables/daily_values.json",
        "tables/trades.json",
        "tables/positions.json",
        "evidence/target_execution.json",
        "evidence/cash_replay_seed.json",
        "evidence/execution_event_journal.json",
        "evidence/comparisons.json",
        "strategy.py",
        "summary.json",
        "equity_curve.csv",
        "orders.csv",
    }
    actual_members = {
        path.relative_to(run_dir).as_posix()
        for path in run_dir.rglob("*")
        if path.is_file()
    }
    assert actual_members == expected_members

    loaded = ArtifactStore.load(run_dir)
    assert loaded.artifact_verified is True
    assert loaded.is_rankable is True
    assert loaded.outcome.engine_kind is EngineKind.CASH_PORTFOLIO
    assert loaded.manifest.run_id == "synthetic_demo"
    assert loaded.read_bytes("strategy_source") == (
        workspace / "strategy.py"
    ).read_bytes()
    assert {
        role: descriptor.path
        for role, descriptor in loaded.manifest.artifacts.items()
        if role.startswith("compat_")
    } == {
        "compat_equity_curve": "equity_curve.csv",
        "compat_orders": "orders.csv",
        "compat_strategy_source": "strategy.py",
        "compat_summary": "summary.json",
    }
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["artifact_verified"] is True
    assert "invented data only" in captured.err


def test_public_demo_gui_command_uses_the_generated_market_data_directory():
    project_root = Path(__file__).resolve().parents[2]
    expected = (
        "diepi gui --data-root diepi_demo/market-data "
        "--results-root diepi_demo/results"
    )

    for relative_path in ("README.md", "docs/product/03-user-guide.md"):
        text = (project_root / relative_path).read_text(encoding="utf-8")
        assert expected in text
        assert "diepi gui --data-root diepi_demo/data" not in text


def test_compare_runs_accepts_a_verified_artifact_without_mutating_it(
    tmp_path, capsys
):
    workspace = tmp_path / "comparison demo"
    assert cli.dispatch(("demo", str(workspace), "--json")) == cli.EXIT_OK
    demo_payload = json.loads(capsys.readouterr().out)
    artifact = Path(demo_payload["run"]["artifact_dir"])
    before = {
        path.relative_to(artifact): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in artifact.rglob("*")
        if path.is_file()
    }

    exit_code = cli.dispatch(
        ("compare", "runs", str(artifact), str(artifact), "--json")
    )
    envelope = json.loads(capsys.readouterr().out)
    comparison = envelope["command_report"]
    after = {
        path.relative_to(artifact): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in artifact.rglob("*")
        if path.is_file()
    }

    assert exit_code == cli.EXIT_OK
    assert comparison["status"] == "EXACT"
    assert comparison["comparison"]["ledger"]["status"] == "EXACT"
    assert comparison["artifact_trust"] == {
        "baseline_rankable": True,
        "baseline_verified": True,
        "candidate_rankable": True,
        "candidate_verified": True,
        "legacy_explicitly_allowed": False,
        "trusted_comparison": True,
    }
    assert comparison["portable_certification"] is False
    assert comparison["verification_scope"] == "local_command_execution"
    assert envelope["report_path"] is None
    assert after == before


def test_parity_recaptures_verified_artifacts_instead_of_trusting_mutable_cache(
    tmp_path, capsys
):
    workspace = tmp_path / "comparison cache trust"
    assert cli.dispatch(("demo", str(workspace), "--json")) == cli.EXIT_OK
    demo_payload = json.loads(capsys.readouterr().out)
    artifact = Path(demo_payload["run"]["artifact_dir"])
    loaded = ArtifactStore.load(artifact)
    loaded.result.daily_values.iloc[-1, loaded.result.daily_values.columns.get_loc(
        "total_value"
    )] += 1_000_000.0

    report = compare_cash_runs(loaded, ArtifactStore.load(artifact))

    assert report.status is RunParityStatus.UNATTESTED
    assert report.projection_status is RunParityStatus.EXACT
    assert report.assurance_status == "PORTABLE_DIAGNOSTIC_ONLY"


def test_compare_report_cannot_be_written_inside_a_verified_run(tmp_path, capsys):
    workspace = tmp_path / "immutable comparison demo"
    assert cli.dispatch(("demo", str(workspace), "--json")) == cli.EXIT_OK
    demo_payload = json.loads(capsys.readouterr().out)
    artifact = Path(demo_payload["run"]["artifact_dir"])
    forbidden = artifact / "parity.json"

    exit_code = cli.dispatch(
        (
            "compare",
            "runs",
            str(artifact),
            str(artifact),
            "--report",
            str(forbidden),
        )
    )

    assert exit_code == cli.EXIT_USAGE
    assert "must not modify the baseline run directory" in capsys.readouterr().err
    assert not forbidden.exists()
    assert ArtifactStore.verify(artifact).run_id == artifact.name


def test_data_validate_command_is_read_only_by_default(tmp_path, capsys):
    workspace = tmp_path / "generated only"
    assert cli.dispatch(("demo", str(workspace), "--generate-only")) == 0
    capsys.readouterr()
    before = {
        path.relative_to(workspace): path.stat().st_mtime_ns
        for path in workspace.rglob("*")
        if path.is_file()
    }

    exit_code = cli.dispatch(
        (
            "data",
            "validate",
            "--data-root",
            str(workspace / "market-data"),
            "--symbols",
            DEMO_SYMBOL,
            "--start",
            DEMO_START_DATE,
            "--end",
            DEMO_END_DATE,
            "--json",
        )
    )
    payload = json.loads(capsys.readouterr().out)
    after = {
        path.relative_to(workspace): path.stat().st_mtime_ns
        for path in workspace.rglob("*")
        if path.is_file()
    }

    assert exit_code == cli.EXIT_OK
    assert payload["contract_ready"] is True
    assert payload["manifest_status"] == "verified"
    assert after == before


def test_pypi_readme_has_no_unresolvable_relative_markdown_links():
    readme = Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text(encoding="utf-8")

    assert "](" not in text
    assert "docs/product/03-user-guide.md" in text
    assert "https://github.com/elonmaskhair-prog/diepi" in text
