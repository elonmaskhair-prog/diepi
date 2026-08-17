"""
数据提供者

实现P0数据接口:
- get_trade_cal() - 交易日历
- get_stock_info() - 股票信息
- get_daily() - 日线数据
- get_minute() - 分钟数据
- get_cyq() - 筹码分布
- get_moneyflow() - 资金流向
- get_margin() - 融资融券
- get_basic() - 基本面数据
"""

import logging
import bisect
import math
from typing import Optional, Union, List, Set
from datetime import datetime, timedelta
from enum import Enum

import pandas as pd

from ..instruments import is_exchange_fund


class InstrumentType(Enum):
    """证券类型"""
    EQUITY = 'equity'    # 股票
    ETF = 'etf'          # ETF基金
    INDEX = 'index'      # 指数


def get_instrument_type(symbol: str) -> InstrumentType:
    """
    根据代码前缀判断证券类型

    场内基金代码规则委托 ``instruments.is_exchange_fund`` 唯一真源。

    Args:
        symbol: 证券代码 (如 '510050.SH' 或 '510050')

    Returns:
        InstrumentType枚举值
    """
    if symbol is None:
        return InstrumentType.EQUITY

    # 提取纯数字部分
    code = symbol.split('.')[0].strip()

    # ETF/LOF/REITs/封基统一按场内基金码段归入现有 ETF 枚举。
    if is_exchange_fund(symbol):
        return InstrumentType.ETF

    # 指数前缀判断 (000xxx, 399xxx 等)
    if code.startswith('000') and len(code) == 6:
        # 000001-000999 可能是指数或股票，需要看交易所
        if symbol.endswith('.SH'):
            return InstrumentType.INDEX
    if code.startswith('399'):
        return InstrumentType.INDEX

    return InstrumentType.EQUITY

from .cache_manager import CacheManager, normalize_data_symbol
from .contract import (
    AlignedMarketData,
    AmountUnit,
    Frequency,
    PreCloseSource,
    PriceSpace,
    validate_adjustment_factor_ratio,
    validate_and_align_pair,
)
from .exceptions import ParameterError, DataNotFoundError
from .calendar import TradeCalendarIdentity, identify_trade_calendar
from ..config import PRICE_MODE_STRATEGY, PRICE_MODE_EXECUTION

logger = logging.getLogger(__name__)


class ParameterValidator:
    """参数验证器"""

    @staticmethod
    def validate_date_params(start: str = None, end: str = None,
                              count: int = None) -> None:
        """
        验证日期参数组合

        规则:
        - start + end + count 同时存在 → 错误
        - 其他组合均合法
        """
        if start and end and count:
            raise ParameterError(
                "Cannot specify start, end, and count together. "
                "Use: count alone, end+count, start+end, or start+count"
            )

    @staticmethod
    def normalize_date(date: str) -> Optional[str]:
        """标准化日期格式为 YYYYMMDD"""
        if date is None:
            return None
        # 处理各种输入格式: '2019-01-02', '2019/01/02', 20190102, 20190102.0
        date_str = str(date).replace('-', '').replace('/', '')
        # 处理 float 格式 (如 '20190102.0')
        if '.' in date_str:
            date_str = date_str.split('.')[0]
        return date_str[:8]

    @staticmethod
    def normalize_date_column(df: pd.DataFrame, col: str) -> pd.DataFrame:
        """
        标准化 DataFrame 中的日期列为 YYYYMMDD 字符串格式

        处理各种输入: int64 (20190102), float64 (20190102.0), str ('20190102')
        """
        if col not in df.columns:
            return df
        # 转为字符串，处理可能的小数点
        df[col] = df[col].astype(str).str.split('.').str[0]
        return df

    @staticmethod
    def normalize_symbol(symbol: str) -> Optional[str]:
        """
        标准化证券代码为 000001.SZ 格式

        支持股票和ETF:
        - 上交所股票: 6xxxxx.SH
        - 深交所股票: 0xxxxx.SZ, 3xxxxx.SZ
        - 北交所股票: 4xxxxx.BJ, 8xxxxx.BJ
        - 上交所ETF: 510xxx.SH, 511xxx.SH, 512xxx.SH, 513xxx.SH, 515xxx.SH, 516xxx.SH, 518xxx.SH, 588xxx.SH
        - 深交所ETF: 159xxx.SZ
        """
        if symbol is None:
            return None
        if type(symbol) is str and not symbol.strip():
            return None
        symbol = normalize_data_symbol(symbol)
        if '.' not in symbol:
            # 推断交易所（基金码段判定与 instruments.is_exchange_fund 同源：
            # 沪市基金 5xxxxx 全段、深市基金 15x/16x/18x）
            if symbol.startswith('5'):
                symbol = f"{symbol}.SH"
            elif symbol.startswith(('15', '16', '18')):
                symbol = f"{symbol}.SZ"
            # 上交所股票
            elif symbol.startswith('6'):
                symbol = f"{symbol}.SH"
            # 深交所股票
            elif symbol.startswith(('0', '3')):
                symbol = f"{symbol}.SZ"
            # 北交所股票（92x 新段 + 43/83/87 老段）
            elif symbol.startswith(('4', '8', '92')):
                symbol = f"{symbol}.BJ"
        return symbol


class DateHelper:
    """日期工具类"""

    def __init__(self, cache_manager: CacheManager):
        self._cache = cache_manager
        self._trade_cal: Optional[pd.DataFrame] = None
        self._trade_days_set: Optional[Set[str]] = None
        self._trade_days_list: Optional[List[str]] = None
        self._coverage_start: Optional[str] = None
        self._coverage_end: Optional[str] = None
        self._identity: Optional[TradeCalendarIdentity] = None

    def _ensure_loaded(self) -> None:
        """确保交易日历已加载"""
        if self._trade_cal is None:
            trade_cal = self._cache.get_trade_cal()
            required = {'cal_date', 'is_open'}
            if (
                trade_cal is None
                or trade_cal.empty
                or not required.issubset(trade_cal.columns)
            ):
                raise DataNotFoundError(
                    "交易日历不可用；若本地 trade_cal.parquet 存在，它会完整覆盖"
                    "内置日历且必须通过严格校验。"
                )
            self._trade_cal = trade_cal
            dates = (
                self._trade_cal['cal_date']
                .astype('string')
                .str.strip()
                .str.replace(r'\.0$', '', regex=True)
            )
            valid_shape = dates.str.fullmatch(r'\d{8}', na=False)
            parsed_dates = pd.to_datetime(
                dates.where(valid_shape), format='%Y%m%d', errors='coerce'
            )
            if (~valid_shape | parsed_dates.isna()).any():
                raise DataNotFoundError(
                    "交易日历 cal_date 必须全部是有效 YYYYMMDD 日期"
                )
            is_open = pd.to_numeric(self._trade_cal['is_open'], errors='coerce')
            if (is_open.isna() | ~is_open.isin((0, 1))).any():
                raise DataNotFoundError(
                    "交易日历 is_open 必须全部是 0 或 1"
                )
            canonical = pd.DataFrame({
                'cal_date': dates.astype(str),
                'is_open': is_open.astype('int8'),
            })
            conflicts = canonical.groupby('cal_date')['is_open'].nunique()
            if conflicts.gt(1).any():
                raise DataNotFoundError(
                    "交易日历同一日期含有冲突的 is_open 值"
                )
            canonical = (
                canonical.drop_duplicates('cal_date')
                .sort_values('cal_date', kind='mergesort')
                .reset_index(drop=True)
            )
            self._coverage_start = str(canonical['cal_date'].iloc[0])
            self._coverage_end = str(canonical['cal_date'].iloc[-1])
            expected_dates = pd.date_range(
                pd.to_datetime(self._coverage_start, format='%Y%m%d'),
                pd.to_datetime(self._coverage_end, format='%Y%m%d'),
                freq='D',
            ).strftime('%Y%m%d')
            if len(expected_dates) != len(canonical) or tuple(expected_dates) != tuple(
                canonical['cal_date']
            ):
                raise DataNotFoundError(
                    "交易日历必须包含其覆盖区间内的每一个自然日"
                )
            open_days = canonical[canonical['is_open'] == 1]
            self._trade_days_list = open_days['cal_date'].tolist()
            self._trade_days_set = set(self._trade_days_list)
            identity = getattr(self._cache, 'trade_calendar_identity', None)
            self._identity = identity or identify_trade_calendar(
                self._trade_cal, source='local_override'
            )

    @property
    def identity(self) -> TradeCalendarIdentity:
        self._ensure_loaded()
        if self._identity is None:  # defensive only
            raise DataNotFoundError("交易日历身份不可用")
        return self._identity

    def require_coverage(self, start: str, end: str = None) -> None:
        """Fail closed when a calendar query exceeds proven coverage."""

        self._ensure_loaded()
        canonical_start = ParameterValidator.normalize_date(start)
        canonical_end = ParameterValidator.normalize_date(
            end if end is not None else start
        )
        for name, value in (
            ('start', canonical_start), ('end', canonical_end)
        ):
            try:
                datetime.strptime(value, '%Y%m%d')
            except (TypeError, ValueError):
                raise ParameterError(
                    f"calendar {name} must be a valid YYYYMMDD date"
                ) from None
        if canonical_start > canonical_end:
            raise ParameterError("calendar start must not be after end")
        if (
            canonical_start < self._coverage_start
            or canonical_end > self._coverage_end
        ):
            identity = self.identity
            raise DataNotFoundError(
                "trade calendar coverage does not cover the requested interval: "
                f"requested={canonical_start}..{canonical_end}, "
                f"available={self._coverage_start}..{self._coverage_end}, "
                f"source={identity.source}, calendar_id={identity.calendar_id}"
            )

    def is_trade_day(self, date: str) -> bool:
        """判断是否为交易日"""
        self._ensure_loaded()
        date = ParameterValidator.normalize_date(date)
        self.require_coverage(date)
        return date in self._trade_days_set

    def get_prev_trade_day(self, date: str, n: int = 1) -> Optional[str]:
        """
        获取前N个交易日

        Args:
            date: 起始日期 (YYYYMMDD)
            n: 向前N天

        Returns:
            交易日字符串，如果不存在返回None
        """
        self._ensure_loaded()
        date = ParameterValidator.normalize_date(date)
        self.require_coverage(date)

        # 使用二分查找 O(log N)
        idx = bisect.bisect_left(self._trade_days_list, date)
        if idx >= n:
            return self._trade_days_list[idx - n]
        return None

    def get_next_trade_day(self, date: str, n: int = 1) -> Optional[str]:
        """
        获取后N个交易日

        Args:
            date: 起始日期 (YYYYMMDD)
            n: 向后N天

        Returns:
            交易日字符串，如果不存在返回None
        """
        self._ensure_loaded()
        date = ParameterValidator.normalize_date(date)
        self.require_coverage(date)

        # 使用二分查找 O(log N)
        idx = bisect.bisect_right(self._trade_days_list, date)
        if idx + n - 1 < len(self._trade_days_list):
            return self._trade_days_list[idx + n - 1]
        return None

    def get_trade_days_between(self, start: str, end: str) -> List[str]:
        """
        获取区间内所有交易日

        Args:
            start: 开始日期 (YYYYMMDD)
            end: 结束日期 (YYYYMMDD)

        Returns:
            交易日列表
        """
        self._ensure_loaded()
        start = ParameterValidator.normalize_date(start)
        end = ParameterValidator.normalize_date(end)
        self.require_coverage(start, end)

        # 使用二分查找定位起止位置 O(log N)
        start_idx = bisect.bisect_left(self._trade_days_list, start)
        end_idx = bisect.bisect_right(self._trade_days_list, end)
        return self._trade_days_list[start_idx:end_idx]

    def get_yesterday(self) -> str:
        """获取最近的交易日 (T-1)"""
        self._ensure_loaded()
        today = datetime.now().strftime('%Y%m%d')
        self.require_coverage(today)
        # 找到小于等于今天的最后一个交易日
        prev_days = [d for d in self._trade_days_list if d <= today]
        if prev_days:
            # 如果今天是交易日且还未收盘，返回前一天
            current_hour = datetime.now().hour
            if prev_days[-1] == today and current_hour < 15:
                return prev_days[-2] if len(prev_days) > 1 else prev_days[-1]
            return prev_days[-1]
        return today


class DataProvider:
    """
    数据提供者

    提供所有P0数据接口

    注意: 已移除单例模式，每个回测引擎实例应创建独立的 DataProvider，
    避免多个回测实例之间的 context 互相污染。
    """

    def __init__(self, context=None,
                 price_mode: str = None,
                 execution_price_mode: str = None,
                 *, data_root=None, cache_manager: CacheManager = None):
        """
        初始化数据提供者

        Args:
            context: 回测上下文 (current_date, current_time, current_symbol)
            data_root: 可选的显式数据根目录；不修改进程环境变量
            cache_manager: 可选的预构造 CacheManager；与 data_root 二选一
        """
        if data_root is not None and cache_manager is not None:
            raise ValueError("data_root and cache_manager are mutually exclusive")
        self._cache = cache_manager or CacheManager(data_root=data_root)
        self._date_helper = DateHelper(self._cache)
        self._context = context
        self._price_mode = self._normalize_base_price_mode(price_mode) or PRICE_MODE_STRATEGY
        self._execution_price_mode = self._normalize_base_price_mode(execution_price_mode) or PRICE_MODE_EXECUTION
        self._adj_factor_cache = {}
        self._adj_factor_ratio_cache = {}

    @property
    def data_root(self):
        """Return the resolved data root used by this provider."""

        return self._cache.config.PARQUET_ROOT.parent.parent

    @property
    def trade_calendar_identity(self) -> TradeCalendarIdentity:
        """Return reproducibility evidence for the selected market clock."""

        return self._date_helper.identity

    def set_context(self, context) -> None:
        """设置回测上下文"""
        self._context = context

    def set_price_modes(self, strategy: str = None, execution: str = None) -> None:
        """运行期调整价格模式（单轨 fallback：全池只有一个数据文件夹时，
        引擎把两腿对齐到可用的那一侧，使日线/分钟读取全部同源）"""
        if strategy is not None:
            self._price_mode = self._normalize_base_price_mode(strategy) or self._price_mode
        if execution is not None:
            self._execution_price_mode = (
                self._normalize_base_price_mode(execution) or self._execution_price_mode
            )

    @property
    def price_mode(self) -> str:
        return self._price_mode

    @property
    def execution_price_mode(self) -> str:
        return self._execution_price_mode

    def set_price_mode(self, price_mode: str) -> None:
        normalized = self._normalize_base_price_mode(price_mode)
        if normalized:
            self._price_mode = normalized

    def set_execution_price_mode(self, price_mode: str) -> None:
        normalized = self._normalize_base_price_mode(price_mode)
        if normalized:
            self._execution_price_mode = normalized

    def _normalize_base_price_mode(self, mode: Optional[str]) -> Optional[str]:
        if mode is None:
            return None
        mode = str(mode).lower().strip()
        if mode in ('raw', 'hfq'):
            return mode
        return None

    def _resolve_price_mode(self, mode: Optional[str]) -> str:
        if mode is None:
            return self._price_mode
        mode = str(mode).lower().strip()
        if mode in ('strategy', 'default'):
            return self._price_mode
        if mode in ('execution', 'exec'):
            return self._execution_price_mode
        base = self._normalize_base_price_mode(mode)
        return base or self._price_mode

    # ==================== P0 数据接口 ====================

    def get_trade_cal(self, exchange: str = 'SSE',
                      start: str = None, end: str = None) -> pd.DataFrame:
        """
        获取交易日历

        Args:
            exchange: 交易所 ('SSE' 上交所, 'SZSE' 深交所)
            start: 开始日期 (YYYYMMDD)
            end: 结束日期 (YYYYMMDD)

        Returns:
            DataFrame: columns = [cal_date, is_open, pretrade_date]
        """
        if start is not None and end is not None:
            self._date_helper.require_coverage(start, end)
        elif start is not None:
            self._date_helper.require_coverage(start)
        elif end is not None:
            self._date_helper.require_coverage(end)
        df = self._cache.get_trade_cal()

        # 按交易所筛选
        if 'exchange' in df.columns:
            df = df[df['exchange'] == exchange]

        # 日期范围筛选
        if start:
            start = ParameterValidator.normalize_date(start)
            df = df[df['cal_date'].astype(str) >= start]
        if end:
            end = ParameterValidator.normalize_date(end)
            df = df[df['cal_date'].astype(str) <= end]

        cols = ['cal_date', 'is_open']
        if 'pretrade_date' in df.columns:
            cols.append('pretrade_date')

        return df[cols].copy()

    def get_stock_info(self, symbol: Union[str, List[str]] = None,
                       fields: List[str] = None) -> Union[pd.DataFrame, pd.Series]:
        """
        获取股票基本信息

        Args:
            symbol: 股票代码，None表示全部
            fields: 返回字段，None表示全部

        Returns:
            单只股票返回Series，多只/全部返回DataFrame
        """
        df = self._cache.get_stock_info()

        if symbol is not None:
            if isinstance(symbol, str):
                # 单只股票
                symbol = ParameterValidator.normalize_symbol(symbol)
                row = df[df['ts_code'] == symbol]
                if row.empty:
                    raise DataNotFoundError(f"Symbol not found: {symbol}")
                result = row.iloc[0]
                if fields:
                    result = result[[f for f in fields if f in result.index]]
                return result
            else:
                # 多只股票
                symbols = [ParameterValidator.normalize_symbol(s) for s in symbol]
                df = df[df['ts_code'].isin(symbols)]

        # 字段筛选
        if fields:
            cols = ['ts_code'] + [f for f in fields if f in df.columns and f != 'ts_code']
            df = df[cols]

        return df.set_index('ts_code')

    def get_daily(self, symbol: str = None, start: str = None,
                  end: str = None, count: int = None,
                  fields: List[str] = None, price_mode: str = None) -> pd.DataFrame:
        """
        获取日线数据 (已后复权)

        Args:
            symbol: 股票代码，None使用上下文
            start: 开始日期 (YYYYMMDD)
            end: 结束日期 (YYYYMMDD)，默认T-1
            count: 返回记录数
            fields: 返回字段

        Returns:
            DataFrame: index=trade_date, columns=[open, high, low, close, ...]

        Parameter Rules:
            - No params: 返回全部历史 (到T-1)
            - count=N: 返回最后N条
            - end + count: 返回截至end的N条
            - start + end: 返回区间
            - start + end + count: ERROR
        """
        ParameterValidator.validate_date_params(start, end, count)
        symbol = self._resolve_symbol(symbol)

        mode = self._resolve_price_mode(price_mode)
        category = 'daily_data_raw' if mode == 'raw' else 'daily_data'
        df = self._cache.get_data(category, symbol)
        if df.empty:
            return pd.DataFrame()

        # 默认end为T-1
        if end is None and start is None:
            end = self._get_default_end_date()
        if end:
            end = ParameterValidator.normalize_date(end)
        if start:
            start = ParameterValidator.normalize_date(start)

        # 应用日期筛选
        df = self._filter_by_date_params(df, 'trade_date', start, end, count)

        # 字段筛选
        if fields:
            cols = [f for f in fields if f in df.columns]
            if 'trade_date' not in cols:
                cols = ['trade_date'] + cols
            df = df[cols]

        return df.set_index('trade_date')

    def get_minute(self, symbol: str = None, trade_date: str = None,
                   start_time: str = None, end_time: str = None,
                   count: int = None,
                   fields: List[str] = None, price_mode: str = None) -> pd.DataFrame:
        """
        获取分钟数据 (已后复权)

        Args:
            symbol: 股票代码，None使用上下文
            trade_date: 交易日期，None表示当日
            start_time: 开始时间 (HH:MM)
            end_time: 结束时间 (HH:MM)
            count: 返回记录数
            fields: 返回字段

        Returns:
            DataFrame: index=trade_time, columns=[open, high, low, close, ...]

        Time Semantics:
            - trade_date=None (当日): 返回截至当前回调的已完成K线，包含
              ``current_time`` 所指的当前已完成分钟 bar；不含正在形成的下一根 bar
            - trade_date=历史日期: 返回该日全部240根K线
            - 在09:35已完成 bar 的回调中，返回截至09:35（含）的K线
        """
        symbol = self._resolve_symbol(symbol)

        mode = self._resolve_price_mode(price_mode)
        category = 'minute_data_raw' if mode == 'raw' else 'minute_data'
        if trade_date is None:
            trade_date = self._get_current_date()
            max_time = self._get_max_visible_time()
        else:
            trade_date = ParameterValidator.normalize_date(trade_date)
            max_time = None
        trade_date = ParameterValidator.normalize_date(trade_date)
        minute_years = (trade_date[:4],)
        if isinstance(self._cache, CacheManager):
            df = self._cache.get_data(
                category, symbol, years=minute_years
            )
        else:
            # Compatibility for narrow test/provider adapters predating the
            # year-scoped CacheManager API.
            df = self._cache.get_data(category, symbol)
        if df.empty:
            return pd.DataFrame()

        # 确保trade_time是datetime类型
        if not pd.api.types.is_datetime64_any_dtype(df['trade_time']):
            df['trade_time'] = pd.to_datetime(df['trade_time'])

        # 日期筛选
        if max_time is not None:
            # 当日：只返回已完成K线
            df['date_str'] = df['trade_time'].dt.strftime('%Y%m%d')
            df = df[df['date_str'] == trade_date]

            if max_time is not None:
                df = df[df['trade_time'] <= max_time]

            df = df.drop(columns=['date_str'])
        else:
            # 历史日期：返回全天数据
            df['date_str'] = df['trade_time'].dt.strftime('%Y%m%d')
            df = df[df['date_str'] == trade_date]
            df = df.drop(columns=['date_str'])

        # 时间范围筛选
        if start_time:
            # 构造完整datetime用于比较
            start_dt = pd.to_datetime(
                f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} {start_time}"
            )
            df = df[df['trade_time'] >= start_dt]

        if end_time:
            end_dt = pd.to_datetime(
                f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} {end_time}"
            )
            df = df[df['trade_time'] <= end_dt]

        # count筛选
        if count:
            df = df.tail(count)

        # 字段筛选
        if fields:
            cols = [f for f in fields if f in df.columns]
            if 'trade_time' not in cols:
                cols = ['trade_time'] + cols
            df = df[cols]

        return df.set_index('trade_time')

    def get_minute_by_days(self, symbol: str, start_date: str, end_date: str,
                           end_time: datetime = None,
                           daily_start_time: str = None,
                           daily_end_time: str = None,
                           fields: List[str] = None, price_mode: str = None) -> pd.DataFrame:
        """
        按交易日范围获取分钟数据

        Args:
            symbol: 股票代码
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            end_time: 结束日期的截止时间（用于盘中场景，datetime类型）
            daily_start_time: 每日开始时间筛选 (HH:MM)
            daily_end_time: 每日结束时间筛选 (HH:MM)
            fields: 返回字段

        Returns:
            DataFrame: index=trade_time
        """
        symbol = self._resolve_symbol(symbol)

        mode = self._resolve_price_mode(price_mode)
        category = 'minute_data_raw' if mode == 'raw' else 'minute_data'
        start_date = ParameterValidator.normalize_date(start_date)
        end_date = ParameterValidator.normalize_date(end_date)
        minute_years = tuple(
            str(year)
            for year in range(int(start_date[:4]), int(end_date[:4]) + 1)
        )
        if isinstance(self._cache, CacheManager):
            df = self._cache.get_data(
                category, symbol, years=minute_years
            )
        else:
            df = self._cache.get_data(category, symbol)
        if df.empty:
            return pd.DataFrame()

        # 确保 trade_time 是 datetime 类型
        if not pd.api.types.is_datetime64_any_dtype(df['trade_time']):
            df['trade_time'] = pd.to_datetime(df['trade_time'])

        df = df.copy()

        df['date_str'] = df['trade_time'].dt.strftime('%Y%m%d')

        # 日期范围筛选
        df = df[(df['date_str'] >= start_date) & (df['date_str'] <= end_date)]

        # 结束日期的时间截断（盘中场景）
        if end_time is not None:
            end_datetime = end_time.replace(second=0, microsecond=0)
            # 对于结束日期，只保留截止时间及之前的数据（包含当前已完成K线）
            mask = (df['date_str'] < end_date) | (df['trade_time'] <= end_datetime)
            df = df[mask]

        # 每日时间段筛选
        if daily_start_time or daily_end_time:
            df['time_str'] = df['trade_time'].dt.strftime('%H:%M')
            if daily_start_time:
                df = df[df['time_str'] >= daily_start_time]
            if daily_end_time:
                df = df[df['time_str'] <= daily_end_time]
            df = df.drop(columns=['time_str'])

        df = df.drop(columns=['date_str'])

        # 字段筛选
        if fields:
            cols = [f for f in fields if f in df.columns]
            if 'trade_time' not in cols:
                cols = ['trade_time'] + cols
            df = df[cols]

        return df.set_index('trade_time')

    def get_aligned_pair(
            self, symbol: str = None, *, frequency: Union[Frequency, str],
            start: str = None, end: str = None, count: int = None,
            trade_date: str = None, start_time: str = None,
            end_time: str = None, fields: List[str] = None,
            pre_close_exempt_dates=(),
    ) -> AlignedMarketData:
        """Read and validate strategy/execution price tracks atomically.

        The tracks are loaded independently from this provider's resolved
        strategy and execution price modes.  This adapter never falls back to
        the other track and never intersects, sorts, or converts the returned
        bars.  The sole enrichment is an audited minute ``pre_close`` lookup
        from the same symbol and same price lane's daily data.  The strict
        DC-1 validator therefore
        rejects an absent track, missing required columns, non-monotonic keys,
        or any key mismatch with ``DataContractError``.

        Daily source ``amount`` is explicitly thousand yuan; minute source
        ``amount`` is yuan.  Both returned frames always expose ``amount`` in
        yuan.  ``pre_close_exempt_dates`` is forwarded explicitly into DC-1
        and remains visible in its report; no provider-wide exemption state is
        consulted.  Distinct raw/HFQ lanes additionally require AFI-1: source
        row zero is the fixed HFQ base, every observed trade day has one exact
        positive finite factor, and close prices satisfy the frozen mapping.
        Missing or malformed factors fail; this method never substitutes one.

        Daily requests accept the same ``start``/``end``/``count``
        combinations as :meth:`get_daily`.  Minute requests either select one
        day (``trade_date``, or the context's current day when omitted) or an
        explicit inclusive ``start`` + ``end`` range.  A minute range cannot
        be combined with ``trade_date`` or ``count``.
        """
        canonical_frequency = self._normalize_pair_frequency(frequency)
        canonical_symbol = self._resolve_symbol(symbol)
        normalized_fields = self._validate_pair_fields(fields)
        normalized_count = self._validate_pair_count(count)
        normalized_start_time = self._normalize_pair_time(
            start_time, 'start_time'
        )
        normalized_end_time = self._normalize_pair_time(end_time, 'end_time')
        if (normalized_start_time is not None
                and normalized_end_time is not None
                and normalized_start_time > normalized_end_time):
            raise ParameterError("start_time must be <= end_time")

        strategy_mode = self._resolve_price_mode('strategy')
        execution_mode = self._resolve_price_mode('execution')
        strategy_space = PriceSpace(strategy_mode)
        execution_space = PriceSpace(execution_mode)
        strategy_pre_close_source = None
        execution_pre_close_source = None
        adjustment_factors = None
        adjustment_factor_source = None
        requires_adjustment_identity = strategy_space is not execution_space

        if canonical_frequency is Frequency.DAILY:
            if trade_date is not None or start_time is not None or end_time is not None:
                raise ParameterError(
                    "daily aligned pairs do not accept trade_date, start_time, "
                    "or end_time"
                )
            normalized_start = self._normalize_pair_date(start, 'start')
            normalized_end = self._normalize_pair_date(end, 'end')
            if (normalized_start is not None and normalized_end is not None
                    and normalized_start > normalized_end):
                raise ParameterError("start must be <= end")
            ParameterValidator.validate_date_params(
                normalized_start, normalized_end, normalized_count
            )
            read_kwargs = {
                'symbol': canonical_symbol,
                'start': normalized_start,
                'end': normalized_end,
                'count': normalized_count,
                'fields': normalized_fields,
            }
            strategy_data = self.get_daily(
                price_mode=strategy_mode, **read_kwargs
            )
            execution_data = self.get_daily(
                price_mode=execution_mode, **read_kwargs
            )
            amount_unit = AmountUnit.THOUSAND_YUAN
        else:
            normalized_start = self._normalize_pair_date(start, 'start')
            normalized_end = self._normalize_pair_date(end, 'end')
            normalized_trade_date = self._normalize_pair_date(
                trade_date, 'trade_date'
            )
            has_range = normalized_start is not None or normalized_end is not None
            if has_range and (
                    normalized_start is None or normalized_end is None):
                raise ParameterError(
                    "minute aligned ranges require both start and end"
                )
            if (has_range and normalized_start is not None
                    and normalized_start > normalized_end):
                raise ParameterError("start must be <= end")
            if has_range and normalized_trade_date is not None:
                raise ParameterError(
                    "minute aligned ranges cannot be combined with trade_date"
                )
            if has_range and normalized_count is not None:
                raise ParameterError(
                    "minute aligned ranges cannot be combined with count"
                )

            if has_range:
                range_kwargs = {
                    'symbol': canonical_symbol,
                    'start_date': normalized_start,
                    'end_date': normalized_end,
                    'daily_start_time': normalized_start_time,
                    'daily_end_time': normalized_end_time,
                    'fields': normalized_fields,
                }
                strategy_data = self.get_minute_by_days(
                    price_mode=strategy_mode, **range_kwargs
                )
                execution_data = self.get_minute_by_days(
                    price_mode=execution_mode, **range_kwargs
                )
            else:
                minute_kwargs = {
                    'symbol': canonical_symbol,
                    'trade_date': normalized_trade_date,
                    'start_time': normalized_start_time,
                    'end_time': normalized_end_time,
                    'count': normalized_count,
                    'fields': normalized_fields,
                }
                strategy_data = self.get_minute(
                    price_mode=strategy_mode, **minute_kwargs
                )
                execution_data = self.get_minute(
                    price_mode=execution_mode, **minute_kwargs
                )
            strategy_data, strategy_pre_close_source = (
                self._enrich_minute_pre_close(
                    strategy_data, canonical_symbol, strategy_mode
                )
            )
            execution_data, execution_pre_close_source = (
                self._enrich_minute_pre_close(
                    execution_data, canonical_symbol, execution_mode
                )
            )
            amount_unit = AmountUnit.YUAN

        if requires_adjustment_identity:
            adjustment_factors = self._adj_factor_cache.get(canonical_symbol)
            if adjustment_factors is None:
                adjustment_factors = self._cache.get_adj_factor(canonical_symbol)
                if not isinstance(adjustment_factors, pd.DataFrame):
                    adjustment_factors = pd.DataFrame()
            adjustment_factors = adjustment_factors.copy(deep=True)
            adjustment_factor_source = "cache.adj_factor"

        aligned = validate_and_align_pair(
            strategy_data,
            execution_data,
            symbol=canonical_symbol,
            strategy_price_space=strategy_space,
            execution_price_space=execution_space,
            strategy_amount_unit=amount_unit,
            execution_amount_unit=amount_unit,
            frequency=canonical_frequency,
            pre_close_exempt_dates=pre_close_exempt_dates,
            strategy_pre_close_source=strategy_pre_close_source,
            execution_pre_close_source=execution_pre_close_source,
            adjustment_factors=adjustment_factors,
            adjustment_factor_source=adjustment_factor_source,
            require_adjustment_factor_identity=requires_adjustment_identity,
        )
        if requires_adjustment_identity:
            # Corporate-action and price-conversion calls must use the same
            # immutable snapshot that AFI-1 accepted for this aligned pair.
            self._adj_factor_cache[canonical_symbol] = adjustment_factors
            ratio_cache = getattr(self, '_adj_factor_ratio_cache', None)
            if ratio_cache is None:
                self._adj_factor_ratio_cache = {}
            else:
                for cache_key in tuple(ratio_cache):
                    if cache_key[0] == canonical_symbol:
                        del ratio_cache[cache_key]
        return aligned

    def _enrich_minute_pre_close(
            self, frame: pd.DataFrame, symbol: str, price_mode: str):
        """Attach pre_close from the same symbol and same price lane only.

        Legacy minute files may omit the column.  Enrichment is explicit in
        the DC-1 report as ``same_lane_daily``.  An incomplete or ambiguous
        daily mapping is not repaired from the other lane: the unchanged
        frame reaches the contract and fails its minute pre_close requirement.
        """
        if not isinstance(frame, pd.DataFrame):
            return frame, PreCloseSource.ABSENT
        if 'pre_close' in frame.columns:
            return frame, PreCloseSource.NATIVE
        if len(frame) == 0:
            return frame, PreCloseSource.ABSENT

        timestamps = self._minute_reference_timestamps(frame)
        if timestamps is None or len(timestamps) != len(frame):
            return frame, PreCloseSource.ABSENT
        if bool(timestamps.isna().any()):
            return frame, PreCloseSource.ABSENT
        needed_dates = tuple(sorted(set(timestamps.strftime('%Y%m%d'))))
        if not needed_dates:
            return frame, PreCloseSource.ABSENT

        daily = self.get_daily(
            symbol,
            start=needed_dates[0],
            end=needed_dates[-1],
            fields=['pre_close'],
            price_mode=price_mode,
        )
        reference = self._daily_pre_close_reference(daily)
        reference = {} if reference is None else reference
        used_suspension_fallback = False
        for date in needed_dates:
            if date in reference:
                continue
            fallback = self._synthetic_suspension_pre_close(
                frame, timestamps, date)
            if fallback is None:
                return frame, PreCloseSource.ABSENT
            reference[date] = fallback
            used_suspension_fallback = True

        enriched = frame.copy(deep=True)
        enriched['pre_close'] = [
            reference[timestamp.strftime('%Y%m%d')]
            for timestamp in timestamps
        ]
        source = (
            PreCloseSource.SAME_LANE_DAILY_WITH_SUSPENSION_FALLBACK
            if used_suspension_fallback
            else PreCloseSource.SAME_LANE_DAILY
        )
        return enriched, source

    @staticmethod
    def _synthetic_suspension_pre_close(
            frame: pd.DataFrame, timestamps: pd.DatetimeIndex,
            trade_date: str) -> Optional[float]:
        """Derive pre-close only from a provably flat, zero-turnover day.

        Some vendors materialize a suspended session as 241 constant minute
        rows while omitting that date from daily data.  Requiring both flat
        OHLC and zero volume/amount avoids treating an ordinary unchanged-price
        trading day as a suspension.
        """
        mask = timestamps.strftime('%Y%m%d') == trade_date
        day = frame.loc[mask]
        required = ('open', 'high', 'low', 'close', 'amount')
        if day.empty or any(column not in day.columns for column in required):
            return None
        volume_column = 'vol' if 'vol' in day.columns else (
            'volume' if 'volume' in day.columns else None)
        if volume_column is None:
            return None
        try:
            prices = day[list(required[:4])].apply(
                pd.to_numeric, errors='raise')
            amount = pd.to_numeric(day['amount'], errors='raise')
            volume = pd.to_numeric(day[volume_column], errors='raise')
            reference = float(prices['close'].iloc[0])
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(reference) or reference <= 0:
            return None
        tolerance = max(1e-9, abs(reference) * 1e-12)
        if not bool(((prices - reference).abs() <= tolerance).all().all()):
            return None
        if not bool((amount.fillna(float('inf')).abs() <= 1e-12).all()):
            return None
        if not bool((volume.fillna(float('inf')).abs() <= 1e-12).all()):
            return None
        return reference

    @staticmethod
    def _minute_reference_timestamps(
            frame: pd.DataFrame) -> Optional[pd.DatetimeIndex]:
        if isinstance(frame.index, pd.DatetimeIndex):
            return pd.DatetimeIndex(frame.index)
        if frame.index.name in ('trade_time', 'timestamp'):
            values = frame.index
        elif 'trade_time' in frame.columns:
            values = frame['trade_time']
        elif 'timestamp' in frame.columns:
            values = frame['timestamp']
        else:
            return None
        try:
            return pd.DatetimeIndex(pd.to_datetime(values, errors='raise'))
        except (TypeError, ValueError, OverflowError):
            return None

    @classmethod
    def _daily_pre_close_reference(cls, frame: pd.DataFrame):
        if (not isinstance(frame, pd.DataFrame)
                or len(frame) == 0
                or 'pre_close' not in frame.columns
                or not frame.index.is_unique):
            return None
        reference = {}
        for index_value, pre_close in frame['pre_close'].items():
            try:
                if isinstance(index_value, (pd.Timestamp, datetime)):
                    timestamp = pd.Timestamp(index_value)
                    if (timestamp.tzinfo is not None
                            or timestamp != timestamp.normalize()):
                        return None
                    canonical_date = timestamp.strftime('%Y%m%d')
                else:
                    canonical_date = cls._normalize_pair_date(
                        str(index_value), 'daily pre_close key'
                    )
            except (TypeError, ValueError, ParameterError):
                return None
            if canonical_date in reference:
                return None
            reference[canonical_date] = pre_close
        return reference

    @staticmethod
    def _normalize_pair_frequency(value: Union[Frequency, str]) -> Frequency:
        if isinstance(value, Frequency):
            return value
        if type(value) is not str:
            raise ParameterError("frequency must be 'daily' or 'minute'")
        try:
            return Frequency(value.strip().lower())
        except ValueError as error:
            raise ParameterError(
                "frequency must be 'daily' or 'minute'"
            ) from error

    @staticmethod
    def _normalize_pair_date(value: str, argument: str) -> Optional[str]:
        if value is None:
            return None
        if type(value) is not str or not value.strip():
            raise ParameterError("%s must be a non-empty date string" % argument)
        text = value.strip()
        formats = {
            8: ('%Y%m%d',),
            10: ('%Y-%m-%d', '%Y/%m/%d'),
        }
        for date_format in formats.get(len(text), ()):
            try:
                return datetime.strptime(text, date_format).strftime('%Y%m%d')
            except ValueError:
                continue
        raise ParameterError(
            "%s must be a valid YYYYMMDD, YYYY-MM-DD, or YYYY/MM/DD date"
            % argument
        )

    @staticmethod
    def _normalize_pair_time(value: str, argument: str) -> Optional[str]:
        if value is None:
            return None
        if type(value) is not str or not value.strip():
            raise ParameterError("%s must be a non-empty HH:MM string" % argument)
        normalized = value.strip()
        if (len(normalized) != 5 or normalized[2] != ':'
                or not normalized[:2].isdigit()
                or not normalized[3:].isdigit()):
            raise ParameterError("%s must be a valid HH:MM time" % argument)
        try:
            parsed = datetime.strptime(normalized, '%H:%M')
        except ValueError as error:
            raise ParameterError(
                "%s must be a valid HH:MM time" % argument
            ) from error
        return parsed.strftime('%H:%M')

    @staticmethod
    def _validate_pair_count(value: int) -> Optional[int]:
        if value is None:
            return None
        if type(value) is not int or value <= 0:
            raise ParameterError("count must be a positive integer")
        return value

    @staticmethod
    def _validate_pair_fields(value: List[str]) -> Optional[List[str]]:
        if value is None:
            return None
        if type(value) is not list:
            raise ParameterError("fields must be a list of unique field names")
        if any(type(field) is not str or not field.strip() for field in value):
            raise ParameterError("fields must contain non-empty strings")
        normalized = [field.strip() for field in value]
        if len(set(normalized)) != len(normalized):
            raise ParameterError("fields must contain unique field names")
        return normalized

    def _load_adj_factor_cache(self, symbol: str):
        df = self._cache.get_adj_factor(symbol)
        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame()
        # Preserve source order and values.  AFI-1 validates the causal prefix
        # and exact requested date; sorting or coercing here would repair the
        # evidence before the contract can inspect it.
        self._adj_factor_cache[symbol] = df.copy(deep=True)
        return self._adj_factor_cache[symbol]

    def get_adj_ratio(self, symbol: str, trade_date: str) -> float:
        """Return the AFI-1 exact-day factor/base ratio.

        There is deliberately no previous-row carry, first-row backfill, or
        ``ratio=1`` fallback.  Callers must propagate ``AdjustmentFactorError``
        so corporate-action failures cannot be silently skipped.
        """
        if symbol is None or trade_date is None:
            raise ParameterError(
                "symbol and trade_date are required for adjustment ratio"
            )
        symbol = ParameterValidator.normalize_symbol(symbol)
        trade_date = ParameterValidator.normalize_date(trade_date)
        ratio_cache = getattr(self, '_adj_factor_ratio_cache', None)
        if ratio_cache is None:
            ratio_cache = {}
            self._adj_factor_ratio_cache = ratio_cache
        cache_key = (symbol, trade_date)
        if cache_key in ratio_cache:
            return ratio_cache[cache_key]
        cache = self._adj_factor_cache.get(symbol)
        if cache is None:
            cache = self._load_adj_factor_cache(symbol)
        ratio = validate_adjustment_factor_ratio(
            cache,
            symbol=symbol,
            trade_date=trade_date,
            source="cache.adj_factor",
        )
        ratio_cache[cache_key] = ratio
        return ratio

    def get_cyq(self, symbol: str = None, trade_date: str = None,
                start: str = None, end: str = None, count: int = None) -> pd.DataFrame:
        """
        获取筹码分布

        Args:
            symbol: 股票代码
            trade_date: 交易日期 (与 start/end/count 互斥，仅获取单日数据)
            start: 开始日期
            end: 结束日期
            count: 返回最近N个交易日的数据

        Returns:
            DataFrame: columns=[trade_date, price, percent]
            如果指定 trade_date，仅返回 [price, percent]
        """
        if trade_date is not None and any(
                value is not None for value in (start, end, count)):
            raise ParameterError(
                "trade_date is mutually exclusive with start/end/count")
        ParameterValidator.validate_date_params(start, end, count)
        symbol = self._resolve_symbol(symbol)

        df = self._cache.get_cyq(symbol)
        if df.empty:
            return pd.DataFrame()

        df = ParameterValidator.normalize_date_column(df, 'trade_date')

        # 如果指定了 trade_date，返回单日数据 (兼容旧逻辑)
        if trade_date is not None:
            trade_date = ParameterValidator.normalize_date(trade_date)
            df = df[df['trade_date'] == trade_date]
            return df[['price', 'percent']]

        # Use the same complete parameter matrix as moneyflow/margin/basic.
        # Filtering unique dates first preserves every CYQ price bucket for a
        # selected day while enforcing end-only/start-only/start+count.
        if any(value is not None for value in (start, end, count)):
            if start is not None:
                start = ParameterValidator.normalize_date(start)
            if end is not None:
                end = ParameterValidator.normalize_date(end)
            unique_dates = pd.DataFrame({
                'trade_date': df['trade_date'].drop_duplicates()
            })
            selected = self._filter_by_date_params(
                unique_dates, 'trade_date', start, end, count)
            df = df[df['trade_date'].isin(selected['trade_date'])]
            return df[['trade_date', 'price', 'percent']]

        # 默认返回 T-1 单日数据 (兼容旧逻辑)
        trade_date = self._get_default_end_date()
        if trade_date:
            df = df[df['trade_date'] == trade_date]
        return df[['price', 'percent']]

    def get_moneyflow(self, symbol: str = None, start: str = None,
                      end: str = None, count: int = None) -> pd.DataFrame:
        """
        获取资金流向

        Args:
            symbol: 股票代码
            start: 开始日期
            end: 结束日期
            count: 返回记录数

        Returns:
            DataFrame: index=trade_date
        """
        ParameterValidator.validate_date_params(start, end, count)
        symbol = self._resolve_symbol(symbol)

        df = self._cache.get_moneyflow(symbol)
        if df.empty:
            return pd.DataFrame()

        if end is None and start is None:
            end = self._get_default_end_date()
        if end:
            end = ParameterValidator.normalize_date(end)
        if start:
            start = ParameterValidator.normalize_date(start)

        df = self._filter_by_date_params(df, 'trade_date', start, end, count)

        return df.set_index('trade_date')

    def get_margin(self, symbol: str = None, start: str = None,
                   end: str = None, count: int = None) -> pd.DataFrame:
        """
        获取融资融券数据

        Args:
            symbol: 股票代码
            start: 开始日期
            end: 结束日期
            count: 返回记录数

        Returns:
            DataFrame: index=trade_date
        """
        ParameterValidator.validate_date_params(start, end, count)
        symbol = self._resolve_symbol(symbol)

        df = self._cache.get_margin(symbol)
        if df.empty:
            return pd.DataFrame()

        if end is None and start is None:
            end = self._get_default_end_date()
        if end:
            end = ParameterValidator.normalize_date(end)
        if start:
            start = ParameterValidator.normalize_date(start)

        df = self._filter_by_date_params(df, 'trade_date', start, end, count)

        return df.set_index('trade_date')

    def get_basic(self, symbol: str = None, start: str = None,
                  end: str = None, count: int = None,
                  fields: List[str] = None) -> pd.DataFrame:
        """
        获取基本面数据

        Args:
            symbol: 股票代码
            start: 开始日期
            end: 结束日期
            count: 返回记录数
            fields: 返回字段

        Returns:
            DataFrame: index=trade_date
        """
        ParameterValidator.validate_date_params(start, end, count)
        symbol = self._resolve_symbol(symbol)

        df = self._cache.get_basic(symbol)
        if df.empty:
            return pd.DataFrame()

        if end is None and start is None:
            end = self._get_default_end_date()
        if end:
            end = ParameterValidator.normalize_date(end)
        if start:
            start = ParameterValidator.normalize_date(start)

        df = self._filter_by_date_params(df, 'trade_date', start, end, count)

        # 字段筛选
        if fields:
            cols = [f for f in fields if f in df.columns]
            if 'trade_date' not in cols:
                cols = ['trade_date'] + cols
            df = df[cols]

        return df.set_index('trade_date')

    # ==================== 日期工具方法 ====================

    def is_trade_day(self, date: str) -> bool:
        """判断是否为交易日"""
        return self._date_helper.is_trade_day(date)

    def get_prev_trade_day(self, date: str, n: int = 1) -> Optional[str]:
        """获取前N个交易日"""
        return self._date_helper.get_prev_trade_day(date, n)

    def get_next_trade_day(self, date: str, n: int = 1) -> Optional[str]:
        """获取后N个交易日"""
        return self._date_helper.get_next_trade_day(date, n)

    def get_trade_days_between(self, start: str, end: str) -> List[str]:
        """获取区间内所有交易日"""
        return self._date_helper.get_trade_days_between(start, end)

    # ==================== P1 数据接口 ====================

    def get_industry(self, symbol: str = None, level: str = 'L1') -> Union[str, pd.DataFrame]:
        """
        获取行业分类

        Args:
            symbol: 股票代码，None返回全部映射
            level: 分类级别 ('L1', 'L2', 'L3')，当前只支持L1

        Returns:
            单只股票返回行业名称字符串，全部返回DataFrame
        """
        df = self._cache.get_industry_mapping()
        if df.empty:
            return '' if symbol else pd.DataFrame()

        if symbol is not None:
            symbol = ParameterValidator.normalize_symbol(symbol)
            row = df[df['ts_code'] == symbol]
            if row.empty:
                return ''
            return row.iloc[0]['industry']

        return df[['ts_code', 'name', 'industry', 'area', 'market']]

    def get_holder_trade(self, symbol: str = None, start: str = None,
                         end: str = None) -> pd.DataFrame:
        """
        获取股东增减持

        Args:
            symbol: 股票代码
            start: 开始日期
            end: 结束日期

        Returns:
            DataFrame: 股东增减持记录
        """
        symbol = self._resolve_symbol(symbol)

        df = self._cache.get_holder_trade(symbol)
        if df.empty:
            return pd.DataFrame()

        # 日期筛选
        if 'ann_date' in df.columns:
            df = ParameterValidator.normalize_date_column(df, 'ann_date')
            if start:
                start = ParameterValidator.normalize_date(start)
                df = df[df['ann_date'] >= start]
            if end:
                end = ParameterValidator.normalize_date(end)
                df = df[df['ann_date'] <= end]
            df = df.sort_values('ann_date', ascending=False)

        return df

    def get_block_trade(self, symbol: str = None, start: str = None,
                        end: str = None) -> pd.DataFrame:
        """
        获取大宗交易

        Args:
            symbol: 股票代码，None返回全市场
            start: 开始日期
            end: 结束日期

        Returns:
            DataFrame: 大宗交易记录
        """
        df = self._cache.get_block_trade()
        if df.empty:
            return pd.DataFrame()

        # 日期格式化
        df = ParameterValidator.normalize_date_column(df, 'trade_date')

        # 股票筛选
        if symbol is not None:
            symbol = ParameterValidator.normalize_symbol(symbol)
            df = df[df['ts_code'] == symbol]

        # 日期筛选
        if start:
            start = ParameterValidator.normalize_date(start)
            df = df[df['trade_date'] >= start]
        if end:
            end = ParameterValidator.normalize_date(end)
            df = df[df['trade_date'] <= end]

        return df.sort_values('trade_date', ascending=False)

    def get_top_list(self, symbol: str = None, trade_date: str = None,
                     start: str = None, end: str = None, count: int = None) -> pd.DataFrame:
        """
        获取龙虎榜

        Args:
            symbol: 股票代码，None返回全市场
            trade_date: 交易日期 (与 start/end/count 互斥)
            start: 开始日期
            end: 结束日期
            count: 返回最近N个交易日的数据

        Returns:
            DataFrame: 龙虎榜记录
        """
        df = self._cache.get_top_list()
        if df.empty:
            return pd.DataFrame()

        # 日期格式化
        df = ParameterValidator.normalize_date_column(df, 'trade_date')

        # 股票筛选
        if symbol is not None:
            symbol = ParameterValidator.normalize_symbol(symbol)
            df = df[df['ts_code'] == symbol]

        # 日期筛选
        if trade_date is not None:
            # 单日查询 (兼容旧逻辑)
            trade_date = ParameterValidator.normalize_date(trade_date)
            df = df[df['trade_date'] == trade_date]
        elif start or end or count:
            # 使用 start/end/count 筛选
            if start:
                start = ParameterValidator.normalize_date(start)
                df = df[df['trade_date'] >= start]
            if end:
                end = ParameterValidator.normalize_date(end)
                df = df[df['trade_date'] <= end]
            elif count and not start:
                # count only 或 end+count: 取最后N个交易日
                default_end = self._get_default_end_date()
                if default_end:
                    df = df[df['trade_date'] <= default_end]
            if count:
                # 获取最近 count 个交易日的数据
                unique_dates = df['trade_date'].drop_duplicates().sort_values(ascending=False).head(count)
                df = df[df['trade_date'].isin(unique_dates)]

        return df.sort_values('trade_date', ascending=False)

    # ==================== 内部方法 ====================

    def _resolve_symbol(self, symbol: str) -> str:
        """解析股票代码 (优先使用参数，其次使用上下文)"""
        if symbol is not None:
            return ParameterValidator.normalize_symbol(symbol)

        if self._context and hasattr(self._context, 'current_symbol'):
            return self._context.current_symbol

        raise ParameterError("Symbol required when no context available")

    def _get_default_end_date(self) -> str:
        """获取默认结束日期 (T-1)"""
        if self._context and hasattr(self._context, 'current_date'):
            return self._date_helper.get_prev_trade_day(self._context.current_date)
        return self._date_helper.get_yesterday()

    def _get_current_date(self) -> str:
        """获取当前回测日期"""
        if self._context and hasattr(self._context, 'current_date'):
            return self._context.current_date
        return datetime.now().strftime('%Y%m%d')

    def _get_max_visible_time(self) -> Optional[pd.Timestamp]:
        """
        获取分钟数据最大可见时间

        ``context.current_time`` 是当前已完成分钟 bar 的时间戳，因此最大
        可见时间包含该 bar；正在形成的下一根 bar 不可见。
        """
        if self._context and hasattr(self._context, 'current_time'):
            current_time = self._context.current_time
            if isinstance(current_time, str):
                current_time = pd.to_datetime(current_time)
            # current_time 对应刚完成的 bar，筛选端使用 <=，因此包含该 bar。
            return current_time.replace(second=0, microsecond=0)
        return None

    def _filter_by_date_params(self, df: pd.DataFrame, date_col: str,
                                start: str, end: str,
                                count: int) -> pd.DataFrame:
        """
        根据参数组合筛选数据

        规则:
        - count only: 返回截至默认end日期的最后N条 (防止未来数据泄露)
        - end + count: 返回截至end的N条
        - start + count: 返回从start开始的N条
        - start + end: 返回区间
        - No params / end only: 返回到end为止的全部
        """
        df = df.copy()
        df = ParameterValidator.normalize_date_column(df, date_col)

        if count and not start and not end:
            # count only: 先过滤到默认end日期，再取最后N条 (关键修复：防止未来数据泄露)
            default_end = self._get_default_end_date()
            if default_end:
                df = df[df[date_col] <= default_end]
            df = df.sort_values(date_col)
            return df.tail(count)

        if end and count and not start:
            # end + count: 截至end的N条
            df = df[df[date_col] <= end]
            df = df.sort_values(date_col)
            return df.tail(count)

        if start and count and not end:
            # start + count: 从start开始的N条，但仍受当前因果边界约束。
            df = df[df[date_col] >= start]
            default_end = self._get_default_end_date()
            if default_end:
                df = df[df[date_col] <= default_end]
            df = df.sort_values(date_col)
            return df.head(count)

        if start and end:
            # start + end: 区间
            df = df[(df[date_col] >= start) & (df[date_col] <= end)]
            return df.sort_values(date_col)

        if end:
            # end only: 到end为止
            df = df[df[date_col] <= end]
            return df.sort_values(date_col)

        if start:
            # start only: 从 start 起到当前因果边界。ParameterValidator 明确
            # 允许这一组合，但它不能借由省略 end 暴露未来行。
            df = df[df[date_col] >= start]
            default_end = self._get_default_end_date()
            if default_end:
                df = df[df[date_col] <= default_end]
            return df.sort_values(date_col)

        # 无参数: 返回全部 (已排序)
        return df.sort_values(date_col)

