"""
自定义异常类
"""


class DataProviderError(Exception):
    """数据提供者基础异常"""
    pass


class DataNotFoundError(DataProviderError):
    """数据文件未找到"""
    pass


class ParameterError(DataProviderError):
    """参数错误"""
    pass


class CacheError(DataProviderError):
    """缓存操作错误"""
    pass
