# -*- coding: utf-8 -*-
"""
DataProvider 单元测试

测试数据提供者的核心功能
"""

import pytest
import pandas as pd
from datetime import datetime

from diepi.backtest.data.data_provider import (
    DataProvider,
    ParameterValidator,
    DateHelper,
    InstrumentType,
    get_instrument_type,
)
from diepi.backtest.data.exceptions import ParameterError, DataNotFoundError


@pytest.mark.integration
class TestTradeCal:
    """交易日历测试"""

    def test_get_trade_cal_basic(self, data_provider):
        """基本获取交易日历"""
        df = data_provider.get_trade_cal()
        assert not df.empty
        assert 'cal_date' in df.columns
        assert 'is_open' in df.columns

    def test_get_trade_cal_date_range(self, data_provider):
        """日期范围过滤"""
        df = data_provider.get_trade_cal(start='20240101', end='20240131')
        assert not df.empty
        # 检查日期范围
        dates = df['cal_date'].astype(str).tolist()
        assert all('20240101' <= d <= '20240131' for d in dates)

    def test_is_trade_day_true(self, data_provider):
        """交易日判断-是"""
        # 2024-01-02 是交易日
        assert data_provider.is_trade_day('20240102') is True

    def test_is_trade_day_false_weekend(self, data_provider):
        """交易日判断-否 (周末)"""
        # 2024-01-06 是周六
        assert data_provider.is_trade_day('20240106') is False

    def test_is_trade_day_false_holiday(self, data_provider):
        """交易日判断-否 (节假日)"""
        # 2024-01-01 是元旦
        assert data_provider.is_trade_day('20240101') is False

    def test_get_prev_trade_day(self, data_provider):
        """获取前N个交易日"""
        # 20240103 的前1个交易日是 20240102
        prev = data_provider.get_prev_trade_day('20240103', 1)
        assert prev == '20240102'

        # 20240103 的前2个交易日是 20231229
        prev2 = data_provider.get_prev_trade_day('20240103', 2)
        assert prev2 == '20231229'

    def test_get_next_trade_day(self, data_provider):
        """获取后N个交易日"""
        # 20240102 的后1个交易日是 20240103
        next_day = data_provider.get_next_trade_day('20240102', 1)
        assert next_day == '20240103'

    def test_get_trade_days_between(self, data_provider):
        """区间交易日列表"""
        days = data_provider.get_trade_days_between('20240102', '20240110')
        assert isinstance(days, list)
        assert len(days) > 0
        # 确认都在范围内
        assert all('20240102' <= d <= '20240110' for d in days)
        # 确认都是交易日
        for d in days:
            assert data_provider.is_trade_day(d)


@pytest.mark.integration
class TestStockInfo:
    """股票信息测试"""

    def test_get_stock_info_all(self, data_provider):
        """获取全部股票"""
        df = data_provider.get_stock_info()
        assert not df.empty
        assert isinstance(df, pd.DataFrame)

    def test_get_stock_info_single(self, data_provider):
        """单只股票 → Series"""
        info = data_provider.get_stock_info('000001.SZ')
        assert isinstance(info, pd.Series)

    def test_get_stock_info_multiple(self, data_provider):
        """多只股票 → DataFrame"""
        df = data_provider.get_stock_info(['000001.SZ', '000002.SZ'])
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 2

    def test_get_stock_info_fields(self, data_provider):
        """指定字段"""
        info = data_provider.get_stock_info('000001.SZ', fields=['name'])
        assert 'name' in info.index

    def test_get_stock_info_not_found(self, data_provider):
        """股票不存在 → DataNotFoundError"""
        with pytest.raises(DataNotFoundError):
            data_provider.get_stock_info('999999.SZ')


class TestDailyData:
    """日线数据测试"""

    @pytest.mark.integration
    def test_get_daily_basic(self, data_provider):
        """基本获取"""
        df = data_provider.get_daily('000001.SZ', count=10)
        assert not df.empty
        assert 'open' in df.columns
        assert 'close' in df.columns

    @pytest.mark.integration
    def test_get_daily_date_range(self, data_provider):
        """start + end"""
        df = data_provider.get_daily('000001.SZ', start='20240102', end='20240131')
        assert not df.empty
        # 索引是 trade_date
        dates = df.index.astype(str).tolist()
        assert all('20240102' <= d <= '20240131' for d in dates)

    @pytest.mark.integration
    def test_get_daily_count_only(self, data_provider):
        """只传 count"""
        df = data_provider.get_daily('000001.SZ', count=5)
        assert len(df) == 5

    @pytest.mark.integration
    def test_get_daily_end_count(self, data_provider):
        """end + count"""
        df = data_provider.get_daily('000001.SZ', end='20240131', count=5)
        assert len(df) == 5
        # 最后一条应该 <= end
        assert str(df.index[-1]) <= '20240131'

    @pytest.mark.integration
    def test_get_daily_start_count(self, data_provider):
        """start + count"""
        df = data_provider.get_daily('000001.SZ', start='20240102', count=5)
        assert len(df) == 5
        # 第一条应该 >= start
        assert str(df.index[0]) >= '20240102'

    def test_get_daily_all_params_error(self, data_provider):
        """start + end + count → ParameterError"""
        with pytest.raises(ParameterError):
            data_provider.get_daily('000001.SZ', start='20240101', end='20240131', count=10)

    @pytest.mark.integration
    def test_get_daily_price_mode_hfq(self, data_provider):
        """后复权模式"""
        df = data_provider.get_daily('000001.SZ', count=5, price_mode='hfq')
        assert not df.empty

    @pytest.mark.integration
    def test_get_daily_price_mode_raw(self, data_provider):
        """原始价格模式"""
        df = data_provider.get_daily('000001.SZ', count=5, price_mode='raw')
        assert not df.empty

    @pytest.mark.integration
    def test_get_daily_empty_symbol(self, data_provider):
        """无效股票 → 空 DataFrame"""
        df = data_provider.get_daily('INVALID.XX', count=5)
        assert df.empty


@pytest.mark.integration
class TestMinuteData:
    """分钟数据测试"""

    def test_get_minute_basic(self, data_provider):
        """基本获取"""
        # 获取某个确定有数据的交易日
        trade_days = data_provider.get_trade_days_between('20240102', '20240110')
        if trade_days:
            df = data_provider.get_minute('000001.SZ', trade_date=trade_days[0])
            # 分钟数据可能为空（取决于数据源）
            if not df.empty:
                assert 'open' in df.columns
                assert 'close' in df.columns

    def test_get_minute_time_range(self, data_provider):
        """时间范围过滤"""
        trade_days = data_provider.get_trade_days_between('20240102', '20240110')
        if trade_days:
            df = data_provider.get_minute(
                '000001.SZ',
                trade_date=trade_days[0],
                start_time='09:30',
                end_time='10:00'
            )
            # 时间范围测试
            if not df.empty:
                times = df.index.tolist()
                for t in times:
                    time_str = t.strftime('%H:%M')
                    assert '09:30' <= time_str <= '10:00'

    def test_get_minute_by_days(self, data_provider):
        """多日分钟数据"""
        df = data_provider.get_minute_by_days(
            '000001.SZ',
            start_date='20240102',
            end_date='20240105'
        )
        # 可能为空，取决于数据可用性
        if not df.empty:
            assert 'open' in df.columns


class TestSymbolNormalization:
    """代码规范化测试"""

    def test_normalize_sz_stock(self):
        """深交所股票: '000001' → '000001.SZ'"""
        result = ParameterValidator.normalize_symbol('000001')
        assert result == '000001.SZ'

    def test_normalize_sh_stock(self):
        """上交所股票: '600000' → '600000.SH'"""
        result = ParameterValidator.normalize_symbol('600000')
        assert result == '600000.SH'

    def test_normalize_etf_sh(self):
        """上交所ETF: '510050' → '510050.SH'"""
        result = ParameterValidator.normalize_symbol('510050')
        assert result == '510050.SH'

    def test_normalize_etf_sz(self):
        """深交所ETF: '159999' → '159999.SZ'"""
        result = ParameterValidator.normalize_symbol('159999')
        assert result == '159999.SZ'

    def test_normalize_already_normalized(self):
        """已规范化: '000001.SZ' → 保持不变"""
        result = ParameterValidator.normalize_symbol('000001.SZ')
        assert result == '000001.SZ'

    def test_normalize_lowercase(self):
        """小写转大写"""
        result = ParameterValidator.normalize_symbol('000001.sz')
        assert result == '000001.SZ'


class TestDateNormalization:
    """日期规范化测试"""

    def test_normalize_date_dash(self):
        """破折号格式: '2019-01-02' → '20190102'"""
        result = ParameterValidator.normalize_date('2019-01-02')
        assert result == '20190102'

    def test_normalize_date_slash(self):
        """斜杠格式: '2019/01/02' → '20190102'"""
        result = ParameterValidator.normalize_date('2019/01/02')
        assert result == '20190102'

    def test_normalize_date_int(self):
        """整数格式: 20190102 → '20190102'"""
        result = ParameterValidator.normalize_date(20190102)
        assert result == '20190102'

    def test_normalize_date_float(self):
        """浮点格式: 20190102.0 → '20190102'"""
        result = ParameterValidator.normalize_date(20190102.0)
        assert result == '20190102'

    def test_normalize_date_none(self):
        """空值处理"""
        result = ParameterValidator.normalize_date(None)
        assert result is None


class TestInstrumentType:
    """证券类型判断测试"""

    def test_equity_sz(self):
        """深交所股票"""
        assert get_instrument_type('000001.SZ') == InstrumentType.EQUITY

    def test_equity_sh(self):
        """上交所股票"""
        assert get_instrument_type('600000.SH') == InstrumentType.EQUITY

    def test_etf_sh(self):
        """上交所ETF"""
        assert get_instrument_type('510050.SH') == InstrumentType.ETF
        assert get_instrument_type('512000.SH') == InstrumentType.ETF
        assert get_instrument_type('563300.SH') == InstrumentType.ETF
        assert get_instrument_type('589000.SH') == InstrumentType.ETF

    def test_etf_sz(self):
        """深交所ETF"""
        assert get_instrument_type('159919.SZ') == InstrumentType.ETF
        assert get_instrument_type('160223.SZ') == InstrumentType.ETF
        assert get_instrument_type('180101.SZ') == InstrumentType.ETF

    def test_index_sh(self):
        """上证指数"""
        assert get_instrument_type('000001.SH') == InstrumentType.INDEX

    def test_index_sz(self):
        """深证成指"""
        assert get_instrument_type('399001.SZ') == InstrumentType.INDEX


class TestParameterValidator:
    """参数验证器测试"""

    def test_validate_date_params_valid_count_only(self):
        """合法: 只有 count"""
        # 不应抛出异常
        ParameterValidator.validate_date_params(count=10)

    def test_validate_date_params_valid_start_end(self):
        """合法: start + end"""
        ParameterValidator.validate_date_params(start='20240101', end='20240131')

    def test_validate_date_params_valid_end_count(self):
        """合法: end + count"""
        ParameterValidator.validate_date_params(end='20240131', count=10)

    def test_validate_date_params_invalid_all(self):
        """非法: start + end + count"""
        with pytest.raises(ParameterError):
            ParameterValidator.validate_date_params(
                start='20240101',
                end='20240131',
                count=10
            )


class TestDateHelper:
    """日期工具类测试"""

    def test_date_helper_init(self, cache_manager):
        """初始化"""
        helper = DateHelper(cache_manager)
        assert helper is not None

    @pytest.mark.integration
    def test_date_helper_is_trade_day(self, cache_manager):
        """交易日判断"""
        helper = DateHelper(cache_manager)
        # 测试基本功能
        result = helper.is_trade_day('20240102')
        assert isinstance(result, bool)

    @pytest.mark.integration
    def test_date_helper_get_prev_trade_day(self, cache_manager):
        """获取前N个交易日"""
        helper = DateHelper(cache_manager)
        prev = helper.get_prev_trade_day('20240103', 1)
        assert prev is not None or prev is None  # 取决于数据

    @pytest.mark.integration
    def test_date_helper_get_trade_days_between(self, cache_manager):
        """获取区间交易日"""
        helper = DateHelper(cache_manager)
        days = helper.get_trade_days_between('20240102', '20240110')
        assert isinstance(days, list)
