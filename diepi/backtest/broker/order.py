"""
订单类

订单状态生命周期:
PENDING -> SUBMITTED -> FILLED/PARTIAL/REJECTED/CANCELLED
"""

from enum import Enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
import uuid

from .validation import (
    finite_float,
    nonnegative_float,
    nonnegative_int,
    positive_float,
    positive_int,
    positive_product,
    unit_fraction,
)


class OrderType(Enum):
    """订单类型"""
    OPEN = 'open'           # 开盘价成交
    CLOSE = 'close'         # 收盘价成交
    MARKET = 'market'       # 市价成交 (当前bar的价格)
    LIMIT = 'limit'         # 限价单
    STOP = 'stop'           # 止损单 (价格下穿触发)
    STOP_PROFIT = 'stop_profit'  # 止盈单 (价格上穿触发)


class OrderSide(Enum):
    """买卖方向"""
    BUY = 'buy'
    SELL = 'sell'


class OrderStatus(Enum):
    """订单状态"""
    PENDING = 'pending'         # 待提交
    SUBMITTED = 'submitted'     # 已提交，等待成交
    FILLED = 'filled'           # 完全成交
    PARTIAL = 'partial'         # 部分成交
    REJECTED = 'rejected'       # 拒绝（资金/持仓不足等）
    CANCELLED = 'cancelled'     # 已撤销


@dataclass
class Order:
    """
    订单类

    Attributes:
        order_id: 订单唯一ID
        symbol: 股票代码
        side: 买卖方向
        order_type: 订单类型
        price: 限价单价格 (市价单为None)
        shares: 委托股数
        amount: 委托金额 (用于计算shares)
        status: 订单状态
        filled_shares: 已成交股数
        filled_amount: 已成交金额
        avg_price: 成交均价
        create_time: 创建时间
        update_time: 更新时间
        note: 备注
        reject_reason: 拒绝原因
    """
    symbol: str
    side: OrderSide
    order_type: OrderType
    shares: int = 0
    price: Optional[float] = None
    amount: Optional[float] = None

    order_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: OrderStatus = OrderStatus.PENDING
    filled_shares: int = 0
    filled_amount: float = 0.0
    avg_price: float = 0.0
    frozen_amount: float = 0.0  # 该订单冻结的资金金额（仅买单有效）
    frozen_shares: int = 0  # 该订单冻结的股数（仅卖单有效）
    commission_paid: float = 0.0
    stamp_duty_paid: float = 0.0
    transfer_fee_paid: float = 0.0
    total_fees: float = 0.0
    fee_fill_count: int = 0
    requested_shares: Optional[int] = None
    requested_amount: Optional[float] = None
    requested_percent: Optional[float] = None
    auto_resized: bool = False
    resized_from_shares: Optional[int] = None
    resized_to_shares: Optional[int] = None
    resize_reason: str = ''
    sizing_price: Optional[float] = None  # 竞价单委托量折算价（limit_up sizing 审计可见性）
    # 模拟事件时钟。create_time/update_time 暂时保留旧的墙钟字段用于兼容，
    # 因果撮合只依赖以下字段。
    created_phase: str = ''
    sim_created_at: Optional[datetime] = None
    eligible_from: Optional[datetime] = None
    expire_date: Optional[str] = None  # DAY TIF 所属交易日（YYYYMMDD）
    create_time: Optional[datetime] = None
    update_time: Optional[datetime] = None
    create_date: Optional[str] = None  # YYYYMMDD 用于T+1判断
    note: str = ''
    reject_reason: str = ''

    def __post_init__(self):
        """初始化后处理"""
        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        if not isinstance(self.side, OrderSide):
            raise ValueError("side must be an OrderSide")
        if not isinstance(self.order_type, OrderType):
            raise ValueError("order_type must be an OrderType")
        if not isinstance(self.status, OrderStatus):
            raise ValueError("status must be an OrderStatus")

        # shares=0 and the zero-valued aggregates are intentional sentinels
        # for rejected/deferred/unfilled orders.  Negative or non-finite state
        # is never legal.
        self.shares = nonnegative_int(self.shares, 'shares')
        if self.price is not None:
            self.price = positive_float(self.price, 'price')
        if self.amount is not None:
            self.amount = positive_float(self.amount, 'amount')
        self.filled_shares = nonnegative_int(
            self.filled_shares, 'filled_shares')
        self.filled_amount = nonnegative_float(
            self.filled_amount, 'filled_amount')
        self.avg_price = nonnegative_float(self.avg_price, 'avg_price')
        self.frozen_amount = nonnegative_float(
            self.frozen_amount, 'frozen_amount')
        self.frozen_shares = nonnegative_int(
            self.frozen_shares, 'frozen_shares')
        self.commission_paid = nonnegative_float(
            self.commission_paid, 'commission_paid')
        self.stamp_duty_paid = nonnegative_float(
            self.stamp_duty_paid, 'stamp_duty_paid')
        self.transfer_fee_paid = nonnegative_float(
            self.transfer_fee_paid, 'transfer_fee_paid')
        self.total_fees = nonnegative_float(
            self.total_fees, 'total_fees')
        self.fee_fill_count = nonnegative_int(
            self.fee_fill_count, 'fee_fill_count')
        expected_fees = round(
            self.commission_paid
            + self.stamp_duty_paid
            + self.transfer_fee_paid,
            2,
        )
        if abs(self.total_fees - expected_fees) > 1e-9:
            raise ValueError("total_fees must equal all cumulative fee components")
        if self.fee_fill_count == 0 and self.total_fees != 0:
            raise ValueError("an order without fee fills must have zero fees")

        if self.filled_shares > self.shares:
            raise ValueError("filled_shares cannot exceed shares")
        if self.frozen_shares > self.shares:
            raise ValueError("frozen_shares cannot exceed shares")
        if self.filled_shares == 0:
            if self.filled_amount != 0 or self.avg_price != 0:
                raise ValueError(
                    "unfilled order must have zero filled_amount and avg_price")
        elif self.filled_amount <= 0 or self.avg_price <= 0:
            raise ValueError(
                "filled order must have positive filled_amount and avg_price")

        if self.requested_shares is not None:
            self.requested_shares = positive_int(
                self.requested_shares, 'requested_shares')
        if self.requested_amount is not None:
            self.requested_amount = positive_float(
                self.requested_amount, 'requested_amount')
        if self.requested_percent is not None:
            self.requested_percent = unit_fraction(
                self.requested_percent, 'requested_percent')
        if not isinstance(self.auto_resized, bool):
            raise ValueError("auto_resized must be a bool")
        if self.resized_from_shares is not None:
            self.resized_from_shares = positive_int(
                self.resized_from_shares, 'resized_from_shares')
        if self.resized_to_shares is not None:
            self.resized_to_shares = positive_int(
                self.resized_to_shares, 'resized_to_shares')
        if (self.resized_from_shares is not None
                and self.resized_to_shares is not None
                and self.resized_to_shares > self.resized_from_shares):
            raise ValueError(
                "resized_to_shares cannot exceed resized_from_shares")
        if self.sizing_price is not None:
            self.sizing_price = positive_float(
                self.sizing_price, 'sizing_price')

        if self.create_time is None:
            self.create_time = datetime.now()
        self.update_time = self.create_time

    @property
    def is_buy(self) -> bool:
        """是否买单"""
        return self.side == OrderSide.BUY

    @property
    def is_sell(self) -> bool:
        """是否卖单"""
        return self.side == OrderSide.SELL

    @property
    def is_open(self) -> bool:
        """是否待成交"""
        return self.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL)

    @property
    def is_done(self) -> bool:
        """是否已完成（成交/拒绝/撤销）"""
        return self.status in (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED)

    @property
    def unfilled_shares(self) -> int:
        """未成交股数"""
        return self.shares - self.filled_shares

    def submit(self) -> 'Order':
        """提交订单"""
        if self.status == OrderStatus.PENDING:
            self.status = OrderStatus.SUBMITTED
            self.update_time = datetime.now()
        return self

    def fill(self, shares: int, price: float) -> 'Order':
        """
        成交

        Args:
            shares: 成交股数
            price: 成交价格
        """
        shares = positive_int(shares, 'fill shares')
        price = positive_float(price, 'fill price')
        if shares > self.unfilled_shares:
            raise ValueError("fill shares cannot exceed unfilled shares")

        # Validate every derived value before mutating the order.  This also
        # catches finite inputs whose notional overflows float64.
        fill_amount = positive_product(shares, price, 'fill amount')
        new_filled_amount = finite_float(
            self.filled_amount + fill_amount, 'filled_amount')
        new_filled_shares = self.filled_shares + shares
        new_avg_price = positive_float(
            new_filled_amount / new_filled_shares, 'avg_price')
        new_update_time = datetime.now()

        # 更新成交信息
        self.filled_amount = new_filled_amount
        self.filled_shares = new_filled_shares
        self.avg_price = new_avg_price

        # 更新状态
        if self.filled_shares >= self.shares:
            self.status = OrderStatus.FILLED
        else:
            self.status = OrderStatus.PARTIAL

        self.update_time = new_update_time
        return self

    def record_fees(self, commission: float, stamp_duty: float,
                    transfer_fee: float, total: float) -> 'Order':
        """Atomically add the auditable fee components for one fill."""
        commission = nonnegative_float(commission, 'commission')
        stamp_duty = nonnegative_float(stamp_duty, 'stamp_duty')
        transfer_fee = nonnegative_float(transfer_fee, 'transfer_fee')
        total = nonnegative_float(total, 'total fee')
        if abs(total - round(
                commission + stamp_duty + transfer_fee, 2)) > 1e-9:
            raise ValueError("total fee must equal all fee components")

        new_commission = nonnegative_float(
            round(self.commission_paid + commission, 2),
            'commission_paid')
        new_stamp_duty = nonnegative_float(
            round(self.stamp_duty_paid + stamp_duty, 2),
            'stamp_duty_paid')
        new_transfer_fee = nonnegative_float(
            round(self.transfer_fee_paid + transfer_fee, 2),
            'transfer_fee_paid')
        new_total = nonnegative_float(
            round(self.total_fees + total, 2), 'total_fees')
        if abs(new_total - round(
                new_commission + new_stamp_duty + new_transfer_fee, 2)) > 1e-9:
            raise ValueError(
                "cumulative total_fees must equal all fee components")
        new_fill_count = nonnegative_int(
            self.fee_fill_count + 1, 'fee_fill_count')

        self.commission_paid = new_commission
        self.stamp_duty_paid = new_stamp_duty
        self.transfer_fee_paid = new_transfer_fee
        self.total_fees = new_total
        self.fee_fill_count = new_fill_count
        return self

    def reject(self, reason: str) -> 'Order':
        """拒绝订单"""
        if self.filled_shares > 0:
            raise ValueError(
                "cannot reject an order with fills; cancel the residual instead")
        self.status = OrderStatus.REJECTED
        self.reject_reason = reason
        self.update_time = datetime.now()
        return self

    def cancel(self) -> 'Order':
        """撤销订单"""
        if self.is_open:
            self.status = OrderStatus.CANCELLED
            self.update_time = datetime.now()
        return self

    def to_dict(self) -> dict:
        """转换为字典"""
        return {
            'order_id': self.order_id,
            'symbol': self.symbol,
            'side': self.side.value,
            'order_type': self.order_type.value,
            'price': self.price,
            'shares': self.shares,
            'status': self.status.value,
            'filled_shares': self.filled_shares,
            'filled_amount': self.filled_amount,
            'avg_price': self.avg_price,
            'frozen_amount': self.frozen_amount,
            'frozen_shares': self.frozen_shares,
            'commission_paid': self.commission_paid,
            'stamp_duty_paid': self.stamp_duty_paid,
            'transfer_fee_paid': self.transfer_fee_paid,
            'total_fees': self.total_fees,
            'fee_fill_count': self.fee_fill_count,
            'requested_shares': self.requested_shares,
            'requested_amount': self.requested_amount,
            'requested_percent': self.requested_percent,
            'auto_resized': self.auto_resized,
            'resized_from_shares': self.resized_from_shares,
            'resized_to_shares': self.resized_to_shares,
            'resize_reason': self.resize_reason,
            'sizing_price': self.sizing_price,
            'created_phase': self.created_phase,
            'sim_created_at': self.sim_created_at.isoformat() if self.sim_created_at else None,
            'eligible_from': self.eligible_from.isoformat() if self.eligible_from else None,
            'expire_date': self.expire_date,
            'create_time': self.create_time.isoformat() if self.create_time else None,
            'note': self.note,
            'reject_reason': self.reject_reason,
        }

    def __repr__(self) -> str:
        return (f"Order({self.order_id}, {self.symbol}, {self.side.value}, "
                f"{self.order_type.value}, shares={self.shares}, "
                f"status={self.status.value})")
