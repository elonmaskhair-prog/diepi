"""
回测引擎

单股票回测执行器
"""

import logging
import hashlib
import json
import math
import sys
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
import pandas as pd

from .context import Context
from ..strategy.base import Strategy, BarData, OpenBarData
from ..broker import (
    Account,
    CashAuditBundle,
    CashReplaySeed,
    cash_replay_trade_records,
)
from ..broker.target_execution import TargetExecutionBundle
from ..broker.broker import BarData as BrokerBarData
from ..data import DataProvider
from ..data.contract import (
    AdjustmentFactorIdentity,
    AdjustmentFactorError,
    adjustment_factor_assumption_value,
)
from ..config import PRICE_MODE_STRATEGY, PRICE_MODE_EXECUTION
from .minute_resampler import resample_minute_data
from .price_mode import ADJUSTMENT_FACTOR_MATERIALITY
from .window import WindowBound, bound_cash_trade_days
from ..session_calendar import (
    SESSION_SNAPSHOT_HASH,
    SESSION_SOURCE_VERSION,
    SessionCalendar,
    SessionKind,
)
from ..liquidity import (
    AuctionCapMode,
    AuctionWindow,
    DailyAuctionLiquidityPolicy,
)
from ..metrics import (
    MetricConfig,
    MetricEngine,
    calculate_trade_round_metrics,
)
from ..outcome import OutcomeTracker, add_trade_calendar_assumptions
from ..result_contract import ResultContract, ResultReason, ResultStatus
from ..rulebook import (
    DEFAULT_RULEBOOK,
    ExecutionEngine,
    LIMIT_BAND_SNAPSHOT_HASH,
    LIMIT_BAND_SOURCE_VERSION,
    RuleBook,
)

logger = logging.getLogger(__name__)


_ADJUSTMENT_FACTOR_IDENTITY_ASSUMPTION = (
    "data.adjustment_factor_identity"
)


def _adjustment_factor_scope_key(
        identity: AdjustmentFactorIdentity) -> tuple:
    """Return one canonical semantic scope key for AFI report deduplication.

    Daily identities use their date bounds.  Minute identity revisions may
    additionally publish explicit timestamp bounds; include every canonical
    start/end timestamp field without treating evidence hashes or row counts
    as separate scopes.  Consequently, two reports for the same semantic
    scope must carry exactly the same identity rather than silently coexisting.
    """

    payload = identity.to_dict()
    boundary_names = {
        name
        for name in payload
        if name in {
            "scope_start",
            "scope_end",
            "scope_observation_keys_sha256",
        }
        or (
            name.startswith("scope_")
            and "timestamp" in name
            and ("start" in name or "end" in name)
        )
    }
    boundaries = {
        name: payload[name]
        for name in sorted(boundary_names)
    }
    canonical_boundaries = json.dumps(
        boundaries,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    frequency = identity.frequency
    frequency_value = getattr(frequency, "value", frequency)
    return identity.symbol, str(frequency_value), canonical_boundaries


def _add_adjustment_factor_identity_assumption(
        tracker: OutcomeTracker, reports) -> None:
    """Commit all non-empty AFI reports to one compact result assumption."""

    identities = []
    by_scope = {}
    for report in reports:
        identity = getattr(report, "adjustment_factor_identity", None)
        if identity is None:
            continue
        if not isinstance(identity, AdjustmentFactorIdentity):
            raise TypeError(
                "data-quality adjustment_factor_identity must be an "
                "AdjustmentFactorIdentity or None"
            )
        scope_key = _adjustment_factor_scope_key(identity)
        canonical_identity = identity.to_json()
        previous = by_scope.get(scope_key)
        if previous is not None:
            if previous != canonical_identity:
                raise ValueError(
                    "conflicting adjustment factor identity for scope "
                    f"{scope_key!r}"
                )
            continue
        by_scope[scope_key] = canonical_identity
        identities.append(identity)

    if not identities:
        return

    # Sort independently of report arrival order before building the compact
    # run-level commitment.
    value = adjustment_factor_assumption_value(
        sorted(identities, key=lambda item: item.to_json())
    )
    existing = getattr(tracker, "_assumptions", {}).get(
        _ADJUSTMENT_FACTOR_IDENTITY_ASSUMPTION
    )
    if existing is not None:
        if existing.value != value:
            raise ValueError(
                "conflicting data.adjustment_factor_identity assumption"
            )
        return
    tracker.add_assumption(
        _ADJUSTMENT_FACTOR_IDENTITY_ASSUMPTION, value
    )


def _assumption_number(value: object, name: str) -> str:
    """Serialize one effective numeric model setting without normalization."""

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return repr(number)


def _add_execution_model_assumptions(
        tracker: OutcomeTracker, engine: object) -> None:
    """Attach the effective cash execution configuration to one result.

    Values are read from the initialized Broker rather than copied from raw
    constructor arguments, so normalization and defaults cannot drift away
    from the evidence published with the result.
    """

    context = getattr(engine, "_context", None)
    broker = getattr(context, "_broker", None)
    if broker is None:
        raise RuntimeError(
            "execution assumptions require an initialized broker"
        )
    schedule = broker.fee_engine.schedule
    stamp_policy = broker.account.stamp_duty_rate
    if isinstance(stamp_policy, str):
        stamp_policy_value = stamp_policy
    else:
        stamp_policy_value = _assumption_number(
            stamp_policy, "stamp_duty_rate")

    limit_overrides = {
        key: broker.limit_pct_overrides[key]
        for key in sorted(broker.limit_pct_overrides)
    }
    t0_overrides = sorted(broker.t0_overrides)
    limit_exempt = {
        str(symbol): sorted(str(trade_date) for trade_date in dates)
        for symbol, dates in sorted(broker._limit_check_exempt.items())
    }
    limit_exempt_json = json.dumps(
        limit_exempt,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    strategy_price_mode = str(engine.strategy_price_mode)
    execution_price_mode = str(engine.execution_price_mode)
    distinct_raw_execution = (
        execution_price_mode == "raw"
        and strategy_price_mode != execution_price_mode
    )
    corporate_action_model = (
        "immediate_total_return_reinvestment_with_fractional_cash_in_lieu"
        if distinct_raw_execution
        else "disabled_same_price_space"
    )
    corporate_action_materiality = (
        _assumption_number(
            ADJUSTMENT_FACTOR_MATERIALITY,
            "adjustment_factor_materiality",
        )
        if distinct_raw_execution
        else "not_applicable"
    )
    dividend_tax_model = "none" if distinct_raw_execution else "not_applicable"
    corporate_action_band_reference = (
        "source_pre_close_else_pre_close_div_audited_factor_change"
        if distinct_raw_execution
        else "source_pre_close_only_no_factor_overlay"
    )
    values = (
        ("execution.frequency", engine.freq),
        ("execution.strategy_price_mode", strategy_price_mode),
        ("execution.execution_price_mode", execution_price_mode),
        ("execution.market_order_model", "worst_bar_v1"),
        (
            "execution.continuous_market_price",
            "buy_high_plus_slippage_sell_low_minus_slippage",
        ),
        (
            "execution.limit_order_model",
            "touch_at_limit_or_better_open_no_queue_shared_bar_liquidity_cap",
        ),
        (
            "execution.stop_order_model",
            "trigger_or_gap_open_with_directional_slippage_shared_bar_liquidity_cap",
        ),
        ("execution.opening_auction_sell_price", "open"),
        (
            "execution.opening_auction_buy_price",
            (
                "open_plus_slippage"
                if (
                    broker.open_buy_resize_mode == "auto"
                    and broker.open_buy_fill_mode == "open+slip"
                )
                else "open"
            ),
        ),
        (
            "execution.closing_auction_price",
            "close_with_directional_slippage",
        ),
        (
            "execution.daily_close_phase_model",
            "bar_close_window_approximation_not_exchange_session_reconstruction",
        ),
        (
            "execution.modeled_fill_price_semantics",
            "effective_price_may_not_be_legal_tick_trade_print",
        ),
        (
            "execution.price_band_fill_policy",
            "adverse_direction_saturation_opposite_direction_fail_fast",
        ),
        (
            "execution.raw_bar_price_band_validation",
            "fail_fast_before_execution_or_strategy_callback",
        ),
        (
            "execution.corporate_action_band_reference",
            corporate_action_band_reference,
        ),
        ("execution.slippage_rate", _assumption_number(
            broker.slippage, "slippage")),
        ("execution.bar_liquidity_model", broker.bar_liquidity_model),
        ("execution.liquidity_cap_ratio", _assumption_number(
            broker.liquidity_cap_ratio, "liquidity_cap_ratio")),
        ("execution.open_buy_fill_mode", broker.open_buy_fill_mode),
        ("execution.open_buy_resize_mode", broker.open_buy_resize_mode),
        ("execution.open_buy_sizing", broker.open_buy_sizing),
        ("execution.lot_size", str(broker.lot_size)),
        (
            "execution.allow_odd_lot_sell",
            "true" if broker.allow_odd_lot_sell else "false",
        ),
        ("execution.order_time_in_force", "day"),
        ("execution.commission_scope", broker.commission_scope),
        ("execution.commission_rate", _assumption_number(
            schedule.commission_rate, "commission_rate")),
        ("execution.min_commission", _assumption_number(
            schedule.min_commission, "min_commission")),
        ("execution.stamp_duty_policy", stamp_policy_value),
        ("execution.transfer_fee_rate", _assumption_number(
            schedule.transfer_fee_rate, "transfer_fee_rate")),
        ("execution.fee_rounding", "cent_half_up"),
        ("execution.fractional_entitlement_policy", "reject"),
        (
            "execution.limit_pct_overrides",
            json.dumps(
                limit_overrides,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        ),
        (
            "execution.t0_overrides",
            json.dumps(
                t0_overrides,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ),
        (
            "execution.limit_exempt_snapshot_sha256",
            hashlib.sha256(limit_exempt_json.encode("utf-8")).hexdigest(),
        ),
        (
            "corporate_action.adjustment_factor_model",
            corporate_action_model,
        ),
        (
            "corporate_action.adjustment_factor_materiality",
            corporate_action_materiality,
        ),
        ("corporate_action.dividend_tax_model", dividend_tax_model),
    )
    for key, value in values:
        tracker.add_assumption(key, value)


def _is_valid_ohlc_row(row) -> bool:
    """bar 有效性：OHLC 全部非 NaN 且 >0（无效 bar 一律按缺 bar 处理，
    防止 NaN 价格进入撮合层冻结 NaN 资金、永久污染账户）"""
    try:
        for col in ('open', 'high', 'low', 'close'):
            v = row.get(col) if hasattr(row, 'get') else getattr(row, col, None)
            if v is None or pd.isna(v) or float(v) <= 0:
                return False
    except (TypeError, ValueError):
        return False
    return True


def _amount_yuan(value, scale: float = 1.0) -> float:
    """成交额归一化为元。

    数据源单位不一致：日线 amount=千元 (scale=1000)、分钟线 amount=元 (scale=1)。
    NaN/缺失/负值一律归 0（撮合层将视为无流动性信息）。
    """
    try:
        if value is None or pd.isna(value):
            return 0.0
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v * scale if v > 0 else 0.0


def _resolve_daily_auction_cap(
        policy: Optional[DailyAuctionLiquidityPolicy],
        window: AuctionWindow,
        previous_day_amount_yuan: Optional[float]) -> Optional[float]:
    """Resolve a daily-auction cap without inventing a fallback.

    Missing configuration or unavailable previous-day turnover remains
    unresolved here.  The broker raises only if an eligible order actually
    attempts to use that auction window, so unrelated daily strategies do not
    need dummy assumptions.
    """
    if policy is None:
        return None
    spec = (
        policy.open_cap
        if window is AuctionWindow.OPEN
        else policy.close_cap
    )
    if spec is None:
        return None
    if (spec.mode is AuctionCapMode.PREVIOUS_DAY_RATIO
            and previous_day_amount_yuan is None):
        return None
    return policy.resolve(
        window,
        previous_day_amount_yuan=previous_day_amount_yuan,
    )


def _execution_adjustment_factor_change(
        provider, symbol: str, trade_date: str) -> Optional[float]:
    """Return the audited T/T-1 factor change for a raw execution bar."""
    if (getattr(provider, "execution_price_mode", None) != "raw"
            or getattr(provider, "price_mode", None) == "raw"):
        return None
    previous = provider.get_prev_trade_day(trade_date, 1)
    if previous is None:
        return None
    today_ratio = float(provider.get_adj_ratio(symbol, trade_date))
    try:
        previous_ratio = float(provider.get_adj_ratio(symbol, previous))
    except AdjustmentFactorError:
        # The first listed observation legitimately has no T-1 factor.  With
        # no audited change, do not invent an alternate reference; listing
        # exemptions and ordinary source-pre-close validation still apply.
        return None
    if (not math.isfinite(today_ratio) or today_ratio <= 0
            or not math.isfinite(previous_ratio) or previous_ratio <= 0):
        raise ValueError("adjustment factor ratios must be finite and positive")
    change = today_ratio / previous_ratio
    if not math.isfinite(change) or change <= 0:
        raise ValueError("adjustment factor change must be finite and positive")
    return change


def _daily_amount_for_date(
        frame: pd.DataFrame, date: str, *, scale: float) -> Optional[float]:
    """Extract one exact daily turnover observation in yuan."""
    if frame is None or frame.empty:
        return None
    try:
        normalized = pd.to_datetime(frame.index).normalize()
    except Exception as exc:
        raise ValueError(
            f"Daily timestamp invalid while loading prior turnover for {date}"
        ) from exc
    target = pd.to_datetime(date).normalize()
    positions = [i for i, value in enumerate(normalized) if value == target]
    if not positions:
        return None
    if len(positions) != 1:
        raise ValueError(
            f"Daily turnover key must be unique for prior date {date}"
        )
    amount = _amount_yuan(
        frame.iloc[positions[0]].get('amount', 0), scale=scale)
    return amount if amount > 0 else None


@dataclass
class BacktestResult:
    """回测结果"""
    symbol: str
    start_date: str
    end_date: str
    initial_cash: float
    final_value: float
    total_return: float  # 总收益率
    annual_return: float  # 年化收益率
    max_drawdown: float  # 最大回撤
    trade_count: int  # 交易次数
    win_rate: Optional[float]  # 胜率；无已平仓交易时为 None
    sharpe_ratio: Optional[float]  # 夏普比率；样本不足/零波动时为 None
    max_drawdown_close_nav: float = 0.0
    max_drawdown_intraday_low_nav: float = 0.0
    max_drawdown_intraday_high_to_low: Optional[float] = None

    # 每日净值
    daily_values: pd.DataFrame = field(default_factory=pd.DataFrame)

    # 交易记录
    trades: List[Dict] = field(default_factory=list)

    # 版本化终态与覆盖证据；历史构造器可保持 None。
    result_contract: Optional[ResultContract] = None

    # 当前引擎显式输出（包括空 bundle）；None 仅兼容旧构造器/旧产物。
    target_execution: Optional[TargetExecutionBundle] = None

    # Strict replay evidence. None is retained only for legacy/manual results.
    cash_audit: Optional[CashAuditBundle] = None

    def __post_init__(self) -> None:
        self._validate_target_execution()
        self._validate_cash_audit()

    def _validate_target_execution(self) -> None:
        bundle = self.target_execution
        if bundle is None:
            return
        if type(bundle) is not TargetExecutionBundle:
            raise TypeError(
                "target_execution must be exactly TargetExecutionBundle or None"
            )
        if (
            type(self.result_contract) is ResultContract
            and self.result_contract.status is ResultStatus.SUCCESS
            and not bundle.complete
        ):
            raise ValueError(
                "SUCCESS BacktestResult requires complete target execution evidence"
            )

    def _validate_cash_audit(self) -> None:
        bundle = self.cash_audit
        if bundle is None:
            self._cash_replay = None
            return
        if type(bundle) is not CashAuditBundle:
            raise TypeError(
                "cash_audit must be exactly CashAuditBundle or None"
            )
        replay = bundle.replay()
        records = cash_replay_trade_records(bundle)
        if self.trades != records:
            raise ValueError(
                "BacktestResult trades must equal the cash replay fill view"
            )
        if self.trade_count != len(replay.trades):
            raise ValueError(
                "BacktestResult trade_count must equal replay fill count"
            )
        if self.win_rate != replay.win_rate:
            raise ValueError(
                "BacktestResult win_rate must equal replay closed-round rate"
            )
        if (
            not math.isfinite(float(self.final_value))
            or not math.isclose(
                float(self.final_value),
                replay.state.nav,
                rel_tol=0.0,
                abs_tol=1e-7,
            )
        ):
            raise ValueError(
                "BacktestResult final_value must equal replay terminal NAV"
            )
        if (
            not math.isfinite(float(self.initial_cash))
            or not math.isclose(
                float(self.initial_cash),
                bundle.seed.initial_nav,
                rel_tol=0.0,
                abs_tol=1e-7,
            )
        ):
            raise ValueError(
                "BacktestResult initial_cash must equal replay initial NAV"
            )
        initial = float(self.initial_cash)
        if initial == 0:
            expected_total_return = 0.0 if self.final_value == 0 else None
        else:
            expected_total_return = float(self.final_value) / initial - 1.0
        if (
            expected_total_return is None
            or not math.isfinite(float(self.total_return))
            or not math.isclose(
                float(self.total_return),
                expected_total_return,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            raise ValueError(
                "BacktestResult total_return must equal final/initial NAV return"
            )
        self._cash_replay = replay

    @property
    def cash_replay_seed(self):
        self._validate_cash_audit()
        return None if self.cash_audit is None else self.cash_audit.seed

    @property
    def event_journal(self):
        self._validate_cash_audit()
        return None if self.cash_audit is None else self.cash_audit.journal

    @property
    def fills(self):
        self._validate_cash_audit()
        return () if self._cash_replay is None else self._cash_replay.fills

    @property
    def fees(self):
        self._validate_cash_audit()
        return () if self._cash_replay is None else self._cash_replay.fees

    @property
    def closed_round_trips(self):
        self._validate_cash_audit()
        return (
            ()
            if self._cash_replay is None
            else self._cash_replay.closed_round_trips
        )

    @property
    def total_fees(self):
        self._validate_cash_audit()
        if self._cash_replay is None:
            return None
        return sum(item.total for item in self._cash_replay.fees)

    @property
    def closed_trade_count(self):
        self._validate_cash_audit()
        if self._cash_replay is None:
            return None
        return len(self._cash_replay.closed_round_trips)

    @property
    def open_position_count(self):
        self._validate_cash_audit()
        if self._cash_replay is None:
            return None
        return len(self._cash_replay.state.positions)

    @property
    def is_rankable(self) -> bool:
        return bool(
            self.result_contract is not None
            and self.result_contract.is_rankable
        )

    def to_dict(self) -> Dict:
        self._validate_target_execution()
        self._validate_cash_audit()
        return {
            'symbol': self.symbol,
            'start_date': self.start_date,
            'end_date': self.end_date,
            'initial_cash': self.initial_cash,
            'final_value': round(self.final_value, 2),
            'total_return': round(self.total_return * 100, 2),
            'annual_return': round(self.annual_return * 100, 2),
            'max_drawdown': round(self.max_drawdown * 100, 2),
            'max_drawdown_close_nav': round(self.max_drawdown_close_nav * 100, 2),
            'max_drawdown_intraday_low_nav': round(self.max_drawdown_intraday_low_nav * 100, 2),
            'max_drawdown_intraday_high_to_low': (
                None
                if self.max_drawdown_intraday_high_to_low is None
                else round(
                    self.max_drawdown_intraday_high_to_low * 100, 2
                )
            ),
            'trade_count': self.trade_count,
            'win_rate': (
                None if self.win_rate is None
                else round(self.win_rate * 100, 2)
            ),
            'sharpe_ratio': (
                None if self.sharpe_ratio is None
                else round(self.sharpe_ratio, 3)
            ),
            'result_contract': (
                None
                if self.result_contract is None
                else self.result_contract.to_dict()
            ),
            'target_execution': (
                None
                if self.target_execution is None
                else self.target_execution.to_dict()
            ),
            'cash_audit': (
                None
                if self.cash_audit is None
                else self.cash_audit.to_dict()
            ),
            'fill_count': (
                None if self._cash_replay is None
                else len(self._cash_replay.fills)
            ),
            'closed_trade_count': self.closed_trade_count,
            'open_position_count': self.open_position_count,
            'total_fees': self.total_fees,
        }


class BacktestEngine:
    """
    回测引擎

    执行单股票回测

    Usage:
        engine = BacktestEngine(
            symbol='000001.SZ',
            start_date='20240101',
            end_date='20241231',
            initial_cash=100000
        )
        result = engine.run(MyStrategy())
    """

    def __init__(self, symbol: str, start_date: str, end_date: str,
                 initial_cash: float = 100000.0,
                 freq: str = 'minute',
                 slippage: float = 0.001,
                 commission_rate: float = 0.00025,
                 stamp_duty: float = 0.001,
                 min_commission: float = 5.0,
                 lot_size: int = 100,
                 strategy_price_mode: str = PRICE_MODE_STRATEGY,
                 execution_price_mode: str = PRICE_MODE_EXECUTION,
                 open_buy_resize_mode: str = 'auto',
                 liquidity_cap_ratio: float = 0.8,
                 limit_pct_overrides: Dict[str, float] = None,
                 open_buy_fill_mode: str = 'open+slip',
                 open_buy_sizing: str = 'limit_up',
                 bar_liquidity_model: str = 'close_lock',
                 t0_overrides=None,
                 trading_days_per_year: int = 252,
                 risk_free_rate: float = 0.03,
                 on_missing_data: str = 'raise',
                 daily_auction_liquidity: Optional[
                     DailyAuctionLiquidityPolicy] = None,
                 rulebook: RuleBook = None,
                 transfer_fee_rate: float = 0.0,
                 data_root=None):
        """
        Args:
            symbol: 股票代码
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            initial_cash: 初始资金
            freq: 回测频率 ('minute' 或 'daily')
            slippage: 滑点比例 (默认千1)
            commission_rate: 佣金费率 (默认万2.5)
            stamp_duty: 印花税率 (默认千1，仅卖出)
            transfer_fee_rate: 双边过户费率 (默认0；不做历史自动切换)
            min_commission: 单笔最低佣金 (默认5元)
            lot_size: 每手股数 (默认100)
            open_buy_resize_mode: 开盘买单缩量模式 ('auto'/'legacy')
            liquidity_cap_ratio: 单根K线最大可吃成交额比例 (默认0.8)
            limit_pct_overrides: 涨跌停幅度覆盖表 {代码: 幅度}，优先于内置规则
            open_buy_fill_mode: 竞价买单成交价 'open+slip'(默认,含滑点惩罚)/'open'(旧口径)
            open_buy_sizing: 竞价买单委托量折算 'limit_up'(默认,按涨停价)/'fill'(旧口径)
            t0_overrides: T+0 品种集合（代码/前缀），命中者当日买入当日可卖
            trading_days_per_year: 年化交易日基数 (默认252，A股建议244)
            risk_free_rate: 夏普无风险利率 (默认0.03)
        """
        self.symbol = symbol
        self.start_date = start_date
        self.end_date = end_date
        self.initial_cash = initial_cash
        if type(freq) is not str:
            raise TypeError("freq must be exactly str")
        if freq not in ('daily', 'minute'):
            raise ValueError("freq must be exactly 'daily' or 'minute'")
        self.freq = freq
        self.slippage = slippage
        self.commission_rate = commission_rate
        self.stamp_duty = stamp_duty
        self.transfer_fee_rate = transfer_fee_rate
        # Keep dataset selection on the engine instance.  This lets a CLI/GUI
        # run point at an explicit local repository without mutating the
        # process-wide DATA_ROOT environment variable.
        self.data_root = data_root
        self.min_commission = min_commission
        self.lot_size = lot_size
        self.strategy_price_mode = strategy_price_mode
        self.execution_price_mode = execution_price_mode
        self.open_buy_resize_mode = open_buy_resize_mode
        self.liquidity_cap_ratio = liquidity_cap_ratio
        self.limit_pct_overrides = limit_pct_overrides
        self.open_buy_fill_mode = open_buy_fill_mode
        self.open_buy_sizing = open_buy_sizing
        self.bar_liquidity_model = bar_liquidity_model
        self.t0_overrides = t0_overrides
        self.trading_days_per_year = int(trading_days_per_year)
        self.risk_free_rate = float(risk_free_rate)
        if (daily_auction_liquidity is not None
                and type(daily_auction_liquidity)
                is not DailyAuctionLiquidityPolicy):
            raise TypeError(
                "daily_auction_liquidity must be exactly "
                "DailyAuctionLiquidityPolicy or None"
            )
        self.daily_auction_liquidity = daily_auction_liquidity
        resolved_rulebook = DEFAULT_RULEBOOK if rulebook is None else rulebook
        if not isinstance(resolved_rulebook, RuleBook):
            raise TypeError("rulebook must be a RuleBook or None")
        self.rulebook = resolved_rulebook
        self.session_calendar = SessionCalendar(rulebook=resolved_rulebook)
        # 数据缺失策略: 'raise'(默认) | 'warn' | 'ignore'
        self.on_missing_data = on_missing_data

        self._context: Optional[Context] = None
        self._strategy: Optional[Strategy] = None
        self._data_provider: Optional[DataProvider] = None
        self._aligned_daily_data = None
        self._data_quality_reports: List[Any] = []
        self._data_contract_compatibility_path = False
        self._previous_daily_amount_date: Optional[str] = None
        self._previous_daily_amount_yuan: Optional[float] = None

        # 每日净值记录
        self._daily_values: List[Dict] = []
        self._current_day_high_market_value: Optional[float] = None
        self._current_day_high_total_value: Optional[float] = None
        self._current_day_low_market_value: Optional[float] = None
        self._current_day_low_total_value: Optional[float] = None
        self.last_result_contract: Optional[ResultContract] = None
        self._outcome_tracker: Optional[OutcomeTracker] = None
        self._window_bound: Optional[WindowBound] = None
        self._cash_replay_seed: Optional[CashReplaySeed] = None
        self.last_cash_audit: Optional[CashAuditBundle] = None

    @property
    def data_quality_reports(self) -> tuple:
        """Immutable snapshot of data-contract reports observed by this run."""

        return tuple(self._data_quality_reports)

    def _metric_config(self) -> MetricConfig:
        return MetricConfig(
            trading_days_per_year=self.trading_days_per_year,
            risk_free_rate=self.risk_free_rate,
        )

    @staticmethod
    def _canonical_trade_day(trade_date: object) -> str:
        return pd.to_datetime(str(trade_date)).strftime('%Y-%m-%d')

    def _new_outcome_tracker(self, trade_days) -> OutcomeTracker:
        expected_ids = tuple(
            f"{self.symbol}|{self._canonical_trade_day(trade_date)}"
            for trade_date in trade_days
        )
        tracker = OutcomeTracker(
            expected_observation_ids=expected_ids,
            coverage_unit="symbol_trade_day",
        )
        add_trade_calendar_assumptions(tracker, self._data_provider)
        for key, value in self._metric_config().assumptions():
            tracker.add_assumption(key, value)
        _add_execution_model_assumptions(tracker, self)
        tracker.add_assumption(
            "cash.audit_contract",
            "strict_pre_event_seed_plus_execution_event_journal_v1",
        )
        tracker.add_assumption("cash.trade_count_unit", "fill")
        tracker.add_assumption(
            "cash.win_rate_unit", "closed_inventory_round"
        )
        tracker.add_assumption("cash.fee_source", "settlement_event")
        if self._cash_replay_seed is not None:
            tracker.add_assumption(
                "cash.audit_seed_as_of",
                self._cash_replay_seed.as_of.isoformat(
                    timespec="microseconds"
                ),
            )
        tracker.add_assumption(
            "metrics.intraday_low_path",
            "causal_prior_close_peak_to_intraday_low",
        )
        tracker.add_assumption(
            "metrics.intraday_high_to_low",
            "pessimistic_same_day_upper_bound",
        )
        tracker.add_assumption(
            "execution.rulebook_source_version", self.rulebook.source_version)
        tracker.add_assumption(
            "execution.limit_band_source_version", LIMIT_BAND_SOURCE_VERSION)
        tracker.add_assumption(
            "execution.limit_band_snapshot_hash", LIMIT_BAND_SNAPSHOT_HASH)
        tracker.add_assumption(
            "execution.session_source_version", SESSION_SOURCE_VERSION)
        tracker.add_assumption(
            "execution.session_snapshot_hash", SESSION_SNAPSHOT_HASH)
        if self.freq != 'minute':
            policy = (
                self.daily_auction_liquidity
                if self.daily_auction_liquidity is not None
                else DailyAuctionLiquidityPolicy()
            )
            for key, value in policy.assumptions():
                tracker.add_assumption(key, value)
        if self._window_bound is not None and self._window_bound.truncated:
            tracker.add_warning(
                "WINDOW_TRUNCATED", self._window_bound.warning_message
            )
        return tracker

    @staticmethod
    def _probe_daily_data_dates(*frames) -> tuple:
        """Return the latest observed date across non-empty daily probes."""

        maxima = []
        for frame in frames:
            if frame is None or frame.empty:
                continue
            values = (
                frame["trade_date"]
                if "trade_date" in frame.columns
                else frame.index
            )
            try:
                # Numeric YYYYMMDD columns must be parsed as calendar text;
                # passing int64 values directly makes pandas interpret them
                # as nanoseconds since the Unix epoch (and yields 1970).
                parsed = pd.to_datetime(
                    values.astype(str), errors="coerce"
                )
                maximum = parsed.max()
            except (TypeError, ValueError):
                continue
            if pd.isna(maximum):
                continue
            maxima.append(pd.Timestamp(maximum).strftime("%Y%m%d"))
        return (max(maxima),) if maxima else ()

    def _mark_data_contract_compatibility(self) -> None:
        """Expose the legacy provider path in the public result contract."""

        if self._data_contract_compatibility_path:
            return
        self._data_contract_compatibility_path = True
        if self._outcome_tracker is None:
            return
        self._outcome_tracker.add_assumption(
            "data.contract_path", "legacy_provider_compatibility"
        )
        self._outcome_tracker.add_warning(
            "DATA_CONTRACT_COMPATIBILITY_PATH",
            "The provider does not expose the aligned DC-1 pair contract; "
            "strategy and execution lanes used the legacy compatibility path.",
        )

    def _observe_completed_day(self, trade_date: object) -> None:
        """Record only a fully returned day that actually saw a valid bar."""
        if (
            self._outcome_tracker is None
            or self._current_day_low_total_value is None
        ):
            return
        canonical = self._canonical_trade_day(trade_date)
        self._outcome_tracker.observe(
            f"{self.symbol}|{canonical}", canonical
        )

    def _snapshot_cash_audit(self) -> CashAuditBundle:
        """Freeze and reconcile the complete cash event stream."""

        if self._cash_replay_seed is None:
            raise RuntimeError("cash replay seed was not captured")
        broker = (
            self._context._broker
            if self._context is not None else None
        )
        if broker is None:
            raise RuntimeError("cash audit requires an initialized broker")
        bundle = CashAuditBundle.from_parts(
            self._cash_replay_seed, broker.event_journal
        )
        state = bundle.replay().state
        state.assert_matches_account(broker.account)
        if state.reservations:
            raise RuntimeError("terminal cash audit retains reservations")
        active_orders = tuple(
            item.order_id
            for item in state.orders
            if item.status in ("accepted", "partial", "created")
        )
        if active_orders:
            raise RuntimeError(
                f"terminal cash audit retains active orders: {active_orders}"
            )
        self.last_cash_audit = bundle
        return bundle

    def _ensure_cash_replay_seed(self) -> CashReplaySeed:
        """Capture a seed for real and prewired/test broker initializers."""

        if self._cash_replay_seed is not None:
            return self._cash_replay_seed
        broker = (
            self._context._broker
            if self._context is not None else None
        )
        if broker is None:
            raise RuntimeError("cash replay seed requires an initialized broker")
        if len(broker.event_journal):
            raise RuntimeError(
                "cannot capture a cash replay seed after the first event"
            )
        self._cash_replay_seed = CashReplaySeed.from_account(
            broker.account,
            as_of=broker._default_simulated_time(),
        )
        return self._cash_replay_seed

    def _set_failed_contract(
            self, *, preserve_primary_error: bool = False) -> ResultContract:
        """Freeze runtime evidence before propagating the original error."""
        tracker = self._outcome_tracker
        if tracker is None:
            tracker = OutcomeTracker(
                expected_observation_ids=(),
                coverage_unit="symbol_trade_day",
            )
            try:
                add_trade_calendar_assumptions(tracker, self._data_provider)
            except Exception:
                if not preserve_primary_error:
                    raise
                logger.exception(
                    "Trade-calendar identity could not be committed to the "
                    "failed result; preserving the primary engine error"
                )
        try:
            try:
                _add_adjustment_factor_identity_assumption(
                    tracker, self._data_quality_reports
                )
            except (TypeError, ValueError):
                if not preserve_primary_error:
                    raise
                logger.exception(
                    "Adjustment-factor identity could not be committed to "
                    "the failed result; preserving the primary engine error"
                )
            contract = tracker.finalize(
                ResultStatus.FAILED,
                reason_code="ENGINE_RUNTIME_ERROR",
                reason_message="The cash backtest engine raised an exception",
            )
        except RuntimeError:
            # A late finalizer can fail after the normal terminal contract was
            # already frozen. Preserve its evidence while changing only the
            # terminal state; the original exception is still re-raised.
            previous = self.last_result_contract
            if previous is None:
                raise
            contract = ResultContract(
                status=ResultStatus.FAILED,
                reason=ResultReason(
                    "ENGINE_RUNTIME_ERROR",
                    "The cash backtest engine raised an exception",
                ),
                warnings=previous.warnings,
                assumptions=previous.assumptions,
                actual_interval=previous.actual_interval,
                data_coverage=previous.data_coverage,
            )
        self.last_result_contract = contract
        return contract

    def run(self, strategy: Strategy) -> BacktestResult:
        """
        运行回测

        Args:
            strategy: 策略实例

        Returns:
            BacktestResult
        """
        self._strategy = strategy
        self.last_result_contract = None
        self.last_cash_audit = None
        self._cash_replay_seed = None
        self._outcome_tracker = None
        self._window_bound = None
        strategy_initialized = False

        try:
            # 初始化
            self._init_engine()
            self._ensure_cash_replay_seed()

            # on_finish 与成功返回的 on_init 配对。若 on_init 自身失败，
            # 仍释放它可能创建的订单，但不调用尚未开始的 finish 生命周期。
            self._strategy.set_context(self._context)
            self._strategy.on_init(self._context)
            strategy_initialized = True

            # 获取交易日列表
            trade_days = self._data_provider.get_trade_days_between(
                self.start_date, self.end_date
            )
            # Probe before freezing the expected scope.  Scheduled sessions
            # beyond the latest completed close or local data snapshot are not
            # valid flat-cash observations.
            probe_exec = self._data_provider.get_daily(
                self.symbol, start=self.start_date, end=self.end_date,
                price_mode='execution'
            )
            probe_strat = self._data_provider.get_daily(
                self.symbol, start=self.start_date, end=self.end_date,
                price_mode='strategy'
            )
            self._window_bound = bound_cash_trade_days(
                trade_days,
                requested_end=self.end_date,
                data_dates=self._probe_daily_data_dates(
                    probe_exec, probe_strat
                ),
            )
            trade_days = list(self._window_bound.trade_days)
            self._outcome_tracker = self._new_outcome_tracker(trade_days)

            if not trade_days:
                logger.warning(
                    f"No completed trade days between {self.start_date} "
                    f"and {self.end_date}"
                )
            else:
                # 数据缺失守卫：修复"静默成功"失真链（拼错代码 → 0%收益0交易正常退出）
                # + 单轨 fallback：只有一腿数据时两腿自动对齐到可用侧（同源运行）
                exec_ok = probe_exec is not None and not probe_exec.empty
                strat_ok = probe_strat is not None and not probe_strat.empty
                pair_loader = getattr(
                    self._data_provider, 'get_aligned_pair', None)
                if strat_ok != exec_ok and not callable(pair_loader):
                    self._mark_data_contract_compatibility()
                    # 单轨：把两腿模式对齐到有数据的一侧（同模式自动短路价格换算与除权调股）
                    if strat_ok:
                        self._data_provider.set_price_modes(
                            execution=self._data_provider.price_mode)
                        self._context._execution_price_mode = self._context._strategy_price_mode
                    else:
                        self._data_provider.set_price_modes(
                            strategy=self._data_provider.execution_price_mode)
                        self._context._strategy_price_mode = self._context._execution_price_mode
                    self._context.set_same_source_symbols({self.symbol})
                    logger.warning(
                        f"{self.symbol} 仅有单轨数据（{'策略腿' if strat_ok else '撮合腿'}），"
                        f"两腿同源运行：价格换算与除权调股已禁用。单轨口径的牺牲见文档。"
                    )
                if not exec_ok and not strat_ok:
                    msg = (
                        f"标的 {self.symbol} 在 {self.start_date}~{self.end_date} 无任何行情数据，"
                        "请检查代码拼写与数据目录。"
                    )
                    if self.freq == 'minute':
                        msg += (
                            " 分钟回测仍正式要求同标的、同价格轨的伴随日线，"
                            "用于交易窗口边界、盘前日线历史和价格带/公司行为语义；"
                            "仅有 minute 分片不构成可运行的 v1 输入，且 "
                            "on_missing_data 不能绕过该要求。"
                        )
                    else:
                        msg += " 可传 on_missing_data='warn'/'ignore' 跳过此检查。"
                    if self.freq == 'minute' or self.on_missing_data == 'raise':
                        raise ValueError(msg)
                    if self.on_missing_data == 'warn':
                        logger.warning(msg)

                if self.freq != 'minute' and (exec_ok or strat_ok):
                    if callable(pair_loader):
                        aligned = pair_loader(
                            self.symbol,
                            frequency='daily',
                            start=self.start_date,
                            end=self.end_date,
                            pre_close_exempt_dates=(
                                self._pre_close_exempt_dates(self.symbol)
                            ),
                        )
                        self._aligned_daily_data = aligned
                        self._data_quality_reports.append(aligned.report)
                    else:
                        self._mark_data_contract_compatibility()
                        logger.warning(
                            "Provider has no get_aligned_pair API; daily data "
                            "is using the legacy compatibility path without a "
                            "DC-1 provider report"
                        )

                # 逐日回测
                for i, trade_date in enumerate(trade_days):
                    next_trade_date = (
                        trade_days[i + 1]
                        if i + 1 < len(trade_days) else None
                    )
                    self._run_day(
                        trade_date, next_trade_date=next_trade_date)
                    self._observe_completed_day(trade_date)
        except BaseException:
            self._set_failed_contract(preserve_primary_error=True)
            raise
        finally:
            # Finalization policy:
            # 1. Always attempt to release orders once a Broker exists.
            # 2. Call on_finish after every successfully completed on_init,
            #    including empty calendars and exceptional runs.
            # 3. A finalizer error propagates on a normal run, but is logged and
            #    suppressed while a primary error is active, so it cannot mask
            #    the actual backtest failure.
            primary_error = sys.exc_info()[1]
            finalizer_error = None

            def finalize(name, callback):
                nonlocal finalizer_error
                try:
                    callback()
                except BaseException as exc:  # preserve an active primary error
                    if primary_error is not None or finalizer_error is not None:
                        logger.exception(
                            "Backtest %s failed during finalization", name)
                    else:
                        finalizer_error = exc

            broker = (
                self._context._broker
                if self._context is not None else None
            )
            if broker is not None:
                finalize('order cleanup', broker.cancel_all_orders)
            if strategy_initialized:
                finalize(
                    'strategy on_finish',
                    lambda: self._strategy.on_finish(self._context),
                )
            # on_finish is not a trading callback, but clean again defensively
            # so an accidental order cannot leak beyond run().
            if broker is not None:
                finalize('post-finish order cleanup', broker.cancel_all_orders)
                finalize('cash audit snapshot', self._snapshot_cash_audit)

            if primary_error is None and finalizer_error is not None:
                self._set_failed_contract(preserve_primary_error=True)
                raise finalizer_error

        try:
            # Build the result only after on_finish, preserving the historical
            # callback/result ordering while covering empty calendars as well.
            if self._outcome_tracker is None:
                # Defensive only: a normal run always declares the calendar
                # scope.
                self._outcome_tracker = self._new_outcome_tracker(())
            _add_adjustment_factor_identity_assumption(
                self._outcome_tracker, self._data_quality_reports
            )
            contract = self._outcome_tracker.finalize_completed()
            self.last_result_contract = contract
            if self._daily_values:
                return self._create_result(result_contract=contract)
            return self._create_empty_result(result_contract=contract)
        except BaseException:
            self._set_failed_contract(preserve_primary_error=True)
            raise

    def _init_engine(self) -> None:
        """初始化引擎"""
        from ..broker import Broker, Account

        # Reject indices, REITs, futures and unknown symbols before any data
        # loading or account mutation can select an ordinary-stock fallback.
        self.rulebook.require_supported(
            self.symbol, engine=ExecutionEngine.CASH)

        # 创建独立的数据提供者（已移除单例，每个引擎实例独立）
        self._data_provider = DataProvider(
            price_mode=self.strategy_price_mode,
            execution_price_mode=self.execution_price_mode,
            data_root=self.data_root,
        )

        # 创建账户和撮合引擎（传入交易成本参数）
        account = Account(
            initial_cash=self.initial_cash,
            commission_rate=self.commission_rate,
            stamp_duty_rate=self.stamp_duty,
            min_commission=self.min_commission,
        )
        broker = Broker(
            account=account,
            slippage=self.slippage,
            lot_size=self.lot_size,
            open_buy_resize_mode=self.open_buy_resize_mode,
            liquidity_cap_ratio=self.liquidity_cap_ratio,
            limit_pct_overrides=self.limit_pct_overrides,
            open_buy_fill_mode=self.open_buy_fill_mode,
            open_buy_sizing=self.open_buy_sizing,
            bar_liquidity_model=self.bar_liquidity_model,
            t0_overrides=self.t0_overrides,
            limit_check_exempt=self._compute_limit_exempt(),
            transfer_fee_rate=self.transfer_fee_rate,
            rulebook=self.rulebook,
        )
        if len(broker.event_journal):
            raise RuntimeError(
                "broker emitted events before the cash replay seed"
            )
        # Broker deliberately timestamps orders created by on_init at its
        # deterministic pre-simulation epoch (currently 1970-01-01).  Capture
        # that exact boundary instead of pretending those events occurred on
        # start_date, which would make the journal causally unreplayable.
        self._cash_replay_seed = CashReplaySeed.from_account(
            account,
            as_of=broker._default_simulated_time(),
        )

        self._context = Context(
            broker=broker,
            data_provider=self._data_provider,
            initial_cash=self.initial_cash
        )
        self._context.set_symbol(self.symbol)
        self._daily_values = []
        self._aligned_daily_data = None
        self._data_quality_reports = []
        self._data_contract_compatibility_path = False
        self._previous_daily_amount_date = None
        self._previous_daily_amount_yuan = None
        self._reset_intraday_low_tracker()

    def _compute_limit_exempt(self):
        """新股上市初期涨跌停豁免日历（见 listing_rules）"""
        from .listing_rules import compute_limit_exempt_dates
        try:
            return compute_limit_exempt_dates(
                self._data_provider, [self.symbol], backtest_start=self.start_date)
        except Exception as e:
            logger.debug(f"limit exempt computation failed: {e}")
            return None

    def _pre_close_exempt_dates(self, symbol: str):
        """Return this run's explicit per-symbol IPO/limit exemption dates."""
        broker = self._context._broker if self._context is not None else None
        mapping = getattr(broker, '_limit_check_exempt', {}) if broker else {}
        return mapping.get(symbol, ())

    def _run_day(self, trade_date: str,
                 next_trade_date: str = None) -> None:
        self._reset_intraday_low_tracker()
        """运行单日回测"""
        # 设置日期
        self._context.set_datetime(f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} 09:00:00")
        self._context._on_day_start()

        # 盘前时段
        self._context.set_market_phase('before_open')
        with self._context._broker.order_creation_scope(
                'before_open', self._context.current_time,
                eligible_from=pd.to_datetime(trade_date),
                expire_date=trade_date):
            self._strategy.on_before_market_open(self._context)

        # 交易时段
        self._context.set_market_phase('trading')
        if self.freq == 'minute':
            self._run_minute_bars(
                trade_date, next_trade_date=next_trade_date)
        else:
            self._run_daily_bar(trade_date, next_trade_date=next_trade_date)

        # 最终撮合窗口已经结束。T 日 DAY 余单必须在盘后回调可见前终结；
        # on_day 产生、expire_date 属于未来交易日的单会由 Broker 保留。
        self._context._broker.cancel_day_end_orders(
            sim_time=self._context.current_time)

        # 盘后时段
        self._context.set_market_phase('after_close')
        self._context.set_datetime(f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} 15:00:00")
        self._strategy.on_after_market_close(self._context)

        # 记录每日净值
        self._record_daily_value(trade_date)

        # 日结束处理
        self._context._on_day_end()

    def _normalize_minute_index(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return df
        if not isinstance(df.index, pd.DatetimeIndex):
            try:
                df = df.copy()
                df.index = pd.to_datetime(df.index)
            except Exception:
                return df
        if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is not None:
            df = df.copy()
            df.index = df.index.tz_localize(None)
        return df

    def _run_minute_bars(self, trade_date: str,
                         next_trade_date: str = None) -> None:
        # Run minute backtest with strategy/execution price modes.
        pair_loader = getattr(self._data_provider, 'get_aligned_pair', None)
        if callable(pair_loader):
            aligned = pair_loader(
                self.symbol,
                frequency='minute',
                trade_date=trade_date,
                pre_close_exempt_dates=(
                    self._pre_close_exempt_dates(self.symbol)
                ),
            )
            minute_data = aligned.strategy
            raw_minute_data = aligned.execution
            self._data_quality_reports.append(aligned.report)
        else:
            self._mark_data_contract_compatibility()
            minute_data = self._data_provider.get_minute(
                self.symbol, trade_date=trade_date, price_mode='strategy'
            )
            raw_minute_data = self._data_provider.get_minute(
                self.symbol, trade_date=trade_date, price_mode='execution'
            )

        if raw_minute_data is None or raw_minute_data.empty:
            raise ValueError(
                f"Minute execution data required for {self.symbol} on "
                f"{trade_date}; freq='minute' never falls back to daily data"
            )
        if minute_data is None or minute_data.empty:
            raise ValueError(
                f"Minute strategy data required for {self.symbol} on "
                f"{trade_date}; missing price lane cannot be substituted"
            )

        minute_data = self._normalize_minute_index(minute_data)
        raw_minute_data = self._normalize_minute_index(raw_minute_data)
        if (not isinstance(minute_data.index, pd.DatetimeIndex)
                or not isinstance(raw_minute_data.index, pd.DatetimeIndex)):
            raise ValueError(
                f"Minute timestamp index invalid for {self.symbol} on {trade_date}"
            )
        if (not minute_data.index.is_unique
                or not raw_minute_data.index.is_unique):
            raise ValueError(
                f"Minute timestamp index not unique for {self.symbol} on "
                f"{trade_date}"
            )

        source_missing = raw_minute_data.index.difference(minute_data.index)
        source_extra = minute_data.index.difference(raw_minute_data.index)
        if len(source_missing) or len(source_extra):
            raise ValueError(
                f"Minute timestamp mismatch for {self.symbol} on "
                f"{trade_date}: missing_in_strategy={list(source_missing[:3])}, "
                f"extra_in_strategy={list(source_extra[:3])}"
            )

        # Exact keys, not vendor row order, define dual-lane alignment.
        minute_data = minute_data.sort_index()
        raw_minute_data = raw_minute_data.sort_index()

        # Route even the nominal 1-minute path through the effective-dated
        # session model.  Strict resampling isolates the exact 09:30 opening
        # observation and aggregates 14:58-15:00 closing-auction rows without
        # allowing either window to borrow continuous-session OHLC/amount.
        minute_data = resample_minute_data(
            minute_data, 1, symbol=self.symbol,
            session_calendar=self.session_calendar)
        raw_minute_data = resample_minute_data(
            raw_minute_data, 1, symbol=self.symbol,
            session_calendar=self.session_calendar)
        minute_data = minute_data.set_index('trade_time')
        raw_minute_data = raw_minute_data.set_index('trade_time')

        missing = raw_minute_data.index.difference(minute_data.index)
        extra = minute_data.index.difference(raw_minute_data.index)
        if len(missing) or len(extra):
            raise ValueError(
                f"Minute timestamp mismatch for {self.symbol} on {trade_date}: "
                f"missing_in_strategy={list(missing[:3])}, "
                f"extra_in_strategy={list(extra[:3])}"
            )
        # Row order may differ, but all strategy values are looked up by exact
        # timestamp rather than by position.
        minute_data = minute_data.reindex(raw_minute_data.index)

        invalid_strategy_times = [
            idx
            for idx, raw_row in raw_minute_data.iterrows()
            if (_is_valid_ohlc_row(raw_row)
                and not _is_valid_ohlc_row(minute_data.loc[idx]))
        ]
        if invalid_strategy_times:
            raise ValueError(
                f"Minute strategy data invalid for {self.symbol} on "
                f"{trade_date} at {invalid_strategy_times[0]}"
            )

        # The execution minute lane is authoritative for its own price band.
        # Daily data is only a fallback when the minute schema predates the
        # pre_close column; preferring daily can silently mix price spaces.
        pre_close = 0.0
        if 'pre_close' in raw_minute_data.columns:
            series = raw_minute_data['pre_close'].dropna()
            if not series.empty:
                pre_close = float(series.iloc[0])
        if pre_close <= 0:
            try:
                daily_row = self._data_provider.get_daily(
                    self.symbol, start=trade_date, end=trade_date,
                    price_mode='execution'
                )
                if not daily_row.empty and 'pre_close' in daily_row.columns:
                    val = daily_row.iloc[0].get('pre_close', 0)
                    if pd.notna(val):
                        pre_close = float(val)
            except Exception:
                pre_close = 0.0
        adjustment_factor_ratio = _execution_adjustment_factor_change(
            self._data_provider, self.symbol, trade_date)

        # 分钟数据缺 amount 列告警（缺列→流动性帽=0→静默全拒单，同日线列守卫口径）
        if 'amount' not in raw_minute_data.columns and \
                self.symbol not in getattr(self, '_warned_no_amount', set()):
            self._warned_no_amount = getattr(self, '_warned_no_amount', set())
            self._warned_no_amount.add(self.symbol)
            logger.warning(
                f"{self.symbol} 分钟数据缺 amount 列，流动性帽=0 将拒绝全部买单")

        valid_positions = [
            pos
            for pos, (_, row) in enumerate(raw_minute_data.iterrows())
            if _is_valid_ohlc_row(row)
        ]
        if not valid_positions:
            raise ValueError(
                f"No valid minute bars for {self.symbol} on {trade_date}"
            )
        last_valid_position = valid_positions[-1]
        sessions = {}
        effective_rule = self.session_calendar.get_rule(
            self.symbol, trade_date)
        rule_has_close_auction = any(
            session.kind is SessionKind.CLOSING_AUCTION
            for session in effective_rule.sessions
        )
        for position in valid_positions:
            stamp = pd.Timestamp(raw_minute_data.index[position])
            _, session = self.session_calendar.session_for_timestamp(
                self.symbol, stamp.to_pydatetime())
            sessions[position] = session
        opening_positions = [
            position for position, session in sessions.items()
            if session.kind is SessionKind.OPENING_AUCTION
        ]
        closing_positions = [
            position for position, session in sessions.items()
            if session.kind is SessionKind.CLOSING_AUCTION
        ]
        if len(opening_positions) > 1 or len(closing_positions) > 1:
            raise AssertionError(
                "strict session resampling produced duplicate auction bars")

        self._context._broker.reset_bar_used_amount()
        if opening_positions:
            opening_position = opening_positions[0]
            opening_row = raw_minute_data.iloc[opening_position]
            opening_price = float(opening_row['open'])
            opening_time = pd.Timestamp(opening_row.name)
            open_bar = BrokerBarData(
                symbol=self.symbol,
                trade_time=str(opening_time),
                open=opening_price,
                high=opening_price,
                low=opening_price,
                close=opening_price,
                vol=0.0,
                amount=_amount_yuan(opening_row.get('amount', 0)),
                pre_close=pre_close,
                adjustment_factor_ratio=adjustment_factor_ratio,
            )
            self._context._execute_open_orders(
                {self.symbol: open_bar}, match_at=opening_time)
            self._context._update_prices(
                {self.symbol: opening_price}, sim_time=opening_time,
                phase='opening_mark')
            self._capture_intraday_range(
                {self.symbol: opening_price},
                {self.symbol: opening_price},
            )
        else:
            open_checkpoint = (
                pd.to_datetime(trade_date)
                + pd.Timedelta(hours=9, minutes=30)
            )
            self._context._execute_open_orders(
                {}, match_at=open_checkpoint)

        deferred_date = next_trade_date or self._context.get_next_trade_day(
            trade_date)
        if deferred_date is None:
            deferred_date = (
                pd.to_datetime(trade_date) + pd.Timedelta(days=1)
            ).strftime('%Y%m%d')
        deferred_time = pd.to_datetime(str(deferred_date))

        prev_raw_bar = None
        for position, (idx, raw_row) in enumerate(raw_minute_data.iterrows()):
            # NaN bar 守卫：无效分钟 bar 按缺 bar 跳过（防 NaN 进撮合层）
            if not _is_valid_ohlc_row(raw_row):
                continue
            raw_bar = BarData.from_series(self.symbol, raw_row)

            broker_bar = BrokerBarData(
                symbol=self.symbol,
                trade_time=str(raw_bar.trade_time),
                open=raw_bar.open,
                high=raw_bar.high,
                low=raw_bar.low,
                close=raw_bar.close,
                vol=raw_bar.vol,
                amount=_amount_yuan(raw_bar.amount),  # 分钟线 amount 单位=元
                pre_close=pre_close,
                adjustment_factor_ratio=adjustment_factor_ratio,
            )
            session = sessions[position]
            is_close_auction = (
                session.kind is SessionKind.CLOSING_AUCTION)
            # A closing-auction bar contains observations stamped at its end.
            # Keep the last completed continuous bar visible through the
            # 14:58 on_before_close callback; publish the auction bar only
            # after the callback has returned.
            if not is_close_auction:
                self._context._set_current_bar(broker_bar)

            if prev_raw_bar is not None:
                self._context._broker.reset_bar_used_amount()
            self._capture_intraday_range(
                {self.symbol: float(raw_bar.high)},
                {self.symbol: float(raw_bar.low)},
            )

            if is_close_auction:
                callback_time = pd.Timestamp(
                    f"{pd.Timestamp(raw_bar.trade_time).date()} {session.start}"
                )
                auction_match_time = pd.Timestamp(raw_bar.trade_time)
                self._context.set_datetime(callback_time)
                with self._context._broker.order_creation_scope(
                        'on_before_close', callback_time,
                        eligible_from=deferred_time,
                        expire_date=str(deferred_date).replace('-', '')[:8],
                        close_eligible_from=auction_match_time,
                        close_expire_date=trade_date):
                    with self._context._broker.target_intent_scope(
                            auction_match_time):
                        self._strategy.on_before_close(self._context)
                self._context._set_current_bar(broker_bar)

            self._context.set_datetime(raw_bar.trade_time)

            # 盘前提交的 MARKET/LIMIT/STOP 在首个有效分钟 bar 即可生效。
            # 首 bar 不 reset，因而与刚执行的 OPEN 单共享同一份流动性帽；
            # 后续 bar 则已在上面 reset。
            self._context._execute_market_orders(
                broker_bar, match_at=raw_bar.trade_time)

            # CLOSE intent is matched before the execution bar can be exposed.
            # A distinct auction bar is never sent to on_minute.
            if is_close_auction or (
                position == last_valid_position
                and not rule_has_close_auction
            ):
                self._context._execute_close_orders(
                    {self.symbol: broker_bar}, match_at=raw_bar.trade_time)

            if is_close_auction:
                self._context._update_prices(
                    {self.symbol: raw_bar.close},
                    sim_time=raw_bar.trade_time,
                    phase='closing_mark')
                prev_raw_bar = raw_bar
                continue

            # Both lanes were validated and aligned before the first callback;
            # never substitute an execution-lane or positional row mid-run.
            strat_row = minute_data.loc[idx]
            strat_bar = BarData.from_series(self.symbol, strat_row)
            sim_time = pd.to_datetime(raw_bar.trade_time)
            with self._context._broker.order_creation_scope(
                    'on_minute', sim_time,
                    eligible_from=sim_time + pd.Timedelta(microseconds=1),
                    expire_date=trade_date):
                self._strategy.on_minute(self._context, strat_bar)

            self._context._update_prices(
                {self.symbol: raw_bar.close}, sim_time=raw_bar.trade_time,
                phase='continuous_mark')

            prev_raw_bar = raw_bar

    def _get_previous_daily_amount_yuan(
            self, trade_date: str) -> Optional[float]:
        """Return the exact prior trading day's execution-lane turnover."""
        policy = self.daily_auction_liquidity
        specs = () if policy is None else (
            policy.open_cap,
            policy.close_cap,
        )
        if not any(
                spec is not None
                and spec.mode is AuctionCapMode.PREVIOUS_DAY_RATIO
                for spec in specs):
            return None

        previous_day = getattr(
            self._data_provider, 'get_prev_trade_day', None)
        if not callable(previous_day):
            return None
        prior = previous_day(trade_date)
        if prior is None:
            return None
        prior_date = str(prior).replace('-', '')[:8]
        if self._previous_daily_amount_date == prior_date:
            return self._previous_daily_amount_yuan

        # This input is turnover, not a strategy price observation.  Read the
        # execution lane explicitly and validate the exact daily key; never
        # mirror the strategy lane or borrow another date.
        frame = self._data_provider.get_daily(
            self.symbol,
            start=prior_date,
            end=prior_date,
            price_mode='execution',
        )
        amount = _daily_amount_for_date(frame, prior_date, scale=1000.0)
        self._previous_daily_amount_date = prior_date
        self._previous_daily_amount_yuan = amount
        return amount

    def _run_daily_bar(self, trade_date: str,
                       next_trade_date: str = None) -> None:
        previous_day_amount = self._get_previous_daily_amount_yuan(trade_date)
        # Advance the cache even when today's symbol bar is absent.  A later
        # day must never reuse stale turnover from two or more sessions ago.
        self._previous_daily_amount_date = trade_date
        self._previous_daily_amount_yuan = None
        amount_scale = 1000.0
        if self._aligned_daily_data is not None:
            timestamp = pd.to_datetime(trade_date).normalize()
            strategy_frame = self._aligned_daily_data.strategy
            execution_frame = self._aligned_daily_data.execution
            strategy_daily = strategy_frame.loc[
                strategy_frame.index == timestamp]
            raw_daily = execution_frame.loc[
                execution_frame.index == timestamp]
            amount_scale = 1.0
        else:
            strategy_daily = self._data_provider.get_daily(
                self.symbol, start=trade_date, end=trade_date,
                price_mode='strategy'
            )
            raw_daily = self._data_provider.get_daily(
                self.symbol, start=trade_date, end=trade_date,
                price_mode='execution'
            )

        raw_missing = raw_daily is None or raw_daily.empty
        strategy_missing = strategy_daily is None or strategy_daily.empty
        if raw_missing and strategy_missing:
            return
        if raw_missing:
            raise ValueError(
                f"Daily execution data required for {self.symbol} on "
                f"{trade_date}; missing price lane cannot be substituted"
            )
        if strategy_missing:
            raise ValueError(
                f"Daily strategy data required for {self.symbol} on "
                f"{trade_date}; missing price lane cannot be substituted"
            )
        if len(raw_daily) != 1 or len(strategy_daily) != 1:
            raise ValueError(
                f"Daily data key must be unique for {self.symbol} on "
                f"{trade_date}: strategy_rows={len(strategy_daily)}, "
                f"execution_rows={len(raw_daily)}"
            )

        expected_date = pd.to_datetime(trade_date).normalize()
        try:
            strategy_date = pd.to_datetime(strategy_daily.index[0]).normalize()
            execution_date = pd.to_datetime(raw_daily.index[0]).normalize()
        except Exception as exc:
            raise ValueError(
                f"Daily timestamp invalid for {self.symbol} on {trade_date}"
            ) from exc
        if strategy_date != expected_date or execution_date != expected_date:
            raise ValueError(
                f"Daily timestamp mismatch for {self.symbol} on {trade_date}: "
                f"strategy={strategy_date}, execution={execution_date}"
            )

        raw_row = raw_daily.iloc[0]
        # NaN bar 守卫：无效 bar 按缺 bar 处理（防 NaN 价格污染账户）
        if not _is_valid_ohlc_row(raw_row):
            logger.warning(f"跳过无效 bar: {self.symbol} {trade_date} (OHLC 含 NaN/非正值)")
            return
        val = raw_row.get('pre_close', 0)
        pre_close = float(val) if pd.notna(val) else 0.0
        adjustment_factor_ratio = _execution_adjustment_factor_change(
            self._data_provider, self.symbol, trade_date)

        strat_row = strategy_daily.iloc[0]
        if not _is_valid_ohlc_row(strat_row):
            raise ValueError(
                f"Daily strategy data invalid for {self.symbol} on {trade_date}"
            )
        amount_yuan = _amount_yuan(
            raw_row.get('amount', 0), scale=amount_scale)
        bar = BarData(
            symbol=self.symbol,
            trade_time=pd.to_datetime(trade_date),
            open=strat_row['open'],
            high=strat_row['high'],
            low=strat_row['low'],
            close=strat_row['close'],
            vol=strat_row.get('vol', 0),
            amount=amount_yuan,
        )

        whole_day_bar = BrokerBarData(
            symbol=self.symbol,
            trade_time=trade_date,
            open=raw_row['open'],
            high=raw_row['high'],
            low=raw_row['low'],
            close=raw_row['close'],
            vol=raw_row.get('vol', 0),
            amount=amount_yuan,
            pre_close=pre_close,
            adjustment_factor_ratio=adjustment_factor_ratio,
        )
        # Raw OHLC is evidence for the day's legal band.  Validate it before
        # any auction, fill, or strategy callback; a contradictory override
        # must fail even when no order happens to be live that day.
        self._context._broker.validate_execution_bar_price_band(whole_day_bar)
        open_liquidity_cap = _resolve_daily_auction_cap(
            self.daily_auction_liquidity,
            AuctionWindow.OPEN,
            previous_day_amount,
        )
        close_liquidity_cap = _resolve_daily_auction_cap(
            self.daily_auction_liquidity,
            AuctionWindow.CLOSE,
            previous_day_amount,
        )
        open_price = float(raw_row['open'])
        close_price = float(raw_row['close'])
        open_bar = BrokerBarData(
            symbol=self.symbol,
            trade_time=trade_date,
            open=open_price,
            high=open_price,
            low=open_price,
            close=open_price,
            vol=0.0,
            amount=0.0,
            pre_close=pre_close,
            adjustment_factor_ratio=adjustment_factor_ratio,
            liquidity_cap_amount=open_liquidity_cap,
            require_explicit_liquidity_cap=True,
            liquidity_window=AuctionWindow.OPEN.value,
        )
        close_bar = BrokerBarData(
            symbol=self.symbol,
            trade_time=trade_date,
            open=close_price,
            high=close_price,
            low=close_price,
            close=close_price,
            vol=0.0,
            amount=0.0,
            pre_close=pre_close,
            adjustment_factor_ratio=adjustment_factor_ratio,
            liquidity_cap_amount=close_liquidity_cap,
            require_explicit_liquidity_cap=True,
            liquidity_window=AuctionWindow.CLOSE.value,
        )
        self._context._broker.reset_bar_used_amount()
        open_time = pd.to_datetime(trade_date) + pd.Timedelta(hours=9, minutes=30)
        close_time = pd.to_datetime(trade_date) + pd.Timedelta(hours=15)
        self._context._execute_open_orders(
            {self.symbol: open_bar}, match_at=open_time)
        self._context._update_prices(
            {self.symbol: open_price}, sim_time=open_time,
            phase='opening_mark')
        self._context._set_current_bar(open_bar)
        self._capture_intraday_range(
            {self.symbol: open_price},
            {self.symbol: open_price},
        )

        eligible_date = next_trade_date or self._context.get_next_trade_day(trade_date)
        if eligible_date is None:
            eligible_date = (
                pd.to_datetime(trade_date) + pd.Timedelta(days=1)
            ).strftime('%Y%m%d')
        eligible_time = pd.to_datetime(str(eligible_date))
        self._context.set_datetime(open_time)
        with self._context._broker.order_creation_scope(
                'on_after_open', open_time,
                eligible_from=eligible_time,
                expire_date=str(eligible_date).replace('-', '')[:8],
                close_eligible_from=close_time,
                close_expire_date=trade_date):
            with self._context._broker.target_intent_scope(close_time):
                self._strategy.on_after_open(
                    self._context,
                    OpenBarData(
                        symbol=self.symbol,
                        trade_time=open_time,
                        open=open_price,
                    ),
                )
        # T 的全部撮合窗口必须先于读取完整 T bar 的 on_day(T)。
        self._context._broker.reset_bar_used_amount()
        # Read-only pessimistic audit path: open was sampled above; for the
        # full daily bar record high before low, then matching and close mark
        # continue in their pre-existing order.
        self._capture_intraday_range(
            {self.symbol: float(raw_row['high'])},
            {self.symbol: float(raw_row['low'])},
        )
        self._context._execute_market_orders(
            whole_day_bar, match_at=close_time)
        self._context._broker.reset_bar_used_amount()
        self._context._execute_close_orders(
            {self.symbol: close_bar}, match_at=close_time)
        # T 日有效单的余量在后验回调前终结，避免策略看到幽灵挂单/冻结。
        self._context._broker.cancel_day_end_orders(sim_time=close_time)
        self._context._update_prices(
            {self.symbol: float(raw_row['close'])}, sim_time=close_time,
            phase='closing_mark')
        self._context._set_current_bar(whole_day_bar)

        self._context.set_datetime(close_time)
        with self._context._broker.order_creation_scope(
                'on_day', close_time,
                eligible_from=eligible_time,
                expire_date=str(eligible_date).replace('-', '')[:8]):
            self._strategy.on_day(self._context, bar)
        self._previous_daily_amount_yuan = (
            whole_day_bar.amount if whole_day_bar.amount > 0 else None
        )

    def _record_daily_value(self, trade_date: str) -> None:
        """记录每日净值"""
        close_market_value = self._context._broker.account.market_value
        close_total_value = self._context.get_total_asset()
        intraday_high_market_value = (
            self._current_day_high_market_value
            if self._current_day_high_market_value is not None
            else close_market_value
        )
        intraday_high_total_value = (
            self._current_day_high_total_value
            if self._current_day_high_total_value is not None
            else close_total_value
        )
        intraday_low_market_value = (
            self._current_day_low_market_value
            if self._current_day_low_market_value is not None
            else close_market_value
        )
        intraday_low_total_value = (
            self._current_day_low_total_value
            if self._current_day_low_total_value is not None
            else close_total_value
        )
        self._daily_values.append({
            'date': trade_date,
            'cash': self._context.get_cash(),
            'market_value': close_market_value,
            'total_value': close_total_value,
            'intraday_high_market_value': intraday_high_market_value,
            'intraday_high_total_value': intraday_high_total_value,
            'intraday_low_market_value': intraday_low_market_value,
            'intraday_low_total_value': intraday_low_total_value,
        })

    def _reset_intraday_low_tracker(self) -> None:
        self._current_day_high_market_value = None
        self._current_day_high_total_value = None
        self._current_day_low_market_value = None
        self._current_day_low_total_value = None

    def _capture_intraday_range(
        self,
        high_prices: Dict[str, float],
        low_prices: Dict[str, float],
    ) -> None:
        """Sample high NAV before low NAV without mutating execution state."""
        if self._context is None or self._context._broker is None:
            return
        account = self._context._broker.account

        # Ordering is deliberate and auditable: high, then low.  Both calls
        # are pure mark-to-market estimates and do not move orders or cash.
        high_market_value = account.estimate_market_value(high_prices)
        high_total_value = (
            account.cash + account.frozen_cash + high_market_value
        )
        if (
            self._current_day_high_total_value is None
            or high_total_value > self._current_day_high_total_value
        ):
            self._current_day_high_market_value = high_market_value
            self._current_day_high_total_value = high_total_value

        low_market_value = account.estimate_market_value(low_prices)
        low_total_value = account.cash + account.frozen_cash + low_market_value
        if (
            self._current_day_low_total_value is None
            or low_total_value < self._current_day_low_total_value
        ):
            self._current_day_low_market_value = low_market_value
            self._current_day_low_total_value = low_total_value

    def _capture_intraday_low(self, prices: Dict[str, float]) -> None:
        """Compatibility wrapper for callers of the former private helper."""
        self._capture_intraday_range(prices, prices)

    def _cash_result_projection(self):
        bundle = self.last_cash_audit
        if bundle is None:
            broker = (
                self._context._broker
                if self._context is not None else None
            )
            if broker is None or not hasattr(broker, "event_journal"):
                # Compatibility for direct private result-builder tests and
                # third-party broker doubles predating event journaling. A
                # public run() never takes this branch.
                return None, None, []
            raise RuntimeError(
                "current cash result requires terminal replay evidence"
            )
        if type(bundle) is not CashAuditBundle:
            raise RuntimeError(
                "current cash result requires terminal replay evidence"
            )
        replay = bundle.replay()
        return bundle, replay, cash_replay_trade_records(bundle)

    def _create_result(
        self,
        result_contract: Optional[ResultContract] = None,
    ) -> BacktestResult:
        """创建回测结果"""
        if not self._daily_values:
            return self._create_empty_result(
                result_contract=result_contract
            )

        df = pd.DataFrame(self._daily_values)
        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')

        initial = self.initial_cash
        final = float(df['total_value'].iloc[-1])
        close_nav = tuple(float(value) for value in df['total_value'])
        intraday_low_nav = tuple(
            min(float(low), close)
            for low, close in zip(
                df['intraday_low_total_value'].fillna(df['total_value']),
                close_nav,
            )
        )
        intraday_high_nav = None
        if 'intraday_high_total_value' in df:
            intraday_high_nav = tuple(
                max(float(high), close, low)
                for high, close, low in zip(
                    df['intraday_high_total_value'],
                    close_nav,
                    intraday_low_nav,
                )
            )
        metric_result = MetricEngine(self._metric_config()).calculate(
            initial_nav=initial,
            dates=tuple(value.strftime('%Y-%m-%d') for value in df.index),
            close_nav=close_nav,
            intraday_low_nav=intraday_low_nav,
            intraday_high_nav=intraday_high_nav,
        )
        total_return = metric_result.total_return
        annual_return = metric_result.annual_return
        max_drawdown_close_nav = metric_result.max_drawdown_close_nav
        max_drawdown_intraday_low_nav = (
            metric_result.max_drawdown_intraday_low_nav
        )
        max_drawdown_intraday_high_to_low = (
            metric_result.max_drawdown_intraday_high_to_low
        )
        max_drawdown = max_drawdown_close_nav
        sharpe = metric_result.sharpe_ratio
        df['daily_return'] = metric_result.daily_returns
        df['drawdown_close_nav'] = metric_result.close_drawdowns
        df['drawdown_intraday_low_nav'] = (
            metric_result.causal_intraday_low_drawdowns
        )
        if metric_result.intraday_high_to_low_drawdowns is not None:
            df['drawdown_intraday_high_to_low'] = (
                metric_result.intraday_high_to_low_drawdowns
            )

        # 交易统计
        cash_audit, cash_replay, trades = self._cash_result_projection()

        # Issue 5 修复：正确计算胜率
        # 按股票分组，计算每只股票的盈亏
        return BacktestResult(
            symbol=self.symbol,
            start_date=self.start_date,
            end_date=self.end_date,
            initial_cash=initial,
            final_value=final,
            total_return=total_return,
            annual_return=annual_return,
            max_drawdown=max_drawdown,
            trade_count=(0 if cash_replay is None else len(cash_replay.trades)),
            win_rate=(None if cash_replay is None else cash_replay.win_rate),
            sharpe_ratio=sharpe,
            max_drawdown_close_nav=max_drawdown_close_nav,
            max_drawdown_intraday_low_nav=max_drawdown_intraday_low_nav,
            max_drawdown_intraday_high_to_low=(
                max_drawdown_intraday_high_to_low
            ),
            daily_values=df,
            trades=trades,
            result_contract=result_contract,
            target_execution=self._target_execution_bundle(),
            cash_audit=cash_audit,
        )

    def _target_execution_bundle(self) -> Optional[TargetExecutionBundle]:
        """Join immutable broker target snapshots at the result boundary."""

        broker = (
            self._context._broker
            if self._context is not None else None
        )
        if broker is None:
            # A legacy/direct result construction without an initialized broker
            # cannot prove that no target decision occurred.
            return None
        intents_snapshot = getattr(broker, "target_intents_snapshot", None)
        achievements_snapshot = getattr(
            broker, "target_achievements_snapshot", None
        )
        if not callable(intents_snapshot) or not callable(achievements_snapshot):
            # Compatibility-only broker doubles predate target journaling.  Do
            # not mislabel their missing evidence as an explicit empty run.
            return None
        return TargetExecutionBundle.from_snapshots(
            intents_snapshot(),
            achievements_snapshot(),
        )

    def _calculate_win_rate(self, trades: List) -> Optional[float]:
        """
        计算胜率

        Issue 5 修复：正确计算胜率
        使用FIFO方式匹配买卖订单，计算每笔完整交易的盈亏

        Args:
            trades: 订单历史列表

        Returns:
            胜率 (0-1)
        """
        from collections import defaultdict

        # 使用引擎实际配置的费率（历史bug：此处曾硬编码万2.5+千1，
        # 与用户配置脱钩，全零费率下赚钱的笔会被判亏）
        account = self._context._broker.account if self._context else None

        # 按股票分组
        buy_queues = defaultdict(list)  # {symbol: [(shares, price, cost), ...]}
        round_trips = []  # [(profit, cost), ...]

        for order in trades:
            if order.filled_shares <= 0:
                continue

            symbol = order.symbol
            trade_date = getattr(order, 'create_date', None)
            gross = order.filled_shares * order.avg_price

            if order.is_buy:
                # 买入：记录成本（含实际配置费率的手续费）
                if account is not None:
                    fee = account.calc_commission(gross, is_sell=False)
                else:
                    fee = 0.0
                cost_per_share = order.avg_price + fee / order.filled_shares
                buy_queues[symbol].append({
                    'shares': order.filled_shares,
                    'cost_per_share': cost_per_share,
                })
            else:
                # 卖出：与买入队列匹配（FIFO），净得按实际配置费率扣减
                sell_shares = order.filled_shares
                if account is not None:
                    fee = account.calc_commission(
                        gross, is_sell=True, symbol=symbol, trade_date=trade_date)
                else:
                    fee = 0.0
                sell_price_per_share = order.avg_price - fee / order.filled_shares

                while sell_shares > 0 and buy_queues[symbol]:
                    buy_record = buy_queues[symbol][0]

                    match_shares = min(sell_shares, buy_record['shares'])

                    # 计算这部分的盈亏
                    buy_cost = match_shares * buy_record['cost_per_share']
                    sell_proceeds = match_shares * sell_price_per_share
                    profit = sell_proceeds - buy_cost

                    round_trips.append((profit, buy_cost))

                    # 更新剩余
                    sell_shares -= match_shares
                    buy_record['shares'] -= match_shares

                    if buy_record['shares'] <= 0:
                        buy_queues[symbol].pop(0)

        return calculate_trade_round_metrics(
            profit for profit, _ in round_trips
        ).win_rate

    def _create_empty_result(
        self,
        result_contract: Optional[ResultContract] = None,
    ) -> BacktestResult:
        """创建空结果"""
        cash_audit, cash_replay, trades = self._cash_result_projection()
        return BacktestResult(
            symbol=self.symbol,
            start_date=self.start_date,
            end_date=self.end_date,
            initial_cash=self.initial_cash,
            final_value=self.initial_cash,
            total_return=0,
            annual_return=0,
            max_drawdown=0,
            trade_count=(0 if cash_replay is None else len(cash_replay.trades)),
            win_rate=(None if cash_replay is None else cash_replay.win_rate),
            sharpe_ratio=None,
            max_drawdown_close_nav=0,
            max_drawdown_intraday_low_nav=0,
            max_drawdown_intraday_high_to_low=None,
            trades=trades,
            result_contract=result_contract,
            target_execution=self._target_execution_bundle(),
            cash_audit=cash_audit,
        )
