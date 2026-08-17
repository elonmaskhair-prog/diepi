"""
账户类

管理资金和持仓
"""

from dataclasses import dataclass, field
from typing import Dict, Optional, List
from datetime import datetime

from .position import Position
from .order import Order, OrderStatus, OrderType
from .validation import (
    finite_float,
    nonnegative_float,
    nonnegative_int,
    positive_float,
    positive_int,
)


@dataclass
class Account:
    """
    账户类

    Attributes:
        initial_cash: 初始资金
        cash: 可用现金
        frozen_cash: 冻结资金 (待成交买单占用)
        commission_rate: 手续费率 (默认万2.5)
        stamp_duty_rate: 印花税率 (卖出时收取，千1)
        min_commission: 最低手续费
    """
    initial_cash: float = 1000000.0
    cash: float = field(init=False)
    frozen_cash: float = 0.0
    commission_rate: float = 0.00025  # 万2.5
    stamp_duty_rate: float = 0.001    # 千1 (仅卖出)
    min_commission: float = 5.0       # 最低5元

    # 持仓字典 {symbol: Position}
    positions: Dict[str, Position] = field(default_factory=dict)

    # 订单记录
    orders: Dict[str, Order] = field(default_factory=dict)
    order_history: List[Order] = field(default_factory=list)

    def __post_init__(self):
        self.initial_cash = nonnegative_float(self.initial_cash, 'initial_cash')
        self.frozen_cash = nonnegative_float(self.frozen_cash, 'frozen_cash')
        self.commission_rate = nonnegative_float(
            self.commission_rate, 'commission_rate')
        self.min_commission = nonnegative_float(
            self.min_commission, 'min_commission')
        if self.stamp_duty_rate != 'auto':
            self.stamp_duty_rate = nonnegative_float(
                self.stamp_duty_rate, 'stamp_duty_rate')
        self.cash = self.initial_cash

    def _validate_cash_state(self) -> None:
        """Fail before mutation when externally assigned state is invalid."""
        finite_float(self.cash, 'cash')
        nonnegative_float(self.frozen_cash, 'frozen_cash')

    def _apply_cash_delta(self, amount: float) -> None:
        """Apply a finite signed cash delta after validating the result."""
        amount = finite_float(amount, 'cash delta')
        self._validate_cash_state()
        self.cash = finite_float(self.cash + amount, 'cash')

    @property
    def market_value(self) -> float:
        """持仓市值"""
        return sum(pos.market_value for pos in self.positions.values())

    @property
    def total_asset(self) -> float:
        """总资产 = 现金 + 冻结资金 + 持仓市值"""
        return self.cash + self.frozen_cash + self.market_value

    @property
    def available_cash(self) -> float:
        """可用资金（未冻结的现金）"""
        return self.cash

    def get_position(self, symbol: str) -> Optional[Position]:
        """获取持仓"""
        return self.positions.get(symbol)

    def get_positions(self) -> Dict[str, Position]:
        """获取所有持仓"""
        return self.positions.copy()

    def get_order(self, order_id: str) -> Optional[Order]:
        """获取订单"""
        return self.orders.get(order_id)

    def get_open_orders(self) -> List[Order]:
        """获取所有未完成订单"""
        return [o for o in self.orders.values() if o.is_open]

    def freeze_cash(self, amount: float) -> bool:
        """
        冻结资金（买单提交时）

        Args:
            amount: 冻结金额

        Returns:
            是否成功冻结
        """
        amount = nonnegative_float(amount, 'freeze amount')
        self._validate_cash_state()
        if amount > self.cash:
            return False

        new_cash = finite_float(self.cash - amount, 'cash')
        new_frozen_cash = nonnegative_float(
            self.frozen_cash + amount, 'frozen_cash')
        self.cash = new_cash
        self.frozen_cash = new_frozen_cash
        return True

    def unfreeze_cash(self, amount: float, *, strict: bool = False) -> None:
        """
        解冻资金（买单取消或成交后剩余）

        Args:
            amount: 解冻金额
        """
        amount = nonnegative_float(amount, 'unfreeze amount')
        if type(strict) is not bool:
            raise ValueError("strict must be a bool")
        self._validate_cash_state()
        if strict and amount > self.frozen_cash + 1e-9:
            raise ValueError("unfreeze amount exceeds frozen_cash")
        actual = min(amount, self.frozen_cash)
        new_frozen_cash = nonnegative_float(
            self.frozen_cash - actual, 'frozen_cash')
        new_cash = finite_float(self.cash + actual, 'cash')
        self.frozen_cash = new_frozen_cash
        self.cash = new_cash

    def freeze_shares(self, symbol: str, shares: int) -> int:
        """
        冻结股票（卖单提交时）

        Args:
            symbol: 股票代码
            shares: 请求冻结的股数

        Returns:
            实际冻结的股数
        """
        shares = nonnegative_int(shares, 'shares')
        pos = self.positions.get(symbol)
        if not pos:
            return 0
        return pos.freeze_shares(shares)

    def unfreeze_shares(self, symbol: str, shares: int, *,
                        strict: bool = False) -> None:
        """
        解冻股票（卖单取消或成交后）

        Args:
            symbol: 股票代码
            shares: 解冻股数
        """
        shares = nonnegative_int(shares, 'shares')
        if type(strict) is not bool:
            raise ValueError("strict must be a bool")
        pos = self.positions.get(symbol)
        if not pos:
            if strict and shares > 0:
                raise ValueError("cannot unfreeze shares without a position")
            return
        if strict and shares > pos.frozen_shares:
            raise ValueError("unfreeze shares exceed position frozen_shares")
        pos.unfreeze_shares(shares)

    def add_cash(self, amount: float) -> None:
        """增加现金（卖出成交）"""
        amount = nonnegative_float(amount, 'cash amount')
        self._apply_cash_delta(amount)

    def deduct_frozen(self, amount: float, *, strict: bool = False) -> None:
        """扣除冻结资金（买入成交）"""
        amount = nonnegative_float(amount, 'deduct amount')
        if type(strict) is not bool:
            raise ValueError("strict must be a bool")
        self._validate_cash_state()
        if strict and amount > self.frozen_cash + 1e-9:
            raise ValueError("deduct amount exceeds frozen_cash")
        self.frozen_cash = nonnegative_float(
            self.frozen_cash - min(amount, self.frozen_cash), 'frozen_cash')

    def resolve_stamp_rate(self, symbol: str = None, trade_date: str = None) -> float:
        """解析印花税率。

        stamp_duty_rate 为数值时直接使用（全局一刀切，默认千1的严格口径）；
        为 'auto' 时按现实规则：场内基金(ETF/LOF/REITs)免征；
        股票 2023-08-28 前千1、之后万5。缺 symbol/date 信息时按股票旧率保守处理。
        """
        rate = self.stamp_duty_rate
        if rate != 'auto':
            return nonnegative_float(rate, 'stamp_duty_rate')
        if symbol:
            from ..instruments import is_exchange_fund
            if is_exchange_fund(symbol):
                return 0.0  # 场内基金免印花税
        if trade_date:
            # Public callers commonly pass ISO dates while the engine uses
            # compact YYYYMMDD strings.  Comparing the raw representations is
            # incorrect (``2023-08-29`` sorts before ``20230828``), so reduce
            # supported date-like values to the same compact calendar key.
            date_key = str(trade_date).replace('-', '').replace('/', '')
            if '.' in date_key:
                date_key = date_key.split('.', 1)[0]
            date_key = date_key[:8]
            try:
                # ``strptime`` accepts non-zero-padded month/day fields.  Use
                # the parsed value to rebuild a fixed-width key before the
                # policy boundary comparison (for example, ``2023-8-27`` must
                # sort before ``20230828``).
                date_key = datetime.strptime(
                    date_key, '%Y%m%d').strftime('%Y%m%d')
            except (TypeError, ValueError):
                # Preserve the documented conservative fallback when the date
                # is absent or unusable.
                date_key = ''
            if date_key >= '20230828':
                return 0.0005
        return 0.001

    def calc_commission(self, amount: float, is_sell: bool = False,
                        symbol: str = None, trade_date: str = None) -> float:
        """
        计算手续费

        Args:
            amount: 成交金额
            is_sell: 是否卖出
            symbol/trade_date: 供 stamp_duty_rate='auto' 时按品种/日期定税率

        Returns:
            手续费总额（四舍五入到分）
        """
        amount = nonnegative_float(amount, 'trade amount')
        commission_rate = nonnegative_float(
            self.commission_rate, 'commission_rate')
        min_commission = nonnegative_float(
            self.min_commission, 'min_commission')

        # 佣金
        commission = nonnegative_float(
            amount * commission_rate, 'commission')
        commission = max(commission, min_commission)

        # 印花税（仅卖出）
        stamp_duty = 0.0
        if is_sell:
            stamp_duty = nonnegative_float(
                amount * self.resolve_stamp_rate(symbol, trade_date),
                'stamp duty')

        # 四舍五入到分，避免浮点精度问题
        return nonnegative_float(
            round(commission + stamp_duty, 2), 'commission')

    def buy(self, symbol: str, shares: int, price: float, trade_date: str,
            t0: bool = False, *, fee_total: float = None,
            strict_settlement: bool = True) -> float:
        """
        买入成交

        Args:
            symbol: 股票代码
            shares: 成交股数
            price: 成交价格
            trade_date: 交易日期
            t0: T+0 品种，买入当日即可卖出
            fee_total: 显式成交费用；None 保持旧接口并由账户计算
            strict_settlement: 要求冻结资金足额，禁止旧接口的静默截断

        Returns:
            实际花费金额（含手续费）
        """
        shares = positive_int(shares, 'shares')
        price = positive_float(price, 'price')
        if type(strict_settlement) is not bool:
            raise ValueError("strict_settlement must be a bool")
        if not isinstance(t0, bool):
            raise ValueError("t0 must be a bool")
        trade_date = Position._validate_date(trade_date, 'trade_date')
        self._validate_cash_state()
        amount = positive_float(shares * price, 'trade amount')
        fees = (
            self.calc_commission(amount, is_sell=False)
            if fee_total is None
            else nonnegative_float(fee_total, 'fee_total')
        )
        total_cost = positive_float(amount + fees, 'total cost')

        # 扣除冻结资金
        self.deduct_frozen(total_cost, strict=strict_settlement)

        # 更新持仓（传递手续费，使成本包含手续费）
        if symbol not in self.positions:
            self.positions[symbol] = Position(symbol=symbol)

        self.positions[symbol].buy(shares, price, trade_date, fees, t0=t0)

        return total_cost

    def sell(self, symbol: str, shares: int, price: float,
             trade_date: str = None, *, fee_total: float = None,
             strict_settlement: bool = True) -> float:
        """
        卖出成交

        Args:
            symbol: 股票代码
            shares: 成交股数
            price: 成交价格
            trade_date: 交易日期（供 stamp_duty_rate='auto' 按日期定税率）
            fee_total: 显式成交费用；None 保持旧接口并由账户计算
            strict_settlement: 要求持仓精确满足 requested shares

        Returns:
            实际获得金额（扣除手续费）
        """
        shares = positive_int(shares, 'shares')
        price = positive_float(price, 'price')
        if type(strict_settlement) is not bool:
            raise ValueError("strict_settlement must be a bool")
        self._validate_cash_state()

        if symbol not in self.positions:
            if strict_settlement:
                raise ValueError("strict sell requires an existing position")
            return 0.0

        pos = self.positions[symbol]
        available_shares = pos.available_shares
        if strict_settlement and shares > available_shares:
            raise ValueError("strict sell shares exceed available_shares")
        actual_shares = shares if strict_settlement else min(shares, available_shares)
        if actual_shares <= 0:
            if strict_settlement:
                raise ValueError("strict sell requires positive available shares")
            return 0.0

        sell_amount = positive_float(
            actual_shares * price, 'trade amount')
        # 扣除手续费
        fees = (
            self.calc_commission(
                sell_amount, is_sell=True, symbol=symbol,
                trade_date=trade_date)
            if fee_total is None
            else nonnegative_float(fee_total, 'fee_total')
        )
        net_amount = finite_float(sell_amount - fees, 'net proceeds')
        new_cash = finite_float(self.cash + net_amount, 'cash')

        # Validate all monetary results before mutating the position.
        sell_amount = pos.sell(actual_shares, price)
        if sell_amount <= 0:
            if strict_settlement:
                raise ValueError("strict sell did not sell the requested shares")
            return 0.0

        # 增加现金
        self.cash = new_cash

        # 清理空仓
        if pos.shares <= 0:
            del self.positions[symbol]

        return net_amount

    def settle_t1(self, current_date: str) -> Dict[str, int]:
        """
        T+1 结算

        Args:
            current_date: 当前交易日期
        """
        unlocked = {}
        for symbol, pos in self.positions.items():
            quantity = pos.settle_t1(current_date)
            if quantity > 0:
                unlocked[symbol] = quantity
        return unlocked

    def update_prices(self, prices: Dict[str, float], *,
                      update_time: Optional[datetime] = None) -> None:
        """
        更新持仓价格

        Args:
            prices: {symbol: price}
        """
        validated = {
            symbol: positive_float(price, f'price[{symbol}]')
            for symbol, price in prices.items()
        }
        for symbol, price in validated.items():
            if symbol in self.positions:
                self.positions[symbol].update_price(
                    price, update_time=update_time)

    def estimate_market_value(self, prices: Dict[str, float]) -> float:
        """Estimate market value with override prices without mutating positions."""
        market_value = 0.0
        overrides = prices or {}
        for symbol, pos in self.positions.items():
            if symbol in overrides:
                price = positive_float(overrides[symbol], f'price[{symbol}]')
            else:
                price = nonnegative_float(
                    pos.current_price, f'current_price[{symbol}]')
            position_value = nonnegative_float(
                pos.shares * price, f'market_value[{symbol}]')
            market_value = nonnegative_float(
                market_value + position_value, 'market value')
        return market_value

    def estimate_total_asset(self, prices: Dict[str, float]) -> float:
        """Estimate total asset with override prices without mutating positions."""
        self._validate_cash_state()
        return finite_float(
            self.cash + self.frozen_cash + self.estimate_market_value(prices),
            'total asset')

    def cancel_expired_orders(self, current_date: str) -> List[Order]:
        """
        取消过期订单（限价单当日有效）

        Args:
            current_date: 当前交易日期

        Returns:
            被取消的订单列表
        """
        cancelled = []
        for order in list(self.orders.values()):
            # DAY 委托可能在 T 的后验回调创建、到 T+1 才首次生效。
            # 不能再按 create_date 取消，否则会在它获得第一次撮合机会前杀掉。
            expiry = order.expire_date or order.create_date
            if order.is_open and expiry and expiry < current_date:
                order.cancel()
                # 买单：解冻资金
                if order.is_buy and order.frozen_amount > 0:
                    self.unfreeze_cash(order.frozen_amount)
                    order.frozen_amount = 0
                # 卖单：解冻股票
                if order.is_sell and order.frozen_shares > 0:
                    self.unfreeze_shares(order.symbol, order.frozen_shares)
                    order.frozen_shares = 0
                cancelled.append(order)
                # 移到历史
                self.order_history.append(order)
                del self.orders[order.order_id]

        return cancelled

    def add_order(self, order: Order) -> None:
        """添加订单"""
        self.orders[order.order_id] = order

    def complete_order(self, order: Order) -> None:
        """完成订单（移到历史）"""
        if order.is_done:
            self.order_history.append(order)
            if order.order_id in self.orders:
                del self.orders[order.order_id]

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'initial_cash': self.initial_cash,
            'cash': round(self.cash, 2),
            'frozen_cash': round(self.frozen_cash, 2),
            'market_value': round(self.market_value, 2),
            'total_asset': round(self.total_asset, 2),
            'positions': {k: v.to_dict() for k, v in self.positions.items()},
            'open_orders': len(self.get_open_orders()),
        }

    def __repr__(self) -> str:
        return (f"Account(cash={self.cash:.2f}, frozen={self.frozen_cash:.2f}, "
                f"market_value={self.market_value:.2f}, total={self.total_asset:.2f})")
