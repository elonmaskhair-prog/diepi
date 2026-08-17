"""CLI adapter for listing and copying installable strategy examples."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Optional, Sequence

from ..examples import copy_example, get_example, list_examples


def configure_parser(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    subparsers = parser.add_subparsers(dest="examples_command", required=True)
    list_parser = subparsers.add_parser("list", help="list installable examples")
    list_parser.add_argument("--json", action="store_true", help="emit JSON")

    copy_parser = subparsers.add_parser("copy", help="copy an example locally")
    copy_parser.add_argument("name", help="stable example name")
    copy_parser.add_argument(
        "destination",
        nargs="?",
        help="new strategy path (default: the example filename)",
    )
    copy_parser.add_argument("--json", action="store_true", help="emit JSON")
    return parser


def execute(args, *, stdout=None, stderr=None) -> int:
    output = stdout or sys.stdout
    error = stderr or sys.stderr
    if args.examples_command == "list":
        examples = list_examples()
        if args.json:
            output.write(
                json.dumps(
                    {"examples": [example.to_dict() for example in examples]},
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                )
                + "\n"
            )
        else:
            for example in examples:
                output.write(
                    f"{example.name}\t{example.filename}\t{example.description}\n"
                )
        return 0

    try:
        example = get_example(args.name)
        destination = args.destination or example.filename
        target = copy_example(example.name, destination)
    except (FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        error.write(f"diePi examples: {type(exc).__name__}: {exc}\n")
        return 2

    payload = {**example.to_dict(), "path": str(target)}
    if args.json:
        output.write(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )
    else:
        output.write(f"copied {example.name}: {target}\n")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = configure_parser(
        argparse.ArgumentParser(
            prog="diepi examples",
            description="List or copy installable diePi strategy examples",
        )
    )
    return execute(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["configure_parser", "execute", "main"]
