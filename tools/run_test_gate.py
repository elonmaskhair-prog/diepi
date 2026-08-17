"""Run pytest and reject failure, zero collected tests, or an all-skipped run."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from typing import Optional, Sequence
import xml.etree.ElementTree as ET


class TestGateError(RuntimeError):
    """Raised when a pytest JUnit report is not a real passing test run."""


@dataclass(frozen=True)
class TestCounts:
    total: int
    passed: int
    skipped: int
    failed: int
    errors: int


def inspect_junit(path: Path, *, min_passed: int = 1) -> TestCounts:
    if type(min_passed) is not int or min_passed < 1:
        raise ValueError("min_passed must be a positive integer")
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise TestGateError(f"cannot read pytest JUnit report: {path}") from exc
    testcases = list(root.iter("testcase"))
    failed = sum(case.find("failure") is not None for case in testcases)
    errors = sum(case.find("error") is not None for case in testcases)
    skipped = sum(case.find("skipped") is not None for case in testcases)
    passed = len(testcases) - failed - errors - skipped
    counts = TestCounts(len(testcases), passed, skipped, failed, errors)
    if counts.total == 0:
        raise TestGateError("pytest collected/executed zero tests")
    if counts.passed == 0:
        raise TestGateError(
            f"pytest had no passing tests (total={counts.total}, "
            f"skipped={counts.skipped})"
        )
    if counts.passed < min_passed:
        raise TestGateError(
            "pytest passed fewer tests than required "
            f"(passed={counts.passed}, minimum={min_passed}, "
            f"total={counts.total}, skipped={counts.skipped})"
        )
    if counts.failed or counts.errors:
        raise TestGateError(
            f"pytest report contains failed={counts.failed}, errors={counts.errors}"
        )
    return counts


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junitxml", required=True, type=Path)
    parser.add_argument(
        "--min-passed",
        type=int,
        default=1,
        help="minimum number of passing tests required by the gate",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="pytest arguments after --; defaults to the data-free unit suite",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    pytest_args = list(args.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args.pop(0)
    if not pytest_args:
        pytest_args = ["-m", "not integration", "tests/backtest", "tests/futures"]
    report = args.junitxml.resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "pytest", f"--junitxml={report}", *pytest_args]
    completed = subprocess.run(command, check=False)
    gate_error = None
    counts = None
    try:
        counts = inspect_junit(report, min_passed=args.min_passed)
    except TestGateError as exc:
        gate_error = exc
    if completed.returncode != 0:
        print(f"test gate failed: pytest exited {completed.returncode}", file=sys.stderr)
        return completed.returncode
    if gate_error is not None:
        print(f"test gate failed: {gate_error}", file=sys.stderr)
        return 1
    assert counts is not None
    print(
        "PASS test gate: "
        f"total={counts.total} passed={counts.passed} skipped={counts.skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
