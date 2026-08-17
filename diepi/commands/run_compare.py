"""Command adapter for verified run-to-run cash parity reports."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Optional, Sequence, TextIO

from diepi.artifacts import ArtifactStore
from diepi.backtest.comparison import (
    RunParityPolicy,
    RunParityStatus,
    compare_cash_runs,
)


EXIT_OK = 0
EXIT_DIFFERENT = 1
EXIT_USAGE = 2
COMMAND_REPORT_SCHEMA = "diepi.run_comparison_command_report"
COMMAND_REPORT_SCHEMA_VERSION = 2


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _command_payload(
    report,
    *,
    legacy_explicitly_allowed: bool,
) -> dict:
    baseline_verified = bool(
        report.baseline.source_kind == "VERIFIED_ARTIFACT"
        and report.baseline.artifact_verified is True
    )
    candidate_verified = bool(
        report.candidate.source_kind == "VERIFIED_ARTIFACT"
        and report.candidate.artifact_verified is True
    )
    baseline_rankable = bool(baseline_verified and report.baseline.rankable is True)
    candidate_rankable = bool(candidate_verified and report.candidate.rankable is True)
    trusted = bool(
        baseline_verified
        and candidate_verified
        and baseline_rankable
        and candidate_rankable
    )
    if not baseline_verified or not candidate_verified:
        status = "UNVERIFIED"
    elif not baseline_rankable or not candidate_rankable:
        status = "NOT_RANKABLE"
    else:
        status = report.projection_status.value
    payload = {
        "artifact_trust": {
            "baseline_rankable": baseline_rankable,
            "baseline_verified": baseline_verified,
            "candidate_rankable": candidate_rankable,
            "candidate_verified": candidate_verified,
            "legacy_explicitly_allowed": legacy_explicitly_allowed,
            "trusted_comparison": trusted,
        },
        "comparison": report.to_dict(),
        "portable_certification": False,
        "schema": COMMAND_REPORT_SCHEMA,
        "schema_version": COMMAND_REPORT_SCHEMA_VERSION,
        "status": status,
        "verification_scope": "local_command_execution",
    }
    payload["command_report_sha256"] = hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()
    return payload


def _command_markdown(payload: dict, report) -> str:
    trust = payload["artifact_trust"]
    core = report.to_markdown().replace(
        "# Run parity report", "## Economic and metric comparison", 1
    )
    rows = [
        "# Run comparison command report",
        "",
        f"- Overall status: `{payload['status']}`",
        f"- Baseline artifact verified: `{str(trust['baseline_verified']).lower()}`",
        f"- Baseline result rankable: `{str(trust['baseline_rankable']).lower()}`",
        f"- Candidate artifact verified: `{str(trust['candidate_verified']).lower()}`",
        f"- Candidate result rankable: `{str(trust['candidate_rankable']).lower()}`",
        f"- Trusted comparison: `{str(trust['trusted_comparison']).lower()}`",
        "- Portable certification/signature: `false`",
        "- Canonical command JSON payload SHA-256: "
        f"`{payload['command_report_sha256']}`",
        "",
    ]
    if not trust["baseline_verified"] or not trust["candidate_verified"]:
        rows.extend(
            (
                "> At least one input is an explicitly allowed, unverified legacy "
                "directory. The economic projection below is diagnostic only and "
                "does not make the overall comparison trusted.",
                "",
            )
        )
    elif not trust["trusted_comparison"]:
        rows.extend(
            (
                "> Both directories are verified artifacts, but at least one "
                "result is not rankable. The projection below is diagnostic "
                "only and cannot be an overall successful parity result.",
                "",
            )
        )
    rows.append(core.rstrip("\n"))
    return "\n".join(rows) + "\n"


def _write_command_report(
    path: Path, payload: dict, report, *, overwrite: bool
) -> Path:
    if type(overwrite) is not bool:
        raise TypeError("overwrite must be exactly bool")
    target = path.expanduser().resolve()
    if target.suffix.lower() not in {".json", ".md"}:
        raise ValueError("run comparison report path must end in .json or .md")
    if target.exists() and not overwrite:
        raise FileExistsError(f"run comparison report already exists: {target}")
    if target.suffix.lower() == ".md":
        text = _command_markdown(payload, report)
    else:
        text = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ) + "\n"
    target.write_text(text, encoding="utf-8")
    return target


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _load(path: str, *, allow_unverified_legacy: bool):
    root = Path(path).expanduser().absolute()
    if not root.is_dir():
        raise FileNotFoundError(f"运行目录不存在或不是目录: {root}")
    if (root / "manifest.json").is_file():
        loaded = ArtifactStore.load(root)
        if not loaded.artifact_verified:
            raise ValueError(f"RunArtifact 未通过验证: {root}")
        return loaded, True
    if not allow_unverified_legacy:
        raise ValueError(
            "目录不是 RunArtifact v1；若确需比较旧结果，请显式使用 "
            f"--allow-unverified-legacy: {root}"
        )
    return ArtifactStore.load_legacy(root), False


def execute(
    args,
    *,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        baseline, baseline_verified = _load(
            args.baseline,
            allow_unverified_legacy=args.allow_unverified_legacy,
        )
        candidate, candidate_verified = _load(
            args.candidate,
            allow_unverified_legacy=args.allow_unverified_legacy,
        )
        if baseline_verified and candidate_verified:
            policy = RunParityPolicy(
                absolute_tolerance=args.atol,
                relative_tolerance=args.rtol,
            )
        else:
            policy = RunParityPolicy.legacy_common(
                absolute_tolerance=args.atol,
                relative_tolerance=args.rtol,
            )
        report = compare_cash_runs(
            baseline,
            candidate,
            baseline_label=args.baseline_label,
            candidate_label=args.candidate_label,
            policy=policy,
        )
        payload = _command_payload(
            report,
            legacy_explicitly_allowed=bool(args.allow_unverified_legacy),
        )
        report_path = None
        if args.report:
            requested_report = Path(args.report).expanduser().absolute()
            for label, loaded in (("baseline", baseline), ("candidate", candidate)):
                run_root = Path(loaded.root).resolve()
                resolved_report = requested_report.resolve(strict=False)
                if _is_within(resolved_report, run_root):
                    raise ValueError(
                        f"report path must not modify the {label} run directory: "
                        f"{resolved_report}"
                    )
            report_path = _write_command_report(
                requested_report,
                payload,
                report,
                overwrite=args.overwrite,
            )
        output_payload = {
            "command_report": payload,
            "report_path": None if report_path is None else str(report_path),
        }
        if args.json:
            stdout.write(
                json.dumps(
                    output_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            )
        else:
            stdout.write(
                "diePi run comparison: "
                f"{payload['status']}\n"
                f"ledger: {report.ledger_status.value}\n"
                "metric definitions: "
                f"{report.metric_definition_status.value}\n"
                f"scope equal: {str(report.scope_equal).lower()}\n"
                f"core comparison payload sha256: {report.report_sha256}\n"
                "command report payload sha256: "
                f"{payload['command_report_sha256']}\n"
            )
            if report_path is not None:
                stdout.write(f"report: {report_path}\n")
            if not payload["artifact_trust"]["baseline_verified"] or not payload[
                "artifact_trust"
            ]["candidate_verified"]:
                stderr.write(
                    "警告: 至少一侧是显式允许的未验证 legacy 目录；"
                    "比较结果不升级其可信状态。\n"
                )
        trusted = payload["artifact_trust"]["trusted_comparison"]
        return EXIT_OK if trusted and report.projection_status in (
            RunParityStatus.EXACT,
            RunParityStatus.WITHIN_TOLERANCE,
        ) else EXIT_DIFFERENT
    except (FileExistsError, FileNotFoundError, OSError, TypeError, ValueError) as exc:
        stderr.write(f"diePi run comparison: {type(exc).__name__}: {exc}\n")
        return EXIT_USAGE


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diepi compare runs",
        description=(
            "Compare two cash RunArtifacts without intersecting dates or "
            "conflating economic-ledger and metric-definition differences"
        ),
    )
    parser.add_argument("baseline", help="baseline RunArtifact v1 directory")
    parser.add_argument("candidate", help="candidate RunArtifact v1 directory")
    parser.add_argument("--baseline-label", default="baseline")
    parser.add_argument("--candidate-label", default="candidate")
    parser.add_argument("--atol", type=float, default=0.0)
    parser.add_argument("--rtol", type=float, default=0.0)
    parser.add_argument("--report", help="write a new .json or .md report")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="explicitly replace an existing report file",
    )
    parser.add_argument(
        "--allow-unverified-legacy",
        action="store_true",
        help="allow old ResultStorage directories without treating them as verified",
    )
    parser.add_argument("--json", action="store_true", help="emit full JSON report")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    return execute(_parser().parse_args(argv))


__all__ = [
    "COMMAND_REPORT_SCHEMA",
    "COMMAND_REPORT_SCHEMA_VERSION",
    "execute",
    "main",
]
