"""CLI publication tests for the verified RunArtifact boundary."""

from pathlib import Path
import importlib
import shutil

import pandas as pd
import pytest

from diepi.artifacts import ArtifactStore
from diepi.backtest.broker.order import OrderStatus
from diepi.backtest.cli.runner import run_backtest
from diepi.backtest.engine.portfolio_engine import PortfolioEngine
from diepi.backtest.result_contract import ResultStatus
from diepi.backtest.ui.worker import load_gui_run
from diepi.demo import generate_synthetic_demo


STOCK = "600000.SH"
T0_ETF = "511010.SH"
MIXED_START = "20240102"
MIXED_END = "20240103"

_MIXED_STRATEGY_SOURCE = '''\
from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy

STOCK = "600000.SH"
T0_ETF = "511010.SH"


class MixedCashStrategy(PortfolioStrategy):
    def __init__(self):
        super().__init__()
        self.stock_same_day_sell = None
        self.etf_same_day_sell = None

    def on_before_market_open(self, ctx):
        if ctx.current_date == "20240102":
            ctx.buy_at_open(STOCK, shares=1000, note="mixed-stock-buy")
            ctx.buy_at_open(T0_ETF, shares=1000, note="mixed-etf-buy")
        elif ctx.current_date == "20240103":
            ctx.sell_at_open(STOCK, shares=1000, note="mixed-stock-sell")
        return [STOCK, T0_ETF]

    def on_after_open(self, ctx, bars):
        if ctx.current_date == "20240102":
            self.stock_same_day_sell = ctx.sell_at_close(
                STOCK, shares=1000, note="stock-t1-probe"
            )
            self.etf_same_day_sell = ctx.sell_at_close(
                T0_ETF, shares=1000, note="etf-t0-sell"
            )
'''


def _write_mixed_raw_market_data(root: Path) -> None:
    rows = {
        STOCK: (
            "daily_raw",
            (
                (MIXED_START, 10.05, 10.05, 10.05, 10.05, 10.05),
                (MIXED_END, 10.15, 10.15, 10.15, 10.15, 10.05),
            ),
        ),
        T0_ETF: (
            "etf_daily_raw",
            (
                (MIXED_START, 3.005, 3.015, 3.005, 3.015, 3.005),
                (MIXED_END, 3.020, 3.020, 3.020, 3.020, 3.015),
            ),
        ),
    }
    for symbol, (directory, values) in rows.items():
        target = (
            root / "parquet" / "timeseries" / directory
            / f"{symbol}.parquet"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(
            [
                {
                    "ts_code": symbol,
                    "trade_date": date,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "pre_close": pre_close,
                    "amount": 1_000_000.0,
                }
                for date, open_price, high, low, close, pre_close in values
            ]
        ).to_parquet(target, index=False)


def test_cli_publishes_one_verified_atomic_artifact(tmp_path):
    demo = generate_synthetic_demo(tmp_path / "demo")
    output = run_backtest(
        strategy_file=str(demo.strategy_file),
        start_date=demo.manifest.start_date,
        end_date=demo.manifest.end_date,
        initial_cash=1_000_000.0,
        data_root=demo.data_root,
        output_dir=tmp_path / "results",
        run_name="verified-run",
        pool_symbols=[demo.manifest.symbols[0]],
        daily_open_previous_day_ratio=0.1,
        verbose=False,
    )

    artifact = Path(output["artifact_dir"])
    loaded = ArtifactStore.load(artifact)
    assert output["artifact_verified"] is True
    assert output["rankable"] is True
    assert loaded.artifact_verified is True
    assert loaded.is_rankable is True
    assert loaded.result.result_contract == loaded.outcome.result_contract
    assert loaded.result.target_execution is not None
    assert loaded.result.cash_audit is not None
    assert loaded.provenance.data_identity_level == "content_sha256"
    assert loaded.provenance.sources[0].logical_path == "diepi_dataset.json"
    assert {
        source.logical_path for source in loaded.provenance.sources
        if source.kind == 'market_data_file'
    } == {
        'parquet/timeseries/adj_factor/000001.SZ.parquet',
        'parquet/timeseries/daily/000001.SZ.parquet',
        'parquet/timeseries/daily_raw/000001.SZ.parquet',
    }
    assert dict(loaded.config)["strategy_file"] == "inputs/strategy.py"
    assert (artifact / "inputs" / "strategy.py").is_file()
    assert (artifact / "summary.json").is_file()
    assert (artifact / "equity_curve.csv").is_file()


def test_raw_minimal_cli_artifact_without_dataset_manifest_is_gui_verifiable(
    tmp_path,
):
    demo = generate_synthetic_demo(tmp_path / 'raw-minimal')
    (demo.data_root / 'diepi_dataset.json').unlink()
    shutil.rmtree(demo.data_root / 'parquet' / 'timeseries' / 'daily')
    shutil.rmtree(demo.data_root / 'parquet' / 'timeseries' / 'adj_factor')

    output = run_backtest(
        strategy_file=str(demo.strategy_file),
        start_date=demo.manifest.start_date,
        end_date=demo.manifest.end_date,
        initial_cash=1_000_000.0,
        data_root=demo.data_root,
        output_dir=tmp_path / 'results',
        run_name='raw-minimal-no-manifest',
        pool_symbols=[demo.manifest.symbols[0]],
        price_mode='raw',
        daily_open_previous_day_ratio=0.1,
        verbose=False,
    )

    loaded = ArtifactStore.load(output['artifact_dir'])
    market_sources = [
        source for source in loaded.provenance.sources
        if source.kind == 'market_data_file'
    ]
    assert [source.logical_path for source in market_sources] == [
        'parquet/timeseries/daily_raw/000001.SZ.parquet'
    ]
    assert all(
        source.kind != 'dataset_manifest'
        for source in loaded.provenance.sources
    )

    gui = load_gui_run(output['artifact_dir'])
    assert gui.artifact_verified is True
    assert gui.config['price_mode'] == 'raw'
    assert gui.config['symbols'] == [demo.manifest.symbols[0]]
    assert gui.provenance == loaded.provenance


def test_formal_runner_mixes_stock_and_etf_in_one_cash_portfolio(
    tmp_path, monkeypatch,
):
    """One public run keeps mixed instruments in one audited cash account."""

    data_root = tmp_path / "mixed-data"
    _write_mixed_raw_market_data(data_root)
    strategy_file = tmp_path / "mixed_strategy.py"
    strategy_file.write_text(_MIXED_STRATEGY_SOURCE, encoding="utf-8")

    captured = {}
    real_run = PortfolioEngine.run

    def capture_engine(self, strategy):
        captured["engine"] = self
        captured["strategy"] = strategy
        return real_run(self, strategy)

    monkeypatch.setattr(PortfolioEngine, "run", capture_engine)
    output = run_backtest(
        strategy_file=str(strategy_file),
        start_date=MIXED_START,
        end_date=MIXED_END,
        initial_cash=100_000.0,
        data_root=data_root,
        output_dir=tmp_path / "results",
        run_name="mixed-stock-etf",
        pool_symbols=[STOCK, T0_ETF],
        price_mode="raw",
        slippage=0.0,
        commission=0.0,
        stamp_duty="auto",
        transfer_fee_rate=0.0,
        min_commission=0.0,
        daily_open_cap_yuan=1_000_000_000.0,
        daily_close_cap_yuan=1_000_000_000.0,
        open_buy_fill_mode="open",
        open_buy_sizing="fill",
        verbose=False,
    )

    loaded = ArtifactStore.load(output["artifact_dir"])
    result = loaded.result
    assert loaded.artifact_verified is True
    assert loaded.is_rankable is True
    assert result.result_contract.status is ResultStatus.SUCCESS
    assert result.trade_count == 4
    assert result.final_value == pytest.approx(100_104.92)
    assert sum(trade["cash_delta"] for trade in result.trades) == pytest.approx(
        result.final_value - result.initial_cash
    )

    # The formal artifact proves that each symbol came from its own raw route;
    # no stock/ETF directory fallback or bundled sample data is involved.
    assert {
        source.logical_path
        for source in loaded.provenance.sources
        if source.kind == "market_data_file"
    } == {
        f"parquet/timeseries/daily_raw/{STOCK}.parquet",
        f"parquet/timeseries/etf_daily_raw/{T0_ETF}.parquet",
    }

    trades = {
        (trade["symbol"], trade["direction"]): trade
        for trade in result.trades
    }
    assert set(trades) == {
        (STOCK, "BUY"),
        (STOCK, "SELL"),
        (T0_ETF, "BUY"),
        (T0_ETF, "SELL"),
    }
    assert trades[(STOCK, "BUY")]["price"] == pytest.approx(10.05)
    assert trades[(T0_ETF, "BUY")]["price"] == pytest.approx(3.005)
    assert trades[(STOCK, "SELL")]["time"] == MIXED_END
    assert trades[(STOCK, "SELL")]["t0"] is False
    assert trades[(STOCK, "SELL")]["stamp_duty"] == pytest.approx(5.08)
    assert trades[(T0_ETF, "SELL")]["time"] == MIXED_START
    assert trades[(T0_ETF, "SELL")]["t0"] is True
    assert trades[(T0_ETF, "SELL")]["stamp_duty"] == 0.0
    for trade in result.trades:
        assert trade["commission"] == 0.0
        assert trade["transfer_fee"] == 0.0

    strategy = captured["strategy"]
    assert strategy.stock_same_day_sell.status is OrderStatus.REJECTED
    assert "failed to freeze shares" in (
        strategy.stock_same_day_sell.reject_reason
    )
    assert strategy.etf_same_day_sell.status is OrderStatus.FILLED

    # These are the exact per-symbol rules used by the captured engine/Broker.
    # Half-tick boundaries make a two-decimal stock and three-decimal ETF
    # observably different rather than merely checking rule metadata.
    broker = captured["engine"]._context._broker
    assert broker._price_decimals(STOCK) == 2
    assert broker._price_decimals(T0_ETF) == 3
    assert broker._get_limit_pct(STOCK, MIXED_START) == pytest.approx(0.10)
    assert broker._get_limit_pct(T0_ETF, MIXED_START) == pytest.approx(0.10)
    assert broker._limit_up_price(STOCK, 10.05) == pytest.approx(11.06)
    assert broker._limit_down_price(STOCK, 10.05) == pytest.approx(9.05)
    assert broker._limit_up_price(T0_ETF, 3.005) == pytest.approx(3.306)
    assert broker._limit_down_price(T0_ETF, 3.005) == pytest.approx(2.705)
    assert broker._is_t0(STOCK) is False
    assert broker._is_t0(T0_ETF) is True


def test_cli_fails_closed_when_market_files_change_during_run(
    tmp_path, monkeypatch,
):
    runner_module = importlib.import_module('diepi.backtest.cli.runner')
    demo = generate_synthetic_demo(tmp_path / 'changing-demo')
    real_capture = runner_module._source_fingerprints
    captures = 0

    def changing_capture(*args, **kwargs):
        nonlocal captures
        captures += 1
        value = real_capture(*args, **kwargs)
        return value if captures == 1 else ()

    monkeypatch.setattr(
        runner_module, '_source_fingerprints', changing_capture
    )

    with pytest.raises(OSError, match='MARKET_DATA_CHANGED_DURING_RUN'):
        run_backtest(
            strategy_file=str(demo.strategy_file),
            start_date=demo.manifest.start_date,
            end_date=demo.manifest.end_date,
            initial_cash=1_000_000.0,
            data_root=demo.data_root,
            output_dir=tmp_path / 'results',
            run_name='changed-during-run',
            pool_symbols=[demo.manifest.symbols[0]],
            daily_open_previous_day_ratio=0.1,
            verbose=False,
        )

    failed = ArtifactStore.load(tmp_path / 'results' / 'changed-during-run')
    assert failed.outcome.result_contract.status.value == 'FAILED'
    assert failed.outcome.error.phase == 'data_stability_check'
    assert all(
        source.kind != 'market_data_file'
        for source in failed.provenance.sources
    )


def test_cli_exception_publishes_structured_failed_artifact(tmp_path):
    strategy = tmp_path / "broken.py"
    strategy.write_text("def on_init(:\n    pass\n", encoding="utf-8")
    results = tmp_path / "results"

    with pytest.raises(SyntaxError):
        run_backtest(
            strategy_file=str(strategy),
            start_date="20240102",
            end_date="20240103",
            output_dir=results,
            run_name="failed-run",
            pool_symbols=["000001.SZ"],
            verbose=False,
        )

    loaded = ArtifactStore.load(results / "failed-run")
    assert loaded.artifact_verified is True
    assert loaded.is_rankable is False
    assert loaded.outcome.result_contract.status.value == "FAILED"
    assert loaded.outcome.error.category.value == "STRATEGY"
    assert loaded.outcome.error.phase == "strategy_compile"
    assert b"SyntaxError" in loaded.read_bytes("traceback")
    assert (results / "failed-run" / "error.log").is_file()
