"""Read-only installation and runtime-path diagnostics."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from importlib import metadata, util
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Dict, Optional, Sequence, Tuple

from .. import __version__
from ..backtest.data.calendar import builtin_calendar_identity
from ..backtest.data.cache_manager import CacheManager
from ..backtest.data.plain_files import plain_file_exists
from ..runtime import RuntimePaths


DOCTOR_SCHEMA_VERSION = 1
MINIMUM_PYTHON = (3, 10)
CORE_DISTRIBUTIONS = (
    ("numpy", "numpy", "1.23", None),
    ("pandas", "pandas", "1.5", None),
    ("pyarrow", "pyarrow", "23.0.1", None),
)
GUI_DISTRIBUTIONS = (
    ("PySide6", "PySide6", "6.10", "7"),
    ("pyqtgraph", "pyqtgraph", "0.14", "0.15"),
)


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str
    value: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in {"pass", "warn", "fail"}:
            raise ValueError("doctor check status must be pass, warn, or fail")

    def to_dict(self) -> Dict[str, Optional[str]]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "value": self.value,
        }


@dataclass(frozen=True)
class DoctorReport:
    paths: RuntimePaths
    checks: Tuple[DoctorCheck, ...]
    schema_version: int = DOCTOR_SCHEMA_VERSION

    @property
    def ok(self) -> bool:
        return not any(check.status == "fail" for check in self.checks)

    @property
    def status(self) -> str:
        if not self.ok:
            return "fail"
        if any(check.status == "warn" for check in self.checks):
            return "warn"
        return "pass"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "ok": self.ok,
            "diepi_version": __version__,
            "paths": {
                "data_root": str(self.paths.data_root),
                "data_root_source": self.paths.data_root_source,
                "results_root": str(self.paths.results_root),
                "results_root_source": self.paths.results_root_source,
                "source_checkout_root": (
                    str(self.paths.source_checkout_root)
                    if self.paths.source_checkout_root is not None
                    else None
                ),
            },
            "checks": [check.to_dict() for check in self.checks],
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"


def _release_tuple(value: str) -> Optional[Tuple[int, ...]]:
    """Return the numeric release prefix used by diePi's direct requirements."""

    match = re.match(r"^\s*(\d+(?:\.\d+)*)", value)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _compare_releases(left: Tuple[int, ...], right: Tuple[int, ...]) -> int:
    size = max(len(left), len(right))
    normalized_left = left + (0,) * (size - len(left))
    normalized_right = right + (0,) * (size - len(right))
    return (normalized_left > normalized_right) - (
        normalized_left < normalized_right
    )


def _requirement_text(minimum: str, maximum: Optional[str]) -> str:
    text = f">={minimum}"
    if maximum is not None:
        text += f",<{maximum}"
    return text


def _version_satisfies(
    version: str, *, minimum: str, maximum: Optional[str]
) -> bool:
    release = _release_tuple(version)
    minimum_release = _release_tuple(minimum)
    maximum_release = _release_tuple(maximum) if maximum is not None else None
    if release is None or minimum_release is None:
        return False
    if _compare_releases(release, minimum_release) < 0:
        return False
    if maximum_release is not None and _compare_releases(release, maximum_release) >= 0:
        return False
    return True


def _distribution_check(
    name: str,
    module_name: str,
    minimum: str,
    maximum: Optional[str],
    *,
    optional: bool,
) -> DoctorCheck:
    requirement = _requirement_text(minimum, maximum)
    if util.find_spec(module_name) is None:
        return DoctorCheck(
            name=f"dependency:{name}",
            status="warn" if optional else "fail",
            message=(
                "optional GUI dependency is not installed"
                if optional
                else "required dependency is not installed"
            ),
        )
    try:
        version = metadata.version(name)
    except metadata.PackageNotFoundError:
        return DoctorCheck(
            name=f"dependency:{name}",
            status="warn" if optional else "fail",
            message=f"dependency is importable but version {requirement} cannot be verified",
            value="version metadata unavailable",
        )
    if not _version_satisfies(version, minimum=minimum, maximum=maximum):
        return DoctorCheck(
            name=f"dependency:{name}",
            status="fail",
            message=f"installed dependency does not satisfy {requirement}",
            value=version,
        )
    return DoctorCheck(
        name=f"dependency:{name}",
        status="pass",
        message=f"dependency satisfies {requirement}",
        value=version,
    )


def _builtin_calendar_check() -> DoctorCheck:
    try:
        identity = builtin_calendar_identity()
    except Exception as exc:
        return DoctorCheck(
            name="trade_calendar",
            status="fail",
            message="bundled trade calendar failed its identity check",
            value=f"{type(exc).__name__}: {exc}",
        )
    return DoctorCheck(
        name="trade_calendar",
        status="pass",
        message="bundled A-share trade calendar is available",
        value=(
            f"{identity.calendar_id} "
            f"{identity.coverage_start}..{identity.coverage_end}"
        ),
    )


def run_doctor(
    *, data_root=None, results_root=None, check_gui: bool = True
) -> DoctorReport:
    """Inspect an installation without creating files or importing the GUI."""

    paths = RuntimePaths.resolve(
        data_root=data_root,
        results_root=results_root,
        require_data_root=False,
    )
    checks = []
    current_python = sys.version_info[:3]
    checks.append(
        DoctorCheck(
            name="python",
            status="pass" if current_python >= MINIMUM_PYTHON else "fail",
            message=(
                "Python satisfies diePi's minimum"
                if current_python >= MINIMUM_PYTHON
                else "Python is older than diePi's minimum 3.10"
            ),
            value=".".join(str(value) for value in current_python),
        )
    )
    checks.append(
        DoctorCheck(
            name="diepi",
            status="pass",
            message="diePi package imported",
            value=__version__,
        )
    )
    checks.extend(
        _distribution_check(name, module, minimum, maximum, optional=False)
        for name, module, minimum, maximum in CORE_DISTRIBUTIONS
    )

    data_root_configured = paths.data_root_source in {"explicit", "environment"}
    if not data_root_configured:
        checks.append(
            DoctorCheck(
                name="data_root",
                status="warn",
                message=(
                    "NOT_CONFIGURED: pass --data-root or set DATA_ROOT before "
                    "running a market-data backtest"
                ),
                value=str(paths.data_root),
            )
        )
        checks.append(_builtin_calendar_check())
    elif not paths.data_root.is_dir():
        checks.append(
            DoctorCheck(
                name="data_root",
                status="fail",
                message="data root does not exist or is not a directory",
                value=str(paths.data_root),
            )
        )
    else:
        checks.append(
            DoctorCheck(
                name="data_root",
                status="pass",
                message="data root is a directory",
                value=str(paths.data_root),
            )
        )
        local_calendar = paths.metadata_root / "common" / "trade_cal.parquet"
        try:
            local_calendar_exists = plain_file_exists(
                local_calendar,
                root=paths.data_root,
                label="local trade-calendar override",
            )
        except Exception as exc:
            checks.append(
                DoctorCheck(
                    name="trade_calendar",
                    status="fail",
                    message="local trade-calendar override failed strict validation",
                    value=f"{type(exc).__name__}: {exc}",
                )
            )
        else:
            if local_calendar_exists:
                try:
                    identity = CacheManager(
                        data_root=paths.data_root
                    ).trade_calendar_identity
                except Exception as exc:
                    checks.append(
                        DoctorCheck(
                            name="trade_calendar",
                            status="fail",
                            message="local trade-calendar override failed strict validation",
                            value=f"{type(exc).__name__}: {exc}",
                        )
                    )
                else:
                    checks.append(
                        DoctorCheck(
                            name="trade_calendar",
                            status="pass",
                            message="local trade-calendar override is valid and takes precedence",
                            value=(
                                f"{identity.calendar_id} "
                                f"{identity.coverage_start}..{identity.coverage_end}"
                            ),
                        )
                    )
            else:
                checks.append(_builtin_calendar_check())
        required_paths = (
            (
                "timeseries_root",
                paths.parquet_root,
                "timeseries directory is present",
            ),
        )
        for name, path, success in required_paths:
            checks.append(
                DoctorCheck(
                    name=name,
                    status="pass" if path.exists() else "fail",
                    message=success if path.exists() else "required data path is missing",
                    value=str(path),
                )
            )

    if paths.results_root.exists():
        result_ok = paths.results_root.is_dir()
        checks.append(
            DoctorCheck(
                name="results_root",
                status="pass" if result_ok else "fail",
                message=(
                    "results root is a directory"
                    if result_ok
                    else "results root exists but is not a directory"
                ),
                value=str(paths.results_root),
            )
        )
    else:
        parent = paths.results_root.parent
        parent_ok = parent.is_dir() and os.access(parent, os.W_OK)
        checks.append(
            DoctorCheck(
                name="results_root",
                status="pass" if parent_ok else "fail",
                message=(
                    "results root is absent and can be created by an explicit run"
                    if parent_ok
                    else "results root parent is unavailable or not writable"
                ),
                value=str(paths.results_root),
            )
        )

    if check_gui:
        checks.extend(
            _distribution_check(name, module, minimum, maximum, optional=True)
            for name, module, minimum, maximum in GUI_DISTRIBUTIONS
        )
    return DoctorReport(paths=paths, checks=tuple(checks))


def format_doctor_report(report: DoctorReport) -> str:
    lines = [f"diePi doctor: {report.status.upper()}"]
    lines.append(
        f"data root: {report.paths.data_root} ({report.paths.data_root_source})"
    )
    lines.append(
        "results root: "
        f"{report.paths.results_root} ({report.paths.results_root_source})"
    )
    for check in report.checks:
        suffix = f" [{check.value}]" if check.value else ""
        lines.append(
            f"[{check.status.upper()}] {check.name}: {check.message}{suffix}"
        )
    return "\n".join(lines) + "\n"


def configure_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--data-root", help="local market-data root")
    parser.add_argument("--results-root", help="backtest results root")
    parser.add_argument(
        "--no-gui-check",
        action="store_true",
        help="skip optional PySide6 and pyqtgraph checks",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def execute(args, *, stdout=None, stderr=None) -> int:
    del stderr
    stream = stdout or sys.stdout
    report = run_doctor(
        data_root=getattr(args, "data_root", None),
        results_root=getattr(args, "results_root", None),
        check_gui=not getattr(args, "no_gui_check", False),
    )
    stream.write(report.to_json() if getattr(args, "json", False) else format_doctor_report(report))
    return 0 if report.ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = configure_parser(
        argparse.ArgumentParser(
            prog="diepi doctor",
            description="Read-only installation and path diagnostics",
        )
    )
    return execute(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DOCTOR_SCHEMA_VERSION",
    "DoctorCheck",
    "DoctorReport",
    "configure_parser",
    "execute",
    "format_doctor_report",
    "main",
    "run_doctor",
]
