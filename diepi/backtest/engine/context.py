"""
回测上下文

提供策略与系统交互的统一接口
"""

from typing import Optional, Dict, List, Union
from datetime import datetime
import pandas as pd

from ..broker import Broker, Order, Position, Account, TargetIntent
from ..broker.broker import BarData
from ..broker.validation import finite_float, nonnegative_float
from ..data import DataProvider
from .price_mode import PriceModeMixin


class Context(PriceModeMixin):
    """
    回测上下文

    策略通过 Context 与回测系统交互:
    - 获取当前时间
    - 下单交易
    - 查询持仓
    - 获取数据

    Attributes:
        current_date: 当前交易日期 (YYYYMMDD)
        current_time: 当前时间 (datetime)
        current_symbol: 当前处理的股票代码
    """

    def __init__(self, broker: Broker = None, data_provider: DataProvider = None,
                 initial_cash: float = 1000000.0):
        """
        Args:
            broker: 撮合引擎
            data_provider: 数据提供者
            initial_cash: 初始资金
        """
        self._broker = broker or Broker(Account(initial_cash=initial_cash))
        self._data = data_provider or DataProvider()

        # 设置数据提供者的上下文
        self._data.set_context(self)

        self._strategy_price_mode = getattr(self._data, 'price_mode', 'hfq')
        self._execution_price_mode = getattr(self._data, 'execution_price_mode', self._strategy_price_mode)


        # 当前时间状态
        self.current_date: Optional[str] = None
        self.current_time: Optional[datetime] = None
        self.current_symbol: Optional[str] = None
        # Current execution-lane bar used only for conservative reservation
        # estimates.  Strategy callbacks still receive their strategy lane.
        self._current_bar = None

    # ==================== 时间属性 ====================

    def set_datetime(self, dt: Union[str, datetime]) -> None:
        """
        设置当前时间

        Args:
            dt: 时间 (datetime 或 'YYYY-MM-DD HH:MM:SS' 格式字符串)
        """
        if isinstance(dt, str):
            dt = pd.to_datetime(dt)

        self.current_time = dt
        self.current_date = dt.strftime('%Y%m%d')
        self._broker.set_date(self.current_date)

    def set_symbol(self, symbol: str) -> None:
        """设置当前股票代码"""
        self.current_symbol = symbol

    def set_market_phase(self, phase: str) -> None:
        """
        设置交易时段

        Args:
            phase: 'before_open', 'trading', 'after_close'
        """
        self._broker.set_market_phase(phase)

    # ==================== 交易接口 ====================

    def buy_at_open(self, symbol: str = None, amount: float = None,
                    shares: int = None, percent: float = None,
                    note: str = '') -> Order:
        """
        开盘价买入

        Args:
            symbol: 股票代码，None使用当前股票
            amount: 买入金额
            shares: 买入股数
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）
            note: 备注

        Returns:
            Order 对象
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        return self._broker.buy_at_open(symbol, amount, shares, percent, note)

    def buy_at_market(self, symbol: str = None, amount: float = None,
                      shares: int = None, percent: float = None,
                      note: str = '', current_price: float = None) -> Order:
        """
        市价买入

        Args:
            symbol: 股票代码
            amount: 买入金额
            shares: 买入股数
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）
            current_price: 当前价格（用于资金估算）

        Returns:
            Order 对象
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        derived_price = False
        if current_price is None:
            bar = self._current_bar
            if bar is not None and getattr(bar, 'symbol', None) == symbol:
                current_price = float(bar.high) * (1 + self._broker.slippage)
                derived_price = True
        if current_price is not None and not derived_price:
            current_price = self._convert_price_for_execution(symbol, current_price)
        return self._broker.buy_at_market(symbol, amount, shares, percent, note, current_price)

    def buy_at_price(self, symbol: str = None, price: float = None,
                     amount: float = None, shares: int = None,
                     percent: float = None, note: str = '') -> Order:
        """
        限价买入

        Args:
            symbol: 股票代码
            price: 限价
            amount: 买入金额
            shares: 买入股数
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）

        Returns:
            Order 对象
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        if price is None:
            raise ValueError("Price required for limit order")
        price = self._convert_price_for_execution(symbol, price)
        return self._broker.buy_at_price(symbol, price, amount, shares, percent, note)

    def sell_at_open(self, symbol: str = None, shares: int = None,
                     percent: float = None, note: str = '') -> Order:
        """
        开盘价卖出

        Args:
            symbol: 股票代码
            shares: 卖出股数
            percent: 占可卖持仓比例 (1.0 = 全部)

        Returns:
            Order 对象
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        return self._broker.sell_at_open(symbol, shares, percent, note)

    def sell_at_market(self, symbol: str = None, shares: int = None,
                       percent: float = None, note: str = '') -> Order:
        """
        市价卖出

        Args:
            symbol: 股票代码
            shares: 卖出股数
            percent: 占可卖持仓比例

        Returns:
            Order 对象
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        return self._broker.sell_at_market(symbol, shares, percent, note)

    def sell_at_price(self, symbol: str = None, price: float = None,
                      shares: int = None, percent: float = None,
                      note: str = '') -> Order:
        """
        限价卖出

        Args:
            symbol: 股票代码
            price: 限价
            shares: 卖出股数
            percent: 占可卖持仓比例

        Returns:
            Order 对象
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        if price is None:
            raise ValueError("Price required for limit order")
        price = self._convert_price_for_execution(symbol, price)
        return self._broker.sell_at_price(symbol, price, shares, percent, note)

    # ==================== 收盘价交易接口 ====================

    def buy_at_close(self, symbol: str = None, amount: float = None,
                     shares: int = None, percent: float = None,
                     note: str = '', current_price: float = None) -> Order:
        """
        收盘价买入

        注意: 只能在交易时段 (on_day) 调用

        Args:
            symbol: 股票代码，None使用当前股票
            amount: 买入金额
            shares: 买入股数
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）
            note: 备注

        Returns:
            Order 对象
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        derived_price = False
        if current_price is None:
            bar = self._current_bar
            if bar is not None and getattr(bar, 'symbol', None) == symbol:
                current_price = float(bar.close) * (1 + self._broker.slippage)
                derived_price = True
        if current_price is not None and not derived_price:
            current_price = self._convert_price_for_execution(symbol, current_price)
        return self._broker.buy_at_close(symbol, amount, shares, percent, note, current_price)

    def sell_at_close(self, symbol: str = None, shares: int = None,
                      percent: float = None, note: str = '') -> Order:
        """
        收盘价卖出

        注意: 只能在交易时段 (on_day) 调用

        Args:
            symbol: 股票代码，None使用当前股票
            shares: 卖出股数
            percent: 占可卖持仓比例

        Returns:
            Order 对象
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        return self._broker.sell_at_close(symbol, shares, percent, note)

    def schedule_at_close(
        self,
        side: str,
        symbol: str = None,
        *,
        amount: float = None,
        shares: int = None,
        percent: float = None,
        note: str = '',
        current_price: float = None,
    ) -> Order:
        """Submit explicit CLOSE intent from a causal close-scheduling hook."""

        if type(side) is not str:
            raise TypeError("side must be exactly str")
        canonical = side.strip().lower()
        if canonical == 'buy':
            return self.buy_at_close(
                symbol=symbol,
                amount=amount,
                shares=shares,
                percent=percent,
                note=note,
                current_price=current_price,
            )
        if canonical == 'sell':
            if amount is not None or current_price is not None:
                raise ValueError(
                    "sell CLOSE intent does not accept amount/current_price")
            return self.sell_at_close(
                symbol=symbol,
                shares=shares,
                percent=percent,
                note=note,
            )
        raise ValueError("side must be 'buy' or 'sell'")

    def order_target_percent(
            self, symbol: str = None, target_weight: float = 0.0,
            when: str = 'open', min_trade_amount: float = 0.0,
            note: str = '') -> Optional[Union[Order, TargetIntent]]:
        """Target account exposure; close targets remain unsized intents."""

        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        if when not in ('open', 'close'):
            raise ValueError("when must be 'open' or 'close'")
        target_weight = finite_float(target_weight, 'target_weight')
        if target_weight < 0 or target_weight > 1:
            raise ValueError("target_weight must be between 0 and 1")
        min_trade_amount = nonnegative_float(
            min_trade_amount, 'min_trade_amount')
        if when == 'close':
            return self._broker.submit_target_intent(
                symbol,
                target_weight,
                min_trade_amount=min_trade_amount,
                note=note,
            )

        total = self._broker.account.total_asset
        if total <= 0:
            return None
        position = self._broker.get_position(symbol)
        position_value = 0.0
        if position is not None and position.shares > 0:
            price = (
                position.current_price
                if position.current_price > 0 else position.avg_cost)
            position_value = position.shares * price
        delta = target_weight * total - position_value
        clearing = (
            target_weight == 0.0
            and position is not None and position.shares > 0)
        if not clearing and abs(delta) < max(min_trade_amount, 1e-6):
            return None
        if delta > 0:
            return self.buy_at_open(
                symbol=symbol, amount=delta, note=note)
        if position is None or position.shares <= 0 or position_value <= 0:
            return None
        shares = int(round(position.shares * (-delta) / position_value))
        shares = min(shares, position.available_shares)
        if shares <= 0:
            return None
        return self.sell_at_open(symbol=symbol, shares=shares, note=note)

    def rebalance(self, weights: Dict[str, float], when: str = 'open',
                  min_trade_amount: float = 0.0,
                  note: str = '') -> List[Union[Order, TargetIntent]]:
        """Batch target API; close intents from one call share a batch ID."""

        if when not in ('open', 'close'):
            raise ValueError("when must be 'open' or 'close'")
        validated = {}
        for symbol, raw_weight in weights.items():
            weight = finite_float(raw_weight, f'target_weight[{symbol}]')
            if weight < 0 or weight > 1:
                raise ValueError(
                    f"target_weight[{symbol}] must be between 0 and 1")
            validated[symbol] = weight
        min_trade_amount = nonnegative_float(
            min_trade_amount, 'min_trade_amount')
        if when == 'close':
            return list(self._broker.submit_target_intent_batch(
                validated,
                min_trade_amount=min_trade_amount,
                note=note,
            ))
        if sum(validated.values()) > 1.0 + 1e-9:
            raise ValueError("target weights exceed 1")
        result = []
        for symbol in sorted(validated):
            order = self.order_target_percent(
                symbol,
                validated[symbol],
                when='open',
                min_trade_amount=min_trade_amount,
                note=note,
            )
            if order is not None:
                result.append(order)
        return result

    # ==================== 止损止盈接口 ====================

    def sell_stop_loss(self, symbol: str = None, price: float = None,
                       shares: int = None, percent: float = None,
                       note: str = '') -> Order:
        """
        止损卖出 (价格下穿触发)

        当价格 <= stop_price 时触发卖出，用于保护仓位

        Args:
            symbol: 股票代码
            price: 止损价格
            shares: 卖出股数
            percent: 占可卖持仓比例 (1.0 = 全部)

        Returns:
            Order 对象
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        if price is None:
            raise ValueError("Stop price required")
        price = self._convert_price_for_execution(symbol, price)
        return self._broker.sell_stop_loss(symbol, price, shares, percent, note)

    # 兼容旧名称
    sell_stop = sell_stop_loss

    def sell_stop_profit(self, symbol: str = None, price: float = None,
                         shares: int = None, percent: float = None,
                         note: str = '') -> Order:
        """
        止盈卖出 (价格上穿触发)

        当价格 >= profit_price 时触发卖出，用于锁定利润

        Args:
            symbol: 股票代码
            price: 止盈价格
            shares: 卖出股数
            percent: 占可卖持仓比例 (1.0 = 全部)

        Returns:
            Order 对象
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        if price is None:
            raise ValueError("Profit price required")
        price = self._convert_price_for_execution(symbol, price)
        return self._broker.sell_stop_profit(symbol, price, shares, percent, note)

    def buy_stop(self, symbol: str = None, price: float = None,
                 shares: int = None, amount: float = None,
                 percent: float = None, note: str = '',
                 current_price: float = None) -> Order:
        """
        突破买入 (价格上穿触发)

        当价格 >= trigger_price 时触发买入，用于追涨/突破策略

        Args:
            symbol: 股票代码
            price: 触发价格
            shares: 买入股数
            amount: 买入金额
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）

        Returns:
            Order 对象
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            raise ValueError("Symbol required")
        if price is None:
            raise ValueError("Trigger price required")
        price = self._convert_price_for_execution(symbol, price)
        if current_price is not None:
            current_price = self._convert_price_for_execution(symbol, current_price)
        return self._broker.buy_stop(symbol, price, shares, amount, percent, note, current_price)

    # ==================== 取消订单接口 ====================

    def cancel_order(self, order_id: str) -> bool:
        """
        取消指定订单

        Args:
            order_id: 订单ID

        Returns:
            是否取消成功
        """
        return self._broker.cancel_order(order_id)

    def cancel_orders(self, symbol: str = None, side: str = None) -> List[Order]:
        """
        批量取消订单

        Args:
            symbol: 股票代码。None = 当前标的（单标的引擎语境）。
                注意与组合上下文 PortfolioContext.cancel_orders 的差异：
                组合侧 None = 全部标的。
            side: 'buy'/'sell' (None=所有方向)

        Returns:
            已取消的订单列表
        """
        if symbol is None:
            symbol = self.current_symbol
        return self._broker.cancel_orders(symbol, side)

    def cancel_all_orders(self) -> List[Order]:
        """取消所有未完成订单"""
        return self._broker.cancel_all_orders()

    # ==================== 查询接口 ====================

    def get_cash(self) -> float:
        """获取可用现金"""
        return self._broker.get_cash()

    def get_total_asset(self) -> float:
        """获取总资产"""
        return self._broker.get_total_asset()

    def get_position(self, symbol: str = None) -> Optional[Position]:
        """
        获取持仓

        Args:
            symbol: 股票代码，None使用当前股票

        Returns:
            Position 对象，无持仓返回 None
        """
        symbol = symbol or self.current_symbol
        if symbol is None:
            return None
        return self._broker.get_position(symbol)

    def get_positions(self) -> Dict[str, Position]:
        """获取所有持仓"""
        return self._broker.get_positions()

    def get_order(self, order_id: str) -> Optional[Order]:
        """获取订单"""
        return self._broker.get_order(order_id)

    def get_open_orders(self) -> List[Order]:
        """获取未完成订单"""
        return self._broker.get_open_orders()

    def get_target_intents(self) -> tuple:
        """Return the broker's immutable target-decision snapshot."""

        return self._broker.target_intents_snapshot()

    def get_target_achievements(self) -> tuple:
        """Return the broker's immutable target-outcome snapshot."""

        return self._broker.target_achievements_snapshot()

    # ==================== 数据边界方法 ====================

    def _get_daily_boundary(self) -> str:
        """
        获取日级别数据可见边界日期

        盘前/盘中: T-1 (前一交易日)
        盘后: T (当日)
        """
        if self.current_date is None:
            raise RuntimeError(
                "historical data is unavailable during on_init because no "
                "simulation date is active; move the warm-up read to "
                "on_before_market_open or query DataProvider with an explicit "
                "causal end date"
            )
        market_phase = self._broker._market_phase
        if (market_phase == 'after_close'
                or self._broker.current_created_phase == 'on_day'):
            return self.current_date  # 盘后可见当日
        else:
            return self.get_prev_trade_day(self.current_date)  # 盘前/盘中只见T-1

    def _get_minute_boundary(self) -> tuple:
        """
        获取分钟数据可见边界

        Returns:
            (end_date, end_time) - end_time 为 None 表示全天
        """
        if self.current_date is None:
            raise RuntimeError(
                "historical data is unavailable during on_init because no "
                "simulation date is active; move the warm-up read to "
                "on_before_market_open or query DataProvider with an explicit "
                "causal end date"
            )
        market_phase = self._broker._market_phase
        created_phase = self._broker.current_created_phase
        if (market_phase == 'after_close'
                or created_phase == 'on_day'):
            return (self.current_date, None)  # 盘后：T全天
        if created_phase == 'on_after_open':
            # ``on_after_open`` receives only the auction/open snapshot.  A
            # complete 09:30 minute bar contains observations made after that
            # decision point, so exposing it here would be a same-day look-ahead.
            return (self.get_prev_trade_day(self.current_date), None)
        elif market_phase == 'trading':
            # 盘中：当前分钟-1
            return (self.current_date, self.current_time)
        else:
            # 盘前：T-1全天
            return (self.get_prev_trade_day(self.current_date), None)

    # ==================== 数据接口辅助方法 ====================

    def _resolve_daily_range(self, days: int = None, start_date: str = None,
                             end_date: str = None) -> tuple:
        """
        解析日级别数据的日期范围

        参数组合规则:
        - days=5: 从可见边界向前推5个交易日
        - start_date: 从指定日期到可见边界
        - end_date: 从历史第一天到指定日期（截断到边界）
        - start_date + end_date: 指定区间（截断到边界）
        - days + start_date: 从指定日期往后推N个交易日（截断到边界）
        - days + end_date: 从指定日期往前推N个交易日（截断到边界）
        - days + start_date + end_date: 报错

        Returns:
            (start, end) 元组
        """
        boundary = self._get_daily_boundary()

        # 检查不允许的组合
        if days is not None and start_date is not None and end_date is not None:
            raise ValueError("Cannot specify days, start_date and end_date at the same time")

        # 至少需要一个参数
        if days is None and start_date is None and end_date is None:
            raise ValueError("At least one of days, start_date, end_date is required")

        # 情况1: 只有 days - 从边界向前推
        if days is not None and start_date is None and end_date is None:
            end = boundary
            if days == 1:
                start = end
            else:
                start = self.get_prev_trade_day(end, days - 1)
            return (start, end)

        # 情况2: 只有 start_date - 从指定日期到边界
        if start_date is not None and end_date is None and days is None:
            start = start_date
            end = boundary
            return (start, end)

        # 情况3: 只有 end_date - 从历史第一天到指定日期（截断到边界）
        if end_date is not None and start_date is None and days is None:
            start = None  # 表示从最早开始
            end = min(end_date, boundary)
            return (start, end)

        # 情况4: start_date + end_date - 指定区间（截断到边界）
        if start_date is not None and end_date is not None and days is None:
            start = start_date
            end = min(end_date, boundary)
            return (start, end)

        # 情况5: days + start_date - 从指定日期往后推N个交易日（截断到边界）
        if days is not None and start_date is not None and end_date is None:
            start = start_date
            # 往后推 days-1 个交易日得到 end
            end = self._data.get_next_trade_day(start_date, days - 1)
            if end is None:
                end = boundary
            else:
                end = min(end, boundary)
            return (start, end)

        # 情况6: days + end_date - 从指定日期往前推N个交易日（截断到边界）
        if days is not None and end_date is not None and start_date is None:
            end = min(end_date, boundary)
            if days == 1:
                start = end
            else:
                start = self.get_prev_trade_day(end, days - 1)
            return (start, end)

        raise ValueError("Invalid parameter combination")

    def _resolve_minute_range(self, days: int = None, start_date: str = None,
                              end_date: str = None) -> tuple:
        """
        解析分钟级数据的日期范围

        Returns:
            (start_date, end_date, end_time) 元组
        """
        boundary_date, boundary_time = self._get_minute_boundary()

        # 检查不允许的组合
        if days is not None and start_date is not None and end_date is not None:
            raise ValueError("Cannot specify days, start_date and end_date at the same time")

        # 至少需要一个参数
        if days is None and start_date is None and end_date is None:
            raise ValueError("At least one of days, start_date, end_date is required")

        # 情况1: 只有 days - 从边界向前推
        if days is not None and start_date is None and end_date is None:
            end = boundary_date
            if days == 1:
                start = end
            else:
                start = self.get_prev_trade_day(end, days - 1)
            return (start, end, boundary_time)

        # 情况2: 只有 start_date - 从指定日期到边界
        if start_date is not None and end_date is None and days is None:
            start = start_date
            end = boundary_date
            return (start, end, boundary_time)

        # 情况3: 只有 end_date - 从历史第一天到指定日期（截断到边界）
        if end_date is not None and start_date is None and days is None:
            start = None
            if end_date >= boundary_date:
                end = boundary_date
                end_time = boundary_time
            else:
                end = end_date
                end_time = None  # 历史日期返回全天
            return (start, end, end_time)

        # 情况4: start_date + end_date - 指定区间（截断到边界）
        if start_date is not None and end_date is not None and days is None:
            start = start_date
            if end_date >= boundary_date:
                end = boundary_date
                end_time = boundary_time
            else:
                end = end_date
                end_time = None
            return (start, end, end_time)

        # 情况5: days + start_date - 从指定日期往后推N个交易日（截断到边界）
        if days is not None and start_date is not None and end_date is None:
            start = start_date
            end = self._data.get_next_trade_day(start_date, days - 1)
            if end is None or end >= boundary_date:
                end = boundary_date
                end_time = boundary_time
            else:
                end_time = None
            return (start, end, end_time)

        # 情况6: days + end_date - 从指定日期往前推N个交易日（截断到边界）
        if days is not None and end_date is not None and start_date is None:
            if end_date >= boundary_date:
                end = boundary_date
                end_time = boundary_time
            else:
                end = end_date
                end_time = None
            if days == 1:
                start = end
            else:
                start = self.get_prev_trade_day(end, days - 1)
            return (start, end, end_time)

        raise ValueError("Invalid parameter combination")

    # ==================== 数据接口 ====================

    def get_daily(self, symbol: str = None, days: int = None,
                  start_date: str = None, end_date: str = None,
                  fields: List[str] = None) -> pd.DataFrame:
        """
        获取日线数据

        Args:
            symbol: 股票代码
            days: 交易日步长
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            fields: 返回字段

        参数组合:
            - days=5: 从可见边界向前推5个交易日
            - start_date='20250101': 从指定日期到可见边界
            - end_date='20250301': 从历史第一天到指定日期（截断到边界）
            - start_date + end_date: 指定区间（截断到边界）
            - days + start_date: 从指定日期往后推N个交易日（截断到边界）
            - days + end_date: 从指定日期往前推N个交易日（截断到边界）

        Returns:
            DataFrame: index=trade_date
        """
        symbol = symbol or self.current_symbol
        start, end = self._resolve_daily_range(days, start_date, end_date)
        return self._data.get_daily(symbol, start=start, end=end, fields=fields, price_mode='strategy')

    def get_minute(self, symbol: str = None, days: int = None,
                   start_date: str = None, end_date: str = None,
                   start_time: str = None, end_time: str = None,
                   fields: List[str] = None) -> pd.DataFrame:
        """
        获取分钟数据

        Args:
            symbol: 股票代码
            days: 交易日步长
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            start_time: 每日开始时间 (HH:MM)
            end_time: 每日结束时间 (HH:MM)
            fields: 返回字段

        参数组合: 同 get_daily

        Returns:
            DataFrame: index=trade_time
        """
        symbol = symbol or self.current_symbol
        start, end, boundary_time = self._resolve_minute_range(days, start_date, end_date)

        return self._data.get_minute_by_days(
            symbol, start, end, boundary_time,
            start_time, end_time, fields, price_mode='strategy'
        )

    def get_cyq(self, symbol: str = None, days: int = None,
                start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        获取筹码分布

        Args:
            symbol: 股票代码
            days: 交易日步长
            start_date: 开始日期
            end_date: 结束日期

        参数组合: 同 get_daily

        Returns:
            DataFrame
        """
        symbol = symbol or self.current_symbol
        start, end = self._resolve_daily_range(days, start_date, end_date)
        return self._data.get_cyq(symbol, start=start, end=end)

    def get_moneyflow(self, symbol: str = None, days: int = None,
                      start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        获取资金流向

        Args:
            symbol: 股票代码
            days: 交易日步长
            start_date: 开始日期
            end_date: 结束日期

        参数组合: 同 get_daily

        Returns:
            DataFrame
        """
        symbol = symbol or self.current_symbol
        start, end = self._resolve_daily_range(days, start_date, end_date)
        return self._data.get_moneyflow(symbol, start=start, end=end)

    def get_margin(self, symbol: str = None, days: int = None,
                   start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        获取融资融券

        Args:
            symbol: 股票代码
            days: 交易日步长
            start_date: 开始日期
            end_date: 结束日期

        参数组合: 同 get_daily

        Returns:
            DataFrame
        """
        symbol = symbol or self.current_symbol
        start, end = self._resolve_daily_range(days, start_date, end_date)
        return self._data.get_margin(symbol, start=start, end=end)

    def get_basic(self, symbol: str = None, days: int = None,
                  start_date: str = None, end_date: str = None,
                  fields: List[str] = None) -> pd.DataFrame:
        """
        获取基本面数据

        Args:
            symbol: 股票代码
            days: 交易日步长
            start_date: 开始日期
            end_date: 结束日期
            fields: 返回字段

        参数组合: 同 get_daily

        Returns:
            DataFrame
        """
        symbol = symbol or self.current_symbol
        start, end = self._resolve_daily_range(days, start_date, end_date)
        return self._data.get_basic(symbol, start=start, end=end, fields=fields)

    def get_stock_info(self, symbol: str = None) -> pd.Series:
        """获取股票信息"""
        symbol = symbol or self.current_symbol
        return self._data.get_stock_info(symbol)

    def _resolve_calendar_date(self, date: str = None) -> str:
        """Resolve an optional calendar date without inventing an init date."""
        if date is not None:
            return date
        if self.current_date is None:
            raise RuntimeError(
                "calendar date is unavailable during on_init because no "
                "simulation date is active; pass an explicit date or move "
                "the query to on_before_market_open"
            )
        return self.current_date

    def is_trade_day(self, date: str = None) -> bool:
        """判断是否交易日"""
        date = self._resolve_calendar_date(date)
        return self._data.is_trade_day(date)

    def get_prev_trade_day(self, date: str = None, n: int = 1) -> Optional[str]:
        """获取前N个交易日"""
        date = self._resolve_calendar_date(date)
        return self._data.get_prev_trade_day(date, n)

    def get_next_trade_day(self, date: str = None, n: int = 1) -> Optional[str]:
        """获取后N个交易日"""
        date = self._resolve_calendar_date(date)
        return self._data.get_next_trade_day(date, n)

    def get_stock_pool(self) -> List[str]:
        """
        获取股票池

        在单股票回测模式下，返回当前股票
        (PortfolioContext 会重写此方法返回完整股票池)

        Returns:
            股票代码列表
        """
        if self.current_symbol:
            return [self.current_symbol]
        return []

    # ==================== 内部接口 ====================

    def _on_day_start(self) -> None:
        """日开始处理"""
        self._current_bar = None
        self._broker.on_day_start(
            self.current_date, sim_time=self.current_time)
        self._adjust_positions_for_corporate_actions()

    def _set_current_bar(self, bar) -> None:
        """Set the current execution-lane bar for reservation estimates."""
        self._current_bar = bar

    def _on_day_end(self) -> None:
        """日结束处理"""
        self._broker.on_day_end(sim_time=self.current_time)

    def _execute_open_orders(self, bars: Dict[str, BarData],
                             match_at=None) -> List[Order]:
        """执行开盘单"""
        return self._broker.execute_open_orders(bars, match_at=match_at)

    def _execute_market_orders(self, bar: BarData,
                               match_at=None) -> List[Order]:
        """执行市价单"""
        return self._broker.execute_market_orders(bar, match_at=match_at)

    def _execute_close_orders(self, bars: Dict[str, BarData],
                              match_at=None) -> List[Order]:
        """执行收盘单"""
        return self._broker.execute_close_orders(bars, match_at=match_at)

    def _update_prices(self, prices: Dict[str, float], sim_time=None,
                       phase: str = 'mark') -> None:
        """更新持仓价格"""
        self._broker.update_prices(
            prices,
            sim_time=self.current_time if sim_time is None else sim_time,
            phase=phase,
        )
