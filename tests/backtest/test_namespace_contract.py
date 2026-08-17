"""Contracts for the one-way migration to the ``diepi`` Python namespace."""

from __future__ import annotations

import importlib.util
import pickle
import sys


def test_delayed_futures_rule_import_uses_the_diepi_sibling_package(
    tmp_path, monkeypatch
):
    fake_package = tmp_path / "futures"
    fake_package.mkdir()
    (fake_package / "__init__.py").write_text(
        "raise AssertionError('loaded an unrelated top-level futures package')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    from diepi.backtest.rulebook import RuleBook

    rule = RuleBook().get_rule("IF2609.CFX")
    assert rule.multiplier == 300
    assert rule.margin_rate == 0.12
    assert "futures" not in sys.modules


def test_retired_top_level_modules_are_not_importable():
    assert importlib.util.find_spec("backtest") is None
    assert importlib.util.find_spec("futures") is None
    assert importlib.util.find_spec("run_backtest") is None


def test_public_imports_share_canonical_class_identity():
    import diepi
    import diepi.backtest as cash_api
    import diepi.futures as futures_api
    from diepi.backtest.engine import BacktestEngine
    from diepi.backtest.engine.backtest_engine import (
        BacktestEngine as CanonicalBacktestEngine,
    )
    from diepi.backtest.result_contract import ResultContract
    from diepi.futures.engine import FuturesEngine
    from diepi.futures.result import ResultContract as FuturesResultContract

    assert diepi.__version__
    assert cash_api.BacktestEngine is BacktestEngine is CanonicalBacktestEngine
    assert futures_api.FuturesEngine is FuturesEngine
    assert cash_api.ResultContract is ResultContract is FuturesResultContract
    assert not ({"backtest", "futures", "run_backtest"} & sys.modules.keys())


def test_pickle_payloads_only_reference_the_diepi_namespace():
    from diepi.backtest.engine.parallel_runner import _run_single_backtest
    from diepi.backtest.liquidity import (
        AuctionCapSpec,
        DailyAuctionLiquidityPolicy,
    )

    policy = DailyAuctionLiquidityPolicy(
        open_cap=AuctionCapSpec.fixed_yuan(10_000.0),
        close_cap=AuctionCapSpec.previous_day_ratio(0.05),
    )
    payload = pickle.dumps(
        {"policy": policy, "worker": _run_single_backtest},
        protocol=pickle.HIGHEST_PROTOCOL,
    )
    restored = pickle.loads(payload)

    assert restored["policy"] == policy
    assert restored["worker"] is _run_single_backtest
    assert type(restored["policy"]).__module__ == "diepi.backtest.liquidity"
    assert restored["worker"].__module__ == "diepi.backtest.engine.parallel_runner"
    assert b"diepi.backtest" in payload
    assert b"\x08backtest" not in payload
    assert b"\x07futures" not in payload
    assert b"run_backtest" not in payload
