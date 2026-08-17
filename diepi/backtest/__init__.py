"""
本地量化回测系统
"""

from .. import __version__

from .data import DataProvider, CacheManager, StockPool, PoolSource
from .broker import (
    Account,
    Broker,
    CashAuditBundle,
    CashEventJournalReducer,
    CashReplayError,
    CashReplayResult,
    CashReplaySeed,
    Order,
    Position,
    TargetAchievement,
    TargetAchievementStatus,
    TargetExecutionBundle,
    TargetIntent,
    TargetPlanSide,
    TargetWindow,
    cash_replay_trade_records,
    replay_cash_journal,
)
from .engine import (
    Context, BacktestEngine, BacktestResult,
    ParallelRunner, ParallelResult,
    PortfolioContext, PortfolioEngine, PortfolioResult,
)
from .strategy import Strategy, BarData, PortfolioStrategy, PortfolioBarData
from .liquidity import (
    AuctionCapMode,
    AuctionCapSpec,
    AuctionLiquidityUnavailable,
    AuctionWindow,
    DailyAuctionLiquidityPolicy,
    build_daily_auction_liquidity_policy,
)
from .result_contract import ResultContract, ResultStatus

__all__ = [
    # 数据层
    'DataProvider',
    'CacheManager',
    'StockPool',
    'PoolSource',
    # 交易层
    'Broker',
    'Account',
    'Order',
    'Position',
    'CashAuditBundle',
    'CashReplayError',
    'CashReplaySeed',
    'CashReplayResult',
    'CashEventJournalReducer',
    'cash_replay_trade_records',
    'replay_cash_journal',
    'TargetIntent',
    'TargetAchievement',
    'TargetAchievementStatus',
    'TargetPlanSide',
    'TargetWindow',
    'TargetExecutionBundle',
    # 引擎层
    'Context',
    'BacktestEngine',
    'BacktestResult',
    'ParallelRunner',
    'ParallelResult',
    'PortfolioContext',
    'PortfolioEngine',
    'PortfolioResult',
    # 策略层
    'Strategy',
    'BarData',
    'PortfolioStrategy',
    'PortfolioBarData',
    # 鏄惧紡妯″瀷鍋囪涓庣粨鏋滃绾?
    'AuctionCapMode',
    'AuctionCapSpec',
    'AuctionLiquidityUnavailable',
    'AuctionWindow',
    'DailyAuctionLiquidityPolicy',
    'build_daily_auction_liquidity_policy',
    'ResultContract',
    'ResultStatus',
]
