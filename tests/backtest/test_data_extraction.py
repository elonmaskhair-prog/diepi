"""Contracts for the private, local-only data extraction workflow."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
import stat

import pandas as pd
import pytest

from diepi import cli
from diepi.backtest.data.dataset_manifest import DatasetManifest
from diepi.backtest.data.extraction_service import extract_local_data
from diepi.backtest.data import extraction_service
from diepi.backtest.data.validation_service import validate_local_data
from diepi.demo import DEMO_SYMBOL, generate_synthetic_demo


START = "20240108"
END = "20240119"
PREVIOUS_OPEN = "20240105"
ETF_SYMBOL = "510300.SH"


def _file_bytes(root: Path):
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_extract_is_read_only_deterministic_scoped_and_private(tmp_path):
    source_workspace = generate_synthetic_demo(tmp_path / "source demo")
    source = source_workspace.data_root
    private_signals = source / "signals" / "private.csv"
    private_signals.parent.mkdir()
    private_signals.write_text(
        "date,symbol,target_weight\n20240108,000001.SZ,1\n",
        encoding="utf-8",
    )
    before = _file_bytes(source)

    first = extract_local_data(
        source_data_root=source,
        workspace=tmp_path / "first extract",
        symbols=(DEMO_SYMBOL,),
        start_date=START,
        end_date=END,
        include_metadata=True,
    )
    second = extract_local_data(
        source_data_root=source,
        workspace=tmp_path / "second extract",
        symbols=(DEMO_SYMBOL,),
        start_date=START,
        end_date=END,
        include_metadata=True,
    )

    assert _file_bytes(source) == before
    assert first.validation_report.contract_ready
    assert first.validation_report.manifest_status == "verified"
    assert first.manifest.dataset_kind == "user_supplied"
    assert first.manifest.manifest_sha256 == second.manifest.manifest_sha256
    assert first.scope.to_json() == second.scope.to_json()

    # The extractor relies on diePi's bundled market clock.  It neither
    # requires nor republishes the source workspace's optional override.
    assert not (
        first.data_root / "parquet/metadata/common/trade_cal.parquet"
    ).exists()
    for lane in ("daily", "daily_raw"):
        bars = pd.read_parquet(
            first.data_root
            / "parquet/timeseries"
            / lane
            / f"{DEMO_SYMBOL}.parquet"
        )
        assert bars["trade_date"].iloc[0] == PREVIOUS_OPEN
        assert bars["trade_date"].iloc[-1] == END

    factors = pd.read_parquet(
        first.data_root
        / "parquet/timeseries/adj_factor"
        / f"{DEMO_SYMBOL}.parquet"
    )
    assert factors["trade_date"].iloc[0] == "20240101"
    assert PREVIOUS_OPEN in set(factors["trade_date"])
    assert factors["trade_date"].iloc[-1] == END

    scope_text = first.scope_file.read_text(encoding="utf-8")
    scope = json.loads(scope_text)
    assert str(source) not in scope_text
    assert scope["data_origin"] == "user-supplied"
    assert scope["privacy"] == "private"
    assert scope["redistribution_policy"] == (
        "non-redistributable-by-default"
    )
    assert scope["redistribution_authorized"] is False
    assert scope["source_location_recorded"] is False
    assert scope["whole_source_hash_recorded"] is False
    assert scope["strategy_signals_included"] is False
    assert scope["natural_day_calendar_included"] is False
    assert scope["calendar_source"] == "bundled"
    assert scope["calendar_id"] == "cn-a-share-2010-2026-v1"
    assert scope["calendar_version"] == "1"
    assert scope["calendar_content_sha256"] == (
        "882bb722fd15cbe4ceddb2370422c30fead8c6e81f842300221996ea3cb44a59"
    )
    assert not (first.workspace / "signals").exists()
    assert not any(
        "signal" in path.name.lower()
        for path in first.workspace.rglob("*")
    )

    manifest = DatasetManifest.read(first.manifest_file)
    assert all("signal" not in identity.path for identity in manifest.files)
    assert all("trade_cal" not in identity.path for identity in manifest.files)
    report = validate_local_data(
        data_root=first.data_root,
        symbols=(DEMO_SYMBOL,),
        start_date=START,
        end_date=END,
    )
    assert report.contract_ready
    assert report.manifest_status == "verified"


def test_extract_auto_routes_etf_and_filters_etf_metadata(tmp_path):
    generated = generate_synthetic_demo(tmp_path / "ETF source fixture")
    source = generated.data_root
    timeseries = source / "parquet/timeseries"
    source_lanes = {
        "etf_daily": "daily",
        "etf_daily_raw": "daily_raw",
        "etf_adj_factor": "adj_factor",
    }
    for destination_lane, source_lane in source_lanes.items():
        frame = pd.read_parquet(
            timeseries / source_lane / f"{DEMO_SYMBOL}.parquet"
        )
        frame.loc[:, "ts_code"] = ETF_SYMBOL
        destination = timeseries / destination_lane
        destination.mkdir()
        frame.to_parquet(destination / f"{ETF_SYMBOL}.parquet", index=False)
    metadata = source / "parquet/metadata/etf"
    metadata.mkdir()
    pd.DataFrame(
        {
            "ts_code": [ETF_SYMBOL, "510500.SH"],
            "name": ["selected", "must-not-leak"],
            "list_date": ["20120528", "20130206"],
        }
    ).to_parquet(metadata / "basic.parquet", index=False)

    extracted = extract_local_data(
        source_data_root=source,
        workspace=tmp_path / "ETF extract",
        symbols=(ETF_SYMBOL,),
        start_date=START,
        end_date=END,
        include_metadata=True,
    )

    for lane in source_lanes:
        assert (
            extracted.data_root
            / "parquet/timeseries"
            / lane
            / f"{ETF_SYMBOL}.parquet"
        ).is_file()
    assert not (
        extracted.data_root
        / "parquet/timeseries/daily"
        / f"{ETF_SYMBOL}.parquet"
    ).exists()
    basic = pd.read_parquet(
        extracted.data_root / "parquet/metadata/etf/basic.parquet"
    )
    assert basic["ts_code"].tolist() == [ETF_SYMBOL]
    assert extracted.validation_report.contract_ready
    assert not extracted.validation_report.warnings


def test_extract_basic_metadata_requires_an_instrument_identity(tmp_path):
    generated = generate_synthetic_demo(tmp_path / "missing identity source")
    basic = (
        generated.data_root
        / "parquet"
        / "metadata"
        / "stock"
        / "basic.parquet"
    )
    frame = pd.read_parquet(basic).drop(columns=["ts_code", "symbol"])
    frame.to_parquet(basic, index=False)
    workspace = tmp_path / "must not publish"

    with pytest.raises(ValueError, match="must contain ts_code or symbol"):
        extract_local_data(
            source_data_root=generated.data_root,
            workspace=workspace,
            symbols=(DEMO_SYMBOL,),
            start_date=START,
            end_date=END,
            include_metadata=True,
        )

    assert not workspace.exists()


def test_extract_basic_metadata_rejects_conflicting_identity_columns(tmp_path):
    generated = generate_synthetic_demo(tmp_path / "conflicting identity source")
    basic = (
        generated.data_root
        / "parquet"
        / "metadata"
        / "stock"
        / "basic.parquet"
    )
    frame = pd.read_parquet(basic)
    frame.loc[:, "symbol"] = "600000"
    frame.to_parquet(basic, index=False)

    with pytest.raises(ValueError, match="outside the requested scope"):
        extract_local_data(
            source_data_root=generated.data_root,
            workspace=tmp_path / "conflict must not publish",
            symbols=(DEMO_SYMBOL,),
            start_date=START,
            end_date=END,
            include_metadata=True,
        )


@pytest.mark.parametrize(
    "relative,include_metadata",
    [
        (f"parquet/timeseries/daily/{DEMO_SYMBOL}.parquet", False),
        (f"parquet/timeseries/daily_raw/{DEMO_SYMBOL}.parquet", False),
        (f"parquet/timeseries/adj_factor/{DEMO_SYMBOL}.parquet", False),
        ("parquet/metadata/stock/basic.parquet", True),
    ],
)
def test_extract_rejects_embedded_strategy_columns(
    tmp_path, relative, include_metadata
):
    generated = generate_synthetic_demo(
        tmp_path / relative.replace("/", "-").replace(".", "-")
    )
    source = generated.data_root / Path(relative)
    frame = pd.read_parquet(source)
    frame.loc[:, "target_weight"] = 1.0
    frame.to_parquet(source, index=False)
    workspace = tmp_path / ("rejected-" + source.parent.name)

    with pytest.raises(ValueError, match="unsupported columns"):
        extract_local_data(
            source_data_root=generated.data_root,
            workspace=workspace,
            symbols=(DEMO_SYMBOL,),
            start_date=START,
            end_date=END,
            include_metadata=include_metadata,
        )

    assert not workspace.exists()


def test_extract_strips_opaque_parquet_dataframe_attributes(tmp_path):
    generated = generate_synthetic_demo(tmp_path / "attribute source")
    source = (
        generated.data_root
        / "parquet"
        / "timeseries"
        / "daily"
        / f"{DEMO_SYMBOL}.parquet"
    )
    frame = pd.read_parquet(source)
    secret = "PRIVATE_STRATEGY_SIGNAL_MUST_NOT_LEAVE_SOURCE"
    frame.attrs["signal"] = secret
    frame.attrs["target_weight"] = {DEMO_SYMBOL: 1.0}
    frame.to_parquet(source, index=False)

    extracted = extract_local_data(
        source_data_root=generated.data_root,
        workspace=tmp_path / "sanitized attributes",
        symbols=(DEMO_SYMBOL,),
        start_date=START,
        end_date=END,
    )
    output = (
        extracted.data_root
        / "parquet"
        / "timeseries"
        / "daily"
        / f"{DEMO_SYMBOL}.parquet"
    )

    assert pd.read_parquet(output).attrs == {}
    assert secret.encode("utf-8") not in output.read_bytes()


def test_extract_refuses_overwrite_unsafe_destination_and_cleans_failure(tmp_path):
    generated = generate_synthetic_demo(tmp_path / "protected source")
    source = generated.data_root
    existing = tmp_path / "existing extract"
    existing.mkdir()
    marker = existing / "owned.txt"
    marker.write_text("user-owned", encoding="utf-8")

    with pytest.raises(FileExistsError):
        extract_local_data(
            source_data_root=source,
            workspace=existing,
            symbols=(DEMO_SYMBOL,),
            start_date=START,
            end_date=END,
        )
    assert marker.read_text(encoding="utf-8") == "user-owned"

    inside_source = source / "parquet" / "must-not-be-created"
    with pytest.raises(ValueError, match="read-only source Parquet"):
        extract_local_data(
            source_data_root=source,
            workspace=inside_source,
            symbols=(DEMO_SYMBOL,),
            start_date=START,
            end_date=END,
        )
    assert not inside_source.exists()

    failure_parent = tmp_path / "failure parent"
    failure_parent.mkdir()
    failed_target = failure_parent / "failed"
    with pytest.raises(FileNotFoundError):
        extract_local_data(
            source_data_root=source,
            workspace=failed_target,
            symbols=("600000.SH",),
            start_date=START,
            end_date=END,
        )
    assert not failed_target.exists()
    assert not list(failure_parent.glob(".diepi-extract-*"))

    with pytest.raises(ValueError, match="canonical six-digit"):
        extract_local_data(
            source_data_root=source,
            workspace=tmp_path / "unsafe symbol",
            symbols=("../000001.SZ",),
            start_date=START,
            end_date=END,
        )

    with pytest.raises(ValueError, match="dot segments"):
        extract_local_data(
            source_data_root=source,
            workspace=tmp_path / "not-used" / ".." / "ambiguous target",
            symbols=(DEMO_SYMBOL,),
            start_date=START,
            end_date=END,
        )


def test_staging_cleanup_refuses_replaced_directory_and_reparse_identity(tmp_path):
    parent = tmp_path / "staging parent"
    parent.mkdir()
    staging = parent / ".diepi-extract-owned"
    staging.mkdir()
    identity = extraction_service._capture_staging_identity(staging)

    original = parent / "original staging"
    staging.rename(original)
    staging.mkdir()
    with pytest.raises(RuntimeError, match="identity changed"):
        extraction_service._safe_cleanup_staging(staging, parent, identity)
    assert staging.is_dir()
    assert original.is_dir()

    reparse_info = SimpleNamespace(
        st_mode=stat.S_IFDIR,
        st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
    )
    assert extraction_service._is_link_or_reparse(reparse_info)


def test_extract_refuses_target_that_appears_during_publication(
    tmp_path, monkeypatch
):
    generated = generate_synthetic_demo(tmp_path / "publication race source")
    workspace = tmp_path / "late target"
    marker = workspace / "owned.txt"
    original_validate = extraction_service.validate_local_data

    def validate_then_create_target(**kwargs):
        report = original_validate(**kwargs)
        workspace.mkdir()
        marker.write_text("concurrent owner", encoding="utf-8")
        return report

    monkeypatch.setattr(
        extraction_service, "validate_local_data", validate_then_create_target
    )

    with pytest.raises(FileExistsError, match="appeared during publication"):
        extract_local_data(
            source_data_root=generated.data_root,
            workspace=workspace,
            symbols=(DEMO_SYMBOL,),
            start_date=START,
            end_date=END,
        )

    assert marker.read_text(encoding="utf-8") == "concurrent owner"
    assert not list(tmp_path.glob(".diepi-extract-*"))


def test_atomic_publish_never_replaces_an_existing_target(tmp_path):
    staging = tmp_path / ".diepi-extract-direct-test"
    staging.mkdir()
    (staging / "staged.txt").write_text("staged", encoding="utf-8")
    target = tmp_path / "owned target"
    target.mkdir()
    marker = target / "owned.txt"
    marker.write_text("owner", encoding="utf-8")

    with pytest.raises(FileExistsError):
        extraction_service._publish_no_replace(staging, target)

    assert marker.read_text(encoding="utf-8") == "owner"
    assert (staging / "staged.txt").read_text(encoding="utf-8") == "staged"


def test_extract_cleans_staging_after_keyboard_interrupt(tmp_path, monkeypatch):
    generated = generate_synthetic_demo(tmp_path / "interrupt source")

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(extraction_service, "_calendar_slice", interrupt)
    with pytest.raises(KeyboardInterrupt):
        extract_local_data(
            source_data_root=generated.data_root,
            workspace=tmp_path / "interrupted target",
            symbols=(DEMO_SYMBOL,),
            start_date=START,
            end_date=END,
        )

    assert not list(tmp_path.glob(".diepi-extract-*"))


def test_extract_detects_source_file_changed_while_reading(tmp_path, monkeypatch):
    generated = generate_synthetic_demo(tmp_path / "changing source")
    original_read = extraction_service.pd.read_parquet
    changed = False

    def read_then_change(path, *args, **kwargs):
        nonlocal changed
        frame = original_read(path, *args, **kwargs)
        if not changed:
            changed = True
            with Path(path).open("ab") as handle:
                handle.write(b"changed-after-read")
        return frame

    monkeypatch.setattr(extraction_service.pd, "read_parquet", read_then_change)
    with pytest.raises(RuntimeError, match="changed during extraction"):
        extract_local_data(
            source_data_root=generated.data_root,
            workspace=tmp_path / "changed target",
            symbols=(DEMO_SYMBOL,),
            start_date=START,
            end_date=END,
        )

    assert not list(tmp_path.glob(".diepi-extract-*"))


def test_extract_rejects_dangling_workspace_link(tmp_path):
    generated = generate_synthetic_demo(tmp_path / "link source")
    target = tmp_path / "dangling target"
    try:
        target.symlink_to(tmp_path / "missing target", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="link or reparse point"):
        extract_local_data(
            source_data_root=generated.data_root,
            workspace=target,
            symbols=(DEMO_SYMBOL,),
            start_date=START,
            end_date=END,
        )

    assert os.path.lexists(target)


def test_extract_rejects_workspace_reparse_leaf(tmp_path, monkeypatch):
    generated = generate_synthetic_demo(tmp_path / "reparse source")
    target = tmp_path / "reparse target"
    target.mkdir()
    original_lstat = Path.lstat

    def reparse_lstat(path):
        info = original_lstat(path)
        if path == target:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=getattr(
                    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
                ),
            )
        return info

    monkeypatch.setattr(Path, "lstat", reparse_lstat)
    with pytest.raises(ValueError, match="link or reparse point"):
        extract_local_data(
            source_data_root=generated.data_root,
            workspace=target,
            symbols=(DEMO_SYMBOL,),
            start_date=START,
            end_date=END,
        )


@pytest.mark.skipif(os.name == "nt", reason="Windows chmod has no POSIX mode bits")
def test_extract_outputs_use_private_posix_modes(tmp_path):
    generated = generate_synthetic_demo(tmp_path / "permission source")
    extracted = extract_local_data(
        source_data_root=generated.data_root,
        workspace=tmp_path / "private target",
        symbols=(DEMO_SYMBOL,),
        start_date=START,
        end_date=END,
    )

    for path in (extracted.workspace, *extracted.workspace.rglob("*")):
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected


def test_data_extract_cli_redacts_local_paths_by_default(tmp_path, capsys):
    secret_source = tmp_path / "sensitive owner name" / "missing source"
    exit_code = cli.dispatch(
        (
            "data",
            "extract",
            "--source-data-root",
            str(secret_source),
            "--workspace",
            str(tmp_path / "never created"),
            "--symbols",
            DEMO_SYMBOL,
            "--start",
            START,
            "--end",
            END,
        )
    )
    captured = capsys.readouterr()

    assert exit_code == cli.EXIT_USAGE
    assert "SOURCE_OR_PARENT_NOT_FOUND" in captured.err
    assert str(secret_source) not in captured.err

    verbose_code = cli.dispatch(
        (
            "data",
            "extract",
            "--source-data-root",
            str(secret_source),
            "--workspace",
            str(tmp_path / "never created verbose"),
            "--symbols",
            DEMO_SYMBOL,
            "--start",
            START,
            "--end",
            END,
            "--verbose-errors",
        )
    )
    verbose = capsys.readouterr()
    assert verbose_code == cli.EXIT_USAGE
    assert str(secret_source) in verbose.err


def test_data_extract_cli_dispatches_and_metadata_is_opt_in(tmp_path, capsys):
    generated = generate_synthetic_demo(tmp_path / "CLI source")
    workspace = tmp_path / "CLI output"

    exit_code = cli.dispatch(
        (
            "data",
            "extract",
            "--source-data-root",
            str(generated.data_root),
            "--workspace",
            str(workspace),
            "--symbols",
            DEMO_SYMBOL,
            "--start",
            START,
            "--end",
            END,
            "--json",
        )
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert exit_code == cli.EXIT_OK
    assert captured.err == ""
    assert payload["private"] is True
    assert payload["redistribution_authorized"] is False
    assert payload["strategy_signals_included"] is False
    assert payload["storage_start_date"] == PREVIOUS_OPEN
    assert not (workspace / "market-data/parquet/metadata/stock/basic.parquet").exists()
    scope = json.loads(
        (workspace / "extraction_scope.json").read_text(encoding="utf-8")
    )
    assert scope["metadata_included"] is False


def test_data_extract_has_local_help():
    with pytest.raises(SystemExit) as stopped:
        cli.dispatch(("data", "extract", "--help"))
    assert stopped.value.code == 0
