"""
组合策略基类

支持动态选股和多股票交易的策略基类
"""

from abc import ABC
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
import pandas as pd

from .base import BarData, OpenBarData


@dataclass(frozen=True)
class PortfolioOpenBarData:
    """Restricted per-symbol daily opening observations."""

    bars: Dict[str, OpenBarData] = field(default_factory=dict)
    trade_date: str = ""

    def get(self, symbol: str) -> Optional[OpenBarData]:
        return self.bars.get(symbol)

    def symbols(self) -> List[str]:
        return list(self.bars.keys())

    def __len__(self) -> int:
        return len(self.bars)


@dataclass
class PortfolioBarData:
    """
    组合K线数据

    包含多只股票的K线数据

    Attributes:
        bars: 股票K线字典 {symbol: BarData}
        trade_date: 交易日期 (YYYYMMDD)
    """
    bars: Dict[str, BarData] = field(default_factory=dict)
    trade_date: str = ""

    def get(self, symbol: str) -> Optional[BarData]:
        """
        获取指定股票的K线

        Args:
            symbol: 股票代码

        Returns:
            BarData 或 None
        """
        return self.bars.get(symbol)

    def symbols(self) -> List[str]:
        """
        获取所有股票代码

        Returns:
            股票代码列表
        """
        return list(self.bars.keys())

    def __len__(self) -> int:
        return len(self.bars)

    def __iter__(self):
        return iter(self.bars.items())

    def __contains__(self, symbol: str) -> bool:
        return symbol in self.bars


class PortfolioStrategy(ABC):
    """
    组合策略基类

    与单股票 Strategy 的区别:
    1. on_before_market_open: 可进行选股，返回当日交易股票池
    2. on_day/on_minute: 接收所有股票的 bar 数据
    3. 支持跨股票资金调配

    Example:
        class MyPortfolioStrategy(PortfolioStrategy):
            def on_before_market_open(self, ctx) -> List[str]:
                # 选股: 返回今日要交易的股票
                pool = ctx.get_stock_pool()
                # 简单示例: 选择前10只
                return pool[:10]

            def on_day(self, ctx, bars: PortfolioBarData):
                # 遍历所有股票
                for symbol in bars.symbols():
                    bar = bars.get(symbol)
                    pos = ctx.get_position(symbol)

                    # 无持仓则买入
                    if pos is None or pos.shares == 0:
                        ctx.buy_at_market(symbol, percent=0.1)

            def on_minute(self, ctx, bars: PortfolioBarData):
                # 分钟级交易逻辑
                pass
    """

    def __init__(self, params: Dict[str, Any] = None):
        """
        Args:
            params: 策略参数
        """
        self._params = params or {}
        self._context = None

    def set_context(self, context) -> None:
        """设置回测上下文"""
        self._context = context

    def get_param(self, key: str, default: Any = None) -> Any:
        """获取策略参数"""
        return self._params.get(key, default)

    def set_param(self, key: str, value: Any) -> None:
        """设置策略参数"""
        self._params[key] = value

    @property
    def params(self) -> Dict[str, Any]:
        """获取所有参数"""
        return self._params.copy()

    # ==================== 策略回调 ====================

    def on_init(self, ctx) -> None:
        """
        策略初始化

        在回测开始前调用一次，用于初始化策略状态

        Args:
            ctx: 组合回测上下文 (PortfolioContext)
        """
        pass

    def on_before_market_open(self, ctx) -> Optional[List[str]]:
        """
        盘前回调 - 选股阶段

        每日盘前调用，可用于:
        1. 动态选股
        2. 下开盘单

        Args:
            ctx: 组合回测上下文

        Returns:
            今日要交易的股票列表
            - 返回 None 表示使用全部股票池
            - 返回空列表表示今日不交易
            - 返回股票列表表示只交易这些股票

        Example:
            def on_before_market_open(self, ctx):
                pool = ctx.get_stock_pool()
                # 筛选股票
                selected = []
                for symbol in pool:
                    daily = ctx.get_daily(symbol, days=5)
                    if not daily.empty:
                        # 5日涨幅超过10%
                        ret = daily['close'].iloc[-1] / daily['close'].iloc[0] - 1
                        if ret > 0.1:
                            selected.append(symbol)
                return selected[:10]  # 最多10只
        """
        return None  # 默认使用全部股票池

    def on_day(self, ctx, bars: PortfolioBarData) -> None:
        """
        日线回调 (日线模式)

        T 日收盘后调用一次。可读取完整 T 日 K 线，但这里创建的任何订单
        最早只能从 T+1 的合法执行窗口生效，绝不会回填 T 日。

        Args:
            ctx: 组合回测上下文
            bars: 所有活跃股票的当日K线

        Example:
            def on_day(self, ctx, bars):
                for symbol in bars.symbols():
                    bar = bars.get(symbol)
                    pos = ctx.get_position(symbol)

                    # 买入逻辑
                    if pos is None and bar.close > bar.open:
                        ctx.buy_at_market(symbol, percent=0.1)

                    # 卖出逻辑
                    elif pos and pos.available_shares > 0:
                        if bar.close < bar.open:
                            ctx.sell_at_market(symbol, percent=1.0)
        """
        pass

    def on_minute(self, ctx, bars: PortfolioBarData) -> None:
        """
        分钟回调 (分钟模式)

        每根已完成分钟 K 线后调用一次 (09:30-15:00)。本回调创建的
        订单最早在下一有效执行窗口撮合，不得回填 ``bars`` 中的当前 K 线。

        Args:
            ctx: 组合回测上下文
            bars: 所有活跃股票的当前分钟K线
        """
        pass

    def on_after_open(self, ctx, bars: PortfolioOpenBarData) -> None:
        """Daily-only causal callback with T open and T-1 history only."""

        pass

    def on_before_close(self, ctx) -> None:
        """Minute-only callback before any symbol's close-auction match."""

        pass

    def on_after_market_close(self, ctx) -> None:
        """
        盘后回调

        每日收盘后调用，只用于统计、记录等；不得创建新交易订单

        Args:
            ctx: 组合回测上下文
        """
        pass

    def on_finish(self, ctx) -> None:
        """
        策略结束

        在回测结束后调用一次

        Args:
            ctx: 组合回测上下文
        """
        pass
