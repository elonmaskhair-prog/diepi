# -*- coding: utf-8 -*-
"""
StockPool 单元测试

测试股票池管理
"""

import pytest
import pandas as pd

from diepi.backtest.data.stock_pool import StockPool, PoolSource


class TestStockPool:
    """股票池测试"""

    def test_get_pool_specified(self, stock_pool):
        """指定股票列表"""
        symbols = stock_pool.get_pool(
            PoolSource.SPECIFIED,
            symbols=['000001.SZ', '000002.SZ', '600000.SH']
        )
        assert len(symbols) == 3
        assert '000001.SZ' in symbols
        assert '000002.SZ' in symbols
        assert '600000.SH' in symbols

    def test_get_pool_specified_empty(self, stock_pool):
        """指定模式但未提供列表"""
        symbols = stock_pool.get_pool(PoolSource.SPECIFIED, symbols=None)
        assert symbols == []

    @pytest.mark.integration
    def test_get_pool_all_market(self, stock_pool):
        """全市场"""
        symbols = stock_pool.get_pool(PoolSource.ALL_MARKET)
        # 应该返回非空列表（取决于数据源）
        assert isinstance(symbols, list)

    @pytest.mark.integration
    def test_get_pool_all_market_exclude_st(self, stock_pool):
        """全市场排除 ST"""
        symbols = stock_pool.get_pool(
            PoolSource.ALL_MARKET,
            exclude_st=True
        )
        # 验证没有 ST 股票（如果有数据）
        for s in symbols:
            # 注意：无法直接验证名称，因为只有代码
            pass
        assert isinstance(symbols, list)

    @pytest.mark.integration
    def test_get_pool_industry(self, stock_pool):
        """按行业"""
        symbols = stock_pool.get_pool(
            PoolSource.INDUSTRY,
            industry='银行'
        )
        # 可能为空（取决于行业数据）
        assert isinstance(symbols, list)

    def test_get_pool_industry_no_industry(self, stock_pool):
        """行业模式但未指定行业"""
        symbols = stock_pool.get_pool(PoolSource.INDUSTRY, industry=None)
        assert symbols == []

    @pytest.mark.integration
    def test_get_pool_etf(self, stock_pool):
        """ETF/场内基金列表"""
        symbols = stock_pool.get_pool(PoolSource.ETF)
        assert isinstance(symbols, list)

        # 池来自 etf_daily 数据目录，实际是场内基金全集：
        # 沪市基金 5xxxxx（含 588 科创板ETF、501/502 LOF、508 REITs 等），
        # 深市基金 15xxxx/16xxxx/18xxxx（159 ETF、160-169 LOF、180 REITs）
        for s in symbols:
            code = s.split('.')[0]
            assert len(code) == 6 and (
                code.startswith('5') or code.startswith(('15', '16', '18'))
            ), f"非场内基金代码混入 ETF 池: {s}"


class TestSymbolNormalization:
    """代码规范化测试"""

    def test_normalize_sz_stock(self, stock_pool):
        """深交所股票代码规范化"""
        result = stock_pool._normalize_symbols(['000001', '000002'])
        assert '000001.SZ' in result
        assert '000002.SZ' in result

    def test_normalize_sh_stock(self, stock_pool):
        """上交所股票代码规范化"""
        result = stock_pool._normalize_symbols(['600000', '600001'])
        assert '600000.SH' in result
        assert '600001.SH' in result

    def test_normalize_bj_stock(self, stock_pool):
        """北交所股票代码规范化"""
        result = stock_pool._normalize_symbols(['830001', '430001'])
        assert '830001.BJ' in result
        assert '430001.BJ' in result

    def test_normalize_already_normalized(self, stock_pool):
        """已规范化的代码"""
        result = stock_pool._normalize_symbols(['000001.SZ', '600000.SH'])
        assert '000001.SZ' in result
        assert '600000.SH' in result

    def test_normalize_with_spaces(self, stock_pool):
        """带空格的代码"""
        result = stock_pool._normalize_symbols([' 000001 ', ' 600000 '])
        assert '000001.SZ' in result
        assert '600000.SH' in result

    def test_normalize_lowercase(self, stock_pool):
        """小写转大写"""
        result = stock_pool._normalize_symbols(['000001.sz'])
        assert '000001.SZ' in result

    def test_normalize_empty_list(self, stock_pool):
        """空列表"""
        result = stock_pool._normalize_symbols([])
        assert result == []

    def test_normalize_empty_strings(self, stock_pool):
        """空字符串过滤"""
        result = stock_pool._normalize_symbols(['', '000001', ''])
        assert len(result) == 1
        assert '000001.SZ' in result


class TestPoolSource:
    """股票池来源枚举测试"""

    def test_pool_source_values(self):
        """枚举值"""
        assert PoolSource.SPECIFIED.value == 'specified'
        assert PoolSource.ALL_MARKET.value == 'all_market'
        assert PoolSource.INDUSTRY.value == 'industry'
        assert PoolSource.ETF.value == 'etf'


class TestAvailableIndustries:
    """行业列表测试"""

    @pytest.mark.integration
    def test_get_available_industries(self, stock_pool):
        """获取可用行业列表"""
        industries = stock_pool.get_available_industries()
        assert isinstance(industries, list)
        # 列表应该是排序的
        if len(industries) > 1:
            assert industries == sorted(industries)


class TestCacheManagement:
    """缓存管理测试"""

    @pytest.mark.integration
    def test_clear_cache(self, stock_pool):
        """清除缓存"""
        # 先获取一些数据来填充缓存
        stock_pool.get_pool(PoolSource.ALL_MARKET)

        # 清除缓存
        stock_pool.clear_cache()

        # 验证缓存已清空
        assert stock_pool._all_stocks_cache is None
        assert stock_pool._industry_cache is None

    @pytest.mark.integration
    def test_cache_reuse(self, stock_pool):
        """缓存复用"""
        # 第一次调用
        result1 = stock_pool.get_pool(PoolSource.ALL_MARKET)

        # 第二次调用应该使用缓存
        result2 = stock_pool.get_pool(PoolSource.ALL_MARKET)

        assert result1 == result2


class TestDataProviderIntegration:
    """与 DataProvider 集成测试"""

    def test_lazy_loading(self):
        """延迟加载 DataProvider"""
        pool = StockPool(data_provider=None)
        # data 属性应该延迟创建 DataProvider
        data = pool.data
        assert data is not None

    def test_with_explicit_provider(self, data_provider):
        """使用显式提供的 DataProvider"""
        pool = StockPool(data_provider=data_provider)
        assert pool._data is data_provider
