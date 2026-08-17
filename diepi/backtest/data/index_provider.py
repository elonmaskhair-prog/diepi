"""
指数数据提供者

提供基准指数数据接口，用于回测结果对比
"""

import logging
import math
import hashlib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List
import pandas as pd

from ..config import INDEX_DIR, INDEX_PARQUET_DIR
from ..comparison import (
    ComparisonScope,
    ReferenceIndexInvalidError,
    ReferenceIndexPartialError,
    ReferenceIndexSpec,
    ReferenceIndexUnavailableError,
    TotalReturnIndexSeries,
)
from .exceptions import DataNotFoundError

logger = logging.getLogger(__name__)


# 支持的指数列表
INDEX_LIST = {
    '000300.SH': '沪深300',
    '000001.SH': '上证指数',
    '399001.SZ': '深证成指',
    '000905.SH': '中证500',
    '399006.SZ': '创业板指',
    '000852.SH': '中证1000',
}


class IndexProvider:
    """
    指数数据提供者 (单例)

    数据存储 (Parquet):
        {DATA_ROOT}/parquet/timeseries/index_daily/
        ├── 000300_SH.parquet
        ├── 000001_SH.parquet
        └── ...

    Parquet格式:
        trade_date,open,high,low,close,vol,amount
    """

    _instance: Optional['IndexProvider'] = None

    # 默认数据目录
    DEFAULT_PARQUET_DIR = Path(INDEX_PARQUET_DIR)
    DEFAULT_DATA_DIR = Path(INDEX_DIR)  # deprecated, CSV后备

    def __new__(
        cls, data_dir: Path = None, *, data_root=None, parquet_dir: Path = None
    ):
        # Preserve the historical shared default provider.  An explicitly
        # scoped provider is intentionally independent so two local datasets
        # can be inspected in the same process without cache/path leakage.
        if data_root is not None or parquet_dir is not None or data_dir is not None:
            instance = super().__new__(cls)
            instance._initialized = False
            return instance
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self, data_dir: Path = None, *, data_root=None, parquet_dir: Path = None
    ):
        if self._initialized:
            return
        if data_root is not None and parquet_dir is not None:
            raise ValueError("data_root and parquet_dir are mutually exclusive")
        if data_root is not None:
            root = Path(data_root).expanduser().resolve()
            self._parquet_dir = root / "parquet" / "timeseries" / "index_daily"
            self._data_dir = root / "index_daily"
        else:
            self._parquet_dir = (
                Path(parquet_dir).expanduser().resolve()
                if parquet_dir is not None
                else self.DEFAULT_PARQUET_DIR
            )
            self._data_dir = data_dir or self.DEFAULT_DATA_DIR  # deprecated CSV
        self._cache: Dict[str, pd.DataFrame] = {}
        self._initialized = True

        logger.info(f"IndexProvider initialized, parquet_dir={self._parquet_dir}")

    @property
    def available_indices(self) -> Dict[str, str]:
        """获取可用的指数列表"""
        return INDEX_LIST.copy()

    def get_index_name(self, code: str) -> str:
        """获取指数名称"""
        return INDEX_LIST.get(code, code)

    def _normalize_code(self, code: str) -> str:
        """标准化指数代码"""
        code = code.upper().strip()
        if '.' not in code:
            # 推断交易所
            if code.startswith('0'):
                code = f"{code}.SH"
            elif code.startswith('3'):
                code = f"{code}.SZ"
        return code

    def _get_parquet_path(self, code: str) -> Path:
        """获取指数 Parquet 文件路径 (000300.SH -> 000300_SH.parquet)"""
        parquet_name = code.replace('.', '_') + '.parquet'
        return self._parquet_dir / parquet_name

    def _get_csv_path(self, code: str) -> Path:
        """获取指数 CSV 文件路径 (deprecated)"""
        return self._data_dir / f"{code}.csv"

    def get_total_return_source_identity(
        self,
        code: str,
        *,
        value_column: str = "total_return_close",
    ) -> Optional[tuple[str, str]]:
        """Return an immutable local-file identity for a comparison source.

        A path is not a source version: its contents can change in place and
        absolute paths leak host details.  The logical source ID therefore
        names only the provider lane/code, while the source version hashes the
        exact Parquet bytes consumed by this provider instance.  ``None``
        means no source file currently exists; callers must represent the
        comparison as unavailable rather than inventing metadata.
        """

        normalized_code = self._normalize_code(code)
        if type(value_column) is not str or not value_column.strip():
            raise ValueError("value_column must be a non-empty string")
        value_column = value_column.strip()
        path = self._get_parquet_path(normalized_code)
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        source_id = "diepi.local.index_total_return:{}:{}".format(
            normalized_code, value_column
        )
        return source_id, "sha256:{}".format(digest.hexdigest())

    def _load_index(self, code: str) -> pd.DataFrame:
        """
        加载指数数据 (仅 Parquet)

        文件名格式: 000300.SH -> 000300_SH.parquet
        """
        parquet_path = self._get_parquet_path(code)

        if not parquet_path.exists():
            logger.warning(f"Index data not found: {parquet_path}")
            logger.warning(f"Please run the data update scripts to generate Parquet files")
            return pd.DataFrame()

        try:
            df = pd.read_parquet(parquet_path)

            # 标准化日期列
            if 'trade_date' in df.columns:
                df['trade_date'] = df['trade_date'].astype(str).str.split('.').str[0]
                df = df.sort_values('trade_date')

            return df

        except Exception as e:
            logger.error(f"Failed to load index {code}: {e}")
            return pd.DataFrame()

    def get_index_daily(self, code: str, start: str = None,
                        end: str = None) -> pd.DataFrame:
        """
        获取指数日线数据

        Args:
            code: 指数代码 (如 '000300.SH')
            start: 开始日期 (YYYYMMDD)
            end: 结束日期 (YYYYMMDD)

        Returns:
            DataFrame: columns=[trade_date, open, high, low, close, vol, amount]
                       index=trade_date
        """
        code = self._normalize_code(code)

        # 检查缓存
        if code not in self._cache:
            self._cache[code] = self._load_index(code)

        df = self._cache[code].copy()

        if df.empty:
            return df

        # 日期筛选
        if start:
            start = str(start).replace('-', '')[:8]
            df = df[df['trade_date'] >= start]
        if end:
            end = str(end).replace('-', '')[:8]
            df = df[df['trade_date'] <= end]

        # 设置索引
        if not df.empty and 'trade_date' in df.columns:
            df = df.set_index('trade_date')

        return df

    def get_normalized_returns(self, code: str, start: str,
                               end: str) -> pd.DataFrame:
        """
        获取归一化收益率序列 (用于基准对比)

        从起始日收盘价归一化到1，后续值为相对涨跌幅

        Args:
            code: 指数代码
            start: 开始日期
            end: 结束日期

        Returns:
            DataFrame: columns=[close, normalized]
                       normalized: 归一化值，起始为1
        """
        df = self.get_index_daily(code, start, end)

        if df.empty:
            return pd.DataFrame()

        # 归一化: 以第一天收盘价为基准
        first_close = df['close'].iloc[0]
        df['normalized'] = df['close'] / first_close

        return df[['close', 'normalized']]

    def get_period_return(self, code: str, start: str, end: str) -> float:
        """
        获取价格指数的区间收益率。

        This method consumes the ordinary ``close`` lane and therefore is a
        price-index return.  It must not be relabelled as a total-return
        benchmark; use :meth:`get_total_return_period_return` for that.

        Args:
            code: 指数代码
            start: 开始日期
            end: 结束日期

        Returns:
            收益率 (如 0.15 表示 15%)
        """
        # 基期修正：用窗口前一交易日收盘作基（此前用窗口首日收盘，
        # 首日涨跌被剔除，与策略端"从期初资金起算"不对齐，2024全年实测差1.52pp）。
        # 数据缺失时抛异常而非静默返 0（静默 0 会让超额收益悄悄等于总收益）。
        start_dt = datetime.strptime(str(start), '%Y%m%d')
        pre_start = (start_dt - timedelta(days=15)).strftime('%Y%m%d')
        df = self.get_index_daily(code, pre_start, end)

        if df.empty or len(df) < 2:
            raise DataNotFoundError(
                f"基准指数 {code} 数据缺失或不足（{start}~{end}），"
                f"请检查 index_daily 数据目录"
            )

        dates = df.index.astype(str)
        prior = df[dates < str(start)]
        base_close = prior['close'].iloc[-1] if not prior.empty else df['close'].iloc[0]
        window = df[dates >= str(start)]
        if window.empty:
            raise DataNotFoundError(f"基准指数 {code} 在 {start}~{end} 窗口内无数据")
        last_close = window['close'].iloc[-1]

        return (last_close - base_close) / base_close

    def get_total_return_period_return(
        self,
        code: str,
        start: str,
        end: str,
        *,
        value_column: str = 'total_return_close',
    ) -> float:
        """Return an explicitly sourced total-return-index return.

        The method deliberately refuses to fall back to ``close``.  A price
        index and a total-return index are distinct comparison objects; using
        the former when the latter is unavailable would silently omit cash
        distributions.  As with strategy NAV, the return base is the last
        available observation before ``start`` so the first in-window move is
        retained.
        """
        if not isinstance(value_column, str) or not value_column.strip():
            raise ValueError("value_column must be a non-empty string")
        value_column = value_column.strip()
        normalized_start = str(start).replace('-', '')[:8]
        normalized_end = str(end).replace('-', '')[:8]
        try:
            start_dt = datetime.strptime(normalized_start, '%Y%m%d')
            datetime.strptime(normalized_end, '%Y%m%d')
        except ValueError:
            raise ValueError("start and end must be valid YYYYMMDD dates") from None
        if normalized_start > normalized_end:
            raise ValueError("start must be <= end")

        pre_start = (start_dt - timedelta(days=15)).strftime('%Y%m%d')
        df = self.get_index_daily(code, pre_start, normalized_end)
        if df.empty:
            raise DataNotFoundError(
                f"total-return index {code} has no data for "
                f"{normalized_start}~{normalized_end}"
            )
        if value_column not in df.columns:
            raise DataNotFoundError(
                f"total-return index {code} requires explicit column "
                f"{value_column!r}; price close fallback is forbidden"
            )
        dates = df.index.astype(str).str.replace('-', '', regex=False).str[:8]
        if dates.duplicated().any():
            raise DataNotFoundError(
                f"total-return index {code} contains duplicate dates"
            )
        values = pd.to_numeric(df[value_column], errors='coerce')
        prior = values[dates < normalized_start]
        window = values[
            (dates >= normalized_start) & (dates <= normalized_end)
        ]
        if prior.empty or window.empty:
            raise DataNotFoundError(
                f"total-return index {code} lacks a prior base or in-window data"
            )
        relevant = pd.concat([prior.tail(1), window])
        if any(
            not math.isfinite(float(value)) or float(value) <= 0.0
            for value in relevant
        ):
            raise DataNotFoundError(
                f"total-return index {code} values must be finite and positive"
            )
        base_value = float(prior.iloc[-1])
        last_value = float(window.iloc[-1])
        result = last_value / base_value - 1.0
        if not math.isfinite(result):
            raise DataNotFoundError(
                f"total-return index {code} return is not finite"
            )
        return result

    def get_total_return_series(
        self,
        spec: ReferenceIndexSpec,
        scope: ComparisonScope,
    ) -> TotalReturnIndexSeries:
        """Load explicit total-return levels on exactly ``scope``.

        The ordinary index ``close`` lane is never a fallback.  A successful
        result contains one value for every strategy observation plus the
        latest available prior-session level used as its return base.

        Semantic provider failures are deliberately structured:

        - :class:`ReferenceIndexUnavailableError`: no total-return data lane;
        - :class:`ReferenceIndexPartialError`: incomplete exact-date coverage;
        - :class:`ReferenceIndexInvalidError`: corrupt dates or numeric data.

        Unexpected I/O/runtime errors are not disguised as data availability
        and propagate to the caller.
        """
        if type(spec) is not ReferenceIndexSpec:
            raise TypeError("spec must be exactly ReferenceIndexSpec")
        if type(scope) is not ComparisonScope:
            raise TypeError("scope must be exactly ComparisonScope")

        frame = self.get_index_daily(
            spec.code,
            end=scope.end_date,
        )
        if frame.empty:
            raise ReferenceIndexUnavailableError(
                "REFERENCE_TOTAL_RETURN_UNAVAILABLE",
                "reference index {} has no total-return data for {}~{}".format(
                    spec.code, scope.start_date, scope.end_date
                ),
            )
        if spec.value_column not in frame.columns:
            raise ReferenceIndexUnavailableError(
                "REFERENCE_TOTAL_RETURN_COLUMN_MISSING",
                "reference index {} requires explicit column {!r}; price "
                "close fallback is forbidden".format(
                    spec.code, spec.value_column
                ),
            )

        raw_dates = (
            frame["trade_date"]
            if "trade_date" in frame.columns
            else pd.Series(frame.index, index=frame.index)
        )
        canonical_dates = []
        for position, raw_date in enumerate(raw_dates.tolist()):
            try:
                if hasattr(raw_date, "strftime"):
                    canonical = raw_date.strftime("%Y%m%d")
                    if type(canonical) is not str:
                        raise ValueError
                    parsed = datetime.strptime(canonical, "%Y%m%d")
                else:
                    text = str(raw_date).strip()
                    if (
                        text.endswith(".0")
                        and text[:-2].isdigit()
                    ):
                        text = text[:-2]
                    if len(text) == 8 and text.isdigit():
                        canonical = text
                    elif (
                        len(text) == 10
                        and text[4] == "-"
                        and text[7] == "-"
                    ):
                        canonical = text.replace("-", "")
                    else:
                        raise ValueError
                    parsed = datetime.strptime(canonical, "%Y%m%d")
                if parsed.strftime("%Y%m%d") != canonical:
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                raise ReferenceIndexInvalidError(
                    "REFERENCE_TOTAL_RETURN_DATE_INVALID",
                    "reference index {} has an invalid date at row {}".format(
                        spec.code, position
                    ),
                ) from None
            canonical_dates.append(canonical)

        dates = tuple(canonical_dates)
        if len(set(dates)) != len(dates):
            raise ReferenceIndexInvalidError(
                "REFERENCE_TOTAL_RETURN_DATES_DUPLICATED",
                "reference index {} contains duplicate dates".format(spec.code),
            )
        if tuple(sorted(dates)) != dates:
            raise ReferenceIndexInvalidError(
                "REFERENCE_TOTAL_RETURN_DATES_UNORDERED",
                "reference index {} dates must be strictly increasing".format(
                    spec.code
                ),
            )

        prior_positions = [
            index for index, value in enumerate(dates)
            if value < scope.start_date
        ]
        if not prior_positions:
            raise ReferenceIndexPartialError(
                "REFERENCE_TOTAL_RETURN_BASE_MISSING",
                "reference index {} lacks a prior-session total-return base "
                "before {}".format(spec.code, scope.start_date),
            )
        base_position = prior_positions[-1]

        window_positions = [
            index for index, value in enumerate(dates)
            if scope.start_date <= value <= scope.end_date
        ]
        window_dates = tuple(dates[index] for index in window_positions)
        if window_dates != scope.observation_ids:
            expected = set(scope.observation_ids)
            actual = set(window_dates)
            missing = sorted(expected - actual)
            unexpected = sorted(actual - expected)
            raise ReferenceIndexPartialError(
                "REFERENCE_TOTAL_RETURN_SCOPE_MISMATCH",
                "reference index {} exact scope mismatch; missing={}, "
                "unexpected={}".format(spec.code, missing, unexpected),
            )

        numeric_values = pd.to_numeric(
            frame[spec.value_column], errors="coerce"
        ).tolist()
        selected_positions = [base_position] + window_positions
        selected_values = []
        for position in selected_positions:
            try:
                value = float(numeric_values[position])
            except (TypeError, ValueError, OverflowError):
                value = float("nan")
            if not math.isfinite(value) or value <= 0.0:
                raise ReferenceIndexInvalidError(
                    "REFERENCE_TOTAL_RETURN_VALUE_INVALID",
                    "reference index {} total-return value at {} must be "
                    "finite and positive".format(spec.code, dates[position]),
                )
            selected_values.append(value)

        return TotalReturnIndexSeries(
            spec=spec,
            scope=scope,
            base_observation_id=dates[base_position],
            base_level=selected_values[0],
            levels=tuple(selected_values[1:]),
        )

    def is_available(self, code: str) -> bool:
        """检查指数数据是否可用"""
        code = self._normalize_code(code)
        parquet_path = self._get_parquet_path(code)
        return parquet_path.exists()

    def get_available_codes(self) -> List[str]:
        """获取本地已有数据的指数代码列表"""
        available = []
        for code in INDEX_LIST:
            if self.is_available(code):
                available.append(code)
        return available

    def clear_cache(self) -> None:
        """清除内存缓存"""
        self._cache.clear()
        logger.info("IndexProvider cache cleared")

    def get_data_info(self) -> Dict:
        """获取数据状态信息"""
        info = {
            'parquet_dir': str(self._parquet_dir),
            'indices': {}
        }

        for code, name in INDEX_LIST.items():
            parquet_path = self._get_parquet_path(code)
            if parquet_path.exists():
                df = self.get_index_daily(code)
                if not df.empty:
                    info['indices'][code] = {
                        'name': name,
                        'start': df.index[0],
                        'end': df.index[-1],
                        'records': len(df),
                    }
                else:
                    info['indices'][code] = {
                        'name': name,
                        'status': 'empty'
                    }
            else:
                info['indices'][code] = {
                    'name': name,
                    'status': 'not_found'
                }

        return info

    @classmethod
    def reset_instance(cls):
        """重置单例 (用于测试)"""
        cls._instance = None
