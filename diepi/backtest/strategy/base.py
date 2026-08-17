"""
策略基类

因果执行模型:
- on_before_market_open: 盘前 (09:00)，只见 T-1，订单可从 T 首个合法窗口生效
- on_day: 日线 T 收盘后的后验回调，订单最早从 T+1 生效
- on_minute: 每根已完成分钟 bar 后调用，订单不得回填当前 bar
- on_after_market_close: 盘后统计回调，不接受新交易订单
"""

from abc import ABC, abstractmethod
from typing import Optional, Dict, Any
from dataclasses import dataclass
import pandas as pd


@dataclass(frozen=True)
class OpenBarData:
    """Restricted daily opening observation.

    The callback intentionally receives no same-day high, low, close, volume,
    or turnover.  Keeping this as a distinct type makes accidental lookahead
    visible to both type checkers and runtime users.
    """

    symbol: str
    trade_time: pd.Timestamp
    open: float


@dataclass
class BarData:
    """K线数据"""
    symbol: str
    trade_time: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    vol: float = 0.0
    amount: float = 0.0

    @classmethod
    def from_series(cls, symbol: str, row: pd.Series) -> 'BarData':
        """从 DataFrame 行创建"""
        return cls(
            symbol=symbol,
            trade_time=row.name if isinstance(row.name, pd.Timestamp) else pd.to_datetime(row.name),
            open=row.get('open', 0),
            high=row.get('high', 0),
            low=row.get('low', 0),
            close=row.get('close', 0),
            vol=row.get('vol', 0),
            amount=row.get('amount', 0),
        )


class Strategy(ABC):
    """
    策略基类

    用户继承此类实现自己的策略逻辑

    Example:
        class MyStrategy(Strategy):
            def on_before_market_open(self, ctx):
                # 盘前逻辑
                pass

            def on_minute(self, ctx, bar):
                # 每分钟逻辑
                if bar.close > self.get_param('threshold', 10):
                    ctx.buy_at_market(shares=100)

            def on_after_market_close(self, ctx):
                # 盘后逻辑
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

        在回测开始前调用一次，用于初始化策略状态。此时尚无活动模拟
        日期，``ctx.current_date``/``ctx.current_time`` 为 ``None``；不带
        显式日期的历史数据或交易日历查询不可用。需要暖机数据时，应将
        查询移到 ``on_before_market_open``，或直接使用 DataProvider 并
        传入明确且因果安全的日期边界。

        Args:
            ctx: 回测上下文
        """
        pass

    def on_before_market_open(self, ctx) -> None:
        """
        盘前回调 (09:00)

        可见数据: 截至 T-1 的所有日线数据
        下单: 开盘单在 T 日 09:30 成交

        Args:
            ctx: 回测上下文
        """
        pass

    def on_minute(self, ctx, bar: BarData) -> None:
        """
        分钟回调 (09:30-15:00)

        每根已完成的分钟 K 线后调用一次

        Args:
            ctx: 回测上下文
            bar: 刚刚完成的当前 K 线（OHLC 均已知）

        注意:
            - ``bar`` 是策略在本回调中唯一直接取得的当前已完成 K 线
            - ``get_minute()`` 的闭区间查询边界包含当前这根已完成 ``bar``；
              不包含尚未完成或未来的分钟
            - 本回调创建的订单最早在下一有效执行窗口撮合；绝不回填当前 bar
            - 当天没有后续合法窗口时，DAY 订单在日终取消并释放冻结资源
        """
        pass

    def on_after_open(self, ctx, bar: OpenBarData) -> None:
        """Daily-only causal decision point after the opening match.

        ``bar`` contains only T's opening observation.  Daily history queried
        through ``ctx`` remains capped at T-1.  A CLOSE order submitted here
        may participate in T's closing window; other order types are deferred
        to their next legal bar by the engine lifecycle contract.
        """

        pass

    def on_before_close(self, ctx) -> None:
        """Minute-only callback immediately before a distinct close auction.

        Only already completed continuous-session observations are visible.
        The closing-auction bar has not been matched or exposed yet.
        """

        pass

    def on_day(self, ctx, bar: BarData) -> None:
        """
        日线回调 (日线模式)

        每日调用一次，适用于日线级别策略

        Args:
            ctx: 回测上下文
            bar: 当日K线 (包含完整的 open/high/low/close)

        注意:
            - 此方法仅在日线模式 (freq='daily') 下调用
            - 分钟模式下使用 on_minute()
            - 这是 T 收盘后的后验决策点，允许读取完整 T bar
            - 本回调创建的任何订单均不得在 T 成交，最早从 T+1 生效
            - 市价买入使用下一有效执行 bar 的 high+滑点，卖出使用 low-滑点
        """
        pass

    def on_after_market_close(self, ctx) -> None:
        """
        盘后回调 (15:00后)

        只用于统计、保存指标等；不得在此创建新交易订单

        Args:
            ctx: 回测上下文
        """
        pass

    def on_finish(self, ctx) -> None:
        """
        策略结束

        ``on_init`` 成功返回后，本方法恰好调用一次；无交易日或运行中异常
        也会调用。调用前引擎会取消未完成订单并释放冻结资源。

        若回测主流程已经抛出异常，本方法自己的异常只记录日志，不会掩盖
        原始异常；正常流程中本方法的异常会向调用者传播。``on_init`` 自身
        失败时不调用本方法。

        Args:
            ctx: 回测上下文
        """
        pass
