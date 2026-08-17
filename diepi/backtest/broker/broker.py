"""
撮合引擎

处理订单执行和成交
"""

import logging
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from math import ceil
from typing import Optional, Dict, List, Union, Any
from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, time

from .order import Order, OrderType, OrderSide, OrderStatus
from .position import Position
from .account import Account
from .fees import FeeBreakdown, FeeEngine, FeeSide, ParentOrderFeeState
from .events import (
    CorporateActionPayload,
    EventAttributes,
    ExecutionEvent,
    ExecutionEventJournal,
    ExecutionEventType,
    FillPayload,
    MarkPayload,
    OrderEventPayload,
    ReservationPayload,
    SettlementPayload,
    T1UnlockPayload,
)
from .settlement import SettlementInvariantError, SettlementUnitOfWork
from .target import (
    TargetAchievement,
    TargetAchievementStatus,
    TargetIntent,
    TargetPlanSide,
)
from ..liquidity import AuctionLiquidityUnavailable
from ..rulebook import (
    DEFAULT_RULEBOOK,
    ExecutionEngine,
    LIMIT_BAND_FUND_SNAPSHOT_FREEZE_DATE,
    LimitBandRoundingPolicy,
    LimitBandService,
    RuleBook,
)
from .validation import (
    finite_float,
    fraction_below_one,
    nonnegative_float,
    nonnegative_int,
    positive_float,
    positive_int,
    positive_product,
    unit_fraction,
    unit_interval,
)

logger = logging.getLogger(__name__)


@dataclass
class BarData:
    """K线数据（撮合用）

    amount 单位约定为「元」。数据源单位不一致（tushare 日线 amount=千元、
    分钟线 amount=元），由引擎在构造本对象时统一归一化为元。
    """
    symbol: str
    trade_time: any       # datetime 或可转换为 datetime 的类型
    open: float
    high: float
    low: float
    close: float
    vol: float = 0.0
    amount: float = 0.0   # 成交额（元）
    pre_close: float = 0.0  # 前收盘价（用于涨跌停判断）
    # Exact-day adjustment factor divided by the previous trade day's
    # factor.  It lets the broker reconcile vendor ``pre_close`` (which may
    # remain in the prior raw price space) with an ex-right execution bar.
    adjustment_factor_ratio: Optional[float] = None
    # 已由上游因果流动性策略解析出的最终可执行名义金额。它不是参与率的
    # 输入，Broker 不得再乘 liquidity_cap_ratio。
    liquidity_cap_amount: Optional[float] = None
    require_explicit_liquidity_cap: bool = False
    liquidity_window: Optional[str] = None  # 审计标签，例如 open/close

    def __post_init__(self):
        """Normalize a bar only after validating its market-data contract."""
        import pandas as pd

        if not isinstance(self.symbol, str) or not self.symbol.strip():
            raise ValueError("symbol must be a non-empty string")

        if self.trade_time is None or self.trade_time is pd.NaT:
            raise ValueError("trade_time must be a valid datetime")
        if isinstance(self.trade_time, pd.Timestamp):
            if pd.isna(self.trade_time):
                raise ValueError("trade_time must be a valid datetime")
            self.trade_time = self.trade_time.to_pydatetime()
        elif isinstance(self.trade_time, str):
            if not self.trade_time.strip():
                raise ValueError("trade_time must be a valid datetime")
            try:
                parsed = pd.to_datetime(self.trade_time, errors='raise')
            except (TypeError, ValueError, OverflowError):
                raise ValueError("trade_time must be a valid datetime") from None
            if pd.isna(parsed):
                raise ValueError("trade_time must be a valid datetime")
            self.trade_time = parsed.to_pydatetime()
        elif not isinstance(self.trade_time, datetime):
            raise ValueError("trade_time must be a valid datetime")

        self.open = positive_float(self.open, 'open')
        self.high = positive_float(self.high, 'high')
        self.low = positive_float(self.low, 'low')
        self.close = positive_float(self.close, 'close')
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must be >= open, low, and close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must be <= open, high, and close")

        # Zero volume/amount and pre_close=0 are intentional sentinels:
        # respectively no liquidity and unavailable previous-close data.
        self.vol = nonnegative_float(self.vol, 'vol')
        self.amount = nonnegative_float(self.amount, 'amount')
        self.pre_close = nonnegative_float(self.pre_close, 'pre_close')
        if self.adjustment_factor_ratio is not None:
            self.adjustment_factor_ratio = positive_float(
                self.adjustment_factor_ratio, 'adjustment_factor_ratio')
        if self.liquidity_cap_amount is not None:
            self.liquidity_cap_amount = positive_float(
                self.liquidity_cap_amount, 'liquidity_cap_amount')
        if type(self.require_explicit_liquidity_cap) is not bool:
            raise ValueError("require_explicit_liquidity_cap must be a bool")
        if self.liquidity_window is not None:
            if (not isinstance(self.liquidity_window, str)
                    or not self.liquidity_window.strip()):
                raise ValueError(
                    "liquidity_window must be a non-empty string or None")
            self.liquidity_window = self.liquidity_window.strip().lower()


@dataclass(frozen=True)
class _OrderCreationScope:
    """引擎传给 Broker 的模拟事件上下文。"""

    phase: str
    sim_time: datetime
    eligible_from: Optional[datetime]
    expire_date: Optional[str]
    close_eligible_from: Optional[datetime]
    close_expire_date: Optional[str]
    close_symbols: Optional[frozenset]


@dataclass(frozen=True)
class _TargetIntentScope:
    """Exact close window made available to target-intent creation."""

    match_at: datetime


class Broker:
    """
    撮合引擎

    职责：
    1. 接收下单请求，创建订单
    2. 验证订单有效性（资金、持仓）
    3. 在合适时机执行订单撮合
    4. 更新账户状态

    撮合规则：
    - 开盘单：卖出用 raw open；买入由 open_buy_fill_mode 决定是否加滑点
    - 市价单：买入用 high + 滑点，卖出用 low - 滑点
    - 限价单：检查价格是否触及
    """

    def __init__(self, account: Account = None, slippage: float = 0.001,
                 lot_size: int = 100, allow_odd_lot_sell: bool = True,
                 open_buy_resize_mode: str = 'auto',
                 liquidity_cap_ratio: float = 0.8,
                 limit_pct_overrides: Dict[str, float] = None,
                 open_buy_fill_mode: str = 'open+slip',
                 open_buy_sizing: str = 'limit_up',
                 bar_liquidity_model: str = 'close_lock',
                 t0_overrides=None,
                 limit_check_exempt: Dict[str, set] = None,
                 commission_scope: str = 'parent_order',
                 transfer_fee_rate: float = 0.0,
                 rulebook: RuleBook = None):
        """
        Args:
            account: 账户对象
            slippage: 滑点比例 (默认千1)
            lot_size: 每手股数 (默认100；科创板/北交所有独立申报规则见 _lot_rule)
            allow_odd_lot_sell: 是否允许零股卖出
            open_buy_resize_mode: 开盘买单缩量模式 ('auto'/'legacy')
            liquidity_cap_ratio: 单根K线最大可吃成交额比例 (默认0.8，
                即单笔成交不超过该K线成交额的80%；bar.amount 单位为元)
            limit_pct_overrides: 涨跌停幅度覆盖表 {6位代码或完整symbol: 幅度}，
                如 {'159781': 0.20}；优先于内置规则，不随日期变化
            open_buy_fill_mode: 开盘买单成交价口径（收盘买固定含滑点）：
                'open+slip'(默认) = open×(1+slippage)，模拟"竞价卖出→回款买入"
                的执行摩擦（先卖后买用滑点惩罚）；
                'open' = 按开盘价原价成交（旧口径，复现历史结果用）
            open_buy_sizing: 开盘/收盘买单委托量折算基准：
                'limit_up'(默认) = 现金÷当日涨停价（模拟"挂涨停价抢单保证成交"
                的真实委托方式，委托时不知道成交价，按最坏价折算可买量）；
                'fill' = 现金÷成交价（旧口径，隐含已知开盘价的轻微前视）
            bar_liquidity_model: 涨跌停整 bar 流动性 veto。默认
                'close_lock'：close 等于涨停时仅阻断买、等于跌停时仅阻断卖；
                'any_touch'：high/low 触板即阻断，作为更严格压力模式
            t0_overrides: T+0 品种集合（代码/前缀/完整symbol），命中者当日买入
                当日可卖（如债券/货币/跨境/黄金 ETF）；默认全部 T+1
            limit_check_exempt: 涨跌停豁免日历 {symbol: {YYYYMMDD,...}}
                （新股上市初期无涨跌幅限制，由引擎按 list_date 计算注入）
            commission_scope: 最低佣金聚合边界；默认 'parent_order'，
                显式 'fill' 为逐成交压力模式
            transfer_fee_rate: 每笔成交的过户费率，默认 0
            rulebook: 证券分类与 tick/价格带规则来源；默认使用内置快照
        """
        resolved_rulebook = DEFAULT_RULEBOOK if rulebook is None else rulebook
        if not isinstance(resolved_rulebook, RuleBook):
            raise TypeError("rulebook must be a RuleBook or None")
        self.rulebook = resolved_rulebook
        self.limit_band_service = LimitBandService(resolved_rulebook)
        self.account = account or Account()
        schedule_stamp_rate = (
            self.account.stamp_duty_rate
            if self.account.stamp_duty_rate != 'auto'
            else 0.0
        )
        self.fee_engine = FeeEngine.from_rates(
            commission_rate=self.account.commission_rate,
            min_commission=self.account.min_commission,
            stamp_duty_rate=schedule_stamp_rate,
            transfer_fee_rate=transfer_fee_rate,
            commission_scope=commission_scope,
        )
        self.commission_scope = self.fee_engine.commission_scope.value
        self._order_fee_states: Dict[str, ParentOrderFeeState] = {}
        self.slippage = fraction_below_one(slippage, 'slippage')
        self.lot_size = positive_int(lot_size, 'lot_size')
        if not isinstance(allow_odd_lot_sell, bool):
            raise ValueError("allow_odd_lot_sell must be a bool")
        self.allow_odd_lot_sell = allow_odd_lot_sell
        self.liquidity_cap_ratio = unit_interval(
            liquidity_cap_ratio, 'liquidity_cap_ratio')
        raw_limit_overrides = (
            dict(limit_pct_overrides) if limit_pct_overrides else {})
        self.limit_pct_overrides = {}
        for symbol, limit_pct in raw_limit_overrides.items():
            if not isinstance(symbol, str) or not symbol.strip():
                raise ValueError(
                    "limit_pct_overrides keys must be non-empty strings")
            validated_pct = unit_fraction(
                limit_pct, f"limit_pct_overrides[{symbol!r}]")
            self.limit_pct_overrides[symbol] = validated_pct
        self.open_buy_fill_mode = (open_buy_fill_mode or 'open+slip').lower()
        if self.open_buy_fill_mode not in ('open+slip', 'open'):
            raise ValueError(
                f"Invalid open_buy_fill_mode={open_buy_fill_mode}, expected 'open+slip' or 'open'"
            )
        self.open_buy_sizing = (open_buy_sizing or 'limit_up').lower()
        if self.open_buy_sizing not in ('limit_up', 'fill'):
            raise ValueError(
                f"Invalid open_buy_sizing={open_buy_sizing}, expected 'limit_up' or 'fill'"
            )
        self.bar_liquidity_model = str(bar_liquidity_model).strip().lower()
        if self.bar_liquidity_model not in ('close_lock', 'any_touch'):
            raise ValueError(
                "bar_liquidity_model must be 'close_lock' or 'any_touch'"
            )
        if isinstance(t0_overrides, str):
            # frozenset('511') 会拆成 {'5','1'}——所有沪市基金悄悄变 T+0
            raise TypeError(
                f"t0_overrides 应为代码/前缀的集合（如 ['511','513']），"
                f"收到裸字符串 {t0_overrides!r}（会被拆成单字符前缀）"
            )
        self.t0_overrides = frozenset(t0_overrides) if t0_overrides else frozenset()
        if '' in self.t0_overrides:
            raise ValueError("t0_overrides 不允许空字符串（会匹配一切标的）")
        self._limit_check_exempt = dict(limit_check_exempt) if limit_check_exempt else {}
        self.open_buy_resize_mode = (open_buy_resize_mode or 'auto').lower()
        if self.open_buy_resize_mode not in ('auto', 'legacy'):
            raise ValueError(
                f"Invalid open_buy_resize_mode={open_buy_resize_mode}, expected 'auto' or 'legacy'"
            )

        # 当前交易日期
        self.current_date: Optional[str] = None

        # 交易时段: 'before_open', 'trading', 'after_close'
        self._market_phase: str = 'before_open'

        # 策略回调与市场阶段是两条轴：on_day/on_minute 都曾共用 trading，
        # 因此订单的因果来源不能从 _market_phase 反推。
        self._order_creation_scope: Optional[_OrderCreationScope] = None

        # 待执行的开盘单
        self._pending_open_orders: List[Order] = []

        # 待执行的收盘单
        self._pending_close_orders: List[Order] = []

        # 成交额限制: 每根K线已用成交额 {symbol: 已用金额}
        self._bar_used_amount: Dict[str, float] = {}

        # Open-buy execution audit
        self._open_buy_total: int = 0
        self._open_buy_auto_resized_count: int = 0
        self._open_buy_reject_insuff_cash_count: int = 0
        self._open_buy_reject_invalid_count: int = 0
        self._open_buy_resize_ratio_sum: float = 0.0

        # Every Broker instance is one deterministic simulation run.  IDs are
        # local counters rather than UUIDs, and the event envelope never reads
        # the wall clock.
        self._event_journal = ExecutionEventJournal()
        self._order_id_counter: int = 0
        self._event_id_counter: int = 0
        self._fill_id_counter: int = 0
        self._settlement_id_counter: int = 0
        self._corporate_action_id_counter: int = 0
        # Keep the object alongside its ID.  A bare ``id(order)`` key can be
        # reused by CPython after collection and would then incorrectly treat
        # a different order as already stamped.
        self._stamped_orders: Dict[int, tuple] = {}
        self._terminal_event_orders = set()
        self._journal_reservations: Dict[tuple, float] = {}
        self._active_match_time: Optional[datetime] = None
        self._target_intent_scope: Optional[_TargetIntentScope] = None
        self._target_intents: List[TargetIntent] = []
        self._target_achievements: List[TargetAchievement] = []
        self._target_intent_id_counter: int = 0
        self._target_batch_id_counter: int = 0
        self._target_order_intents: Dict[str, str] = {}

    @property
    def event_journal(self) -> ExecutionEventJournal:
        """The immutable-event source of truth for this broker run."""

        return self._event_journal

    @staticmethod
    def _event_attributes(**values) -> EventAttributes:
        return EventAttributes(tuple(
            (key, value) for key, value in values.items() if value is not None
        ))

    def _default_simulated_time(self) -> datetime:
        if self.current_date:
            try:
                return datetime.strptime(str(self.current_date), '%Y%m%d')
            except ValueError:
                pass
        return datetime(1970, 1, 1)

    def _simulated_time(self, preferred=None) -> datetime:
        if preferred is not None:
            result = self._coerce_sim_time(preferred)
        elif self._order_creation_scope is not None:
            result = self._order_creation_scope.sim_time
        else:
            result = self._default_simulated_time()
            if len(self._event_journal):
                previous_time = self._event_journal[-1].simulated_time
                if previous_time.date() == result.date() and previous_time > result:
                    result = previous_time
        if result.tzinfo is not None:
            raise ValueError("simulated event time must be timezone-naive")
        return result

    @staticmethod
    def _phase_number(phase: str) -> int:
        return {
            'day_start': 0,
            'before_open': 10,
            'opening_match': 20,
            'opening_mark': 25,
            'on_after_open': 30,
            'on_minute': 40,
            'continuous_match': 45,
            'continuous_mark': 48,
            'on_before_close': 50,
            'target_planning': 55,
            'closing_match': 60,
            'closing_mark': 65,
            'on_day': 70,
            'mark': 80,
            'after_close': 90,
            'day_end': 100,
        }.get(str(phase or '').strip().lower(), 50)

    def _prepare_events(self, *, simulated_time, phase: str, specs):
        """Construct and validate a deterministic batch without publishing it."""

        sim_time = self._simulated_time(simulated_time)
        phase_seq = self._phase_number(phase)
        previous = self._event_journal[-1] if len(self._event_journal) else None
        if previous is not None:
            if sim_time < previous.simulated_time:
                raise SettlementInvariantError(
                    "simulated event time moved backwards")
            if sim_time == previous.simulated_time:
                phase_seq = max(phase_seq, previous.phase_seq)
        first_sequence = self._event_journal.next_sequence
        first_event_id = self._event_id_counter + 1
        events = tuple(
            ExecutionEvent(
                event_id=f"event-{first_event_id + offset:012d}",
                event_type=event_type,
                simulated_time=sim_time,
                phase_seq=phase_seq,
                sequence=first_sequence + offset,
                payload=payload,
            )
            for offset, (event_type, payload) in enumerate(tuple(specs))
        )
        return events

    def _commit_prepared_events(self, events) -> tuple:
        appended = self._event_journal.append_batch(events)
        self._event_id_counter += len(appended)
        return appended

    def _append_events(self, *, simulated_time=None, phase: str, specs) -> tuple:
        events = self._prepare_events(
            simulated_time=simulated_time, phase=phase, specs=specs)
        return self._commit_prepared_events(events)

    def _next_order_id(self) -> str:
        occupied = set(self.account.orders)
        occupied.update(order.order_id for order in self.account.order_history)
        while True:
            self._order_id_counter += 1
            candidate = f"order-{self._order_id_counter:012d}"
            if candidate not in occupied:
                return candidate

    @staticmethod
    def _reservation_asset(order: Order) -> str:
        return 'CNY' if order.is_buy else order.symbol

    @staticmethod
    def _reservation_id(order: Order) -> str:
        suffix = 'cash' if order.is_buy else 'shares'
        return f"reservation-{order.order_id}-{suffix}"

    def _reservation_amount(self, order: Order) -> float:
        value = order.frozen_amount if order.is_buy else order.frozen_shares
        return float(value)

    def _reservation_key(self, order: Order) -> tuple:
        return order.order_id, self._reservation_asset(order)

    def _stamp_order_with_events(self, order: Order, *, sim_time=None) -> Order:
        """Atomically publish an order's deterministic identity and events."""
        with SettlementUnitOfWork(self, order) as submission:
            stamped = self._stamp_order_with_events_inner(
                order, sim_time=sim_time)
            submission.commit()
            return stamped

    def _stamp_order_with_events_inner(self, order: Order, *,
                                       sim_time=None) -> Order:
        """Assign simulation-local identity/time and publish submission facts."""

        object_key = id(order)
        existing_stamp = self._stamped_orders.get(object_key)
        if existing_stamp is not None and existing_stamp[0] is order:
            return order

        old_id = order.order_id
        new_id = self._next_order_id()
        active = self.account.orders.get(old_id) is order
        if active:
            del self.account.orders[old_id]
        order.order_id = new_id
        if active:
            self.account.orders[new_id] = order

        scope = self._order_creation_scope
        event_time = self._simulated_time(
            sim_time if sim_time is not None
            else (scope.sim_time if scope is not None else None))
        if scope is not None:
            order.created_phase = scope.phase
            close_window_applies = (
                order.order_type is OrderType.CLOSE
                and scope.close_eligible_from is not None
                and (
                    scope.close_symbols is None
                    or order.symbol in scope.close_symbols
                )
            )
            order.eligible_from = (
                scope.close_eligible_from
                if close_window_applies else scope.eligible_from
            )
            order.expire_date = (
                scope.close_expire_date
                if close_window_applies else scope.expire_date
            )
        else:
            order.created_phase = order.created_phase or self._market_phase
            order.expire_date = order.expire_date or self.current_date
        order.sim_created_at = event_time
        order.create_time = event_time
        order.update_time = event_time

        attributes = self._event_attributes(
            side=order.side.value,
            order_type=order.order_type.value,
            status=order.status.value,
            shares=order.shares,
            price=order.price,
            requested_shares=order.requested_shares,
            requested_amount=order.requested_amount,
            requested_percent=order.requested_percent,
            auto_resized=order.auto_resized,
            resized_from_shares=order.resized_from_shares,
            resized_to_shares=order.resized_to_shares,
            resize_reason=order.resize_reason,
            sizing_price=order.sizing_price,
            created_phase=order.created_phase,
            eligible_from=(
                order.eligible_from.isoformat(timespec='microseconds')
                if order.eligible_from is not None else None),
            expire_date=order.expire_date,
            note=order.note,
        )
        specs = [(
            ExecutionEventType.ORDER_CREATED,
            OrderEventPayload(
                order_id=order.order_id, symbol=order.symbol,
                attributes=attributes),
        )]
        terminal = order.status is OrderStatus.REJECTED
        specs.append((
            ExecutionEventType.ORDER_REJECTED if terminal
            else ExecutionEventType.ORDER_ACCEPTED,
            OrderEventPayload(
                order_id=order.order_id,
                symbol=order.symbol,
                reason=order.reject_reason or None,
                attributes=attributes,
            ),
        ))
        reserved = self._reservation_amount(order)
        if reserved > 0:
            specs.append((
                ExecutionEventType.RESERVE,
                ReservationPayload(
                    reservation_id=self._reservation_id(order),
                    order_id=order.order_id,
                    asset=self._reservation_asset(order),
                    amount=reserved,
                    attributes=self._event_attributes(
                        reservation_kind=(
                            'cash' if order.is_buy else 'shares')),
                ),
            ))
        phase = scope.phase if scope is not None else order.created_phase
        try:
            self._append_events(
                simulated_time=event_time, phase=phase, specs=specs)
        except BaseException:
            if active:
                self.account.orders.pop(new_id, None)
                order.order_id = old_id
                self.account.orders[old_id] = order
            raise
        self._stamped_orders[object_key] = (order, order.order_id)
        if terminal:
            self._terminal_event_orders.add(order.order_id)
        if reserved > 0:
            self._journal_reservations[self._reservation_key(order)] = reserved
        return order

    def set_date(self, date: str) -> None:
        """
        设置当前交易日期

        Args:
            date: 日期 YYYYMMDD
        """
        self.current_date = date

    def set_market_phase(self, phase: str) -> None:
        """
        设置交易时段

        Args:
            phase: 'before_open', 'trading', 'after_close'
        """
        self._market_phase = phase

    @property
    def current_created_phase(self) -> str:
        scope = self._order_creation_scope
        return scope.phase if scope is not None else ''

    @contextmanager
    def order_creation_scope(self, phase: str, sim_time,
                             eligible_from=None,
                             expire_date: str = None,
                             close_eligible_from=None,
                             close_expire_date: str = None,
                             close_symbols=None):
        """为一个策略回调内创建的订单绑定模拟时钟与首次可成交时点。"""
        previous = self._order_creation_scope
        sim_dt = self._coerce_sim_time(sim_time)
        eligible_dt = (
            self._coerce_sim_time(eligible_from)
            if eligible_from is not None else None
        )
        close_eligible_dt = (
            self._coerce_sim_time(close_eligible_from)
            if close_eligible_from is not None else None
        )
        if isinstance(close_symbols, str):
            raise TypeError("close_symbols must be an iterable of symbols")
        close_symbol_set = (
            frozenset(close_symbols) if close_symbols is not None else None
        )
        self._order_creation_scope = _OrderCreationScope(
            phase=str(phase), sim_time=sim_dt,
            eligible_from=eligible_dt, expire_date=expire_date,
            close_eligible_from=close_eligible_dt,
            close_expire_date=close_expire_date,
            close_symbols=close_symbol_set,
        )
        try:
            yield
        finally:
            self._order_creation_scope = previous

    @contextmanager
    def target_intent_scope(self, match_at):
        """Expose the exact same close timestamp later passed to matching."""

        previous = self._target_intent_scope
        self._target_intent_scope = _TargetIntentScope(
            match_at=self._coerce_sim_time(match_at))
        try:
            yield
        finally:
            self._target_intent_scope = previous

    def _next_target_intent_id(self) -> str:
        self._target_intent_id_counter += 1
        return f"target-intent-{self._target_intent_id_counter:012d}"

    def _next_target_batch_id(self) -> str:
        self._target_batch_id_counter += 1
        return f"target-batch-{self._target_batch_id_counter:012d}"

    def _submit_target_intents(self, specifications, *, batch_id=None):
        scope = self._order_creation_scope
        target_scope = self._target_intent_scope
        if scope is None or scope.phase not in {
                'on_after_open', 'on_before_close'}:
            raise ValueError(
                "close target intents may only be created in "
                "on_after_open/on_before_close")
        if target_scope is None:
            raise ValueError(
                "engine did not expose the exact close match timestamp")

        specs = tuple(specifications)
        if not specs:
            return ()
        if batch_id is not None and (
                type(batch_id) is not str or not batch_id.strip()):
            raise ValueError("batch_id must be a non-empty string")

        with SettlementUnitOfWork(self) as submission:
            resolved_batch_id = (
                batch_id.strip() if batch_id is not None
                else self._next_target_batch_id())
            created = []
            for symbol, target_weight, min_trade_amount, note in specs:
                self._cash_rule(symbol)
                intent = TargetIntent(
                    intent_id=self._next_target_intent_id(),
                    batch_id=resolved_batch_id,
                    symbol=symbol,
                    target_weight=target_weight,
                    created_at=scope.sim_time,
                    created_phase=scope.phase,
                    eligible_from=target_scope.match_at,
                    expire_date=target_scope.match_at.strftime('%Y%m%d'),
                    min_trade_amount=min_trade_amount,
                    note=note,
                )
                self._target_intents.append(intent)
                created.append(intent)
            submission.commit()
            return tuple(created)

    def submit_target_intent(self, symbol: str, target_weight: float, *,
                             min_trade_amount: float = 0.0,
                             note: str = '', batch_id: str = None
                             ) -> TargetIntent:
        """Record one unsized close target without reserving cash or shares."""

        return self._submit_target_intents((
            (symbol, target_weight, min_trade_amount, note),
        ), batch_id=batch_id)[0]

    def submit_target_intent_batch(self, targets: Dict[str, float], *,
                                   min_trade_amount: float = 0.0,
                                   note: str = '') -> tuple:
        """Record a rebalance call under one audit batch identifier."""

        if not isinstance(targets, dict):
            raise TypeError("targets must be a dict")
        return self._submit_target_intents((
            (symbol, targets[symbol], min_trade_amount, note)
            for symbol in sorted(targets)
        ))

    def target_intents_snapshot(self) -> tuple:
        """Immutable view of every decision-time target contract."""

        return tuple(self._target_intents)

    def target_achievements_snapshot(self) -> tuple:
        """Immutable view of terminal close-window target outcomes."""

        return tuple(self._target_achievements)

    @property
    def target_intents(self) -> tuple:
        return self.target_intents_snapshot()

    @property
    def target_achievements(self) -> tuple:
        return self.target_achievements_snapshot()

    @staticmethod
    def _coerce_sim_time(value) -> datetime:
        if type(value) is datetime:
            result = value
        else:
            try:
                import pandas as pd
                parsed = pd.to_datetime(value, errors='raise')
                if pd.isna(parsed) or not isinstance(parsed, pd.Timestamp):
                    raise ValueError
                result = parsed.to_pydatetime()
            except Exception as exc:
                raise ValueError(f"Invalid simulated time: {value!r}") from exc
        if type(result) is not datetime:
            raise ValueError(f"Invalid simulated time: {value!r}")
        if result.tzinfo is not None:
            raise ValueError("simulated time must be timezone-naive")
        if result.fold != 0:
            raise ValueError("simulated time fold must be 0")
        return result

    def _stamp_order(self, order: Order, *, sim_time=None) -> Order:
        return self._stamp_order_with_events(order, sim_time=sim_time)

    def _add_and_stamp_order(self, order: Order, *, sim_time=None) -> Order:
        """Publish a newly constructed, not-yet-accounted order atomically."""
        with SettlementUnitOfWork(self) as submission:
            self.account.add_order(order)
            self._stamp_order(order, sim_time=sim_time)
            submission.commit()
            return order

    def _ensure_order_stamped(self, order: Order, *, before_time=None) -> None:
        existing_stamp = self._stamped_orders.get(id(order))
        if existing_stamp is not None and existing_stamp[0] is order:
            return
        event_time = order.sim_created_at
        if event_time is None:
            if self.current_date:
                event_time = self._default_simulated_time()
            elif before_time is not None:
                fill_time = self._simulated_time(before_time)
                event_time = datetime.combine(fill_time.date(), time.min)
            else:
                event_time = self._default_simulated_time()
        self._stamp_order(order, sim_time=event_time)

    def _record_reserve(self, order: Order, amount: float, *, sim_time,
                        phase: str) -> None:
        amount = positive_float(amount, 'reservation amount')
        key = self._reservation_key(order)
        current = nonnegative_float(
            self._journal_reservations.get(key, 0.0),
            'journal reservation')
        self._append_events(
            simulated_time=sim_time,
            phase=phase,
            specs=((
                ExecutionEventType.RESERVE,
                ReservationPayload(
                    reservation_id=self._reservation_id(order),
                    order_id=order.order_id,
                    asset=self._reservation_asset(order),
                    amount=amount,
                    attributes=self._event_attributes(
                        reservation_kind=(
                            'cash' if order.is_buy else 'shares')),
                ),
            ),),
        )
        self._journal_reservations[key] = nonnegative_float(
            current + amount, 'journal reservation')

    def _terminal_specs(self, order: Order, event_type: ExecutionEventType,
                        reason: str = None):
        if order.order_id in self._terminal_event_orders:
            return (), None
        key = self._reservation_key(order)
        outstanding = nonnegative_float(
            self._journal_reservations.get(key, 0.0),
            'journal reservation')
        specs = []
        if outstanding > 0:
            if self._reservation_amount(order) > 1e-7:
                raise SettlementInvariantError(
                    "terminal order retained an economic reservation")
            specs.append((
                ExecutionEventType.RELEASE,
                ReservationPayload(
                    reservation_id=self._reservation_id(order),
                    order_id=order.order_id,
                    asset=self._reservation_asset(order),
                    amount=outstanding,
                    attributes=self._event_attributes(
                        release_reason=event_type.value),
                ),
            ))
        specs.append((
            event_type,
            OrderEventPayload(
                order_id=order.order_id,
                symbol=order.symbol,
                reason=reason or order.reject_reason or None,
                attributes=self._event_attributes(
                    side=order.side.value,
                    order_type=order.order_type.value,
                    status=order.status.value,
                    filled_shares=order.filled_shares,
                    filled_amount=order.filled_amount,
                ),
            ),
        ))
        return tuple(specs), key

    def _emit_terminal_order(self, order: Order, *, event_type=None,
                             reason: str = None, sim_time=None,
                             phase: str = None) -> None:
        if order.status is OrderStatus.FILLED:
            return
        if event_type is None:
            if order.status is OrderStatus.REJECTED:
                event_type = ExecutionEventType.ORDER_REJECTED
            elif order.status is OrderStatus.CANCELLED:
                event_type = ExecutionEventType.ORDER_CANCELED
            else:
                raise SettlementInvariantError(
                    "terminal event requires a terminal order status")
        specs, reservation_key = self._terminal_specs(
            order, event_type, reason)
        if not specs:
            return
        event_time = self._simulated_time(sim_time)
        order.update_time = event_time
        self._append_events(
            simulated_time=event_time,
            phase=phase or self.current_created_phase or self._market_phase,
            specs=specs,
        )
        if reservation_key is not None:
            self._journal_reservations.pop(reservation_key, None)
        self._terminal_event_orders.add(order.order_id)

    def _complete_order(self, order: Order, *, event_type=None,
                        reason: str = None, sim_time=None,
                        phase: str = None) -> None:
        self.account.complete_order(order)
        if order.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
            self._emit_terminal_order(
                order,
                event_type=event_type,
                reason=reason,
                sim_time=sim_time,
                phase=phase,
            )

    def _prepare_fill_events(self, order: Order, *, shares: int,
                             price: float, notional: float,
                             fees: FeeBreakdown, cash_amount: float,
                             direction: str, sim_time, phase: str,
                             reservation_release: float):
        fill_number = self._fill_id_counter + 1
        settlement_number = self._settlement_id_counter + 1
        fill_id = f"fill-{fill_number:012d}"
        settlement_id = f"settlement-{settlement_number:012d}"
        specs = [(
            ExecutionEventType.FILL,
            FillPayload(
                fill_id=fill_id,
                order_id=order.order_id,
                symbol=order.symbol,
                quantity=shares,
                price=price,
                amount=notional,
                fee=fees.total,
                attributes=self._event_attributes(
                    side=order.side.value,
                    order_type=order.order_type.value,
                    # Replay must use the rule decision made for this fill;
                    # querying a later RuleBook snapshot would make T+1 state
                    # non-deterministic.  Record the exact bool on both sides.
                    t0=self._is_t0(order.symbol),
                    commission=fees.commission,
                    stamp_duty=fees.stamp_duty,
                    transfer_fee=fees.transfer_fee,
                    trade_date=self.current_date,
                    note=order.note,
                    # Auction/deferred sizing can be decided after the
                    # immutable OrderCreated event.  Persist the final sizing
                    # facts on each fill so result projection never has to
                    # inspect the later-mutated Order object.
                    auto_resized=order.auto_resized,
                    resized_from_shares=order.resized_from_shares,
                    resized_to_shares=order.resized_to_shares,
                    resize_reason=order.resize_reason,
                    sizing_price=order.sizing_price,
                ),
            ),
        ), (
            ExecutionEventType.SETTLEMENT,
            SettlementPayload(
                settlement_id=settlement_id,
                account_id='cash-account',
                asset='CNY',
                amount=cash_amount,
                direction=direction,
                settlement_date=self._iso_current_date(sim_time),
                attributes=self._event_attributes(
                    fill_id=fill_id,
                    order_id=order.order_id,
                    gross_notional=notional,
                    fee=fees.total,
                ),
            ),
        )]
        if reservation_release > 0:
            specs.append((
                ExecutionEventType.RELEASE,
                ReservationPayload(
                    reservation_id=self._reservation_id(order),
                    order_id=order.order_id,
                    asset=self._reservation_asset(order),
                    amount=reservation_release,
                    attributes=self._event_attributes(
                        release_reason='fill_settlement', fill_id=fill_id),
                ),
            ))
        return self._prepare_events(
            simulated_time=sim_time, phase=phase, specs=specs)

    def _commit_fill_events(self, order: Order, events,
                            reservation_release: float) -> None:
        self._commit_prepared_events(events)
        self._fill_id_counter += 1
        self._settlement_id_counter += 1
        key = self._reservation_key(order)
        outstanding = nonnegative_float(
            self._journal_reservations.get(key, 0.0),
            'journal reservation')
        if reservation_release > outstanding + 1e-7:
            raise SettlementInvariantError(
                "fill released more than the journaled reservation")
        remaining = max(0.0, outstanding - reservation_release)
        if remaining > 0:
            self._journal_reservations[key] = remaining
        else:
            self._journal_reservations.pop(key, None)

    def _iso_current_date(self, sim_time=None) -> str:
        raw = str(self.current_date or '')
        if len(raw) == 8 and raw.isdigit():
            return datetime.strptime(raw, '%Y%m%d').date().isoformat()
        return self._simulated_time(sim_time).date().isoformat()

    @staticmethod
    def _matching_phase(order: Order, bar: BarData) -> str:
        if bar.liquidity_window == 'open' or order.order_type is OrderType.OPEN:
            return 'opening_match'
        if bar.liquidity_window == 'close' or order.order_type is OrderType.CLOSE:
            return 'closing_match'
        return 'continuous_match'

    @contextmanager
    def _matching_time_scope(self, match_at=None):
        """Expose an API-level auction timestamp to nested settlement code."""
        previous = self._active_match_time
        self._active_match_time = (
            self._coerce_sim_time(match_at)
            if match_at is not None else None
        )
        try:
            yield
        finally:
            self._active_match_time = previous

    def _event_time_for_bar(self, bar: BarData) -> datetime:
        candidate = (
            self._active_match_time
            if self._active_match_time is not None else bar.trade_time
        )
        return self._simulated_time(candidate)

    def _is_order_eligible(self, order: Order, match_at=None) -> bool:
        if order.eligible_from is None or match_at is None:
            return True
        return self._coerce_sim_time(match_at) >= order.eligible_from

    @property
    def is_before_market_open(self) -> bool:
        """是否处于盘前时段"""
        return self._market_phase == 'before_open'

    @property
    def is_trading(self) -> bool:
        """是否处于交易时段"""
        return self._market_phase == 'trading'

    def on_day_start(self, date: str, sim_time=None) -> None:
        """
        日开始处理

        Args:
            date: 交易日期
        """
        return self._on_day_start_atomic(date, sim_time=sim_time)

    def _on_day_start_atomic(self, date: str, *, sim_time=None) -> None:
        try:
            parsed_date = datetime.strptime(str(date), '%Y%m%d')
        except (TypeError, ValueError):
            raise ValueError("date must use YYYYMMDD") from None
        event_time = self._simulated_time(sim_time or parsed_date)

        with SettlementUnitOfWork(self) as day_start:
            self.current_date = date
            for pos in self.account.positions.values():
                pos.set_current_date(date)

            unlocked = self.account.settle_t1(date)
            unlock_specs = tuple(
                (
                    ExecutionEventType.T1_UNLOCK,
                    T1UnlockPayload(
                        account_id='cash-account',
                        symbol=symbol,
                        quantity=unlocked[symbol],
                        trade_date=parsed_date.date().isoformat(),
                    ),
                )
                for symbol in sorted(unlocked)
            )
            if unlock_specs:
                self._append_events(
                    simulated_time=event_time,
                    phase='day_start',
                    specs=unlock_specs,
                )

            expiring = [
                order for order in tuple(self.account.orders.values())
                if order.is_open
                and (order.expire_date or order.create_date)
                and (order.expire_date or order.create_date) < date
            ]
            for order in expiring:
                self._ensure_order_stamped(order, before_time=event_time)
            cancelled = self.account.cancel_expired_orders(date)
            for order in cancelled:
                self._remove_from_pending(order)
                self._emit_terminal_order(
                    order,
                    event_type=ExecutionEventType.ORDER_EXPIRED,
                    reason='DAY order expired',
                    sim_time=event_time,
                    phase='day_start',
                )
            day_start.commit()

        if cancelled:
            logger.info(f"Cancelled {len(cancelled)} expired orders")

    def update_prices(self, prices: Dict[str, float], *, sim_time=None,
                      phase: str = 'mark') -> None:
        """Atomically mark held positions and append exact mark facts."""
        event_time = self._simulated_time(sim_time)
        with SettlementUnitOfWork(self) as marking:
            self.account.update_prices(prices, update_time=event_time)
            specs = tuple(
                (
                    ExecutionEventType.MARK,
                    MarkPayload(symbol=symbol, price=float(prices[symbol])),
                )
                for symbol in sorted(prices)
                if symbol in self.account.positions
            )
            if specs:
                self._append_events(
                    simulated_time=event_time,
                    phase=phase,
                    specs=specs,
                )
            marking.commit()

    def apply_corporate_action(self, symbol: str, ratio: float, *,
                               effective_date: str, sim_time=None,
                               phase: str = 'day_start') -> bool:
        """Apply and journal one split action as a single transaction.

        The position layer rejects fractional entitlements because this engine
        has no explicit cash-in-lieu ledger.
        """
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        compact_date = str(effective_date).replace('-', '')
        try:
            parsed_date = datetime.strptime(compact_date, '%Y%m%d')
        except (TypeError, ValueError):
            raise ValueError(
                "effective_date must use YYYYMMDD or YYYY-MM-DD") from None
        event_time = self._simulated_time(sim_time or parsed_date)
        open_orders = sorted(
            (
                order for order in self.account.get_open_orders()
                if order.symbol == symbol
            ),
            key=lambda order: order.order_id,
        )
        position = self.account.positions.get(symbol)
        if position is None and not open_orders:
            return False

        with SettlementUnitOfWork(self) as action:
            for order in open_orders:
                self._cancel_active_order(
                    order,
                    reason='corporate_action_price_space_changed',
                    sim_time=event_time,
                    phase=phase,
                )

            old_shares = position.shares if position is not None else 0
            old_price = position.current_price if position is not None else 0.0
            if position is not None:
                position.apply_split_ratio(ratio, update_time=event_time)

            numeric_ratio = positive_float(ratio, 'corporate action ratio')
            self._corporate_action_id_counter += 1
            action_id = (
                f"corporate-action-"
                f"{self._corporate_action_id_counter:012d}"
            )
            self._append_events(
                simulated_time=event_time,
                phase=phase,
                specs=((
                    ExecutionEventType.CORPORATE_ACTION,
                    CorporateActionPayload(
                        action_id=action_id,
                        symbol=symbol,
                        action_type='split_ratio',
                        effective_date=parsed_date.date().isoformat(),
                        attributes=self._event_attributes(
                            ratio=numeric_ratio,
                            old_shares=old_shares,
                            new_shares=(
                                position.shares if position is not None else 0),
                            old_price=old_price,
                            new_price=(
                                position.current_price
                                if position is not None else 0.0),
                            fractional_policy='reject',
                        ),
                    ),
                ),),
            )
            action.commit()
        return True

    def apply_adjustment_factor_total_return(
            self, symbol: str, ratio: float, *, effective_date: str,
            sim_time=None, phase: str = 'day_start') -> bool:
        """Apply a bare factor as reinvestment with integer cash-in-lieu.

        This is deliberately separate from :meth:`apply_corporate_action`:
        authoritative split terms retain strict fractional rejection, while a
        data-vendor factor is modeled as immediate total-return reinvestment
        with only the fractional-share remainder credited as cash.
        """
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol must be a non-empty string")
        numeric_ratio = positive_float(ratio, 'adjustment factor ratio')
        compact_date = str(effective_date).replace('-', '')
        try:
            parsed_date = datetime.strptime(compact_date, '%Y%m%d')
        except (TypeError, ValueError):
            raise ValueError(
                "effective_date must use YYYYMMDD or YYYY-MM-DD") from None
        event_time = self._simulated_time(sim_time or parsed_date)
        open_orders = sorted(
            (
                order for order in self.account.get_open_orders()
                if order.symbol == symbol
            ),
            key=lambda order: order.order_id,
        )
        position = self.account.positions.get(symbol)
        if position is None and not open_orders:
            return False

        with SettlementUnitOfWork(self) as action:
            for order in open_orders:
                self._cancel_active_order(
                    order,
                    reason='corporate_action_price_space_changed',
                    sim_time=event_time,
                    phase=phase,
                )

            old_shares = position.shares if position is not None else 0
            old_price = position.current_price if position is not None else 0.0
            cash_amount = 0.0
            if position is not None:
                cash_amount = position.apply_adjustment_factor_total_return(
                    numeric_ratio, update_time=event_time)
                if cash_amount > 0:
                    self.account.add_cash(cash_amount)

            self._corporate_action_id_counter += 1
            action_id = (
                f"corporate-action-"
                f"{self._corporate_action_id_counter:012d}"
            )
            self._append_events(
                simulated_time=event_time,
                phase=phase,
                specs=((
                    ExecutionEventType.CORPORATE_ACTION,
                    CorporateActionPayload(
                        action_id=action_id,
                        symbol=symbol,
                        action_type='adjustment_factor_total_return',
                        effective_date=parsed_date.date().isoformat(),
                        attributes=self._event_attributes(
                            ratio=numeric_ratio,
                            old_shares=old_shares,
                            new_shares=(
                                position.shares if position is not None else 0),
                            old_price=old_price,
                            new_price=(
                                position.current_price
                                if position is not None else 0.0),
                            cash_amount=cash_amount,
                            fractional_policy='cash_in_lieu',
                            valuation_model='immediate_total_return_reinvestment',
                        ),
                    ),
                ),),
            )
            action.commit()
        return True

    def on_day_end(self, sim_time=None) -> None:
        """日结束处理"""
        # 只撤销有效期已到的 DAY 单；on_day 后验回调创建、T+1 才生效的单
        # 必须跨夜保留。
        self.cancel_day_end_orders(sim_time=sim_time)
        self._pending_open_orders = [
            order for order in self._pending_open_orders if order.is_open
        ]
        self._pending_close_orders = [
            order for order in self._pending_close_orders if order.is_open
        ]

    # ==================== 成交额限制 ====================

    def reset_bar_used_amount(self) -> None:
        """
        重置K线已用成交额

        每根新K线开始时调用，清空已用成交额记录
        """
        self._bar_used_amount.clear()

    def _bar_liquidity_cap(self, bar: Union[BarData, float]) -> float:
        """Resolve the final executable-notional cap for one matching bar.

        ``BarData.liquidity_cap_amount`` is already the final auction cap and
        is never multiplied by ``liquidity_cap_ratio``.  A numeric argument is
        retained for the legacy private helper/tests and represents an ordinary
        bar amount, so the configured participation ratio still applies.
        """
        if isinstance(bar, BarData):
            if bar.liquidity_cap_amount is not None:
                return positive_float(
                    bar.liquidity_cap_amount, 'liquidity_cap_amount')
            if bar.require_explicit_liquidity_cap:
                window = bar.liquidity_window or "auction"
                cli_hint = ""
                if window in ("open", "close"):
                    cli_hint = (
                        f"; CLI users must set --daily-{window}-cap-yuan or "
                        f"--daily-{window}-previous-day-ratio"
                    )
                raise AuctionLiquidityUnavailable(
                    f"{bar.symbol} {window} requires an explicit liquidity cap"
                    f"{cli_hint}")
            amount = bar.amount
        else:
            amount = bar
        amount = nonnegative_float(amount, 'bar amount')
        return nonnegative_float(
            amount * self.liquidity_cap_ratio, 'bar liquidity cap')

    def _get_available_amount(
            self, symbol: str, bar: Union[BarData, float]) -> float:
        """
        获取当前K线剩余可用成交额

        Args:
            symbol: 股票代码
            bar: BarData，或兼容旧私有调用的普通K线成交额（元）

        Returns:
            剩余可用成交额
        """
        if isinstance(bar, BarData) and bar.symbol != symbol:
            raise SettlementInvariantError(
                "liquidity bar symbol does not match the order symbol")
        max_amount = self._bar_liquidity_cap(bar)
        used = nonnegative_float(
            self._bar_used_amount.get(symbol, 0.0), 'bar used amount')
        if used > max_amount + 1e-7:
            raise SettlementInvariantError(
                "bar used amount exceeds the configured liquidity cap")
        return max(0.0, max_amount - used)

    def _consume_amount(self, symbol: str, amount: float) -> None:
        """
        消耗成交额

        Args:
            symbol: 股票代码
            amount: 本次成交金额
        """
        amount = nonnegative_float(amount, 'consumed bar amount')
        used = nonnegative_float(
            self._bar_used_amount.get(symbol, 0.0), 'bar used amount')
        self._bar_used_amount[symbol] = nonnegative_float(
            used + amount, 'bar used amount')

    @staticmethod
    def _amounts_close(left: float, right: float) -> bool:
        # Settlement amounts are generated from the same validated operands.
        # A magnitude-relative tolerance would silently permit economically
        # material drift in a large account (for example, one yuan at 1e12).
        return abs(left - right) <= 1e-7

    def _validate_execution_envelope(self, order: Order,
                                     bar: BarData) -> tuple:
        """Validate active-order identity and current bar accounting."""
        if self.account.orders.get(order.order_id) is not order:
            raise SettlementInvariantError(
                "settlement order must be the active account order object")
        if not order.is_open:
            raise SettlementInvariantError("settlement order must be open")
        if order.symbol != bar.symbol:
            raise SettlementInvariantError(
                "settlement order symbol does not match bar symbol")
        # A manually inserted Order must not bypass the public order-entry
        # guard and fall through to ordinary-stock execution rules.
        self._cash_rule(order.symbol)
        self._require_bar_pre_close(order.symbol, bar)
        self.account._validate_cash_state()
        reservation_baselines = self._reservation_baselines(order.symbol)
        max_amount = self._bar_liquidity_cap(bar)
        used = nonnegative_float(
            self._bar_used_amount.get(order.symbol, 0.0), 'bar used amount')
        if used > max_amount + 1e-7:
            raise SettlementInvariantError(
                "bar used amount exceeds the configured liquidity cap")
        return reservation_baselines

    def _reservation_baselines(self, symbol: str) -> tuple:
        """Return unmanaged cash/share reservations after validating coverage.

        The legacy Account API permits callers to freeze assets without an
        Order.  Such positive unmanaged balances are retained for compatibility,
        while a negative baseline proves active orders collectively promise
        more than the account/position actually reserved.
        """
        reserved_cash = 0.0
        reserved_shares = 0
        for active_order in self.account.orders.values():
            if not active_order.is_open:
                continue
            if active_order.is_buy:
                reserved_cash = nonnegative_float(
                    reserved_cash + nonnegative_float(
                        active_order.frozen_amount,
                        'active buy order frozen amount'),
                    'aggregate buy order frozen amount',
                )
            elif active_order.symbol == symbol:
                reserved_shares += nonnegative_int(
                    active_order.frozen_shares,
                    'active sell order frozen shares',
                )

        account_frozen = nonnegative_float(
            self.account.frozen_cash, 'account frozen_cash')
        cash_baseline = finite_float(
            account_frozen - reserved_cash, 'unmanaged frozen_cash baseline')
        cash_tolerance = 1e-7
        if cash_baseline < -cash_tolerance:
            raise SettlementInvariantError(
                "aggregate active BUY reservations exceed account frozen_cash")
        if cash_baseline < 0:
            cash_baseline = 0.0

        position = self.account.positions.get(symbol)
        position_frozen = (
            nonnegative_int(position.frozen_shares, 'position frozen shares')
            if position is not None else 0
        )
        shares_baseline = position_frozen - reserved_shares
        if shares_baseline < 0:
            raise SettlementInvariantError(
                "aggregate active SELL reservations exceed position frozen shares")
        return cash_baseline, shares_baseline

    def _assert_reservation_baselines(self, symbol: str,
                                      expected: tuple) -> None:
        """Ensure one fill changes account and order reservations in lockstep."""
        actual_cash, actual_shares = self._reservation_baselines(symbol)
        expected_cash, expected_shares = expected
        if not self._amounts_close(actual_cash, expected_cash):
            raise SettlementInvariantError(
                "unmanaged frozen_cash baseline changed during settlement")
        if actual_shares != expected_shares:
            raise SettlementInvariantError(
                "unmanaged frozen-share baseline changed during settlement")

    def _calculate_fill_fees(self, order: Order, shares: int,
                             price: float) -> FeeBreakdown:
        notional = positive_product(shares, price, 'fill notional')
        prior_state = self._order_fee_states.get(order.order_id)
        side = FeeSide.BUY if order.is_buy else FeeSide.SELL
        stamp_rate = self.account.resolve_stamp_rate(
            order.symbol, self.current_date)
        return self.fee_engine.calculate_fill(
            notional,
            parent_order_id=order.order_id,
            side=side,
            prior_state=prior_state,
            stamp_duty_rate=stamp_rate,
        )

    def _validate_fee_continuity(self, order: Order,
                                 fees: FeeBreakdown) -> None:
        prior_state = self._order_fee_states.get(order.order_id)
        if prior_state is None:
            if order.fee_fill_count != 0 or any((
                    order.commission_paid,
                    order.stamp_duty_paid,
                    order.transfer_fee_paid,
                    order.total_fees,
            )):
                raise SettlementInvariantError(
                    "order fee totals exist without parent-order fee state")
            if order.filled_shares != 0 or order.filled_amount != 0:
                raise SettlementInvariantError(
                    "filled order is missing parent-order fee state")
        else:
            if prior_state.fill_count != order.fee_fill_count:
                raise SettlementInvariantError(
                    "order fee fill count does not match fee state")
            if not self._amounts_close(
                    prior_state.cumulative_notional, order.filled_amount):
                raise SettlementInvariantError(
                    "order filled amount does not match fee state")
            if not self._amounts_close(
                    prior_state.commission_charged,
                    order.commission_paid):
                raise SettlementInvariantError(
                    "order commission does not match fee state")
        if fees.next_state.fill_count != order.fee_fill_count + 1:
            raise SettlementInvariantError(
                "next fee state does not advance exactly one fill")

    def _validate_fill_preconditions(self, order: Order, bar: BarData,
                                     shares: int, price: float,
                                     fees: FeeBreakdown) -> None:
        """Fail before mutation when a fill cannot settle exactly."""
        self._validate_execution_envelope(order, bar)
        shares = positive_int(shares, 'settlement shares')
        price = positive_float(price, 'settlement price')
        if shares > order.unfilled_shares:
            raise SettlementInvariantError(
                "settlement shares exceed order unfilled shares")
        notional = positive_product(shares, price, 'fill notional')
        if not self._amounts_close(notional, fees.notional):
            raise SettlementInvariantError(
                "fee notional does not match settlement notional")
        self._validate_fee_continuity(order, fees)

        max_amount = self._bar_liquidity_cap(bar)
        used = nonnegative_float(
            self._bar_used_amount.get(order.symbol, 0.0), 'bar used amount')
        next_used = nonnegative_float(
            used + notional, 'next bar used amount')
        if next_used > max_amount + 1e-7:
            raise SettlementInvariantError(
                "settlement would exceed the configured bar liquidity cap")

        if order.is_buy:
            order_frozen = nonnegative_float(
                order.frozen_amount, 'order frozen amount')
            account_frozen = nonnegative_float(
                self.account.frozen_cash, 'account frozen_cash')
            total_cost = positive_float(
                notional + fees.total, 'settlement total cost')
            if order_frozen + 1e-7 < total_cost:
                raise SettlementInvariantError(
                    "buy order reservation is insufficient for settlement")
            if account_frozen + 1e-7 < order_frozen:
                raise SettlementInvariantError(
                    "account frozen_cash is below the order reservation")
            return

        position = self.account.positions.get(order.symbol)
        if position is None:
            raise SettlementInvariantError(
                "sell settlement requires an existing position")
        # available_shares validates the complete Position state.
        position.available_shares
        frozen_order = nonnegative_int(
            order.frozen_shares, 'order frozen shares')
        frozen_position = nonnegative_int(
            position.frozen_shares, 'position frozen shares')
        if frozen_order != order.unfilled_shares:
            raise SettlementInvariantError(
                "sell order reservation must equal its unfilled shares")
        if frozen_order < shares:
            raise SettlementInvariantError(
                "sell order reservation is insufficient for settlement")
        if frozen_position < frozen_order:
            raise SettlementInvariantError(
                "position frozen shares are below the order reservation")

    def _capture_fill_state(self, order: Order) -> Dict[str, Any]:
        """Capture the numeric/lifecycle state used by fill postconditions."""
        position = self.account.positions.get(order.symbol)
        if position is None:
            position_state = {
                'position_exists': False,
                'position_shares': 0,
                'position_frozen_shares': 0,
                'position_available_shares': 0,
                'position_cost_basis': 0.0,
                'position_avg_cost': 0.0,
            }
        else:
            position_state = {
                'position_exists': True,
                'position_shares': nonnegative_int(
                    position.shares, 'position shares'),
                'position_frozen_shares': nonnegative_int(
                    position.frozen_shares, 'position frozen shares'),
                'position_available_shares': nonnegative_int(
                    position.available_shares, 'position available shares'),
                'position_cost_basis': nonnegative_float(
                    position.cost_basis, 'position cost_basis'),
                'position_avg_cost': nonnegative_float(
                    position.avg_cost, 'position avg_cost'),
            }
        return {
            'cash': finite_float(self.account.cash, 'cash'),
            'frozen_cash': nonnegative_float(
                self.account.frozen_cash, 'frozen_cash'),
            'order_shares': nonnegative_int(
                order.shares, 'order shares'),
            'order_filled_shares': nonnegative_int(
                order.filled_shares, 'order filled shares'),
            'order_filled_amount': nonnegative_float(
                order.filled_amount, 'order filled amount'),
            'order_frozen_amount': nonnegative_float(
                order.frozen_amount, 'order frozen amount'),
            'order_frozen_shares': nonnegative_int(
                order.frozen_shares, 'order frozen shares'),
            'commission_paid': nonnegative_float(
                order.commission_paid, 'order commission_paid'),
            'stamp_duty_paid': nonnegative_float(
                order.stamp_duty_paid, 'order stamp_duty_paid'),
            'transfer_fee_paid': nonnegative_float(
                order.transfer_fee_paid, 'order transfer_fee_paid'),
            'total_fees': nonnegative_float(
                order.total_fees, 'order total_fees'),
            'fee_fill_count': nonnegative_int(
                order.fee_fill_count, 'order fee_fill_count'),
            'history_length': len(self.account.order_history),
            **position_state,
        }

    def _assert_fill_order_postconditions(
            self, order: Order, before: Dict[str, Any], shares: int,
            notional: float, fees: FeeBreakdown) -> bool:
        """Reconcile Order, fee accumulator, and active/history membership."""
        expected_filled_shares = before['order_filled_shares'] + shares
        expected_filled_amount = finite_float(
            before['order_filled_amount'] + notional,
            'expected order filled amount')
        expected_done = expected_filled_shares == before['order_shares']
        expected_status = (
            OrderStatus.FILLED if expected_done else OrderStatus.PARTIAL)
        if order.shares != before['order_shares']:
            raise SettlementInvariantError(
                "order requested-shares postcondition failed")
        if order.filled_shares != expected_filled_shares:
            raise SettlementInvariantError(
                "order filled-shares postcondition failed")
        if not self._amounts_close(
                order.filled_amount, expected_filled_amount):
            raise SettlementInvariantError(
                "order filled-amount postcondition failed")
        expected_avg = positive_float(
            expected_filled_amount / expected_filled_shares,
            'expected order average price')
        if not self._amounts_close(order.avg_price, expected_avg):
            raise SettlementInvariantError(
                "order average-price postcondition failed")
        if order.status is not expected_status:
            raise SettlementInvariantError(
                "order status postcondition failed")

        expected_fee_values = {
            'commission_paid': round(
                before['commission_paid'] + fees.commission, 2),
            'stamp_duty_paid': round(
                before['stamp_duty_paid'] + fees.stamp_duty, 2),
            'transfer_fee_paid': round(
                before['transfer_fee_paid'] + fees.transfer_fee, 2),
            'total_fees': round(before['total_fees'] + fees.total, 2),
        }
        for field, expected in expected_fee_values.items():
            if not self._amounts_close(getattr(order, field), expected):
                raise SettlementInvariantError(
                    f"order {field} postcondition failed")
        if order.fee_fill_count != before['fee_fill_count'] + 1:
            raise SettlementInvariantError(
                "order fee-fill-count postcondition failed")
        if self._order_fee_states.get(order.order_id) != fees.next_state:
            raise SettlementInvariantError(
                "parent-order fee-state postcondition failed")

        history = self.account.order_history
        if expected_done:
            if order.order_id in self.account.orders:
                raise SettlementInvariantError(
                    "filled order remained in the active order map")
            if (len(history) != before['history_length'] + 1
                    or history[-1] is not order):
                raise SettlementInvariantError(
                    "filled order history-membership postcondition failed")
        else:
            if self.account.orders.get(order.order_id) is not order:
                raise SettlementInvariantError(
                    "partial order lost active-map membership")
            if len(history) != before['history_length']:
                raise SettlementInvariantError(
                    "partial order history-membership postcondition failed")
        return expected_done

    def _assert_buy_fill_postconditions(
            self, order: Order, before: Dict[str, Any], shares: int,
            notional: float, cost: float, fees: FeeBreakdown,
            *, t0: bool) -> None:
        """Reconcile the complete post-state of one BUY fill."""
        expected_done = self._assert_fill_order_postconditions(
            order, before, shares, notional, fees)
        remaining_reservation = finite_float(
            before['order_frozen_amount'] - cost,
            'expected remaining order reservation')
        if remaining_reservation < -1e-7:
            raise SettlementInvariantError(
                "buy postcondition has a negative reservation")
        remaining_reservation = max(0.0, remaining_reservation)
        expected_order_frozen = 0.0 if expected_done else remaining_reservation
        if not self._amounts_close(
                order.frozen_amount, expected_order_frozen):
            raise SettlementInvariantError(
                "buy order reservation postcondition failed")

        expected_cash = before['cash']
        expected_frozen_cash = before['frozen_cash'] - cost
        if expected_done:
            expected_cash += remaining_reservation
            expected_frozen_cash -= remaining_reservation
        if not self._amounts_close(self.account.cash, expected_cash):
            raise SettlementInvariantError(
                "buy cash postcondition failed")
        if not self._amounts_close(
                self.account.frozen_cash, expected_frozen_cash):
            raise SettlementInvariantError(
                "buy frozen-cash postcondition failed")

        position = self.account.positions.get(order.symbol)
        if position is None:
            raise SettlementInvariantError(
                "buy position postcondition requires a position")
        expected_shares = before['position_shares'] + shares
        expected_cost_basis = positive_float(
            before['position_cost_basis'] + cost,
            'expected buy position cost_basis')
        expected_avg_cost = positive_float(
            expected_cost_basis / expected_shares,
            'expected buy position avg_cost')
        expected_available = (
            before['position_available_shares'] + shares
            if t0 else before['position_available_shares'])
        if position.shares != expected_shares:
            raise SettlementInvariantError(
                "buy position quantity postcondition failed")
        if position.available_shares != expected_available:
            raise SettlementInvariantError(
                "buy position available-shares postcondition failed")
        if not self._amounts_close(
                position.cost_basis, expected_cost_basis):
            raise SettlementInvariantError(
                "buy position cost-basis postcondition failed")
        if not self._amounts_close(position.avg_cost, expected_avg_cost):
            raise SettlementInvariantError(
                "buy position average-cost postcondition failed")

    def _assert_sell_fill_postconditions(
            self, order: Order, before: Dict[str, Any], shares: int,
            notional: float, proceeds: float, fees: FeeBreakdown) -> None:
        """Reconcile the complete post-state of one SELL fill."""
        self._assert_fill_order_postconditions(
            order, before, shares, notional, fees)
        expected_order_frozen = before['order_frozen_shares'] - shares
        if order.frozen_shares != expected_order_frozen:
            raise SettlementInvariantError(
                "sell order reservation postcondition failed")
        if not self._amounts_close(
                self.account.cash, before['cash'] + proceeds):
            raise SettlementInvariantError(
                "sell cash postcondition failed")
        if not self._amounts_close(
                self.account.frozen_cash, before['frozen_cash']):
            raise SettlementInvariantError(
                "sell frozen-cash postcondition failed")

        expected_shares = before['position_shares'] - shares
        if expected_shares == 0:
            if order.symbol in self.account.positions:
                raise SettlementInvariantError(
                    "fully sold position remained in the position map")
            return

        position = self.account.positions.get(order.symbol)
        if position is None:
            raise SettlementInvariantError(
                "partial sell removed the remaining position")
        expected_frozen = before['position_frozen_shares'] - shares
        expected_cost_basis = nonnegative_float(
            before['position_cost_basis']
            - shares * before['position_avg_cost'],
            'expected sell position cost_basis')
        expected_avg_cost = positive_float(
            expected_cost_basis / expected_shares,
            'expected sell position avg_cost')
        if position.shares != expected_shares:
            raise SettlementInvariantError(
                "sell position quantity postcondition failed")
        if position.frozen_shares != expected_frozen:
            raise SettlementInvariantError(
                "sell position frozen-shares postcondition failed")
        if position.available_shares != before['position_available_shares']:
            raise SettlementInvariantError(
                "sell position available-shares postcondition failed")
        if not self._amounts_close(
                position.cost_basis, expected_cost_basis):
            raise SettlementInvariantError(
                "sell position cost-basis postcondition failed")
        if not self._amounts_close(position.avg_cost, expected_avg_cost):
            raise SettlementInvariantError(
                "sell position average-cost postcondition failed")

    def cancel_day_end_orders(self, sim_time=None) -> List[Order]:
        """
        收盘撤销所有未完成订单

        Returns:
            被撤销的订单列表
        """
        cancelled = []
        event_time = self._simulated_time(sim_time)
        for order in list(self.account.orders.values()):
            if order.is_open or order.status == OrderStatus.PARTIAL:
                expiry = order.expire_date or order.create_date
                if expiry and self.current_date and expiry > self.current_date:
                    continue
                with SettlementUnitOfWork(self, order) as lifecycle:
                    # 买单：计算剩余冻结资金并释放
                    if order.is_buy and order.frozen_amount > 0:
                        self.account.unfreeze_cash(
                            order.frozen_amount, strict=True)
                        order.frozen_amount = 0

                    # 卖单：释放冻结股票
                    if order.is_sell and order.frozen_shares > 0:
                        self.account.unfreeze_shares(
                            order.symbol, order.frozen_shares, strict=True)
                        order.frozen_shares = 0

                    if (order.is_buy and order.filled_shares == 0
                            and 'frozen_insufficient' in (order.resize_reason or '')):
                        order.reject(
                            f"Frozen insufficient for min lot: 冻结资金整日不足"
                            f"最小申报增量（含最低佣金侵蚀），日内未能成交"
                        )
                    else:
                        order.cancel()
                    self._remove_from_pending(order)
                    self._complete_order(
                        order, sim_time=event_time,
                        phase='day_end')
                    lifecycle.commit()
                cancelled.append(order)

        if cancelled:
            logger.info(f"Day end: cancelled {len(cancelled)} unfilled orders")

        return cancelled

    # ==================== 下单接口 ====================

    def buy_at_open(self, symbol: str, amount: float = None,
                    shares: int = None, percent: float = None,
                    note: str = '') -> Order:
        """
        开盘价买入

        注意: 可在盘前调用；日线 on_after_open/on_day 创建的订单顺延到
        下一交易日开盘窗口。

        Args:
            symbol: 股票代码
            amount: 买入金额
            shares: 买入股数
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）

        Returns:
            Order 对象
        """
        # 检查是否在盘前时段
        if (not self.is_before_market_open
                and self.current_created_phase not in {
                    'on_after_open', 'on_day'}):
            order = Order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=OrderType.OPEN,
                shares=0,
                price=None,
                create_date=self.current_date,
                note=note
            )
            order.reject(
                "开盘单只能在盘前或日线 on_after_open/on_day 下单")
            return self._add_and_stamp_order(order)

        order = self._create_buy_order(
            symbol, OrderType.OPEN, None, amount, shares, percent, note
        )
        self._stamp_order(order)
        if order.status != OrderStatus.REJECTED:
            self._pending_open_orders.append(order)
        return order

    def buy_at_market(self, symbol: str, amount: float = None,
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
        return self._stamp_order(self._create_buy_order(
            symbol, OrderType.MARKET, None, amount, shares, percent, note, current_price
        ))

    def buy_at_price(self, symbol: str, price: float,
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
        return self._stamp_order(self._create_buy_order(
            symbol, OrderType.LIMIT, price, amount, shares, percent, note
        ))

    def sell_at_open(self, symbol: str, shares: int = None,
                     percent: float = None, note: str = '') -> Order:
        """
        开盘价卖出

        注意: 可在盘前调用；日线 on_after_open/on_day 创建的订单顺延到
        下一交易日开盘窗口。

        Args:
            symbol: 股票代码
            shares: 卖出股数
            percent: 占可卖持仓比例

        Returns:
            Order 对象
        """
        # 检查是否在盘前时段
        if (not self.is_before_market_open
                and self.current_created_phase not in {
                    'on_after_open', 'on_day'}):
            order = Order(
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=OrderType.OPEN,
                shares=0,
                price=None,
                create_date=self.current_date,
                note=note
            )
            order.reject(
                "开盘单只能在盘前或日线 on_after_open/on_day 下单")
            return self._add_and_stamp_order(order)

        order = self._create_sell_order(
            symbol, OrderType.OPEN, None, shares, percent, note
        )
        self._stamp_order(order)
        if order.status != OrderStatus.REJECTED:
            self._pending_open_orders.append(order)
        return order

    def sell_at_market(self, symbol: str, shares: int = None,
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
        return self._stamp_order(self._create_sell_order(
            symbol, OrderType.MARKET, None, shares, percent, note
        ))

    def sell_at_price(self, symbol: str, price: float,
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
        return self._stamp_order(self._create_sell_order(
            symbol, OrderType.LIMIT, price, shares, percent, note
        ))

    # ==================== 收盘价交易接口 ====================

    def buy_at_close(self, symbol: str, amount: float = None,
                     shares: int = None, percent: float = None,
                     note: str = '', current_price: float = None) -> Order:
        """
        收盘价买入

        注意: 只能在交易时段 (on_day) 调用

        Args:
            symbol: 股票代码
            amount: 买入金额
            shares: 买入股数
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）

        Returns:
            Order 对象
        """
        # 检查是否在交易时段
        if not self.is_trading:
            order = Order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=OrderType.CLOSE,
                shares=0,
                price=None,
                create_date=self.current_date,
                note=note
            )
            order.reject("收盘单只能在交易时段 (on_day) 下单")
            return self._add_and_stamp_order(order)

        order = self._create_buy_order(
            symbol, OrderType.CLOSE, None, amount, shares, percent, note, current_price
        )
        self._stamp_order(order)
        if order.status != OrderStatus.REJECTED:
            self._pending_close_orders.append(order)
        return order

    def sell_at_close(self, symbol: str, shares: int = None,
                      percent: float = None, note: str = '') -> Order:
        """
        收盘价卖出

        注意: 只能在交易时段 (on_day) 调用

        Args:
            symbol: 股票代码
            shares: 卖出股数
            percent: 占可卖持仓比例

        Returns:
            Order 对象
        """
        # 检查是否在交易时段
        if not self.is_trading:
            order = Order(
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=OrderType.CLOSE,
                shares=0,
                price=None,
                create_date=self.current_date,
                note=note
            )
            order.reject("收盘单只能在交易时段 (on_day) 下单")
            return self._add_and_stamp_order(order)

        order = self._create_sell_order(
            symbol, OrderType.CLOSE, None, shares, percent, note
        )
        self._stamp_order(order)
        if order.status != OrderStatus.REJECTED:
            self._pending_close_orders.append(order)
        return order

    # ==================== 止损止盈接口 ====================

    def sell_stop_loss(self, symbol: str, price: float,
                       shares: int = None, percent: float = None,
                       note: str = '') -> Order:
        """
        止损卖出 (价格下穿触发)

        当价格 <= stop_price 时触发卖出

        Args:
            symbol: 股票代码
            price: 止损价格
            shares: 卖出股数
            percent: 占可卖持仓比例

        Returns:
            Order 对象
        """
        return self._stamp_order(self._create_sell_order(
            symbol, OrderType.STOP, price, shares, percent, note or '止损单'
        ))

    # 兼容旧名称
    sell_stop = sell_stop_loss

    def sell_stop_profit(self, symbol: str, price: float,
                         shares: int = None, percent: float = None,
                         note: str = '') -> Order:
        """
        止盈卖出 (价格上穿触发)

        当价格 >= profit_price 时触发卖出

        Args:
            symbol: 股票代码
            price: 止盈价格
            shares: 卖出股数
            percent: 占可卖持仓比例

        Returns:
            Order 对象
        """
        return self._stamp_order(self._create_sell_order(
            symbol, OrderType.STOP_PROFIT, price, shares, percent, note or '止盈单'
        ))

    def buy_stop(self, symbol: str, price: float,
                 shares: int = None, amount: float = None,
                 percent: float = None, note: str = '',
                 current_price: float = None) -> Order:
        """
        突破买入 (价格上穿触发)

        当价格 >= trigger_price 时触发买入 (用于突破策略)

        Args:
            symbol: 股票代码
            price: 触发价格
            shares: 买入股数
            amount: 买入金额
            percent: 占可用现金比例（执行时点结算；竞价单含当日竞价卖出回款；默认 open_buy_sizing='limit_up' 下按涨停价折算，实际金额约为 现金×percent÷(1+涨跌幅)）

        Returns:
            Order 对象
        """
        return self._stamp_order(self._create_buy_order(
            symbol, OrderType.STOP, price, amount, shares, percent,
            note or '突破买入',
            current_price if current_price is not None else price
        ))

    # ==================== 取消订单接口 ====================

    def _remove_from_pending(self, order: Order) -> None:
        """把订单从竞价待执行队列中移除。

        历史P0：撤单只把订单移出 account.orders，但仍留在
        _pending_open_orders/_pending_close_orders 里；竞价执行点上
        已撤销订单被当作 deferred 单重新定量并冻结资金，随后因 is_done
        直接返回不解冻——冻结资金永久泄漏（实测一单冻死 ~50% 本金）。
        """
        if order in self._pending_open_orders:
            self._pending_open_orders.remove(order)
        if order in self._pending_close_orders:
            self._pending_close_orders.remove(order)

    def _cancel_active_order(self, order: Order, *, reason: str = None,
                             event_type=ExecutionEventType.ORDER_CANCELED,
                             sim_time=None, phase: str = None) -> None:
        self._ensure_order_stamped(order, before_time=sim_time)
        with SettlementUnitOfWork(self, order) as lifecycle:
            order.cancel()
            if reason:
                order.reject_reason = reason
            if order.is_buy and order.frozen_amount > 0:
                self.account.unfreeze_cash(order.frozen_amount, strict=True)
                order.frozen_amount = 0
            if order.is_sell and order.frozen_shares > 0:
                self.account.unfreeze_shares(
                    order.symbol, order.frozen_shares, strict=True)
                order.frozen_shares = 0
            self._remove_from_pending(order)
            self._complete_order(
                order, event_type=event_type, reason=reason,
                sim_time=sim_time, phase=phase)
            lifecycle.commit()

    def _reject_active_order(self, order: Order, *, reason: str,
                             sim_time=None, phase: str = None) -> None:
        """Reject an unfilled live order and release its reservation atomically."""
        if order.filled_shares:
            raise SettlementInvariantError(
                "a partially filled order must be cancelled, not rejected")
        self._ensure_order_stamped(order, before_time=sim_time)
        baselines = self._reservation_baselines(order.symbol)
        with SettlementUnitOfWork(self, order) as lifecycle:
            order.reject(reason)
            if order.is_buy and order.frozen_amount > 0:
                self.account.unfreeze_cash(order.frozen_amount, strict=True)
                order.frozen_amount = 0.0
            if order.is_sell and order.frozen_shares > 0:
                self.account.unfreeze_shares(
                    order.symbol, order.frozen_shares, strict=True)
                order.frozen_shares = 0
            self._remove_from_pending(order)
            self._complete_order(
                order,
                event_type=ExecutionEventType.ORDER_REJECTED,
                reason=reason,
                sim_time=sim_time,
                phase=phase,
            )
            self._assert_reservation_baselines(order.symbol, baselines)
            lifecycle.commit()

    def cancel_order(self, order_id: str) -> bool:
        """
        取消指定订单

        Args:
            order_id: 订单ID

        Returns:
            是否取消成功
        """
        order = self.account.get_order(order_id)
        if order and order.is_open:
            self._cancel_active_order(
                order,
                sim_time=self._simulated_time(None),
                phase=self.current_created_phase or self._market_phase,
            )
            logger.info(f"Order cancelled: {order_id}")
            return True
        return False

    def cancel_orders(self, symbol: str = None, side: str = None) -> List[Order]:
        """
        批量取消订单

        Args:
            symbol: 股票代码 (None=所有股票)
            side: 'buy'/'sell' (None=所有方向)

        Returns:
            已取消的订单列表
        """
        cancelled = []
        for order in self.account.get_open_orders():
            # 过滤条件
            if symbol and order.symbol != symbol:
                continue
            if side == 'buy' and not order.is_buy:
                continue
            if side == 'sell' and not order.is_sell:
                continue

            self._cancel_active_order(
                order,
                sim_time=self._simulated_time(None),
                phase=self.current_created_phase or self._market_phase,
            )
            cancelled.append(order)

        if cancelled:
            logger.info(f"Cancelled {len(cancelled)} orders")
        return cancelled

    def cancel_all_orders(self) -> List[Order]:
        """取消所有未完成订单"""
        return self.cancel_orders()

    def execute_market_orders(self, bar: BarData,
                              match_at=None) -> List[Order]:
        """
        Execute market/limit/stop orders by price path matching.
        """
        return self._execute_orders_with_path(bar, match_at=match_at)

    def _execute_orders_with_path(self, bar: BarData,
                                  match_at=None) -> List[Order]:
        """
        Match open orders with conservative intra-bar path: open -> low -> high -> close.
        """
        self.validate_execution_bar_price_band(bar)
        executed = []
        executed_ids = set()
        actual_match_at = match_at if match_at is not None else bar.trade_time
        open_orders = [
            o for o in self.account.get_open_orders()
            if o.symbol == bar.symbol
            and self._is_order_eligible(o, actual_match_at)
        ]
        if not open_orders:
            return executed

        # Reserve future-eligible sells in submission order before order-type
        # trigger priority is applied.  This mirrors ordinary submission-time
        # reservation and makes competing deferred sells deterministic.
        for order in open_orders:
            if order.is_sell:
                with SettlementUnitOfWork(self, order) as reservation:
                    self._materialize_deferred_sell(
                        order, sim_time=actual_match_at,
                        phase='continuous_match')
                    reservation.commit()
        open_orders = [order for order in open_orders if order.is_open]
        if not open_orders:
            return executed

        priority = {
            OrderType.STOP: 0,
            OrderType.STOP_PROFIT: 1,
            OrderType.LIMIT: 2,
            OrderType.MARKET: 3,
            OrderType.OPEN: 4,
        }
        open_orders.sort(key=lambda o: priority.get(o.order_type, 99))

        price_path = [
            ('open', bar.open),
            ('low', bar.low),
            ('high', bar.high),
            ('close', bar.close),
        ]

        # ``bar.trade_time`` can be a date-only timestamp for daily bars.  The
        # public execution boundary supplies the causal match instant; expose
        # it to nested fill/settlement event creation just as the auction paths
        # do, otherwise a next-day fill can be journalled at midnight and move
        # backwards behind the order's prior-close submission event.
        with self._matching_time_scope(actual_match_at):
            for order in open_orders:
                if order.is_open:
                    self._validate_explicit_limit_price(order, bar)
            open_orders = [order for order in open_orders if order.is_open]
            for price_name, current_price in price_path:
                for order in open_orders:
                    if order.is_done:
                        continue
                    triggered, fill_price, ref_price = self._check_order_trigger(
                        order, current_price, price_name, bar)
                    if triggered:
                        before_filled = order.filled_shares
                        self._execute_order(
                            order, fill_price, bar, ref_price=ref_price)
                        if (order.filled_shares > before_filled
                                and order.order_id not in executed_ids):
                            executed.append(order)
                            executed_ids.add(order.order_id)
        return executed

    def _check_order_trigger(self, order: Order, current_price: float,
                             price_name: str, bar: BarData) -> tuple:
        """
        Check whether an order is triggered at current price point.
        Returns:
            (triggered: bool, fill_price: float, ref_price: float)
            ref_price = 未加滑点的市场参照价，用于涨跌停判定
        """
        if order.order_type == OrderType.STOP:
            if order.is_sell:
                if current_price <= order.price:
                    if price_name == 'open' and bar.open <= order.price:
                        return True, bar.open * (1 - self.slippage), bar.open
                    return True, order.price * (1 - self.slippage), order.price
            else:
                if current_price >= order.price:
                    if price_name == 'open' and bar.open >= order.price:
                        return True, bar.open * (1 + self.slippage), bar.open
                    return True, order.price * (1 + self.slippage), order.price

        elif order.order_type == OrderType.STOP_PROFIT:
            if order.is_sell and current_price >= order.price:
                if price_name == 'open' and bar.open >= order.price:
                    return True, bar.open * (1 - self.slippage), bar.open
                return True, order.price * (1 - self.slippage), order.price

        elif order.order_type == OrderType.LIMIT:
            if order.price is None:
                return False, 0, 0
            if order.is_buy and current_price <= order.price:
                # 开盘跳空穿越限价：按更优的开盘价成交（集合竞价价格改善，
                # 与 STOP 单同款特判；此前 LIMIT 单缺失该分支导致跳空日虚亏 gap 差）
                if price_name == 'open' and bar.open < order.price:
                    return True, bar.open, bar.open
                return True, order.price, order.price
            if order.is_sell and current_price >= order.price:
                if price_name == 'open' and bar.open > order.price:
                    return True, bar.open, bar.open
                return True, order.price, order.price

        elif order.order_type == OrderType.MARKET:
            if price_name == 'close':
                if order.is_buy:
                    return True, bar.high * (1 + self.slippage), bar.high
                return True, bar.low * (1 - self.slippage), bar.low

        return False, 0, 0

    def _check_limit(self, symbol: str, bar: BarData, is_buy: bool,
                     fill_price: float = None, ref_price: float = None,
                     order_type: OrderType = None) -> tuple:
        """
        Check limit up/down constraints.

        Args:
            fill_price: 实际成交价（含滑点）
            ref_price: 涨跌停判定参照价（未加滑点的市场价，如 bar.open/high/low/
                触发价）。滑点是成本近似而非真实市场价，不应参与触限判定，
                否则会把未触板的成交误判为涨跌停拒单。
        """
        # 新股上市初期无涨跌幅限制（由引擎按 list_date 计算豁免日历）
        if self._is_limit_check_exempt(symbol):
            return True, None
        self._require_bar_pre_close(symbol, bar)

        band_reference = self._price_band_reference(bar)
        limit_up = self._limit_up_price(symbol, band_reference)
        limit_down = self._limit_down_price(symbol, band_reference)
        nd = self._price_decimals(symbol)
        up_ticks = self._price_ticks(limit_up, nd)
        down_ticks = self._price_ticks(limit_down, nd)

        if order_type is OrderType.OPEN:
            # The opening auction is a distinct execution window.  A daily
            # bar's close/high/low are future information at 09:30 and must
            # never decide whether an OPEN order filled.
            locked = (
                self._price_ticks(bar.open, nd) == up_ticks
                if is_buy else self._price_ticks(bar.open, nd) == down_ticks
            )
            reference = bar.open
        elif self.bar_liquidity_model == 'close_lock':
            locked = (
                self._price_ticks(bar.close, nd) == up_ticks
                if is_buy else self._price_ticks(bar.close, nd) == down_ticks
            )
            reference = bar.close
        else:
            locked = (
                self._price_ticks(bar.high, nd) >= up_ticks
                if is_buy else self._price_ticks(bar.low, nd) <= down_ticks
            )
            reference = bar.high if is_buy else bar.low

        if locked:
            direction = 'Limit-up blocked buy' if is_buy else 'Limit-down blocked sell'
            boundary = limit_up if is_buy else limit_down
            return False, (
                f"{direction}: model={self.bar_liquidity_model}, "
                f"ref={reference:.3f}, boundary={boundary:.3f}"
            )
        return True, None

    def _cash_rule(self, symbol: str):
        """Return a supported cash rule or fail before account mutation."""
        return self.rulebook.require_supported(
            symbol, engine=ExecutionEngine.CASH)

    def _get_limit_pct(self, symbol: str, trade_date: str) -> float:
        """Return the effective-dated limit ratio, with explicit stress override."""
        # Resolve first even when an override exists: an override is a stress
        # parameter, not authority to turn an unknown/unsupported security into
        # an ordinary A share.
        rule = self.limit_band_service.get_rule(symbol, trade_date)
        code = symbol.split('.', 1)[0][:6]
        pct = self.limit_pct_overrides.get(code)
        if pct is None:
            pct = self.limit_pct_overrides.get(symbol)
        return float(rule.limit_pct if pct is None else pct)

    def _price_decimals(self, symbol: str) -> int:
        """Return tick precision from the classified instrument rule."""
        return self._cash_rule(symbol).price_decimals

    @staticmethod
    def _round_price_to_tick(
            price: float, decimals: int,
            rounding=ROUND_HALF_UP) -> float:
        """Round a positive price to the configured legal tick."""
        price = positive_float(price, 'price')
        quantum = Decimal(1).scaleb(-decimals)
        return float(Decimal(str(price)).quantize(
            quantum, rounding=rounding))

    @classmethod
    def _price_ticks(cls, price: float, decimals: int) -> int:
        rounded = cls._round_price_to_tick(price, decimals)
        scale = Decimal(10) ** decimals
        return int(Decimal(str(rounded)) * scale)

    def set_limit_check_exempt(self, mapping: Dict[str, set]) -> None:
        """注入涨跌停校验豁免日历（新股上市初期无涨跌幅限制）"""
        self._limit_check_exempt = dict(mapping) if mapping else {}

    def _is_limit_check_exempt(self, symbol: str) -> bool:
        return self.current_date in self._limit_check_exempt.get(symbol, set())

    def _require_bar_pre_close(self, symbol: str, bar: BarData) -> None:
        if bar.pre_close > 0 or self._is_limit_check_exempt(symbol):
            return
        raise ValueError(
            "pre_close must be positive for execution unless the symbol/date "
            "has an explicit limit_check_exempt entry"
        )

    def _is_t0(self, symbol: str) -> bool:
        """Whether rulebook settlement or an explicit override permits T+0."""
        if self._cash_rule(symbol).settlement == "T+0":
            return True
        if not self.t0_overrides:
            return False
        if symbol in self.t0_overrides:
            return True
        code = symbol[:6]
        if code in self.t0_overrides:
            return True
        return any(code.startswith(p) for p in self.t0_overrides if len(p) < 6)

    def _lot_rule(self, symbol: str) -> tuple:
        """申报单位规则 (最低买入股数, 递增单位)。

        科创板(688/689): 200股起、1股递增；
        北交所(92/43/83/87): 100股起、1股递增；
        其余(含ETF): lot_size 整手(默认100)。
        最低股数仅约束买单目标量；部分成交与卖出不受最低限制。
        """
        rule = self._cash_rule(symbol)
        if (rule.buy_min_qty, rule.buy_step) != (100, 100):
            return (rule.buy_min_qty, rule.buy_step)
        # Preserve the public lot_size stress/configuration parameter for the
        # ordinary 100-share rule while classification remains fail-fast.
        return (self.lot_size, self.lot_size)

    def _round_buy_shares(self, symbol: str, raw_shares: float) -> int:
        """把目标买入量按申报规则取整（低于最低申报量返回0）"""
        min_shares, inc = self._lot_rule(symbol)
        shares = (int(raw_shares) // inc) * inc
        return shares if shares >= min_shares else 0

    def _limit_up_price(self, symbol: str, pre_close: float) -> Optional[float]:
        """当日涨停价（pre_close 缺失返回 None）"""
        self._cash_rule(symbol)
        if pre_close is None or pre_close <= 0:
            return None
        # on_init runs before the engine has selected its first trade date.
        # Use the deterministic rule snapshot date for reservation-only calls
        # instead of leaking a wall-clock date or raising a bare TypeError.
        effective_date = (
            self.current_date or LIMIT_BAND_FUND_SNAPSHOT_FREEZE_DATE)
        pct = self._get_limit_pct(symbol, effective_date)
        raw = Decimal(str(pre_close)) * (Decimal(1) + Decimal(str(pct)))
        rule = self.limit_band_service.get_rule(symbol, effective_date)
        rounding = (
            ROUND_DOWN
            if rule.rounding_policy is LimitBandRoundingPolicy.TOWARD_REFERENCE
            else ROUND_HALF_UP
        )
        return self._round_price_to_tick(
            float(raw), self._price_decimals(symbol), rounding=rounding)

    def _limit_down_price(self, symbol: str, pre_close: float) -> Optional[float]:
        """当日跌停价（pre_close 缺失返回 None）。"""
        self._cash_rule(symbol)
        if pre_close is None or pre_close <= 0:
            return None
        effective_date = (
            self.current_date or LIMIT_BAND_FUND_SNAPSHOT_FREEZE_DATE)
        pct = self._get_limit_pct(symbol, effective_date)
        raw = Decimal(str(pre_close)) * (Decimal(1) - Decimal(str(pct)))
        rule = self.limit_band_service.get_rule(symbol, effective_date)
        rounding = (
            ROUND_UP
            if rule.rounding_policy is LimitBandRoundingPolicy.TOWARD_REFERENCE
            else ROUND_HALF_UP
        )
        return self._round_price_to_tick(
            float(raw), self._price_decimals(symbol), rounding=rounding)

    def validate_execution_bar_price_band(self, bar: BarData) -> None:
        """Reject execution OHLC that contradicts the effective daily band.

        The raw execution lane is market evidence, while a
        ``limit_pct_overrides`` entry is only a modelling assumption.  If the
        evidence cannot exist under that assumption (for example 588000.SH
        trading +20% while an override says 10%), continuing would let later
        fill logic manufacture prices.  Validate the evidence before any
        order or strategy callback can consume it.
        """
        if not isinstance(bar, BarData):
            raise TypeError("execution price-band validation requires BarData")
        if self._is_limit_check_exempt(bar.symbol):
            return
        self._require_bar_pre_close(bar.symbol, bar)
        band_reference = self._price_band_reference(bar)
        limit_up = self._limit_up_price(bar.symbol, band_reference)
        limit_down = self._limit_down_price(bar.symbol, band_reference)
        decimals = self._price_decimals(bar.symbol)
        conflicts = []
        for field in ("open", "high", "low", "close"):
            value = positive_float(getattr(bar, field), field)
            if value > limit_up + 1e-12 or value < limit_down - 1e-12:
                conflicts.append(f"{field}={value:.{decimals}f}")
        if conflicts:
            date_text = bar.trade_time.strftime("%Y%m%d")
            code = bar.symbol.split('.', 1)[0][:6]
            override = self.limit_pct_overrides.get(
                bar.symbol, self.limit_pct_overrides.get(code))
            override_text = (
                f", limit_pct_override={float(override):.6g}"
                if override is not None else ""
            )
            raise ValueError(
                "Raw execution OHLC conflicts with effective daily price "
                f"band: symbol={bar.symbol}, trade_date={date_text}, "
                f"pre_close={bar.pre_close:.{decimals}f}, "
                f"band_reference={band_reference:.{decimals}f}, "
                f"band=[{limit_down:.{decimals}f},"
                f"{limit_up:.{decimals}f}], "
                f"out_of_band={','.join(conflicts)}{override_text}. "
                "The execution data and limit rule/override cannot both be "
                "true; correct the rule or data instead of clipping the bar."
            )

    def _ohlc_within_price_band(self, bar: BarData,
                                reference: float) -> bool:
        limit_up = self._limit_up_price(bar.symbol, reference)
        limit_down = self._limit_down_price(bar.symbol, reference)
        return all(
            limit_down - 1e-12 <= float(getattr(bar, field))
            <= limit_up + 1e-12
            for field in ("open", "high", "low", "close")
        )

    def _price_band_reference(self, bar: BarData) -> float:
        """Resolve a comparable raw reference across corporate actions.

        Vendor ``pre_close`` conventions differ on ex-right dates: some feeds
        already publish the exchange-adjusted reference, while others retain
        the preceding raw close.  Keep the source value whenever its OHLC is
        coherent.  Only when it is incoherent, and the audited day-over-day
        adjustment factor makes the entire raw bar coherent, use
        ``pre_close / factor_ratio``.  An arbitrary limit override therefore
        cannot obtain this escape hatch without factor evidence.
        """
        source = positive_float(bar.pre_close, 'pre_close')
        ratio = bar.adjustment_factor_ratio
        if ratio is None or abs(float(ratio) - 1.0) < 1e-5:
            return source
        if self._ohlc_within_price_band(bar, source):
            return source
        adjusted = positive_float(
            source / float(ratio), 'corporate-action band reference')
        if self._ohlc_within_price_band(bar, adjusted):
            return adjusted
        return source

    def _saturate_modeled_fill_to_limit_band(
            self, symbol: str, bar: BarData, is_buy: bool,
            fill_price: float) -> float:
        """Saturate only an adverse modeled overage at the legal boundary.

        A buy cost model may exceed limit-up and a sell cost model may fall
        below limit-down.  The opposite directions are favorable prices, not
        slippage saturation, and therefore indicate a broken matching/data
        invariant rather than something that may be silently rewritten.
        """
        self._cash_rule(symbol)
        fill_price = positive_float(fill_price, 'fill_price')
        if self._is_limit_check_exempt(symbol):
            return fill_price
        self._require_bar_pre_close(symbol, bar)
        band_reference = self._price_band_reference(bar)
        limit_up = self._limit_up_price(symbol, band_reference)
        limit_down = self._limit_down_price(symbol, band_reference)
        decimals = self._price_decimals(symbol)
        if is_buy:
            if fill_price < limit_down - 1e-12:
                raise SettlementInvariantError(
                    "modeled BUY fill is below the legal limit-down boundary: "
                    f"symbol={symbol}, fill={fill_price}, "
                    f"band=[{limit_down},{limit_up}]"
                )
            return min(fill_price, limit_up)
        if fill_price > limit_up + 1e-12:
            raise SettlementInvariantError(
                "modeled SELL fill is above the legal limit-up boundary: "
                f"symbol={symbol}, fill={fill_price}, "
                f"band=[{limit_down},{limit_up}]"
            )
        return max(fill_price, limit_down)

    def _validate_explicit_limit_price(self, order: Order,
                                       bar: BarData) -> None:
        """Reject, rather than redirect, an explicit off-band limit order."""
        if order.order_type is not OrderType.LIMIT or order.price is None:
            return
        if self._is_limit_check_exempt(order.symbol):
            return
        self._require_bar_pre_close(order.symbol, bar)
        band_reference = self._price_band_reference(bar)
        limit_up = self._limit_up_price(order.symbol, band_reference)
        limit_down = self._limit_down_price(order.symbol, band_reference)
        decimals = self._price_decimals(order.symbol)
        if limit_down - 1e-12 <= order.price <= limit_up + 1e-12:
            return
        reason = (
            "Explicit LIMIT order price outside legal daily price band: "
            f"price={order.price:.{decimals}f}, "
            f"band=[{limit_down:.{decimals}f},{limit_up:.{decimals}f}]"
        )
        self._reject_active_order(
            order,
            reason=reason,
            sim_time=self._active_match_time or bar.trade_time,
            phase=self._matching_phase(order, bar),
        )

    def _continuous_buy_reservation_price(
            self, symbol: str, order_type: OrderType,
            estimated_price: float) -> float:
        """Return a conservative price bound for a pre-funded buy order.

        A LIMIT buy cannot execute above its own limit.  MARKET and STOP-style
        buys can execute after an adverse move, however, so ``est * 1.01`` is
        not a meaningful upper bound under the worst-bar model.  Reserve one
        full daily limit move from the observable estimate (and at least the
        configured slippage move); the eventual fill is independently clamped
        against the execution bar's actual legal band.
        """

        estimate = positive_float(estimated_price, 'estimated_price')
        if order_type is OrderType.LIMIT:
            return estimate
        limit_upper = self._limit_up_price(symbol, estimate)
        slippage_upper = positive_float(
            estimate * (1 + self.slippage), 'slippage reservation price')
        return max(estimate, slippage_upper, limit_upper or estimate)

    def _reject_invalid_buy_order(self, symbol: str, order_type: OrderType,
                                  note: str, reason: str) -> Order:
        """Record a safe rejection without retaining the invalid numeric input."""
        order = Order(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=order_type,
            shares=0,
            price=None,
            create_date=self.current_date,
            note=note,
        )
        order.reject(f"Invalid buy order: {reason}")
        self.account.add_order(order)
        return order

    def _create_buy_order(self, symbol: str, order_type: OrderType,
                          price: float = None, amount: float = None,
                          shares: int = None, percent: float = None,
                          note: str = '', current_price: float = None) -> Order:
        """Create, reserve, identify, and journal one buy atomically."""
        with SettlementUnitOfWork(self) as submission:
            order = self._create_buy_order_inner(
                symbol, order_type, price, amount, shares, percent, note,
                current_price)
            self._stamp_order(order)
            submission.commit()
            return order

    def _create_buy_order_inner(self, symbol: str, order_type: OrderType,
                                price: float = None, amount: float = None,
                                shares: int = None, percent: float = None,
                                note: str = '',
                                current_price: float = None) -> Order:
        """Create buy order."""
        self._cash_rule(symbol)
        try:
            if price is not None:
                price = positive_float(price, 'price')
            elif order_type in (OrderType.LIMIT, OrderType.STOP,
                                 OrderType.STOP_PROFIT):
                raise ValueError('price must be provided')
            if current_price is not None:
                current_price = positive_float(current_price, 'current_price')
            if amount is not None:
                amount = positive_float(amount, 'amount')
            if shares is not None:
                shares = positive_int(shares, 'shares')
            if percent is not None:
                percent = unit_fraction(percent)
        except ValueError as exc:
            if order_type == OrderType.OPEN:
                self._open_buy_total += 1
                self._open_buy_reject_invalid_count += 1
            return self._reject_invalid_buy_order(
                symbol, order_type, note, str(exc))

        if order_type == OrderType.OPEN:
            return self._create_open_buy_order(
                symbol=symbol,
                order_type=order_type,
                price=price,
                amount=amount,
                shares=shares,
                percent=percent,
                note=note,
            )

        if self._market_phase == 'after_close' and order_type not in (
                OrderType.OPEN, OrderType.CLOSE):
            # 盘后挂的 LIMIT/MARKET/STOP 单当晚会被日终静默撤销（订单不过夜），
            # 此前无时段校验导致"盘后挂止损、以为挂上了"的静默陷阱——
            # 与 OPEN(仅盘前)/CLOSE(仅盘中) 的显式时段校验对称化
            order = Order(
                symbol=symbol, side=OrderSide.BUY, order_type=order_type,
                shares=0, price=price, create_date=self.current_date, note=note,
            )
            order.reject(
                "盘后(after_close)不可挂盘中订单：本引擎订单不过夜（日终自动撤销），"
                "请在下一交易日盘前/盘中下单"
            )
            self.account.add_order(order)
            return order

        if order_type == OrderType.CLOSE and self.open_buy_resize_mode == 'auto':
            # 收盘竞价买单（auto）：延迟到收盘执行时点定量（卖先买后，
            # percent/amount 按回款后现金折算），提交时不冻结、不校验资金。
            # 与开盘 auto 口径对称；legacy 模式保留提交时冻结的旧行为。
            if amount is None and shares is None and percent is None:
                order = Order(
                    symbol=symbol,
                    side=OrderSide.BUY,
                    order_type=order_type,
                    shares=0,
                    price=None,
                    create_date=self.current_date,
                    note=note,
                )
                order.reject("Invalid close order: amount/shares/percent required")
                self.account.add_order(order)
                return order
            order = Order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=order_type,
                shares=0,
                price=None,
                create_date=self.current_date,
                note=note,
                requested_shares=int(shares) if shares is not None else None,
                requested_amount=float(amount) if amount is not None else None,
                requested_percent=float(percent) if percent is not None else None,
            )
            order.submit()
            self.account.add_order(order)
            return order

        est_price = price if price is not None else current_price
        if est_price is None:
            order = Order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=order_type,
                shares=0,
                price=price,
                create_date=self.current_date,
                note=note,
                requested_shares=int(shares) if shares is not None else None,
                requested_amount=amount,
                requested_percent=percent,
            )
            if order_type in (OrderType.MARKET, OrderType.CLOSE):
                order.reject("Missing current_price for market/close order")
            else:
                order.reject("Missing price for order")
            self.account.add_order(order)
            return order

        # 三口径优先级统一为 shares > amount > percent（与竞价单一致；
        # 历史上非竞价单曾是 percent 覆盖 shares，与竞价单方向相反）
        given = [n for n, v in (('shares', shares), ('amount', amount),
                                ('percent', percent)) if v is not None]
        if len(given) > 1:
            logger.warning(
                f"{symbol} 下单同时给了 {'/'.join(given)}，"
                f"按优先级 shares > amount > percent 取 {given[0]}")
        requested_shares_arg = int(shares) if shares is not None else None
        if shares is None and amount is not None:
            shares = self._round_buy_shares(symbol, amount / est_price)
        elif shares is None and percent is not None:
            target_amount = self.account.available_cash * percent
            shares = self._round_buy_shares(symbol, target_amount / est_price)

        if shares is None or shares <= 0:
            order = Order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=order_type,
                shares=0,
                price=price,
                create_date=self.current_date,
                note=note,
                requested_shares=requested_shares_arg,
                requested_amount=amount,
                requested_percent=percent,
            )
            order.reject("Invalid shares: must be positive")
            self.account.add_order(order)
            return order

        min_shares, _lot_inc = self._lot_rule(symbol)
        shares = (int(shares) // _lot_inc) * _lot_inc
        if shares < min_shares:
            # 历史坑：99 股取整为 0 后仍 SUBMITTED（僵尸单，整日不成交无拒因）；
            # 科创板 200 股最低申报此前只在竞价路径校验
            order = Order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=order_type,
                shares=0,
                price=price,
                create_date=self.current_date,
                note=note,
                requested_shares=requested_shares_arg,
                requested_amount=amount,
                requested_percent=percent,
            )
            order.reject(
                f"Below minimum lot: 取整后 {shares} 股 < 最低申报量 {min_shares} 股"
            )
            self.account.add_order(order)
            return order
        order = Order(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=order_type,
            shares=shares,
            price=price,
            amount=amount,
            create_date=self.current_date,
            note=note,
            requested_shares=int(shares),
            requested_amount=amount,
            requested_percent=percent,
        )
        try:
            reservation_price = self._continuous_buy_reservation_price(
                symbol, order_type, est_price)
            required_amount = self._estimate_buy_total_cost(
                shares, reservation_price, order=order)
        except ValueError as exc:
            return self._reject_invalid_buy_order(
                symbol, order_type, note, str(exc))
        if required_amount > self.account.available_cash:
            # ``amount`` and ``percent`` are budget-sizing intents, not exact
            # share instructions.  MARKET and STOP-style orders reserve at a
            # conservative daily upper bound; when that bound alone pushes an
            # otherwise valid budget request over cash, shrink to the largest
            # exactly fundable lot.  Explicit ``shares`` remains all-or-reject.
            budget_sized_continuous = (
                requested_shares_arg is None
                and (amount is not None or percent is not None)
                and order_type in (
                    OrderType.MARKET,
                    OrderType.STOP,
                    OrderType.STOP_PROFIT,
                )
            )
            if budget_sized_continuous:
                requested_quantity = int(order.shares)
                affordable = self._calc_max_affordable_shares(
                    self.account.available_cash,
                    reservation_price,
                    max_shares=requested_quantity,
                    symbol=symbol,
                    order=order,
                )
                # _calc_max_affordable_shares has a sub-cent numerical guard
                # for execution paths.  Submission freezing is exact, so make
                # the final lot obey Account.freeze_cash's strict boundary.
                while affordable >= min_shares:
                    affordable_cost = self._estimate_buy_total_cost(
                        affordable, reservation_price, order=order)
                    if affordable_cost <= self.account.available_cash:
                        break
                    affordable -= _lot_inc
                if affordable >= min_shares:
                    order.shares = affordable
                    order.auto_resized = True
                    order.resized_from_shares = requested_quantity
                    order.resized_to_shares = affordable
                    order.resize_reason = "reservation_cash_limit"
                    required_amount = affordable_cost
                else:
                    affordable = 0

            if not budget_sized_continuous or affordable <= 0:
                order.reject(
                    f"Insufficient cash: need {required_amount:.2f}, have {self.account.available_cash:.2f}"
                )
                self.account.add_order(order)
                return order

        if not self.account.freeze_cash(required_amount):
            order.reject("Failed to freeze cash")
            self.account.add_order(order)
            return order
        order.frozen_amount = required_amount
        order.submit()
        self.account.add_order(order)
        return order

    def _create_open_buy_order(self, symbol: str, order_type: OrderType,
                               price: float = None, amount: float = None,
                               shares: int = None, percent: float = None,
                               note: str = '') -> Order:
        """Create open buy order under auto/legacy mode."""
        self._open_buy_total += 1
        requested_shares = int(shares) if shares is not None else None
        requested_amount = float(amount) if amount is not None else None
        requested_percent = float(percent) if percent is not None else None

        if requested_shares is None and requested_amount is None and requested_percent is None:
            order = Order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=order_type,
                shares=0,
                price=price,
                create_date=self.current_date,
                note=note,
                requested_shares=requested_shares,
                requested_amount=requested_amount,
                requested_percent=requested_percent,
            )
            order.reject("Invalid open order: amount or shares required")
            self._open_buy_reject_invalid_count += 1
            self.account.add_order(order)
            return order

        if requested_shares is not None and requested_shares <= 0:
            order = Order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=order_type,
                shares=0,
                price=price,
                create_date=self.current_date,
                note=note,
                requested_shares=requested_shares,
                requested_amount=requested_amount,
                requested_percent=requested_percent,
            )
            order.reject("Invalid shares: must be positive")
            self._open_buy_reject_invalid_count += 1
            self.account.add_order(order)
            return order

        if requested_amount is not None and requested_amount <= 0:
            order = Order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=order_type,
                shares=0,
                price=price,
                create_date=self.current_date,
                note=note,
                requested_shares=requested_shares,
                requested_amount=requested_amount,
                requested_percent=requested_percent,
            )
            order.reject("Invalid amount: must be positive")
            self._open_buy_reject_invalid_count += 1
            self.account.add_order(order)
            return order

        if requested_percent is not None and requested_percent <= 0:
            order = Order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=order_type,
                shares=0,
                price=price,
                create_date=self.current_date,
                note=note,
                requested_shares=requested_shares,
                requested_amount=requested_amount,
                requested_percent=requested_percent,
            )
            order.reject("Invalid percent: must be positive")
            self._open_buy_reject_invalid_count += 1
            self.account.add_order(order)
            return order

        if self.open_buy_resize_mode == 'legacy':
            if requested_percent is not None and requested_amount is None and requested_shares is None:
                requested_amount = self.account.available_cash * requested_percent
            if requested_amount is not None and requested_shares is None:
                try:
                    required_amount = positive_float(
                        requested_amount * (1 + self.slippage) * 1.01,
                        'required amount')
                except ValueError as exc:
                    self._open_buy_reject_invalid_count += 1
                    return self._reject_invalid_buy_order(
                        symbol, order_type, note, str(exc))
                if required_amount > self.account.available_cash:
                    order = Order(
                        symbol=symbol,
                        side=OrderSide.BUY,
                        order_type=order_type,
                        shares=0,
                        price=price,
                        amount=requested_amount,
                        create_date=self.current_date,
                        note=note,
                        requested_shares=requested_shares,
                        requested_amount=requested_amount,
                        requested_percent=requested_percent,
                    )
                    order.reject(
                        f"Insufficient cash: need {required_amount:.2f}, have {self.account.available_cash:.2f}"
                    )
                    self._open_buy_reject_insuff_cash_count += 1
                    self.account.add_order(order)
                    return order

                order = Order(
                    symbol=symbol,
                    side=OrderSide.BUY,
                    order_type=order_type,
                    shares=0,
                    price=price,
                    amount=requested_amount,
                    create_date=self.current_date,
                    note=note,
                    requested_shares=requested_shares,
                    requested_amount=requested_amount,
                    requested_percent=requested_percent,
                )
                self.account.freeze_cash(required_amount)
                order.frozen_amount = required_amount
                order.submit()
                self.account.add_order(order)
                return order

            order = Order(
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=order_type,
                shares=requested_shares or 0,
                price=price,
                create_date=self.current_date,
                note=note,
                requested_shares=requested_shares,
                requested_amount=requested_amount,
                requested_percent=requested_percent,
            )
            order.submit()
            self.account.add_order(order)
            return order

        order = Order(
            symbol=symbol,
            side=OrderSide.BUY,
            order_type=order_type,
            shares=0,
            price=price,
            amount=requested_amount,
            create_date=self.current_date,
            note=note,
            requested_shares=requested_shares,
            requested_amount=requested_amount,
            requested_percent=requested_percent,
        )
        order.submit()
        self.account.add_order(order)
        return order

    def _create_sell_order(self, symbol: str, order_type: OrderType,
                           price: float = None, shares: int = None,
                           percent: float = None, note: str = '') -> Order:
        """Create, reserve, identify, and journal one sell atomically."""
        with SettlementUnitOfWork(self) as submission:
            order = self._create_sell_order_inner(
                symbol, order_type, price, shares, percent, note)
            self._stamp_order(order)
            submission.commit()
            return order

    def _create_sell_order_inner(self, symbol: str, order_type: OrderType,
                                 price: float = None, shares: int = None,
                                 percent: float = None,
                                 note: str = '') -> Order:
        """Create sell order."""
        self._cash_rule(symbol)
        try:
            if price is not None:
                price = positive_float(price, 'price')
            elif order_type in (OrderType.LIMIT, OrderType.STOP,
                                 OrderType.STOP_PROFIT):
                raise ValueError('price must be provided')
            if shares is not None:
                shares = positive_int(shares, 'shares')
            if percent is not None:
                percent = unit_fraction(percent)
            if (order_type in (OrderType.STOP, OrderType.STOP_PROFIT)
                    and shares is None and percent is None):
                raise ValueError(
                    'STOP orders require explicit shares or percent; '
                    'use percent=1.0 for a full-position stop'
                )
        except ValueError as exc:
            # Never retain a non-finite input on the rejected order itself.
            order = Order(
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=order_type,
                shares=0,
                price=None,
                create_date=self.current_date,
                note=note,
            )
            order.reject(f"Invalid sell order: {exc}")
            self.account.add_order(order)
            return order

        if self._market_phase == 'after_close' and order_type not in (
                OrderType.OPEN, OrderType.CLOSE):
            # 与买侧对称：盘后挂的盘中订单当晚会被日终静默撤销（见买侧注释）
            order = Order(
                symbol=symbol, side=OrderSide.SELL, order_type=order_type,
                shares=0, price=price, create_date=self.current_date, note=note,
            )
            order.reject(
                "盘后(after_close)不可挂盘中订单：本引擎订单不过夜（日终自动撤销），"
                "请在下一交易日盘前/盘中下单"
            )
            self.account.add_order(order)
            return order

        pos = self.account.get_position(symbol)
        scope = self._order_creation_scope
        defer_until_eligible = bool(
            scope is not None
            and scope.eligible_from is not None
            and scope.eligible_from > scope.sim_time
        )
        future_target_shares = 0
        if defer_until_eligible and pos is not None and pos.shares > 0:
            if shares is not None:
                future_target_shares = shares
            elif percent is not None:
                future_target_shares = int(pos.shares * percent)
            else:
                future_target_shares = pos.shares
        requires_deferred_inventory = (
            defer_until_eligible
            and pos is not None
            and pos.shares > 0
            and future_target_shares > pos.available_shares
        )
        if requires_deferred_inventory:
            # An on_day(T) order may target shares bought on T which remain
            # T+1-locked at submission time.  Reserve every share that is
            # already available now so a later order cannot jump the queue;
            # the first eligible window reserves the still-locked remainder.
            target_shares = min(future_target_shares, pos.shares)
            if not self.allow_odd_lot_sell:
                target_shares = (target_shares // self.lot_size) * self.lot_size
            reserve_now = min(target_shares, pos.available_shares)
            if not self.allow_odd_lot_sell:
                reserve_now = (reserve_now // self.lot_size) * self.lot_size
            frozen_now = (
                self.account.freeze_shares(symbol, reserve_now)
                if reserve_now > 0 else 0
            )
            requested_percent = (
                percent if percent is not None
                else (1.0 if shares is None else None)
            )
            if target_shares <= 0:
                order = Order(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_type=order_type,
                    shares=0,
                    price=price,
                    create_date=self.current_date,
                    note=note,
                )
                order.reject("Invalid shares")
                self.account.add_order(order)
                return order
            order = Order(
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=order_type,
                shares=frozen_now,
                price=price,
                create_date=self.current_date,
                note=note,
                # Always retain the resolved target.  requested_percent keeps
                # the user's original sizing expression when applicable.
                requested_shares=target_shares,
                requested_percent=requested_percent,
            )
            order.frozen_shares = frozen_now
            order.submit()
            self.account.add_order(order)
            return order

        if pos is None or pos.available_shares <= 0:
            order = Order(
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=order_type,
                shares=0,
                price=price,
                create_date=self.current_date,
                note=note
            )
            order.reject("No available position to sell")
            self.account.add_order(order)
            return order

        # 优先级统一为 shares > percent（与买侧一致；两参全缺=全部可卖）
        if shares is not None and percent is not None:
            logger.warning(
                f"{symbol} 卖单同时给了 shares/percent，"
                f"按优先级 shares > percent 取 shares")
        if shares is None:
            if percent is not None:
                shares = int(pos.available_shares * percent)
            else:
                shares = pos.available_shares

        shares = min(shares, pos.available_shares)

        if not self.allow_odd_lot_sell:
            shares = (shares // self.lot_size) * self.lot_size

        if shares <= 0:
            order = Order(
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=order_type,
                shares=0,
                price=price,
                create_date=self.current_date,
                note=note
            )
            order.reject("Invalid shares")
            self.account.add_order(order)
            return order

        frozen = self.account.freeze_shares(symbol, shares)
        if frozen <= 0:
            # Conditional orders reserve real inventory just like other sells.
            # Overlapping stops are not OCO orders: a later order must not
            # manufacture a second reservation after availability reaches 0.
            order = Order(
                symbol=symbol,
                side=OrderSide.SELL,
                order_type=order_type,
                shares=0,
                price=price,
                create_date=self.current_date,
                note=note
            )
            order.reject("Failed to freeze shares")
            self.account.add_order(order)
            return order

        if frozen < shares:
            if not self.allow_odd_lot_sell:
                shares = (frozen // self.lot_size) * self.lot_size
            else:
                shares = frozen
            if shares <= 0:
                self.account.unfreeze_shares(symbol, frozen)
                order = Order(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    order_type=order_type,
                    shares=0,
                    price=price,
                    create_date=self.current_date,
                    note=note
                )
                order.reject("Frozen shares not available")
                self.account.add_order(order)
                return order

        order = Order(
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=order_type,
            shares=shares,
            price=price,
            create_date=self.current_date,
            note=note
        )
        order.frozen_shares = shares
        order.submit()
        self.account.add_order(order)
        return order

    def _materialize_deferred_sell(self, order: Order, *, sim_time=None,
                                   phase: str = None) -> bool:
        """Complete a deferred sell reservation at its first eligible window."""
        self._ensure_order_stamped(order, before_time=sim_time)
        if not order.is_sell:
            return True
        if (order.requested_shares is None
                and order.requested_percent is None):
            return True

        pos = self.account.get_position(order.symbol)
        if pos is None:
            order.reject("Deferred sell rejected: no available position")
            self._complete_order(
                order, sim_time=sim_time, phase=phase)
            return False

        inventory_basis = pos.shares + order.filled_shares
        if order.requested_shares is not None:
            target_shares = order.requested_shares
        else:
            target_shares = int(
                inventory_basis * order.requested_percent)
        target_shares = min(target_shares, inventory_basis)
        if not self.allow_odd_lot_sell:
            target_shares = (
                target_shares // self.lot_size) * self.lot_size
        if target_shares <= order.filled_shares or order.shares > target_shares:
            reason = "Deferred sell residual cancelled: inventory changed"
            if order.filled_shares > 0:
                self._cancel_active_order(
                    order, reason=reason, sim_time=sim_time, phase=phase)
            else:
                self._reject_active_order(
                    order,
                    reason="Deferred sell rejected: invalid shares",
                    sim_time=sim_time,
                    phase=phase,
                )
            return False

        needed = target_shares - order.shares
        if needed <= 0:
            return True
        reserve_now = min(needed, pos.available_shares)
        if not self.allow_odd_lot_sell:
            reserve_now = (reserve_now // self.lot_size) * self.lot_size
        frozen = (
            self.account.freeze_shares(order.symbol, reserve_now)
            if reserve_now > 0 else 0
        )
        resolved_shares = order.shares + frozen
        if resolved_shares <= 0:
            order.reject("Deferred sell rejected: failed to freeze shares")
            self._complete_order(
                order, sim_time=sim_time, phase=phase)
            return False

        order.shares = resolved_shares
        order.frozen_shares += frozen
        if frozen > 0:
            self._record_reserve(
                order, float(frozen), sim_time=sim_time,
                phase=phase or 'continuous_match')
        if resolved_shares < target_shares:
            order.auto_resized = True
            order.resized_from_shares = target_shares
            order.resized_to_shares = resolved_shares
            order.resize_reason = 'deferred_inventory_shortfall'
            # Freeze the resolved quantity exactly once.  The original target
            # remains in resized_from_shares and the original percent remains
            # in requested_percent for audit.
            order.requested_shares = resolved_shares
        return True

    def _preflight_auction_caps(self, orders: List[Order],
                                bars: Dict[str, BarData],
                                match_at=None) -> None:
        """Resolve every eligible auction cap before mutating any order.

        This makes a missing required cap atomic across the entire broker call:
        a later symbol cannot fail after earlier symbols have already filled.
        """
        checked = set()
        for order in orders:
            if order.is_done or order.symbol not in bars:
                continue
            if (match_at is not None
                    and not self._is_order_eligible(order, match_at)):
                continue
            bar = bars[order.symbol]
            if bar.symbol != order.symbol:
                raise SettlementInvariantError(
                    "auction bar mapping key does not match BarData.symbol")
            actual_match_at = (
                match_at if match_at is not None else bar.trade_time)
            if not self._is_order_eligible(order, actual_match_at):
                continue
            key = (order.symbol, id(bar))
            if key not in checked:
                self._get_available_amount(order.symbol, bar)
                checked.add(key)

    def _cancel_auction_residual(self, order: Order, reason: str) -> None:
        """Cancel one auction remainder and atomically release reservations."""
        if order.is_done:
            return
        if self.account.orders.get(order.order_id) is not order:
            raise SettlementInvariantError(
                "auction residual must be the active account order object")
        if not order.is_open:
            raise SettlementInvariantError(
                "auction residual must be an open order")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("auction cancellation reason must be non-empty")
        transient_veto = order.reject_reason or ''
        if transient_veto.startswith(
                ('Limit-up blocked buy', 'Limit-down blocked sell')):
            reason = transient_veto
        reservation_baselines = self._reservation_baselines(order.symbol)
        history_length = len(self.account.order_history)
        event_time = (
            self._active_match_time
            if self._active_match_time is not None
            else self._simulated_time(None)
        )
        event_phase = (
            'opening_match'
            if order.order_type is OrderType.OPEN else 'closing_match'
        )
        with SettlementUnitOfWork(self, order) as settlement:
            order.cancel()
            if order.status is not OrderStatus.CANCELLED:
                raise SettlementInvariantError(
                    "auction residual did not enter CANCELLED state")
            # Reuse the existing serialized audit field without conflating the
            # lifecycle status with a validation rejection.
            order.reject_reason = reason.strip()
            if order.is_buy and order.frozen_amount > 0:
                self.account.unfreeze_cash(
                    order.frozen_amount, strict=True)
                order.frozen_amount = 0.0
            if order.is_sell and order.frozen_shares > 0:
                self.account.unfreeze_shares(
                    order.symbol, order.frozen_shares, strict=True)
                order.frozen_shares = 0
            self._complete_order(
                order,
                event_type=ExecutionEventType.ORDER_CANCELED,
                reason=reason,
                sim_time=event_time,
                phase=event_phase,
            )
            if order.order_id in self.account.orders:
                raise SettlementInvariantError(
                    "cancelled auction residual remained active")
            if (len(self.account.order_history) != history_length + 1
                    or self.account.order_history[-1] is not order):
                raise SettlementInvariantError(
                    "cancelled auction residual was not archived exactly once")
            self._assert_reservation_baselines(
                order.symbol, reservation_baselines)
            settlement.commit()
        self._remove_from_pending(order)

    # ==================== 撮合接口 ====================

    def execute_open_orders(self, bars: Dict[str, BarData],
                            match_at=None,
                            reject_missing: bool = True) -> List[Order]:
        for bar in bars.values():
            self.validate_execution_bar_price_band(bar)
        with self._matching_time_scope(match_at):
            return self._execute_open_orders_inner(
                bars, match_at=match_at, reject_missing=reject_missing)

    def _execute_open_orders_inner(self, bars: Dict[str, BarData],
                                   match_at=None,
                                   reject_missing: bool = True) -> List[Order]:
        """
        执行开盘单

        Args:
            bars: {symbol: BarData} 开盘K线数据
            reject_missing: 缺少 symbol 的 bar 时是否立即拒单。组合分钟按
                symbol 逐个喂入首根 bar 时应传 False，让其他 symbol 留在队列。

        Returns:
            已执行的订单列表
        """
        executed = []
        pending_orders = list(self._pending_open_orders)
        self._preflight_auction_caps(pending_orders, bars, match_at)
        sell_orders = [o for o in pending_orders if o.is_sell]
        buy_orders = [o for o in pending_orders if o.is_buy]

        # Always execute open sells first, then open buys.
        for order in sell_orders:
            if order.is_done:
                continue
            if (match_at is not None
                    and not self._is_order_eligible(order, match_at)):
                continue
            if order.symbol in bars:
                bar = bars[order.symbol]
                actual_match_at = match_at if match_at is not None else bar.trade_time
                if not self._is_order_eligible(order, actual_match_at):
                    continue
                with SettlementUnitOfWork(self, order) as auction:
                    if not self._materialize_deferred_sell(
                            order, sim_time=actual_match_at,
                            phase='opening_match'):
                        auction.commit()
                        self._remove_from_pending(order)
                        continue
                    # Opening-auction sells preserve the established cash/parity
                    # contract: the raw auction open is the executable price.
                    # Opening buys may opt into a slippage penalty; closing
                    # auction orders remain directionally slipped on both sides.
                    fill_price = bar.open
                    self._execute_order(
                        order, fill_price, bar, ref_price=bar.open)
                    self._cancel_auction_residual(
                        order,
                        "Open auction residual cancelled after one window")
                    auction.commit()
                if order.is_done:
                    self._remove_from_pending(order)
                executed.append(order)
            else:
                if reject_missing:
                    self._cancel_auction_residual(
                        order, "Open auction cancelled: no market data")

        for order in buy_orders:
            if order.is_done:
                continue
            if (match_at is not None
                    and not self._is_order_eligible(order, match_at)):
                continue
            if order.symbol not in bars:
                if reject_missing:
                    self._cancel_auction_residual(
                        order, "Open auction cancelled: no market data")
                continue

            bar = bars[order.symbol]
            actual_match_at = match_at if match_at is not None else bar.trade_time
            if not self._is_order_eligible(order, actual_match_at):
                continue
            if self.open_buy_resize_mode == 'legacy':
                with SettlementUnitOfWork(self, order) as auction:
                    attempted = self._execute_open_buy_legacy(order, bar)
                    if attempted:
                        self._cancel_auction_residual(
                            order,
                            "Open auction residual cancelled after one window")
                    auction.commit()
                if attempted:
                    executed.append(order)
                if order.is_done:
                    self._remove_from_pending(order)
                continue

            with SettlementUnitOfWork(self, order) as auction:
                attempted = self._execute_open_buy_auto(order, bar)
                if attempted:
                    self._cancel_auction_residual(
                        order,
                        "Open auction residual cancelled after one window")
                auction.commit()
            if attempted:
                executed.append(order)
            if order.is_done:
                self._remove_from_pending(order)

        self._pending_open_orders = [
            order for order in self._pending_open_orders if order.is_open
        ]
        return executed

    def _reject_open_order_no_data(self, order: Order) -> None:
        """Compatibility wrapper for terminal no-data auction cancellation."""
        self._cancel_auction_residual(
            order, "Auction cancelled: no market data")

    def _build_open_buy_target_shares(self, order: Order, sizing_price: float) -> int:
        """Convert requested shares/amount/percent into target shares at execution time.

        sizing_price: 委托量折算价。open_buy_sizing='limit_up' 时为当日涨停价
        （模拟挂涨停抢单、按最坏价折算可买量），'fill' 时为成交价。
        """
        if sizing_price <= 0:
            return 0

        requested_shares = order.requested_shares
        requested_amount = order.requested_amount if order.requested_amount is not None else order.amount
        requested_percent = order.requested_percent

        min_shares, inc = self._lot_rule(order.symbol)
        if requested_shares is not None:
            shares = (int(requested_shares) // inc) * inc
            return shares if shares >= min_shares else 0
        if requested_amount is not None:
            return self._round_buy_shares(order.symbol, requested_amount / sizing_price)
        if requested_percent is not None:
            target_amount = self.account.available_cash * float(requested_percent)
            return self._round_buy_shares(order.symbol, target_amount / sizing_price)
        return 0

    def _estimate_buy_total_cost(self, shares: int, price: float,
                                 order: Order = None) -> float:
        if shares <= 0 or price <= 0:
            return 0.0
        amount = shares * price
        if order is None:
            fees = self.account.calc_commission(amount, is_sell=False)
        else:
            fees = self._calculate_fill_fees(order, shares, price).total
        return positive_float(amount + fees, 'estimated buy total cost')

    def _calc_max_affordable_shares(self, cash: float, price: float,
                                    max_shares: Optional[int] = None,
                                    symbol: str = None,
                                    order: Order = None) -> int:
        if cash <= 0 or price <= 0:
            return 0

        _, inc = self._lot_rule(symbol) if symbol else (self.lot_size, self.lot_size)

        if max_shares is None:
            approx_max = int(cash / price / inc) * inc
        else:
            approx_max = max(0, (int(max_shares) // inc) * inc)
        if approx_max <= 0:
            return 0

        low_units = 0
        high_units = approx_max // inc
        while low_units < high_units:
            mid_units = (low_units + high_units + 1) // 2
            shares = mid_units * inc
            need = self._estimate_buy_total_cost(
                shares, price, order=order)
            if need <= cash + 1e-9:
                low_units = mid_units
            else:
                high_units = mid_units - 1
        return low_units * inc

    def _execute_open_buy_legacy(self, order: Order, bar: BarData) -> bool:
        """Legacy behavior: preserve pre-freeze/validation semantics."""
        if order.is_done:
            return False
        self._validate_execution_envelope(order, bar)
        with SettlementUnitOfWork(self, order) as settlement:
            result = self._execute_open_buy_legacy_inner(order, bar)
            settlement.commit()
            return result

    def _execute_open_buy_legacy_inner(self, order: Order,
                                       bar: BarData) -> bool:
        """Prepare and execute a legacy OPEN buy inside an outer UOW."""
        if order.is_buy and order.amount and order.shares == 0:
            open_price = bar.open * (1 + self.slippage)
            order.shares = int(order.amount / open_price / self.lot_size) * self.lot_size
            if order.shares <= 0:
                order.reject(
                    f"Insufficient amount: {order.amount:.2f} too small for open price {bar.open:.2f}"
                )
                self._open_buy_reject_invalid_count += 1
                if order.frozen_amount > 0:
                    self.account.unfreeze_cash(order.frozen_amount)
                    order.frozen_amount = 0
                self._complete_order(
                order, sim_time=self._event_time_for_bar(bar), phase='opening_match')
                return False

        if order.is_buy and order.shares > 0 and order.frozen_amount <= 0:
            required_per_share = bar.open * 1.01
            required_amount = order.shares * required_per_share
            if required_amount > self.account.available_cash:
                order.reject(
                    f"Insufficient cash at open: need {required_amount:.2f}, "
                    f"have {self.account.available_cash:.2f}"
                )
                self._open_buy_reject_insuff_cash_count += 1
                self._complete_order(
                order, sim_time=self._event_time_for_bar(bar), phase='opening_match')
                return False
            if not self.account.freeze_cash(required_amount):
                order.reject("Failed to freeze cash at open")
                self._open_buy_reject_insuff_cash_count += 1
                self._complete_order(
                order, sim_time=self._event_time_for_bar(bar), phase='opening_match')
                return False
            order.frozen_amount = required_amount
            self._record_reserve(
                order, required_amount, sim_time=self._event_time_for_bar(bar),
                phase='opening_match')

        self._execute_order(order, bar.open, bar)
        return True

    def _execute_open_buy_auto(self, order: Order, bar: BarData) -> bool:
        """开盘竞价买单（auto 模式）：见 _execute_auction_buy_auto"""
        open_price = float(bar.open)
        if self.open_buy_fill_mode == 'open+slip':
            fill_price = open_price * (1 + self.slippage)
        else:
            fill_price = open_price
        return self._execute_auction_buy_auto(order, bar, fill_price, ref_price=open_price)

    def _execute_close_buy_auto(self, order: Order, bar: BarData) -> bool:
        """收盘竞价买单（auto 模式）：卖先买后，执行时点按回款后现金重新定量。

        成交价 = close×(1+slippage)（收盘单历来含滑点）；
        委托量 = 现金÷当日涨停价（挂涨停抢收盘竞价语义，同开盘口径）。
        修复历史缺陷：收盘换仓买腿按卖出前现金定死股数，满仓换仓静默变 ~1% 仓位。
        """
        close_price = float(bar.close)
        fill_price = close_price * (1 + self.slippage)
        return self._execute_auction_buy_auto(order, bar, fill_price, ref_price=close_price)

    def _execute_auction_buy_auto(self, order: Order, bar: BarData,
                                  fill_price: float, ref_price: float) -> bool:
        """竞价买单通用执行（开盘/收盘共用）：执行时点定量 + 精确费用冻结。

        入口防御：已终结订单（撤销/拒绝/成交）绝不重新定量冻结——
        没有这道守卫时，撤单后的残留 pending 单会在此冻结资金后
        被 _execute_order 的 is_done 检查静默丢弃，资金永久泄漏。
        """
        if order.is_done:
            return False
        self._validate_execution_envelope(order, bar)
        with SettlementUnitOfWork(self, order) as settlement:
            result = self._execute_auction_buy_auto_inner(
                order, bar, fill_price, ref_price)
            settlement.commit()
            return result

    def _execute_auction_buy_auto_inner(self, order: Order, bar: BarData,
                                        fill_price: float, ref_price: float) -> bool:
        """竞价买单执行主体（见 _execute_auction_buy_auto）。

        成交价口径 (open_buy_fill_mode):
            'open+slip'(默认) = 竞价基准价×(1+slippage)——先卖后买的换仓摩擦惩罚；
            'open' = 基准价原价（旧口径；收盘单不受此开关影响，恒含滑点）。
        委托量口径 (open_buy_sizing):
            'limit_up'(默认) = 按当日涨停价折算可买量（挂涨停抢单语义，
            委托时不预知成交价，按最坏价折算保证可成交）；'fill' = 按成交价折算（旧口径）。
        """
        # Must fail before sizing_price, shares, or reservations are mutated.
        self._get_available_amount(order.symbol, bar)
        try:
            fill_price = positive_float(fill_price, 'fill_price')
            ref_price = positive_float(ref_price, 'ref_price')
            sizing_price = fill_price
            if self.open_buy_sizing == 'limit_up':
                limit_up = self._limit_up_price(
                    order.symbol, self._price_band_reference(bar))
                if limit_up is not None and limit_up > 0:
                    sizing_price = max(limit_up, fill_price)
            sizing_price = positive_float(sizing_price, 'sizing_price')
        except (ValueError, OverflowError) as exc:
            order.reject(f"Invalid open buy price: {exc}")
            self._open_buy_reject_invalid_count += 1
            self._complete_order(
                order, sim_time=self._event_time_for_bar(bar),
                phase=self._matching_phase(order, bar))
            return False
        # 审计可见性：委托量折算价落盘到订单（limit_up 折算会使 percent/amount
        # 口径的实际成交金额约缩水 涨跌幅/(1+涨跌幅)，此前无任何可见标记）
        order.sizing_price = round(float(sizing_price), 4)

        target_shares = self._build_open_buy_target_shares(order, sizing_price)
        if target_shares <= 0:
            order.reject("Invalid open buy target shares")
            self._open_buy_reject_invalid_count += 1
            self._complete_order(
                order, sim_time=self._event_time_for_bar(bar),
                phase=self._matching_phase(order, bar))
            return False

        cash_cap = self._calc_max_affordable_shares(
            self.account.available_cash, fill_price, symbol=order.symbol,
            order=order)

        # Cash determines the valid submitted quantity.  Auction liquidity is
        # a fill constraint, not an order-resizing rule: _execute_order may
        # partially fill this quantity and the one-window TIF then cancels the
        # residual with its reservation intact until cancellation.
        final_shares = min(target_shares, cash_cap)
        if final_shares <= 0:
            self._open_buy_reject_insuff_cash_count += 1
            order.reject("Open buy rejected: cash_limit")
            self._complete_order(
                order, sim_time=self._event_time_for_bar(bar),
                phase=self._matching_phase(order, bar))
            return False

        minimum, _ = self._lot_rule(order.symbol)
        if final_shares < minimum:
            self._open_buy_reject_insuff_cash_count += 1
            order.reject(
                "Open buy rejected: cash_limit below minimum lot "
                f"{minimum}"
            )
            self._complete_order(
                order, sim_time=self._event_time_for_bar(bar),
                phase=self._matching_phase(order, bar))
            return False

        resize_reasons = []
        if final_shares < target_shares:
            order.auto_resized = True
            order.resized_from_shares = target_shares
            order.resized_to_shares = final_shares
            if cash_cap < target_shares:
                resize_reasons.append("cash_limit")
            order.resize_reason = "+".join(resize_reasons) if resize_reasons else "cash_limit"
            self._open_buy_auto_resized_count += 1
            self._open_buy_resize_ratio_sum += (final_shares / target_shares)

        required_amount = self._estimate_buy_total_cost(
            final_shares, fill_price, order=order)
        if required_amount <= 0:
            order.reject("Open buy required amount invalid")
            self._open_buy_reject_invalid_count += 1
            self._complete_order(
                order, sim_time=self._event_time_for_bar(bar),
                phase=self._matching_phase(order, bar))
            return False
        if not self.account.freeze_cash(required_amount):
            order.reject(
                f"Insufficient cash at open: need {required_amount:.2f}, "
                f"have {self.account.available_cash:.2f}"
            )
            self._open_buy_reject_insuff_cash_count += 1
            self._complete_order(
                order, sim_time=self._event_time_for_bar(bar),
                phase=self._matching_phase(order, bar))
            return False

        order.shares = final_shares
        order.frozen_amount = required_amount
        self._record_reserve(
            order, required_amount, sim_time=self._event_time_for_bar(bar),
            phase=self._matching_phase(order, bar))
        # 涨跌停判定用未加滑点的竞价基准价（滑点是成本近似，不是市场价）
        self._execute_order(order, fill_price, bar, ref_price=ref_price)
        return True

    def _eligible_target_intents(self, match_at: datetime) -> List[TargetIntent]:
        terminal = {
            achievement.intent_id for achievement in self._target_achievements
        }
        trade_date = match_at.strftime('%Y%m%d')
        return sorted((
            intent for intent in self._target_intents
            if intent.intent_id not in terminal
            and intent.expire_date == trade_date
            and intent.eligible_from <= match_at
        ), key=lambda intent: (
            intent.symbol, intent.created_at, intent.batch_id, intent.intent_id))

    def _preflight_target_manual_close_conflict(
            self, match_at: datetime) -> None:
        manual = sorted((
            order for order in self.account.get_open_orders()
            if order.order_type is OrderType.CLOSE
            and order.order_id not in self._target_order_intents
            and self._is_order_eligible(order, match_at)
        ), key=lambda order: order.order_id)
        if manual:
            ids = ','.join(order.order_id for order in manual)
            raise SettlementInvariantError(
                "eligible manual CLOSE orders conflict with target intents "
                f"in the same window: {ids}")

    def _target_reference_state(self, intents, bars):
        targeted_symbols = {intent.symbol for intent in intents}
        reference_prices = {}
        for symbol in sorted(targeted_symbols):
            bar = bars.get(symbol)
            if bar is None:
                raise SettlementInvariantError(
                    f"target intent has no close bar: {symbol}")
            if bar.symbol != symbol:
                raise SettlementInvariantError(
                    "target close bar mapping key does not match BarData.symbol")
            reference_prices[symbol] = positive_float(
                bar.close, f'target reference price[{symbol}]')

        for symbol, position in sorted(self.account.positions.items()):
            if position.shares <= 0 or symbol in reference_prices:
                continue
            bar = bars.get(symbol)
            if bar is not None:
                if bar.symbol != symbol:
                    raise SettlementInvariantError(
                        "retained close bar mapping key does not match "
                        "BarData.symbol")
                price = bar.close
            elif position.current_price > 0:
                price = position.current_price
            else:
                raise SettlementInvariantError(
                    f"retained holding has no close reference price: {symbol}")
            reference_prices[symbol] = positive_float(
                price, f'target reference price[{symbol}]')

        reference_nav = positive_float(
            self.account.estimate_total_asset(reference_prices),
            'target reference NAV')
        retained_value = 0.0
        for symbol, position in sorted(self.account.positions.items()):
            if position.shares <= 0 or symbol in targeted_symbols:
                continue
            retained_value += position.shares * reference_prices[symbol]
        stale_reference_symbols = {
            symbol for symbol, position in self.account.positions.items()
            if position.shares > 0
            and symbol not in targeted_symbols
            and symbol not in bars
        }
        return (
            reference_nav,
            reference_prices,
            retained_value,
            frozenset(stale_reference_symbols),
        )

    def _build_target_plans(self, intents, reference_nav, reference_prices):
        plans = {}
        for intent in intents:
            price = reference_prices[intent.symbol]
            position = self.account.positions.get(intent.symbol)
            current_shares = position.shares if position is not None else 0
            target_notional = reference_nav * intent.target_weight
            current_value = current_shares * price
            delta_value = target_notional - current_value
            side = TargetPlanSide.NONE
            desired_shares = 0
            reasons = []
            if delta_value > 1e-8:
                side = TargetPlanSide.BUY
                if delta_value < intent.min_trade_amount:
                    reasons.append('min_trade_amount')
                else:
                    raw_shares = delta_value / price
                    desired_shares = self._round_buy_shares(
                        intent.symbol, raw_shares)
                    if desired_shares <= 0:
                        reasons.append('minimum_lot')
                    elif desired_shares * price < delta_value - 1e-8:
                        reasons.append('lot_rounding')
            elif delta_value < -1e-8:
                side = TargetPlanSide.SELL
                if (-delta_value < intent.min_trade_amount
                        and intent.target_weight != 0.0):
                    reasons.append('min_trade_amount')
                elif current_shares > 0:
                    _, increment = self._lot_rule(intent.symbol)
                    raw_shares = (-delta_value) / price
                    desired_shares = min(
                        current_shares,
                        int(ceil(raw_shares / increment)) * increment,
                    )
                    if desired_shares * price > -delta_value + 1e-8:
                        reasons.append('lot_rounding')
            plans[intent.intent_id] = {
                'intent': intent,
                'reference_price': price,
                'side': side,
                'desired_shares': desired_shares,
                'planned_shares': desired_shares,
                'order_ids': [],
                'reasons': reasons,
            }
        return plans

    def _target_worst_buy_price(self, symbol: str, bar: BarData) -> float:
        modeled_fill = positive_float(
            bar.close * (1 + self.slippage), 'target modeled buy price')
        exempt = self._is_limit_check_exempt(symbol)
        self._require_bar_pre_close(symbol, bar)
        limit_up = None if exempt else self._limit_up_price(
            symbol, self._price_band_reference(bar))
        return positive_float(
            limit_up if limit_up is not None else modeled_fill,
            'target worst legal buy price')

    def _target_buy_reserve_cost(self, symbol: str, shares: int,
                                 price: float) -> float:
        if shares <= 0:
            return 0.0
        notional = positive_product(
            shares, price, 'target reserve notional')
        fees = self.fee_engine.calculate_fill(
            notional,
            parent_order_id=f"target-reserve-{symbol}",
            side=FeeSide.BUY,
            prior_state=None,
            stamp_duty_rate=self.account.resolve_stamp_rate(
                symbol, self.current_date),
        )
        return positive_float(
            notional + fees.total, 'target buy reserve cost')

    def _target_scaled_buy_shares(self, buy_plans, bars) -> dict:
        desired = {}
        worst_prices = {}
        increments = {}
        for plan in buy_plans:
            intent = plan['intent']
            symbol = intent.symbol
            desired[symbol] = plan['desired_shares']
            worst_prices[symbol] = self._target_worst_buy_price(
                symbol, bars[symbol])
            increments[symbol] = self._lot_rule(symbol)

        def allocate(ratio):
            allocation = {}
            total = 0.0
            for symbol in sorted(desired):
                minimum, increment = increments[symbol]
                shares = int(desired[symbol] * ratio // increment) * increment
                if shares < minimum:
                    shares = 0
                allocation[symbol] = shares
                total += self._target_buy_reserve_cost(
                    symbol, shares, worst_prices[symbol])
            return allocation, total

        full_allocation, full_cost = allocate(1.0)
        cash = nonnegative_float(
            self.account.available_cash, 'target available cash')
        # Use the same exact affordability boundary as Account.freeze_cash.
        # Accepting even a tiny modeled overage here would make planning and
        # atomic reservation disagree downstream.
        if full_cost <= cash:
            return {
                symbol: (shares, worst_prices[symbol], False)
                for symbol, shares in full_allocation.items()
            }

        ratio = cash / full_cost if full_cost > 0 else 0.0
        allocation, scaled_cost = allocate(ratio)
        if scaled_cost > cash:
            low, high = 0.0, ratio
            for _ in range(64):
                middle = (low + high) / 2
                candidate, candidate_cost = allocate(middle)
                if candidate_cost <= cash:
                    low = middle
                    allocation = candidate
                else:
                    high = middle
        return {
            symbol: (shares, worst_prices[symbol], True)
            for symbol, shares in allocation.items()
        }

    def _submit_target_close_order(self, plan, *, shares: int,
                                   match_at: datetime,
                                   reserve_price: float = None) -> Order:
        intent = plan['intent']
        side = plan['side']
        if shares <= 0 or side is TargetPlanSide.NONE:
            raise SettlementInvariantError(
                "target close order requires a positive directional plan")
        order_side = (
            OrderSide.BUY if side is TargetPlanSide.BUY else OrderSide.SELL)
        order = Order(
            symbol=intent.symbol,
            side=order_side,
            order_type=OrderType.CLOSE,
            shares=shares,
            create_date=self.current_date,
            note=intent.note,
            requested_shares=shares if order_side is OrderSide.BUY else None,
            sizing_price=reserve_price if order_side is OrderSide.BUY else None,
        )
        if order_side is OrderSide.BUY:
            required = self._estimate_buy_total_cost(
                shares, reserve_price, order=order)
            if not self.account.freeze_cash(required):
                raise SettlementInvariantError(
                    "proportional target buy reservation exceeded cash")
            order.frozen_amount = required
        else:
            frozen = self.account.freeze_shares(intent.symbol, shares)
            if frozen != shares:
                raise SettlementInvariantError(
                    "target sell reservation changed after planning")
            order.frozen_shares = shares
        order.submit()
        with self.order_creation_scope(
                'target_planning', match_at,
                eligible_from=match_at,
                expire_date=match_at.strftime('%Y%m%d')):
            self.account.add_order(order)
            self._stamp_order(order, sim_time=match_at)
        self._pending_close_orders.append(order)
        self._target_order_intents[order.order_id] = intent.intent_id
        plan['order_ids'].append(order.order_id)
        return order

    @staticmethod
    def _target_order_outcome_reasons(order: Order) -> List[str]:
        reasons = []
        text = (order.reject_reason or '').lower()
        if 'limit-up blocked buy' in text:
            reasons.append('limit_up_blocked_buy')
        elif 'limit-down blocked sell' in text:
            reasons.append('limit_down_blocked_sell')
        if order.filled_shares < order.shares:
            if 'auction residual' in text or 'liquidity' in text:
                reasons.append('liquidity_cap')
            elif not reasons:
                reasons.append('unfilled_close_window')
        return reasons

    def _append_target_achievement(self, intent, *, reference_nav,
                                   reference_price, side, planned_shares,
                                   order_ids=(), reasons=(),
                                   status_override=None):
        position = self.account.positions.get(intent.symbol)
        final_shares = position.shares if position is not None else 0
        achieved_value = final_shares * reference_price
        achieved_weight = achieved_value / reference_nav
        gap_weight = achieved_weight - intent.target_weight
        normalized_reasons = list(dict.fromkeys(reasons))
        if status_override is not None:
            status = status_override
        elif abs(gap_weight) <= 1e-12:
            status = TargetAchievementStatus.ACHIEVED
        elif side is TargetPlanSide.NONE:
            status = TargetAchievementStatus.NOOP
        elif gap_weight < 0:
            status = TargetAchievementStatus.UNDER_TARGET
        else:
            status = TargetAchievementStatus.OVER_TARGET
        if status is not TargetAchievementStatus.ACHIEVED and not normalized_reasons:
            normalized_reasons.append(
                'lot_rounding' if side is not TargetPlanSide.NONE
                else 'no_trade_required')
        # Completed orders live in history rather than account.orders.
        history_by_id = {
            order.order_id: order for order in self.account.order_history
        }
        filled_shares = sum(
            (self.account.get_order(order_id)
             or history_by_id.get(order_id)).filled_shares
            for order_id in order_ids
            if (self.account.get_order(order_id)
                or history_by_id.get(order_id)) is not None
        )
        achievement = TargetAchievement(
            intent_id=intent.intent_id,
            batch_id=intent.batch_id,
            trade_date=intent.expire_date,
            symbol=intent.symbol,
            target_weight=intent.target_weight,
            reference_nav=reference_nav,
            reference_price=reference_price,
            target_notional=reference_nav * intent.target_weight,
            planned_side=side,
            planned_shares=planned_shares,
            order_ids=tuple(order_ids),
            filled_shares=filled_shares,
            achieved_value=achieved_value,
            achieved_weight=achieved_weight,
            gap_weight=gap_weight,
            cap_respected=gap_weight <= 1e-12,
            status=status,
            reasons=tuple(normalized_reasons),
        )
        self._target_achievements.append(achievement)
        return achievement

    def _execute_target_close_batch(self, intents, bars, *, match_at,
                                    reject_missing):
        self._preflight_target_manual_close_conflict(match_at)
        latest_by_symbol = {}
        for intent in intents:
            prior = latest_by_symbol.get(intent.symbol)
            if prior is None or intent.ordering_key > prior.ordering_key:
                latest_by_symbol[intent.symbol] = intent
        active = sorted(
            latest_by_symbol.values(),
            key=lambda intent: (intent.symbol, intent.intent_id))
        superseded_ids = {
            intent.intent_id for intent in intents
            if latest_by_symbol[intent.symbol].intent_id != intent.intent_id
        }
        missing_ids = {
            intent.intent_id for intent in intents
            if intent.symbol not in bars
        }
        processable_intents = [
            intent for intent in intents
            if intent.intent_id not in missing_ids
        ]
        processable_active = [
            intent for intent in active
            if intent.intent_id not in missing_ids
        ]

        (reference_nav, reference_prices, retained_value,
         stale_reference_symbols) = (
            self._target_reference_state(processable_intents, bars))
        plans = self._build_target_plans(
            processable_active, reference_nav, reference_prices)
        if stale_reference_symbols:
            for plan in plans.values():
                plan['reasons'].append('stale_reference_mark')

        def append_missing(intent):
            is_superseded = intent.intent_id in superseded_ids
            reasons = ['superseded'] if is_superseded else []
            reasons.append('no_close_bar')
            if intent.symbol in stale_reference_symbols:
                reasons.append('stale_reference_mark')
            return self._append_target_achievement(
                intent,
                reference_nav=reference_nav,
                reference_price=reference_prices.get(intent.symbol, 0.0),
                side=TargetPlanSide.NONE,
                planned_shares=0,
                reasons=tuple(reasons),
                status_override=(
                    TargetAchievementStatus.SUPERSEDED
                    if is_superseded
                    else TargetAchievementStatus.BLOCKED
                ),
            )

        capacity = (
            sum(intent.target_weight for intent in processable_active)
            + retained_value / reference_nav
        )
        if capacity > 1.0 + 1e-12:
            for intent in intents:
                if intent.intent_id in missing_ids:
                    append_missing(intent)
                    continue
                if intent.intent_id in superseded_ids:
                    self._append_target_achievement(
                        intent,
                        reference_nav=reference_nav,
                        reference_price=reference_prices[intent.symbol],
                        side=TargetPlanSide.NONE,
                        planned_shares=0,
                        reasons=(
                            ('superseded', 'stale_reference_mark')
                            if stale_reference_symbols
                            else ('superseded',)
                        ),
                        status_override=TargetAchievementStatus.SUPERSEDED,
                    )
                else:
                    plan = plans[intent.intent_id]
                    self._append_target_achievement(
                        intent,
                        reference_nav=reference_nav,
                        reference_price=reference_prices[intent.symbol],
                        side=plan['side'],
                        planned_shares=plan['planned_shares'],
                        reasons=(
                            ('account_weight_cap', 'stale_reference_mark')
                            if stale_reference_symbols
                            else ('account_weight_cap',)
                        ),
                        status_override=TargetAchievementStatus.INFEASIBLE,
                    )
            return []

        executed = []
        sell_plans = [
            plans[intent.intent_id] for intent in processable_active
            if plans[intent.intent_id]['side'] is TargetPlanSide.SELL
        ]
        for plan in sell_plans:
            desired = plan['desired_shares']
            position = self.account.positions.get(plan['intent'].symbol)
            available = position.available_shares if position is not None else 0
            planned = min(desired, available)
            plan['planned_shares'] = planned
            if planned < desired:
                plan['reasons'].append('t1_unavailable')
            if not self.allow_odd_lot_sell and planned > 0:
                legal_planned = (
                    planned // self.lot_size) * self.lot_size
                if legal_planned < planned:
                    plan['reasons'].append('odd_lot_sell_disabled')
                planned = legal_planned
                plan['planned_shares'] = planned
            if planned > 0:
                self._submit_target_close_order(
                    plan, shares=planned, match_at=match_at)
        if any(plan['order_ids'] for plan in sell_plans):
            executed.extend(self._execute_close_orders_inner(
                bars, match_at=match_at, reject_missing=reject_missing))

        buy_plans = [
            plans[intent.intent_id] for intent in processable_active
            if plans[intent.intent_id]['side'] is TargetPlanSide.BUY
            and plans[intent.intent_id]['desired_shares'] > 0
        ]
        scaled = self._target_scaled_buy_shares(buy_plans, bars)
        for plan in buy_plans:
            symbol = plan['intent'].symbol
            planned, worst_price, cash_scaled = scaled[symbol]
            plan['planned_shares'] = planned
            if cash_scaled:
                plan['reasons'].append('cash_scaled')
            if planned > 0:
                self._submit_target_close_order(
                    plan, shares=planned, match_at=match_at,
                    reserve_price=worst_price)
        if any(plan['order_ids'] for plan in buy_plans):
            executed.extend(self._execute_close_orders_inner(
                bars, match_at=match_at, reject_missing=reject_missing))

        for plan in plans.values():
            for order_id in plan['order_ids']:
                order = next((
                    candidate for candidate in self.account.order_history
                    if candidate.order_id == order_id
                ), self.account.get_order(order_id))
                if order is not None:
                    plan['reasons'].extend(
                        self._target_order_outcome_reasons(order))

        for intent in intents:
            if intent.intent_id in missing_ids:
                append_missing(intent)
                continue
            if intent.intent_id in superseded_ids:
                self._append_target_achievement(
                    intent,
                    reference_nav=reference_nav,
                    reference_price=reference_prices[intent.symbol],
                    side=TargetPlanSide.NONE,
                    planned_shares=0,
                    reasons=(
                        ('superseded', 'stale_reference_mark')
                        if stale_reference_symbols
                        else ('superseded',)
                    ),
                    status_override=TargetAchievementStatus.SUPERSEDED,
                )
                continue
            plan = plans[intent.intent_id]
            self._append_target_achievement(
                intent,
                reference_nav=reference_nav,
                reference_price=reference_prices[intent.symbol],
                side=plan['side'],
                planned_shares=plan['planned_shares'],
                order_ids=tuple(plan['order_ids']),
                reasons=tuple(plan['reasons']),
            )
        return executed

    def execute_close_orders(self, bars: Dict[str, BarData],
                             match_at=None,
                             reject_missing: bool = True) -> List[Order]:
        for bar in bars.values():
            self.validate_execution_bar_price_band(bar)
        effective_match_at = match_at
        if effective_match_at is None:
            pending_intent_ids = {
                intent.intent_id for intent in self._target_intents
            } - {
                achievement.intent_id
                for achievement in self._target_achievements
            }
            if pending_intent_ids:
                bar_times = {
                    self._coerce_sim_time(bar.trade_time)
                    for bar in bars.values()
                }
                if len(bar_times) != 1:
                    raise SettlementInvariantError(
                        "target close execution requires one exact match_at")
                effective_match_at = next(iter(bar_times))
        with self._matching_time_scope(effective_match_at):
            if effective_match_at is None:
                return self._execute_close_orders_inner(
                    bars, match_at=match_at,
                    reject_missing=reject_missing)
            match_dt = self._coerce_sim_time(effective_match_at)
            intents = self._eligible_target_intents(match_dt)
            if not intents:
                return self._execute_close_orders_inner(
                    bars, match_at=match_dt,
                    reject_missing=reject_missing)
            with SettlementUnitOfWork(self) as target_batch:
                executed = self._execute_target_close_batch(
                    intents, bars, match_at=match_dt,
                    reject_missing=reject_missing)
                target_batch.commit()
                return executed

    def _execute_close_orders_inner(self, bars: Dict[str, BarData],
                                    match_at=None,
                                    reject_missing: bool = True) -> List[Order]:
        """
        执行收盘单

        Args:
            bars: {symbol: BarData} K线数据
            reject_missing: 缺少 symbol 的 bar 时是否立即取消；分 symbol
                喂入收盘竞价数据时传 False，待该 symbol 的窗口到达再处理。

        Returns:
            已执行的订单列表
        """
        executed = []

        # 收盘竞价与开盘同口径：先执行全部卖单（回款立即入账），再执行买单
        pending = list(self._pending_close_orders)
        self._preflight_auction_caps(pending, bars, match_at)
        sell_orders = [o for o in pending if o.is_sell]
        buy_orders = [o for o in pending if o.is_buy]

        for order in sell_orders:
            if order.is_done:
                continue
            if (match_at is not None
                    and not self._is_order_eligible(order, match_at)):
                continue
            if order.symbol in bars:
                bar = bars[order.symbol]
                actual_match_at = match_at if match_at is not None else bar.trade_time
                if not self._is_order_eligible(order, actual_match_at):
                    continue
                with SettlementUnitOfWork(self, order) as auction:
                    if not self._materialize_deferred_sell(
                            order, sim_time=actual_match_at,
                            phase='closing_match'):
                        auction.commit()
                        self._remove_from_pending(order)
                        continue
                    fill_price = bar.close * (1 - self.slippage)
                    self._execute_order(
                        order, fill_price, bar, ref_price=bar.close)
                    self._cancel_auction_residual(
                        order,
                        "Close auction residual cancelled after one window")
                    auction.commit()
                if order.is_done:
                    self._remove_from_pending(order)
                executed.append(order)
            else:
                if reject_missing:
                    self._cancel_auction_residual(
                        order, "Close auction cancelled: no market data")

        for order in buy_orders:
            if order.is_done:
                continue
            if (match_at is not None
                    and not self._is_order_eligible(order, match_at)):
                continue
            if order.symbol not in bars:
                if reject_missing:
                    self._cancel_auction_residual(
                        order, "Close auction cancelled: no market data")
                continue
            bar = bars[order.symbol]
            actual_match_at = match_at if match_at is not None else bar.trade_time
            if not self._is_order_eligible(order, actual_match_at):
                continue
            deferred = (order.frozen_amount <= 0 and order.shares == 0 and (
                order.requested_shares is not None
                or order.requested_amount is not None
                or order.requested_percent is not None
            ))
            if deferred:
                # auto 模式：执行时点定量（含收盘卖回款）
                with SettlementUnitOfWork(self, order) as auction:
                    attempted = self._execute_close_buy_auto(order, bar)
                    if attempted:
                        self._cancel_auction_residual(
                            order,
                            "Close auction residual cancelled after one window")
                    auction.commit()
                if attempted:
                    executed.append(order)
            else:
                # legacy 模式：提交时已定量冻结，按原口径执行
                with SettlementUnitOfWork(self, order) as auction:
                    fill_price = bar.close * (1 + self.slippage)
                    self._execute_order(
                        order, fill_price, bar, ref_price=bar.close)
                    self._cancel_auction_residual(
                        order,
                        "Close auction residual cancelled after one window")
                    auction.commit()
                executed.append(order)
            if order.is_done:
                self._remove_from_pending(order)

        self._pending_close_orders = [
            order for order in self._pending_close_orders if order.is_open
        ]
        return executed

    def _execute_order(self, order: Order, fill_price: float, bar: BarData,
                       ref_price: float = None) -> None:
        """Execute one match as a single rollback boundary.

        Legacy callers may insert an unstamped :class:`Order` directly into an
        account before invoking this private method.  Stamping that order is
        therefore part of the same unit of work as all preconditions and the
        eventual fill/rejection; a failed precondition must not leak a new ID,
        simulated timestamp, reservation event, or journal sequence.
        """
        with SettlementUnitOfWork(self, order) as execution:
            result = self._execute_order_inner(
                order, fill_price, bar, ref_price=ref_price)
            execution.commit()
            return result

    def _execute_order_inner(self, order: Order, fill_price: float,
                             bar: BarData,
                             ref_price: float = None) -> None:
        """
        执行订单（含成交额限制）

        Args:
            order: 订单
            fill_price: 成交价格（含滑点）
            bar: K线数据
            ref_price: 涨跌停判定参照价（未加滑点的市场价；None 时退回 fill_price）
        """
        if order.is_done:
            return

        event_time = self._event_time_for_bar(bar)
        event_phase = self._matching_phase(order, bar)
        self._ensure_order_stamped(order, before_time=event_time)

        # Matching-derived prices are validated before any cash/share state is
        # released or consumed.  A finite input can still overflow when it is
        # multiplied by a large share count, so the notional is checked below
        # once the executable quantity is known.
        fill_price = positive_float(fill_price, 'fill_price')
        if ref_price is not None:
            ref_price = positive_float(ref_price, 'ref_price')
        reservation_baselines = self._validate_execution_envelope(order, bar)

        # 检查涨跌停限制
        can_trade, reason = self._check_limit(
            order.symbol, bar, order.is_buy, fill_price, ref_price=ref_price,
            order_type=order.order_type)
        if not can_trade:
            # A locked price band is a property of this bar, not a terminal
            # validation result for the order.  Preserve reservations and let
            # a later minute/bar retry; one-shot auction/day expiry code owns
            # the eventual terminal cancellation.
            order.reject_reason = reason
            logger.info(f"Order vetoed for this bar: {order.symbol} - {reason}")
            return

        if (order.reject_reason or '').startswith(
                ('Limit-up blocked buy', 'Limit-down blocked sell')):
            order.reject_reason = None

        # Slippage is a cost model, not permission to print outside the legal
        # exchange band.  Saturate only in the adverse direction; a favorable
        # off-band price indicates a broken matching/data invariant.
        fill_price = self._saturate_modeled_fill_to_limit_band(
            order.symbol, bar, order.is_buy, fill_price)

        # ==================== 成交额限制检查 ====================
        unfilled = order.unfilled_shares
        if unfilled <= 0:
            return

        # 获取本K线剩余可用成交额（所有订单类型统一受限）
        available_amount = self._get_available_amount(order.symbol, bar)
        if available_amount <= 0:
            # 本K线额度已用完，等待下一根K线
            return

        # 计算成交额限制下的最大可成交股数（按申报递增单位取整；部分成交不受最低申报量约束）
        _, lot_inc = self._lot_rule(order.symbol)
        max_shares_by_amount = int(available_amount / fill_price / lot_inc) * lot_inc
        if max_shares_by_amount <= 0:
            return

        actual_shares = min(unfilled, max_shares_by_amount)

        if order.is_buy:
            # ==================== 买入 ====================
            affordable_shares = self._calc_max_affordable_shares(
                order.frozen_amount, fill_price, max_shares=actual_shares,
                symbol=order.symbol, order=order
            )
            frozen_resize_reason = None
            if affordable_shares < actual_shares:
                # 冻结资金按估价冻结，实际成交价更高（或最低佣金侵蚀）时欠额成交
                # ——打审计标记，不再完全静默
                if 'frozen_insufficient' not in (order.resize_reason or ''):
                    frozen_resize_reason = (
                        (order.resize_reason + '+' if order.resize_reason else '')
                        + 'frozen_insufficient')
            actual_shares = min(actual_shares, affordable_shares)

            if actual_shares <= 0:
                # 历史坑：小额单（如 <500 元）冻结资金被最低佣金侵蚀后连最小
                # 申报增量都买不起。不能在此立即拒单——审查确认回归：分钟频率下
                # 后续 bar 跳空低开（LIMIT 价格改善分支）可使 affordable 由 0
                # 转正，旧引擎实测可成交。保持挂单，日终仍未成交时由
                # cancel_day_end_orders 依 frozen_insufficient 审计标记转显式拒单
                if frozen_resize_reason is not None:
                    order.resize_reason = frozen_resize_reason
                return

            fill_notional = positive_product(
                actual_shares, fill_price, 'fill notional')
            fees = self._calculate_fill_fees(
                order, actual_shares, fill_price)
            self._validate_fill_preconditions(
                order, bar, actual_shares, fill_price, fees)
            fill_state_before = self._capture_fill_state(order)
            used_before = self._bar_used_amount.get(order.symbol, 0.0)
            expected_cost = positive_float(
                fill_notional + fees.total, 'settlement total cost')
            is_t0 = self._is_t0(order.symbol)
            reservation_key = self._reservation_key(order)
            journal_reserved = nonnegative_float(
                self._journal_reservations.get(reservation_key, 0.0),
                'journal reservation')
            will_complete = (
                order.filled_shares + actual_shares == order.shares)
            reservation_release = (
                journal_reserved if will_complete else expected_cost)
            prepared_events = self._prepare_fill_events(
                order,
                shares=actual_shares,
                price=fill_price,
                notional=fill_notional,
                fees=fees,
                cash_amount=expected_cost,
                direction='debit',
                sim_time=event_time,
                phase=event_phase,
                reservation_release=reservation_release,
            )

            with SettlementUnitOfWork(self, order) as settlement:
                if frozen_resize_reason is not None:
                    order.resize_reason = frozen_resize_reason
                cost = self.account.buy(
                    order.symbol,
                    actual_shares,
                    fill_price,
                    self.current_date,
                    t0=is_t0,
                    fee_total=fees.total,
                    strict_settlement=True,
                )
                settled_position = self.account.positions.get(order.symbol)
                if settled_position is not None:
                    settled_position.last_update = event_time
                if not self._amounts_close(cost, expected_cost):
                    raise SettlementInvariantError(
                        "account buy cost does not match settlement plan")
                order.fill(actual_shares, fill_price)
                order.record_fees(
                    fees.commission,
                    fees.stamp_duty,
                    fees.transfer_fee,
                    fees.total,
                )
                remaining_frozen = finite_float(
                    order.frozen_amount - cost,
                    'remaining order frozen amount')
                if remaining_frozen < -1e-7:
                    raise SettlementInvariantError(
                        "buy settlement consumed more than its reservation")
                order.frozen_amount = max(0.0, remaining_frozen)

                self._consume_amount(order.symbol, fill_notional)
                expected_used = nonnegative_float(
                    used_before + fill_notional, 'expected bar used amount')
                actual_used = nonnegative_float(
                    self._bar_used_amount.get(order.symbol, 0.0),
                    'bar used amount')
                if not self._amounts_close(actual_used, expected_used):
                    raise SettlementInvariantError(
                        "bar liquidity consumption does not match settlement")

                if order.is_done:
                    if order.frozen_amount > 0:
                        self.account.unfreeze_cash(
                            order.frozen_amount, strict=True)
                        order.frozen_amount = 0
                    self._complete_order(order)
                self._order_fee_states[order.order_id] = fees.next_state
                self._assert_buy_fill_postconditions(
                    order,
                    fill_state_before,
                    actual_shares,
                    fill_notional,
                    cost,
                    fees,
                    t0=is_t0,
                )
                self._assert_reservation_baselines(
                    order.symbol, reservation_baselines)
                order.update_time = event_time
                self._commit_fill_events(
                    order, prepared_events, reservation_release)
                settlement.commit()

            logger.info(
                f"Buy filled: {order.symbol} {actual_shares} @ {fill_price:.2f}, "
                f"cost={cost:.2f}, remaining_frozen={order.frozen_amount:.2f}"
            )

        else:
            # ==================== 卖出 ====================
            fill_notional = positive_product(
                actual_shares, fill_price, 'fill notional')
            fees = self._calculate_fill_fees(
                order, actual_shares, fill_price)
            self._validate_fill_preconditions(
                order, bar, actual_shares, fill_price, fees)
            fill_state_before = self._capture_fill_state(order)
            used_before = self._bar_used_amount.get(order.symbol, 0.0)
            expected_proceeds = finite_float(
                fill_notional - fees.total, 'expected net proceeds')
            if expected_proceeds <= 0:
                reason = (
                    "Sell rejected: fees are greater than or equal to "
                    "the executable notional"
                )
                if order.filled_shares > 0:
                    self._cancel_active_order(
                        order, reason=reason, sim_time=event_time,
                        phase=event_phase)
                else:
                    self._reject_active_order(
                        order, reason=reason, sim_time=event_time,
                        phase=event_phase)
                return
            reservation_release = float(actual_shares)
            prepared_events = self._prepare_fill_events(
                order,
                shares=actual_shares,
                price=fill_price,
                notional=fill_notional,
                fees=fees,
                cash_amount=expected_proceeds,
                direction='credit',
                sim_time=event_time,
                phase=event_phase,
                reservation_release=reservation_release,
            )

            with SettlementUnitOfWork(self, order) as settlement:
                # 先严格解冻要卖出的股票，再执行精确数量的卖出。
                self.account.unfreeze_shares(
                    order.symbol, actual_shares, strict=True)
                order.frozen_shares = nonnegative_int(
                    order.frozen_shares - actual_shares,
                    'order frozen shares')

                proceeds = self.account.sell(
                    order.symbol,
                    actual_shares,
                    fill_price,
                    trade_date=self.current_date,
                    fee_total=fees.total,
                    strict_settlement=True,
                )
                settled_position = self.account.positions.get(order.symbol)
                if settled_position is not None:
                    settled_position.last_update = event_time
                if not self._amounts_close(proceeds, expected_proceeds):
                    raise SettlementInvariantError(
                        "account sell proceeds do not match settlement plan")

                order.fill(actual_shares, fill_price)
                order.record_fees(
                    fees.commission,
                    fees.stamp_duty,
                    fees.transfer_fee,
                    fees.total,
                )
                self._consume_amount(order.symbol, fill_notional)
                expected_used = nonnegative_float(
                    used_before + fill_notional, 'expected bar used amount')
                actual_used = nonnegative_float(
                    self._bar_used_amount.get(order.symbol, 0.0),
                    'bar used amount')
                if not self._amounts_close(actual_used, expected_used):
                    raise SettlementInvariantError(
                        "bar liquidity consumption does not match settlement")

                if order.is_done:
                    if order.frozen_shares > 0:
                        self.account.unfreeze_shares(
                            order.symbol, order.frozen_shares, strict=True)
                        order.frozen_shares = 0
                    self._complete_order(order)
                self._order_fee_states[order.order_id] = fees.next_state
                self._assert_sell_fill_postconditions(
                    order,
                    fill_state_before,
                    actual_shares,
                    fill_notional,
                    proceeds,
                    fees,
                )
                self._assert_reservation_baselines(
                    order.symbol, reservation_baselines)
                order.update_time = event_time
                self._commit_fill_events(
                    order, prepared_events, reservation_release)
                settlement.commit()

            logger.info(f"Sell filled: {order.symbol} {actual_shares} @ {fill_price:.2f}, "
                       f"proceeds={proceeds:.2f}, remaining_frozen_shares={order.frozen_shares}")

    # ==================== 查询接口 ====================

    def get_cash(self) -> float:
        """获取可用现金"""
        return self.account.available_cash

    def get_total_asset(self) -> float:
        """获取总资产"""
        return self.account.total_asset

    def get_position(self, symbol: str) -> Optional[Position]:
        """获取持仓"""
        return self.account.get_position(symbol)

    def get_positions(self) -> Dict[str, Position]:
        """获取所有持仓"""
        return self.account.get_positions()

    def get_order(self, order_id: str) -> Optional[Order]:
        """获取订单"""
        return self.account.get_order(order_id)

    def get_open_orders(self) -> List[Order]:
        """获取未完成订单"""
        return self.account.get_open_orders()

    def get_eligible_open_orders(self, match_at) -> List[Order]:
        """返回在给定模拟时点已经获得撮合资格的未完成订单。"""
        return [
            order for order in self.account.get_open_orders()
            if self._is_order_eligible(order, match_at)
        ]

    def get_open_buy_stats(self) -> Dict[str, Any]:
        """Execution audit stats for open buy orders."""
        avg_resize_ratio = (
            self._open_buy_resize_ratio_sum / self._open_buy_auto_resized_count
            if self._open_buy_auto_resized_count > 0 else 1.0
        )
        return {
            'open_buy_resize_mode': self.open_buy_resize_mode,
            'open_buy_total': int(self._open_buy_total),
            'open_buy_auto_resized_count': int(self._open_buy_auto_resized_count),
            'open_buy_reject_insuff_cash_count': int(self._open_buy_reject_insuff_cash_count),
            'open_buy_reject_invalid_count': int(self._open_buy_reject_invalid_count),
            'open_buy_avg_resize_ratio': float(avg_resize_ratio),
        }
