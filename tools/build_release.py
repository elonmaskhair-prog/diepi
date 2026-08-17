"""Build and verify wheel/sdist in a new repository-local staging directory.

This builds the current working tree's explicitly allowlisted source files.  It
is isolated from stale ``build/`` and ``*.egg-info`` directories, but it is not
a substitute for publishing from a clean public Git history.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from typing import Dict, Optional, Sequence, Tuple
import venv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.check_release_artifacts import (
    _EXAMPLE_FILES,
    _PRODUCT_DOCUMENT_FILES,
    _TOOL_FILES,
    ReleaseGateError,
    check_artifact,
    check_artifact_set,
    load_public_git_allowlist,
    load_wheel_payload_allowlist,
    source_public_test_files,
    validate_market_data_v1_source,
    validate_source_payload,
)


_BOOTSTRAP_PIP_REQUIREMENTS = "tools/bootstrap_pip_requirements.txt"
_RELEASE_TOOL_CONSTRAINTS = "tools/release_tool_constraints.txt"
_REQUIRED_RELEASE_TOOLS = frozenset(
    {"pip", "setuptools", "build", "twine", "pip-audit"}
)
_MINIMUM_SAFE_PIP = (26, 1, 2)
_EXACT_CONSTRAINT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")


_SOURCE_ROOT_FILES = frozenset(
    {
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "SECURITY.md",
        "THIRD_PARTY_NOTICES.md",
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
    }
)


def _public_test_files(project_root: Path) -> frozenset[str]:
    return source_public_test_files(project_root)


def _copy_source_snapshot(project_root: Path, destination: Path) -> None:
    allowlist = load_wheel_payload_allowlist(project_root)
    public_allowlist = load_public_git_allowlist(project_root)
    validate_source_payload(project_root, allowlist)
    validate_market_data_v1_source(project_root)
    relative_files = (
        set(allowlist)
        | set(_SOURCE_ROOT_FILES)
        | set(_EXAMPLE_FILES)
        | set(_PRODUCT_DOCUMENT_FILES)
        | set(_TOOL_FILES)
        | set(_public_test_files(project_root))
    )
    not_public = sorted(relative_files - public_allowlist)
    if not_public:
        raise ReleaseGateError(
            "release source files are missing from the public Git allowlist:\n  "
            + "\n  ".join(not_public)
        )
    for relative in sorted(relative_files):
        source = project_root / relative
        if not source.is_file():
            raise ReleaseGateError(f"required release source is missing: {relative}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _run(command: Sequence[str], cwd: Path) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=cwd, check=True)


def _load_release_tool_constraints(project_root: Path) -> Dict[str, str]:
    path = project_root / _RELEASE_TOOL_CONSTRAINTS
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseGateError(f"release tool constraints are unavailable: {path}") from exc
    constraints: Dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _EXACT_CONSTRAINT.fullmatch(line)
        if match is None:
            raise ReleaseGateError(
                f"release tool constraint must be an exact pin at line {line_number}: {line}"
            )
        name, version = match.groups()
        normalized_name = name.lower().replace("_", "-")
        if normalized_name in constraints:
            raise ReleaseGateError(f"duplicate release tool constraint: {name}")
        constraints[normalized_name] = version
    missing = sorted(_REQUIRED_RELEASE_TOOLS - constraints.keys())
    if missing:
        raise ReleaseGateError(
            "release tool constraints are missing: " + ", ".join(missing)
        )
    return constraints


def _check_release_tools(project_root: Path) -> None:
    constraints = _load_release_tool_constraints(project_root)
    mismatches = []
    for name in sorted(_REQUIRED_RELEASE_TOOLS):
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = "not installed"
        expected = constraints[name]
        if installed != expected:
            mismatches.append(f"{name}: expected {expected}, found {installed}")
    if mismatches:
        raise ReleaseGateError(
            "release tools do not match tools/release_tool_constraints.txt; "
            "install the reviewed toolchain before building:\n  "
            + "\n  ".join(mismatches)
        )


def _pip_release(python: Path, cwd: Path) -> Tuple[int, ...]:
    completed = subprocess.run(
        [str(python), "-m", "pip", "--version"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    match = re.match(r"^pip (\d+(?:\.\d+)+)\b", completed.stdout.strip())
    if match is None:
        raise ReleaseGateError(f"cannot parse pip version: {completed.stdout.strip()}")
    return tuple(int(part) for part in match.group(1).split("."))


def _require_safe_pip(python: Path, cwd: Path) -> None:
    release = _pip_release(python, cwd)
    size = max(len(release), len(_MINIMUM_SAFE_PIP))
    normalized = release + (0,) * (size - len(release))
    minimum = _MINIMUM_SAFE_PIP + (0,) * (size - len(_MINIMUM_SAFE_PIP))
    if normalized < minimum:
        found = ".".join(str(part) for part in release)
        raise ReleaseGateError(
            f"pip {found} is below the audited release floor 26.1.2"
        )


def _bootstrap_smoke_pip(python: Path, cwd: Path, project_root: Path) -> None:
    requirements = project_root / _BOOTSTRAP_PIP_REQUIREMENTS
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "-r",
            str(requirements),
        ],
        cwd,
    )
    _require_safe_pip(python, cwd)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_checksum_manifest(artifacts: Sequence[Path], destination: Path) -> None:
    lines = [f"{_sha256(path)}  {path.name}" for path in sorted(artifacts)]
    destination.write_text("\n".join(lines) + "\n", encoding="ascii")


def _extract_sdist(sdist: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=False)
    destination_root = destination.resolve()
    roots = set()
    with tarfile.open(sdist, mode="r:*") as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                raise ReleaseGateError(f"unsafe sdist member: {member.name}")
            relative = Path(*Path(member.name.replace("\\", "/")).parts)
            target = (destination / relative).resolve()
            try:
                target.relative_to(destination_root)
            except ValueError as exc:
                raise ReleaseGateError(f"unsafe sdist path: {member.name}") from exc
            roots.add(relative.parts[0])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseGateError(f"cannot extract sdist member: {member.name}")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
    if len(roots) != 1:
        raise ReleaseGateError("sdist extraction did not produce one source root")
    source_root = destination / next(iter(roots))
    if not source_root.is_dir():
        raise ReleaseGateError("sdist source root is missing after extraction")
    return source_root


def _collect_sdist_tests(source_root: Path) -> None:
    """Prove that the published source tree has a collectable test suite."""

    _run(
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
        source_root,
    )


def _smoke_install(wheel: Path, run_root: Path, project_root: Path) -> None:
    environment = run_root / "smoke-venv"
    cwd = run_root / "smoke-cwd"
    cwd.mkdir(parents=True, exist_ok=False)
    print("+ create isolated virtual environment")
    venv.EnvBuilder(with_pip=True).create(environment)
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise ReleaseGateError("isolated virtual environment has no Python launcher")
    _bootstrap_smoke_pip(python, cwd, project_root)
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            f"{wheel}[gui]",
        ],
        cwd,
    )
    _run([str(python), "-m", "pip", "check"], cwd)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    import_code = (
        "import pathlib, sys; import diepi, diepi.backtest, diepi.futures, "
        "diepi.artifacts, diepi.commands, diepi.demo; "
        "assert diepi.__version__; "
        "assert pathlib.Path(diepi.__file__).resolve().is_relative_to("
        "pathlib.Path(sys.prefix).resolve()); "
        "assert not any(name in sys.modules for name in ('backtest', 'futures', 'run_backtest'))"
    )
    print("+ isolated wheel import smoke")
    subprocess.run([str(python), "-c", import_code], cwd=cwd, env=env, check=True)
    help_code = (
        "import sys, diepi.cli; "
        "sys.argv=['diepi', '--help']; diepi.cli.main()"
    )
    print("+ isolated console help smoke")
    subprocess.run(
        [str(python), "-c", help_code],
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print("+ isolated python -m diepi help smoke")
    subprocess.run(
        [str(python), "-m", "diepi", "--help"],
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    launcher = environment / ("Scripts" if os.name == "nt" else "bin") / (
        "diepi.exe" if os.name == "nt" else "diepi"
    )
    if not launcher.is_file():
        raise ReleaseGateError(f"installed console launcher is missing: {launcher}")
    print("+ isolated installed diepi launcher smoke")
    subprocess.run(
        [str(launcher), "--help"],
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    gui_env = env.copy()
    gui_env["QT_QPA_PLATFORM"] = "offscreen"
    gui_env["PYQTGRAPH_QT_LIB"] = "PySide6"
    gui_code = (
        "from PySide6.QtWidgets import QApplication; "
        "from diepi.backtest.ui.main_window import MainWindow; "
        "app=QApplication.instance() or QApplication([]); "
        "window=MainWindow(); assert window.windowTitle(); window.close()"
    )
    print("+ isolated PySide6 main-window smoke")
    subprocess.run(
        [str(python), "-c", gui_code], cwd=cwd, env=gui_env, check=True
    )


def build_release(output_root: Path, project_root: Path = PROJECT_ROOT) -> Path:
    project_root = project_root.resolve()
    output_root = output_root.resolve()
    _check_release_tools(project_root)
    _require_safe_pip(Path(sys.executable), project_root)
    try:
        relative_output = output_root.relative_to(project_root)
    except ValueError as exc:
        raise ReleaseGateError("output root must be inside the repository") from exc
    if not relative_output.parts:
        raise ReleaseGateError("repository root itself cannot be the release output root")
    output_root.mkdir(parents=True, exist_ok=True)
    run_name = datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%SZ-") + str(os.getpid())
    run_root = output_root / run_name
    run_root.mkdir(exist_ok=False)
    source_snapshot = run_root / "source-snapshot"
    source_snapshot.mkdir()
    _copy_source_snapshot(project_root, source_snapshot)
    dist_dir = run_root / "dist"
    dist_dir.mkdir()
    _run(
        [
            sys.executable,
            "-m",
            "build",
            "--sdist",
            "--no-isolation",
            "--outdir",
            str(dist_dir),
        ],
        source_snapshot,
    )
    sdists = list(dist_dir.glob("*.tar.gz"))
    if len(sdists) != 1:
        raise ReleaseGateError("build must produce exactly one sdist")
    sdist = sdists[0]
    check_artifact(sdist, project_root)
    wheel_source = _extract_sdist(sdist, run_root / "wheel-source")
    _collect_sdist_tests(wheel_source)
    _run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(dist_dir),
        ],
        wheel_source,
    )
    wheels = list(dist_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise ReleaseGateError("build must produce exactly one wheel")
    artifacts = [sdist, wheels[0]]
    check_artifact_set(artifacts, project_root)
    _run(
        [sys.executable, "-m", "twine", "check", *(str(path) for path in artifacts)],
        dist_dir,
    )
    _write_checksum_manifest(artifacts, dist_dir / "SHA256SUMS")
    _smoke_install(wheels[0], run_root, project_root)
    print(f"PASS release build: {dist_dir}")
    return dist_dir


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=PROJECT_ROOT / ".release-gate",
        help="repository-local parent for a new, never-overwritten run directory",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        build_release(args.output_root)
    except (OSError, subprocess.CalledProcessError, tarfile.TarError, ReleaseGateError) as exc:
        print(f"release build failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
