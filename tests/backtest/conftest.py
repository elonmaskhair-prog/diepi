# -*- coding: utf-8 -*-
"""
pytest fixtures - 共享的测试 fixtures

纯单元测试默认可运行；真实数据测试须显式标记 integration。
"""

import pytest
import pandas as pd
from datetime import datetime
import os



# ==================== integration 标记与无数据环境 skip ====================
# 只有测试自身显式声明 ``pytest.mark.integration`` 时，才把它视为依赖
# 本地真实行情仓库。不要按文件名猜测：同一文件里可以同时包含纯单元测试
# 和数据集成测试，文件级白名单会把前者一起误杀。

def _has_data_repo() -> bool:
    try:
        from diepi.backtest.config import METADATA_PATHS
        return os.path.isfile(METADATA_PATHS['trade_cal'])
    except Exception:
        return False


_HAS_DATA = _has_data_repo()

@pytest.fixture(autouse=True)
def _skip_integration_without_market_data(request):
    """Skip only explicitly marked data integration tests.

    An autouse fixture is intentionally used instead of a collection hook. A
    nested ``pytest_collection_modifyitems`` hook can see items outside its
    directory; that previously let the futures suite mark/skip the whole repo.
    """
    if request.node.get_closest_marker('integration') and not _HAS_DATA:
        pytest.skip(
            "需要本地真实数据仓库 (trade_cal 缺失)。数据准备见 README；"
            "纯单元测试模式: pytest -m 'not integration'"
        )


# ==================== 数据层 fixtures ====================

@pytest.fixture
def data_provider():
    """创建 DataProvider 实例"""
    from diepi.backtest.data.data_provider import DataProvider
    return DataProvider()


@pytest.fixture
def cache_manager():
    """创建 CacheManager 实例"""
    from diepi.backtest.data.cache_manager import CacheManager
    return CacheManager()


@pytest.fixture
def index_provider():
    """创建 IndexProvider 实例"""
    from diepi.backtest.data.index_provider import IndexProvider
    # 重置单例以确保测试隔离
    IndexProvider.reset_instance()
    return IndexProvider()


@pytest.fixture
def stock_pool(data_provider):
    """创建 StockPool 实例"""
    from diepi.backtest.data.stock_pool import StockPool
    return StockPool(data_provider=data_provider)


# ==================== 经纪商层 fixtures ====================

@pytest.fixture
def account():
    """创建测试账户"""
    from diepi.backtest.broker.account import Account
    return Account(initial_cash=1000000.0)


@pytest.fixture
def broker(account):
    """创建测试经纪商"""
    from diepi.backtest.broker.broker import Broker
    return Broker(account=account, slippage=0.001)


@pytest.fixture
def position():
    """创建测试持仓"""
    from diepi.backtest.broker.position import Position
    return Position(symbol='000001.SZ')


@pytest.fixture
def order():
    """创建测试订单"""
    from diepi.backtest.broker.order import Order, OrderSide, OrderType
    return Order(
        symbol='000001.SZ',
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        shares=1000,
    )


# ==================== 引擎层 fixtures ====================

@pytest.fixture
def context(broker, data_provider, monkeypatch):
    """创建测试上下文；价格转换使用不访问磁盘的恒等复权因子。"""
    from diepi.backtest.engine.context import Context
    monkeypatch.setattr(
        data_provider,
        'get_adj_ratio',
        lambda *args, **kwargs: 1.0,
    )
    ctx = Context(broker=broker, data_provider=data_provider)
    ctx.set_symbol('000001.SZ')
    ctx.set_datetime('2024-01-02 09:30:00')
    return ctx


@pytest.fixture
def backtest_engine():
    """创建回测引擎"""
    from diepi.backtest.engine.backtest_engine import BacktestEngine
    return BacktestEngine(
        symbol='000001.SZ',
        start_date='20240102',
        end_date='20240131',
        initial_cash=1000000.0,
        freq='daily',
    )


# ==================== 测试数据 fixtures ====================

@pytest.fixture
def sample_daily_data():
    """样本日线数据"""
    return pd.DataFrame({
        'trade_date': ['20240102', '20240103', '20240104', '20240105', '20240108'],
        'open': [10.0, 10.5, 10.2, 10.8, 11.0],
        'high': [10.8, 11.2, 10.9, 11.5, 11.3],
        'low': [9.8, 10.3, 10.0, 10.5, 10.8],
        'close': [10.5, 10.2, 10.8, 11.0, 11.2],
        'vol': [1000000, 1200000, 1100000, 1500000, 1300000],
        'amount': [10500000, 12600000, 11440000, 16500000, 14560000],
        'pre_close': [9.8, 10.5, 10.2, 10.8, 11.0],
    })


@pytest.fixture
def sample_minute_data():
    """样本分钟数据"""
    times = pd.date_range('2024-01-02 09:30:00', periods=5, freq='1min')
    return pd.DataFrame({
        'trade_time': times,
        'open': [10.0, 10.1, 10.2, 10.15, 10.25],
        'high': [10.15, 10.25, 10.3, 10.25, 10.35],
        'low': [9.95, 10.05, 10.15, 10.1, 10.2],
        'close': [10.1, 10.2, 10.15, 10.25, 10.3],
        'vol': [100000, 120000, 110000, 130000, 125000],
        'amount': [1010000, 1224000, 1122500, 1332500, 1287500],
    })


@pytest.fixture
def sample_bar_data():
    """样本 BarData"""
    from diepi.backtest.broker.broker import BarData
    return BarData(
        symbol='000001.SZ',
        trade_time=datetime(2024, 1, 2, 9, 30),
        open=10.0,
        high=10.5,
        low=9.8,
        close=10.2,
        vol=1000000,
        amount=10200000,
        pre_close=9.8,
    )


# ==================== 真实数据 fixtures ====================

@pytest.fixture
def real_trade_days(data_provider):
    """获取真实交易日列表 (2024年)"""
    return data_provider.get_trade_days_between('20240102', '20240131')


@pytest.fixture
def real_daily_data(data_provider):
    """获取真实日线数据"""
    return data_provider.get_daily('000001.SZ', start='20240102', end='20240131')


@pytest.fixture
def test_symbol():
    """测试股票代码"""
    return '000001.SZ'


@pytest.fixture
def test_date():
    """测试日期"""
    return '20240102'


@pytest.fixture
def test_date_range():
    """测试日期范围"""
    return ('20240102', '20240131')


# ==================== 清理 fixtures ====================

@pytest.fixture(autouse=True)
def cleanup_index_provider():
    """测试结束后清理 IndexProvider 单例"""
    yield
    from diepi.backtest.data.index_provider import IndexProvider
    IndexProvider.reset_instance()
