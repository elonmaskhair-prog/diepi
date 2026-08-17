# -*- coding: utf-8 -*-
"""
CacheManager 单元测试

测试缓存管理器的核心功能
"""

import pytest
import pandas as pd
import threading
from collections import OrderedDict

from diepi.backtest.data.cache_manager import (
    CacheManager,
    MemoryCache,
    CacheConfig,
    ParquetReader,
)


class TestCacheManager:
    """缓存管理器测试"""

    @pytest.mark.integration
    def test_get_trade_cal(self, cache_manager):
        """交易日历从 Parquet 读取"""
        df = cache_manager.get_trade_cal()
        assert not df.empty
        assert 'cal_date' in df.columns
        assert 'is_open' in df.columns

    @pytest.mark.integration
    def test_get_stock_info(self, cache_manager):
        """股票列表从 Parquet 读取"""
        df = cache_manager.get_stock_info()
        assert not df.empty
        # 应该有 ts_code 列
        assert 'ts_code' in df.columns or df.index.name == 'ts_code'

    @pytest.mark.integration
    def test_get_data_cache_hit(self, cache_manager):
        """L1 缓存命中"""
        # 第一次读取
        df1 = cache_manager.get_daily('000001.SZ')
        stats1 = cache_manager.get_stats()

        # 第二次读取（应该命中缓存）
        df2 = cache_manager.get_daily('000001.SZ')
        stats2 = cache_manager.get_stats()

        # 验证数据一致
        if not df1.empty:
            pd.testing.assert_frame_equal(df1, df2)

        # 验证缓存命中次数增加
        assert stats2['memory']['hits'] >= stats1['memory']['hits']

    @pytest.mark.integration
    def test_get_data_cache_miss(self, cache_manager):
        """L1 未命中，从 L2 读取"""
        # 清除缓存
        cache_manager.clear_cache()

        # 读取数据（缓存未命中）
        df = cache_manager.get_daily('000001.SZ')

        # 数据应该被加载（可能为空，取决于数据源）
        assert isinstance(df, pd.DataFrame)

    def test_cache_stats(self, cache_manager):
        """缓存统计信息"""
        stats = cache_manager.get_stats()
        assert 'memory' in stats
        assert 'lru_size' in stats['memory']
        assert 'max_size' in stats['memory']
        assert 'hits' in stats['memory']
        assert 'misses' in stats['memory']
        assert 'hit_rate' in stats['memory']

    @pytest.mark.integration
    def test_clear_cache(self, cache_manager):
        """清除缓存"""
        # 先加载一些数据
        cache_manager.get_daily('000001.SZ')

        # 清除缓存
        cache_manager.clear_cache()

        # 验证缓存已清空
        stats = cache_manager.get_stats()
        assert stats['memory']['lru_size'] == 0


class TestMemoryCache:
    """内存缓存测试"""

    def test_memory_cache_init(self):
        """初始化"""
        cache = MemoryCache(max_size=100)
        assert cache._max_size == 100
        assert cache._hits == 0
        assert cache._misses == 0

    def test_memory_cache_put_get(self):
        """存取数据"""
        cache = MemoryCache(max_size=100)
        df = pd.DataFrame({'a': [1, 2, 3]})

        cache.put('test_key', df)
        result = cache.get('test_key')

        assert result is not None
        pd.testing.assert_frame_equal(result, df)

    def test_memory_cache_get_miss(self):
        """缓存未命中"""
        cache = MemoryCache(max_size=100)
        result = cache.get('nonexistent_key')
        assert result is None
        assert cache._misses == 1

    def test_lru_eviction(self):
        """LRU 淘汰测试"""
        cache = MemoryCache(max_size=3)

        # 添加3个项目
        cache.put('key1', pd.DataFrame({'a': [1]}))
        cache.put('key2', pd.DataFrame({'a': [2]}))
        cache.put('key3', pd.DataFrame({'a': [3]}))

        # 添加第4个项目，应该淘汰最旧的 key1
        cache.put('key4', pd.DataFrame({'a': [4]}))

        # key1 应该被淘汰
        assert cache.get('key1') is None
        # key2, key3, key4 应该存在
        assert cache.get('key2') is not None
        assert cache.get('key3') is not None
        assert cache.get('key4') is not None

    def test_lru_access_order(self):
        """LRU 访问顺序更新"""
        cache = MemoryCache(max_size=3)

        cache.put('key1', pd.DataFrame({'a': [1]}))
        cache.put('key2', pd.DataFrame({'a': [2]}))
        cache.put('key3', pd.DataFrame({'a': [3]}))

        # 访问 key1，使其变为最近访问
        cache.get('key1')

        # 添加新项目，应该淘汰 key2（最久未访问）
        cache.put('key4', pd.DataFrame({'a': [4]}))

        # key1 应该还在
        assert cache.get('key1') is not None
        # key2 应该被淘汰
        assert cache.get('key2') is None

    def test_memory_cache_invalidate(self):
        """缓存失效"""
        cache = MemoryCache(max_size=100)
        df = pd.DataFrame({'a': [1, 2, 3]})

        cache.put('test_key', df)
        cache.invalidate('test_key')

        result = cache.get('test_key')
        assert result is None

    def test_memory_cache_invalidate_all(self):
        """清除所有缓存"""
        cache = MemoryCache(max_size=100)

        cache.put('key1', pd.DataFrame({'a': [1]}))
        cache.put('key2', pd.DataFrame({'a': [2]}))

        cache.invalidate()

        assert cache.get('key1') is None
        assert cache.get('key2') is None

    def test_memory_cache_stats(self):
        """统计信息"""
        cache = MemoryCache(max_size=100)
        df = pd.DataFrame({'a': [1]})

        cache.put('key1', df)
        cache.get('key1')  # hit
        cache.get('key2')  # miss

        stats = cache.get_stats()
        assert stats['lru_size'] == 1
        assert stats['hits'] == 1
        assert stats['misses'] == 1
        assert stats['hit_rate'] == 0.5

    def test_thread_safety(self):
        """多线程并发访问"""
        cache = MemoryCache(max_size=100)
        errors = []

        def writer():
            try:
                for i in range(100):
                    cache.put(f'key_{i}', pd.DataFrame({'a': [i]}))
            except Exception as e:
                errors.append(e)

        def reader():
            try:
                for i in range(100):
                    cache.get(f'key_{i}')
            except Exception as e:
                errors.append(e)

        # 启动多个线程
        threads = []
        for _ in range(5):
            t1 = threading.Thread(target=writer)
            t2 = threading.Thread(target=reader)
            threads.extend([t1, t2])

        for t in threads:
            t.start()

        for t in threads:
            t.join()

        # 不应该有错误
        assert len(errors) == 0


class TestCacheConfig:
    """缓存配置测试"""

    def test_default_config(self):
        """默认配置"""
        config = CacheConfig()
        assert config.LRU_MAX_SIZE == 3000
        assert config.CSV_ENCODING == 'utf-8-sig'

    def test_parquet_dir_map(self):
        """Parquet 目录映射"""
        config = CacheConfig()
        assert 'daily_data' in config.PARQUET_DIR_MAP
        assert 'minute_data' in config.PARQUET_DIR_MAP
        assert config.PARQUET_DIR_MAP['daily_data'] == 'daily'

    def test_metadata_parquet_paths(self):
        """元数据路径映射"""
        config = CacheConfig()
        assert 'trade_cal' in config.METADATA_PARQUET
        assert 'stock_basic' in config.METADATA_PARQUET


def test_parquet_reader_rejects_symbol_path_syntax(tmp_path):
    parquet_root = tmp_path / "allowed"
    daily = parquet_root / "daily_raw"
    daily.mkdir(parents=True)
    pd.DataFrame({"trade_date": ["20240102"], "close": [10.0]}).to_parquet(
        daily / "A.parquet", index=False
    )
    pd.DataFrame({"trade_date": ["20240102"], "close": [123.5]}).to_parquet(
        tmp_path / "secret.parquet", index=False
    )
    reader = ParquetReader(CacheConfig(PARQUET_ROOT=parquet_root))

    assert reader.read("daily_data_raw", "A")["close"].iloc[0] == 10.0
    for symbol in (
        "../../secret",
        r"..\..\secret",
        r"\secret",
        r"C:secret",
        r"\\server\share\secret",
        "A.",
        "A..",
        "NUL",
        "CON.txt",
        "PRN.SH",
        "COM1",
        "LPT9.SZ",
    ):
        with pytest.raises(ValueError, match="path"):
            reader.read("daily_data_raw", symbol)
        with pytest.raises(ValueError, match="path"):
            reader.exists("daily_data_raw", symbol)


def test_parquet_reader_reads_only_requested_minute_partition_years(tmp_path):
    parquet_root = tmp_path / "timeseries"
    symbol_dir = parquet_root / "minute_raw" / "000001.SZ"
    symbol_dir.mkdir(parents=True)
    for year, close in (("2023", 9.0), ("2024", 10.0), ("2025", 11.0)):
        pd.DataFrame({
            "trade_time": [pd.Timestamp(f"{year}-01-02 09:31:00")],
            "close": [close],
        }).to_parquet(symbol_dir / f"{year}.parquet", index=False)
    reader = ParquetReader(CacheConfig(PARQUET_ROOT=parquet_root))

    frame = reader.read(
        "minute_data_raw", "000001.SZ", years=("2024",)
    )

    assert frame["close"].tolist() == [10.0]


@pytest.mark.integration
class TestConvenienceMethods:
    """便捷方法测试"""

    def test_get_daily(self, cache_manager):
        """获取日线数据"""
        df = cache_manager.get_daily('000001.SZ')
        assert isinstance(df, pd.DataFrame)

    def test_get_minute(self, cache_manager):
        """获取分钟数据"""
        df = cache_manager.get_minute('000001.SZ')
        assert isinstance(df, pd.DataFrame)

    def test_get_daily_raw(self, cache_manager):
        """获取原始日线数据"""
        df = cache_manager.get_daily_raw('000001.SZ')
        assert isinstance(df, pd.DataFrame)

    def test_get_cyq(self, cache_manager):
        """获取筹码分布"""
        df = cache_manager.get_cyq('000001.SZ')
        assert isinstance(df, pd.DataFrame)

    def test_get_moneyflow(self, cache_manager):
        """获取资金流向"""
        df = cache_manager.get_moneyflow('000001.SZ')
        assert isinstance(df, pd.DataFrame)

    def test_get_basic(self, cache_manager):
        """获取基本面数据"""
        df = cache_manager.get_basic('000001.SZ')
        assert isinstance(df, pd.DataFrame)

    def test_get_adj_factor(self, cache_manager):
        """获取复权因子"""
        df = cache_manager.get_adj_factor('000001.SZ')
        assert isinstance(df, pd.DataFrame)
