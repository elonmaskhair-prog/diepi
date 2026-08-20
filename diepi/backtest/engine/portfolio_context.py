"""
组合回测上下文

支持多股票交易的上下文
"""

from typing import Callable, Optional, Dict, Iterable, List, Union
from datetime import datetime
import pandas as pd
import logging

from ..broker import Broker, Order, Position, Account, TargetIntent
from ..broker.broker import BarData
from ..broker.settlement import SettlementUnitOfWork
from ..broker.validation import finite_float, nonnegative_float
from ..data import DataProvider
from .price_mode import PriceModeMixin
from ..data.stock_pool import StockPool, PoolSource

logger = logging.getLogger(__name__)


class PortfolioContext(PriceModeMixin):
    """
    组合回测上下文

    与 Context 的区别:
    1. 不绑定单一股票 (无 current_symbol 默认值)
    2. 提供股票池管理
    3. 支持批量数据查询

    Attributes:
        current_date: 当前交易日期 (YYYYMMDD)
        current_time: 当前时间 (datetime)
        active_symbols: 当日活跃股票列表 (on_before_market_open 确定)
    """

    def __init__(
        self,
        broker: Broker = None,
        data_provider: DataProvider = None,
        initial_cash: float = 1000000.0,
        pool_source: PoolSource = PoolSource.ALL_MARKET,
        pool_symbols: List[str] = None,
        pool_industry: str = None,
        pool_start_date: str = None,
        pool_end_date: str = None,
    ):
        """
        Args:
            broker: 撮合引擎
            data_provider: 数据提供者
            initial_cash: 初始资金
            pool_source: 股票池来源
            pool_symbols: 指定股票列表
            pool_industry: 行业名称
        """
        self._broker = broker or Broker(Account(initial_cash=initial_cash))
        self._data = data_provider or DataProvider()
        self._strategy_price_mode = getattr(self._data, 'price_mode', 'hfq')
        self._execution_price_mode = getattr(self._data, 'execution_price_mode', self._strategy_price_mode)
        self._stock_pool = StockPool(self._data)

        # 股票池配置
        self._pool_source = pool_source
        self._pool_symbols = pool_symbols
        self._pool_industry = pool_industry
        self._pool_start_date = pool_start_date
        self._pool_end_date = pool_end_date

        # 缓存的完整股票池
        self._full_pool: List[str] = []
        self._full_pool_loaded = False
        self._daily_pool_cache: Dict[str, List[str]] = {}

        # 当日活跃股票 (on_before_market_open 设置)
        self.active_symbols: List[str] = []
        self._market_data_observer: Optional[Callable[..., None]] = None
        self._result_warning_callback: Optional[
            Callable[[str, str], None]
        ] = None

        # 时间状态
        self.current_date: Optional[str] = None
        self.current_time: Optional[datetime] = None
        self._current_bars: Dict[str, BarData] = {}

        # K线索引 (当日第几根K线，从0开始)
        self._bar_index: int = 0
        self._total_bars: int = 0  # 当日总K线数

    # ==================== 股票池管理 ====================

    def _uses_historical_equity_pool(self) -> bool:
        return bool(
            self._pool_source in (PoolSource.ALL_MARKET, PoolSource.INDUSTRY)
            and self._pool_start_date is not None
            and self._pool_end_date is not None
        )

    def _load_stock_pool(
        self,
        *,
        as_of_date: str = None,
        preload_window: bool = False,
    ) -> List[str]:
        kwargs = {
            "source": self._pool_source,
            "symbols": self._pool_symbols,
            "industry": self._pool_industry,
        }
        if self._uses_historical_equity_pool():
            # Current names cannot reconstruct historical ST status.  The
            # engine records this explicit semantic in ResultContract.
            kwargs["exclude_st"] = False
            if preload_window:
                kwargs["start_date"] = self._pool_start_date
                kwargs["end_date"] = self._pool_end_date
            else:
                kwargs["as_of_date"] = as_of_date
        return self._stock_pool.get_pool(**kwargs)

    def get_preload_stock_pool(self) -> List[str]:
        """Return the union of securities eligible anywhere in the window."""

        if not self._full_pool_loaded:
            self._full_pool = self._load_stock_pool(preload_window=True)
            self._full_pool_loaded = True
        return self._full_pool.copy()

    def get_stock_pool(self) -> List[str]:
        """
        获取完整股票池

        Returns:
            股票代码列表
        """
        if self._uses_historical_equity_pool() and self.current_date is not None:
            key = str(self.current_date)
            if key not in self._daily_pool_cache:
                self._daily_pool_cache[key] = self._load_stock_pool(
                    as_of_date=key
                )
            return self._daily_pool_cache[key].copy()
        return self.get_preload_stock_pool()

    def set_active_symbols(self, symbols: List[str] = None) -> None:
        """
        设置当日活跃股票

        Args:
            symbols: 股票列表，None 时使用全部股票池
        """
        if symbols is None:
            self.active_symbols = self.get_stock_pool()
        else:
            self.active_symbols = symbols
        self._observe_market_data(self.active_symbols)

    def get_active_symbols(self) -> List[str]:
        """获取当日活跃股票"""
        return self.active_symbols.copy()

    def _set_market_data_observer(
        self, callback: Optional[Callable[..., None]]
    ) -> None:
        if callback is not None and not callable(callback):
            raise TypeError("market_data_observer must be callable or None")
        self._market_data_observer = callback

    def _observe_market_data(
        self,
        symbols: Iterable[str],
        *,
        frequency: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        require_direct_prices: bool = True,
    ) -> None:
        callback = self._market_data_observer
        if callback is not None:
            kwargs = {
                "frequency": frequency,
                "start_date": start_date,
                "end_date": end_date,
            }
            if not require_direct_prices:
                kwargs["require_direct_prices"] = False
            callback(tuple(symbols), **kwargs)

    def _set_result_warning_callback(
        self, callback: Optional[Callable[[str, str], None]]
    ) -> None:
        if callback is not None and not callable(callback):
            raise TypeError("result_warning_callback must be callable or None")
        self._result_warning_callback = callback

    def add_result_warning(self, code: str, message: str) -> None:
        """Attach a strategy diagnostic to the machine-readable run result."""

        callback = self._result_warning_callback
        if callback is None:
            raise RuntimeError("result warning collection is unavailable")
        callback(code, message)

    # ==================== 交易接口 ====================

    def buy_at_open(self, symbol: str, amount: float = None,
                    shares: int = None, percent: float = None,
                    note: str = '') -> Order:
        """
        开盘价买入

        Args:
            symbol: 股票代码 (必需)
            amount: 买入金额
            shares: 买入股数
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）
            note: 备注

        Returns:
            Order 对象
        """
        if not symbol:
            raise ValueError("symbol is required")
        return self._broker.buy_at_open(symbol, amount, shares, percent, note)

    def buy_at_market(self, symbol: str, amount: float = None,
                      shares: int = None, percent: float = None,
                      note: str = '', current_price: float = None) -> Order:
        """
        市价买入

        Args:
            symbol: 股票代码 (必需)
            amount: 买入金额
            shares: 买入股数
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）
            note: 备注
            current_price: 当前价格（用于资金估算）

        Returns:
            Order 对象
        """
        if not symbol:
            raise ValueError("symbol is required")
        derived_price = False
        if current_price is None:
            bar = self._current_bars.get(symbol)
            if bar:
                current_price = bar.high * (1 + self._broker.slippage)
                derived_price = True
        if current_price is not None and not derived_price:
            current_price = self._convert_price_for_execution(symbol, current_price)
        return self._broker.buy_at_market(symbol, amount, shares, percent, note, current_price)

    def buy_at_price(self, symbol: str, price: float,
                     amount: float = None, shares: int = None,
                     percent: float = None, note: str = '') -> Order:
        """
        限价买入

        Args:
            symbol: 股票代码 (必需)
            price: 限价 (必需)
            amount: 买入金额
            shares: 买入股数
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）

        Returns:
            Order 对象
        """
        if not symbol:
            raise ValueError("symbol is required")
        if price is None:
            raise ValueError("price is required for limit order")
        price = self._convert_price_for_execution(symbol, price)
        return self._broker.buy_at_price(symbol, price, amount, shares, percent, note)

    def buy_at_limit(self, symbol: str, price: float,
                     amount: float = None, shares: int = None,
                     percent: float = None, note: str = '') -> Order:
        """别名：限价买入"""
        return self.buy_at_price(symbol, price, amount, shares, percent, note)

    def sell_at_open(self, symbol: str, shares: int = None,
                     percent: float = None, note: str = '') -> Order:
        """
        开盘价卖出

        Args:
            symbol: 股票代码 (必需)
            shares: 卖出股数
            percent: 占可卖持仓比例 (1.0 = 全部)

        Returns:
            Order 对象
        """
        if not symbol:
            raise ValueError("symbol is required")
        return self._broker.sell_at_open(symbol, shares, percent, note)

    def sell_at_market(self, symbol: str, shares: int = None,
                       percent: float = None, note: str = '') -> Order:
        """
        市价卖出

        Args:
            symbol: 股票代码 (必需)
            shares: 卖出股数
            percent: 占可卖持仓比例

        Returns:
            Order 对象
        """
        if not symbol:
            raise ValueError("symbol is required")
        return self._broker.sell_at_market(symbol, shares, percent, note)

    def sell_at_price(self, symbol: str, price: float,
                      shares: int = None, percent: float = None,
                      note: str = '') -> Order:
        """
        限价卖出

        Args:
            symbol: 股票代码 (必需)
            price: 限价 (必需)
            shares: 卖出股数
            percent: 占可卖持仓比例

        Returns:
            Order 对象
        """
        if not symbol:
            raise ValueError("symbol is required")
        if price is None:
            raise ValueError("price is required for limit order")
        price = self._convert_price_for_execution(symbol, price)
        return self._broker.sell_at_price(symbol, price, shares, percent, note)

    # ==================== 止损/止盈/触发单 ====================

    def sell_stop_loss(self, symbol: str, price: float = None,
                       shares: int = None, percent: float = None,
                       note: str = '', stop_price: float = None) -> Order:
        """止损卖出 (价格下穿触发)"""
        if not symbol:
            raise ValueError("symbol is required")
        price = price if price is not None else stop_price
        if price is None:
            raise ValueError("stop price is required")
        price = self._convert_price_for_execution(symbol, price)
        return self._broker.sell_stop_loss(symbol, price, shares, percent, note)

    def sell_stop_profit(self, symbol: str, price: float = None,
                         shares: int = None, percent: float = None,
                         note: str = '', profit_price: float = None) -> Order:
        """止盈卖出 (价格上穿触发)"""
        if not symbol:
            raise ValueError("symbol is required")
        price = price if price is not None else profit_price
        if price is None:
            raise ValueError("profit price is required")
        price = self._convert_price_for_execution(symbol, price)
        return self._broker.sell_stop_profit(symbol, price, shares, percent, note)

    def buy_stop(self, symbol: str, price: float = None,
                 shares: int = None, amount: float = None,
                 percent: float = None, note: str = '',
                 current_price: float = None, trigger_price: float = None) -> Order:
        """触发买入 (价格上穿触发，用于突破/追涨)"""
        if not symbol:
            raise ValueError("symbol is required")
        price = price if price is not None else trigger_price
        if price is None:
            raise ValueError("trigger price is required")
        price = self._convert_price_for_execution(symbol, price)
        if current_price is not None:
            current_price = self._convert_price_for_execution(symbol, current_price)
        return self._broker.buy_stop(symbol, price, shares, amount, percent, note, current_price)

    # ==================== 收盘价交易接口 ====================

    def buy_at_close(self, symbol: str, amount: float = None,
                     shares: int = None, percent: float = None,
                     note: str = '', current_price: float = None) -> Order:
        """
        收盘价买入

        注意: 只能在交易时段 (on_day) 调用

        Args:
            symbol: 股票代码 (必需)
            amount: 买入金额
            shares: 买入股数
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）
            note: 备注

        Returns:
            Order 对象
        """
        if not symbol:
            raise ValueError("symbol is required")
        derived_price = False
        if current_price is None:
            bar = self._current_bars.get(symbol)
            if bar:
                current_price = bar.close * (1 + self._broker.slippage)
                derived_price = True
        if current_price is not None and not derived_price:
            current_price = self._convert_price_for_execution(symbol, current_price)
        return self._broker.buy_at_close(symbol, amount, shares, percent, note, current_price)

    def sell_at_close(self, symbol: str, shares: int = None,
                      percent: float = None, note: str = '') -> Order:
        """
        收盘价卖出

        注意: 只能在交易时段 (on_day) 调用

        Args:
            symbol: 股票代码 (必需)
            shares: 卖出股数
            percent: 占可卖持仓比例
            note: 备注

        Returns:
            Order 对象
        """
        if not symbol:
            raise ValueError("symbol is required")
        return self._broker.sell_at_close(symbol, shares, percent, note)

    def schedule_at_close(
        self,
        side: str,
        symbol: str,
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

    # ==================== 查询接口 ====================

    def get_cash(self) -> float:
        """获取可用现金"""
        return self._broker.get_cash()

    def get_total_asset(self) -> float:
        """获取总资产"""
        return self._broker.get_total_asset()

    def get_position(self, symbol: str) -> Optional[Position]:
        """
        获取指定股票持仓

        Args:
            symbol: 股票代码

        Returns:
            Position 对象，无持仓返回 None
        """
        if not symbol:
            return None
        return self._broker.get_position(symbol)

    def get_positions(self) -> Dict[str, Position]:
        """获取所有持仓"""
        return self._broker.get_positions()

    def get_position_value(self, symbol: str) -> float:
        """
        获取指定股票持仓市值

        Args:
            symbol: 股票代码

        Returns:
            市值，无持仓返回 0
        """
        pos = self.get_position(symbol)
        return pos.market_value if pos else 0.0

    def get_position_weight(self, symbol: str) -> float:
        """
        获取指定股票持仓占比

        Args:
            symbol: 股票代码

        Returns:
            持仓占总资产比例
        """
        total = self.get_total_asset()
        if total <= 0:
            return 0.0
        return self.get_position_value(symbol) / total

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

    def cancel_order(self, order_id: str) -> bool:
        """取消指定订单（历史P1：GUI 速查一直宣传本方法，但组合上下文此前
        并未提供，用户复制示例代码直接 AttributeError）"""
        return self._broker.cancel_order(order_id)

    def cancel_orders(self, symbol: str = None, side: str = None) -> List[Order]:
        """批量取消订单

        Args:
            symbol: 股票代码 (None=所有股票)
            side: 'buy'/'sell' (None=所有方向)
        """
        return self._broker.cancel_orders(symbol, side)

    def cancel_all_orders(self) -> List[Order]:
        """取消所有未完成订单"""
        return self._broker.cancel_all_orders()

    # ==================== 目标权重下单 ====================

    def _effective_position_value(self, pos) -> float:
        """持仓估值；日线路径当日新开仓 current_price 尚未刷新(=0)时回退成本价。

        审查确认P1：不回退时 when='close' 的当日目标下调会因 market_value=0
        方向反转成买入、总资产基数塌缩为剩余现金。"""
        if pos is None or pos.shares <= 0:
            return 0.0
        if pos.current_price > 0:
            return pos.market_value
        return pos.shares * pos.avg_cost

    def _effective_total_asset(self) -> float:
        """总资产；对 current_price 未刷新的持仓补上成本价估值"""
        total = self._broker.get_total_asset()
        for pos in self._broker.get_positions().values():
            if pos.shares > 0 and pos.current_price <= 0:
                total += pos.shares * pos.avg_cost
        return total

    def _live_auction_orders(self, symbol: str):
        """本标的仍会执行的竞价挂单（OPEN/CLOSE）。

        只看竞价单：条件单（STOP/止盈/限价）可能永不触发，把保护性止损
        当确定卖出会使目标权重方向反转（核查组确认P1：止盈委托价还会把
        承诺放大 53%、终态 2.2 倍目标）。OPEN 单在开盘撮合已过后（盘中及
        以后时段）的部分成交残量永不再撮合（legacy 模式幽灵承诺），同样排除。"""
        from ..broker.order import OrderType
        phase = self._broker._market_phase
        now = self.current_time
        current_date = self.current_date
        out = []
        for o in self._broker.get_open_orders():
            if o.symbol != symbol or o.is_done:
                continue
            if o.order_type == OrderType.OPEN and phase != 'before_open':
                # An OPEN order created from ``on_day`` is explicitly dated
                # for the next session.  It remains a live commitment even
                # though today's opening auction has passed.  Same-session
                # OPEN residuals, by contrast, are dead after that auction.
                future_expiry = bool(
                    o.expire_date and current_date
                    and o.expire_date > current_date
                )
                future_eligibility = bool(
                    o.eligible_from is not None and now is not None
                    and o.eligible_from > now
                )
                if not (future_expiry or future_eligibility):
                    continue
            if o.order_type in (OrderType.OPEN, OrderType.CLOSE):
                out.append(o)
        return out

    def _pending_order_value(self, symbol: str) -> float:
        """同窗未成交竞价挂单的净承诺市值（买为正、卖为负）。

        审查确认P1：竞价 auto 买单提交时不冻结现金也不改持仓，重复调用
        order_target_percent 会算出相同 delta 再挂一单（双倍建仓/超卖清仓）。
        把未成交竞价挂单计入现状即可幂等。卖侧按有效市价估值
        （不能用委托价——见 _live_auction_orders）。"""
        pos = self._broker.get_position(symbol)
        ref_price = 0.0
        if pos is not None and pos.shares > 0:
            ref_price = pos.current_price if pos.current_price > 0 else pos.avg_cost
        committed = 0.0
        for o in self._live_auction_orders(symbol):
            if o.is_buy:
                if o.filled_shares == 0 and o.requested_amount:
                    committed += o.requested_amount
                elif o.shares:
                    unfilled = max(0, o.shares - o.filled_shares)
                    committed += unfilled * (o.price or ref_price)
            else:
                unfilled = max(0, (o.shares or 0) - o.filled_shares)
                committed -= unfilled * ref_price
        return committed

    def _cancel_auction_orders(self, symbol: str, side: str) -> int:
        """撤销本标的指定方向的在途竞价挂单（cancel-replace 语义用）。

        绝不触碰条件单（用户的保护性止损/止盈必须保留）。"""
        n = 0
        for o in self._live_auction_orders(symbol):
            if (side == 'buy') == bool(o.is_buy):
                if self._broker.cancel_order(o.order_id):
                    n += 1
        return n

    def _submit_open_target_delta(
            self, symbol: str, delta: float, pos, pos_value: float, *,
            clearing: bool, min_trade_amount: float,
            note: str) -> Optional[Order]:
        """Submit one already-resolved OPEN target delta."""

        if not clearing and abs(delta) < max(min_trade_amount, 1e-6):
            return None
        if delta > 0:
            return self.buy_at_open(symbol, amount=delta, note=note)
        if pos is None or pos.shares <= 0 or pos_value <= 0:
            return None
        sell_shares = int(round(pos.shares * (-delta) / pos_value))
        sell_shares = min(sell_shares, pos.available_shares)
        if sell_shares <= 0:
            return None
        return self.sell_at_open(symbol, shares=sell_shares, note=note)

    def order_target_percent(self, symbol: str, target_weight: float,
                             when: str = 'open',
                             min_trade_amount: float = 0.0,
                             note: str = '') -> Optional[Union[Order, TargetIntent]]:
        """把标的仓位调整到总资产的目标比例（竞价执行；尽力而为、当日有效）。

        与 percent 参数的区别：percent=占可用现金的分配比例；
        target_weight=占总资产的仓位目标（组合管理语义）。

        语义与边界（详见文档《目标权重下单》）：
        - 基数 = 挂单时点总资产（现金+冻结+持仓按最新价估值）
        - delta>0 → 竞价买 amount=delta（现金不够→竞价 auto 缩量成交并打审计标记；
          默认 limit_up sizing 使达成度 ≈ delta/(1+涨跌幅)，保守方向）
        - delta<0 → 竞价卖（卖到可卖上限为止：T+1 当日买入部分今天卖不掉；
          盘前订单在当日窗口执行，on_after_open/on_day 创建的 OPEN 订单
          明确顺延到下一交易日；单次竞价窗口后的未成交残量不再顺延）
        - |delta| < min_trade_amount → 不动作（死区，避免为微小漂移空转手续费）
        - when='open' 须盘前调用；when='close' 须盘中调用（时段门由 broker 校验）

        Returns:
            挂出的订单；死区/无需动作时返回 None
        """
        if when not in ('open', 'close'):
            raise ValueError(f"when 应为 'open'/'close'，收到 {when!r}")
        target_weight = finite_float(target_weight, 'target_weight')
        if target_weight < 0:
            raise ValueError(f"target_weight 不能为负（收到 {target_weight}；做空不支持）")
        if target_weight > 1:
            raise ValueError(
                f"target_weight 不能 > 1（收到 {target_weight}；杠杆不支持）")
        min_trade_amount = nonnegative_float(
            min_trade_amount, 'min_trade_amount')

        if when == 'close':
            return self._broker.submit_target_intent(
                symbol,
                target_weight,
                min_trade_amount=min_trade_amount,
                note=note,
            )

        total = self._effective_total_asset()
        if total <= 0:
            return None
        pos = self._broker.get_position(symbol)
        pos_value = self._effective_position_value(pos)
        # 现状 = 已成交持仓 + 同窗未成交竞价挂单承诺（幂等：重复调用不再补单）
        pending = self._pending_order_value(symbol)
        delta = target_weight * total - pos_value - pending

        # 显式 target=0 的清仓不受死区约束（尘埃仓位也必须清干净）
        clearing = target_weight == 0.0 and pos is not None and pos.shares > 0
        if not clearing and abs(delta) < max(min_trade_amount, 1e-6):
            return None

        # 声明式 cancel-replace：目标方向与在途竞价挂单方向相反时先撤对向
        # 挂单再重算（核查确认P2：同窗先0.5再0.3曾走卖出分支因无持仓静默
        # 返回 None，500k 买挂单原样成交终态0.45；有持仓时则清掉全部既有
        # 持仓凑数=多付一轮全额换手）。条件单不在撤销范围。
        # 前置可达性预检避免确定会被时段门拒绝的替换；真正的 cancel+create
        # 仍须由下方统一事务覆盖，因为事件序列化等后置步骤也可能失败。
        # Mirror Broker.buy_at_open/sell_at_open's complete legality rule.
        # In particular, on_day/on_after_open OPEN orders are live T+1
        # commitments; excluding those phases makes a lower target or clear
        # request silently leave the earlier, larger order in force.
        replace_ok = (
            self._broker.is_before_market_open
            or self._broker.current_created_phase in {
                'on_after_open', 'on_day'
            }
        )
        needs_cancel_replace = replace_ok and (
            (delta < 0 and pending > 0) or (delta > 0 and pending < 0)
        )
        if needs_cancel_replace:
            # Cancel and replacement are one economic mutation.  A replacement
            # can still fail while publishing its immutable event (for example
            # an unsupported note payload); without this outer transaction the
            # original live order has already disappeared permanently.
            with SettlementUnitOfWork(self._broker) as replacement:
                self._cancel_auction_orders(
                    symbol, 'buy' if pending > 0 else 'sell')
                pending = self._pending_order_value(symbol)
                delta = target_weight * total - pos_value - pending
                result = self._submit_open_target_delta(
                    symbol, delta, pos, pos_value,
                    clearing=clearing,
                    min_trade_amount=min_trade_amount,
                    note=note,
                )
                replacement.commit()
                return result

        return self._submit_open_target_delta(
            symbol, delta, pos, pos_value,
            clearing=clearing,
            min_trade_amount=min_trade_amount,
            note=note,
        )

    def rebalance(self, weights: Dict[str, float], when: str = 'open',
                  min_trade_amount: float = 0.0,
                  note: str = '') -> List[Union[Order, TargetIntent]]:
        """按目标权重表批量调仓（先减后加；竞价窗口本就卖先买后，回款闭环）。

        三态语义（与清单驱动格式一致）：
        - weight > 0 = 持有到该比例
        - weight == 0 = 显式清仓
        - 未提及的持仓 = 保留不动（不得从缺席推断卖出）

        权重和 > 1 直接拒绝（配置错误不静默归一化）。

        Returns:
            全部挂出的订单列表（死区跳过的不含在内）
        """
        if when not in ('open', 'close'):
            raise ValueError(f"when must be 'open'/'close', got {when!r}")
        validated_weights = {}
        for s, raw_weight in weights.items():
            w = finite_float(raw_weight, f'target_weight[{s}]')
            if w < 0:
                raise ValueError(f"{s} 目标权重为负（{w}；做空不支持）")
            if w > 1:
                raise ValueError(f"{s} 目标权重 {w} > 1（杠杆不支持）")
            validated_weights[s] = w
        min_trade_amount = nonnegative_float(
            min_trade_amount, 'min_trade_amount')
        weights = validated_weights
        total_w = sum(weights.values())
        if when == 'close':
            return list(self._broker.submit_target_intent_batch(
                weights,
                min_trade_amount=min_trade_amount,
                note=note,
            ))
        if total_w > 1.0 + 1e-9:
            raise ValueError(
                f"目标权重之和 {total_w:.4f} > 1（配置错误，不做静默归一化）")

        orders: List[Order] = []
        total = self._effective_total_asset()
        # 先减仓后加仓（确定性顺序 sorted；竞价窗口执行时仍是全部卖单先于买单）
        decreases, increases = [], []
        for symbol in sorted(weights):
            pos = self._broker.get_position(symbol)
            pos_value = self._effective_position_value(pos)
            delta = weights[symbol] * total - pos_value
            (increases if delta > 0 else decreases).append(symbol)
        for symbol in decreases + increases:
            order = self.order_target_percent(
                symbol, weights[symbol], when=when,
                min_trade_amount=min_trade_amount, note=note)
            if order is not None:
                orders.append(order)
        return orders

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
            # The completed 09:30 OHLCV minute is not known at the
            # immediately-after-open decision point.
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

    def get_daily(self, symbol: str, days: int = None,
                  start_date: str = None, end_date: str = None,
                  fields: List[str] = None) -> pd.DataFrame:
        """
        获取日线数据

        Args:
            symbol: 股票代码 (必需)
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
        if not symbol:
            raise ValueError("symbol is required")

        start, end = self._resolve_daily_range(days, start_date, end_date)
        self._observe_market_data(
            (symbol,), frequency="daily", start_date=start, end_date=end
        )
        return self._data.get_daily(symbol, start=start, end=end, fields=fields, price_mode='strategy')

    def get_daily_batch(self, symbols: List[str], days: int = None,
                        start_date: str = None, end_date: str = None,
                        fields: List[str] = None) -> Dict[str, pd.DataFrame]:
        """
        批量获取日线数据

        Args:
            symbols: 股票代码列表
            days: 交易日步长
            start_date: 开始日期
            end_date: 结束日期
            fields: 返回字段

        参数组合: 同 get_daily

        Returns:
            {symbol: DataFrame} 字典
        """
        start, end = self._resolve_daily_range(days, start_date, end_date)

        result = {}
        for symbol in symbols:
            self._observe_market_data(
                (symbol,), frequency="daily", start_date=start, end_date=end
            )
            try:
                result[symbol] = self._data.get_daily(symbol, start=start, end=end, fields=fields, price_mode='strategy')
            except Exception as e:
                logger.debug(f"Failed to get daily for {symbol}: {e}")
                result[symbol] = pd.DataFrame()
        return result

    def get_minute(self, symbol: str, days: int = None,
                   start_date: str = None, end_date: str = None,
                   start_time: str = None, end_time: str = None,
                   fields: List[str] = None) -> pd.DataFrame:
        """
        获取分钟数据

        Args:
            symbol: 股票代码 (必需)
            days: 交易日步长
            start_date: 开始日期
            end_date: 结束日期
            start_time: 每日开始时间 (HH:MM)
            end_time: 每日结束时间 (HH:MM)
            fields: 返回字段

        参数组合: 同 get_daily

        Returns:
            DataFrame: index=trade_time
        """
        if not symbol:
            raise ValueError("symbol is required")

        start, end, boundary_time = self._resolve_minute_range(days, start_date, end_date)
        self._observe_market_data(
            (symbol,), frequency="minute", start_date=start, end_date=end
        )

        return self._data.get_minute_by_days(
            symbol, start, end, boundary_time,
            start_time, end_time, fields, price_mode='strategy'
        )

    def get_cyq(self, symbol: str, days: int = None,
                start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        获取筹码分布

        Args:
            symbol: 股票代码 (必需)
            days: 交易日步长
            start_date: 开始日期
            end_date: 结束日期

        参数组合: 同 get_daily

        Returns:
            DataFrame
        """
        if not symbol:
            raise ValueError("symbol is required")

        start, end = self._resolve_daily_range(days, start_date, end_date)
        self._observe_market_data(
            (symbol,),
            frequency="daily",
            start_date=start,
            end_date=end,
            require_direct_prices=False,
        )
        return self._data.get_cyq(symbol, start=start, end=end)

    def get_moneyflow(self, symbol: str, days: int = None,
                      start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        获取资金流向

        Args:
            symbol: 股票代码 (必需)
            days: 交易日步长
            start_date: 开始日期
            end_date: 结束日期

        参数组合: 同 get_daily

        Returns:
            DataFrame
        """
        if not symbol:
            raise ValueError("symbol is required")

        start, end = self._resolve_daily_range(days, start_date, end_date)
        self._observe_market_data(
            (symbol,),
            frequency="daily",
            start_date=start,
            end_date=end,
            require_direct_prices=False,
        )
        return self._data.get_moneyflow(symbol, start=start, end=end)

    def get_margin(self, symbol: str, days: int = None,
                   start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """
        获取融资融券

        Args:
            symbol: 股票代码 (必需)
            days: 交易日步长
            start_date: 开始日期
            end_date: 结束日期

        参数组合: 同 get_daily

        Returns:
            DataFrame
        """
        if not symbol:
            raise ValueError("symbol is required")

        start, end = self._resolve_daily_range(days, start_date, end_date)
        self._observe_market_data(
            (symbol,),
            frequency="daily",
            start_date=start,
            end_date=end,
            require_direct_prices=False,
        )
        return self._data.get_margin(symbol, start=start, end=end)

    def get_basic(self, symbol: str, days: int = None,
                  start_date: str = None, end_date: str = None,
                  fields: List[str] = None) -> pd.DataFrame:
        """
        获取基本面数据

        Args:
            symbol: 股票代码 (必需)
            days: 交易日步长
            start_date: 开始日期
            end_date: 结束日期
            fields: 返回字段

        参数组合: 同 get_daily

        Returns:
            DataFrame
        """
        if not symbol:
            raise ValueError("symbol is required")

        start, end = self._resolve_daily_range(days, start_date, end_date)
        self._observe_market_data(
            (symbol,),
            frequency="daily",
            start_date=start,
            end_date=end,
            require_direct_prices=False,
        )
        return self._data.get_basic(symbol, start=start, end=end, fields=fields)

    def get_stock_info(self, symbol: str = None) -> pd.DataFrame:
        """
        获取股票信息

        Args:
            symbol: 股票代码，None 时返回全部

        Returns:
            DataFrame 或 Series
        """
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

    # ==================== 时间管理 ====================

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

    def set_market_phase(self, phase: str) -> None:
        """
        设置交易时段

        Args:
            phase: 'before_open', 'trading', 'after_close'
        """
        self._broker.set_market_phase(phase)

    # ==================== K线索引 ====================

    @property
    def bar_index(self) -> int:
        """
        当前是今日第几根 K 线 (从0开始)

        Returns:
            K线索引，0 表示第一根
        """
        return self._bar_index

    @property
    def is_first_bar(self) -> bool:
        """
        当前是否是今日首根 K 线

        Returns:
            True 表示是首根K线
        """
        return self._bar_index == 0

    @property
    def is_last_bar(self) -> bool:
        """
        当前是否是今日最后一根 K 线

        Returns:
            True 表示是最后一根K线
        """
        return self._total_bars > 0 and self._bar_index == self._total_bars - 1

    @property
    def total_bars(self) -> int:
        """
        今日总 K 线数

        Returns:
            K线总数
        """
        return self._total_bars

    def _set_bar_index(self, index: int, total: int = 0) -> None:
        """
        设置K线索引 (引擎内部调用)

        Args:
            index: 当前K线索引
            total: 今日总K线数
        """
        self._bar_index = index
        if total > 0:
            self._total_bars = total

    # ==================== 内部接口 ====================

    def _on_day_start(self) -> None:
        """日开始处理"""
        self._current_bars = {}
        self._bar_index = 0
        self._total_bars = 0
        self._broker.on_day_start(
            self.current_date, sim_time=self.current_time)
        self._adjust_positions_for_corporate_actions()

    def _on_day_end(self) -> None:
        """日结束处理"""
        self._current_bars = {}
        self._broker.on_day_end(sim_time=self.current_time)

    def _execute_open_orders(self, bars: Dict[str, BarData],
                             match_at=None,
                             reject_missing: bool = True) -> List[Order]:
        """执行开盘单"""
        return self._broker.execute_open_orders(
            bars, match_at=match_at, reject_missing=reject_missing)

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

    def _set_current_bars(self, bars: Dict[str, BarData]) -> None:
        """缓存当前价格路径，便于估算下单所需资金"""
        self._current_bars = bars or {}
