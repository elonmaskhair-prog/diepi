"""CLI adapter for creating a private slice of user-supplied local data."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence, Tuple

from ..backtest.data.extraction_service import extract_local_data


def parse_symbols(values: Sequence[str]) -> Tuple[str, ...]:
    symbols = []
    for value in values or ():
        symbols.extend(part.strip() for part in value.split(",") if part.strip())
    if not symbols:
        raise ValueError("at least one symbol is required")
    return tuple(symbols)


def configure_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument(
        "--source-data-root",
        required=True,
        help="existing local data root to read; it is never modified",
    )
    parser.add_argument(
        "--workspace",
        required=True,
        help="new workspace path; existing paths are never overwritten",
    )
    parser.add_argument(
        "--symbols",
        action="append",
        required=True,
        metavar="CODE[,CODE...]",
        help="stock/ETF symbol(s) in 000001.SZ form; repeatable",
    )
    parser.add_argument("--start", required=True, help="scope start YYYYMMDD")
    parser.add_argument("--end", required=True, help="scope end YYYYMMDD")
    parser.add_argument(
        "--include-metadata",
        action="store_true",
        help="include only selected stock/ETF security-master rows",
    )
    parser.add_argument(
        "--verbose-errors",
        action="store_true",
        help="include local path and backend details in error output",
    )
    parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def execute(args, *, stdout=None, stderr=None) -> int:
    output = stdout or sys.stdout
    error = stderr or sys.stderr
    try:
        symbols = parse_symbols(getattr(args, "symbols", ()))
        extracted = extract_local_data(
            source_data_root=args.source_data_root,
            workspace=args.workspace,
            symbols=symbols,
            start_date=args.start,
            end_date=args.end,
            include_metadata=bool(getattr(args, "include_metadata", False)),
        )
    except (
        FileExistsError,
        FileNotFoundError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        if isinstance(exc, FileExistsError):
            code = "DESTINATION_EXISTS"
        elif isinstance(exc, FileNotFoundError):
            code = "SOURCE_OR_PARENT_NOT_FOUND"
        elif isinstance(exc, PermissionError):
            code = "ACCESS_DENIED"
        elif isinstance(exc, (TypeError, ValueError)):
            code = "INVALID_REQUEST_OR_SOURCE_DATA"
        else:
            code = "EXTRACTION_FAILED"
        error.write(f"diePi data extract: ERROR [{code}]\n")
        if bool(getattr(args, "verbose_errors", False)):
            error.write(f"detail: {type(exc).__name__}: {exc}\n")
        return 2

    if getattr(args, "json", False):
        output.write(
            json.dumps(
                extracted.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
    else:
        output.write("diePi data extract: READY\n")
        output.write(f"workspace: {extracted.workspace}\n")
        output.write(f"data root: {extracted.data_root}\n")
        output.write(
            "scope: "
            f"{','.join(extracted.scope.symbols)} "
            f"{extracted.scope.requested_start_date}.."
            f"{extracted.scope.requested_end_date}\n"
        )
        output.write(
            "validation: PASS "
            f"({extracted.validation_report.report_sha256})\n"
        )
        output.write(
            "PRIVATE USER-SUPPLIED DATA - non-redistributable by default; "
            "no strategy signals were copied.\n"
        )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = configure_parser(
        argparse.ArgumentParser(
            prog="diepi data extract",
            description=(
                "Create a validated private daily dual-price slice from "
                "user-supplied local Parquet data"
            ),
        )
    )
    return execute(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["configure_parser", "execute", "main", "parse_symbols"]
