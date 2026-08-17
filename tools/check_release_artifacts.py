"""Offline allowlist, metadata, license, and sensitive-text checks.

The checker intentionally uses only the Python standard library.  It verifies
the archives themselves instead of trusting setuptools discovery rules.
"""

from __future__ import annotations

import argparse
import configparser
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tarfile
from typing import Dict, List, Mapping, Optional, Sequence
import zipfile


PROJECT_NAME = "diepi"
EXPECTED_LICENSE_EXPRESSION = "Apache-2.0"
EXPECTED_REQUIRES_PYTHON = ">=3.10"
EXPECTED_REQUIRES_DIST = frozenset(
    {
        'build<2,>=1.2; extra == "dev"',
        'coverage[toml]<8,>=7.6; extra == "dev"',
        "numpy>=1.23",
        "pandas>=1.5",
        'pip-audit<3,>=2.9; extra == "dev"',
        "pyarrow>=23.0.1",
        'PySide6<7,>=6.10; extra == "gui"',
        'pyqtgraph<0.15,>=0.14; extra == "gui"',
        'pytest>=7; extra == "dev"',
        'ruff<1,>=0.12; extra == "dev"',
        'twine<8,>=7; extra == "dev"',
    }
)

_ROOT_FILES = frozenset(
    {
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "MANIFEST.in",
        "PKG-INFO",
        "README.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
        "requirements.txt",
        "setup.cfg",
        "setup.py",
    }
)
_EGG_INFO_FILES = frozenset(
    {
        "PKG-INFO",
        "SOURCES.txt",
        "dependency_links.txt",
        "entry_points.txt",
        "requires.txt",
        "top_level.txt",
    }
)
_MARKET_DATA_V1_PARQUET_FILES = frozenset(
    {
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
_MARKET_DATA_V1_DATA_FILES = _MARKET_DATA_V1_PARQUET_FILES | frozenset(
    {"examples/market_data_v1/data/diepi_dataset.json"}
)
_MARKET_DATA_V1_FILES = _MARKET_DATA_V1_DATA_FILES | frozenset(
    {
        "examples/market_data_v1/README.md",
        "examples/market_data_v1/generate.py",
    }
)
_MARKET_DATA_V1_MAX_FILE_BYTES = 4 * 1024 * 1024
_MARKET_DATA_V1_MAX_TOTAL_BYTES = 16 * 1024 * 1024

_EXAMPLE_FILES = _MARKET_DATA_V1_FILES | frozenset(
    {
        "examples/README.md",
        "examples/chanlun_divergence_strategy.py",
        "examples/etf_2b_reversal.py",
        "examples/etf_simple_test.py",
        "examples/etf_static_benchmark.py",
        "examples/ma_cross_strategy.py",
    }
)
_PRODUCT_DOCUMENT_FILES = frozenset(
    {
        "docs/product/README.md",
        "docs/product/01-author-note.md",
        "docs/product/02-core-features.md",
        "docs/product/03-user-guide.md",
        "docs/product/04-reference-and-boundaries.md",
        "docs/product/05-local-market-data-format-v1.md",
    }
)
_TOOL_FILES = frozenset(
    {
        "tools/__init__.py",
        "tools/bootstrap_pip_requirements.txt",
        "tools/build_release.py",
        "tools/check_markdown_links.py",
        "tools/check_public_tree.py",
        "tools/check_release_artifacts.py",
        "tools/minimum_core_requirements.txt",
        "tools/minimum_gui_requirements.txt",
        "tools/public_git_allowlist.txt",
        "tools/release_tool_constraints.txt",
        "tools/run_test_gate.py",
        "tools/wheel_payload_allowlist.txt",
    }
)
_PUBLIC_TEST_SUPPORT_FILES = frozenset(
    {
        "tests/__init__.py",
        "tests/backtest/__init__.py",
        "tests/backtest/auction_test_policy.py",
        "tests/backtest/conftest.py",
        "tests/futures/__init__.py",
        "tests/futures/conftest.py",
    }
)
_DENIED_COMPONENTS = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".release-gate",
        "__pycache__",
        "diepi_results",
        "build",
        "dist",
        "output",
        "parity_runs_v2",
        "results",
    }
)
_DENIED_SUFFIXES = frozenset(
    {
        ".csv",
        ".feather",
        ".joblib",
        ".json",
        ".log",
        ".npy",
        ".npz",
        ".parquet",
        ".pickle",
        ".pkl",
        ".pyc",
        ".pyo",
    }
)

_SENSITIVE_PATTERNS = (
    ("retired project brand",
     re.compile(re.escape(bytes((113,117,97,110,116,98,97,99,107))), re.IGNORECASE)),
    ("Windows absolute path",
     re.compile(rb"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\/]")),
    (
        "user home path",
        re.compile(rb"(?i)(?:^|[\\s\"'`])/(?:home|Users)/[^/\\s\"'`]+/"),
    ),
    (
        "private key",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    ),
    ("AWS access key", re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}")),
    ("GitHub token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("OpenAI-style secret", re.compile(rb"sk-[A-Za-z0-9_-]{20,}")),
    (
        "credential assignment",
        re.compile(
            rb"(?im)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)"
            rb"\s*[:=]\s*[\"'][^\"'\r\n\s]{8,}[\"']"
        ),
    ),
)
_TRACKED_TEXT_SUFFIXES = frozenset(
    {
        ".bat",
        ".cfg",
        ".csv",
        ".ini",
        ".json",
        ".md",
        ".ps1",
        ".py",
        ".rst",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_TRACKED_TEXT_NAMES = frozenset(
    {".gitattributes", ".gitignore", "LICENSE", "MANIFEST.in"}
)
_TRACKED_DENIED_COMPONENTS = frozenset(
    {"diepi_results", "parity_runs_v2", "results"}
)
_TRACKED_PRIVATE_PATTERNS = (
    (
        "private strategy identifier",
        re.compile(rb"(?i)\b(?:strategy_v[0-9]+|combo_v[0-9]+)\b"),
    ),
    (
        "private strategy helper identifier",
        re.compile(rb"\bbuild_effective_open\b"),
    ),
)
_PUBLIC_GIT_ALLOWLIST = "tools/public_git_allowlist.txt"
_PUBLIC_TREE_REQUIRED_FILES = frozenset(
    {
        ".github/workflows/ci.yml",
        ".gitattributes",
        ".gitignore",
        "LICENSE",
        "README.md",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
        _PUBLIC_GIT_ALLOWLIST,
    }
)
_PUBLIC_TREE_FORBIDDEN_PATHS = frozenset({"docs/stage1_notes.md"})
_PUBLIC_TREE_FORBIDDEN_PREFIXES = ("docs/audit/",)
_LFS_POINTER = re.compile(
    rb"\Aversion https://git-lfs\.github\.com/spec/v1(?:\r?\n|$)"
)


class ReleaseGateError(RuntimeError):
    """Raised when an artifact violates a release invariant."""


@dataclass(frozen=True)
class ArchiveView:
    path: Path
    kind: str
    files: Mapping[str, bytes]


@dataclass(frozen=True)
class ArtifactReport:
    path: Path
    kind: str
    version: str
    file_count: int


def load_wheel_payload_allowlist(project_root: Path) -> frozenset[str]:
    path = project_root / "tools" / "wheel_payload_allowlist.txt"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseGateError(f"cannot read wheel payload allowlist: {path}") from exc
    entries = frozenset(
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not entries or "diepi/__init__.py" not in entries:
        raise ReleaseGateError("wheel payload allowlist is empty or incomplete")
    for entry in entries:
        _validate_member_name(entry)
        if not entry.endswith(".py"):
            raise ReleaseGateError(f"wheel payload entry is not Python source: {entry}")
    return entries


def load_public_git_allowlist(project_root: Path) -> frozenset[str]:
    """Load the exact set of paths approved for the first public Git tree."""

    path = project_root / _PUBLIC_GIT_ALLOWLIST
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseGateError(f"cannot read public Git allowlist: {path}") from exc
    entries_list = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    duplicates = sorted(
        entry for entry in set(entries_list) if entries_list.count(entry) > 1
    )
    if duplicates:
        raise ReleaseGateError(
            "public Git allowlist contains duplicate paths:\n  "
            + "\n  ".join(duplicates)
        )
    entries = frozenset(entries_list)
    missing_required = sorted(_PUBLIC_TREE_REQUIRED_FILES - entries)
    if missing_required:
        raise ReleaseGateError(
            "public Git allowlist is missing required paths:\n  "
            + "\n  ".join(missing_required)
        )
    for entry in entries:
        normalized = _validate_member_name(entry)
        if normalized != entry:
            raise ReleaseGateError(
                f"public Git allowlist path must use normalized separators: {entry!r}"
            )
        if entry in _PUBLIC_TREE_FORBIDDEN_PATHS or entry.startswith(
            _PUBLIC_TREE_FORBIDDEN_PREFIXES
        ):
            raise ReleaseGateError(
                f"private review material cannot enter the public Git allowlist: {entry}"
            )
    return entries


def _check_exact_public_files(
    files: Mapping[str, bytes],
    allowlist: frozenset[str],
    origin: str,
) -> None:
    actual = frozenset(files)
    missing = sorted(allowlist - actual)
    unexpected = sorted(actual - allowlist)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing:\n  " + "\n  ".join(missing))
        if unexpected:
            details.append("unexpected:\n  " + "\n  ".join(unexpected))
        raise ReleaseGateError(
            f"{origin} differs from the exact public Git allowlist:\n"
            + "\n".join(details)
        )

    _validate_market_data_v1_payload(files)
    _scan_sensitive_text(files)
    findings = []
    for name, content in files.items():
        if _LFS_POINTER.match(content):
            findings.append(f"{name}: Git LFS pointer")
        if name in _MARKET_DATA_V1_PARQUET_FILES:
            continue
        for label, pattern in _TRACKED_PRIVATE_PATTERNS:
            if pattern.search(content):
                findings.append(f"{name}: {label}")
    if findings:
        raise ReleaseGateError(
            f"{origin} privacy gate failed:\n  " + "\n  ".join(sorted(findings))
        )


def _load_filesystem_public_files(
    project_root: Path,
) -> tuple[Dict[str, bytes], frozenset[str]]:
    """Read every entry in a no-Git candidate, rejecting link-like entries."""

    project_root = project_root.resolve()
    if not project_root.is_dir():
        raise ReleaseGateError(f"public candidate is not a directory: {project_root}")
    files: Dict[str, bytes] = {}
    directories = set()
    for path in sorted(project_root.rglob("*")):
        relative_path = path.relative_to(project_root)
        if relative_path.parts and relative_path.parts[0] == ".git":
            continue
        relative = relative_path.as_posix()
        entry = os.lstat(path)
        is_link_like = stat.S_ISLNK(entry.st_mode) or path.is_symlink()
        is_junction = getattr(path, "is_junction", lambda: False)()
        if is_link_like or is_junction:
            raise ReleaseGateError(
                f"public filesystem candidate must not contain links: {relative}"
            )
        if stat.S_ISDIR(entry.st_mode):
            directories.add(_validate_member_name(relative))
            continue
        if not stat.S_ISREG(entry.st_mode):
            raise ReleaseGateError(
                f"public filesystem candidate contains a special file: {relative}"
            )
        normalized = _validate_member_name(relative)
        files[normalized] = path.read_bytes()
    return files, frozenset(directories)


def _check_public_filesystem_directories(
    directories: frozenset[str], allowlist: frozenset[str]
) -> None:
    approved = frozenset(
        parent.as_posix()
        for relative in allowlist
        for parent in PurePosixPath(relative).parents
        if parent != PurePosixPath(".")
    )
    unexpected = sorted(directories - approved)
    if unexpected:
        raise ReleaseGateError(
            "public filesystem candidate differs from the exact public Git allowlist:\n"
            "unexpected directories:\n  " + "\n  ".join(unexpected)
        )


def check_public_filesystem_tree(
    project_root: Path,
    allowlist: Optional[frozenset[str]] = None,
) -> None:
    """Require an uncommitted candidate directory to match the public allowlist."""

    project_root = project_root.resolve()
    if (project_root / ".git").exists():
        raise ReleaseGateError(
            "public filesystem candidate must not contain .git metadata"
        )
    approved = allowlist or load_public_git_allowlist(project_root)
    files, directories = _load_filesystem_public_files(project_root)
    _check_exact_public_files(files, approved, "public filesystem candidate")
    _check_public_filesystem_directories(directories, approved)


def _require_exact_git_root(project_root: Path) -> None:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ReleaseGateError("git is required for public tree checks") from exc
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace").strip()
        raise ReleaseGateError(
            f"cannot identify public Git worktree: {detail or 'git failed'}"
        )
    reported_root = Path(os.fsdecode(result.stdout).strip()).resolve()
    if reported_root != project_root.resolve():
        raise ReleaseGateError(
            "project root must be exactly the public Git worktree root"
        )


def _load_git_commit_files(
    project_root: Path, revision: str = "HEAD"
) -> Dict[str, bytes]:
    """Read regular blobs from a committed Git tree, not from the worktree."""

    project_root = project_root.resolve()
    _require_exact_git_root(project_root)
    tree = subprocess.run(
        ["git", "-C", str(project_root), "ls-tree", "-r", "-z", "--full-tree", revision],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if tree.returncode != 0:
        detail = tree.stderr.decode(errors="replace").strip()
        raise ReleaseGateError(
            f"cannot enumerate public Git revision {revision!r}: {detail or 'git failed'}"
        )

    objects = []
    for record in tree.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded_path = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split(b" ", 2)
        except ValueError as exc:
            raise ReleaseGateError("git returned an invalid tree entry") from exc
        relative = os.fsdecode(encoded_path).replace("\\", "/")
        normalized = _validate_member_name(relative)
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise ReleaseGateError(
                "public Git tree permits only regular blobs: "
                f"{normalized} mode={mode.decode(errors='replace')} "
                f"type={object_type.decode(errors='replace')}"
            )
        objects.append((normalized, object_id.decode("ascii")))

    files: Dict[str, bytes] = {}
    for relative, object_id in objects:
        blob = subprocess.run(
            ["git", "-C", str(project_root), "cat-file", "blob", object_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if blob.returncode != 0:
            detail = blob.stderr.decode(errors="replace").strip()
            raise ReleaseGateError(
                f"cannot read public Git blob for {relative}: {detail or 'git failed'}"
            )
        files[relative] = blob.stdout
    return files


def check_public_git_tree(
    project_root: Path,
    revision: str = "HEAD",
    allowlist: Optional[frozenset[str]] = None,
    require_clean: bool = True,
) -> None:
    """Require a clean committed tree to match the exact public allowlist."""

    project_root = project_root.resolve()
    _require_exact_git_root(project_root)
    if require_clean:
        status = subprocess.run(
            [
                "git",
                "-C",
                str(project_root),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if status.returncode != 0:
            detail = status.stderr.decode(errors="replace").strip()
            raise ReleaseGateError(
                f"cannot inspect public Git worktree status: {detail or 'git failed'}"
            )
        if status.stdout:
            raise ReleaseGateError(
                "public Git worktree must be clean before release-tree validation"
            )
    approved = allowlist or load_public_git_allowlist(project_root)
    files = _load_git_commit_files(project_root, revision)
    _check_exact_public_files(files, approved, f"public Git tree {revision}")


def check_public_tree(project_root: Path, revision: str = "HEAD") -> str:
    """Check a Git commit when present, otherwise a no-Git filesystem candidate."""

    project_root = project_root.resolve()
    if (project_root / ".git").exists():
        check_public_git_tree(project_root, revision)
        return "git"
    check_public_filesystem_tree(project_root)
    return "filesystem"


def validate_source_payload(
    project_root: Path, runtime_allowlist: frozenset[str]
) -> None:
    """Require every runtime source module to be explicitly reviewed."""
    project_root = project_root.resolve()
    discovered = set()
    for path in (project_root / "diepi").rglob("*.py"):
        relative = path.relative_to(project_root).as_posix()
        discovered.add(relative)
    unexpected = sorted(discovered - runtime_allowlist)
    missing = sorted(
        relative
        for relative in runtime_allowlist
        if not (project_root / relative).is_file()
    )
    if unexpected:
        raise ReleaseGateError(
            "runtime source modules are not in the wheel allowlist:\n  "
            + "\n  ".join(unexpected)
        )
    if missing:
        raise ReleaseGateError(
            "wheel allowlist points to missing runtime modules:\n  "
            + "\n  ".join(missing)
        )


def source_public_test_files(project_root: Path) -> frozenset[str]:
    """Return the exact reviewed test payload expected in a public sdist."""

    files = set(_PUBLIC_TEST_SUPPORT_FILES)
    for suite in ("backtest", "futures"):
        suite_dir = project_root / "tests" / suite
        for path in sorted(suite_dir.glob("*.py")):
            if (
                path.name in {"__init__.py", "conftest.py"}
                or path.name.startswith("test_")
            ):
                files.add(path.relative_to(project_root).as_posix())
    missing = sorted(
        relative for relative in files if not (project_root / relative).is_file()
    )
    if missing:
        raise ReleaseGateError(
            "required public test source is missing:\n  " + "\n  ".join(missing)
        )
    return frozenset(files)


def _reviewed_market_data_relative(name: str) -> Optional[str]:
    """Return the canonical reviewed data path, allowing one sdist root."""

    normalized = name.replace("\\", "/")
    if normalized in _MARKET_DATA_V1_DATA_FILES:
        return normalized
    parts = PurePosixPath(normalized).parts
    if len(parts) > 1:
        candidate = PurePosixPath(*parts[1:]).as_posix()
        if candidate in _MARKET_DATA_V1_DATA_FILES:
            return candidate
    return None


def _validate_market_data_v1_payload(files: Mapping[str, bytes]) -> None:
    """Validate the bounded binary example without opening Parquet in the gate."""

    present = frozenset(files) & _MARKET_DATA_V1_FILES
    if not present:
        return
    if present != _MARKET_DATA_V1_FILES:
        missing = sorted(_MARKET_DATA_V1_FILES - present)
        unexpected = sorted(present - _MARKET_DATA_V1_FILES)
        details = []
        if missing:
            details.append("missing:\n  " + "\n  ".join(missing))
        if unexpected:
            details.append("unexpected:\n  " + "\n  ".join(unexpected))
        raise ReleaseGateError(
            "market-data-v1 example must be complete:\n" + "\n".join(details)
        )

    total_bytes = sum(len(files[path]) for path in _MARKET_DATA_V1_DATA_FILES)
    if total_bytes > _MARKET_DATA_V1_MAX_TOTAL_BYTES:
        raise ReleaseGateError(
            "market-data-v1 example exceeds its 16 MiB release budget: "
            f"{total_bytes} bytes"
        )
    oversized = sorted(
        path
        for path in _MARKET_DATA_V1_PARQUET_FILES
        if len(files[path]) > _MARKET_DATA_V1_MAX_FILE_BYTES
    )
    if oversized:
        raise ReleaseGateError(
            "market-data-v1 Parquet file exceeds the 4 MiB per-file budget:\n  "
            + "\n  ".join(oversized)
        )
    invalid_parquet = sorted(
        path
        for path in _MARKET_DATA_V1_PARQUET_FILES
        if len(files[path]) < 8
        or not files[path].startswith(b"PAR1")
        or not files[path].endswith(b"PAR1")
    )
    if invalid_parquet:
        raise ReleaseGateError(
            "market-data-v1 contains an invalid Parquet envelope:\n  "
            + "\n  ".join(invalid_parquet)
        )
    manifest_path = "examples/market_data_v1/data/diepi_dataset.json"
    try:
        manifest = json.loads(files[manifest_path].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseGateError(
            "market-data-v1 diepi_dataset.json must be valid UTF-8 JSON"
        ) from exc
    if not isinstance(manifest, dict):
        raise ReleaseGateError(
            "market-data-v1 diepi_dataset.json must contain a JSON object"
        )


def validate_market_data_v1_source(project_root: Path) -> None:
    """Validate the reviewed market-data example in the source worktree."""

    files: Dict[str, bytes] = {}
    for relative in _MARKET_DATA_V1_FILES:
        path = project_root / relative
        try:
            files[relative] = path.read_bytes()
        except OSError as exc:
            raise ReleaseGateError(
                f"required market-data-v1 source is missing: {relative}"
            ) from exc
    _validate_market_data_v1_payload(files)


def _check_market_data_v1_content(
    files: Mapping[str, bytes], project_root: Path, artifact_kind: str
) -> None:
    validate_market_data_v1_source(project_root)
    mismatched = sorted(
        relative
        for relative in _MARKET_DATA_V1_FILES
        if files.get(relative) != (project_root / relative).read_bytes()
    )
    if mismatched:
        raise ReleaseGateError(
            f"{artifact_kind} market-data-v1 files differ from the reviewed source tree:\n  "
            + "\n  ".join(mismatched)
        )


def _validate_member_name(name: str) -> str:
    normalized = name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if not normalized or normalized.startswith("/") or pure.is_absolute():
        raise ReleaseGateError(f"archive contains an absolute/empty path: {name!r}")
    if any(part in ("", ".", "..") for part in pure.parts):
        raise ReleaseGateError(f"archive contains an unsafe path: {name!r}")
    if any(part.lower() in _DENIED_COMPONENTS for part in pure.parts):
        raise ReleaseGateError(f"archive contains denied path component: {name}")
    if (
        pure.suffix.lower() in _DENIED_SUFFIXES
        and _reviewed_market_data_relative(normalized) is None
    ):
        raise ReleaseGateError(f"archive contains denied generated/data file: {name}")
    return pure.as_posix()


def _read_zip(path: Path) -> ArchiveView:
    files: Dict[str, bytes] = {}
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = _validate_member_name(info.filename)
            if name in files:
                raise ReleaseGateError(f"duplicate archive member: {name}")
            files[name] = archive.read(info)
    return ArchiveView(path=path, kind="wheel", files=files)


def _read_tar(path: Path) -> ArchiveView:
    files: Dict[str, bytes] = {}
    with tarfile.open(path, mode="r:*") as archive:
        for member in archive.getmembers():
            name = _validate_member_name(member.name.rstrip("/"))
            if member.isdir():
                continue
            if not member.isfile():
                raise ReleaseGateError(
                    f"sdist contains a link or special file: {member.name}"
                )
            if name in files:
                raise ReleaseGateError(f"duplicate archive member: {name}")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ReleaseGateError(f"cannot read archive member: {name}")
            files[name] = extracted.read()
    return ArchiveView(path=path, kind="sdist", files=files)


def read_archive(path: Path) -> ArchiveView:
    path = path.resolve()
    if not path.is_file():
        raise ReleaseGateError(f"artifact does not exist: {path}")
    if path.suffix == ".whl":
        return _read_zip(path)
    if path.name.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
        return _read_tar(path)
    raise ReleaseGateError(f"unsupported artifact type: {path.name}")


def _scan_sensitive_text(files: Mapping[str, bytes]) -> None:
    findings: List[str] = []
    for name, content in files.items():
        for label, pattern in _SENSITIVE_PATTERNS:
            if pattern.search(name.encode("utf-8")):
                findings.append(f"{name}: {label} in archive member name")
            if _reviewed_market_data_relative(name) in _MARKET_DATA_V1_PARQUET_FILES:
                continue
            if pattern.search(content):
                findings.append(f"{name}: {label}")
    if findings:
        raise ReleaseGateError(
            "sensitive text found in archive:\n  " + "\n  ".join(sorted(findings))
        )


def _load_git_tracked_text(project_root: Path) -> Dict[str, bytes]:
    """Read present, Git-tracked text files from the release worktree."""

    project_root = project_root.resolve()
    try:
        top_level = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise ReleaseGateError("git is required for tracked-text checks") from exc
    if top_level.returncode != 0:
        detail = top_level.stderr.decode(errors="replace").strip()
        raise ReleaseGateError(
            f"cannot identify release Git worktree: {detail or 'git failed'}"
        )
    reported_root = Path(
        os.fsdecode(top_level.stdout).strip()
    ).resolve()
    if reported_root != project_root:
        raise ReleaseGateError(
            "project root must be exactly the release Git worktree root"
        )

    tracked = subprocess.run(
        ["git", "-C", str(project_root), "ls-files", "--cached", "-z", "--"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if tracked.returncode != 0:
        detail = tracked.stderr.decode(errors="replace").strip()
        raise ReleaseGateError(
            f"cannot enumerate tracked release files: {detail or 'git failed'}"
        )

    files: Dict[str, bytes] = {}
    for encoded in tracked.stdout.split(b"\0"):
        if not encoded:
            continue
        relative = os.fsdecode(encoded).replace("\\", "/")
        pure = PurePosixPath(relative)
        if (
            pure.is_absolute()
            or not pure.parts
            or any(part in ("", ".", "..") for part in pure.parts)
        ):
            raise ReleaseGateError(
                f"git returned an unsafe tracked path: {relative!r}"
            )
        if (
            pure.name not in _TRACKED_TEXT_NAMES
            and pure.suffix.lower() not in _TRACKED_TEXT_SUFFIXES
        ):
            continue
        path = project_root.joinpath(*pure.parts)
        try:
            entry = os.lstat(path)
        except FileNotFoundError:
            # A working-tree deletion is absent from the release snapshot and
            # will disappear from the index when the release commit is made.
            continue
        if stat.S_ISLNK(entry.st_mode):
            raise ReleaseGateError(
                f"tracked text path must not be a symlink: {relative}"
            )
        if not stat.S_ISREG(entry.st_mode):
            continue
        try:
            files[pure.as_posix()] = path.read_bytes()
        except OSError as exc:
            raise ReleaseGateError(
                f"cannot read tracked text file: {relative}"
            ) from exc
    return files


def check_tracked_text(project_root: Path) -> None:
    """Reject private paths, identifiers, credentials, and result artifacts."""

    files = _load_git_tracked_text(project_root)
    findings: List[str] = []
    for name, content in files.items():
        pure = PurePosixPath(name)
        if any(
            part.lower() in _TRACKED_DENIED_COMPONENTS
            for part in pure.parts
        ):
            findings.append(f"{name}: generated/private result path")
        for label, pattern in (
            *_SENSITIVE_PATTERNS,
            *_TRACKED_PRIVATE_PATTERNS,
        ):
            if pattern.search(content):
                findings.append(f"{name}: {label}")
    if findings:
        raise ReleaseGateError(
            "tracked-text privacy gate failed:\n  "
            + "\n  ".join(sorted(findings))
        )


def _metadata_report(metadata_bytes: bytes, origin: str) -> tuple[str, str]:
    metadata = BytesParser(policy=policy.default).parsebytes(metadata_bytes)
    name = metadata.get("Name", "")
    version = metadata.get("Version", "")
    if name != PROJECT_NAME:
        raise ReleaseGateError(f"{origin}: unexpected project name {name!r}")
    if not version:
        raise ReleaseGateError(f"{origin}: missing project version")
    if metadata.get("License-Expression") != EXPECTED_LICENSE_EXPRESSION:
        raise ReleaseGateError(
            f"{origin}: License-Expression must be {EXPECTED_LICENSE_EXPRESSION}"
        )
    classifiers = metadata.get_all("Classifier", [])
    if any(value.startswith("License ::") for value in classifiers):
        raise ReleaseGateError(
            f"{origin}: legacy license classifier conflicts with License-Expression"
        )
    if "LICENSE" not in metadata.get_all("License-File", []):
        raise ReleaseGateError(f"{origin}: License-File metadata is missing LICENSE")
    description_type = (metadata.get("Description-Content-Type") or "").split(";", 1)[0]
    if description_type.strip().lower() != "text/markdown":
        raise ReleaseGateError(f"{origin}: README content type is not text/markdown")
    requires_python = metadata.get("Requires-Python")
    if requires_python != EXPECTED_REQUIRES_PYTHON:
        raise ReleaseGateError(
            f"{origin}: Requires-Python must be {EXPECTED_REQUIRES_PYTHON!r}, "
            f"got {requires_python!r}"
        )
    requires_dist = frozenset(metadata.get_all("Requires-Dist", []))
    if requires_dist != EXPECTED_REQUIRES_DIST:
        missing = sorted(EXPECTED_REQUIRES_DIST - requires_dist)
        unexpected = sorted(requires_dist - EXPECTED_REQUIRES_DIST)
        details = []
        if missing:
            details.append("missing:\n  " + "\n  ".join(missing))
        if unexpected:
            details.append("unexpected:\n  " + "\n  ".join(unexpected))
        raise ReleaseGateError(
            f"{origin}: Requires-Dist differs from reviewed metadata:\n"
            + "\n".join(details)
        )
    return name, version


def _expected_license(project_root: Path) -> bytes:
    try:
        content = (project_root / "LICENSE").read_bytes()
    except OSError as exc:
        raise ReleaseGateError("repository LICENSE is missing") from exc
    required_fragments = (
        b"Apache License",
        b"Version 2.0, January 2004",
        b"TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
        b"END OF TERMS AND CONDITIONS",
    )
    if any(fragment not in content for fragment in required_fragments):
        raise ReleaseGateError("repository LICENSE is not the complete Apache-2.0 text")
    return content


def _check_runtime_content(
    files: Mapping[str, bytes],
    project_root: Path,
    runtime_allowlist: frozenset[str],
    artifact_kind: str,
) -> None:
    mismatched = []
    for relative in sorted(runtime_allowlist):
        try:
            source = (project_root / relative).read_bytes()
        except OSError as exc:
            raise ReleaseGateError(f"cannot read runtime source: {relative}") from exc
        if files.get(relative) != source:
            mismatched.append(relative)
    if mismatched:
        raise ReleaseGateError(
            f"{artifact_kind} runtime files differ from the reviewed source tree:\n  "
            + "\n  ".join(mismatched)
        )


def _check_wheel(
    archive: ArchiveView,
    project_root: Path,
    runtime_allowlist: frozenset[str],
) -> ArtifactReport:
    metadata_names = [
        name for name in archive.files if name.endswith(".dist-info/METADATA")
    ]
    if len(metadata_names) != 1:
        raise ReleaseGateError("wheel must contain exactly one dist-info/METADATA")
    dist_info = metadata_names[0].rsplit("/", 1)[0]
    _, version = _metadata_report(archive.files[metadata_names[0]], metadata_names[0])
    expected_dist_info = f"{PROJECT_NAME}-{version}.dist-info"
    if dist_info != expected_dist_info:
        raise ReleaseGateError(
            f"wheel dist-info directory must be {expected_dist_info}: {dist_info}"
        )
    expected_filename = f"{PROJECT_NAME}-{version}-py3-none-any.whl"
    if archive.path.name != expected_filename:
        raise ReleaseGateError(
            f"wheel filename must be {expected_filename}: {archive.path.name}"
        )
    allowed_metadata = {
        f"{dist_info}/METADATA",
        f"{dist_info}/RECORD",
        f"{dist_info}/WHEEL",
        f"{dist_info}/entry_points.txt",
        f"{dist_info}/top_level.txt",
        f"{dist_info}/licenses/LICENSE",
    }
    actual = frozenset(archive.files)
    unexpected = sorted(actual - runtime_allowlist - allowed_metadata)
    missing = sorted(runtime_allowlist - actual)
    if unexpected:
        raise ReleaseGateError(
            "wheel contains files outside the allowlist:\n  "
            + "\n  ".join(unexpected)
        )
    if missing:
        raise ReleaseGateError(
            "wheel is missing allowlisted runtime files:\n  " + "\n  ".join(missing)
        )
    license_name = f"{dist_info}/licenses/LICENSE"
    if archive.files.get(license_name) != _expected_license(project_root):
        raise ReleaseGateError("wheel LICENSE is missing or differs from repository LICENSE")
    try:
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        parser.read_string(
            archive.files[f"{dist_info}/entry_points.txt"].decode("utf-8")
        )
        sections = set(parser.sections())
        console_scripts = dict(parser.items("console_scripts", raw=True))
    except (KeyError, UnicodeDecodeError, configparser.Error) as exc:
        raise ReleaseGateError("wheel console entry point is missing or invalid") from exc
    if sections != {"console_scripts"} or console_scripts != {
        "diepi": "diepi.cli:main"
    }:
        raise ReleaseGateError("wheel console entry point is missing or unexpected")
    try:
        top_levels = {
            line.strip()
            for line in archive.files[f"{dist_info}/top_level.txt"].decode("utf-8").splitlines()
            if line.strip()
        }
    except (KeyError, UnicodeDecodeError) as exc:
        raise ReleaseGateError("wheel top_level.txt is missing or invalid") from exc
    if top_levels != {"diepi"}:
        raise ReleaseGateError(f"wheel exposes unexpected top-level modules: {sorted(top_levels)}")
    _check_runtime_content(archive.files, project_root, runtime_allowlist, "wheel")
    _scan_sensitive_text(archive.files)
    return ArtifactReport(archive.path, archive.kind, version, len(archive.files))


def _strip_sdist_root(files: Mapping[str, bytes]) -> Dict[str, bytes]:
    roots = {name.split("/", 1)[0] for name in files}
    if len(roots) != 1:
        raise ReleaseGateError("sdist must contain exactly one top-level directory")
    root = next(iter(roots))
    if not root.startswith(f"{PROJECT_NAME}-"):
        raise ReleaseGateError(f"unexpected sdist root directory: {root}")
    stripped: Dict[str, bytes] = {}
    for name, content in files.items():
        if "/" not in name:
            raise ReleaseGateError(f"sdist file is outside its root directory: {name}")
        relative = name.split("/", 1)[1]
        if relative in stripped:
            raise ReleaseGateError(f"duplicate relative sdist member: {relative}")
        stripped[relative] = content
    return stripped


def _is_public_test(path: str) -> bool:
    if path in _PUBLIC_TEST_SUPPORT_FILES:
        return True
    pure = PurePosixPath(path)
    if len(pure.parts) == 3 and pure.parts[:2] == ("tests", "backtest"):
        return pure.suffix == ".py" and (
            pure.name in {"__init__.py", "conftest.py"}
            or pure.name.startswith("test_")
        )
    if len(pure.parts) == 3 and pure.parts[:2] == ("tests", "futures"):
        return pure.suffix == ".py" and (
            pure.name in {"__init__.py", "conftest.py"}
            or pure.name.startswith("test_")
        )
    return False


def _is_egg_info(path: str) -> bool:
    pure = PurePosixPath(path)
    return (
        len(pure.parts) == 2
        and pure.parts[0].endswith(".egg-info")
        and pure.parts[1] in _EGG_INFO_FILES
    )


def _validate_sdist_product_documents(files: Mapping[str, bytes]) -> None:
    """Require the exact public product-document set and no other docs."""

    document_files = frozenset(
        path
        for path in files
        if PurePosixPath(path).parts[:1] == ("docs",)
    )
    unexpected = sorted(document_files - _PRODUCT_DOCUMENT_FILES)
    if unexpected:
        raise ReleaseGateError(
            "sdist contains documents outside the product documentation allowlist:\n  "
            + "\n  ".join(unexpected)
        )
    missing = sorted(_PRODUCT_DOCUMENT_FILES - document_files)
    if missing:
        raise ReleaseGateError(
            "sdist is missing required product documentation:\n  "
            + "\n  ".join(missing)
        )


def _check_sdist(
    archive: ArchiveView,
    project_root: Path,
    runtime_allowlist: frozenset[str],
) -> ArtifactReport:
    files = _strip_sdist_root(archive.files)
    _validate_sdist_product_documents(files)
    _validate_market_data_v1_payload(files)
    _check_market_data_v1_content(files, project_root, "sdist")
    expected_tests = source_public_test_files(project_root)
    actual_tests = frozenset(
        path for path in files if PurePosixPath(path).parts[:1] == ("tests",)
    )
    if actual_tests != expected_tests:
        missing_tests = sorted(expected_tests - actual_tests)
        unexpected_tests = sorted(actual_tests - expected_tests)
        details = []
        if missing_tests:
            details.append("missing:\n  " + "\n  ".join(missing_tests))
        if unexpected_tests:
            details.append("unexpected:\n  " + "\n  ".join(unexpected_tests))
        raise ReleaseGateError(
            "sdist public test payload differs from the reviewed source tree:\n"
            + "\n".join(details)
        )
    allowed = (
        runtime_allowlist
        | _ROOT_FILES
        | _EXAMPLE_FILES
        | _PRODUCT_DOCUMENT_FILES
        | _TOOL_FILES
    )
    unexpected = sorted(
        path
        for path in files
        if path not in allowed and not _is_public_test(path) and not _is_egg_info(path)
    )
    if unexpected:
        raise ReleaseGateError(
            "sdist contains files outside the allowlist:\n  "
            + "\n  ".join(unexpected)
        )
    required = (
        runtime_allowlist
        | (_ROOT_FILES - {"PKG-INFO"})
        | _EXAMPLE_FILES
        | _PRODUCT_DOCUMENT_FILES
        | _TOOL_FILES
        | _PUBLIC_TEST_SUPPORT_FILES
    )
    missing = sorted(required - frozenset(files))
    if missing:
        raise ReleaseGateError(
            "sdist is missing required source files:\n  " + "\n  ".join(missing)
        )
    if not any(path.startswith("tests/backtest/test_") for path in files):
        raise ReleaseGateError("sdist must retain public diepi.backtest tests")
    if not any(
        path.startswith("tests/futures/test_") and path.endswith(".py") for path in files
    ):
        raise ReleaseGateError("sdist must retain public diepi.futures tests")
    if files.get("LICENSE") != _expected_license(project_root):
        raise ReleaseGateError("sdist LICENSE is missing or differs from repository LICENSE")
    metadata_names = [path for path in files if _is_egg_info(path) and path.endswith("/PKG-INFO")]
    if "PKG-INFO" in files:
        metadata_names.append("PKG-INFO")
    if not metadata_names:
        raise ReleaseGateError("sdist package metadata is missing")
    _, version = _metadata_report(files[metadata_names[0]], metadata_names[0])
    egg_info_dirs = {
        PurePosixPath(path).parts[0] for path in files if _is_egg_info(path)
    }
    expected_egg_info = f"{PROJECT_NAME}.egg-info"
    if egg_info_dirs != {expected_egg_info}:
        raise ReleaseGateError(
            f"sdist egg-info directory must be {expected_egg_info}: "
            f"{sorted(egg_info_dirs)}"
        )
    expected_root = f"{PROJECT_NAME}-{version}"
    roots = {name.split("/", 1)[0] for name in archive.files}
    if roots != {expected_root}:
        raise ReleaseGateError(
            f"sdist root directory must be {expected_root}: {sorted(roots)}"
        )
    expected_filename = f"{PROJECT_NAME}-{version}.tar.gz"
    if archive.path.name != expected_filename:
        raise ReleaseGateError(
            f"sdist filename must be {expected_filename}: {archive.path.name}"
        )
    _check_runtime_content(files, project_root, runtime_allowlist, "sdist")
    _scan_sensitive_text(files)
    return ArtifactReport(archive.path, archive.kind, version, len(files))


def check_artifact(path: Path, project_root: Path) -> ArtifactReport:
    project_root = project_root.resolve()
    archive = read_archive(path)
    runtime_allowlist = load_wheel_payload_allowlist(project_root)
    validate_source_payload(project_root, runtime_allowlist)
    if archive.kind == "wheel":
        return _check_wheel(archive, project_root, runtime_allowlist)
    return _check_sdist(archive, project_root, runtime_allowlist)


def discover_artifacts(paths: Sequence[Path]) -> List[Path]:
    discovered: List[Path] = []
    for path in paths:
        if path.is_dir():
            discovered.extend(sorted(path.glob("*.whl")))
            discovered.extend(sorted(path.glob("*.tar.gz")))
        else:
            discovered.append(path)
    unique = list(dict.fromkeys(path.resolve() for path in discovered))
    if not unique:
        raise ReleaseGateError("no wheel or sdist artifacts were provided")
    return unique


def check_artifact_set(paths: Sequence[Path], project_root: Path) -> List[ArtifactReport]:
    reports = [check_artifact(path, project_root) for path in discover_artifacts(paths)]
    kinds = {report.kind for report in reports}
    if kinds != {"wheel", "sdist"}:
        raise ReleaseGateError("release gate requires both one wheel and one sdist")
    if len(reports) != 2:
        raise ReleaseGateError("release gate requires exactly one wheel and one sdist")
    versions = {report.version for report in reports}
    if len(versions) != 1:
        raise ReleaseGateError("wheel and sdist versions differ")
    return reports


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "artifacts",
        nargs="+",
        type=Path,
        help="artifact file(s), or one directory containing exactly a wheel and sdist",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root containing LICENSE and tools/wheel_payload_allowlist.txt",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        reports = check_artifact_set(args.artifacts, args.project_root)
    except (OSError, tarfile.TarError, zipfile.BadZipFile, ReleaseGateError) as exc:
        print(f"release artifact gate failed: {exc}", file=sys.stderr)
        return 1
    for report in reports:
        print(
            f"PASS {report.kind}: {report.path.name} "
            f"version={report.version} files={report.file_count}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
