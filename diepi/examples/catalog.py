"""Stable catalog and no-overwrite copier for installable examples."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


@dataclass(frozen=True)
class ExampleSpec:
    """One public example distributed inside the wheel."""

    name: str
    filename: str
    title: str
    description: str

    @property
    def source_path(self) -> Path:
        return Path(__file__).resolve().parent / self.filename

    def read_source(self) -> str:
        return self.source_path.read_text(encoding="utf-8")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "filename": self.filename,
            "title": self.title,
            "description": self.description,
        }


_EXAMPLES = (
    ExampleSpec(
        name="ma-cross",
        filename="ma_cross_strategy.py",
        title="MA5/MA20 crossover",
        description=(
            "Use completed daily bars to buy on an MA5 upward crossover of "
            "MA20 and sell on the downward crossover at the next open."
        ),
    ),
)


def list_examples() -> Tuple[ExampleSpec, ...]:
    """Return the deterministic public catalog."""

    return _EXAMPLES


def get_example(name: str) -> ExampleSpec:
    """Resolve a public example by its stable command name."""

    normalized = str(name).strip().lower()
    for example in _EXAMPLES:
        if example.name == normalized:
            return example
    choices = ", ".join(example.name for example in _EXAMPLES)
    raise ValueError(f"unknown example {name!r}; available: {choices}")


def copy_example(name: str, destination) -> Path:
    """Copy one example without overwriting an existing path."""

    example = get_example(name)
    target = Path(destination).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    target = target.absolute()
    if not target.parent.is_dir():
        raise FileNotFoundError(
            f"example destination parent does not exist: {target.parent}"
        )
    # Text mode ``x`` gives the command a simple, cross-platform no-overwrite
    # contract.  The copied strategy is source code, not an atomic data bundle.
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(example.read_source())
    return target


__all__ = ["ExampleSpec", "copy_example", "get_example", "list_examples"]
