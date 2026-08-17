"""Check local Markdown links and line anchors without network access."""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath
import re
import sys
from typing import Optional, Sequence
from urllib.parse import unquote


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
_LINE_FRAGMENT = re.compile(r"^L([1-9][0-9]*)$")
_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


class MarkdownLinkError(RuntimeError):
    """Raised when a checked local Markdown target is invalid."""


def _public_files(project_root: Path) -> frozenset[str]:
    allowlist = project_root / "tools" / "public_git_allowlist.txt"
    entries = frozenset(
        line.strip()
        for line in allowlist.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not entries:
        raise MarkdownLinkError("public Git allowlist is empty")
    return entries


def _markdown_files(project_root: Path, public_files: frozenset[str]) -> tuple[Path, ...]:
    return tuple(
        project_root / relative
        for relative in sorted(public_files)
        if relative.lower().endswith(".md")
    )


def _split_target(raw: str) -> tuple[str, str]:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    path, separator, fragment = target.partition("#")
    return unquote(path), unquote(fragment) if separator else ""


def _heading_anchors(text: str) -> frozenset[str]:
    anchors: set[str] = set()
    occurrences: dict[str, int] = {}
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match is None:
            continue
        heading = re.sub(r"<[^>]+>", "", match.group(1))
        heading = re.sub(r"[`*_~]", "", heading).strip().lower()
        base = re.sub(r"[^\w\- ]", "", heading, flags=re.UNICODE)
        base = re.sub(r"\s+", "-", base)
        suffix = occurrences.get(base, 0)
        occurrences[base] = suffix + 1
        anchors.add(base if suffix == 0 else f"{base}-{suffix}")
    return frozenset(anchors)


def check_markdown_links(project_root: Path) -> int:
    """Validate repository-local targets and ``#L<number>`` fragments."""

    project_root = project_root.resolve()
    public_files = _public_files(project_root)
    checked = 0
    findings: list[str] = []
    for source in _markdown_files(project_root, public_files):
        relative_source = source.relative_to(project_root).as_posix()
        if not source.is_file():
            findings.append(f"{relative_source}: public Markdown file is missing")
            continue
        text = source.read_text(encoding="utf-8")
        for match in _LINK.finditer(text):
            raw_target = match.group(1)
            path_text, fragment = _split_target(raw_target)
            lowered = path_text.lower()
            if lowered.startswith(("http://", "https://", "mailto:")):
                continue
            pure = PurePosixPath(path_text.replace("\\", "/")) if path_text else None
            if pure is None:
                target = source
            elif pure.is_absolute():
                findings.append(f"{relative_source}: absolute target {raw_target}")
                continue
            else:
                target = (source.parent / Path(*pure.parts)).resolve()
            try:
                target.relative_to(project_root)
            except ValueError:
                findings.append(f"{relative_source}: target escapes root {raw_target}")
                continue
            checked += 1
            if not target.is_file():
                findings.append(f"{relative_source}: missing target {raw_target}")
                continue
            relative_target = target.relative_to(project_root).as_posix()
            if relative_target not in public_files:
                findings.append(
                    f"{relative_source}: target is outside the public tree {raw_target}"
                )
                continue
            line_match = _LINE_FRAGMENT.fullmatch(fragment)
            if line_match is not None:
                requested = int(line_match.group(1))
                line_count = len(target.read_text(encoding="utf-8").splitlines())
                if requested > line_count:
                    findings.append(
                        f"{relative_source}: line anchor L{requested} "
                        f"exceeds {relative_target} ({line_count} lines)"
                    )
            elif fragment:
                if target.suffix.lower() != ".md":
                    findings.append(
                        f"{relative_source}: unsupported local fragment {raw_target}"
                    )
                elif fragment not in _heading_anchors(
                    target.read_text(encoding="utf-8")
                ):
                    findings.append(
                        f"{relative_source}: missing Markdown heading {raw_target}"
                    )
    if findings:
        raise MarkdownLinkError("Markdown link gate failed:\n  " + "\n  ".join(findings))
    return checked


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        checked = check_markdown_links(args.project_root)
    except (OSError, UnicodeError, MarkdownLinkError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"PASS Markdown links: {checked} local targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
