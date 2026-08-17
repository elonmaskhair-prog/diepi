"""Public examples are executable product surfaces, not untested snippets."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from diepi.backtest.cli.runner import compile_strategy


ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = tuple(sorted((ROOT / "examples").glob("*.py")))


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_every_public_example_compiles_as_a_supported_strategy(path: Path):
    strategy_type = compile_strategy(path.read_text(encoding="utf-8"))
    assert isinstance(strategy_type, type)


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda path: path.name)
def test_public_examples_do_not_swallow_runtime_errors(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    broad_handlers = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:
            broad_handlers.append(node.lineno)
        elif isinstance(node.type, ast.Name) and node.type.id in {
            "Exception",
            "BaseException",
        }:
            broad_handlers.append(node.lineno)
    assert broad_handlers == [], (
        f"{path.name} silently catches broad exceptions at {broad_handlers}"
    )


def test_fixed_date_etf_example_uses_a_trading_day_for_the_exit():
    source = (ROOT / "examples" / "etf_simple_test.py").read_text(
        encoding="utf-8"
    )
    assert "20150109" in source
    assert "trade_date == '20150110'" not in source


def test_etf_2b_documented_command_contains_auction_capacity():
    source = (ROOT / "examples" / "etf_2b_reversal.py").read_text(
        encoding="utf-8"
    )
    assert "--daily-open-previous-day-ratio" in source
    assert "EXIT_TYPE" not in source
