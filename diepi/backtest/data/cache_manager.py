"""
缓存管理器 (简化版)

两层缓存架构:
- L1: 内存缓存 (LRU)
- L2: Parquet 直接读取

数据源:
- 股票数据: {DATA_ROOT}/parquet/timeseries (Parquet)
- 元数据: {DATA_ROOT}/parquet/metadata (Parquet)
"""

import os
import re
import stat as stat_module
import threading
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional, List
from collections import OrderedDict

import pandas as pd

from .exceptions import CacheError, DataNotFoundError
from .calendar import (
    TradeCalendarIdentity,
    builtin_calendar_identity,
    identify_trade_calendar,
    load_builtin_trade_calendar,
)
from .plain_files import (
    METADATA_PARQUET_MAX_BYTES,
    TRADE_CALENDAR_PARQUET_MAX_BYTES,
    plain_file_exists,
    read_plain_parquet,
)
from ..config import (
    DATA_ROOT as DEFAULT_DATA_ROOT,
    METADATA_ROOT,
    ONECSV_DIR,
    PARQUET_ROOT,
)
from ..instruments import is_exchange_fund

logger = logging.getLogger(__name__)


_SAFE_SYMBOL_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_WINDOWS_RESERVED_BASENAME = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$",
    re.IGNORECASE,
)


def normalize_data_symbol(symbol) -> str:
    """Return a path-safe market-data symbol component.

    The data layer intentionally accepts short synthetic identifiers such as
    ``A`` in tests and private research adapters, so this is a filesystem
    safety contract rather than an exchange-membership policy.
    """

    if type(symbol) is not str:
        raise ValueError("symbol must be a non-empty path-safe string")
    normalized = symbol.strip().upper()
    device_basename = normalized.split(".", 1)[0]
    if (
        not _SAFE_SYMBOL_COMPONENT.fullmatch(normalized)
        or normalized.endswith(".")
        or _WINDOWS_RESERVED_BASENAME.fullmatch(device_basename)
    ):
        raise ValueError(
            "symbol must contain only ASCII letters, digits, dot, underscore "
            "or hyphen, cannot contain path syntax or Windows device names, "
            "and cannot end with a dot"
        )
    return normalized


def is_supported_direct_parquet_file(path, *, root=None) -> bool:
    """Return whether a direct-source candidate is a plain regular file.

    Direct market-data routing deliberately rejects directories, symbolic
    links, junctions and other Windows reparse points.  Readers and provenance
    collection share this predicate so candidate fallback can never bind a
    different path from the one actually read.
    """

    return _is_supported_direct_path(path, root=root, expect_directory=False)


def is_supported_direct_parquet_directory(path, *, root=None) -> bool:
    """Return whether a direct-source directory has a link-free ancestry."""

    return _is_supported_direct_path(path, root=root, expect_directory=True)


def _is_supported_direct_path(path, *, root, expect_directory: bool) -> bool:
    candidate = Path(os.path.abspath(os.fspath(Path(path).expanduser())))
    trusted_root = (
        candidate.parent
        if root is None
        else Path(os.path.abspath(os.fspath(Path(root).expanduser())))
    )
    try:
        relative = candidate.relative_to(trusted_root)
        root_info = trusted_root.lstat()
    except (OSError, ValueError):
        return False
    reparse_flag = getattr(
        stat_module, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x0400
    )
    if (
        not stat_module.S_ISDIR(root_info.st_mode)
        or stat_module.S_ISLNK(root_info.st_mode)
        or getattr(root_info, 'st_file_attributes', 0) & reparse_flag
    ):
        return False
    current = trusted_root
    if not relative.parts:
        return expect_directory
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            info = current.lstat()
        except OSError:
            return False
        if (
            stat_module.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & reparse_flag
        ):
            return False
        leaf = index == len(relative.parts) - 1
        if not leaf and not stat_module.S_ISDIR(info.st_mode):
            return False
        if leaf:
            if expect_directory:
                return stat_module.S_ISDIR(info.st_mode)
            return (
                stat_module.S_ISREG(info.st_mode)
                and getattr(info, 'st_nlink', 1) == 1
            )
    return False


@dataclass
class CacheConfig:
    """缓存配置"""
    # 数据目录
    ONECSV_DIR: Path = field(default_factory=lambda: Path(ONECSV_DIR))  # deprecated
    PARQUET_ROOT: Path = field(default_factory=lambda: Path(PARQUET_ROOT))
    METADATA_ROOT: Path = field(default_factory=lambda: Path(METADATA_ROOT))

    # 内存缓存大小
    LRU_MAX_SIZE: int = 3000

    # CSV读取选项 (deprecated)
    CSV_ENCODING: str = 'utf-8-sig'

    # 元数据 Parquet 路径映射
    METADATA_PARQUET: Dict[str, str] = field(default_factory=lambda: {
        'trade_cal': 'common/trade_cal.parquet',
        'stock_basic': 'stock/basic.parquet',
        'industry_mapping': 'common/industry/mapping.parquet',
    })

    # onecsv 文件名映射 (deprecated, 仅为兼容)
    ONECSV_FILES: Dict[str, str] = field(default_factory=lambda: {
        'trade_cal': 'trade_cal/trade_cal.csv',
        'stock_history': 'stock_history/stock_history.csv',
        'block_trade': 'block_trade/block_trade.csv',
        'top_list': 'top_list/top_list.csv',
        'industry_mapping': 'industry_classification/stock_industry_mapping.csv',
    })

    # Parquet 目录映射 (category -> parquet子目录)
    PARQUET_DIR_MAP: Dict[str, str] = field(default_factory=lambda: {
        'daily_data': 'daily',           # 后复权日线
        'daily_data_raw': 'daily_raw',   # 原始日线
        'minute_data': 'minute',         # 后复权分钟线 (年度分割)
        'minute_data_raw': 'minute_raw', # 原始分钟线 (年度分割)
        'etf_daily_data': 'etf_daily',
        'etf_daily_data_raw': 'etf_daily_raw',
        'etf_minute_data': 'etf_minute',
        'etf_minute_data_raw': 'etf_minute_raw',
        'daily_basic': 'daily_basic',
        'adj_factor': 'adj_factor',
        'etf_adj_factor': 'etf_adj_factor',
        'moneyflow_data': 'moneyflow',
        'cyq_chips': 'cyq_chips',
        'margin_detail': 'margin',
        'holder_trade': 'holder_trade',
        'repurchase': 'repurchase',
    })

    # ETF 截面数据目录 (section格式)
    # 注意: PARQUET_ROOT 指向 timeseries 目录，ETF在其父目录的 section 下
    ETF_CROSS_SECTION_DIR: str = '../section/etf_daily'       # 后复权ETF截面
    ETF_SECTION_RAW_DIR: str = '../section/etf_daily_raw'     # 不复权ETF截面

    # When paths came from one explicit data root, retain that lexical trust
    # boundary so metadata reads also inspect every intermediate component.
    # Kept last to preserve the positional order of the historical fields.
    # ``None`` retains the custom-config contract, where the caller explicitly
    # designates METADATA_ROOT as the trusted root.
    DATA_ROOT: Optional[Path] = None

    @classmethod
    def from_data_root(cls, data_root) -> "CacheConfig":
        """Build an isolated cache configuration for one explicit data root."""

        from ...runtime import RuntimePaths

        paths = RuntimePaths.resolve(data_root=data_root)
        return cls(
            ONECSV_DIR=paths.data_root / 'onecsv',
            PARQUET_ROOT=paths.parquet_root,
            METADATA_ROOT=paths.metadata_root,
            DATA_ROOT=paths.data_root,
        )

class MemoryCache:
    """
    L1 内存缓存 (LRU)

    - trade_cal, stock_info: 启动时全量加载
    - 其他数据: LRU缓存
    """

    def __init__(self, max_size: int = 100):
        self._trade_cal: Optional[pd.DataFrame] = None
        self._stock_info: Optional[pd.DataFrame] = None
        self._lru_cache: OrderedDict[str, pd.DataFrame] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.RLock()

        # 统计信息
        self._hits = 0
        self._misses = 0

    @property
    def trade_cal(self) -> Optional[pd.DataFrame]:
        return self._trade_cal

    @trade_cal.setter
    def trade_cal(self, df: pd.DataFrame):
        self._trade_cal = df

    @property
    def stock_info(self) -> Optional[pd.DataFrame]:
        return self._stock_info

    @stock_info.setter
    def stock_info(self, df: pd.DataFrame):
        self._stock_info = df

    def get(self, key: str) -> Optional[pd.DataFrame]:
        """从LRU缓存获取数据"""
        with self._lock:
            if key in self._lru_cache:
                self._lru_cache.move_to_end(key)
                self._hits += 1
                return self._lru_cache[key]
            self._misses += 1
            return None

    def put(self, key: str, df: pd.DataFrame) -> None:
        """添加到LRU缓存"""
        with self._lock:
            if key in self._lru_cache:
                self._lru_cache.move_to_end(key)
                self._lru_cache[key] = df
            else:
                if len(self._lru_cache) >= self._max_size:
                    self._lru_cache.popitem(last=False)
                self._lru_cache[key] = df

    def invalidate(self, key: str = None) -> None:
        """清除缓存"""
        with self._lock:
            if key is None:
                self._lru_cache.clear()
                self._trade_cal = None
                self._stock_info = None
            elif key in self._lru_cache:
                del self._lru_cache[key]

    def get_stats(self) -> Dict:
        """获取缓存统计"""
        total = self._hits + self._misses
        return {
            'lru_size': len(self._lru_cache),
            'max_size': self._max_size,
            'hits': self._hits,
            'misses': self._misses,
            'hit_rate': self._hits / total if total > 0 else 0,
            'trade_cal_loaded': self._trade_cal is not None,
            'stock_info_loaded': self._stock_info is not None,
        }


class ParquetReader:
    """
    L2 Parquet 数据读取器

    从 {PARQUET_ROOT}（config 解析的 parquet/timeseries 目录）直接读取 Parquet 文件
    支持:
    - 普通数据: {category}/{symbol}.parquet
    - 分钟数据: {category}/{symbol}/{year}.parquet (年度分割)
    """

    def __init__(self, config: CacheConfig):
        self._config = config
        self._lock = threading.RLock()

    def _trust_root(self) -> Path:
        return Path(self._config.DATA_ROOT or self._config.PARQUET_ROOT)

    def _is_minute_category(self, category: str) -> bool:
        """判断是否为分钟数据类别"""
        return category in ('minute_data', 'minute_data_raw')

    def _is_etf_symbol(self, symbol: str) -> bool:
        """判断是否为场内基金代码（路由到 etf_* 数据目录）

        历史P0：此处曾用 9 个硬编码前缀，560-563/589/517/501/508/16x 等
        新段基金（本地 51.8% 的基金文件）被误判为股票、报"无数据"。
        现统一走 instruments.is_exchange_fund 宽码段判定。
        """
        return is_exchange_fund(symbol)

    def _symbol_path_candidates(self, symbol: str) -> List[str]:
        """Return candidate filename/dir names for a symbol."""
        canonical = normalize_data_symbol(symbol)
        candidates = [canonical]
        alt = canonical.replace('.', '_')
        if alt not in candidates:
            candidates.append(alt)
        return candidates

    def _category_root(self, parquet_dir: str) -> Path:
        """Resolve one configured category without leaving PARQUET_ROOT."""

        parquet_root = Path(
            os.path.abspath(os.fspath(Path(self._config.PARQUET_ROOT).expanduser()))
        )
        category_root = Path(
            os.path.abspath(os.fspath(parquet_root / parquet_dir))
        )
        try:
            category_root.relative_to(parquet_root)
        except ValueError as exc:
            raise CacheError(
                f"Parquet category escapes configured root: {parquet_dir!r}"
            ) from exc
        return category_root

    def _single_file_path(self, parquet_dir: str, candidate: str) -> Path:
        category_root = self._category_root(parquet_dir)
        path = category_root / f"{candidate}.parquet"
        try:
            Path(os.path.abspath(os.fspath(path))).relative_to(category_root)
        except ValueError as exc:
            raise CacheError("market-data file escapes its category root") from exc
        return path

    def _minute_symbol_path(self, parquet_dir: str, candidate: str) -> Path:
        category_root = self._category_root(parquet_dir)
        path = category_root / candidate
        try:
            Path(os.path.abspath(os.fspath(path))).relative_to(category_root)
        except ValueError as exc:
            raise CacheError("minute symbol directory escapes its category root") from exc
        return path

    def read(
        self,
        category: str,
        symbol: str,
        *,
        years: Optional[Iterable[str]] = None,
    ) -> pd.DataFrame:
        """
        读取 Parquet 数据

        Args:
            category: 数据类别
            symbol: 股票代码

        Returns:
            DataFrame，如果文件不存在返回空 DataFrame
        """
        symbol = normalize_data_symbol(symbol)
        with self._lock:
            # ETF日线/分钟/复权因子特殊处理
            if self._is_etf_symbol(symbol):
                if category in ('daily_data', 'daily_data_raw'):
                    etf_category = 'etf_daily_data' if category == 'daily_data' else 'etf_daily_data_raw'
                    parquet_dir = self._config.PARQUET_DIR_MAP.get(etf_category)
                    if parquet_dir:
                        df = self._read_single_file(parquet_dir, symbol)
                        if not df.empty:
                            return df
                    return self._read_etf_cross_section(symbol, raw=(category == 'daily_data_raw'))
                if category in ('minute_data', 'minute_data_raw'):
                    etf_category = 'etf_minute_data' if category == 'minute_data' else 'etf_minute_data_raw'
                    parquet_dir = self._config.PARQUET_DIR_MAP.get(etf_category)
                    if parquet_dir:
                        return self._read_minute_data(
                            parquet_dir, symbol, years=years
                        )
                if category == 'adj_factor':
                    parquet_dir = self._config.PARQUET_DIR_MAP.get('etf_adj_factor')
                    if parquet_dir:
                        return self._read_single_file(parquet_dir, symbol)

            parquet_dir = self._config.PARQUET_DIR_MAP.get(category)
            if not parquet_dir:
                logger.debug(f"Unknown category for Parquet: {category}")
                return pd.DataFrame()

            if self._is_minute_category(category):
                return self._read_minute_data(
                    parquet_dir, symbol, years=years
                )
            else:
                return self._read_single_file(parquet_dir, symbol)

    def _read_single_file(self, parquet_dir: str, symbol: str) -> pd.DataFrame:
        """读取单个 Parquet 文件"""
        last_path = None
        for candidate in self._symbol_path_candidates(symbol):
            parquet_path = self._single_file_path(parquet_dir, candidate)
            last_path = parquet_path
            if not is_supported_direct_parquet_file(
                parquet_path, root=self._trust_root()
            ):
                continue
            try:
                df = pd.read_parquet(parquet_path)
                if 'trade_date' in df.columns:
                    df['trade_date'] = df['trade_date'].astype(str).str.split('.').str[0]
                return df
            except Exception as e:
                logger.warning(f"Failed to read Parquet {parquet_path}: {e}")
                return pd.DataFrame()

        # warning 级别：数据文件缺失是"静默 0% 收益"失真链的源头，必须可见
        logger.warning(f"Parquet not found: {last_path}")
        return pd.DataFrame()

    def _read_minute_data(
        self,
        parquet_dir: str,
        symbol: str,
        *,
        years: Optional[Iterable[str]] = None,
    ) -> pd.DataFrame:
        """
        读取分钟数据 (年度分割)

        目录结构: minute/{symbol}/{year}.parquet
        """
        symbol_dir = None
        for candidate in self._symbol_path_candidates(symbol):
            trial_dir = self._minute_symbol_path(parquet_dir, candidate)
            if is_supported_direct_parquet_directory(
                trial_dir, root=self._trust_root()
            ):
                symbol_dir = trial_dir
                break
        if symbol_dir is None:
            logger.debug(f"Minute data dir not found: {self._config.PARQUET_ROOT / parquet_dir / symbol}")
            return pd.DataFrame()

        requested_years = None
        if years is not None:
            requested_years = frozenset(str(value) for value in years)
            if any(
                len(value) != 4 or not value.isdigit()
                for value in requested_years
            ):
                raise ValueError("minute partition years must be YYYY strings")

        dfs = []
        try:
            for f in sorted(symbol_dir.glob("*.parquet")):
                if (
                    requested_years is not None
                    and f.stem not in requested_years
                ):
                    continue
                if not is_supported_direct_parquet_file(
                    f, root=self._trust_root()
                ):
                    raise CacheError(
                        f"Unsupported minute Parquet member: {f.name}"
                    )
                dfs.append(pd.read_parquet(f))
        except Exception as e:
            logger.warning(f"Failed to read minute data for {symbol}: {e}")
            return pd.DataFrame()

        if not dfs:
            return pd.DataFrame()

        result = pd.concat(dfs, ignore_index=True)

        if 'trade_time' in result.columns:
            if not pd.api.types.is_datetime64_any_dtype(result['trade_time']):
                result['trade_time'] = pd.to_datetime(result['trade_time'])
            result = result.sort_values('trade_time')

        return result

    def _read_etf_cross_section(self, symbol: str, raw: bool = False) -> pd.DataFrame:
        """
        从截面数据读取ETF日线

        ETF数据存储为截面格式: section/etf_daily/{date}.parquet 或
        section/etf_daily_raw/{date}.parquet
        每个文件包含当天所有ETF的数据，需要筛选出指定symbol
        """
        etf_subdir = self._config.ETF_SECTION_RAW_DIR if raw else self._config.ETF_CROSS_SECTION_DIR
        etf_dir = self._config.PARQUET_ROOT / etf_subdir
        if not etf_dir.exists():
            logger.debug(f"ETF cross-section dir not found: {etf_dir}")
            return pd.DataFrame()

        dfs = []
        try:
            # 获取所有日期文件并排序
            date_files = sorted(etf_dir.glob("*.parquet"))
            if not date_files:
                logger.debug(f"No ETF cross-section files found in {etf_dir}")
                return pd.DataFrame()

            logger.info(f"Reading ETF {symbol} from {len(date_files)} cross-section files...")

            # 逐个读取并筛选
            for f in date_files:
                try:
                    df = pd.read_parquet(f, columns=['ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'pre_close', 'change', 'pct_chg', 'vol', 'amount'])
                    row = df[df['ts_code'] == symbol]
                    if not row.empty:
                        dfs.append(row)
                except Exception as e:
                    logger.debug(f"Failed to read {f}: {e}")
                    continue

        except Exception as e:
            logger.warning(f"Failed to read ETF cross-section for {symbol}: {e}")
            return pd.DataFrame()

        if not dfs:
            logger.debug(f"No data found for ETF {symbol}")
            return pd.DataFrame()

        result = pd.concat(dfs, ignore_index=True)

        # 标准化日期格式
        if 'trade_date' in result.columns:
            result['trade_date'] = result['trade_date'].astype(str).str.split('.').str[0]
            result = result.sort_values('trade_date')

        logger.info(f"Loaded ETF {symbol}: {len(result)} rows")
        return result

    def exists(self, category: str, symbol: str) -> bool:
        """检查数据是否存在"""
        symbol = normalize_data_symbol(symbol)
        # ETF特殊处理
        if self._is_etf_symbol(symbol):
            if category in ('daily_data', 'daily_data_raw'):
                etf_category = 'etf_daily_data' if category == 'daily_data' else 'etf_daily_data_raw'
                parquet_dir = self._config.PARQUET_DIR_MAP.get(etf_category)
                if parquet_dir:
                    for candidate in self._symbol_path_candidates(symbol):
                        parquet_path = self._single_file_path(
                            parquet_dir, candidate
                        )
                        if is_supported_direct_parquet_file(
                            parquet_path,
                            root=self._trust_root(),
                        ):
                            return True
                etf_subdir = self._config.ETF_SECTION_RAW_DIR if category == 'daily_data_raw' else self._config.ETF_CROSS_SECTION_DIR
                etf_dir = self._config.PARQUET_ROOT / etf_subdir
                return etf_dir.exists() and any(etf_dir.glob("*.parquet"))
            if category in ('minute_data', 'minute_data_raw'):
                etf_category = 'etf_minute_data' if category == 'minute_data' else 'etf_minute_data_raw'
                parquet_dir = self._config.PARQUET_DIR_MAP.get(etf_category)
                if not parquet_dir:
                    return False
                for candidate in self._symbol_path_candidates(symbol):
                    symbol_dir = self._minute_symbol_path(
                        parquet_dir, candidate
                    )
                    if is_supported_direct_parquet_directory(
                        symbol_dir, root=self._trust_root()
                    ) and any(
                        is_supported_direct_parquet_file(
                            f, root=self._trust_root()
                        )
                        for f in symbol_dir.glob("*.parquet")
                    ):
                        return True
                return False
            if category == 'adj_factor':
                parquet_dir = self._config.PARQUET_DIR_MAP.get('etf_adj_factor')
                if not parquet_dir:
                    return False
                for candidate in self._symbol_path_candidates(symbol):
                    parquet_path = self._single_file_path(
                        parquet_dir, candidate
                    )
                    if is_supported_direct_parquet_file(
                        parquet_path,
                        root=self._trust_root(),
                    ):
                        return True
                return False

        parquet_dir = self._config.PARQUET_DIR_MAP.get(category)
        if not parquet_dir:
            return False

        if self._is_minute_category(category):
            return any(
                is_supported_direct_parquet_directory(
                    self._minute_symbol_path(parquet_dir, candidate),
                    root=self._trust_root(),
                )
                and any(
                    is_supported_direct_parquet_file(
                        f,
                        root=self._trust_root(),
                    )
                    for f in self._minute_symbol_path(
                        parquet_dir, candidate
                    ).glob("*.parquet")
                )
                for candidate in self._symbol_path_candidates(symbol)
            )
        else:
            return any(
                is_supported_direct_parquet_file(
                    self._single_file_path(parquet_dir, candidate),
                    root=self._trust_root(),
                )
                for candidate in self._symbol_path_candidates(symbol)
            )

    def get_etf_symbols(self) -> List[str]:
        """获取所有ETF代码列表"""
        etf_dir = self._config.PARQUET_ROOT / self._config.ETF_CROSS_SECTION_DIR
        if not etf_dir.exists():
            return []

        # 从最新的截面文件获取ETF列表
        date_files = sorted(etf_dir.glob("*.parquet"), reverse=True)
        if not date_files:
            return []

        try:
            df = pd.read_parquet(date_files[0], columns=['ts_code'])
            return sorted(df['ts_code'].unique().tolist())
        except Exception as e:
            logger.warning(f"Failed to get ETF symbols: {e}")
            return []


class CacheManager:
    """
    缓存管理器 (简化版)

    两层架构:
    - L1: 内存缓存 (LRU)
    - L2: Parquet 直接读取

    数据流:
    1. 检查 L1 内存缓存
    2. 未命中则从 Parquet 读取
    3. 存入 L1 并返回
    """

    def __init__(self, config: CacheConfig = None, *, data_root=None):
        if config is not None and data_root is not None:
            raise ValueError("config and data_root are mutually exclusive")
        if config is None and data_root is None:
            configured_root = os.environ.get('DATA_ROOT')
            if configured_root and not Path(configured_root).expanduser().is_dir():
                raise FileNotFoundError(
                    "环境变量 DATA_ROOT 指向的目录不存在: "
                    f"{Path(configured_root).expanduser()}\n"
                    "请检查路径拼写，或向 DataProvider/CacheManager 传入"
                    "显式 data_root。"
                )
        self._config = config or (
            CacheConfig.from_data_root(data_root)
            if data_root is not None
            else CacheConfig(DATA_ROOT=Path(DEFAULT_DATA_ROOT))
        )
        self._memory = MemoryCache(self._config.LRU_MAX_SIZE)
        self._parquet = ParquetReader(self._config)
        self._trade_calendar_identity: Optional[TradeCalendarIdentity] = None

        logger.info("CacheManager initialized (Parquet mode)")

    @property
    def config(self) -> CacheConfig:
        """Return this manager's resolved, instance-local configuration."""

        return self._config

    @property
    def trade_calendar_identity(self) -> TradeCalendarIdentity:
        """Identify the exact local override or bundled fallback in use."""

        if self._trade_calendar_identity is None:
            self.get_trade_cal()
        if self._trade_calendar_identity is None:  # defensive: load failed
            raise DataNotFoundError("trade calendar identity is unavailable")
        return self._trade_calendar_identity

    def _read_metadata(self, category: str) -> pd.DataFrame:
        """
        读取元数据 (仅 Parquet)

        Args:
            category: 数据类别 ('trade_cal', 'stock_basic', 'industry_mapping')

        Returns:
            DataFrame，如果文件不存在返回空 DataFrame
        """
        parquet_file = self._config.METADATA_PARQUET.get(category)
        if not parquet_file:
            logger.warning(f"Unknown metadata category: {category}")
            return pd.DataFrame()

        parquet_path = self._config.METADATA_ROOT / parquet_file
        metadata_root = self._config.DATA_ROOT or self._config.METADATA_ROOT
        try:
            metadata_exists = plain_file_exists(
                parquet_path,
                root=metadata_root,
                label=f"{category} metadata",
            )
        except Exception as e:
            if category == 'trade_cal':
                raise DataNotFoundError(
                    "local trade-calendar override failed strict validation: "
                    f"{type(e).__name__}: {e}"
                ) from e
            logger.error("Metadata path failed strict validation: %s", category)
            return pd.DataFrame()
        if not metadata_exists:
            if category == 'trade_cal':
                frame = load_builtin_trade_calendar()
                self._trade_calendar_identity = builtin_calendar_identity()
                logger.info(
                    "Local trade-calendar override is absent; using bundled %s",
                    self._trade_calendar_identity.calendar_id,
                )
                return frame
            logger.error(f"Metadata file not found: {parquet_path}")
            logger.error(f"Please run the data update scripts to generate Parquet files")
            return pd.DataFrame()

        try:
            max_bytes = (
                TRADE_CALENDAR_PARQUET_MAX_BYTES
                if category == 'trade_cal'
                else METADATA_PARQUET_MAX_BYTES
            )
            frame = read_plain_parquet(
                parquet_path,
                root=metadata_root,
                max_bytes=max_bytes,
                label=f"{category} metadata",
            )
            if category == 'trade_cal':
                # File presence means complete replacement, never a partial
                # merge with bundled dates.  Invalid/stale overrides fail
                # closed instead of silently changing the market clock.
                self._trade_calendar_identity = identify_trade_calendar(
                    frame, source="local_override"
                )
            return frame
        except Exception as e:
            if category == 'trade_cal':
                raise DataNotFoundError(
                    "local trade-calendar override failed strict validation: "
                    f"{type(e).__name__}: {e}"
                ) from e
            logger.error(f"Failed to read Parquet {parquet_path}: {e}")
            return pd.DataFrame()

    def _read_onecsv(self, category: str) -> pd.DataFrame:
        """
        读取 onecsv 基础数据 (deprecated, 仅为兼容)

        新代码应使用 _read_metadata() 从 Parquet 读取
        """
        csv_file = self._config.ONECSV_FILES.get(category)
        if not csv_file:
            logger.warning(f"Unknown onecsv category: {category}")
            return pd.DataFrame()

        csv_path = self._config.ONECSV_DIR / csv_file
        if not csv_path.exists():
            logger.warning(f"CSV not found: {csv_path}")
            return pd.DataFrame()

        try:
            return pd.read_csv(csv_path, encoding=self._config.CSV_ENCODING)
        except Exception as e:
            logger.warning(f"Failed to read CSV {csv_path}: {e}")
            return pd.DataFrame()

    def get_trade_cal(self) -> pd.DataFrame:
        """获取交易日历 (从 Parquet 读取)"""
        if self._memory.trade_cal is None:
            self._memory.trade_cal = self._read_metadata('trade_cal')
        return self._memory.trade_cal.copy()

    def get_stock_info(self) -> pd.DataFrame:
        """获取股票信息 (从 Parquet 读取)"""
        if self._memory.stock_info is None:
            df = self._read_metadata('stock_basic')
            # Rename 'symbol' to 'ts_code' for backward compatibility
            if 'symbol' in df.columns and 'ts_code' not in df.columns:
                df = df.rename(columns={'symbol': 'ts_code'})
            self._memory.stock_info = df
        return self._memory.stock_info.copy()

    def get_data(
        self,
        category: str,
        symbol: str,
        *,
        years: Optional[Iterable[str]] = None,
    ) -> pd.DataFrame:
        """
        通用数据获取接口

        1. 检查 L1 内存缓存
        2. 未命中则从 Parquet 读取
        3. 存入 L1 并返回
        """
        canonical_years = None
        if years is not None:
            canonical_years = tuple(sorted(set(str(value) for value in years)))
        cache_key = f"{category}:{symbol}"
        if canonical_years is not None:
            cache_key += ":" + ",".join(canonical_years)

        # L1 检查
        df = self._memory.get(cache_key)
        if df is not None:
            return df.copy()

        # L2 Parquet 读取
        df = self._parquet.read(
            category, symbol, years=canonical_years
        )

        if not df.empty:
            self._memory.put(cache_key, df)
            return df.copy()

        return df

    # ========== 便捷方法 ==========

    def get_daily(self, symbol: str) -> pd.DataFrame:
        """获取日线数据 (后复权)"""
        return self.get_data('daily_data', symbol)

    def get_minute(self, symbol: str) -> pd.DataFrame:
        """获取分钟数据 (后复权)"""
        return self.get_data('minute_data', symbol)

    def get_daily_raw(self, symbol: str) -> pd.DataFrame:
        """获取日线数据 (原始)"""
        return self.get_data('daily_data_raw', symbol)

    def get_minute_raw(self, symbol: str) -> pd.DataFrame:
        """获取分钟数据 (原始)"""
        return self.get_data('minute_data_raw', symbol)

    def get_cyq(self, symbol: str) -> pd.DataFrame:
        """获取筹码分布"""
        return self.get_data('cyq_chips', symbol)

    def get_moneyflow(self, symbol: str) -> pd.DataFrame:
        """获取资金流向"""
        return self.get_data('moneyflow_data', symbol)

    def get_margin(self, symbol: str) -> pd.DataFrame:
        """获取融资融券"""
        return self.get_data('margin_detail', symbol)

    def get_basic(self, symbol: str) -> pd.DataFrame:
        """获取基本面数据"""
        return self.get_data('daily_basic', symbol)

    def get_adj_factor(self, symbol: str) -> pd.DataFrame:
        """获取复权因子"""
        return self.get_data('adj_factor', symbol)

    def get_holder_trade(self, symbol: str) -> pd.DataFrame:
        """获取股东增减持"""
        return self.get_data('holder_trade', symbol)

    # ========== onecsv 数据 ==========

    def get_industry_mapping(self) -> pd.DataFrame:
        """获取行业分类映射 (从 Parquet 读取)"""
        cache_key = 'industry_mapping'
        df = self._memory.get(cache_key)
        if df is not None:
            return df.copy()

        df = self._read_metadata('industry_mapping')
        if not df.empty:
            self._memory.put(cache_key, df)
        return df.copy() if not df.empty else df

    def get_block_trade(self) -> pd.DataFrame:
        """获取大宗交易"""
        cache_key = 'block_trade'
        df = self._memory.get(cache_key)
        if df is not None:
            return df.copy()

        df = self._read_onecsv('block_trade')
        if not df.empty:
            self._memory.put(cache_key, df)
        return df.copy() if not df.empty else df

    def get_top_list(self) -> pd.DataFrame:
        """获取龙虎榜"""
        cache_key = 'top_list'
        df = self._memory.get(cache_key)
        if df is not None:
            return df.copy()

        df = self._read_onecsv('top_list')
        if not df.empty:
            self._memory.put(cache_key, df)
        return df.copy() if not df.empty else df

    # ========== 缓存管理 ==========

    def clear_cache(self, level: str = 'all') -> None:
        """
        清除缓存

        Args:
            level: 'l1' 或 'all' (L2 是 Parquet 文件，不需要清除)
        """
        if level in ('l1', 'all'):
            self._memory.invalidate()
            self._trade_calendar_identity = None
        logger.info(f"Cache cleared: {level}")

    def get_stats(self) -> Dict:
        """获取缓存统计"""
        stats = {
            'memory': self._memory.get_stats(),
        }
        if self._trade_calendar_identity is not None:
            stats['trade_calendar'] = self._trade_calendar_identity.to_dict()
        return stats
