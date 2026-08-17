"""
全局配置

统一管理数据路径，自动检测数据根目录，支持移动硬盘/不同盘符
"""

import os
from pathlib import Path


def _source_checkout_root():
    """Return the repository root when running from a source checkout."""

    package_root = Path(__file__).resolve().parents[1]
    candidate = package_root.parent
    if (
        package_root.name == "diepi"
        and (candidate / "pyproject.toml").is_file()
        and (candidate / "diepi").resolve() == package_root
    ):
        return candidate
    return None


_SOURCE_CHECKOUT_ROOT = _source_checkout_root()


def _detect_data_root() -> str:
    """
    自动检测数据根目录

    优先级:
    1. 环境变量 DATA_ROOT（保留原值；默认 DataProvider 初始化时验证，
       使 doctor/显式 data_root 可以在同一进程诊断错误环境变量）
    2. 从脚本位置推断 (config.py -> backtest/ -> 仓库根 -> 父目录)
    """
    # 1. 环境变量
    env_root = os.environ.get('DATA_ROOT')
    if env_root:
        return str(Path(env_root).expanduser().resolve())

    # 2. Source checkouts retain the historical repository-parent data root.
    if _SOURCE_CHECKOUT_ROOT is not None:
        return str(_SOURCE_CHECKOUT_ROOT.parent)

    # 3. Installed wheels must not infer writable/data paths from site-packages.
    return str(Path.cwd().resolve())


def _detect_results_dir() -> str:
    """Resolve the shared CLI/GUI result root without writing into packages."""

    env_root = os.environ.get("DIEPI_RESULTS_DIR")
    if env_root:
        return str(Path(env_root).expanduser().resolve())
    runtime_root = _SOURCE_CHECKOUT_ROOT or Path.cwd().resolve()
    return str(runtime_root / "diepi_results")


# ==================== 数据根目录 (自动检测) ====================
DATA_ROOT = _detect_data_root()

# ==================== Parquet 数据目录 ====================
# 股票行情数据 (日线、分钟线、资金流等)
PARQUET_ROOT = os.path.join(DATA_ROOT, 'parquet', 'timeseries')

# 元数据目录 (交易日历、股票列表、行业分类等)
METADATA_ROOT = os.path.join(DATA_ROOT, 'parquet', 'metadata')

# 元数据路径映射 (仅 Parquet)
METADATA_PATHS = {
    'trade_cal': os.path.join(METADATA_ROOT, 'common', 'trade_cal.parquet'),
    'stock_basic': os.path.join(METADATA_ROOT, 'stock', 'basic.parquet'),
    'industry_mapping': os.path.join(METADATA_ROOT, 'common', 'industry', 'mapping.parquet'),
}

# 指数 Parquet 目录
INDEX_PARQUET_DIR = os.path.join(PARQUET_ROOT, 'index_daily')

# ==================== 输出目录 ====================
# CLI and GUI use the same result directory.  Override with
# DIEPI_RESULTS_DIR when a stable user-specific location is preferred.
RESULTS_DIR = _detect_results_dir()

# ==================== 旧路径 (deprecated, 仅为兼容) ====================
ONECSV_DIR = os.path.join(DATA_ROOT, 'onecsv')  # deprecated
INDEX_DIR = os.path.join(DATA_ROOT, 'index_daily')  # deprecated

# ==================== Price modes ====================
# strategy data (default): 'hfq' (back-adjusted) or 'raw'
PRICE_MODE_STRATEGY = "hfq"
# execution data: 'raw' for realistic fills, or 'hfq' to keep legacy behavior
PRICE_MODE_EXECUTION = "raw"
# UI display (charts/trades): usually match execution
PRICE_MODE_UI = "raw"
