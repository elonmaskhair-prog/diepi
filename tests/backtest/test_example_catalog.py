"""Installable examples are one tested product surface."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from diepi.commands.examples import main as examples_main
from diepi.cli import dispatch
from diepi.examples import copy_example, get_example, list_examples
from diepi.examples.ma_cross_strategy import moving_average_cross


ROOT = Path(__file__).resolve().parents[2]


def test_repository_ma_example_matches_the_installable_canonical_source():
    packaged = get_example("ma-cross").source_path.read_bytes()
    repository = (ROOT / "examples" / "ma_cross_strategy.py").read_bytes()
    assert repository == packaged


def test_ma_example_detects_strict_up_and_down_crosses():
    up = pd.Series([10.0] * 20 + [11.0])
    down = pd.Series([10.0] * 15 + [12.0] * 5 + [1.0])

    assert moving_average_cross(up) == (True, False)
    assert moving_average_cross(down) == (False, True)
    assert moving_average_cross(pd.Series([10.0] * 20)) == (False, False)


def test_example_catalog_copy_is_exact_and_never_overwrites(tmp_path):
    assert [example.name for example in list_examples()] == ["ma-cross"]
    destination = tmp_path / "strategy.py"
    copied = copy_example("ma-cross", destination)
    assert copied == destination
    assert destination.read_bytes() == get_example("ma-cross").source_path.read_bytes()

    try:
        copy_example("ma-cross", destination)
    except FileExistsError:
        pass
    else:
        raise AssertionError("copy_example overwrote an existing strategy")


def test_examples_cli_lists_and_copies_machine_readable(tmp_path, capsys):
    assert dispatch(["examples", "list", "--json"]) == 0
    listed = capsys.readouterr().out
    assert '"name": "ma-cross"' in listed

    destination = tmp_path / "ma.py"
    assert examples_main(
        ["copy", "ma-cross", str(destination), "--json"]
    ) == 0
    copied = capsys.readouterr().out
    assert '"name": "ma-cross"' in copied
    assert destination.is_file()
