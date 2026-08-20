"""Link, containment, and size boundaries for local contract files."""

import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pandas as pd
import pytest

from diepi.backtest.data.cache_manager import (
    CacheConfig,
    CacheManager,
    is_supported_direct_parquet_file,
)
from diepi.backtest.data.dataset_manifest import DatasetFileIdentity, DatasetManifest
from diepi.backtest.data.exceptions import DataNotFoundError
from diepi.backtest.data.plain_files import DATASET_MANIFEST_MAX_BYTES
from diepi.backtest.data.validation_service import (
    DataValidationScope,
    _validate_calendar,
)
from diepi.commands.doctor import run_doctor
import diepi.backtest.data.plain_files as plain_files


def _write_calendar(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "cal_date": ["20240101", "20240102"],
            "is_open": [0, 1],
        }
    ).to_parquet(path, index=False)


def test_dataset_manifest_rejects_an_oversized_plain_file(tmp_path):
    manifest = tmp_path / "diepi_dataset.json"
    with manifest.open("wb") as stream:
        stream.seek(DATASET_MANIFEST_MAX_BYTES)
        stream.write(b"x")

    with pytest.raises(ValueError, match="safety limit"):
        DatasetManifest.read(manifest, root=tmp_path)


def test_dataset_manifest_rejects_a_file_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "diepi_dataset.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable on this platform")

    with pytest.raises(ValueError, match="link or reparse"):
        DatasetManifest.read(link, root=tmp_path)


def test_dataset_manifest_rejects_a_hard_link(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "diepi_dataset.json"
    try:
        os.link(target, link)
    except OSError:
        pytest.skip("hard links are unavailable on this platform")

    with pytest.raises(ValueError, match="single-link regular file"):
        DatasetManifest.read(link, root=tmp_path)


def test_direct_market_source_rejects_a_hard_link(tmp_path):
    target = tmp_path / "outside.parquet"
    target.write_bytes(b"parquet-placeholder")
    link = tmp_path / "data" / "parquet" / "timeseries" / "daily_raw" / "A.parquet"
    link.parent.mkdir(parents=True)
    try:
        os.link(target, link)
    except OSError:
        pytest.skip("hard links are unavailable on this platform")

    assert not is_supported_direct_parquet_file(link, root=tmp_path / "data")


def test_direct_market_source_rejects_an_intermediate_reparse_point(
    tmp_path, monkeypatch
):
    root = tmp_path / "data"
    source = root / "parquet" / "timeseries" / "daily_raw" / "A.parquet"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"parquet-placeholder")
    real_lstat = Path.lstat
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)

    def simulated_reparse(path):
        info = real_lstat(path)
        if path == root / "parquet":
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_file_attributes=(
                    getattr(info, "st_file_attributes", 0) | reparse_flag
                ),
                st_nlink=getattr(info, "st_nlink", 1),
            )
        return info

    monkeypatch.setattr(Path, "lstat", simulated_reparse)
    assert not is_supported_direct_parquet_file(source, root=root)


def test_read_rechecks_intermediate_directory_identity(tmp_path, monkeypatch):
    member = tmp_path / "metadata" / "member.json"
    member.parent.mkdir()
    member.write_bytes(b"{}")
    real_lstat = plain_files._lstat
    directory_reads = 0

    def replaced_directory(path, *, label):
        nonlocal directory_reads
        info = real_lstat(path, label=label)
        if path == member.parent:
            directory_reads += 1
            if directory_reads == 2:
                return SimpleNamespace(
                    st_mode=info.st_mode,
                    st_dev=info.st_dev,
                    st_ino=info.st_ino + 1,
                    st_ctime_ns=info.st_ctime_ns,
                    st_file_attributes=getattr(info, "st_file_attributes", 0),
                )
        return info

    monkeypatch.setattr(plain_files, "_lstat", replaced_directory)
    with pytest.raises(ValueError, match="parent changed"):
        plain_files.read_plain_bytes(
            member,
            root=tmp_path,
            max_bytes=1024,
            label="test member",
        )


def test_parquet_decode_error_does_not_echo_attacker_text(tmp_path):
    marker = "ATTACKER-CONTROLLED\x00\x1b[31m".encode("utf-8")
    source = tmp_path / "invalid.parquet"
    source.write_bytes(marker)

    with pytest.raises(ValueError) as captured:
        plain_files.read_plain_parquet(
            source,
            root=tmp_path,
            max_bytes=1024,
            label="metadata",
        )
    assert "ATTACKER-CONTROLLED" not in str(captured.value)
    assert "\x1b" not in str(captured.value)


def test_manifest_path_error_does_not_echo_control_text():
    marker = "ATTACKER-CONTROLLED\n\x1b[31m.parquet"
    with pytest.raises(ValueError) as captured:
        DatasetFileIdentity(
            path=marker,
            rows=0,
            columns=("trade_date",),
            logical_sha256="0" * 64,
        )
    assert "ATTACKER-CONTROLLED" not in str(captured.value)
    assert "\x1b" not in str(captured.value)


@pytest.mark.parametrize(
    "path",
    (
        "".join(("C", ":/outside.parquet")),
        "CON.parquet",
        "a.parquet.",
        "folder /member.parquet",
        "file.parquet:secret",
        "a?.parquet",
        "a*.parquet",
        "a<.parquet",
        "a>.parquet",
        'a".parquet',
        "a|.parquet",
    ),
)
def test_manifest_member_paths_obey_the_windows_portable_contract(path):
    with pytest.raises(ValueError, match="portable safety contract"):
        DatasetFileIdentity(
            path=path,
            rows=0,
            columns=("trade_date",),
            logical_sha256="0" * 64,
        )


def test_manifest_member_paths_are_casefold_unique():
    def identity(path):
        return DatasetFileIdentity(
            path=path,
            rows=0,
            columns=("trade_date",),
            logical_sha256="0" * 64,
        )

    with pytest.raises(ValueError, match="portable-case unique"):
        DatasetManifest(
            dataset_kind="user_supplied",
            dataset_id="case-collision",
            symbols=("600000.SH",),
            start_date="20240101",
            end_date="20240102",
            generator="test",
            generator_version="1",
            files=(identity("A.parquet"), identity("a.parquet")),
        )


def test_calendar_override_symlink_never_falls_back_to_bundled(tmp_path):
    metadata_root = tmp_path / "parquet" / "metadata"
    target = tmp_path / "outside-calendar.parquet"
    _write_calendar(target)
    link = metadata_root / "common" / "trade_cal.parquet"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable on this platform")

    manager = CacheManager(
        config=CacheConfig(
            PARQUET_ROOT=tmp_path / "parquet" / "timeseries",
            METADATA_ROOT=metadata_root,
        )
    )
    with pytest.raises(DataNotFoundError, match="strict validation"):
        manager.get_trade_cal()


def test_calendar_override_rejects_an_intermediate_directory_link(tmp_path):
    data_root = tmp_path / "data"
    outside_metadata = tmp_path / "outside" / "metadata"
    _write_calendar(outside_metadata / "common" / "trade_cal.parquet")
    (data_root / "parquet").mkdir(parents=True)
    metadata_link = data_root / "parquet" / "metadata"
    try:
        metadata_link.symlink_to(outside_metadata, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this platform")

    manager = CacheManager(data_root=data_root)
    with pytest.raises(DataNotFoundError, match="strict validation"):
        manager.get_trade_cal()

    scope = DataValidationScope(
        symbols=("510300.SH",),
        start_date="20240101",
        end_date="20240102",
        price_mode="raw",
    )
    report, _open_days, issues = _validate_calendar(
        metadata_link / "common" / "trade_cal.parquet",
        scope,
        data_root=data_root,
    )
    assert report["status"] == "fail"
    assert {issue.code for issue in issues} == {"TRADE_CALENDAR_READ_ERROR"}


def test_calendar_override_rejects_an_intermediate_reparse_point(
    tmp_path, monkeypatch
):
    data_root = tmp_path / "data"
    calendar = data_root / "parquet" / "metadata" / "common" / "trade_cal.parquet"
    _write_calendar(calendar)
    real_lstat = plain_files._lstat
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)

    def simulated_reparse(path, *, label):
        info = real_lstat(path, label=label)
        if path == data_root / "parquet":
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_dev=info.st_dev,
                st_ino=info.st_ino,
                st_ctime_ns=info.st_ctime_ns,
                st_file_attributes=(
                    getattr(info, "st_file_attributes", 0) | reparse_flag
                ),
            )
        return info

    monkeypatch.setattr(plain_files, "_lstat", simulated_reparse)
    manager = CacheManager(data_root=data_root)
    with pytest.raises(DataNotFoundError, match="strict validation"):
        manager.get_trade_cal()

    scope = DataValidationScope(
        symbols=("510300.SH",),
        start_date="20240101",
        end_date="20240102",
        price_mode="raw",
    )
    report, _open_days, issues = _validate_calendar(
        calendar,
        scope,
        data_root=data_root,
    )
    assert report["status"] == "fail"
    assert {issue.code for issue in issues} == {"TRADE_CALENDAR_READ_ERROR"}


def test_missing_calendar_below_an_invalid_parent_never_falls_back(
    tmp_path, monkeypatch
):
    data_root = tmp_path / "data"
    metadata_root = data_root / "parquet" / "metadata"
    (metadata_root / "common").mkdir(parents=True)
    calendar = metadata_root / "common" / "trade_cal.parquet"
    assert not calendar.exists()
    real_lstat = plain_files._lstat
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)

    def simulated_reparse(path, *, label):
        info = real_lstat(path, label=label)
        if path == metadata_root:
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_dev=info.st_dev,
                st_ino=info.st_ino,
                st_ctime_ns=info.st_ctime_ns,
                st_file_attributes=(
                    getattr(info, "st_file_attributes", 0) | reparse_flag
                ),
            )
        return info

    monkeypatch.setattr(plain_files, "_lstat", simulated_reparse)
    manager = CacheManager(data_root=data_root)
    with pytest.raises(DataNotFoundError, match="strict validation"):
        manager.get_trade_cal()

    scope = DataValidationScope(
        symbols=("510300.SH",),
        start_date="20240101",
        end_date="20240102",
        price_mode="raw",
    )
    report, _open_days, issues = _validate_calendar(
        calendar,
        scope,
        data_root=data_root,
    )
    assert report["source"] == "local_override"
    assert report["status"] == "fail"
    assert {issue.code for issue in issues} == {"TRADE_CALENDAR_READ_ERROR"}

    doctor = run_doctor(
        data_root=data_root,
        results_root=tmp_path / "results",
        check_gui=False,
    )
    calendar_check = next(
        check for check in doctor.checks if check.name == "trade_calendar"
    )
    assert calendar_check.status == "fail"


def test_custom_metadata_mapping_cannot_escape_its_root(tmp_path):
    metadata_root = tmp_path / "metadata"
    metadata_root.mkdir()
    outside = tmp_path / "outside.parquet"
    pd.DataFrame({"ts_code": ["600000.SH"]}).to_parquet(outside, index=False)
    manager = CacheManager(
        config=CacheConfig(
            PARQUET_ROOT=tmp_path / "timeseries",
            METADATA_ROOT=metadata_root,
            METADATA_PARQUET={"trade_cal": "../outside.parquet"},
        )
    )

    with pytest.raises(DataNotFoundError, match="strict validation"):
        manager.get_trade_cal()


def test_broken_calendar_symlink_is_detected_as_an_invalid_override(tmp_path):
    metadata_root = tmp_path / "metadata"
    link = metadata_root / "common" / "trade_cal.parquet"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(tmp_path / "missing.parquet")
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks are unavailable on this platform")
    assert os.path.lexists(link)

    manager = CacheManager(
        config=CacheConfig(
            PARQUET_ROOT=tmp_path / "timeseries",
            METADATA_ROOT=metadata_root,
        )
    )
    with pytest.raises(DataNotFoundError, match="strict validation"):
        manager.get_trade_cal()
