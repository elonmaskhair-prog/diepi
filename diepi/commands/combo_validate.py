"""Read-only CLI validation for frozen combo replay bundles."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Any, Optional, Sequence, TextIO

from ..backtest.cli.combo_bundle import load_combo_bundle


EXIT_VALID = 0
EXIT_INVALID = 1
EXIT_USAGE = 2
EXIT_INTERNAL = 3


def _validation_report(bundle: Any) -> dict[str, Any]:
    """Return a deterministic, path-private projection of a canonical bundle."""

    return {
        "bundle": bundle.manifest_dict(),
        "status": "VALID",
    }


def _format_report(report: dict[str, Any]) -> str:
    bundle = report["bundle"]
    scope = bundle["scope"]
    rows = bundle["rows"]
    digests = bundle["sha256"]
    return (
        "diePi combo validate: VALID\n"
        f"schema: {bundle['schema']} v{bundle['schema_version']}\n"
        f"tag: {bundle['tag']}\n"
        f"scope: {scope['start_date']}..{scope['end_date']}; "
        f"symbols={len(scope['symbols'])}\n"
        f"rows: targets={rows['targets']}; "
        f"close_sells={rows['close_sells']}; daily={rows['daily']}\n"
        f"sha256: targets={digests['targets']}; "
        f"close_sells={digests['close_sells']}; daily={digests['daily']}\n"
        "read-only: no manifest was generated or overwritten\n"
    )


def _path_private_message(exc: BaseException, bundle_path: str) -> str:
    """Redact the user-supplied bundle root from loader diagnostics."""

    message = str(exc).strip() or type(exc).__name__
    supplied = Path(bundle_path).expanduser()
    candidates = set()
    if supplied.is_absolute():
        candidates.add(str(supplied))
    try:
        absolute = supplied.absolute()
    except OSError:
        absolute = supplied
    candidates.update((str(absolute), absolute.as_posix()))
    try:
        resolved = absolute.resolve(strict=False)
    except OSError:
        resolved = absolute
    candidates.update((str(resolved), resolved.as_posix()))
    for candidate in sorted(
        (value for value in candidates if value), key=len, reverse=True
    ):
        message = re.sub(
            re.escape(candidate), "<bundle>", message, flags=re.IGNORECASE
        )
    return message


def _emit_invalid(
    exc: BaseException,
    *,
    bundle_path: str,
    json_output: bool,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    error = {
        "message": _path_private_message(exc, bundle_path),
        "type": type(exc).__name__,
    }
    if json_output:
        stdout.write(
            json.dumps(
                {"error": error, "status": "INVALID"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        return
    stderr.write(
        "diePi combo validate: INVALID\n"
        f"error: {error['type']}: {error['message']}\n"
    )


def configure_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("bundle", help="frozen combo bundle directory")
    parser.add_argument(
        "--tag",
        help="tag for a legacy new_combo_*_<tag>.csv layout",
    )
    parser.add_argument("--json", action="store_true", help="emit stable JSON")
    return parser


def execute(args: Any, *, stdout: TextIO = None, stderr: TextIO = None) -> int:
    output = stdout or sys.stdout
    error = stderr or sys.stderr
    bundle_path = str(args.bundle)
    try:
        bundle = load_combo_bundle(bundle_path, tag=args.tag)
        report = _validation_report(bundle)
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        _emit_invalid(
            exc,
            bundle_path=bundle_path,
            json_output=bool(args.json),
            stdout=output,
            stderr=error,
        )
        return EXIT_INVALID
    except Exception as exc:  # unexpected validator failures have a stable code
        _emit_invalid(
            RuntimeError(f"internal validation error: {type(exc).__name__}"),
            bundle_path=bundle_path,
            json_output=bool(args.json),
            stdout=output,
            stderr=error,
        )
        return EXIT_INTERNAL

    if args.json:
        output.write(
            json.dumps(
                report,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
    else:
        output.write(_format_report(report))
    return EXIT_VALID


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = configure_parser(
        argparse.ArgumentParser(
            prog="diepi combo validate",
            description=(
                "Strictly validate a frozen combo bundle without writing files"
            ),
        )
    )
    return execute(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EXIT_INTERNAL",
    "EXIT_INVALID",
    "EXIT_USAGE",
    "EXIT_VALID",
    "configure_parser",
    "execute",
    "main",
]
