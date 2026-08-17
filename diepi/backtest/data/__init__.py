"""
数据层模块

提供数据缓存和数据访问接口
"""

from .exceptions import DataProviderError, DataNotFoundError, ParameterError, CacheError
from .cache_manager import CacheManager, CacheConfig
from .data_provider import DataProvider
from .calendar import (
    BUILTIN_CALENDAR_END,
    BUILTIN_CALENDAR_ID,
    BUILTIN_CALENDAR_START,
    TradeCalendarIdentity,
    builtin_calendar_identity,
)
from .stock_pool import StockPool, PoolSource
from .index_provider import IndexProvider, INDEX_LIST
from .result_storage import ResultStorage
from .report_generator import ReportGenerator
from .dataset_manifest import DatasetManifest
from .validation_service import (
    DataValidationReport,
    DataValidationScope,
    validate_local_data,
)
from .extraction_service import (
    ExtractedWorkspace,
    ExtractionScope,
    extract_local_data,
)

__all__ = [
    'CacheManager',
    'CacheConfig',
    'DataProvider',
    'BUILTIN_CALENDAR_END',
    'BUILTIN_CALENDAR_ID',
    'BUILTIN_CALENDAR_START',
    'TradeCalendarIdentity',
    'builtin_calendar_identity',
    'DataProviderError',
    'DataNotFoundError',
    'ParameterError',
    'CacheError',
    'StockPool',
    'PoolSource',
    'IndexProvider',
    'INDEX_LIST',
    'ResultStorage',
    'ReportGenerator',
    'DatasetManifest',
    'DataValidationReport',
    'DataValidationScope',
    'validate_local_data',
    'ExtractedWorkspace',
    'ExtractionScope',
    'extract_local_data',
]
