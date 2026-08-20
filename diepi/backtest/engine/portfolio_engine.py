"""
组合回测引擎

支持多股票共享资金的组合策略回测
"""

import logging
import math
import time
from typing import Optional, List, Dict, Any, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from .portfolio_context import PortfolioContext
from .window import WindowBound, bound_cash_trade_days
from .backtest_engine import (
    _add_adjustment_factor_identity_assumption,
    _add_execution_model_assumptions,
    _amount_yuan,
    _daily_amount_for_date,
    _execution_adjustment_factor_change,
    _is_valid_ohlc_row,
    _resolve_daily_auction_cap,
)
from .minute_resampler import resample_minute_data, ResampleConfig
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
from ..strategy.portfolio_strategy import (
    PortfolioStrategy,
    PortfolioBarData,
    PortfolioOpenBarData,
)
from ..strategy.base import BarData, OpenBarData
from ..broker import (
    Account,
    Broker,
    CashAuditBundle,
    CashReplaySeed,
    cash_replay_trade_records,
)
from ..broker.broker import BarData as BrokerBarData
from ..data import DataProvider
from ..broker.target_execution import TargetExecutionBundle
from ..data.contract import (
    AmountUnit,
    DataContractError,
    Frequency,
    validate_and_align_pair,
)
from ..config import PRICE_MODE_STRATEGY, PRICE_MODE_EXECUTION
from ..comparison import (
    ComparisonBundle,
    reference_total_return_excess as _reference_total_return_excess,
)
from ..data.stock_pool import PointInTimeUniverseError, PoolSource
from ..rulebook import (
    DEFAULT_RULEBOOK,
    ExecutionEngine,
    LIMIT_BAND_SNAPSHOT_HASH,
    LIMIT_BAND_SOURCE_VERSION,
    RuleBook,
)
from ..metrics import (
    MetricConfig,
    MetricEngine,
)
from ..outcome import OutcomeTracker, add_trade_calendar_assumptions
from ..result_contract import ResultContract, ResultReason, ResultStatus

logger = logging.getLogger(__name__)

# 支持的分钟频率
MINUTE_FREQS = {'1min', '5min', '15min', '30min', '60min', 'minute'}


def _is_empty_pair_contract_error(error: DataContractError) -> bool:
    """Return True only for a genuinely empty two-lane market-data window.

    DC-1 also reports schema consequences (missing columns/timestamps) for an
    empty frame.  Those consequences may follow ``on_missing_data`` policy,
    while every contract failure involving at least one non-empty lane must
    remain fail-fast.
    """
    report = getattr(error, 'report', None)
    return bool(
        report is not None
        and report.strategy_rows == 0
        and report.execution_rows == 0
        and 'MISSING_DATASET' in report.issue_codes
    )


@dataclass
class PortfolioResult:
    """
    组合回测结果

    包含回测统计和详细记录
    """
    start_date: str
    end_date: str
    initial_cash: float
    final_value: float
    total_return: float
    annual_return: float
    max_drawdown: float
    trade_count: int
    sharpe_ratio: Optional[float]
    max_drawdown_close_nav: float = 0.0
    max_drawdown_intraday_low_nav: float = 0.0
    max_drawdown_intraday_high_to_low: Optional[float] = None

    # Deprecated legacy price-index fields.  Retained for artifact/API
    # compatibility; they are not explicit total-return comparisons.
    benchmark_code: str = ''
    benchmark_return: float = 0.0
    excess_return: float = 0.0

    # 每日净值
    daily_values: pd.DataFrame = field(default_factory=pd.DataFrame)

    # 交易记录
    trades: List[Dict] = field(default_factory=list)

    # 持仓历史
    position_history: List[Dict] = field(default_factory=list)
    execution_stats: Dict[str, Any] = field(default_factory=dict)

    # 无已平仓交易时保持 None，避免伪造 0% 胜率。
    win_rate: Optional[float] = None

    # 版本化终态与覆盖证据；历史构造器可保持 None。
    result_contract: Optional[ResultContract] = None

    # 错误信息
    error: str = ""

    # Explicitly typed comparisons; never inferred from legacy benchmark_code.
    comparisons: Optional[ComparisonBundle] = None

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
                "SUCCESS PortfolioResult requires complete target execution evidence"
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
                "PortfolioResult trades must equal the cash replay fill view"
            )
        if self.trade_count != len(replay.trades):
            raise ValueError(
                "PortfolioResult trade_count must equal replay fill count"
            )
        if self.win_rate != replay.win_rate:
            raise ValueError(
                "PortfolioResult win_rate must equal replay closed-round rate"
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
                "PortfolioResult final_value must equal replay terminal NAV"
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
                "PortfolioResult initial_cash must equal replay initial NAV"
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
                "PortfolioResult total_return must equal final/initial NAV return"
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

    @property
    def reference_total_return_excess(self) -> Optional[float]:
        return _reference_total_return_excess(
            strategy_total_return=self.total_return,
            result_contract=self.result_contract,
            daily_values=self.daily_values,
            comparisons=self.comparisons,
        )

    def to_dict(self) -> Dict:
        """转换为字典"""
        self._validate_target_execution()
        self._validate_cash_audit()
        return {
            'start_date': self.start_date,
            'end_date': self.end_date,
            'initial_cash': self.initial_cash,
            'final_value': round(self.final_value, 2),
            'total_return': round(self.total_return * 100, 2),
            'benchmark_return': round(self.benchmark_return * 100, 2),
            'excess_return': round(self.excess_return * 100, 2),
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
            'sharpe_ratio': (
                None if self.sharpe_ratio is None
                else round(self.sharpe_ratio, 3)
            ),
            'win_rate': (
                None if self.win_rate is None
                else round(self.win_rate * 100, 2)
            ),
            'execution_stats': self.execution_stats,
            'result_contract': (
                None
                if self.result_contract is None
                else self.result_contract.to_dict()
            ),
            'comparisons': (
                None
                if self.comparisons is None
                else self.comparisons.to_dict()
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
            'reference_total_return_excess': (
                None
                if self.reference_total_return_excess is None
                else round(self.reference_total_return_excess * 100, 2)
            ),
        }

    def summary(self) -> str:
        """生成摘要文本"""
        lines = [
            f"回测期间: {self.start_date} ~ {self.end_date}",
            f"初始资金: {self.initial_cash:,.0f}",
            f"最终资产: {self.final_value:,.0f}",
            f"总收益率: {self.total_return * 100:.2f}%",
        ]
        if self.benchmark_code:
            lines.append(f"基准收益: {self.benchmark_return * 100:.2f}% ({self.benchmark_code})")
            lines.append(f"超额收益: {self.excess_return * 100:.2f}%")
        lines.extend([
            f"年化收益: {self.annual_return * 100:.2f}%",
            f"最大回撤: {self.max_drawdown * 100:.2f}%",
            "夏普比率: " + (
                "N/A" if self.sharpe_ratio is None
                else f"{self.sharpe_ratio:.3f}"
            ),
            f"交易次数: {self.trade_count}",
        ])
        return '\n'.join(lines)


class PortfolioEngine:
    """
    组合回测引擎

    支持:
    1. 多股票共享资金
    2. 动态选股 (on_before_market_open)
    3. 日线/分钟级回测

    Example:
        engine = PortfolioEngine(
            start_date='20240101',
            end_date='20241231',
            initial_cash=1000000,
            pool_source=PoolSource.SPECIFIED,
            pool_symbols=['000001.SZ', '000002.SZ', '600000.SH'],
        )
        result = engine.run(MyPortfolioStrategy())
        print(result.summary())
    """

    def __init__(
        self,
        start_date: str,
        end_date: str,
        initial_cash: float = 1000000.0,
        freq: str = 'daily',
        pool_source: PoolSource = PoolSource.ALL_MARKET,
        pool_symbols: List[str] = None,
        pool_industry: str = None,
        slippage: float = 0.001,
        commission_rate: float = 0.00025,
        stamp_duty: float = 0.001,
        min_commission: float = 5.0,
        lot_size: int = 100,
        strategy_price_mode: str = PRICE_MODE_STRATEGY,
        execution_price_mode: str = PRICE_MODE_EXECUTION,
        benchmark: str = '000300.SH',
        max_days: int = None,
        progress_callback: Callable[[int, int, str], None] = None,
        stop_check: Callable[[], bool] = None,
        daily_callback: Callable[[List[Dict]], None] = None,
        update_interval: int = 50,
        open_buy_resize_mode: str = 'auto',
        liquidity_cap_ratio: float = 0.8,
        limit_pct_overrides: Dict[str, float] = None,
        open_buy_fill_mode: str = 'open+slip',
        open_buy_sizing: str = 'limit_up',
        bar_liquidity_model: str = 'close_lock',
        t0_overrides=None,
        trading_days_per_year: int = 252,
        risk_free_rate: float = 0.03,
        on_missing_data: str = None,
        daily_auction_liquidity: Optional[
            DailyAuctionLiquidityPolicy] = None,
        rulebook: RuleBook = None,
        transfer_fee_rate: float = 0.0,
        data_root=None,
        market_data_observer: Optional[Callable[..., None]] = None,
    ):
        """
        Args:
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            initial_cash: 初始资金
            freq: 回测频率 ('minute' 或 'daily')
            pool_source: 股票池来源
            pool_symbols: 指定股票列表
            pool_industry: 行业名称
            slippage: 滑点 (默认千1)
            commission_rate: 佣金率 (默认万2.5)
            stamp_duty: 印花税 (默认千1)
            transfer_fee_rate: 双边过户费率 (默认0；不做历史自动切换)
            min_commission: 单笔最低佣金 (默认5元)
            lot_size: 每手股数 (默认100)
            benchmark: 基准指数代码 (默认沪深300)
            max_days: 最大运行天数 (用于语法检查)
            progress_callback: 进度回调函数 (current, total, message)
            stop_check: 停止检查回调函数 (返回 True 则停止)
            daily_callback: 每日结束回调函数 (daily_values_list)
            update_interval: UI更新间隔天数 (默认10天更新一次，0=禁用)
            open_buy_resize_mode: 开盘买单缩量模式 ('auto'/'legacy')
            liquidity_cap_ratio: 单根K线最大可吃成交额比例 (默认0.8)
            limit_pct_overrides: 涨跌停幅度覆盖表 {代码: 幅度}，优先于内置规则
            open_buy_fill_mode: 竞价买单成交价 'open+slip'(默认,含滑点惩罚)/'open'(旧口径)
            open_buy_sizing: 竞价买单委托量折算 'limit_up'(默认,按涨停价)/'fill'(旧口径)
            t0_overrides: T+0 品种集合（代码/前缀），命中者当日买入当日可卖
            trading_days_per_year: 年化交易日基数 (默认252，A股建议244)
            risk_free_rate: 夏普无风险利率 (默认0.03)
        """
        self.start_date = start_date
        self.end_date = end_date
        self.initial_cash = initial_cash
        if type(freq) is not str:
            raise TypeError("freq must be exactly str")
        if freq != 'daily' and freq not in MINUTE_FREQS:
            allowed = ', '.join(('daily', *sorted(MINUTE_FREQS)))
            raise ValueError(f"freq must be one of: {allowed}")
        self.freq = freq
        self.pool_source = pool_source
        self.pool_symbols = pool_symbols
        self.pool_industry = pool_industry
        self.slippage = slippage
        self.commission_rate = commission_rate
        self.stamp_duty = stamp_duty
        self.transfer_fee_rate = transfer_fee_rate
        # Dataset selection is per engine.  Explicit paths must not require
        # changing DATA_ROOT or reloading configuration modules in-process.
        self.data_root = data_root
        if market_data_observer is not None and not callable(
            market_data_observer
        ):
            raise TypeError("market_data_observer must be callable or None")
        self.market_data_observer = market_data_observer
        self.min_commission = min_commission
        self.lot_size = lot_size
        self.strategy_price_mode = strategy_price_mode
        self.execution_price_mode = execution_price_mode
        self.benchmark = benchmark
        if max_days is not None and (
            type(max_days) is not int or max_days <= 0
        ):
            raise ValueError("max_days must be a positive integer or None")
        self.max_days = max_days
        self.progress_callback = progress_callback
        self.stop_check = stop_check
        self.daily_callback = daily_callback
        self.update_interval = update_interval
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
        # 数据缺失策略: None=自动(指定池raise/其余warn) | 'raise' | 'warn' | 'ignore'
        self.on_missing_data = on_missing_data
        self._preload_missing: List[str] = []
        self._preload_invalid: List[str] = []
        self._runtime_market_data_gaps = set()
        self._provenance_incomplete_symbols = set()
        self._realized_market_symbols = set()
        self._strategy_warnings: Dict[str, str] = {}
        self._max_days_truncated = False
        # GIL 让出只在 GUI 场景需要（daily_callback/stop_check 是 GUI worker 的签名）；
        # 纯 CLI/API 回测无条件 sleep 曾占长回测 94% 耗时（13.5年 25s→1.5s）
        self._needs_gil_yield = bool(daily_callback or stop_check)

        self._context: Optional[PortfolioContext] = None
        self._strategy: Optional[PortfolioStrategy] = None
        self._data_provider: Optional[DataProvider] = None
        self._data_quality_reports: List[Any] = []
        self._data_contract_compatibility_symbols = set()
        self._daily_amount_yuan_symbols = set()
        self._previous_daily_amount_cache: Dict[
            str, tuple] = {}

        # 每日净值记录
        self._daily_values: List[Dict] = []
        self._current_day_high_market_value: Optional[float] = None
        self._current_day_high_total_value: Optional[float] = None
        self._current_day_low_market_value: Optional[float] = None
        self._current_day_low_total_value: Optional[float] = None

        # 持仓历史
        self._position_history: List[Dict] = []

        # 数据缓存
        self._daily_data_cache: Dict[str, pd.DataFrame] = {}
        self._daily_data_raw_cache: Dict[str, pd.DataFrame] = {}

        # 日期索引缓存 {symbol: {date_str: row_index}}
        self._date_index_cache: Dict[str, Dict[str, int]] = {}
        self._date_index_raw_cache: Dict[str, Dict[str, int]] = {}

        # 停止标志
        self._stopped = False
        self.last_result_contract: Optional[ResultContract] = None
        self._outcome_tracker: Optional[OutcomeTracker] = None
        self._window_bound: Optional[WindowBound] = None
        self._cash_replay_seed: Optional[CashReplaySeed] = None
        self.last_cash_audit: Optional[CashAuditBundle] = None

    @property
    def data_quality_reports(self) -> tuple:
        """Immutable snapshot of data-contract reports observed by this run."""

        return tuple(self._data_quality_reports)

    @property
    def realized_market_symbols(self) -> tuple:
        """Canonical realized symbol scope observed by this engine run."""

        return tuple(sorted(self._realized_market_symbols))

    def _observe_market_symbols(
        self,
        symbols: Iterable[str],
        *,
        frequency: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        require_direct_prices: bool = True,
    ) -> None:
        if isinstance(symbols, (str, bytes)):
            raise TypeError("market symbols must be an iterable of strings")
        observed = {
            str(value).strip()
            for value in symbols
            if str(value).strip()
        }
        canonical = tuple(
            sorted(observed - self._realized_market_symbols)
        )
        if not canonical and frequency is None:
            return
        # The external observer must freeze inputs before the provider reads
        # them.  Commit the realized scope only after that succeeds.
        if self.market_data_observer is not None:
            kwargs = {
                "frequency": frequency,
                "start_date": start_date,
                "end_date": end_date,
            }
            if not require_direct_prices:
                kwargs["require_direct_prices"] = False
            incomplete = self.market_data_observer(
                tuple(sorted(observed)), **kwargs
            )
            if incomplete is not None:
                if isinstance(incomplete, (str, bytes)):
                    raise TypeError(
                        "market-data observer incomplete scope must be iterable"
                    )
                unresolved = {
                    str(value).strip()
                    for value in incomplete
                    if str(value).strip()
                }
                if not unresolved.issubset(observed):
                    raise ValueError(
                        "market-data observer returned symbols outside its scope"
                    )
                self._provenance_incomplete_symbols.update(unresolved)
        self._realized_market_symbols.update(canonical)

    def _record_strategy_warning(self, code: str, message: str) -> None:
        previous = self._strategy_warnings.get(code)
        if previous is not None:
            if previous != message:
                raise ValueError(
                    f"strategy warning code {code!r} has conflicting messages"
                )
            return
        self._strategy_warnings[code] = message
        if self._outcome_tracker is not None:
            self._outcome_tracker.add_warning(code, message)

    def _record_runtime_market_gap(
        self, symbol: str, trade_date: str
    ) -> None:
        self._runtime_market_data_gaps.add((symbol, trade_date))
        code = "RUNTIME_MARKET_DATA_INCOMPLETE"
        if code in self._strategy_warnings:
            return
        self._record_strategy_warning(
            code,
            "At least one active symbol/date lacked usable market data; "
            "the result cannot represent the full realized universe",
        )

    def _metric_config(self) -> MetricConfig:
        return MetricConfig(
            trading_days_per_year=self.trading_days_per_year,
            risk_free_rate=self.risk_free_rate,
        )

    @staticmethod
    def _canonical_trade_day(trade_date: object) -> str:
        return pd.to_datetime(str(trade_date)).strftime('%Y-%m-%d')

    def _loaded_daily_data_dates(self) -> tuple:
        """Return the single global end date observed in either daily lane."""

        maxima = []
        for cache in (
            self._daily_data_cache,
            self._daily_data_raw_cache,
        ):
            for frame in cache.values():
                if frame is None or frame.empty or len(frame.index) == 0:
                    continue
                try:
                    value = frame.index.max()
                    parsed = pd.to_datetime(str(value), errors="coerce")
                except (TypeError, ValueError):
                    continue
                if pd.isna(parsed):
                    continue
                maxima.append(pd.Timestamp(parsed).strftime("%Y%m%d"))
        return (max(maxima),) if maxima else ()

    def _add_universe_contract_evidence(
        self, tracker: OutcomeTracker
    ) -> None:
        if self.pool_source not in (
            PoolSource.ALL_MARKET,
            PoolSource.INDUSTRY,
        ):
            return
        tracker.add_assumption(
            "universe.listing_membership",
            "point_in_time_list_date_inclusive_delist_date_exclusive",
        )
        tracker.add_assumption(
            "universe.preload_scope",
            "securities_whose_listing_interval_overlaps_requested_window",
        )
        tracker.add_assumption(
            "universe.st_filter",
            "not_applied_historical_st_status_unavailable",
        )
        tracker.add_warning(
            "UNIVERSE_ST_HISTORY_UNAVAILABLE",
            "Historical ST-status snapshots are unavailable; current names "
            "were not used to exclude securities from a historical pool.",
        )
        if self.pool_source is PoolSource.INDUSTRY:
            tracker.add_assumption(
                "universe.industry_membership",
                "current_snapshot_intersected_with_point_in_time_listing_interval",
            )
            tracker.add_warning(
                "UNIVERSE_INDUSTRY_SNAPSHOT_BIAS",
                "Historical industry constituents are unavailable; the current "
                "industry snapshot was used and the result is not rankable.",
            )

    def _add_data_contract_compatibility_evidence(
        self, tracker: OutcomeTracker
    ) -> None:
        symbols = tuple(sorted(self._data_contract_compatibility_symbols))
        if not symbols:
            return
        tracker.add_assumption(
            "data.contract_path", "legacy_provider_compatibility"
        )
        tracker.add_assumption(
            "data.contract_compatibility_symbols", ",".join(symbols)
        )
        shown = ", ".join(symbols[:10])
        suffix = "..." if len(symbols) > 10 else ""
        tracker.add_warning(
            "DATA_CONTRACT_COMPATIBILITY_PATH",
            "The provider does not expose the aligned DC-1 pair contract; "
            f"legacy compatibility was used for {len(symbols)} symbol(s): "
            f"{shown}{suffix}",
        )

    def _finalize_completed_outcome(
        self, tracker: OutcomeTracker
    ) -> ResultContract:
        if (
            "COMBO_WARM_START_UNAVAILABLE" in self._strategy_warnings
            and tracker.actual_observations
        ):
            return tracker.finalize(
                ResultStatus.PARTIAL,
                reason_code="COMBO_WARM_START_UNAVAILABLE",
                reason_message=(
                    "The combo subwindow begins with a close-priority target "
                    "that requires a pre-window position; ranking is disabled"
                ),
            )
        if self._max_days_truncated and tracker.actual_observations:
            return tracker.finalize(
                ResultStatus.PARTIAL,
                reason_code="MAX_DAYS_TRUNCATED",
                reason_message=(
                    "execution.max_days truncated the requested completed-data "
                    "window; the result cannot be ranked"
                ),
            )
        incomplete_universe = tuple(sorted(
            set(self._preload_missing)
            | set(self._preload_invalid)
            | {symbol for symbol, _ in self._runtime_market_data_gaps}
            | set(self._provenance_incomplete_symbols)
        ))
        if incomplete_universe and tracker.actual_observations:
            return tracker.finalize(
                ResultStatus.PARTIAL,
                reason_code="UNIVERSE_MARKET_DATA_INCOMPLETE",
                reason_message=(
                    "At least one eligible universe member lacked usable "
                    "market data; the result cannot represent the full universe"
                ),
            )
        if (
            self.pool_source is PoolSource.INDUSTRY
            and tracker.expected_observations
            and tracker.actual_observations == tracker.expected_observations
        ):
            return tracker.finalize(
                ResultStatus.PARTIAL,
                reason_code="POINT_IN_TIME_INDUSTRY_UNAVAILABLE",
                reason_message=(
                    "Historical industry membership is unavailable; the "
                    "current snapshot cannot support ranking"
                ),
            )
        return tracker.finalize_completed()

    def _new_outcome_tracker(self, trade_days) -> OutcomeTracker:
        expected_ids = tuple(
            self._canonical_trade_day(trade_date)
            for trade_date in trade_days
        )
        tracker = OutcomeTracker(
            expected_observation_ids=expected_ids,
            coverage_unit="portfolio_trade_day",
        )
        for code, message in sorted(self._strategy_warnings.items()):
            tracker.add_warning(code, message)
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
        if self.freq == 'daily':
            policy = (
                self.daily_auction_liquidity
                if self.daily_auction_liquidity is not None
                else DailyAuctionLiquidityPolicy()
            )
            for key, value in policy.assumptions():
                tracker.add_assumption(key, value)
        if self.max_days is not None:
            tracker.add_assumption("execution.max_days", str(self.max_days))
        if self._max_days_truncated:
            tracker.add_warning(
                "MAX_DAYS_TRUNCATED",
                "execution.max_days stopped the run before the end of the "
                "requested completed-data window",
            )
        incomplete_universe = tuple(sorted(
            set(self._preload_missing)
            | set(self._preload_invalid)
            | set(self._provenance_incomplete_symbols)
        ))
        if incomplete_universe:
            shown = ", ".join(incomplete_universe[:10])
            suffix = "..." if len(incomplete_universe) > 10 else ""
            tracker.add_warning(
                "UNIVERSE_MARKET_DATA_INCOMPLETE",
                f"{len(incomplete_universe)} eligible universe member(s) "
                f"lacked usable market data: {shown}{suffix}",
            )
        if self._window_bound is not None and self._window_bound.truncated:
            tracker.add_warning(
                "WINDOW_TRUNCATED", self._window_bound.warning_message
            )
        self._add_data_contract_compatibility_evidence(tracker)
        self._add_universe_contract_evidence(tracker)
        return tracker

    def _observe_completed_day(self, trade_date: object) -> None:
        """Record only a fully returned day that actually saw a valid bar."""
        if (
            self._outcome_tracker is None
            or self._current_day_low_total_value is None
            or self._stopped
        ):
            return
        canonical = self._canonical_trade_day(trade_date)
        self._outcome_tracker.observe(canonical, canonical)

    def _ensure_cash_replay_seed(self) -> CashReplaySeed:
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

    def _snapshot_cash_audit(self) -> CashAuditBundle:
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

    def _set_failed_contract(
            self, *, preserve_primary_error: bool = False) -> ResultContract:
        """Freeze runtime evidence before propagating the original error."""
        tracker = self._outcome_tracker
        if tracker is None:
            tracker = OutcomeTracker(
                expected_observation_ids=(),
                coverage_unit="portfolio_trade_day",
            )
            for code, message in sorted(self._strategy_warnings.items()):
                tracker.add_warning(code, message)
            try:
                add_trade_calendar_assumptions(tracker, self._data_provider)
            except Exception:
                if not preserve_primary_error:
                    raise
                logger.exception(
                    "Trade-calendar identity could not be committed to the "
                    "failed result; preserving the primary engine error"
                )
            self._add_data_contract_compatibility_evidence(tracker)
            self._add_universe_contract_evidence(tracker)
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
                reason_message="The portfolio backtest engine raised an exception",
            )
        except RuntimeError:
            previous = self.last_result_contract
            if previous is None:
                raise
            contract = ResultContract(
                status=ResultStatus.FAILED,
                reason=ResultReason(
                    "ENGINE_RUNTIME_ERROR",
                    "The portfolio backtest engine raised an exception",
                ),
                warnings=previous.warnings,
                assumptions=previous.assumptions,
                actual_interval=previous.actual_interval,
                data_coverage=previous.data_coverage,
            )
        self.last_result_contract = contract
        return contract

    def run(self, strategy: PortfolioStrategy) -> PortfolioResult:
        """
        运行回测

        Args:
            strategy: 组合策略实例

        Returns:
            PortfolioResult 回测结果
        """
        self._strategy = strategy
        self._stopped = False
        self.last_result_contract = None
        self.last_cash_audit = None
        self._cash_replay_seed = None
        self._outcome_tracker = None
        self._window_bound = None
        self._strategy_warnings = {}
        self._max_days_truncated = False
        strategy_initialized = False
        strategy_finish_attempted = False
        primary_error = None

        try:
            # 初始化
            self._init_engine()
            self._ensure_cash_replay_seed()

            # Resolve the wall-clock portion of the requested scope before
            # touching symbol data.  A wholly future SPECIFIED run has no
            # expected observations; treating its necessarily empty preload
            # as a broken symbol would misclassify INVALID input as FAILED.
            scheduled_trade_days = self._data_provider.get_trade_days_between(
                self.start_date, self.end_date
            )
            wall_bound = bound_cash_trade_days(
                scheduled_trade_days,
                requested_end=self.end_date,
                data_dates=(),
            )
            requested_start = datetime.strptime(
                str(self.start_date).strip().replace("-", "").replace("/", ""),
                "%Y%m%d",
            ).strftime("%Y%m%d")
            entirely_future = (
                requested_start > wall_bound.wall_clock_cutoff
                and not wall_bound.trade_days
            )
            if entirely_future:
                # Keep the strategy lifecycle paired even when the requested
                # window has no completed sessions.  We intentionally skip
                # stock-pool resolution and data preload, but on_init/on_finish
                # remain part of every successfully initialized engine run.
                self._strategy.set_context(self._context)
                self._strategy.on_init(self._context)
                strategy_initialized = True
                self._window_bound = wall_bound
                self._outcome_tracker = self._new_outcome_tracker(())
                _add_adjustment_factor_identity_assumption(
                    self._outcome_tracker, self._data_quality_reports
                )
                self._context._broker.cancel_all_orders()
                strategy_finish_attempted = True
                self._strategy.on_finish(self._context)
                self._context._broker.cancel_all_orders()
                contract = self._finalize_completed_outcome(
                    self._outcome_tracker
                )
                self.last_result_contract = contract
                self._snapshot_cash_audit()
                return self._create_empty_result(
                    "日期范围内无已完成交易日", result_contract=contract
                )

            # 获取股票池并预加载数据
            # Before the first trading date, PortfolioContext resolves this as
            # the listing-interval union needed for preload compatibility.
            try:
                pool = self._context.get_stock_pool()
            except PointInTimeUniverseError as exc:
                self._outcome_tracker = self._new_outcome_tracker(())
                self._outcome_tracker.add_warning(
                    "POINT_IN_TIME_UNIVERSE_UNAVAILABLE", str(exc)
                )
                raise
            if not pool:
                logger.warning("股票池为空")
                empty_scope = self._new_outcome_tracker(())
                _add_adjustment_factor_identity_assumption(
                    empty_scope, self._data_quality_reports
                )
                contract = empty_scope.finalize(
                    ResultStatus.INVALID,
                    reason_code="EMPTY_STOCK_POOL",
                    reason_message="The portfolio stock pool is empty",
                )
                self.last_result_contract = contract
                self._snapshot_cash_audit()
                return self._create_empty_result(
                    "股票池为空", result_contract=contract
                )

            for symbol in pool:
                self.rulebook.require_supported(
                    symbol, engine=ExecutionEngine.CASH)

            self._observe_market_symbols(pool)

            # 新股上市初期涨跌停豁免日历（只对窗口内上市的标的有条目）
            try:
                from .listing_rules import compute_limit_exempt_dates
                exempt = compute_limit_exempt_dates(
                    self._data_provider, pool, backtest_start=self.start_date)
                if exempt:
                    self._context._broker.set_limit_check_exempt(exempt)
            except Exception as e:
                logger.debug(f"limit exempt computation failed: {e}")

            logger.info(f"股票池大小: {len(pool)}")
            self._preload_data(pool)

            # 策略初始化
            self._strategy.set_context(self._context)
            self._strategy.on_init(self._context)
            strategy_initialized = True

            # 获取交易日列表
            trade_days = scheduled_trade_days
            self._window_bound = bound_cash_trade_days(
                trade_days,
                requested_end=self.end_date,
                data_dates=self._loaded_daily_data_dates(),
            )
            trade_days = list(self._window_bound.trade_days)

            if not trade_days:
                logger.warning(f"日期范围内无交易日: {self.start_date} ~ {self.end_date}")
                self._outcome_tracker = self._new_outcome_tracker(())
                _add_adjustment_factor_identity_assumption(
                    self._outcome_tracker, self._data_quality_reports
                )
                self._context._broker.cancel_all_orders()
                strategy_finish_attempted = True
                self._strategy.on_finish(self._context)
                self._context._broker.cancel_all_orders()
                contract = self._finalize_completed_outcome(
                    self._outcome_tracker
                )
                self.last_result_contract = contract
                self._snapshot_cash_audit()
                return self._create_empty_result(
                    "日期范围内无交易日", result_contract=contract
                )

            # 限制运行天数 (用于语法检查)
            expected_trade_days = tuple(trade_days)
            if self.max_days and len(trade_days) > self.max_days:
                trade_days = trade_days[:self.max_days]
                self._max_days_truncated = True
                logger.info(f"限制运行天数: {self.max_days}")
            self._outcome_tracker = self._new_outcome_tracker(
                expected_trade_days
            )

            # 逐日回测
            total_days = len(trade_days)
            for i, trade_date in enumerate(trade_days):
                # 检查停止标志
                if self.stop_check and self.stop_check():
                    self._stopped = True
                    logger.info("回测已停止")
                    break

                next_trade_date = (
                    trade_days[i + 1] if i + 1 < len(trade_days) else None
                )
                self._run_day(trade_date, next_trade_date=next_trade_date)
                self._observe_completed_day(trade_date)
                if self._stopped:
                    break

                # 每天让出一次 GIL，让 UI 线程有机会执行（仅 GUI 场景）
                self._yield_gil()

                # 进度回调 (按 update_interval 间隔触发，避免频繁更新导致UI卡顿)
                if self.progress_callback:
                    day_num = i + 1
                    is_last_day = (day_num == total_days)
                    should_update_progress = (
                        self.update_interval <= 0 or  # 间隔<=0表示每天更新
                        day_num % self.update_interval == 0 or
                        is_last_day
                    )
                    if should_update_progress:
                        self.progress_callback(
                            i + 1, total_days,
                            f"回测中: {trade_date} ({i + 1}/{total_days})"
                        )

                # 每日更新回调 (用于实时刷新图表、交易、持仓)
                # 按 update_interval 间隔触发，避免频繁更新导致UI卡顿
                if self.daily_callback:
                    day_num = i + 1
                    is_last_day = (day_num == total_days)
                    should_update = (
                        self.update_interval > 0 and
                        (day_num % self.update_interval == 0 or is_last_day)
                    )
                    if should_update:
                        # 发送最近数据，避免一次性大量数据导致卡顿
                        MAX_SEND = 100
                        total_days_so_far = len(self._daily_values)
                        recent_values = self._daily_values[-MAX_SEND:] if total_days_so_far > MAX_SEND else self._daily_values

                        # 实时发送交易记录和持仓（最近50条）
                        recent_trades = self._get_current_trades(50)
                        recent_positions = self._position_history[-50:]

                        self.daily_callback({
                            'daily_values': list(recent_values),
                            'total_days': total_days_so_far,
                            'trades': recent_trades,
                            'positions': recent_positions,
                        })

            # 窗口最后一个 on_day 产生的未来单没有撮合机会；结束前释放冻结。
            self._context._broker.cancel_all_orders()

            # 策略结束
            self._context._broker.cancel_all_orders()
            strategy_finish_attempted = True
            self._strategy.on_finish(self._context)
            self._context._broker.cancel_all_orders()
            self._snapshot_cash_audit()

            # 生成结果
            _add_adjustment_factor_identity_assumption(
                self._outcome_tracker, self._data_quality_reports
            )
            if self._stopped:
                contract = self._outcome_tracker.finalize(
                    ResultStatus.CANCELED,
                    reason_code="STOP_REQUESTED",
                    reason_message="The user requested cancellation",
                )
            else:
                contract = self._finalize_completed_outcome(
                    self._outcome_tracker
                )
            self.last_result_contract = contract
            if self._daily_values:
                return self._create_result(result_contract=contract)
            return self._create_empty_result(result_contract=contract)

        except BaseException as e:
            primary_error = e
            logger.exception(f"回测出错: {e}")
            self._set_failed_contract(preserve_primary_error=True)
            raise
        finally:
            # Every exit path must release reservations.  A secondary cleanup
            # error cannot mask a primary backtest error that must be
            # preserved and re-raised by the strict result contract.
            finalizer_error = None

            def finalize(name, callback):
                nonlocal finalizer_error
                try:
                    callback()
                except BaseException as exc:
                    if primary_error is not None or finalizer_error is not None:
                        logger.exception(
                            "Portfolio backtest %s failed during finalization",
                            name,
                        )
                    else:
                        finalizer_error = exc

            broker = (
                self._context._broker
                if self._context is not None else None
            )
            if broker is not None:
                finalize('order cleanup', broker.cancel_all_orders)
            if strategy_initialized and not strategy_finish_attempted:
                strategy_finish_attempted = True
                finalize(
                    'strategy on_finish',
                    lambda: self._strategy.on_finish(self._context),
                )
            if broker is not None:
                finalize('post-finish order cleanup', broker.cancel_all_orders)
                finalize('cash audit snapshot', self._snapshot_cash_audit)

            if primary_error is None and finalizer_error is not None:
                self._set_failed_contract(preserve_primary_error=True)
                raise finalizer_error

    def _init_engine(self) -> None:
        """初始化引擎"""
        # 创建独立的数据提供者（已移除单例，每个引擎实例独立）
        self._data_provider = DataProvider(
            price_mode=self.strategy_price_mode,
            execution_price_mode=self.execution_price_mode,
            data_root=self.data_root,
        )

        account = Account(
            initial_cash=self.initial_cash,
            commission_rate=self.commission_rate,
            stamp_duty_rate=self.stamp_duty,
            min_commission=self.min_commission,
        )

        broker = Broker(
            account,
            slippage=self.slippage,
            lot_size=self.lot_size,
            open_buy_resize_mode=self.open_buy_resize_mode,
            liquidity_cap_ratio=self.liquidity_cap_ratio,
            limit_pct_overrides=self.limit_pct_overrides,
            open_buy_fill_mode=self.open_buy_fill_mode,
            open_buy_sizing=self.open_buy_sizing,
            bar_liquidity_model=self.bar_liquidity_model,
            t0_overrides=self.t0_overrides,
            transfer_fee_rate=self.transfer_fee_rate,
            rulebook=self.rulebook,
        )

        self._context = PortfolioContext(
            broker=broker,
            data_provider=self._data_provider,
            initial_cash=self.initial_cash,
            pool_source=self.pool_source,
            pool_symbols=self.pool_symbols,
            pool_industry=self.pool_industry,
            pool_start_date=self.start_date,
            pool_end_date=self.end_date,
        )
        self._context._set_market_data_observer(
            self._observe_market_symbols
        )
        self._context._set_result_warning_callback(
            self._record_strategy_warning
        )

        # 关键：设置 DataProvider 的上下文，使其能获取当前回测日期
        self._data_provider.set_context(self._context)

        # 清空记录
        self._daily_values = []
        self._position_history = []
        self._daily_data_cache = {}
        self._date_index_cache = {}
        self._daily_data_raw_cache = {}
        self._date_index_raw_cache = {}
        self._data_quality_reports = []
        self._preload_missing = []
        self._preload_invalid = []
        self._runtime_market_data_gaps = set()
        self._provenance_incomplete_symbols = set()
        self._realized_market_symbols = set()
        self._data_contract_compatibility_symbols = set()
        self._daily_amount_yuan_symbols = set()
        self._previous_daily_amount_cache = {}
        self._reset_intraday_low_tracker()

    def _align_daily_frames(self, symbol, strategy_data, execution_data,
                            strategy_space, execution_space):
        """Validate a cached daily pair and normalize both amounts to yuan."""
        return validate_and_align_pair(
            strategy_data,
            execution_data,
            symbol=symbol,
            strategy_price_space=strategy_space,
            execution_price_space=execution_space,
            strategy_amount_unit=AmountUnit.THOUSAND_YUAN,
            execution_amount_unit=AmountUnit.THOUSAND_YUAN,
            frequency=Frequency.DAILY,
            pre_close_exempt_dates=(
                self._pre_close_exempt_dates(symbol)
            ),
        )

    def _pre_close_exempt_dates(self, symbol: str):
        """Return this run's explicit per-symbol IPO/limit exemption dates."""
        broker = self._context._broker if self._context is not None else None
        mapping = getattr(broker, '_limit_check_exempt', {}) if broker else {}
        return mapping.get(symbol, ())

    def _build_date_index(self, df: pd.DataFrame) -> Dict[str, int]:
        """
        构建日期到行号的索引

        Args:
            df: DataFrame with date index

        Returns:
            {date_str: row_index} 映射
        """
        index_map = {}
        if isinstance(df.index, pd.DatetimeIndex):
            for i, d in enumerate(df.index):
                index_map[d.strftime('%Y%m%d')] = i
        else:
            for i, d in enumerate(df.index):
                # 处理各种日期格式
                date_str = str(d).replace('-', '')[:8]
                index_map[date_str] = i
        return index_map

    def _preload_data(self, symbols: List[str]) -> None:
        """Preload daily data for strategy and execution."""
        logger.info(f"Preloading {len(symbols)} symbols...")
        pair_loader = getattr(self._data_provider, 'get_aligned_pair', None)
        if not callable(pair_loader):
            self._data_contract_compatibility_symbols.update(symbols)
            logger.warning(
                "Provider has no get_aligned_pair API; daily preload is using "
                "the legacy compatibility path without a DC-1 provider report"
            )

        def load_single_stock(symbol):
            try:
                if callable(pair_loader):
                    aligned = pair_loader(
                        symbol,
                        frequency='daily',
                        start=self.start_date,
                        end=self.end_date,
                        pre_close_exempt_dates=(
                            self._pre_close_exempt_dates(symbol)
                        ),
                    )
                    df = aligned.strategy
                    raw_df = aligned.execution
                    return (
                        symbol,
                        df,
                        self._build_date_index(df),
                        raw_df,
                        self._build_date_index(raw_df),
                        True,
                        True,
                        aligned.report,
                    )

                # Compatibility-only path for legacy providers/test doubles
                # that predate the paired adapter.  Production DataProvider
                # always takes the strict branch above.
                df = self._data_provider.get_daily(
                    symbol, start=self.start_date, end=self.end_date, price_mode='strategy'
                )
                raw_df = self._data_provider.get_daily(
                    symbol, start=self.start_date, end=self.end_date, price_mode='execution'
                )
                strat_ok = df is not None and not df.empty
                raw_ok = raw_df is not None and not raw_df.empty
                # 单轨镜像 fallback：只有一腿数据时两腿同源
                # （只有策略腿→撮合用策略数据；只有撮合腿→策略看撮合数据。
                #  历史陷阱：只放 daily/ 曾静默 0 成交 exit 0）
                if strat_ok and not raw_ok:
                    raw_df = df
                elif raw_ok and not strat_ok:
                    df = raw_df
                report = None
                if df is not None and not df.empty and raw_df is not None and not raw_df.empty:
                    strategy_space = self._data_provider.price_mode
                    execution_space = self._data_provider.execution_price_mode
                    if strat_ok and not raw_ok:
                        execution_space = strategy_space
                    elif raw_ok and not strat_ok:
                        strategy_space = execution_space
                    aligned = self._align_daily_frames(
                        symbol, df, raw_df, strategy_space, execution_space)
                    df = aligned.strategy
                    raw_df = aligned.execution
                    report = aligned.report
                if df is not None and not df.empty:
                    date_index = self._build_date_index(df)
                else:
                    date_index = None
                if raw_df is not None and not raw_df.empty:
                    raw_index = self._build_date_index(raw_df)
                else:
                    raw_index = None
                return (symbol, df, date_index, raw_df, raw_index,
                        strat_ok, raw_ok, report)
            except DataContractError as exc:
                if not _is_empty_pair_contract_error(exc):
                    raise
                logger.debug(
                    "No market data for %s in %s~%s",
                    symbol, self.start_date, self.end_date,
                )
            except Exception as exc:
                logger.debug("Preload failed for %s: %s", symbol, exc)
            return symbol, None, None, None, None, False, False, None

        max_workers = min(32, len(symbols))
        if max_workers < 1:
            max_workers = 1

        self._preload_missing: List[str] = []

        same_source = set()
        only_strategy = set()
        only_raw = set()
        dual_track = set()

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_symbol = {executor.submit(load_single_stock, sym): sym for sym in symbols}
            loaded_count = 0
            for future in as_completed(future_to_symbol):
                (symbol, df, date_index, raw_df, raw_index,
                 strat_ok, raw_ok, report) = future.result()
                if df is not None and not df.empty:
                    self._daily_data_cache[symbol] = df
                    self._date_index_cache[symbol] = date_index
                    loaded_count += 1
                if raw_df is not None and not raw_df.empty:
                    self._daily_data_raw_cache[symbol] = raw_df
                    self._date_index_raw_cache[symbol] = raw_index
                if report is not None:
                    self._data_quality_reports.append(report)
                    self._daily_amount_yuan_symbols.add(symbol)
                if strat_ok and raw_ok:
                    dual_track.add(symbol)
                elif strat_ok != raw_ok:
                    same_source.add(symbol)
                    (only_strategy if strat_ok else only_raw).add(symbol)
            logger.info(f"Loaded {loaded_count} symbols")

        # ==================== 单轨/双轨语义处理 ====================
        if same_source:
            # 同源标的：禁用挂单价换算；仅 hfq 镜像腿（only_strategy）禁调股
            # （raw 单轨 + 因子的除权调股=真实账户行为，保留）
            self._context.set_same_source_symbols(same_source, skip_adjust=only_strategy)
            shown = ', '.join(sorted(same_source)[:5]) + ('...' if len(same_source) > 5 else '')
            logger.warning(
                f"{len(same_source)} 个标的仅有单轨数据（{shown}），策略/撮合两腿同源运行："
                f"价格换算与除权调股已对其禁用。单轨口径的牺牲见文档《数据契约》。"
            )
            # 全池同侧单轨：全局对齐 provider 价格模式（覆盖分钟等非缓存读取路径）
            loaded = set(self._daily_data_cache) | set(self._daily_data_raw_cache)
            if loaded and same_source >= loaded:
                if only_strategy and not only_raw:
                    self._data_provider.set_price_modes(
                        execution=self._data_provider.price_mode)
                elif only_raw and not only_strategy:
                    self._data_provider.set_price_modes(
                        strategy=self._data_provider.execution_price_mode)

        # 双轨但缺复权因子的强警告：两腿价格实质不同时，缺 adj_factor 意味着
        # 挂单换算按 1.0（限价系统性错位）且除权日不调股——几乎必然是配置错误
        adj_missing_dual = []
        for symbol in dual_track:
            df = self._daily_data_cache.get(symbol)
            raw_df = self._daily_data_raw_cache.get(symbol)
            if df is None or raw_df is None or df.empty or raw_df.empty:
                continue
            try:
                s_close = float(df['close'].iloc[-1])
                r_close = float(raw_df['close'].iloc[-1])
                if r_close > 0 and abs(s_close / r_close - 1.0) > 0.01:
                    last_date = str(raw_df.index[-1])[:8] if raw_df.index.name else None
                    ratio = self._data_provider.get_adj_ratio(
                        symbol, last_date or self.end_date)
                    if abs(ratio - 1.0) < 1e-9:
                        adj_missing_dual.append(symbol)
            except Exception:
                continue
        if adj_missing_dual:
            shown = ', '.join(adj_missing_dual[:5]) + ('...' if len(adj_missing_dual) > 5 else '')
            logger.warning(
                f"{len(adj_missing_dual)} 个标的策略腿与撮合腿价格差异>1% 但缺复权因子"
                f"（{shown}）：限价/止损单换算将系统性错位、除权日不调整持仓。"
                f"请提供 adj_factor，或单轨运行（只放一个数据目录）。"
            )

        # 数据缺失守卫：修复"静默成功"失真链（拼错代码/缺文件 → 0%收益0交易退出码0）。
        # 指定股票池：任何缺数据标的默认直接报错；全市场池：缺数据是常态
        # （未上市/已退市），只汇总警告。可用 on_missing_data 覆盖。
        missing = [s for s in symbols
                   if s not in self._daily_data_cache
                   and s not in self._daily_data_raw_cache]

        # 列级守卫：行数守卫会被"有行但缺列"绕过——缺 amount 列时流动性帽=0、
        # 全部买单静默拒单，与缺数据同形态（历史P1）。撮合必需列缺失按缺数据处理。
        REQUIRED_COLS = {'open', 'high', 'low', 'close', 'amount'}
        bad_cols: Dict[str, set] = {}
        for s, df_raw in self._daily_data_raw_cache.items():
            lack = REQUIRED_COLS - set(df_raw.columns)
            if lack:
                bad_cols[s] = lack
        if bad_cols:
            shown_cols = '; '.join(
                f"{s}缺{sorted(c)}" for s, c in list(bad_cols.items())[:5])
            all_lacking = set().union(*bad_cols.values())
            col_msg = (
                f"{len(bad_cols)} 个标的行情缺撮合必需列（{shown_cols}"
                f"{'...' if len(bad_cols) > 5 else ''}）。缺列后果："
                f"amount→流动性帽=0全部买单静默拒单；open/close→无法撮合。"
                f"本次实际缺失: {sorted(all_lacking)}"
            )
            mode = self.on_missing_data
            if self.freq in MINUTE_FREQS:
                mode = 'raise'
            if mode is None:
                mode = 'raise' if self.pool_source == PoolSource.SPECIFIED else 'warn'
            if mode == 'raise':
                raise ValueError(col_msg)
            if mode == 'warn':
                logger.warning(col_msg)
        self._preload_invalid = sorted(bad_cols)

        self._preload_missing = sorted(missing)
        if missing:
            mode = self.on_missing_data
            if self.freq in MINUTE_FREQS:
                mode = 'raise'
            if mode is None:
                mode = 'raise' if self.pool_source == PoolSource.SPECIFIED else 'warn'
            shown = ', '.join(missing[:10]) + ('...' if len(missing) > 10 else '')
            msg = (
                f"{len(missing)}/{len(symbols)} 个标的在 {self.start_date}~{self.end_date} "
                f"无任何行情数据: {shown}\n"
                f"请检查代码拼写与数据目录（PARQUET_ROOT 见 diepi/backtest/config.py）。"
            )
            if self.freq in MINUTE_FREQS:
                msg += (
                    " 分钟回测仍正式要求同标的、同价格轨的伴随日线，"
                    "用于交易窗口边界、盘前日线历史和价格带/公司行为语义；"
                    "仅有 minute 分片不构成可运行的 v1 输入，且 "
                    "on_missing_data 不能绕过该要求。"
                )
            else:
                msg += " 可传 on_missing_data='warn'/'ignore' 跳过此检查。"
            if mode == 'raise':
                raise ValueError(msg)
            if mode == 'warn':
                logger.warning(msg)

    def _yield_gil(self) -> None:
        """让出 GIL 使 GUI 线程可刷新；纯 CLI/API 场景跳过（见 __init__ 注释）"""
        if self._needs_gil_yield:
            time.sleep(0.001)

    def _run_day(self, trade_date: str,
                 next_trade_date: str = None) -> None:
        self._reset_intraday_low_tracker()
        """
        运行单日回测

        Args:
            trade_date: 交易日期 (YYYYMMDD)
        """
        # 设置日期时间 (09:00)
        dt_str = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} 09:00:00"
        self._context.set_datetime(dt_str)
        self._context._on_day_start()

        # 盘前时段 - 选股
        self._context.set_market_phase('before_open')
        with self._context._broker.order_creation_scope(
                'before_open', self._context.current_time,
                eligible_from=pd.to_datetime(trade_date),
                expire_date=trade_date):
            selected_symbols = self._strategy.on_before_market_open(self._context)

        # None = 使用全部股票池（docstring 契约；基类默认返回 None——
        # 历史P0：None 曾被折叠成空列表，只写 on_day 的最小策略整场静默零成交）
        if selected_symbols is None:
            selected_symbols = self._context.get_stock_pool()
            if len(selected_symbols) > 500 and not getattr(self, '_warned_full_pool', False):
                self._warned_full_pool = True
                logger.warning(
                    f"on_before_market_open 返回 None → 使用全部股票池"
                    f"（{len(selected_symbols)} 只），全市场回测耗时/内存显著增加；"
                    f"如非本意请在该回调中返回具体标的列表"
                )

        # 自动添加所有持仓股票到活跃列表（防止策略遗漏导致持仓无法卖出）
        holding_symbols = [
            s for s, p in self._context.get_positions().items()
            if getattr(p, 'shares', 0) > 0
        ]
        pending_symbols = [
            order.symbol
            for order in self._context._broker.get_eligible_open_orders(
                pd.to_datetime(trade_date))
        ]
        # sorted 而非 list(set)：字符串集合迭代序随 PYTHONHASHSEED 变化，
        # 会经 bars 构造序→策略遍历序→撮合序传导到现金竞争下的成交分配，
        # 历史P0：同一命令跨进程收益差 4pp 不可复现
        all_active = sorted(set(selected_symbols)
                            | set(holding_symbols)
                            | set(pending_symbols))
        for symbol in all_active:
            self.rulebook.require_supported(
                symbol, engine=ExecutionEngine.CASH
            )
        # Freeze the exact bar source before the engine reads it.  This also
        # closes provenance for symbols submitted directly as orders even when
        # the strategy did not return them from on_before_market_open().
        self._observe_market_symbols(
            all_active,
            frequency=self.freq,
            start_date=trade_date,
            end_date=trade_date,
        )
        self._context.set_active_symbols(all_active)
        self._yield_gil()  # GIL 释放：选股后

        # 获取当日活跃股票
        active_symbols = self._context.active_symbols
        if not active_symbols:
            # A deliberately cash-only portfolio day is still one complete
            # portfolio_trade_day observation.  No market bar is required to
            # value it, but the pessimistic NAV sampler must receive an
            # explicit cash-only high/low so coverage is not mistaken for
            # missing market data.
            self._capture_intraday_range({}, {})

        # 交易时段
        self._context.set_market_phase('trading')
        if self.freq == 'daily':
            self._run_daily_bars(
                trade_date, active_symbols,
                next_trade_date=next_trade_date)
        elif self.freq in MINUTE_FREQS:
            self._run_minute_bars(
                trade_date, active_symbols,
                next_trade_date=next_trade_date)
        else:
            # 未知频率，尝试按分钟处理
            logger.warning(f"未知频率 {self.freq}，尝试按分钟处理")
            self._run_minute_bars(
                trade_date, active_symbols,
                next_trade_date=next_trade_date)
        self._yield_gil()  # GIL 释放：交易后

        # 最终撮合窗口已经结束。T 日 DAY 余单必须在盘后回调可见前终结；
        # on_day 产生、expire_date 属于未来交易日的单会由 Broker 保留。
        self._context._broker.cancel_day_end_orders(
            sim_time=self._context.current_time)

        # 盘后时段 (15:00)
        self._context.set_market_phase('after_close')
        dt_str = f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]} 15:00:00"
        self._context.set_datetime(dt_str)
        self._strategy.on_after_market_close(self._context)
        self._yield_gil()  # GIL 释放：盘后回调后

        # 记录每日净值
        self._record_daily_value(trade_date)

        # 记录持仓
        self._record_positions(trade_date)

        # 日结束处理
        self._context._on_day_end()

    def _get_previous_daily_amount_yuan(
            self, symbol: str, trade_date: str) -> Optional[float]:
        """Return one symbol's exact prior-session execution turnover."""
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
        cached = self._previous_daily_amount_cache.get(symbol)
        if cached is not None and cached[0] == prior_date:
            return cached[1]

        frame = self._data_provider.get_daily(
            symbol,
            start=prior_date,
            end=prior_date,
            price_mode='execution',
        )
        amount = _daily_amount_for_date(frame, prior_date, scale=1000.0)
        self._previous_daily_amount_cache[symbol] = (prior_date, amount)
        return amount

    def _run_daily_bars(self, trade_date: str, symbols: List[str],
                        next_trade_date: str = None) -> None:
        bars = {}
        broker_bars = {}
        previous_day_amounts = {}
        trade_datetime = pd.to_datetime(trade_date)
        pair_loader = getattr(self._data_provider, 'get_aligned_pair', None)

        def get_row(df, date_index):
            if df is None or df.empty:
                return None
            try:
                if date_index and trade_date in date_index:
                    return df.iloc[date_index[trade_date]]
                if isinstance(df.index, pd.DatetimeIndex):
                    date_mask = df.index.strftime('%Y%m%d') == trade_date
                else:
                    date_mask = df.index.astype(str).str.replace('-', '') == trade_date
                df_day = df[date_mask]
                if df_day.empty:
                    return None
                return df_day.iloc[0]
            except Exception:
                return None

        for idx, symbol in enumerate(symbols):
            if idx % 500 == 0:
                self._yield_gil()

            previous_day_amounts[symbol] = (
                self._get_previous_daily_amount_yuan(symbol, trade_date)
            )
            # A missing current-day bar makes the immediately preceding
            # turnover unavailable on the following day; never retain a
            # stale earlier observation.
            self._previous_daily_amount_cache[symbol] = (trade_date, None)

            df = self._daily_data_cache.get(symbol)
            date_index = self._date_index_cache.get(symbol)
            raw_df = self._daily_data_raw_cache.get(symbol)
            raw_index = self._date_index_raw_cache.get(symbol)

            if (callable(pair_loader)
                    and (df is None or df.empty
                         or raw_df is None or raw_df.empty)):
                try:
                    aligned = pair_loader(
                        symbol,
                        frequency='daily',
                        start=self.start_date,
                        end=self.end_date,
                        pre_close_exempt_dates=(
                            self._pre_close_exempt_dates(symbol)
                        ),
                    )
                except DataContractError as exc:
                    if not _is_empty_pair_contract_error(exc):
                        raise
                else:
                    df = aligned.strategy
                    raw_df = aligned.execution
                    date_index = self._build_date_index(df)
                    raw_index = self._build_date_index(raw_df)
                    self._daily_data_cache[symbol] = df
                    self._daily_data_raw_cache[symbol] = raw_df
                    self._date_index_cache[symbol] = date_index
                    self._date_index_raw_cache[symbol] = raw_index
                    self._data_quality_reports.append(aligned.report)
                    self._daily_amount_yuan_symbols.add(symbol)

            if raw_df is None or raw_df.empty:
                try:
                    raw_df = self._data_provider.get_daily(
                        symbol, start=self.start_date, end=self.end_date, price_mode='execution'
                    )
                    if not raw_df.empty:
                        self._daily_data_raw_cache[symbol] = raw_df
                        raw_index = self._build_date_index(raw_df)
                        self._date_index_raw_cache[symbol] = raw_index
                except Exception as e:
                    logger.debug(f"Load raw daily failed {symbol}: {e}")

            if df is None or df.empty:
                try:
                    df = self._data_provider.get_daily(
                        symbol, start=self.start_date, end=self.end_date, price_mode='strategy'
                    )
                    if df.empty and raw_df is not None and not raw_df.empty:
                        # raw 单轨镜像：策略腿共用撮合数据 → 登记同源短路换算
                        # （raw+因子保留除权调股，不入 skip_adjust）
                        df = raw_df
                        self._context.add_same_source_symbol(symbol)
                        logger.warning(
                            f"单轨(懒加载): {symbol} 仅有撮合腿数据，策略腿镜像共用")
                    if df is not None and not df.empty:
                        self._daily_data_cache[symbol] = df
                        date_index = self._build_date_index(df)
                        self._date_index_cache[symbol] = date_index
                except Exception as e:
                    logger.debug(f"Load strategy daily failed {symbol}: {e}")

            # hfq 单轨镜像（审查确认P1：此前只有 raw→策略单向镜像，仅有
            # daily/ 的池外标的整段拿不到撮合 bar，订单被 No market data
            # 静默拒掉 exit 0——恰是预加载路径已修的历史陷阱在懒加载复活）
            if ((raw_df is None or raw_df.empty)
                    and df is not None and not df.empty):
                raw_df = df
                raw_index = date_index
                self._daily_data_raw_cache[symbol] = raw_df
                self._date_index_raw_cache[symbol] = raw_index
                # hfq 连续序列自身无除权跳变，须跳过除权调股（双重复权防护）
                self._context.add_same_source_symbol(symbol, skip_adjust=True)
                logger.warning(
                    f"单轨(懒加载): {symbol} 仅有策略腿数据，撮合腿镜像共用"
                    f"（hfq 连续序列，除权调股已禁用）")

            if (symbol not in self._daily_amount_yuan_symbols
                    and df is not None and not df.empty
                    and raw_df is not None and not raw_df.empty):
                strategy_space = self._data_provider.price_mode
                execution_space = self._data_provider.execution_price_mode
                if self._context._is_same_source(symbol):
                    if self._context._skip_corporate_adjust(symbol):
                        execution_space = strategy_space
                    else:
                        strategy_space = execution_space
                aligned = self._align_daily_frames(
                    symbol, df, raw_df, strategy_space, execution_space)
                df = aligned.strategy
                raw_df = aligned.execution
                date_index = self._build_date_index(df)
                raw_index = self._build_date_index(raw_df)
                self._daily_data_cache[symbol] = df
                self._daily_data_raw_cache[symbol] = raw_df
                self._date_index_cache[symbol] = date_index
                self._date_index_raw_cache[symbol] = raw_index
                self._data_quality_reports.append(aligned.report)
                self._daily_amount_yuan_symbols.add(symbol)

            raw_row = get_row(raw_df, raw_index)
            if raw_row is None:
                continue
            # NaN bar 守卫：OHLC 含 NaN/非正值的残行（停牌残留/数据商脏行）
            # 按缺 bar 处理。历史P1：NaN bar 上的市价单会冻结 NaN 金额、
            # 永久污染账户现金为 NaN，最终"总收益 nan%"且 exit 0
            if not _is_valid_ohlc_row(raw_row):
                logger.warning(f"跳过无效 bar: {symbol} {trade_date} (OHLC 含 NaN/非正值)")
                continue
            strat_row = get_row(df, date_index)
            if strat_row is None:
                raise ValueError(
                    f"Daily strategy data required for {symbol} on "
                    f"{trade_date}; missing price lane cannot be substituted"
                )
            if not _is_valid_ohlc_row(strat_row):
                raise ValueError(
                    f"Daily strategy data invalid for {symbol} on {trade_date}"
                )

            val = raw_row.get('pre_close', 0)
            pre_close = float(val) if pd.notna(val) else 0.0
            adjustment_factor_ratio = _execution_adjustment_factor_change(
                self._data_provider, symbol, trade_date)
            amount_yuan = _amount_yuan(
                raw_row.get('amount', 0),
                scale=(1.0 if symbol in self._daily_amount_yuan_symbols
                       else 1000.0),
            )

            bar = BarData(
                symbol=symbol,
                trade_time=trade_datetime,
                open=float(strat_row['open']),
                high=float(strat_row['high']),
                low=float(strat_row['low']),
                close=float(strat_row['close']),
                vol=float(strat_row.get('vol', 0)),
                amount=amount_yuan,
            )
            bars[symbol] = bar

            broker_bars[symbol] = BrokerBarData(
                symbol=symbol,
                trade_time=trade_date,
                open=float(raw_row['open']),
                high=float(raw_row['high']),
                low=float(raw_row['low']),
                close=float(raw_row['close']),
                vol=float(raw_row.get('vol', 0)),
                amount=amount_yuan,
                pre_close=pre_close,
                adjustment_factor_ratio=adjustment_factor_ratio,
            )
            self._context._broker.validate_execution_bar_price_band(
                broker_bars[symbol])

        if not bars:
            return

        open_broker_bars = {
            symbol: BrokerBarData(
                symbol=symbol,
                trade_time=trade_date,
                open=float(day_bar.open),
                high=float(day_bar.open),
                low=float(day_bar.open),
                close=float(day_bar.open),
                vol=0.0,
                amount=0.0,
                pre_close=day_bar.pre_close,
                adjustment_factor_ratio=day_bar.adjustment_factor_ratio,
                liquidity_cap_amount=_resolve_daily_auction_cap(
                    self.daily_auction_liquidity,
                    AuctionWindow.OPEN,
                    previous_day_amounts.get(symbol),
                ),
                require_explicit_liquidity_cap=True,
                liquidity_window=AuctionWindow.OPEN.value,
            )
            for symbol, day_bar in broker_bars.items()
        }
        close_broker_bars = {
            symbol: BrokerBarData(
                symbol=symbol,
                trade_time=trade_date,
                open=float(day_bar.close),
                high=float(day_bar.close),
                low=float(day_bar.close),
                close=float(day_bar.close),
                vol=0.0,
                amount=0.0,
                pre_close=day_bar.pre_close,
                adjustment_factor_ratio=day_bar.adjustment_factor_ratio,
                liquidity_cap_amount=_resolve_daily_auction_cap(
                    self.daily_auction_liquidity,
                    AuctionWindow.CLOSE,
                    previous_day_amounts.get(symbol),
                ),
                require_explicit_liquidity_cap=True,
                liquidity_window=AuctionWindow.CLOSE.value,
            )
            for symbol, day_bar in broker_bars.items()
        }

        self._context._set_current_bars({s: BarData(
            symbol=s,
            trade_time=bars[s].trade_time,
            open=open_broker_bars[s].open,
            high=open_broker_bars[s].open,
            low=open_broker_bars[s].open,
            close=open_broker_bars[s].open,
            vol=0.0,
            amount=0.0,
        ) for s in open_broker_bars})

        self._context._broker.reset_bar_used_amount()

        open_time = pd.to_datetime(trade_date) + pd.Timedelta(hours=9, minutes=30)
        close_time = pd.to_datetime(trade_date) + pd.Timedelta(hours=15)
        self._context._execute_open_orders(
            open_broker_bars, match_at=open_time)
        open_prices = {s: b.open for s, b in open_broker_bars.items()}
        self._context._update_prices(
            open_prices, sim_time=open_time, phase='opening_mark')
        self._capture_intraday_range(open_prices, open_prices)

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
                    PortfolioOpenBarData(
                        bars={
                            symbol: OpenBarData(
                                symbol=symbol,
                                trade_time=open_time,
                                open=float(day_bar.open),
                            )
                            for symbol, day_bar in open_broker_bars.items()
                        },
                        trade_date=trade_date,
                    ),
                )
        # Read-only pessimistic audit path: open was sampled above; record the
        # full-bar high before low without changing matching order.
        self._capture_intraday_range(
            {s: b.high for s, b in broker_bars.items()},
            {s: b.low for s, b in broker_bars.items()},
        )

        # 先完成 T 的全部撮合窗口，再把完整 T bar 交给后验 on_day。
        self._context._broker.reset_bar_used_amount()
        for symbol, broker_bar in broker_bars.items():
            self._context._execute_market_orders(
                broker_bar, match_at=close_time)

        self._context._broker.reset_bar_used_amount()
        self._context._execute_close_orders(
            close_broker_bars, match_at=close_time)
        self._context._broker.cancel_day_end_orders(sim_time=close_time)

        prices = {s: b.close for s, b in broker_bars.items()}
        self._context._update_prices(
            prices, sim_time=close_time, phase='closing_mark')
        self._context._set_current_bars({s: BarData(
            symbol=s,
            trade_time=bars[s].trade_time,
            open=broker_bars[s].open,
            high=broker_bars[s].high,
            low=broker_bars[s].low,
            close=broker_bars[s].close,
            vol=broker_bars[s].vol,
            amount=broker_bars[s].amount,
        ) for s in broker_bars})

        self._context.set_datetime(close_time)
        portfolio_bars = PortfolioBarData(bars=bars, trade_date=trade_date)
        with self._context._broker.order_creation_scope(
                'on_day', close_time,
                eligible_from=eligible_time,
                expire_date=str(eligible_date).replace('-', '')[:8]):
            self._strategy.on_day(self._context, portfolio_bars)
        for symbol, day_bar in broker_bars.items():
            self._previous_daily_amount_cache[symbol] = (
                trade_date,
                day_bar.amount if day_bar.amount > 0 else None,
            )

    def _run_minute_bars(self, trade_date: str, symbols: List[str],
                         next_trade_date: str = None) -> None:
        adjustment_factor_ratios = {
            symbol: _execution_adjustment_factor_change(
                self._data_provider, symbol, trade_date)
            for symbol in symbols
        }
        # 获取重采样配置
        freq_minutes = 1
        if self.freq in MINUTE_FREQS:
            try:
                config = ResampleConfig.from_freq_str(self.freq)
                freq_minutes = config.freq_minutes
            except ValueError:
                freq_minutes = 1

        # Load minute data (strategy and execution).
        minute_data = {}
        raw_minute_data = {}
        minute_time_index = {}  # execution lane: normalized timestamp -> row
        strategy_minute_time_index = {}  # strategy lane: timestamp -> row
        source_execution_time_index = {}
        source_strategy_time_index = {}
        all_times = set()
        pair_loader = getattr(self._data_provider, 'get_aligned_pair', None)

        def build_normalized_time_index(df: pd.DataFrame, symbol: str,
                                        lane: str) -> Dict[pd.Timestamp, int]:
            """Build an exact timestamp index and reject ambiguous timestamps."""
            normalized = {}
            if 'trade_time' in df.columns:
                values = df['trade_time'].tolist()
            else:
                values = df.index.tolist()
            # Iterate the original sequence rather than ``build_time_index``:
            # that helper returns a dict and would silently overwrite duplicate
            # timestamps before this strict boundary could see them.
            for row_index, value in enumerate(values):
                try:
                    timestamp = pd.Timestamp(value)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(
                        f"{symbol} invalid {lane} minute timestamp: {value!r}"
                    ) from exc
                if pd.isna(timestamp):
                    raise ValueError(
                        f"{symbol} invalid {lane} minute timestamp: {value!r}"
                    )
                if timestamp in normalized:
                    raise ValueError(
                        f"{symbol} duplicate {lane} minute timestamp: {timestamp}"
                    )
                normalized[timestamp] = row_index
            return normalized

        def sort_minute_frame(df: pd.DataFrame) -> pd.DataFrame:
            if 'trade_time' in df.columns:
                result = df.copy()
                result['trade_time'] = pd.to_datetime(
                    result['trade_time'], errors='raise')
                return result.sort_values('trade_time')
            return df.sort_index()

        for symbol in symbols:
            aligned_pair = None
            if callable(pair_loader):
                try:
                    aligned_pair = pair_loader(
                        symbol,
                        frequency='minute',
                        trade_date=trade_date,
                        pre_close_exempt_dates=(
                            self._pre_close_exempt_dates(symbol)
                        ),
                    )
                except DataContractError as exc:
                    if not _is_empty_pair_contract_error(exc):
                        raise
                    mode = self.on_missing_data
                    if mode is None:
                        mode = (
                            'raise'
                            if self.pool_source == PoolSource.SPECIFIED
                            else 'warn'
                        )
                    message = (
                        f"{symbol} minute data missing for {trade_date}; "
                        "minute backtests never downgrade to daily"
                    )
                    self._record_runtime_market_gap(symbol, trade_date)
                    if mode == 'raise':
                        raise ValueError(message) from exc
                    if mode == 'warn':
                        logger.warning(message)
                    continue
                self._data_quality_reports.append(aligned_pair.report)
            try:
                raw_df = (
                    aligned_pair.execution
                    if aligned_pair is not None
                    else self._data_provider.get_minute(
                        symbol, trade_date=trade_date,
                        price_mode='execution')
                )
                if not raw_df.empty:
                    # 重采样到目标频率
                    source_execution_time_index[symbol] = (
                        build_normalized_time_index(
                            raw_df, symbol, "execution")
                    )
                    raw_df = sort_minute_frame(raw_df)
                    raw_df = resample_minute_data(
                        raw_df, freq_minutes, symbol=symbol,
                        session_calendar=self.session_calendar)
                    raw_minute_data[symbol] = raw_df
                    minute_time_index[symbol] = build_normalized_time_index(
                        raw_df, symbol, "execution")
                    all_times.update(minute_time_index[symbol])
            except Exception as exc:
                raise RuntimeError(
                    f"{symbol} execution minute data read failed for {trade_date}"
                ) from exc

            if raw_df is None or raw_df.empty:
                raise ValueError(
                    f"{symbol} execution minute data missing for {trade_date}; "
                    "minute backtests never downgrade to daily"
                )

            try:
                df = (
                    aligned_pair.strategy
                    if aligned_pair is not None
                    else self._data_provider.get_minute(
                        symbol, trade_date=trade_date,
                        price_mode='strategy')
                )
                if df is None or df.empty:
                    raise ValueError(
                        f"{symbol} strategy minute data missing for {trade_date}; "
                        "silent lane mirroring is forbidden"
                    )
                if not df.empty:
                    # 重采样到目标频率
                    source_strategy_time_index[symbol] = (
                        build_normalized_time_index(
                            df, symbol, "strategy")
                    )
                    source_raw_times = set(
                        source_execution_time_index.get(symbol, {}))
                    source_strategy_times = set(
                        source_strategy_time_index[symbol])
                    if source_raw_times != source_strategy_times:
                        missing = sorted(
                            source_raw_times - source_strategy_times)
                        extra = sorted(
                            source_strategy_times - source_raw_times)
                        raise ValueError(
                            f"{symbol} minute timestamp mismatch between "
                            f"execution and strategy source lanes: "
                            f"missing_in_strategy={missing[:3]}, "
                            f"extra_in_strategy={extra[:3]}"
                        )
                    df = sort_minute_frame(df)
                    df = resample_minute_data(
                        df, freq_minutes, symbol=symbol,
                        session_calendar=self.session_calendar)
                    minute_data[symbol] = df
                    strategy_minute_time_index[symbol] = (
                        build_normalized_time_index(df, symbol, "strategy")
                    )
            except ValueError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"{symbol} strategy minute data read failed for {trade_date}"
                ) from exc

            raw_index = minute_time_index.get(symbol)
            strategy_index = strategy_minute_time_index.get(symbol)
            if raw_index is not None and strategy_index is not None:
                raw_times = set(raw_index)
                strategy_times = set(strategy_index)
                if raw_times != strategy_times:
                    missing = sorted(raw_times - strategy_times)
                    extra = sorted(strategy_times - raw_times)
                    raise ValueError(
                        f"{symbol} minute timestamp mismatch between execution "
                        f"and strategy lanes: missing_in_strategy={missing[:3]}, "
                        f"extra_in_strategy={extra[:3]}"
                    )

        if not raw_minute_data or not all_times:
            if not symbols:
                return
            raise ValueError(
                f"no execution minute data for {trade_date}; "
                "minute backtests never downgrade to daily"
            )

        pre_close_map: Dict[str, float] = {}
        for symbol in symbols:
            pre_close_map[symbol] = 0.0
            df = raw_minute_data.get(symbol)
            if df is not None and 'pre_close' in df.columns:
                series = df['pre_close'].dropna()
                if not series.empty:
                    pre_close_map[symbol] = float(series.iloc[0])
                    continue
            try:
                daily_row = self._data_provider.get_daily(
                    symbol, start=trade_date, end=trade_date,
                    price_mode='execution'
                )
                if not daily_row.empty and 'pre_close' in daily_row.columns:
                    val = daily_row.iloc[0].get('pre_close', 0)
                    if pd.notna(val):
                        pre_close_map[symbol] = float(val)
            except Exception:
                pass

        sorted_times = sorted(all_times)
        minute_sessions: Dict[str, Dict[pd.Timestamp, Any]] = {}
        final_non_auction_time: Dict[str, pd.Timestamp] = {}
        symbols_with_close_auction = set()
        for symbol, time_index in minute_time_index.items():
            effective_rule = self.session_calendar.get_rule(
                symbol, trade_date)
            if any(
                session.kind is SessionKind.CLOSING_AUCTION
                for session in effective_rule.sessions
            ):
                symbols_with_close_auction.add(symbol)
            symbol_sessions = {}
            for timestamp in sorted(time_index):
                _, session = self.session_calendar.session_for_timestamp(
                    symbol, pd.Timestamp(timestamp).to_pydatetime())
                symbol_sessions[pd.Timestamp(timestamp)] = session
                if session.kind is not SessionKind.CLOSING_AUCTION:
                    final_non_auction_time[symbol] = pd.Timestamp(timestamp)
            minute_sessions[symbol] = symbol_sessions

        def build_bars_at_time(bar_time):
            bars = {}
            broker_bars = {}
            for symbol, raw_df in raw_minute_data.items():
                try:
                    # 使用预索引快速查找 (O(1) 替代 O(n))
                    time_key = pd.Timestamp(bar_time)
                    time_idx = minute_time_index.get(symbol, {})
                    row_idx = time_idx.get(time_key)

                    if row_idx is not None:
                        raw_row = raw_df.iloc[row_idx]
                    else:
                        # 回退到原始查找方式
                        if 'trade_time' in raw_df.columns:
                            raw_row_df = raw_df[raw_df['trade_time'] == bar_time]
                        else:
                            raw_row_df = raw_df[raw_df.index == bar_time]
                        if raw_row_df.empty:
                            continue
                        raw_row = raw_row_df.iloc[0]

                    # NaN bar 守卫（同日线路径）：无效 bar 按缺 bar 处理
                    if not _is_valid_ohlc_row(raw_row):
                        continue

                    strat_df = minute_data[symbol]
                    strat_idx = strategy_minute_time_index.get(
                        symbol, time_idx).get(time_key)
                    if strat_idx is None:
                        # Timestamp sets are validated before any callback.  Do
                        # not reintroduce a positional/future-row fallback if a
                        # caller mutates an in-memory frame during the run.
                        raise ValueError(
                            f"{symbol} minute timestamp mismatch at {time_key}"
                        )
                    strat_row = strat_df.iloc[strat_idx]
                    if not _is_valid_ohlc_row(strat_row):
                        raise ValueError(
                            f"{symbol} invalid strategy minute bar at {time_key}"
                        )

                    bar = BarData(
                        symbol=symbol,
                        trade_time=bar_time,
                        open=float(strat_row['open']),
                        high=float(strat_row['high']),
                        low=float(strat_row['low']),
                        close=float(strat_row['close']),
                        vol=float(strat_row.get('vol', 0)),
                    )
                    bars[symbol] = bar

                    broker_bars[symbol] = BrokerBarData(
                        symbol=symbol,
                        trade_time=str(bar_time),
                        open=float(raw_row['open']),
                        high=float(raw_row['high']),
                        low=float(raw_row['low']),
                        close=float(raw_row['close']),
                        vol=float(raw_row.get('vol', 0)),
                        amount=_amount_yuan(raw_row.get('amount', 0)),  # 分钟线 amount 单位=元
                        pre_close=pre_close_map.get(symbol, 0.0),
                        adjustment_factor_ratio=(
                            adjustment_factor_ratios.get(symbol)),
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"Process minute failed for {symbol} at {bar_time}"
                    ) from exc
            return bars, broker_bars

        total_bars = len(sorted_times)
        # Each symbol owns its own first-valid-bar OPEN window.  A global flag
        # would reject a later-starting symbol merely because another symbol
        # produced an earlier bar.
        open_window_symbols = set()
        deferred_date = next_trade_date or self._context.get_next_trade_day(
            trade_date)
        if deferred_date is None:
            deferred_date = (
                pd.to_datetime(trade_date) + pd.Timedelta(days=1)
            ).strftime('%Y%m%d')
        deferred_time = pd.to_datetime(str(deferred_date))

        for i, bar_time in enumerate(sorted_times):
            if self.stop_check and self.stop_check():
                self._stopped = True
                logger.info("Minute backtest stopped")
                return

            if i % 20 == 0:
                self._yield_gil()

            # 设置K线索引 (用于 is_first_bar, is_last_bar 等)
            self._context._set_bar_index(i, total_bars)

            self._context._broker.reset_bar_used_amount()

            bars, broker_bars = build_bars_at_time(bar_time)
            if not bars:
                continue

            time_key = pd.Timestamp(bar_time)
            close_symbols = {
                symbol
                for symbol in broker_bars
                if minute_sessions[symbol][time_key].kind
                is SessionKind.CLOSING_AUCTION
            }
            close_auction_bars = {
                symbol: broker_bars[symbol] for symbol in close_symbols
            }
            normal_broker_bars = {
                symbol: broker_bar
                for symbol, broker_bar in broker_bars.items()
                if symbol not in close_symbols
            }
            normal_bars = {
                symbol: bars[symbol] for symbol in normal_broker_bars
            }

            if close_auction_bars:
                callback_clock = min(
                    minute_sessions[symbol][time_key].start
                    for symbol in close_symbols
                )
                callback_time = pd.Timestamp(
                    f"{time_key.date()} {callback_clock}"
                )
                self._context.set_datetime(callback_time)
                with self._context._broker.order_creation_scope(
                        'on_before_close', callback_time,
                        eligible_from=deferred_time,
                        expire_date=str(deferred_date).replace('-', '')[:8],
                        close_eligible_from=time_key,
                        close_expire_date=trade_date,
                        close_symbols=close_symbols):
                    with self._context._broker.target_intent_scope(time_key):
                        self._strategy.on_before_close(self._context)

            self._context.set_datetime(time_key)

            opening_bars = {
                symbol: BrokerBarData(
                    symbol=symbol,
                    trade_time=broker_bar.trade_time,
                    open=float(broker_bar.open),
                    high=float(broker_bar.open),
                    low=float(broker_bar.open),
                    close=float(broker_bar.open),
                    vol=0.0,
                    amount=broker_bar.amount,
                    pre_close=broker_bar.pre_close,
                    adjustment_factor_ratio=broker_bar.adjustment_factor_ratio,
                )
                for symbol, broker_bar in normal_broker_bars.items()
                if (
                    minute_sessions[symbol][time_key].kind
                    is SessionKind.OPENING_AUCTION
                    and symbol not in open_window_symbols
                )
            }
            if opening_bars:
                self._context._execute_open_orders(
                    opening_bars,
                    match_at=bar_time,
                    reject_missing=False,
                )
                open_window_symbols.update(opening_bars)

            self._capture_intraday_range(
                {s: b.high for s, b in broker_bars.items()},
                {s: b.low for s, b in broker_bars.items()},
            )
            # 首个有效分钟 bar 上 OPEN 先执行；这里不 reset，盘前普通单与
            # OPEN 共享同一份流动性帽。后续 bar 在循环顶部获得新额度。
            for symbol, broker_bar in normal_broker_bars.items():
                self._context._execute_market_orders(
                    broker_bar, match_at=bar_time)
            for symbol, broker_bar in close_auction_bars.items():
                self._context._execute_market_orders(
                    broker_bar, match_at=bar_time)

            close_execution_bars = dict(close_auction_bars)
            for symbol, broker_bar in normal_broker_bars.items():
                if (
                    symbol not in symbols_with_close_auction
                    and final_non_auction_time.get(symbol) == time_key
                ):
                    close_execution_bars[symbol] = broker_bar
            if close_execution_bars:
                self._context._execute_close_orders(
                    close_execution_bars, match_at=bar_time)

            if normal_bars:
                self._context._set_current_bars({s: BarData(
                    symbol=s,
                    trade_time=normal_bars[s].trade_time,
                    open=normal_broker_bars[s].open,
                    high=normal_broker_bars[s].high,
                    low=normal_broker_bars[s].low,
                    close=normal_broker_bars[s].close,
                    vol=normal_broker_bars[s].vol,
                    amount=normal_broker_bars[s].amount,
                ) for s in normal_broker_bars})
                portfolio_bars = PortfolioBarData(
                    bars=normal_bars, trade_date=trade_date)
                sim_time = pd.to_datetime(bar_time)
                with self._context._broker.order_creation_scope(
                        'on_minute', sim_time,
                        eligible_from=sim_time + pd.Timedelta(microseconds=1),
                        expire_date=trade_date):
                    self._strategy.on_minute(
                        self._context, portfolio_bars)

            prices = {s: b.close for s, b in broker_bars.items()}
            self._context._update_prices(
                prices,
                sim_time=bar_time,
                phase=(
                    'closing_mark'
                    if close_execution_bars else 'continuous_mark'),
            )

        if len(open_window_symbols) < len(raw_minute_data):
            self._context._execute_open_orders(
                {}, match_at=sorted_times[-1], reject_missing=True)

        if sorted_times:
            last_time = sorted_times[-1]
            if hasattr(last_time, 'strftime'):
                close_time = last_time + pd.Timedelta(seconds=1)
                dt_str = close_time.strftime('%Y-%m-%d %H:%M:%S')
            else:
                dt_str = str(last_time) + '.001'
            self._context.set_datetime(dt_str)

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

    def _record_positions(self, trade_date: str) -> None:
        """记录持仓"""
        positions = self._context.get_positions()
        for symbol, pos in positions.items():
            if pos.shares > 0:
                self._position_history.append({
                    'date': trade_date,
                    'symbol': symbol,
                    'shares': pos.shares,
                    'cost': pos.avg_cost,
                    'price': pos.current_price,
                    'market_value': pos.market_value,
                    'profit': pos.profit,
                    'profit_rate': pos.profit_pct,
                })

    def _get_current_trades(self, limit: int = 50) -> List[Dict]:
        """获取最近的交易记录 (用于实时更新)"""
        # 只取最后 limit 条订单，避免遍历全部
        if self._cash_replay_seed is None or self._context is None:
            return []
        broker = self._context._broker
        bundle = CashAuditBundle.from_parts(
            self._cash_replay_seed, broker.event_journal
        )
        trades = cash_replay_trade_records(bundle)
        return trades[-limit:] if len(trades) > limit else trades

    def _cash_result_projection(self):
        bundle = self.last_cash_audit
        if bundle is None:
            broker = (
                self._context._broker
                if self._context is not None else None
            )
            if broker is None or not hasattr(broker, "event_journal"):
                # Compatibility for direct metric-only construction with a
                # third-party broker double.  Current run() results always
                # carry a real journal and never take this branch.
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
    ) -> PortfolioResult:
        """创建回测结果"""
        if not self._daily_values:
            return self._create_empty_result(
                "无交易日数据", result_contract=result_contract
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

        # 基准收益率
        benchmark_code = self.benchmark
        benchmark_return = 0.0
        comparisons = None
        if benchmark_code:
            try:
                from ..comparison import (
                    ComparisonBundle,
                    ComparisonReason,
                    ComparisonStatus,
                    ReferenceIndexResult,
                    ReferenceIndexSpec,
                    build_reference_index_result,
                    comparison_scope_from_daily_values,
                )
                from ..data.index_provider import IndexProvider
                index_provider = IndexProvider(data_root=self.data_root)
                scope = comparison_scope_from_daily_values(df)
                source_identity = (
                    index_provider.get_total_return_source_identity(
                        benchmark_code
                    )
                )
                if source_identity is None:
                    # No file means there is no defensible source version.
                    # Keep a typed, explicitly unavailable leg instead of
                    # inventing an identity or silently dropping comparison.
                    spec = ReferenceIndexSpec(
                        code=benchmark_code,
                        source_id="unavailable:local_index_total_return",
                        source_version="unavailable:no_source_file",
                    )
                    reference = ReferenceIndexResult(
                        status=ComparisonStatus.UNAVAILABLE,
                        spec=spec,
                        scope=scope,
                        reason=ComparisonReason(
                            "REFERENCE_TOTAL_RETURN_SOURCE_UNAVAILABLE",
                            "No local total-return source identity is "
                            "available for {}".format(benchmark_code),
                        ),
                    )
                else:
                    source_id, source_version = source_identity
                    spec = ReferenceIndexSpec(
                        code=benchmark_code,
                        source_id=source_id,
                        source_version=source_version,
                    )
                    reference = build_reference_index_result(
                        spec,
                        index_provider,
                        scope,
                    )
                comparisons = ComparisonBundle(
                    scope=scope,
                    reference_index_total_return=reference,
                )
                if reference.total_return is not None:
                    benchmark_return = reference.total_return
                else:
                    benchmark_code = ''
            except (OSError, RuntimeError) as e:
                # Operational provider failures may make a comparison
                # unavailable. TypeError/ValueError are construction bugs and
                # intentionally remain visible to the caller.
                logger.warning(
                    "Reference comparison provider failed for %s: %s",
                    benchmark_code,
                    e,
                )
                benchmark_code = ''

        # 超额收益（基准不可用时置 0，不再静默呈现"超额=总收益"）
        excess_return = (total_return - benchmark_return) if benchmark_code else 0.0
        execution_stats = {}
        if self._context is not None and self._context._broker is not None:
            getter = getattr(self._context._broker, 'get_open_buy_stats', None)
            if callable(getter):
                execution_stats = getter()

        cash_audit, cash_replay, replay_trades = (
            self._cash_result_projection()
        )
        return PortfolioResult(
                start_date=self.start_date,
                end_date=self.end_date,
                initial_cash=initial,
                final_value=final,
                total_return=total_return,
                annual_return=annual_return,
                max_drawdown=max_drawdown,
                trade_count=(
                    0 if cash_replay is None else len(cash_replay.trades)
                ),
                sharpe_ratio=sharpe,
                max_drawdown_close_nav=max_drawdown_close_nav,
                max_drawdown_intraday_low_nav=(
                    max_drawdown_intraday_low_nav
                ),
                max_drawdown_intraday_high_to_low=(
                    max_drawdown_intraday_high_to_low
                ),
                benchmark_code=benchmark_code,
                benchmark_return=benchmark_return,
                excess_return=excess_return,
                daily_values=df,
                trades=replay_trades,
                position_history=self._position_history,
                execution_stats=execution_stats,
                win_rate=(
                    None if cash_replay is None else cash_replay.win_rate
                ),
                result_contract=result_contract,
                target_execution=self._target_execution_bundle(),
                cash_audit=cash_audit,
                comparisons=comparisons,
            )

    def _target_execution_bundle(self) -> Optional[TargetExecutionBundle]:
        """Join immutable broker target snapshots at the result boundary."""

        broker = (
            self._context._broker
            if self._context is not None else None
        )
        if broker is None:
            return None
        intents_snapshot = getattr(broker, "target_intents_snapshot", None)
        achievements_snapshot = getattr(
            broker, "target_achievements_snapshot", None
        )
        if not callable(intents_snapshot) or not callable(achievements_snapshot):
            return None
        return TargetExecutionBundle.from_snapshots(
            intents_snapshot(),
            achievements_snapshot(),
        )

    def _create_empty_result(
        self,
        error: str = "",
        result_contract: Optional[ResultContract] = None,
    ) -> PortfolioResult:
        """创建空结果"""
        cash_audit, cash_replay, trades = self._cash_result_projection()
        return PortfolioResult(
            start_date=self.start_date,
            end_date=self.end_date,
            initial_cash=self.initial_cash,
            final_value=self.initial_cash,
            total_return=0,
            annual_return=0,
            max_drawdown=0,
            trade_count=(0 if cash_replay is None else len(cash_replay.trades)),
            sharpe_ratio=None,
            max_drawdown_close_nav=0,
            max_drawdown_intraday_low_nav=0,
            max_drawdown_intraday_high_to_low=None,
            win_rate=(None if cash_replay is None else cash_replay.win_rate),
            trades=trades,
            result_contract=result_contract,
            error=error,
            target_execution=self._target_execution_bundle(),
            cash_audit=cash_audit,
        )
