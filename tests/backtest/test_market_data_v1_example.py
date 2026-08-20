"""The published real local-market-data v1 slice is usable and reproducible."""

from __future__ import annotations

from pathlib import Path
import runpy

import numpy as np
import pandas as pd
import pytest

from diepi.artifacts import ArtifactStore
from diepi.backtest.cli.runner import run_backtest
from diepi.backtest.data.cache_manager import CacheManager
from diepi.backtest.data.data_provider import DataProvider
from diepi.backtest.data.dataset_manifest import DatasetManifest
from diepi.backtest.data.extraction_service import extract_local_data
from diepi.backtest.data.validation_service import validate_local_data
from diepi.backtest.engine.backtest_engine import BacktestEngine
from diepi.backtest.rulebook import DEFAULT_RULEBOOK
from diepi.backtest.strategy.base import Strategy
from tests.backtest.auction_test_policy import EXPLICIT_TEST_AUCTION_POLICY


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = ROOT / "examples" / "market_data_v1"
DATA_ROOT = EXAMPLE_ROOT / "data"
GENERATOR = EXAMPLE_ROOT / "generate.py"
SYMBOLS = ("600000.SH", "000001.SZ", "510300.SH", "159915.SZ")
FACTOR_ANCHORS = {
    "600000.SH": "20100104",
    "000001.SZ": "20100104",
    "510300.SH": "20120528",
    "159915.SZ": "20111209",
}
FACTOR_CHANGES_IN_SCOPE = {
    "600000.SH": (),
    "000001.SZ": ("20260612",),
    "510300.SH": ("20260119", "20260626"),
    "159915.SZ": (),
}
DAILY_COLUMNS = (
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "amount",
)
MINUTE_COLUMNS = (
    "ts_code",
    "trade_time",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "amount",
)
FACTOR_COLUMNS = ("ts_code", "trade_date", "adj_factor")
METADATA_COLUMNS = (
    "ts_code",
    "symbol",
    "name",
    "list_date",
    "delist_date",
    "list_status",
)


def _directories(symbol: str):
    if symbol.startswith(("5", "15", "16", "18")):
        return (
            "etf_daily",
            "etf_daily_raw",
            "etf_minute",
            "etf_minute_raw",
            "etf_adj_factor",
        )
    return "daily", "daily_raw", "minute", "minute_raw", "adj_factor"


def _market_path(directory: str, symbol: str, *, minute: bool = False) -> Path:
    suffix = Path(symbol) / "2026.parquet" if minute else Path(f"{symbol}.parquet")
    return DATA_ROOT / "parquet" / "timeseries" / directory / suffix


def _read_lanes(symbol: str):
    daily_dir, raw_daily_dir, minute_dir, raw_minute_dir, factor_dir = (
        _directories(symbol)
    )
    return {
        "daily": pd.read_parquet(_market_path(daily_dir, symbol)),
        "daily_raw": pd.read_parquet(_market_path(raw_daily_dir, symbol)),
        "minute": pd.read_parquet(
            _market_path(minute_dir, symbol, minute=True)
        ),
        "minute_raw": pd.read_parquet(
            _market_path(raw_minute_dir, symbol, minute=True)
        ),
        "factor": pd.read_parquet(_market_path(factor_dir, symbol)),
    }


def _assert_float64_fields(frame: pd.DataFrame, fields) -> None:
    for field in fields:
        assert frame[field].dtype == np.dtype("float64")


def test_committed_real_slice_schema_layout_and_manifest_are_exact():
    manifest = DatasetManifest.read(DATA_ROOT / "diepi_dataset.json")
    assert manifest.dataset_kind == "user_supplied"
    assert manifest.dataset_id == "diepi-local-market-data-v1-public-real-2026h1"
    assert manifest.symbols == SYMBOLS
    assert (manifest.start_date, manifest.end_date) == (
        "20260101",
        "20260630",
    )
    assert len(manifest.files) == 22

    metadata_symbols = []
    for metadata_path in (
        DATA_ROOT / "parquet/metadata/stock/basic.parquet",
        DATA_ROOT / "parquet/metadata/etf/basic.parquet",
    ):
        metadata = pd.read_parquet(metadata_path)
        assert tuple(metadata.columns) == METADATA_COLUMNS
        assert all(pd.api.types.is_string_dtype(dtype) for dtype in metadata.dtypes)
        assert not metadata["name"].str.contains("SYNTHETIC", case=False).any()
        metadata_symbols.extend(metadata["ts_code"].tolist())
    assert tuple(metadata_symbols) == SYMBOLS

    for symbol in SYMBOLS:
        lanes = _read_lanes(symbol)
        for lane in ("daily", "daily_raw"):
            frame = lanes[lane]
            assert tuple(frame.columns) == DAILY_COLUMNS
            assert len(frame) == 116
            assert frame["trade_date"].iloc[0] == "20260105"
            assert frame["trade_date"].iloc[-1] == "20260630"
            assert pd.api.types.is_string_dtype(frame["ts_code"].dtype)
            assert pd.api.types.is_string_dtype(frame["trade_date"].dtype)
            _assert_float64_fields(
                frame,
                ("open", "high", "low", "close", "pre_close", "amount"),
            )
        for lane in ("minute", "minute_raw"):
            frame = lanes[lane]
            assert tuple(frame.columns) == MINUTE_COLUMNS
            assert len(frame) == 27_956
            assert pd.api.types.is_string_dtype(frame["ts_code"].dtype)
            assert frame["trade_time"].dtype == np.dtype("datetime64[ns]")
            _assert_float64_fields(
                frame,
                ("open", "high", "low", "close", "pre_close", "amount"),
            )
        factor = lanes["factor"]
        assert tuple(factor.columns) == FACTOR_COLUMNS
        assert len(factor) == 117
        assert factor.iloc[0]["ts_code"] == symbol
        assert factor.iloc[0]["trade_date"] == FACTOR_ANCHORS[symbol]
        assert factor["trade_date"].iloc[1] == "20260105"
        assert factor["trade_date"].iloc[-1] == "20260630"
        assert factor["adj_factor"].dtype == np.dtype("float64")


def test_real_sessions_ticks_cross_frequency_and_factor_observations():
    for symbol in SYMBOLS:
        lanes = _read_lanes(symbol)
        raw_daily = lanes["daily_raw"]
        hfq_daily = lanes["daily"]

        price_decimals = DEFAULT_RULEBOOK.get_rule(symbol).price_decimals
        tick = 10.0 ** -price_decimals
        for frame in (raw_daily, lanes["minute_raw"]):
            for field in ("open", "high", "low", "close", "pre_close"):
                ticks = frame[field].to_numpy(dtype="float64") / tick
                np.testing.assert_allclose(ticks, np.rint(ticks), atol=1e-9)

        for lane in ("raw", "hfq"):
            daily = raw_daily if lane == "raw" else hfq_daily
            minute = lanes["minute_raw"] if lane == "raw" else lanes["minute"]
            minute = minute.assign(
                trade_date=minute["trade_time"].dt.strftime("%Y%m%d")
            )
            counts = minute.groupby("trade_date", sort=False).size()
            assert counts.eq(241).all()
            first_day = minute[minute["trade_date"] == "20260105"]
            expected_times = {"09:30", "09:31", "11:30", "13:01", "15:00"}
            assert expected_times <= set(first_day["trade_time"].dt.strftime("%H:%M"))
            assert not first_day["trade_time"].dt.strftime("%H:%M").isin(
                ("11:31", "13:00")
            ).any()

            grouped = minute.groupby("trade_date", sort=False)
            aggregate = pd.DataFrame(
                {
                    "open": grouped["open"].first(),
                    "high": grouped["high"].max(),
                    "low": grouped["low"].min(),
                    "close": grouped["close"].last(),
                    "pre_close": grouped["pre_close"].first(),
                    "amount": grouped["amount"].sum() / 1_000.0,
                }
            )
            expected = daily.set_index("trade_date")[aggregate.columns]
            assert grouped["pre_close"].nunique().eq(1).all()
            np.testing.assert_allclose(aggregate["pre_close"], expected["pre_close"])
            price_tolerance = 0.0 if lane == "raw" else 0.005000001
            np.testing.assert_allclose(
                aggregate[["open", "high", "low", "close"]],
                expected[["open", "high", "low", "close"]],
                rtol=0.0,
                atol=price_tolerance,
            )
            np.testing.assert_allclose(
                aggregate["amount"], expected["amount"], rtol=2e-8, atol=1e-6
            )

        scoped_factors = lanes["factor"].iloc[1:].reset_index(drop=True)
        changed = scoped_factors["adj_factor"].ne(
            scoped_factors["adj_factor"].shift()
        )
        changed.iloc[0] = False
        assert tuple(scoped_factors.loc[changed, "trade_date"]) == (
            FACTOR_CHANGES_IN_SCOPE[symbol]
        )


def test_daily_and_minute_dual_contracts_and_raw_validation_pass():
    for price_mode in ("dual", "raw"):
        report = validate_local_data(
            data_root=DATA_ROOT,
            symbols=SYMBOLS,
            start_date="20260101",
            end_date="20260630",
            price_mode=price_mode,
        )
        assert report.contract_ready
        assert report.manifest_status == "verified"
        assert not report.warnings
        assert [pair.aligned_rows for pair in report.pair_reports] == [116] * 4

    provider = DataProvider(
        price_mode="hfq",
        execution_price_mode="raw",
        cache_manager=CacheManager(data_root=DATA_ROOT),
    )
    for symbol in SYMBOLS:
        aligned = provider.get_aligned_pair(
            symbol,
            frequency="minute",
            start="20260101",
            end="20260630",
        )
        assert aligned.report.ok
        assert len(aligned.strategy) == 27_956
        assert aligned.strategy.index[0] == pd.Timestamp("2026-01-05 09:30")
        assert aligned.strategy.index[-1] == pd.Timestamp("2026-06-30 15:00")
        assert aligned.adjustment_factor_identity.base_date == FACTOR_ANCHORS[symbol]
        assert aligned.adjustment_factor_identity.validated_close_rows == 27_956


class _BuyAtFirstOpen(Strategy):
    def on_init(self, ctx):
        self.sent = False

    def on_before_market_open(self, ctx):
        if not self.sent:
            ctx.buy_at_open(shares=100)
            self.sent = True


def _engine(symbol: str, start_date: str, end_date: str, frequency: str):
    return BacktestEngine(
        symbol=symbol,
        start_date=start_date,
        end_date=end_date,
        initial_cash=100_000.0,
        freq=frequency,
        slippage=0.0,
        commission_rate=0.0,
        stamp_duty=0.0,
        min_commission=0.0,
        open_buy_fill_mode="open",
        open_buy_sizing="fill",
        daily_auction_liquidity=EXPLICIT_TEST_AUCTION_POLICY,
        data_root=DATA_ROOT,
    )


def test_formal_engines_execute_real_opening_bar_and_factor_change():
    minute = _engine("600000.SH", "20260105", "20260106", "minute")
    minute_result = minute.run(_BuyAtFirstOpen())
    assert minute_result.trade_count == 1
    assert minute_result.trades[0]["direction"] == "BUY"
    assert minute_result.trades[0]["simulated_time"] == (
        "2026-01-05T09:30:00.000000"
    )

    daily = _engine("000001.SZ", "20260610", "20260616", "daily")
    daily_result = daily.run(_BuyAtFirstOpen())
    assert daily_result.final_value > 0.0
    actions = [
        event
        for event in daily._context._broker.event_journal.events
        if event.event_type.value == "corporate_action"
    ]
    assert len(actions) == 1
    assert actions[0].payload.action_type == "adjustment_factor_total_return"
    factor = (
        _read_lanes("000001.SZ")["factor"]
        .set_index("trade_date")["adj_factor"]
    )
    expected_ratio = float(factor.loc["20260612"] / factor.loc["20260611"])
    assert actions[0].payload.attributes.to_dict()["ratio"] == pytest.approx(
        expected_ratio
    )


def test_canonical_ma_strategy_buys_and_sells_stock_dual_and_etf_raw(tmp_path):
    strategy = ROOT / "examples" / "ma_cross_strategy.py"
    for symbol, price_mode in (("600000.SH", "dual"), ("510300.SH", "raw")):
        output = run_backtest(
            strategy_file=str(strategy),
            start_date="20260101",
            end_date="20260630",
            initial_cash=1_000_000.0,
            data_root=DATA_ROOT,
            output_dir=tmp_path / symbol.replace(".", "_"),
            run_name=f"ma-{symbol}-{price_mode}",
            pool_symbols=[symbol],
            price_mode=price_mode,
            stamp_duty=0.0,
            commission=0.0,
            min_commission=0.0,
            slippage=0.0,
            daily_open_cap_yuan=1_000_000_000.0,
            open_buy_fill_mode="open",
            open_buy_sizing="fill",
            verbose=False,
        )
        artifact = ArtifactStore.load(output["artifact_dir"])
        assert output["result_contract"]["status"] == "SUCCESS"
        assert output["rankable"]
        assert {trade["direction"] for trade in artifact.result.trades} == {
            "BUY",
            "SELL",
        }


def test_builder_is_idempotent_and_refuses_source_or_output_ambiguity(tmp_path):
    namespace = runpy.run_path(str(GENERATOR))
    destination = tmp_path / "rebuilt"
    rebuilt = namespace["generate_dataset"](DATA_ROOT, destination)
    committed = DatasetManifest.read(DATA_ROOT / "diepi_dataset.json")
    assert rebuilt.to_json() == committed.to_json()
    with pytest.raises(FileExistsError, match="destination already exists"):
        namespace["generate_dataset"](DATA_ROOT, destination)
    with pytest.raises(ValueError, match="source data root"):
        namespace["generate_dataset"](tmp_path / "missing", tmp_path / "other")

    readme = (EXAMPLE_ROOT / "README.md").read_text(encoding="utf-8")
    generator = GENERATOR.read_text(encoding="utf-8")
    for statement in (
        "真实历史行情切片",
        "维护者决定继续",
        "不是第三方再分发授权证明",
        "不构成投资建议",
        "本地只读切片构建器",
        "不访问网络",
        "Apache-2.0",
    ):
        assert statement in readme
    assert "synthetic" not in readme.lower()
    assert "synthetic" not in generator.lower()
    assert ":\\" not in readme
    assert ":\\" not in generator


def test_real_example_can_be_extracted_with_portable_basic_metadata(tmp_path):
    extracted = extract_local_data(
        source_data_root=DATA_ROOT,
        workspace=tmp_path / "extracted",
        symbols=SYMBOLS,
        # The extractor includes one previous trading day; starting on the
        # second stored session keeps that warm-up row inside this H1 slice.
        start_date="20260106",
        end_date="20260630",
        include_metadata=True,
    )
    assert extracted.validation_report.contract_ready
    assert extracted.validation_report.manifest_status == "verified"
    assert extracted.scope.metadata_included
