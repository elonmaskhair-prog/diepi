"""Validate an exact public Git tree or a no-Git filesystem candidate."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Optional, Sequence

# Running this checker from inside a pristine no-Git candidate must not mutate
# the candidate with ``tools/__pycache__`` before the exact-tree scan.
sys.dont_write_bytecode = True

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.check_release_artifacts import (
    ReleaseGateError,
    check_public_filesystem_tree,
    check_public_git_tree,
    check_public_tree,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="candidate root containing tools/public_git_allowlist.txt",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "filesystem", "git"),
        default="auto",
        help="auto selects Git only when the candidate itself contains .git",
    )
    parser.add_argument(
        "--revision",
        default="HEAD",
        help="committed Git revision to inspect in git mode",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.mode == "filesystem":
            check_public_filesystem_tree(args.project_root)
            mode = "filesystem"
        elif args.mode == "git":
            check_public_git_tree(args.project_root, args.revision)
            mode = "git"
        else:
            mode = check_public_tree(args.project_root, args.revision)
    except (OSError, ReleaseGateError) as exc:
        print(f"public tree gate failed: {exc}", file=sys.stderr)
        return 1
    print(f"PASS public {mode} tree: {args.project_root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
