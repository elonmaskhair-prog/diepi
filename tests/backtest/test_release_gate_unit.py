"""Self-contained tests for release artifact and pytest outcome gates."""

from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

import pytest

import tools.build_release as release_builder
import tools.check_release_artifacts as release_checker
from tools.check_markdown_links import MarkdownLinkError, check_markdown_links
from tools.check_release_artifacts import (
    _PUBLIC_TEST_SUPPORT_FILES,
    _is_public_test,
    ReleaseGateError,
    _scan_sensitive_text,
    check_public_filesystem_tree,
    check_public_git_tree,
    load_public_git_allowlist,
    load_wheel_payload_allowlist,
    read_archive,
)
from tools.run_test_gate import TestGateError as OutcomeGateError, inspect_junit


_EXPECTED_PRODUCT_DOCUMENT_FILES = frozenset(
    {
        "docs/product/README.md",
        "docs/product/01-author-note.md",
        "docs/product/02-core-features.md",
        "docs/product/03-user-guide.md",
        "docs/product/04-reference-and-boundaries.md",
        "docs/product/05-local-market-data-format-v1.md",
    }
)
_EXPECTED_COMMUNITY_FILES = frozenset(
    {
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
    }
)
_EXPECTED_MARKET_DATA_V1_FILES = frozenset(
    {
        "examples/market_data_v1/README.md",
        "examples/market_data_v1/generate.py",
        "examples/market_data_v1/data/diepi_dataset.json",
        "examples/market_data_v1/data/parquet/metadata/etf/basic.parquet",
        "examples/market_data_v1/data/parquet/metadata/stock/basic.parquet",
        "examples/market_data_v1/data/parquet/timeseries/adj_factor/000001.SZ.parquet",
        "examples/market_data_v1/data/parquet/timeseries/adj_factor/600000.SH.parquet",
        "examples/market_data_v1/data/parquet/timeseries/daily/000001.SZ.parquet",
        "examples/market_data_v1/data/parquet/timeseries/daily/600000.SH.parquet",
        "examples/market_data_v1/data/parquet/timeseries/daily_raw/000001.SZ.parquet",
        "examples/market_data_v1/data/parquet/timeseries/daily_raw/600000.SH.parquet",
        "examples/market_data_v1/data/parquet/timeseries/etf_adj_factor/159915.SZ.parquet",
        "examples/market_data_v1/data/parquet/timeseries/etf_adj_factor/510300.SH.parquet",
        "examples/market_data_v1/data/parquet/timeseries/etf_daily/159915.SZ.parquet",
        "examples/market_data_v1/data/parquet/timeseries/etf_daily/510300.SH.parquet",
        "examples/market_data_v1/data/parquet/timeseries/etf_daily_raw/159915.SZ.parquet",
        "examples/market_data_v1/data/parquet/timeseries/etf_daily_raw/510300.SH.parquet",
        "examples/market_data_v1/data/parquet/timeseries/etf_minute/159915.SZ/2026.parquet",
        "examples/market_data_v1/data/parquet/timeseries/etf_minute/510300.SH/2026.parquet",
        "examples/market_data_v1/data/parquet/timeseries/etf_minute_raw/159915.SZ/2026.parquet",
        "examples/market_data_v1/data/parquet/timeseries/etf_minute_raw/510300.SH/2026.parquet",
        "examples/market_data_v1/data/parquet/timeseries/minute/000001.SZ/2026.parquet",
        "examples/market_data_v1/data/parquet/timeseries/minute/600000.SH/2026.parquet",
        "examples/market_data_v1/data/parquet/timeseries/minute_raw/000001.SZ/2026.parquet",
        "examples/market_data_v1/data/parquet/timeseries/minute_raw/600000.SH/2026.parquet",
    }
)
_EXPECTED_EXAMPLE_FILES = _EXPECTED_MARKET_DATA_V1_FILES | frozenset(
    {
        "examples/README.md",
        "examples/chanlun_divergence_strategy.py",
        "examples/etf_2b_reversal.py",
        "examples/etf_simple_test.py",
        "examples/etf_static_benchmark.py",
        "examples/ma_cross_strategy.py",
    }
)
_SDIST_PREFIX = "diepi-0.0.0/"


def _write_junit(path: Path, cases: str) -> Path:
    path.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>", encoding="utf-8")
    return path


def _isolate_product_document_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        release_builder, "load_wheel_payload_allowlist", lambda project_root: frozenset()
    )
    monkeypatch.setattr(
        release_builder,
        "validate_source_payload",
        lambda project_root, allowlist: None,
    )
    monkeypatch.setattr(
        release_builder,
        "validate_market_data_v1_source",
        lambda project_root: None,
    )
    monkeypatch.setattr(release_builder, "_SOURCE_ROOT_FILES", frozenset())
    monkeypatch.setattr(release_builder, "_EXAMPLE_FILES", frozenset())
    monkeypatch.setattr(release_builder, "_TOOL_FILES", frozenset())
    monkeypatch.setattr(release_builder, "_public_test_files", lambda project_root: ())
    monkeypatch.setattr(
        release_builder,
        "load_public_git_allowlist",
        lambda project_root: _EXPECTED_PRODUCT_DOCUMENT_FILES,
    )


def _write_product_documents(project_root: Path, excluded: frozenset[str] = frozenset()) -> None:
    for relative in _EXPECTED_PRODUCT_DOCUMENT_FILES - excluded:
        path = project_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {path.name}\n", encoding="utf-8")


def _minimal_sdist_archive(project_root: Path, tmp_path: Path):
    metadata = (
        b"Metadata-Version: 2.4\n"
        b"Name: diepi\n"
        b"Version: 0.0.0\n"
        b"License-Expression: Apache-2.0\n"
        b"License-File: LICENSE\n"
        b"Description-Content-Type: text/markdown\n"
        b"Requires-Python: >=3.10\n"
        b"Requires-Dist: numpy>=1.23\n"
        b"Requires-Dist: pandas>=1.5\n"
        b"Requires-Dist: pyarrow>=23.0.1\n"
        b'Requires-Dist: PySide6<7,>=6.10; extra == "gui"\n'
        b'Requires-Dist: pyqtgraph<0.15,>=0.14; extra == "gui"\n'
        b'Requires-Dist: pytest>=7; extra == "dev"\n'
        b'Requires-Dist: build<2,>=1.2; extra == "dev"\n'
        b'Requires-Dist: twine<8,>=7; extra == "dev"\n'
        b'Requires-Dist: coverage[toml]<8,>=7.6; extra == "dev"\n'
        b'Requires-Dist: pip-audit<3,>=2.9; extra == "dev"\n'
        b'Requires-Dist: ruff<1,>=0.12; extra == "dev"\n\n'
    )
    relative_files = (
        release_checker._ROOT_FILES
        | release_checker._EXAMPLE_FILES
        | release_checker._TOOL_FILES
        | release_checker.source_public_test_files(project_root)
        | _EXPECTED_PRODUCT_DOCUMENT_FILES
    )
    prefix = _SDIST_PREFIX
    files = {prefix + relative: b"public\n" for relative in relative_files}
    for relative in _EXPECTED_MARKET_DATA_V1_FILES:
        files[prefix + relative] = (project_root / relative).read_bytes()
    files[prefix + "LICENSE"] = (project_root / "LICENSE").read_bytes()
    files[prefix + "PKG-INFO"] = metadata
    for name in release_checker._EGG_INFO_FILES:
        files[f"{prefix}diepi.egg-info/{name}"] = b"public\n"
    files[f"{prefix}diepi.egg-info/PKG-INFO"] = metadata
    return release_checker.ArchiveView(
        path=tmp_path / "diepi-0.0.0.tar.gz",
        kind="sdist",
        files=files,
    )


def _minimal_wheel_archive(project_root: Path, tmp_path: Path):
    dist_info = "diepi-0.0.0.dist-info"
    metadata = (
        b"Metadata-Version: 2.4\n"
        b"Name: diepi\n"
        b"Version: 0.0.0\n"
        b"License-Expression: Apache-2.0\n"
        b"License-File: LICENSE\n"
        b"Description-Content-Type: text/markdown\n"
        b"Requires-Python: >=3.10\n"
        b"Requires-Dist: numpy>=1.23\n"
        b"Requires-Dist: pandas>=1.5\n"
        b"Requires-Dist: pyarrow>=23.0.1\n"
        b'Requires-Dist: PySide6<7,>=6.10; extra == "gui"\n'
        b'Requires-Dist: pyqtgraph<0.15,>=0.14; extra == "gui"\n'
        b'Requires-Dist: pytest>=7; extra == "dev"\n'
        b'Requires-Dist: build<2,>=1.2; extra == "dev"\n'
        b'Requires-Dist: twine<8,>=7; extra == "dev"\n'
        b'Requires-Dist: coverage[toml]<8,>=7.6; extra == "dev"\n'
        b'Requires-Dist: pip-audit<3,>=2.9; extra == "dev"\n'
        b'Requires-Dist: ruff<1,>=0.12; extra == "dev"\n\n'
    )
    return release_checker.ArchiveView(
        path=tmp_path / "diepi-0.0.0-py3-none-any.whl",
        kind="wheel",
        files={
            f"{dist_info}/METADATA": metadata,
            f"{dist_info}/entry_points.txt": (
                b"[console_scripts]\ndiepi = diepi.cli:main\n"
            ),
            f"{dist_info}/licenses/LICENSE": (
                project_root / "LICENSE"
            ).read_bytes(),
            f"{dist_info}/top_level.txt": b"diepi\n",
        },
    )


def test_junit_gate_accepts_a_real_pass(tmp_path):
    report = _write_junit(
        tmp_path / "pass.xml",
        '<testcase classname="unit" name="passes"/>',
    )

    counts = inspect_junit(report)

    assert counts.total == 1
    assert counts.passed == 1
    assert counts.skipped == 0


def test_junit_gate_rejects_zero_tests(tmp_path):
    report = _write_junit(tmp_path / "empty.xml", "")

    with pytest.raises(OutcomeGateError, match="zero tests"):
        inspect_junit(report)


def test_junit_gate_rejects_all_skipped(tmp_path):
    report = _write_junit(
        tmp_path / "skipped.xml",
        '<testcase classname="unit" name="skipped"><skipped/></testcase>',
    )

    with pytest.raises(OutcomeGateError, match="no passing tests"):
        inspect_junit(report)


def test_junit_gate_enforces_minimum_pass_count(tmp_path):
    report = _write_junit(
        tmp_path / "too-small.xml",
        '<testcase classname="unit" name="a"/>'
        '<testcase classname="unit" name="b"/>',
    )

    with pytest.raises(OutcomeGateError, match="minimum=3"):
        inspect_junit(report, min_passed=3)


def test_wheel_payload_allowlist_has_no_tests_or_result_snapshots():
    project_root = Path(__file__).resolve().parents[2]

    entries = load_wheel_payload_allowlist(project_root)

    assert entries
    assert not any("/tests/" in path for path in entries)
    assert not any(Path(path).name.startswith("test_") for path in entries)
    assert not any("diepi_results" in path for path in entries)
    assert not any("__pycache__" in path for path in entries)
    assert "diepi/backtest/engine/window.py" in entries


def test_distribution_and_console_entry_point_are_diepi(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_wheel_archive(project_root, tmp_path)

    assert release_checker.PROJECT_NAME == "diepi"
    report = release_checker._check_wheel(archive, project_root, frozenset())

    assert report.version == "0.0.0"


def test_wheel_rejects_unsafe_dependency_floor(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_wheel_archive(project_root, tmp_path)
    metadata_name = "diepi-0.0.0.dist-info/METADATA"
    archive.files[metadata_name] = archive.files[metadata_name].replace(
        b"pyarrow>=23.0.1", b"pyarrow>=10"
    )

    with pytest.raises(ReleaseGateError, match="Requires-Dist differs"):
        release_checker._check_wheel(archive, project_root, frozenset())


def test_wheel_rejects_unexpected_console_entry_point(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_wheel_archive(project_root, tmp_path)
    archive.files["diepi-0.0.0.dist-info/entry_points.txt"] = (
        b"[console_scripts]\nother-name = diepi.cli:main\n"
    )

    with pytest.raises(ReleaseGateError, match="console entry point"):
        release_checker._check_wheel(archive, project_root, frozenset())


@pytest.mark.parametrize(
    "entry_points",
    [
        b"[console_scripts]\nnotdiepi = diepi.cli:main\n",
        (
            b"[console_scripts]\ndiepi = diepi.cli:main\n"
            b"other = diepi.cli:main\n"
        ),
    ],
)
def test_wheel_requires_the_exact_console_entry_point_set(tmp_path, entry_points):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_wheel_archive(project_root, tmp_path)
    archive.files["diepi-0.0.0.dist-info/entry_points.txt"] = entry_points

    with pytest.raises(ReleaseGateError, match="console entry point"):
        release_checker._check_wheel(archive, project_root, frozenset())


def test_wheel_requires_canonical_filename_and_dist_info(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_wheel_archive(project_root, tmp_path)
    wrong_filename = release_checker.ArchiveView(
        path=tmp_path / "other-0.0.0-py3-none-any.whl",
        kind="wheel",
        files=archive.files,
    )
    with pytest.raises(ReleaseGateError, match="wheel filename"):
        release_checker._check_wheel(wrong_filename, project_root, frozenset())

    wrong_files = {
        name.replace("diepi-0.0.0.dist-info", "diepi_extra-0.0.0.dist-info"): content
        for name, content in archive.files.items()
    }
    wrong_dist_info = release_checker.ArchiveView(
        path=archive.path,
        kind="wheel",
        files=wrong_files,
    )
    with pytest.raises(ReleaseGateError, match="dist-info directory"):
        release_checker._check_wheel(wrong_dist_info, project_root, frozenset())


@pytest.mark.parametrize(
    "entry_points",
    [
        b"[console_scripts]\nnotdiepi = diepi.cli:main\n",
        (
            b"[console_scripts]\ndiepi = diepi.cli:main\n"
            b"other = diepi.cli:main\n"
        ),
    ],
)
def test_wheel_requires_the_exact_console_entry_point_set(tmp_path, entry_points):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_wheel_archive(project_root, tmp_path)
    archive.files["diepi-0.0.0.dist-info/entry_points.txt"] = entry_points

    with pytest.raises(ReleaseGateError, match="console entry point"):
        release_checker._check_wheel(archive, project_root, frozenset())


def test_wheel_requires_canonical_filename_and_dist_info(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_wheel_archive(project_root, tmp_path)
    wrong_filename = release_checker.ArchiveView(
        path=tmp_path / "other-0.0.0-py3-none-any.whl",
        kind="wheel",
        files=archive.files,
    )
    with pytest.raises(ReleaseGateError, match="wheel filename"):
        release_checker._check_wheel(wrong_filename, project_root, frozenset())

    wrong_files = {
        name.replace("diepi-0.0.0.dist-info", "diepi_extra-0.0.0.dist-info"): content
        for name, content in archive.files.items()
    }
    wrong_dist_info = release_checker.ArchiveView(
        path=archive.path,
        kind="wheel",
        files=wrong_files,
    )
    with pytest.raises(ReleaseGateError, match="dist-info directory"):
        release_checker._check_wheel(wrong_dist_info, project_root, frozenset())


def test_product_document_contract_is_exact_and_manifest_is_explicit():
    project_root = Path(__file__).resolve().parents[2]

    assert release_checker._PRODUCT_DOCUMENT_FILES == _EXPECTED_PRODUCT_DOCUMENT_FILES
    assert _EXPECTED_PRODUCT_DOCUMENT_FILES.isdisjoint(
        load_wheel_payload_allowlist(project_root)
    )

    manifest_lines = {
        line.strip()
        for line in (project_root / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert {
        line for line in manifest_lines if line.startswith("include docs/")
    } == {
        f"include {relative}" for relative in _EXPECTED_PRODUCT_DOCUMENT_FILES
    }
    assert not any(line.startswith("recursive-include docs") for line in manifest_lines)
    assert not any("docs/audit" in line for line in manifest_lines)


def test_community_and_example_release_contracts_are_explicit():
    project_root = Path(__file__).resolve().parents[2]
    manifest_lines = {
        line.strip()
        for line in (project_root / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert _EXPECTED_COMMUNITY_FILES.issubset(release_builder._SOURCE_ROOT_FILES)
    assert _EXPECTED_COMMUNITY_FILES.issubset(release_checker._ROOT_FILES)
    assert release_checker._EXAMPLE_FILES == _EXPECTED_EXAMPLE_FILES
    assert {f"include {relative}" for relative in _EXPECTED_COMMUNITY_FILES}.issubset(
        manifest_lines
    )
    assert {
        f"include {relative}" for relative in _EXPECTED_MARKET_DATA_V1_FILES
    }.issubset(manifest_lines)


def test_market_data_v1_is_a_bounded_source_distribution_example():
    project_root = Path(__file__).resolve().parents[2]

    assert release_checker._MARKET_DATA_V1_FILES == _EXPECTED_MARKET_DATA_V1_FILES
    assert release_checker._MARKET_DATA_V1_MAX_FILE_BYTES == 4 * 1024 * 1024
    assert release_checker._MARKET_DATA_V1_MAX_TOTAL_BYTES == 16 * 1024 * 1024
    assert _EXPECTED_MARKET_DATA_V1_FILES.issubset(
        load_public_git_allowlist(project_root)
    )
    assert _EXPECTED_MARKET_DATA_V1_FILES.isdisjoint(
        load_wheel_payload_allowlist(project_root)
    )

    release_checker.validate_market_data_v1_source(project_root)


def test_only_the_exact_reviewed_market_data_paths_bypass_data_suffix_denial():
    reviewed = (
        "examples/market_data_v1/data/parquet/timeseries/daily/"
        "600000.SH.parquet"
    )

    assert release_checker._validate_member_name(reviewed) == reviewed
    assert release_checker._validate_member_name(f"diepi-0.1.0/{reviewed}") == (
        f"diepi-0.1.0/{reviewed}"
    )
    with pytest.raises(ReleaseGateError, match="denied generated/data file"):
        release_checker._validate_member_name("examples/other/data.parquet")
    with pytest.raises(ReleaseGateError, match="denied generated/data file"):
        release_checker._validate_member_name("examples/other/data.json")


def test_market_data_v1_release_gate_rejects_oversized_or_invalid_parquet():
    project_root = Path(__file__).resolve().parents[2]
    files = {
        relative: (project_root / relative).read_bytes()
        for relative in _EXPECTED_MARKET_DATA_V1_FILES
    }
    parquet_path = sorted(release_checker._MARKET_DATA_V1_PARQUET_FILES)[0]

    oversized = dict(files)
    oversized[parquet_path] = (
        b"PAR1"
        + b"x" * release_checker._MARKET_DATA_V1_MAX_FILE_BYTES
        + b"PAR1"
    )
    with pytest.raises(ReleaseGateError, match="4 MiB per-file budget"):
        release_checker._validate_market_data_v1_payload(oversized)

    invalid = dict(files)
    invalid[parquet_path] = b"not parquet"
    with pytest.raises(ReleaseGateError, match="invalid Parquet envelope"):
        release_checker._validate_market_data_v1_payload(invalid)


def test_source_snapshot_copies_only_product_document_allowlist(
    tmp_path, monkeypatch
):
    _isolate_product_document_snapshot(monkeypatch)
    project_root = tmp_path / "project"
    destination = tmp_path / "snapshot"
    _write_product_documents(project_root)
    extra_product_doc = project_root / "docs/product/05-internal-notes.md"
    extra_product_doc.write_text("private\n", encoding="utf-8")
    audit_doc = project_root / "docs/audit/internal-review.md"
    audit_doc.parent.mkdir(parents=True)
    audit_doc.write_text("private\n", encoding="utf-8")

    release_builder._copy_source_snapshot(project_root, destination)

    copied = {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert copied == _EXPECTED_PRODUCT_DOCUMENT_FILES


def test_source_snapshot_requires_every_product_document(tmp_path, monkeypatch):
    _isolate_product_document_snapshot(monkeypatch)
    project_root = tmp_path / "project"
    missing = "docs/product/04-reference-and-boundaries.md"
    _write_product_documents(project_root, frozenset({missing}))

    with pytest.raises(
        ReleaseGateError,
        match=f"required release source is missing: {missing}",
    ):
        release_builder._copy_source_snapshot(project_root, tmp_path / "snapshot")


def test_sdist_accepts_exact_product_document_set(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_sdist_archive(project_root, tmp_path)

    report = release_checker._check_sdist(archive, project_root, frozenset())

    assert report.kind == "sdist"


def test_sdist_requires_the_exact_source_test_payload(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_sdist_archive(project_root, tmp_path)
    missing = "tests/backtest/test_namespace_contract.py"
    del archive.files[f"{_SDIST_PREFIX}{missing}"]

    with pytest.raises(ReleaseGateError, match="public test payload"):
        release_checker._check_sdist(archive, project_root, frozenset())


def test_sdist_requires_canonical_filename_root_and_egg_info(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_sdist_archive(project_root, tmp_path)
    wrong_filename = release_checker.ArchiveView(
        path=tmp_path / "other-0.0.0.tar.gz",
        kind="sdist",
        files=archive.files,
    )
    with pytest.raises(ReleaseGateError, match="sdist filename"):
        release_checker._check_sdist(wrong_filename, project_root, frozenset())

    wrong_files = {
        name.replace("diepi.egg-info", "other.egg-info"): content
        for name, content in archive.files.items()
    }
    wrong_egg_info = release_checker.ArchiveView(
        path=archive.path,
        kind="sdist",
        files=wrong_files,
    )
    with pytest.raises(ReleaseGateError, match="egg-info directory"):
        release_checker._check_sdist(wrong_egg_info, project_root, frozenset())

    archive = _minimal_sdist_archive(project_root, tmp_path)
    archive.files[f"{_SDIST_PREFIX}tests/backtest/test_unreviewed.py"] = b"pass\n"
    with pytest.raises(ReleaseGateError, match="public test payload"):
        release_checker._check_sdist(archive, project_root, frozenset())


def test_sdist_requires_the_exact_source_test_payload(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_sdist_archive(project_root, tmp_path)
    missing = "tests/backtest/test_namespace_contract.py"
    del archive.files[f"{_SDIST_PREFIX}{missing}"]

    with pytest.raises(ReleaseGateError, match="public test payload"):
        release_checker._check_sdist(archive, project_root, frozenset())

    archive = _minimal_sdist_archive(project_root, tmp_path)
    archive.files[f"{_SDIST_PREFIX}tests/backtest/test_unreviewed.py"] = b"pass\n"
    with pytest.raises(ReleaseGateError, match="public test payload"):
        release_checker._check_sdist(archive, project_root, frozenset())


def test_sdist_rejects_missing_product_document(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_sdist_archive(project_root, tmp_path)
    missing = "docs/product/04-reference-and-boundaries.md"
    del archive.files[f"{_SDIST_PREFIX}{missing}"]

    with pytest.raises(
        ReleaseGateError,
        match="sdist is missing required product documentation",
    ):
        release_checker._check_sdist(archive, project_root, frozenset())


@pytest.mark.parametrize(
    "relative",
    [
        "docs/product/05-internal-notes.md",
        "docs/audit/internal-review.md",
        "docs/development/README.md",
    ],
)
def test_sdist_rejects_document_outside_product_allowlist(tmp_path, relative):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_sdist_archive(project_root, tmp_path)
    archive.files[f"{_SDIST_PREFIX}{relative}"] = b"private\n"

    with pytest.raises(
        ReleaseGateError,
        match="outside the product documentation allowlist",
    ):
        release_checker._check_sdist(archive, project_root, frozenset())


def test_sdist_requires_examples_readme(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_sdist_archive(project_root, tmp_path)
    del archive.files[f"{_SDIST_PREFIX}examples/README.md"]

    with pytest.raises(
        ReleaseGateError,
        match="examples/README.md",
    ):
        release_checker._check_sdist(archive, project_root, frozenset())


def test_sdist_requires_the_complete_market_data_v1_example(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_sdist_archive(project_root, tmp_path)
    missing = sorted(release_checker._MARKET_DATA_V1_PARQUET_FILES)[0]
    del archive.files[f"{_SDIST_PREFIX}{missing}"]

    with pytest.raises(ReleaseGateError, match="market-data-v1 example must be complete"):
        release_checker._check_sdist(archive, project_root, frozenset())


def test_sdist_market_data_v1_bytes_must_match_reviewed_source(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_sdist_archive(project_root, tmp_path)
    relative = "examples/market_data_v1/README.md"
    archive.files[f"{_SDIST_PREFIX}{relative}"] += b"\nmodified\n"

    with pytest.raises(ReleaseGateError, match="differ from the reviewed source tree"):
        release_checker._check_sdist(archive, project_root, frozenset())


@pytest.mark.parametrize("relative", sorted(_EXPECTED_COMMUNITY_FILES))
def test_sdist_requires_community_file(tmp_path, relative):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_sdist_archive(project_root, tmp_path)
    del archive.files[f"{_SDIST_PREFIX}{relative}"]

    with pytest.raises(ReleaseGateError, match=relative):
        release_checker._check_sdist(archive, project_root, frozenset())


@pytest.mark.parametrize(
    "relative",
    [
        "docs/product/README.md",
        "examples/README.md",
        "examples/market_data_v1/data/diepi_dataset.json",
        "examples/market_data_v1/data/parquet/timeseries/daily/600000.SH.parquet",
        "CONTRIBUTING.md",
    ],
)
def test_wheel_rejects_non_runtime_release_payload(tmp_path, relative):
    project_root = Path(__file__).resolve().parents[2]
    archive = _minimal_wheel_archive(project_root, tmp_path)
    archive.files[relative] = b"public source-only material\n"

    with pytest.raises(ReleaseGateError, match="wheel contains files outside"):
        release_checker._check_wheel(archive, project_root, frozenset())


def test_public_test_support_module_is_in_snapshot_and_sdist_allowlists():
    project_root = Path(__file__).resolve().parents[2]
    support_paths = {
        "tests/__init__.py",
        "tests/backtest/__init__.py",
        "tests/backtest/auction_test_policy.py",
        "tests/backtest/conftest.py",
        "tests/futures/__init__.py",
        "tests/futures/conftest.py",
    }
    snapshot_files = set(release_builder._public_test_files(project_root))

    assert support_paths <= _PUBLIC_TEST_SUPPORT_FILES
    assert support_paths <= snapshot_files
    assert all(_is_public_test(path) for path in support_paths)


def test_extracted_sdist_runs_pytest_collection_gate(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        release_builder,
        "_run",
        lambda command, cwd: calls.append((command, cwd)),
    )

    release_builder._collect_sdist_tests(tmp_path)

    assert calls == [
        (
            [
                sys.executable,
                "-m",
                "pytest",
                "--collect-only",
                "-q",
                "-m",
                "not integration",
                "tests/backtest",
                "tests/futures",
            ],
            tmp_path,
        )
    ]


def test_release_tool_constraints_are_exact_and_cover_the_build_chain():
    project_root = Path(__file__).resolve().parents[2]

    constraints = release_builder._load_release_tool_constraints(project_root)

    assert constraints == {
        "build": "1.5.0",
        "pip": "26.2.1",
        "pip-audit": "2.10.1",
        "setuptools": "84.0.0",
        "twine": "7.0.0",
    }


def test_release_checksum_manifest_covers_both_artifacts(tmp_path):
    wheel = tmp_path / "diepi-0.1.0-py3-none-any.whl"
    sdist = tmp_path / "diepi-0.1.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    manifest = tmp_path / "SHA256SUMS"

    release_builder._write_checksum_manifest([wheel, sdist], manifest)

    lines = manifest.read_text(encoding="ascii").splitlines()
    assert len(lines) == 2
    assert any(line.endswith("  diepi-0.1.0-py3-none-any.whl") for line in lines)
    assert any(line.endswith("  diepi-0.1.0.tar.gz") for line in lines)
    assert all(len(line.split()[0]) == 64 for line in lines)


def test_workflows_pin_reviewed_actions_and_bootstrap_pip_first():
    project_root = Path(__file__).resolve().parents[2]
    workflows = sorted((project_root / ".github/workflows").glob("*.yml"))
    expected = {
        "actions/attest": "1e69f48acb82d1966a394da916b4c1698aa569d6",
        "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
        "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
        "actions/setup-python": "5fda3b95a4ea91299a34e894583c3862153e4b97",
        "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
        "pypa/gh-action-pypi-publish": "dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
    }

    assert {path.name for path in workflows} == {"ci.yml", "release.yml"}
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        uses = re.findall(r"^\s*- uses: ([^@\s]+)@([0-9a-f]+)", text, re.MULTILINE)
        assert uses
        assert all(len(commit) == 40 for _, commit in uses)
        assert all(expected[action] == commit for action, commit in uses)

        sections = text.split("- uses: actions/checkout@")
        assert len(sections) > 1
        for section in sections[1:]:
            checkout_step = section.split("\n      - ", 1)[0]
            assert "persist-credentials: false" in checkout_step

        setup_sections = text.split("- uses: actions/setup-python@")
        assert len(setup_sections) > 1
        for section in setup_sections[1:]:
            first_install = section.index("python -m pip install")
            first_command = section[first_install : first_install + 160]
            assert "tools/bootstrap_pip_requirements.txt" in first_command


def test_archive_reader_rejects_result_snapshot_path(tmp_path):
    wheel = tmp_path / "unsafe.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr("diepi_results/run/strategy.py", "pass")

    with pytest.raises(ReleaseGateError, match="denied path component"):
        read_archive(wheel)


def test_sensitive_scanner_rejects_private_absolute_path():
    private_path = bytes((68, 58, 92)) + b"Users\\person\\private-strategy.py"

    with pytest.raises(ReleaseGateError, match="sensitive text"):
        _scan_sensitive_text({"module.py": private_path})


def test_tracked_text_gate_rejects_generated_result_tree(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        release_checker,
        "_load_git_tracked_text",
        lambda project_root: {
            "parity_runs_v2/run/summary.json": b"{}",
        },
    )

    with pytest.raises(ReleaseGateError, match="generated/private result path"):
        release_checker.check_tracked_text(tmp_path)


def test_tracked_text_gate_rejects_private_strategy_identifier(
    tmp_path, monkeypatch
):
    private_identifier = b"strategy_" + b"v2"
    monkeypatch.setattr(
        release_checker,
        "_load_git_tracked_text",
        lambda project_root: {"docs/audit.md": private_identifier},
    )

    with pytest.raises(ReleaseGateError, match="private strategy identifier"):
        release_checker.check_tracked_text(tmp_path)


def test_sensitive_scanner_rejects_retired_project_brand():
    retired_brand = bytes((113, 117, 97, 110, 116, 98, 97, 99, 107))

    with pytest.raises(ReleaseGateError, match="retired project brand"):
        _scan_sensitive_text({"README.md": retired_brand})

    retired_member = retired_brand.decode("ascii") + "/module.py"
    with pytest.raises(ReleaseGateError, match="archive member name"):
        _scan_sensitive_text({retired_member: b"public\n"})


def test_tracked_text_gate_accepts_portable_placeholders(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        release_checker,
        "_load_git_tracked_text",
        lambda project_root: {
            "docs/setup.md": b"Use <DATA_ROOT>/parquet from <repo>."
        },
    )

    release_checker.check_tracked_text(tmp_path)


def test_public_git_allowlist_excludes_local_review_material():
    project_root = Path(__file__).resolve().parents[2]

    entries = load_public_git_allowlist(project_root)

    assert "docs/stage1_notes.md" not in entries
    assert not any(path.startswith("docs/audit/") for path in entries)
    assert "tools/check_public_tree.py" in entries
    assert "tools/public_git_allowlist.txt" in entries


def test_public_tree_cli_can_be_started_by_script_path(tmp_path):
    project_root = Path(__file__).resolve().parents[2]
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    for name in ("__init__.py", "check_public_tree.py", "check_release_artifacts.py"):
        shutil.copy2(project_root / "tools" / name, tools_dir / name)

    completed = subprocess.run(
        [sys.executable, "tools/check_public_tree.py", "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert completed.returncode == 0, completed.stderr
    assert "--project-root" in completed.stdout
    assert not (tools_dir / "__pycache__").exists()


def test_markdown_link_gate_rejects_missing_target_and_line_anchor(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/public_git_allowlist.txt").write_text(
        "README.md\nsrc.py\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text(
        "[missing](missing.md) [line](src.py#L2)\n", encoding="utf-8"
    )
    (tmp_path / "src.py").write_text("VALUE = 1\n", encoding="utf-8")

    with pytest.raises(MarkdownLinkError, match="missing target") as error:
        check_markdown_links(tmp_path)

    assert "line anchor L2 exceeds" in str(error.value)


def test_markdown_link_gate_rejects_non_public_target(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools/public_git_allowlist.txt").write_text(
        "README.md\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("[private](notes.md)\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("# Private\n", encoding="utf-8")

    with pytest.raises(MarkdownLinkError, match="outside the public tree"):
        check_markdown_links(tmp_path)


def test_public_filesystem_tree_accepts_exact_allowlist(tmp_path):
    approved = frozenset({"README.md", "src/module.py"})
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("# public\n", encoding="utf-8")
    (tmp_path / "src/module.py").write_text("VALUE = 1\n", encoding="utf-8")

    check_public_filesystem_tree(tmp_path, approved)


def test_public_filesystem_tree_rejects_unexpected_empty_directory(tmp_path):
    (tmp_path / "README.md").write_text("# public\n", encoding="utf-8")
    (tmp_path / "empty").mkdir()

    with pytest.raises(ReleaseGateError, match="unexpected directories") as error:
        check_public_filesystem_tree(tmp_path, frozenset({"README.md"}))

    assert "\n  empty" in str(error.value)


def test_public_filesystem_tree_rejects_git_metadata(tmp_path):
    (tmp_path / "README.md").write_text("# public\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()

    with pytest.raises(ReleaseGateError, match="must not contain .git"):
        check_public_filesystem_tree(tmp_path, frozenset({"README.md"}))


@pytest.mark.parametrize("kind", ["missing", "unexpected"])
def test_public_filesystem_tree_requires_exact_path_set(tmp_path, kind):
    approved = frozenset({"README.md"})
    if kind == "unexpected":
        (tmp_path / "README.md").write_text("# public\n", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("extra\n", encoding="utf-8")

    with pytest.raises(ReleaseGateError, match="exact public Git allowlist"):
        check_public_filesystem_tree(tmp_path, approved)


def test_public_filesystem_tree_rejects_lfs_pointer(tmp_path):
    pointer = (
        "version https://git-lfs.github.com/spec/v1\n"
        "oid sha256:" + "0" * 64 + "\nsize 1\n"
    )
    (tmp_path / "large.bin").write_text(pointer, encoding="utf-8")

    with pytest.raises(ReleaseGateError, match="Git LFS pointer"):
        check_public_filesystem_tree(tmp_path, frozenset({"large.bin"}))


@pytest.mark.skipif(shutil.which("git") is None, reason="git is unavailable")
def test_public_git_tree_reads_head_and_requires_clean_worktree(tmp_path):
    project_root = tmp_path / "public-repo"
    project_root.mkdir()
    (project_root / "README.md").write_text("# public\n", encoding="utf-8")
    commands = (
        ["git", "init", "--quiet"],
        ["git", "config", "user.email", "release-test@example.invalid"],
        ["git", "config", "user.name", "Release Test"],
        ["git", "add", "README.md"],
        ["git", "commit", "--quiet", "-m", "initial"],
    )
    for command in commands:
        subprocess.run(command, cwd=project_root, check=True)

    check_public_git_tree(project_root, allowlist=frozenset({"README.md"}))

    (project_root / "untracked.txt").write_text("private\n", encoding="utf-8")
    with pytest.raises(ReleaseGateError, match="must be clean"):
        check_public_git_tree(project_root, allowlist=frozenset({"README.md"}))
