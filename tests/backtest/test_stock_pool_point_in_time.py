"""Point-in-time universe and fund-fallback regression tests."""

from types import SimpleNamespace

import pandas as pd
import pytest

import diepi.backtest.data.cache_manager as cache_manager_module
import diepi.backtest.data.stock_pool as stock_pool_module
from diepi.backtest.data.stock_pool import (
    PointInTimeUniverseError,
    PoolSource,
    StockPool,
)
from diepi.backtest.engine.portfolio_context import PortfolioContext
from diepi.backtest.engine.portfolio_engine import PortfolioEngine
from diepi.backtest.outcome import OutcomeTracker
from diepi.backtest.result_contract import ResultStatus
from diepi.backtest.strategy.portfolio_strategy import PortfolioStrategy


class _MetadataProvider:
    price_mode = "hfq"
    execution_price_mode = "raw"

    def __init__(self):
        self.stock_info = pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "name": "*ST historical-name-is-current-only",
                    "list_status": "L",
                    "list_date": "20000101",
                    "delist_date": "",
                },
                {
                    "ts_code": "000002.SZ",
                    "name": "future",
                    "list_status": "L",
                    "list_date": "20250101",
                    "delist_date": "",
                },
                {
                    "ts_code": "000003.SZ",
                    "name": "delisted-before-window",
                    "list_status": "D",
                    "list_date": "20000101",
                    "delist_date": "20171231",
                },
                {
                    "ts_code": "000004.SZ",
                    "name": "overlaps-window",
                    "list_status": "D",
                    "list_date": "2000-01-01",
                    "delist_date": "2018-06-01",
                },
                {
                    "ts_code": "000005.SZ",
                    "name": "delists-later",
                    "list_status": "D",
                    "list_date": "2000/01/01",
                    "delist_date": "20250101",
                },
            ]
        ).set_index("ts_code")
        self.industry = pd.DataFrame(
            {
                "ts_code": list(self.stock_info.index),
                "industry": ["Bank"] * len(self.stock_info),
            }
        )

    def get_stock_info(self):
        return self.stock_info.copy()

    def get_industry(self):
        return self.industry.copy()


def test_window_pool_uses_listing_interval_not_current_listing_status():
    pool = StockPool(_MetadataProvider())

    symbols = pool.get_pool(
        PoolSource.ALL_MARKET,
        exclude_st=False,
        start_date="20180101",
        end_date="20181231",
    )

    assert symbols == ["000001.SZ", "000004.SZ", "000005.SZ"]
    assert "000002.SZ" not in symbols  # listed after the requested window
    assert "000003.SZ" not in symbols  # delisted before the window


def test_daily_pool_changes_at_exclusive_delisting_boundary():
    pool = StockPool(_MetadataProvider())

    before = pool.get_pool(
        PoolSource.ALL_MARKET,
        exclude_st=False,
        as_of_date="20180531",
    )
    on_delist_date = pool.get_pool(
        PoolSource.ALL_MARKET,
        exclude_st=False,
        as_of_date="20180601",
    )

    assert "000004.SZ" in before
    assert "000004.SZ" not in on_delist_date


def test_historical_st_filter_must_not_reuse_current_name_snapshot():
    pool = StockPool(_MetadataProvider())

    with pytest.raises(PointInTimeUniverseError, match="historical ST status"):
        pool.get_pool(PoolSource.ALL_MARKET, as_of_date="20180102")

    explicit = pool.get_pool(
        PoolSource.ALL_MARKET,
        exclude_st=False,
        as_of_date="20180102",
    )
    assert "000001.SZ" in explicit


def test_industry_snapshot_is_intersected_with_listing_interval():
    pool = StockPool(_MetadataProvider())

    symbols = pool.get_pool(
        PoolSource.INDUSTRY,
        industry="Bank",
        exclude_st=False,
        as_of_date="20180102",
    )

    assert symbols == ["000001.SZ", "000004.SZ", "000005.SZ"]


def test_context_preloads_window_union_but_exposes_daily_membership():
    context = PortfolioContext(
        data_provider=_MetadataProvider(),
        pool_source=PoolSource.ALL_MARKET,
        pool_start_date="20180101",
        pool_end_date="20181231",
    )

    assert context.get_preload_stock_pool() == [
        "000001.SZ",
        "000004.SZ",
        "000005.SZ",
    ]
    context.current_date = "20180531"
    assert "000004.SZ" in context.get_stock_pool()
    context.current_date = "20180601"
    assert "000004.SZ" not in context.get_stock_pool()


def test_all_market_contract_discloses_historical_st_semantics():
    engine = PortfolioEngine(
        "20180102", "20180102", pool_source=PoolSource.ALL_MARKET
    )
    tracker = OutcomeTracker(
        expected_observation_ids=("2018-01-02",),
        coverage_unit="portfolio_trade_day",
    )
    engine._add_universe_contract_evidence(tracker)
    tracker.observe("2018-01-02", "2018-01-02")

    contract = engine._finalize_completed_outcome(tracker)

    warnings = {item.code for item in contract.warnings}
    assumptions = {item.key: item.value for item in contract.assumptions}
    assert contract.status is ResultStatus.SUCCESS
    assert "UNIVERSE_ST_HISTORY_UNAVAILABLE" in warnings
    assert assumptions["universe.st_filter"] == (
        "not_applied_historical_st_status_unavailable"
    )


def test_industry_snapshot_contract_is_structured_and_not_rankable():
    engine = PortfolioEngine(
        "20180102",
        "20180102",
        pool_source=PoolSource.INDUSTRY,
        pool_industry="Bank",
    )
    tracker = OutcomeTracker(
        expected_observation_ids=("2018-01-02",),
        coverage_unit="portfolio_trade_day",
    )
    engine._add_universe_contract_evidence(tracker)
    tracker.observe("2018-01-02", "2018-01-02")

    contract = engine._finalize_completed_outcome(tracker)

    assert contract.status is ResultStatus.PARTIAL
    assert contract.reason.code == "POINT_IN_TIME_INDUSTRY_UNAVAILABLE"
    assert not contract.is_rankable
    assert "UNIVERSE_INDUSTRY_SNAPSHOT_BIAS" in {
        item.code for item in contract.warnings
    }


def test_fund_fallback_accepts_all_supported_exchange_prefixes(monkeypatch):
    empty_parquet = SimpleNamespace(get_etf_symbols=lambda: [])
    monkeypatch.setattr(
        cache_manager_module,
        "CacheManager",
        lambda: SimpleNamespace(_parquet=empty_parquet),
    )
    monkeypatch.setattr(stock_pool_module.os.path, "exists", lambda _: True)
    monkeypatch.setattr(
        stock_pool_module.os,
        "listdir",
        lambda _: [
            "501018.SH.parquet",
            "508000.SH.parquet",
            "517001.SH.parquet",
            "560001.SH.parquet",
            "589000.SH.parquet",
            "159915.SZ.parquet",
            "160105.SZ.parquet",
            "180101.SZ.parquet",
            "600000.SH.parquet",
            "000001.SZ.parquet",
        ],
    )
    pool = StockPool(_MetadataProvider())
    monkeypatch.setattr(pool, "_resolve_daily_dir", lambda: "daily")

    symbols = pool.get_pool(PoolSource.ETF)

    assert symbols == [
        "159915.SZ",
        "160105.SZ",
        "180101.SZ",
        "501018.SH",
        "508000.SH",
        "517001.SH",
        "560001.SH",
        "589000.SH",
    ]


def test_historical_pool_fails_closed_without_listing_metadata():
    provider = SimpleNamespace(
        price_mode="hfq",
        execution_price_mode="raw",
        get_stock_info=lambda: pd.DataFrame(
            {"name": ["unknown"]}, index=pd.Index(["000001.SZ"], name="ts_code")
        ),
    )

    with pytest.raises(PointInTimeUniverseError, match="point-in-time fields"):
        StockPool(provider).get_pool(
            PoolSource.ALL_MARKET,
            exclude_st=False,
            as_of_date="20180102",
        )


def test_engine_failure_contract_records_unavailable_point_in_time_metadata():
    provider = SimpleNamespace(
        price_mode="hfq",
        execution_price_mode="raw",
        get_trade_days_between=lambda _start, _end: ["20180102"],
        get_stock_info=lambda: pd.DataFrame(
            {"name": ["unknown"]}, index=pd.Index(["000001.SZ"], name="ts_code")
        ),
    )
    context = PortfolioContext(
        data_provider=provider,
        pool_source=PoolSource.ALL_MARKET,
        pool_start_date="20180101",
        pool_end_date="20181231",
    )
    engine = PortfolioEngine(
        "20180101", "20181231", pool_source=PoolSource.ALL_MARKET
    )
    engine._data_provider = provider
    engine._context = context
    engine._init_engine = lambda: None

    with pytest.raises(PointInTimeUniverseError):
        engine.run(PortfolioStrategy())

    assert engine.last_result_contract.status is ResultStatus.FAILED
    assert "POINT_IN_TIME_UNIVERSE_UNAVAILABLE" in {
        item.code for item in engine.last_result_contract.warnings
    }
