"""
股票池管理

提供股票池的获取和筛选功能
支持四种来源: 指定股票列表 / 全市场 / 按行业 / ETF
"""

from typing import List, Optional
from enum import Enum
import os
import pandas as pd
import logging

from ..config import PARQUET_ROOT

logger = logging.getLogger(__name__)


class PointInTimeUniverseError(RuntimeError):
    """Raised when a historical universe cannot be built without guessing."""


class PoolSource(Enum):
    """股票池来源"""
    SPECIFIED = "specified"      # 指定股票列表
    ALL_MARKET = "all_market"    # 全市场
    INDUSTRY = "industry"        # 按行业
    ETF = "etf"                  # ETF基金


class StockPool:
    """
    股票池管理器

    支持四种来源:
    1. 指定股票列表 (SPECIFIED)
    2. 全市场 (ALL_MARKET)
    3. 按行业分类 (INDUSTRY)
    4. ETF基金 (ETF)

    Example:
        pool = StockPool()

        # 获取全市场股票
        symbols = pool.get_pool(PoolSource.ALL_MARKET)

        # 获取指定行业
        symbols = pool.get_pool(PoolSource.INDUSTRY, industry='银行')

        # 使用指定股票
        symbols = pool.get_pool(PoolSource.SPECIFIED,
                               symbols=['000001.SZ', '000002.SZ'])

        # 获取ETF列表
        symbols = pool.get_pool(PoolSource.ETF)
    """

    def __init__(self, data_provider=None):
        """
        Args:
            data_provider: DataProvider 实例，None 时自动创建
        """
        self._data = data_provider
        self._all_stocks_cache: Optional[pd.DataFrame] = None
        self._industry_cache: Optional[pd.DataFrame] = None

    @property
    def data(self):
        """延迟加载 DataProvider"""
        if self._data is None:
            from .data_provider import DataProvider
            self._data = DataProvider()
        return self._data

    def get_pool(
        self,
        source: PoolSource = PoolSource.ALL_MARKET,
        symbols: List[str] = None,
        industry: str = None,
        exclude_st: bool = True,
        exclude_delisted: bool = True,
        as_of_date: str = None,
        start_date: str = None,
        end_date: str = None,
    ) -> List[str]:
        """
        获取股票池

        Args:
            source: 股票池来源
            symbols: 指定股票列表 (source=SPECIFIED 时必需)
            industry: 行业名称 (source=INDUSTRY 时必需)
            exclude_st: 排除 ST 股票
            exclude_delisted: 排除已退市股票
            as_of_date: 单日点时成员日期；不能与窗口日期同时使用
            start_date: 预加载窗口起点（必须与 end_date 同时提供）
            end_date: 预加载窗口终点（必须与 start_date 同时提供）

        Returns:
            股票代码列表
        """
        self._validate_point_in_time_scope(as_of_date, start_date, end_date)
        historical_scope = bool(as_of_date or start_date or end_date)
        if (
            historical_scope
            and source in (PoolSource.ALL_MARKET, PoolSource.INDUSTRY)
            and exclude_st
        ):
            raise PointInTimeUniverseError(
                "historical ST status is unavailable; pass exclude_st=False "
                "explicitly and disclose that assumption in the result contract"
            )

        if source == PoolSource.SPECIFIED:
            if not symbols:
                logger.warning("指定模式但未提供股票列表")
                return []
            return self._normalize_symbols(symbols)

        elif source == PoolSource.INDUSTRY:
            if not industry:
                logger.warning("行业模式但未指定行业")
                return []
            return self._get_industry_stocks(
                industry,
                as_of_date=as_of_date,
                start_date=start_date,
                end_date=end_date,
            )

        elif source == PoolSource.ETF:
            return self._get_etf_pool()

        else:  # ALL_MARKET
            return self._get_all_stocks(
                exclude_st,
                exclude_delisted,
                as_of_date=as_of_date,
                start_date=start_date,
                end_date=end_date,
            )

    @staticmethod
    def _validate_point_in_time_scope(
        as_of_date: str,
        start_date: str,
        end_date: str,
    ) -> None:
        if as_of_date is not None and (
            start_date is not None or end_date is not None
        ):
            raise ValueError(
                "as_of_date is mutually exclusive with start_date/end_date"
            )
        if (start_date is None) != (end_date is None):
            raise ValueError("start_date and end_date must be supplied together")
        if start_date is not None:
            start = StockPool._canonical_pool_date(start_date, "start_date")
            end = StockPool._canonical_pool_date(end_date, "end_date")
            if start > end:
                raise ValueError("start_date must not be after end_date")
        elif as_of_date is not None:
            StockPool._canonical_pool_date(as_of_date, "as_of_date")

    @staticmethod
    def _canonical_pool_date(value: object, name: str) -> pd.Timestamp:
        try:
            parsed = pd.to_datetime(str(value), errors="raise")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a valid date") from exc
        if pd.isna(parsed):
            raise ValueError(f"{name} must be a valid date")
        return pd.Timestamp(parsed).normalize()

    @classmethod
    def _point_in_time_mask(
        cls,
        df: pd.DataFrame,
        *,
        as_of_date: str = None,
        start_date: str = None,
        end_date: str = None,
    ) -> pd.Series:
        """Return membership in the ``[list_date, delist_date)`` interval.

        Window membership means that the security overlaps any part of the
        requested inclusive backtest window.  It is used only for preloading;
        strategy-facing pools are additionally filtered for each trading day.
        """

        missing = {"list_date", "delist_date"} - set(df.columns)
        if missing:
            raise PointInTimeUniverseError(
                "stock metadata lacks point-in-time fields: "
                + ", ".join(sorted(missing))
            )

        def parse_metadata_dates(column: str, *, required: bool):
            raw = (
                df[column]
                .astype("string")
                .str.strip()
                .str.replace("-", "", regex=False)
                .str.replace("/", "", regex=False)
            )
            blank = raw.isna() | raw.eq("")
            malformed = (~blank) & ~raw.str.fullmatch(r"\d{8}", na=False)
            if malformed.any() or (required and blank.any()):
                raise PointInTimeUniverseError(
                    f"stock metadata contains invalid {column} values"
                )
            parsed = pd.to_datetime(
                raw.mask(blank), format="%Y%m%d", errors="coerce"
            )
            if parsed[~blank].isna().any():
                raise PointInTimeUniverseError(
                    f"stock metadata contains invalid {column} values"
                )
            return parsed

        listed = parse_metadata_dates("list_date", required=True)
        delisted = parse_metadata_dates("delist_date", required=False)
        if as_of_date is not None:
            point = cls._canonical_pool_date(as_of_date, "as_of_date")
            return (listed <= point) & (delisted.isna() | (delisted > point))

        start = cls._canonical_pool_date(start_date, "start_date")
        end = cls._canonical_pool_date(end_date, "end_date")
        return (listed <= end) & (delisted.isna() | (delisted > start))

    def _normalize_symbols(self, symbols: List[str]) -> List[str]:
        """标准化股票代码"""
        # Reuse the public data-layer normalizer so fund and BSE code ranges
        # cannot drift into a second prefix table here.
        from .data_provider import ParameterValidator

        result = []
        for s in symbols:
            normalized = ParameterValidator.normalize_symbol(s)
            if normalized:
                result.append(normalized)
        return result

    def _get_all_stocks(
        self,
        exclude_st: bool = True,
        exclude_delisted: bool = True,
        as_of_date: str = None,
        start_date: str = None,
        end_date: str = None,
    ) -> List[str]:
        """获取全市场股票"""
        historical_scope = bool(as_of_date or start_date or end_date)
        # 方法1：尝试从 stock_history 获取
        try:
            df = self._get_stock_info()
            if not df.empty:
                # ts_code 可能是列或索引
                has_ts_code_col = 'ts_code' in df.columns
                has_ts_code_idx = df.index.name == 'ts_code'

                if has_ts_code_col or has_ts_code_idx:
                    # 筛选条件
                    mask = pd.Series([True] * len(df), index=df.index)

                    if historical_scope:
                        mask &= self._point_in_time_mask(
                            df,
                            as_of_date=as_of_date,
                            start_date=start_date,
                            end_date=end_date,
                        )
                    else:
                        if exclude_st and 'name' in df.columns:
                            mask &= ~df['name'].str.contains(
                                'ST', case=False, na=False
                            )

                        if exclude_delisted and 'list_status' in df.columns:
                            mask &= df['list_status'] == 'L'  # L=上市

                    # 获取股票列表
                    if has_ts_code_col:
                        symbols = df.loc[mask, 'ts_code'].tolist()
                    else:
                        symbols = df.loc[mask].index.tolist()

                    if symbols or historical_scope:
                        logger.info(f"全市场股票池: {len(symbols)} 只")
                        return symbols
        except PointInTimeUniverseError:
            raise
        except Exception as e:
            logger.debug(f"从 stock_info 获取股票失败: {e}")

        if historical_scope:
            raise PointInTimeUniverseError(
                "stock metadata is unavailable for point-in-time membership"
            )

        # 方法2：备选方案 - 从日线数据文件名提取股票列表
        logger.info("stock_info 不可用，尝试从日线数据目录获取股票列表")
        try:
            symbols = self._get_symbols_from_daily_files()
            if symbols:
                logger.info(f"从日线文件获取股票池: {len(symbols)} 只")
                return symbols
        except Exception as e:
            logger.error(f"从日线文件获取股票列表失败: {e}")

        logger.warning("无法获取股票信息")
        return []

    def _resolve_daily_dir(self) -> str:
        """获取日线 Parquet 数据目录"""
        mode = getattr(self.data, 'execution_price_mode', None)
        if mode is None:
            mode = getattr(self.data, 'price_mode', None)
        if mode == 'raw':
            return os.path.join(PARQUET_ROOT, 'daily_raw')
        return os.path.join(PARQUET_ROOT, 'daily')

    def _get_symbols_from_daily_files(self) -> List[str]:
        """从日线 Parquet 文件名提取股票代码列表"""
        import os
        try:
            daily_dir = self._resolve_daily_dir()
            if not os.path.exists(daily_dir):
                return []

            symbols = []
            for filename in os.listdir(daily_dir):
                if filename.endswith('.parquet'):
                    # 从文件名提取股票代码: 000001.SZ.parquet -> 000001.SZ
                    symbol = filename.replace('.parquet', '')
                    symbols.append(symbol)

            return sorted(symbols)
        except Exception as e:
            logger.error(f"从日线文件获取股票列表失败: {e}")
            return []

    def _get_industry_stocks(
        self,
        industry: str,
        *,
        as_of_date: str = None,
        start_date: str = None,
        end_date: str = None,
    ) -> List[str]:
        """获取指定行业股票"""
        try:
            df = self._get_industry_info()
            if df.empty:
                logger.warning("无法获取行业信息")
                return []

            # 查找匹配的行业
            mask = df['industry'].str.contains(industry, case=False, na=False)
            symbols = df.loc[mask, 'ts_code'].tolist()

            if not symbols:
                # 尝试精确匹配
                mask = df['industry'] == industry
                symbols = df.loc[mask, 'ts_code'].tolist()

            if as_of_date or start_date or end_date:
                stock_info = self._get_stock_info()
                if stock_info.empty:
                    raise PointInTimeUniverseError(
                        "stock metadata is unavailable for point-in-time membership"
                    )
                membership = self._point_in_time_mask(
                    stock_info,
                    as_of_date=as_of_date,
                    start_date=start_date,
                    end_date=end_date,
                )
                if 'ts_code' in stock_info.columns:
                    eligible = set(stock_info.loc[membership, 'ts_code'])
                elif stock_info.index.name == 'ts_code':
                    eligible = set(stock_info.loc[membership].index)
                else:
                    raise PointInTimeUniverseError(
                        "stock metadata lacks ts_code identifiers"
                    )
                symbols = [symbol for symbol in symbols if symbol in eligible]

            logger.info(f"行业 '{industry}' 股票池: {len(symbols)} 只")
            return symbols

        except PointInTimeUniverseError:
            raise
        except Exception as e:
            logger.error(f"获取行业股票失败: {e}")
            return []

    def _get_etf_pool(self) -> List[str]:
        """
        获取ETF基金列表

        ETF代码规则:
        - 上交所: 510xxx, 511xxx, 512xxx, 513xxx, 515xxx, 516xxx, 518xxx, 588xxx
        - 深交所: 159xxx

        优先从 section/etf_daily 获取ETF列表
        """
        try:
            # 从 cache_manager 获取ETF列表
            from .cache_manager import CacheManager
            cache = CacheManager()
            symbols = cache._parquet.get_etf_symbols()

            if symbols:
                logger.info(f"ETF股票池: {len(symbols)} 只")
                return symbols

            # 备选：从日线数据目录获取
            daily_dir = self._resolve_daily_dir()
            if not os.path.exists(daily_dir):
                logger.warning(f"日线目录不存在: {daily_dir}")
                return []

            symbols = []
            for filename in os.listdir(daily_dir):
                if filename.endswith('.parquet'):
                    symbol = filename.replace('.parquet', '')
                    code = symbol.split('.')[0]
                    if (
                        code.startswith('5')
                        or code.startswith(('15', '16', '18'))
                    ):
                        symbols.append(symbol)

            logger.info(f"ETF股票池: {len(symbols)} 只")
            return sorted(symbols)

        except Exception as e:
            logger.error(f"获取ETF列表失败: {e}")
            return []

    def _get_stock_info(self) -> pd.DataFrame:
        """获取股票基本信息"""
        if self._all_stocks_cache is not None:
            return self._all_stocks_cache

        try:
            # 从 data_provider 获取
            df = self.data.get_stock_info()
            if df is not None and not df.empty:
                self._all_stocks_cache = df
                return df
        except Exception as e:
            logger.debug(f"从 DataProvider 获取股票信息失败: {e}")

        return pd.DataFrame()

    def _get_industry_info(self) -> pd.DataFrame:
        """获取行业分类信息"""
        if self._industry_cache is not None:
            return self._industry_cache

        try:
            # 从 data_provider 获取
            df = self.data.get_industry()
            if df is not None and not df.empty:
                self._industry_cache = df
                return df
        except Exception as e:
            logger.debug(f"从 DataProvider 获取行业信息失败: {e}")

        return pd.DataFrame()

    def get_available_industries(self) -> List[str]:
        """获取可用的行业列表"""
        try:
            df = self._get_industry_info()
            if df.empty:
                return []
            return sorted(df['industry'].dropna().unique().tolist())
        except Exception as e:
            logger.error(f"获取行业列表失败: {e}")
            return []

    def clear_cache(self) -> None:
        """清除缓存"""
        self._all_stocks_cache = None
        self._industry_cache = None


__all__ = ["PointInTimeUniverseError", "PoolSource", "StockPool"]
