"""
持仓类

管理股票持仓，支持T+1规则
"""

from decimal import Decimal, InvalidOperation
from dataclasses import dataclass, field
from typing import Optional, Dict
from datetime import datetime

from .validation import (
    finite_float,
    nonnegative_float,
    nonnegative_int,
    positive_float,
    positive_int,
    positive_product,
)


@dataclass
class Position:
    """
    持仓类

    Attributes:
        symbol: 股票代码
        shares: 总持仓股数
        frozen_shares: 冻结股数 (卖单占用)
        cost_basis: 持仓成本 (总成本)
        avg_cost: 平均成本价
        current_price: 当前价格
        last_update: 最后更新时间
        entry_date: 首次买入日期 (YYYYMMDD)
    """
    symbol: str
    shares: int = 0
    frozen_shares: int = 0  # 卖单冻结的股数
    cost_basis: float = 0.0
    avg_cost: float = 0.0
    current_price: float = 0.0
    last_update: Optional[datetime] = None
    entry_date: Optional[str] = None  # 首次买入日期

    # T+1 追踪: {买入日期: 股数}
    _pending_shares: Dict[str, int] = field(default_factory=dict)

    # 当前交易日期（用于计算持有天数）
    _current_date: Optional[str] = None

    def __post_init__(self):
        self._validate_state()
        if self.last_update is None:
            self.last_update = datetime.now()

    @staticmethod
    def _validate_date(value: str, name: str) -> str:
        if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
            raise ValueError(f"{name} must use YYYYMMDD")
        try:
            datetime.strptime(value, '%Y%m%d')
        except ValueError:
            raise ValueError(f"{name} must use YYYYMMDD") from None
        return value

    def _validate_state(self) -> None:
        """Validate the complete position state without mutating it."""
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        shares = nonnegative_int(self.shares, 'shares')
        frozen = nonnegative_int(self.frozen_shares, 'frozen_shares')
        cost_basis = nonnegative_float(self.cost_basis, 'cost_basis')
        avg_cost = nonnegative_float(self.avg_cost, 'avg_cost')
        current_price = nonnegative_float(
            self.current_price, 'current_price')
        if frozen > shares:
            raise ValueError("frozen_shares cannot exceed shares")
        if shares == 0 and (cost_basis != 0 or avg_cost != 0):
            raise ValueError("empty position must have zero cost_basis and avg_cost")
        if shares > 0 and (cost_basis <= 0 or avg_cost <= 0):
            raise ValueError("non-empty position must have positive cost state")
        if self.entry_date is not None:
            self._validate_date(self.entry_date, 'entry_date')
        if self._current_date is not None:
            self._validate_date(self._current_date, 'current_date')
        if not isinstance(self._pending_shares, dict):
            raise ValueError("pending shares must be a dict")
        pending_total = 0
        for trade_date, pending in self._pending_shares.items():
            self._validate_date(trade_date, 'pending trade_date')
            pending_total += nonnegative_int(
                pending, f'pending_shares[{trade_date}]')
        if frozen + pending_total > shares:
            raise ValueError(
                "frozen and pending shares cannot exceed total shares")


    @property
    def available_shares(self) -> int:
        """可卖股数 = 总股数 - 冻结股数 - 待解锁股数(T+1)"""
        self._validate_state()
        pending = sum(self._pending_shares.values())
        return self.shares - self.frozen_shares - pending

    def freeze_shares(self, shares: int) -> int:
        """
        冻结股票（卖单提交时）

        Args:
            shares: 请求冻结的股数

        Returns:
            实际冻结的股数
        """
        shares = nonnegative_int(shares, 'shares')
        actual = min(shares, self.available_shares)
        if actual > 0:
            self.frozen_shares = nonnegative_int(
                self.frozen_shares + actual, 'frozen_shares')
        return actual

    def unfreeze_shares(self, shares: int) -> None:
        """
        解冻股票（卖单取消或成交后）

        Args:
            shares: 解冻股数
        """
        shares = nonnegative_int(shares, 'shares')
        self._validate_state()
        actual = min(shares, self.frozen_shares)
        self.frozen_shares -= actual

    @property
    def market_value(self) -> float:
        """持仓市值"""
        self._validate_state()
        if self.shares == 0 or self.current_price == 0:
            return 0.0
        return positive_product(
            self.shares, self.current_price, 'market_value')

    @property
    def profit(self) -> float:
        """浮动盈亏"""
        return finite_float(self.market_value - self.cost_basis, 'profit')

    @property
    def profit_pct(self) -> float:
        """浮动盈亏比例"""
        if self.cost_basis > 0:
            return finite_float(
                self.profit / self.cost_basis, 'profit_pct')
        return 0.0

    @property
    def hold_days(self) -> int:
        """
        持有天数（自然日）

        从首次买入日期到当前日期的天数
        """
        if not self.entry_date or not self._current_date:
            return 0
        try:
            entry = datetime.strptime(self.entry_date, '%Y%m%d')
            current = datetime.strptime(self._current_date, '%Y%m%d')
            return (current - entry).days
        except ValueError:
            return 0

    def set_current_date(self, date: str) -> None:
        """设置当前日期（用于计算持有天数）"""
        self._current_date = self._validate_date(date, 'current_date')

    def apply_split_ratio(self, ratio: float,
                          update_time: Optional[datetime] = None) -> None:
        """Atomically apply a split only when every entitlement is integral.

        This layer has no cash-in-lieu ledger.  Fractional total, frozen, or
        pending entitlements therefore fail fast instead of being rounded.
        """
        if type(ratio) not in (int, float):
            raise ValueError("split ratio must be finite and positive")
        try:
            ratio_decimal = Decimal(str(ratio))
        except (InvalidOperation, ValueError):
            raise ValueError("split ratio must be finite and positive") from None
        if not ratio_decimal.is_finite() or ratio_decimal <= 0:
            raise ValueError("split ratio must be finite and positive")
        if update_time is not None:
            if type(update_time) is not datetime or update_time.tzinfo is not None:
                raise ValueError("update_time must be a timezone-naive datetime")
            if update_time.fold != 0:
                raise ValueError("update_time fold must be 0")
        try:
            self._validate_state()
        except ValueError:
            raise ValueError("invalid position state for split adjustment") from None
        if self.shares <= 0 or ratio_decimal == 1:
            return

        def _integer_entitlement(quantity: int, label: str) -> int:
            entitlement = Decimal(quantity) * ratio_decimal
            integral = entitlement.to_integral_value()
            if entitlement != integral:
                raise ValueError(
                    f"fractional split entitlement for {label}: {entitlement}")
            return int(integral)

        try:
            old_market_value = nonnegative_float(
                self.shares * self.current_price, 'old_market_value')
            new_shares = _integer_entitlement(self.shares, 'shares')
            if new_shares <= 0:
                raise ValueError("split ratio produces zero position shares")
            new_frozen_shares = _integer_entitlement(
                self.frozen_shares, 'frozen_shares')
            new_pending_shares = {
                date: nonnegative_int(
                    _integer_entitlement(
                        shares, f'pending_shares[{date}]'),
                    f'pending_shares[{date}]',
                )
                for date, shares in self._pending_shares.items()
            }
            if new_frozen_shares + sum(new_pending_shares.values()) > new_shares:
                raise ValueError(
                    "adjusted frozen and pending shares exceed total shares")
            new_avg_cost = positive_float(
                self.cost_basis / new_shares, 'avg_cost')
            new_current_price = (
                nonnegative_float(
                    old_market_value / new_shares, 'current_price')
                if self.current_price > 0 else self.current_price
            )
            new_last_update = update_time or datetime.now()
        except ValueError as exc:
            if "fractional split entitlement" in str(exc):
                raise
            raise ValueError("invalid position state for split adjustment") from None
        except (TypeError, OverflowError, ZeroDivisionError):
            raise ValueError("invalid position state for split adjustment") from None

        self.shares = new_shares
        self.frozen_shares = new_frozen_shares
        self._pending_shares = new_pending_shares
        self.avg_cost = new_avg_cost
        self.current_price = new_current_price
        self.last_update = new_last_update

    def apply_adjustment_factor_total_return(
            self, ratio: float,
            update_time: Optional[datetime] = None) -> float:
        """Apply a factor jump as integer reinvestment plus cash in lieu.

        A bare adjustment factor does not identify the legal dividend/split
        terms.  The documented factor-based model is therefore explicit:
        tax-free immediate total-return reinvestment at the ex-date reference
        price.  Share buckets are rounded down independently, and only the
        total-position fractional remainder is returned as cash in lieu.
        Direct, authoritative split actions continue through
        :meth:`apply_split_ratio` and retain strict fractional rejection.
        """
        if type(ratio) not in (int, float):
            raise ValueError("factor ratio must be finite and positive")
        try:
            ratio_decimal = Decimal(str(ratio))
        except (InvalidOperation, ValueError):
            raise ValueError(
                "factor ratio must be finite and positive") from None
        if not ratio_decimal.is_finite() or ratio_decimal <= 0:
            raise ValueError("factor ratio must be finite and positive")
        if update_time is not None:
            if type(update_time) is not datetime or update_time.tzinfo is not None:
                raise ValueError("update_time must be a timezone-naive datetime")
            if update_time.fold != 0:
                raise ValueError("update_time fold must be 0")
        try:
            self._validate_state()
        except ValueError:
            raise ValueError(
                "invalid position state for factor total-return adjustment") from None
        if self.shares <= 0:
            return 0.0

        old_price = positive_float(
            self.current_price, 'factor adjustment previous mark')
        old_price_decimal = Decimal(str(old_price))
        entitlement = Decimal(self.shares) * ratio_decimal
        new_shares = int(entitlement.to_integral_value(rounding='ROUND_FLOOR'))
        if new_shares <= 0:
            raise ValueError("factor ratio produces zero position shares")
        new_frozen_shares = int(
            (Decimal(self.frozen_shares) * ratio_decimal).to_integral_value(
                rounding='ROUND_FLOOR'))
        new_pending_shares = {
            date: int(
                (Decimal(shares) * ratio_decimal).to_integral_value(
                    rounding='ROUND_FLOOR'))
            for date, shares in self._pending_shares.items()
        }
        if new_frozen_shares + sum(new_pending_shares.values()) > new_shares:
            raise ValueError(
                "adjusted frozen and pending shares exceed total shares")
        new_price_decimal = old_price_decimal / ratio_decimal
        cash_decimal = (entitlement - Decimal(new_shares)) * new_price_decimal
        try:
            new_price = positive_float(
                float(new_price_decimal), 'factor adjustment new mark')
            cash_amount = nonnegative_float(
                float(cash_decimal), 'factor adjustment cash in lieu')
            new_avg_cost = positive_float(
                self.cost_basis / new_shares, 'avg_cost')
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                "invalid position state for factor total-return adjustment") from None

        self.shares = new_shares
        self.frozen_shares = new_frozen_shares
        self._pending_shares = new_pending_shares
        self.avg_cost = new_avg_cost
        self.current_price = new_price
        self.last_update = update_time or datetime.now()
        return cash_amount

    def buy(self, shares: int, price: float, trade_date: str, commission: float = 0.0,
            t0: bool = False) -> None:
        """
        买入

        Args:
            shares: 买入股数
            price: 买入价格
            trade_date: 交易日期 (YYYYMMDD)，用于T+1追踪
            commission: 手续费（由account传入，计入成本）
            t0: T+0 品种（债券/货币/跨境/黄金 ETF 等），买入当日即可卖出
        """
        self._validate_state()
        shares = positive_int(shares, 'shares')
        price = positive_float(price, 'price')
        commission = nonnegative_float(commission, 'commission')
        trade_date = self._validate_date(trade_date, 'trade_date')
        if not isinstance(t0, bool):
            raise ValueError("t0 must be a bool")

        notional = positive_product(shares, price, 'buy notional')
        buy_amount = positive_float(notional + commission, 'buy amount')
        new_cost_basis = positive_float(
            self.cost_basis + buy_amount, 'cost_basis')
        new_shares = nonnegative_int(self.shares + shares, 'shares')
        new_avg_cost = positive_float(
            new_cost_basis / new_shares, 'avg_cost')
        new_pending_shares = dict(self._pending_shares)
        if not t0:
            new_pending_shares[trade_date] = nonnegative_int(
                new_pending_shares.get(trade_date, 0) + shares,
                f'pending_shares[{trade_date}]',
            )
        if (self.frozen_shares + sum(new_pending_shares.values())
                > new_shares):
            raise ValueError(
                "frozen and pending shares cannot exceed total shares")

        # Commit only after all derived values have passed validation.
        self.entry_date = self.entry_date or trade_date
        self.cost_basis = new_cost_basis
        self.shares = new_shares
        self.avg_cost = new_avg_cost
        # A fill is an observable price fact.  Mark immediately so account
        # APIs cannot expose a zero/stale market value between settlement and
        # the next bar-level Mark event.
        self.current_price = price
        self._pending_shares = new_pending_shares
        self.last_update = datetime.now()

    def sell(self, shares: int, price: float) -> float:
        """
        卖出

        注意: 调用此方法前，broker 应先解冻对应的 frozen_shares

        Args:
            shares: 卖出股数
            price: 卖出价格

        Returns:
            实际卖出金额
        """
        self._validate_state()
        shares = positive_int(shares, 'shares')
        price = positive_float(price, 'price')

        actual_shares = min(shares, self.available_shares)
        if actual_shares <= 0:
            return 0.0

        sell_amount = positive_product(
            actual_shares, price, 'sell amount')
        sell_cost = positive_product(
            actual_shares, self.avg_cost, 'sell cost')
        new_shares = nonnegative_int(
            self.shares - actual_shares, 'shares')

        if new_shares == 0:
            new_cost_basis = 0.0
            new_avg_cost = 0.0
            new_current_price = 0.0
            new_frozen_shares = 0
            new_entry_date = None
            new_pending_shares = {}
        else:
            new_cost_basis = nonnegative_float(
                self.cost_basis - sell_cost, 'cost_basis')
            new_avg_cost = positive_float(
                new_cost_basis / new_shares, 'avg_cost')
            new_current_price = price
            new_frozen_shares = self.frozen_shares
            new_entry_date = self.entry_date
            new_pending_shares = dict(self._pending_shares)

        # Commit only after all derived values have passed validation.
        self.shares = new_shares
        self.frozen_shares = new_frozen_shares
        self.cost_basis = new_cost_basis
        self.avg_cost = new_avg_cost
        self.current_price = new_current_price
        self.entry_date = new_entry_date
        self._pending_shares = new_pending_shares
        self.last_update = datetime.now()
        return sell_amount

    def settle_t1(self, current_date: str) -> int:
        """
        T+1 结算：将之前买入的股票变为可卖

        Args:
            current_date: 当前交易日期 (YYYYMMDD)
        """
        # 将所有小于当前日期的待解锁股数从 _pending_shares 中删除
        # available_shares 是计算属性，删除后会自动增加可卖数
        self._validate_state()
        current_date = self._validate_date(current_date, 'current_date')
        unlocked = sum(
            pending
            for buy_date, pending in self._pending_shares.items()
            if buy_date < current_date
        )
        remaining = {
            buy_date: pending
            for buy_date, pending in self._pending_shares.items()
            if buy_date >= current_date
        }
        self._pending_shares = remaining
        return unlocked

    def update_price(self, price: float,
                     update_time: Optional[datetime] = None) -> None:
        """更新当前价格"""
        price = positive_float(price, 'price')
        if update_time is not None:
            if type(update_time) is not datetime or update_time.tzinfo is not None:
                raise ValueError("update_time must be a timezone-naive datetime")
            if update_time.fold != 0:
                raise ValueError("update_time fold must be 0")
        self._validate_state()
        self.current_price = price
        self.last_update = update_time or datetime.now()

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'symbol': self.symbol,
            'shares': self.shares,
            'available_shares': self.available_shares,
            'frozen_shares': self.frozen_shares,
            'cost_basis': round(self.cost_basis, 2),
            'avg_cost': round(self.avg_cost, 4),
            'current_price': self.current_price,
            'market_value': round(self.market_value, 2),
            'profit': round(self.profit, 2),
            'profit_pct': round(self.profit_pct * 100, 2),
            'entry_date': self.entry_date,
            'hold_days': self.hold_days,
        }

    def __repr__(self) -> str:
        return (f"Position({self.symbol}, shares={self.shares}, "
                f"available={self.available_shares}, avg_cost={self.avg_cost:.2f})")
