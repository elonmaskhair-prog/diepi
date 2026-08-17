"""CLI adapter for strict, scoped, read-only local-data validation."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Optional, Sequence, Tuple

from ..backtest.data.validation_service import (
    DataValidationReport,
    validate_local_data,
    write_validation_report,
)


def parse_symbols(values: Sequence[str]) -> Tuple[str, ...]:
    symbols = []
    for value in values or ():
        symbols.extend(part.strip() for part in value.split(",") if part.strip())
    if not symbols:
        raise ValueError("at least one symbol is required")
    return tuple(symbols)


def format_validation_report(report: DataValidationReport) -> str:
    lines = [f"diePi data validate: {report.status.upper()}"]
    lines.append(
        "scope: "
        f"{','.join(report.scope.symbols)} "
        f"{report.scope.start_date}..{report.scope.end_date} "
        f"{report.scope.frequency}/{report.scope.price_mode}"
    )
    lines.append(
        f"manifest: {report.manifest_status}; dataset: {report.dataset_kind}"
    )
    for pair in report.pair_reports:
        lines.append(
            f"[{pair.status.upper()}] {pair.symbol}: "
            f"{pair.aligned_rows} aligned rows"
        )
    for issue in report.issues:
        symbol = f" ({issue.symbol})" if issue.symbol else ""
        samples = (
            f"; samples={','.join(issue.sample_keys)}"
            if issue.sample_keys
            else ""
        )
        lines.append(
            f"[{issue.severity.upper()}] {issue.code}{symbol}: "
            f"{issue.message}{samples}"
        )
    lines.append(
        "Passing proves contract readiness only; source rights, authenticity, "
        "and economic correctness are outside this check."
    )
    return "\n".join(lines) + "\n"


def configure_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--data-root", required=True, help="local data root")
    parser.add_argument(
        "--symbols",
        action="append",
        required=True,
        metavar="CODE[,CODE...]",
        help="requested symbol(s); repeatable",
    )
    parser.add_argument("--start", required=True, help="scope start YYYYMMDD")
    parser.add_argument("--end", required=True, help="scope end YYYYMMDD")
    parser.add_argument(
        "--frequency", default="daily", choices=("daily", "minute")
    )
    parser.add_argument(
        "--price-mode", default="dual", choices=("dual", "hfq", "raw")
    )
    parser.add_argument(
        "--skip-manifest",
        action="store_true",
        help="validate tables without checking diepi_dataset.json",
    )
    parser.add_argument(
        "--report",
        help="explicit path for a JSON report; validation otherwise writes nothing",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def execute(args, *, stdout=None, stderr=None) -> int:
    output = stdout or sys.stdout
    error = stderr or sys.stderr
    try:
        symbols = parse_symbols(getattr(args, "symbols", ()))
        report = validate_local_data(
            data_root=args.data_root,
            symbols=symbols,
            start_date=args.start,
            end_date=args.end,
            frequency=args.frequency,
            price_mode=args.price_mode,
            verify_manifest=not args.skip_manifest,
        )
        if args.report:
            report_path = Path(args.report).expanduser().resolve()
            if not report_path.parent.is_dir():
                raise FileNotFoundError(
                    f"report parent does not exist: {report_path.parent}"
                )
            write_validation_report(report, report_path)
        output.write(report.to_json() if args.json else format_validation_report(report))
        return 0 if report.contract_ready else 1
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        error.write(f"diePi data validate: {type(exc).__name__}: {exc}\n")
        return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = configure_parser(
        argparse.ArgumentParser(
            prog="diepi data validate",
            description="Strict, scoped, read-only validation of local market data",
        )
    )
    return execute(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "configure_parser",
    "execute",
    "format_validation_report",
    "main",
    "parse_symbols",
]
