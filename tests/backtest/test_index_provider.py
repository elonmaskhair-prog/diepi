# -*- coding: utf-8 -*-
"""
IndexProvider 单元测试

测试指数数据提供者
"""

import pytest
import pandas as pd
import hashlib

from diepi.backtest.data.index_provider import IndexProvider, INDEX_LIST


class TestIndexProvider:
    """指数数据提供者测试"""

    def test_singleton_pattern(self):
        """单例模式验证"""
        # 重置单例
        IndexProvider.reset_instance()

        # 创建两个实例
        provider1 = IndexProvider()
        provider2 = IndexProvider()

        # 应该是同一个实例
        assert provider1 is provider2

        # 清理
        IndexProvider.reset_instance()

    @pytest.mark.integration
    def test_get_index_daily_basic(self, index_provider):
        """基本获取沪深300"""
        df = index_provider.get_index_daily('000300.SH')

        if not df.empty:
            assert 'close' in df.columns
            assert 'open' in df.columns
            assert 'high' in df.columns
            assert 'low' in df.columns

    @pytest.mark.integration
    def test_get_index_daily_date_range(self, index_provider):
        """日期范围过滤"""
        df = index_provider.get_index_daily(
            '000300.SH',
            start='20240102',
            end='20240131'
        )

        if not df.empty:
            # 索引应该是 trade_date
            dates = df.index.astype(str).tolist()
            assert all('20240102' <= d <= '20240131' for d in dates)

    @pytest.mark.integration
    def test_get_normalized_returns(self, index_provider):
        """归一化收益 (起始=1)"""
        df = index_provider.get_normalized_returns(
            '000300.SH',
            start='20240102',
            end='20240131'
        )

        if not df.empty:
            assert 'normalized' in df.columns
            # 第一个值应该是 1
            assert abs(df['normalized'].iloc[0] - 1.0) < 0.0001

    @pytest.mark.integration
    def test_get_period_return(self, index_provider):
        """区间收益率"""
        ret = index_provider.get_period_return(
            '000300.SH',
            start='20240102',
            end='20240131'
        )

        # 返回值应该是浮点数
        assert isinstance(ret, float)

    @pytest.mark.integration
    def test_is_available(self, index_provider):
        """检查指数可用性"""
        result = index_provider.is_available('000300.SH')
        assert isinstance(result, bool)

    @pytest.mark.integration
    def test_get_available_codes(self, index_provider):
        """获取可用指数列表"""
        codes = index_provider.get_available_codes()
        assert isinstance(codes, list)

    def test_code_normalization(self, index_provider):
        """代码规范化: '000300' → '000300.SH'"""
        # 内部方法测试
        normalized = index_provider._normalize_code('000300')
        assert normalized == '000300.SH'

        # 深交所指数
        normalized_sz = index_provider._normalize_code('399001')
        assert normalized_sz == '399001.SZ'

    @pytest.mark.integration
    def test_missing_index(self, index_provider):
        """不存在的指数 → 空 DataFrame"""
        df = index_provider.get_index_daily('999999.XX')
        assert df.empty

    def test_available_indices_property(self, index_provider):
        """获取可用指数字典"""
        indices = index_provider.available_indices
        assert isinstance(indices, dict)
        # 应该包含主要指数
        assert '000300.SH' in indices  # 沪深300
        assert '000001.SH' in indices  # 上证指数

    def test_get_index_name(self, index_provider):
        """获取指数名称"""
        name = index_provider.get_index_name('000300.SH')
        assert name == '沪深300'

        # 不存在的指数返回代码本身
        unknown_name = index_provider.get_index_name('999999.XX')
        assert unknown_name == '999999.XX'

    @pytest.mark.integration
    def test_clear_cache(self, index_provider):
        """清除内存缓存"""
        # 先加载数据
        index_provider.get_index_daily('000300.SH')

        # 清除缓存
        index_provider.clear_cache()

        # 缓存应该为空
        assert len(index_provider._cache) == 0

    @pytest.mark.integration
    def test_get_data_info(self, index_provider):
        """获取数据状态信息"""
        info = index_provider.get_data_info()
        assert 'parquet_dir' in info
        assert 'indices' in info
        assert isinstance(info['indices'], dict)

    def test_index_list_constants(self):
        """指数列表常量"""
        assert '000300.SH' in INDEX_LIST
        assert INDEX_LIST['000300.SH'] == '沪深300'
        assert '000001.SH' in INDEX_LIST
        assert INDEX_LIST['000001.SH'] == '上证指数'
        assert '399001.SZ' in INDEX_LIST
        assert INDEX_LIST['399001.SZ'] == '深证成指'


class TestIndexProviderEdgeCases:
    """边界情况测试"""

    @pytest.mark.integration
    def test_empty_date_range(self, index_provider):
        """空日期范围"""
        df = index_provider.get_index_daily(
            '000300.SH',
            start='20500101',
            end='20500131'
        )
        assert df.empty

    @pytest.mark.integration
    def test_get_period_return_empty_data(self, index_provider):
        """空数据的区间收益率：显式抛异常而非静默返 0

        （静默 0 会让下游"超额收益"悄悄等于总收益，见审计 portability#6）
        """
        from diepi.backtest.data.exceptions import DataNotFoundError
        import pytest as _pytest
        with _pytest.raises(DataNotFoundError):
            index_provider.get_period_return(
                '999999.XX', start='20240102', end='20240131'
            )

    @pytest.mark.integration
    def test_get_normalized_returns_empty_data(self, index_provider):
        """空数据的归一化收益"""
        df = index_provider.get_normalized_returns(
            '999999.XX',
            start='20240102',
            end='20240131'
        )
        assert df.empty

    def test_case_insensitive_code(self, index_provider):
        """代码大小写不敏感"""
        normalized1 = index_provider._normalize_code('000300.sh')
        normalized2 = index_provider._normalize_code('000300.SH')
        assert normalized1 == normalized2


class TestIndexTotalReturnContract:
    """A price index must never be relabelled as a total-return index."""

    @staticmethod
    def _frame(*, include_total_return=True):
        data = {
            "trade_date": ["20240101", "20240102", "20240103"],
            "close": [100.0, 101.0, 102.0],
        }
        if include_total_return:
            data["total_return_close"] = [200.0, 204.0, 210.0]
        return pd.DataFrame(data).set_index("trade_date")

    def test_total_return_uses_explicit_lane_and_prior_session_base(
        self, index_provider, monkeypatch
    ):
        frame = self._frame()
        monkeypatch.setattr(
            index_provider,
            "get_index_daily",
            lambda code, start=None, end=None: frame.copy(),
        )

        result = index_provider.get_total_return_period_return(
            "000300.SH", "20240102", "20240103"
        )

        assert result == pytest.approx(210.0 / 200.0 - 1.0)
        assert result != pytest.approx(102.0 / 100.0 - 1.0)

    def test_price_only_data_cannot_impersonate_total_return(
        self, index_provider, monkeypatch
    ):
        frame = self._frame(include_total_return=False)
        monkeypatch.setattr(
            index_provider,
            "get_index_daily",
            lambda code, start=None, end=None: frame.copy(),
        )

        from diepi.backtest.data.exceptions import DataNotFoundError

        with pytest.raises(DataNotFoundError, match="total-return"):
            index_provider.get_total_return_period_return(
                "000300.SH", "20240102", "20240103"
            )

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -1.0])
    def test_total_return_lane_rejects_invalid_values(
        self, index_provider, monkeypatch, bad
    ):
        frame = self._frame()
        frame.loc["20240102", "total_return_close"] = bad
        monkeypatch.setattr(
            index_provider,
            "get_index_daily",
            lambda code, start=None, end=None: frame.copy(),
        )

        from diepi.backtest.data.exceptions import DataNotFoundError

        with pytest.raises(DataNotFoundError, match="finite and positive"):
            index_provider.get_total_return_period_return(
                "000300.SH", "20240102", "20240103"
            )

    def test_source_identity_is_path_private_and_content_versioned(
        self, tmp_path
    ):
        parquet_dir = tmp_path / "indices"
        parquet_dir.mkdir()
        source = parquet_dir / "000300_SH.parquet"
        frame = self._frame().reset_index()
        frame.to_parquet(source, index=False)
        provider = IndexProvider(parquet_dir=parquet_dir)

        identity = provider.get_total_return_source_identity("000300.SH")

        assert identity[0] == (
            "diepi.local.index_total_return:000300.SH:total_return_close"
        )
        assert identity[1] == "sha256:" + hashlib.sha256(
            source.read_bytes()
        ).hexdigest()
        assert str(tmp_path) not in identity[0]
        assert str(tmp_path) not in identity[1]

        frame.loc[0, "total_return_close"] += 1.0
        frame.to_parquet(source, index=False)
        changed = provider.get_total_return_source_identity("000300.SH")
        assert changed[0] == identity[0]
        assert changed[1] != identity[1]

    def test_source_identity_is_none_when_file_is_absent(self, tmp_path):
        provider = IndexProvider(parquet_dir=tmp_path)

        assert provider.get_total_return_source_identity("000300.SH") is None
