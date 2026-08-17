"""Deterministic tests for runtime paths, doctor, validation, and demo data."""

from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from diepi.backtest.data.data_provider import DataProvider
from diepi.backtest.data.dataset_manifest import logical_frame_sha256
from diepi.backtest.data.validation_service import (
    DataValidationScope,
    _validate_instrument_basic,
    validate_local_data,
)
from diepi.commands import data_validate, demo as demo_command, doctor
from diepi.demo import (
    DEMO_END_DATE,
    DEMO_START_DATE,
    DEMO_SYMBOL,
    generate_synthetic_demo,
)
from diepi.runtime import RuntimePaths


def test_runtime_paths_use_explicit_values_before_environment(tmp_path):
    explicit_data = tmp_path / "explicit data"
    environment_data = tmp_path / "environment data"
    explicit_results = tmp_path / "explicit results"
    explicit_data.mkdir()
    environment_data.mkdir()

    paths = RuntimePaths.resolve(
        data_root=explicit_data,
        results_root=explicit_results,
        environ={
            "DATA_ROOT": str(environment_data),
            "DIEPI_RESULTS_DIR": str(tmp_path / "environment results"),
        },
    )

    assert paths.data_root == explicit_data.resolve()
    assert paths.results_root == explicit_results.resolve()
    assert paths.data_root_source == "explicit"
    assert paths.results_root_source == "explicit"


def test_explicit_provider_root_isolated_from_bad_environment(tmp_path, monkeypatch):
    generated = generate_synthetic_demo(tmp_path / "provider demo")
    monkeypatch.setenv("DATA_ROOT", str(tmp_path / "does not exist"))

    provider = DataProvider(
        data_root=generated.data_root,
        price_mode="hfq",
        execution_price_mode="raw",
    )
    aligned = provider.get_aligned_pair(
        DEMO_SYMBOL,
        frequency="daily",
        start=DEMO_START_DATE,
        end=DEMO_END_DATE,
    )

    assert provider.data_root == generated.data_root
    assert aligned.report.ok
    assert aligned.report.aligned_rows == 34


def test_synthetic_demo_is_deterministic_manifested_and_self_validating(tmp_path):
    first = generate_synthetic_demo(tmp_path / "含 空格 demo one")
    second = generate_synthetic_demo(tmp_path / "demo two")

    assert first.validation_report.contract_ready
    assert first.validation_report.manifest_status == "verified"
    assert first.manifest.manifest_sha256 == second.manifest.manifest_sha256
    assert (
        first.validation_report.report_sha256
        == second.validation_report.report_sha256
    )
    assert first.strategy_file.is_file()
    assert first.validation_file.is_file()
    basic = pd.read_parquet(
        first.data_root / "parquet" / "metadata" / "stock" / "basic.parquet"
    )
    assert basic.loc[0, "name"] == "SYNTHETIC_DEMO_NOT_REAL"


def test_synthetic_demo_never_overwrites_existing_workspace(tmp_path):
    workspace = tmp_path / "existing"
    workspace.mkdir()
    marker = workspace / "owned.txt"
    marker.write_text("user-owned", encoding="utf-8")

    with pytest.raises(FileExistsError):
        generate_synthetic_demo(workspace)

    assert marker.read_text(encoding="utf-8") == "user-owned"


def test_validation_detects_manifest_and_contract_corruption(tmp_path):
    generated = generate_synthetic_demo(tmp_path / "corruption demo")
    raw_path = (
        generated.data_root
        / "parquet"
        / "timeseries"
        / "daily_raw"
        / f"{DEMO_SYMBOL}.parquet"
    )
    raw = pd.read_parquet(raw_path).drop(columns=["amount"])
    raw.to_parquet(raw_path, index=False)

    report = validate_local_data(
        data_root=generated.data_root,
        symbols=(DEMO_SYMBOL,),
        start_date=DEMO_START_DATE,
        end_date=DEMO_END_DATE,
    )

    issue_codes = {issue.code for issue in report.issues}
    pair_codes = {
        code for pair in report.pair_reports for code in pair.issue_codes
    }
    assert not report.contract_ready
    assert "DATASET_MANIFEST_IDENTITY_MISMATCH" in issue_codes
    assert "MARKET_PAIR_CONTRACT_FAILED" in issue_codes
    assert "MISSING_COLUMN" in pair_codes


def test_minute_validation_requires_companion_daily_pair(tmp_path):
    generated = generate_synthetic_demo(tmp_path / "minute companion")
    for lane in ("daily", "daily_raw"):
        (
            generated.data_root
            / "parquet"
            / "timeseries"
            / lane
            / f"{DEMO_SYMBOL}.parquet"
        ).unlink()

    report = validate_local_data(
        data_root=generated.data_root,
        symbols=(DEMO_SYMBOL,),
        start_date=DEMO_START_DATE,
        end_date=DEMO_END_DATE,
        frequency="minute",
    )

    assert not report.contract_ready
    assert "MINUTE_COMPANION_DAILY_REQUIRED" in {
        issue.code for issue in report.errors
    }


def test_missing_data_root_returns_deterministic_failure_report(tmp_path):
    missing = tmp_path / "missing"
    first = validate_local_data(
        data_root=missing,
        symbols=(DEMO_SYMBOL,),
        start_date=DEMO_START_DATE,
        end_date=DEMO_END_DATE,
    )
    second = validate_local_data(
        data_root=missing,
        symbols=(DEMO_SYMBOL,),
        start_date=DEMO_START_DATE,
        end_date=DEMO_END_DATE,
    )

    assert first.status == "fail"
    assert first.report_sha256 == second.report_sha256
    assert [issue.code for issue in first.errors] == ["DATA_ROOT_MISSING"]


def test_validation_command_is_read_only_without_explicit_report(tmp_path):
    generated = generate_synthetic_demo(tmp_path / "validate command")
    before = {
        path.relative_to(generated.workspace): path.stat().st_mtime_ns
        for path in generated.workspace.rglob("*")
        if path.is_file()
    }
    stdout = StringIO()
    stderr = StringIO()
    args = SimpleNamespace(
        data_root=str(generated.data_root),
        symbols=[DEMO_SYMBOL],
        start=DEMO_START_DATE,
        end=DEMO_END_DATE,
        frequency="daily",
        price_mode="dual",
        skip_manifest=False,
        report=None,
        json=True,
    )

    exit_code = data_validate.execute(args, stdout=stdout, stderr=stderr)
    after = {
        path.relative_to(generated.workspace): path.stat().st_mtime_ns
        for path in generated.workspace.rglob("*")
        if path.is_file()
    }

    assert exit_code == 0
    assert '"contract_ready": true' in stdout.getvalue()
    assert stderr.getvalue() == ""
    assert after == before


def test_doctor_and_demo_command_adapters_have_stable_exit_codes(
    tmp_path, monkeypatch
):
    demo_stdout = StringIO()
    demo_stderr = StringIO()
    workspace = tmp_path / "command demo"
    demo_exit = demo_command.execute(
        SimpleNamespace(workspace=str(workspace), json=True, generate_only=True),
        stdout=demo_stdout,
        stderr=demo_stderr,
    )
    assert demo_exit == 0
    assert '"synthetic": true' in demo_stdout.getvalue()
    assert demo_stderr.getvalue() == ""

    declared_versions = {
        "numpy": "1.23",
        "pandas": "1.5",
        "pyarrow": "23.0.1",
    }
    monkeypatch.setattr(
        doctor.metadata,
        "version",
        lambda name: declared_versions[name],
    )
    report = doctor.run_doctor(
        data_root=workspace / "market-data",
        results_root=tmp_path / "new results",
        check_gui=False,
    )
    assert report.ok
    assert report.status == "pass"


def test_doctor_rejects_an_importable_dependency_below_declared_floor(monkeypatch):
    monkeypatch.setattr(doctor.util, "find_spec", lambda name: object())
    monkeypatch.setattr(doctor.metadata, "version", lambda name: "22.0.0")

    check = doctor._distribution_check(
        "pyarrow", "pyarrow", "23.0.1", None, optional=False
    )

    assert check.status == "fail"
    assert check.value == "22.0.0"
    assert ">=23.0.1" in check.message


def test_doctor_warns_when_data_root_is_not_configured(tmp_path, monkeypatch):
    fallback = doctor.RuntimePaths(
        data_root=tmp_path,
        results_root=tmp_path / "results",
        data_root_source="cwd",
        results_root_source="cwd",
    )
    monkeypatch.setattr(
        doctor.RuntimePaths,
        "resolve",
        classmethod(lambda cls, **kwargs: fallback),
    )
    declared_versions = {
        "numpy": "1.23",
        "pandas": "1.5",
        "pyarrow": "23.0.1",
    }
    monkeypatch.setattr(
        doctor.metadata,
        "version",
        lambda name: declared_versions[name],
    )

    report = doctor.run_doctor(check_gui=False)
    data_check = next(item for item in report.checks if item.name == "data_root")

    assert report.ok
    assert report.status == "warn"
    assert data_check.status == "warn"
    assert data_check.message.startswith("NOT_CONFIGURED:")
    assert not any(item.name == "timeseries_root" for item in report.checks)


def test_logical_hash_ignores_column_storage_order():
    frame = pd.DataFrame({"b": [2.5, 3.5], "a": ["x", "y"]})
    assert logical_frame_sha256(frame) == logical_frame_sha256(frame[["a", "b"]])


def test_etf_scope_uses_etf_basic_metadata_instead_of_stock_basic(tmp_path):
    metadata = tmp_path / "parquet" / "metadata" / "etf"
    metadata.mkdir(parents=True)
    pd.DataFrame(
        {
            "ts_code": ["510300.SH"],
            "list_date": ["20120528"],
            "list_status": ["L"],
        }
    ).to_parquet(metadata / "basic.parquet", index=False)
    scope = DataValidationScope(
        symbols=("510300.SH",),
        start_date="20240102",
        end_date="20240103",
    )

    assert _validate_instrument_basic(tmp_path, scope) == []


def test_missing_etf_metadata_has_family_specific_warning(tmp_path):
    scope = DataValidationScope(
        symbols=("510300.SH",),
        start_date="20240102",
        end_date="20240103",
    )

    issues = _validate_instrument_basic(tmp_path, scope)

    assert [issue.code for issue in issues] == ["ETF_BASIC_NOT_CHECKED"]
    assert "etf/basic.parquet" in issues[0].message


def test_mixed_scope_validates_stock_and_etf_security_masters(tmp_path):
    stock = tmp_path / "parquet" / "metadata" / "stock"
    etf = tmp_path / "parquet" / "metadata" / "etf"
    stock.mkdir(parents=True)
    etf.mkdir(parents=True)
    pd.DataFrame(
        {
            "ts_code": ["600000.SH"],
            "list_date": ["19991110"],
            "delist_date": [""],
        }
    ).to_parquet(stock / "basic.parquet", index=False)
    pd.DataFrame(
        {"ts_code": ["510300.SH"], "list_date": ["20120528"]}
    ).to_parquet(etf / "basic.parquet", index=False)
    scope = DataValidationScope(
        symbols=("600000.SH", "510300.SH"),
        start_date="20240102",
        end_date="20240103",
    )

    assert _validate_instrument_basic(tmp_path, scope) == []
