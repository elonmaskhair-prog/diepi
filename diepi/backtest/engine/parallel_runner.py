"""
并行回测执行器

支持多股票并行回测
"""

import logging
import hashlib
import json
import math
import multiprocessing as mp
from numbers import Real
from typing import List, Dict, Any, Optional, Type
from dataclasses import dataclass, field
from concurrent.futures import ProcessPoolExecutor, as_completed
import pandas as pd

from .backtest_engine import BacktestEngine, BacktestResult
from ..broker.replay import CashAuditBundle, cash_replay_trade_records
from ..broker.target_execution import TargetExecutionBundle
from ..comparison import ComparisonReason, ComparisonStatus
from ..liquidity import DailyAuctionLiquidityPolicy
from ..session_calendar import normalize_session_date
from ..result_contract import (
    ResultAssumption,
    ResultContract,
    ResultWarning,
)
from ..strategy.base import Strategy

logger = logging.getLogger(__name__)


_STOPPED_ERROR = "CANCELED: parallel run stopped before result collection"
_MISSING_RESULT_ERROR = "NO_RESULT: no terminal result was collected"
_PARALLEL_RESULT_WIRE_SCHEMA = "diepi.parallel_backtest_result"
_PARALLEL_RESULT_WIRE_SCHEMA_VERSION = 2
_PARALLEL_RESULT_WIRE_KEYS = frozenset(
    {
        "annual_return",
        "cash_audit",
        "comparisons",
        "end_date",
        "final_value",
        "initial_cash",
        "max_drawdown",
        "max_drawdown_close_nav",
        "max_drawdown_intraday_high_to_low",
        "max_drawdown_intraday_low_nav",
        "observation_ids",
        "reference_total_return_excess",
        "result_contract",
        "schema",
        "schema_version",
        "sharpe_ratio",
        "start_date",
        "symbol",
        "target_execution",
        "total_return",
        "trade_count",
        "win_rate",
    }
)
_PARALLEL_COMPARISON_REASON = ComparisonReason(
    "PARALLEL_COMPARISON_SERIES_UNAVAILABLE",
    "ParallelRunner retains child observation dates for ranking validation "
    "but not child daily NAV/reference series; aggregate comparisons were "
    "not generated",
)
_PARALLEL_COMPARISON_WARNING = ResultWarning(
    "PARALLEL_COMPARISON_NOT_GENERATED",
    "ParallelRunner aggregate comparisons require child daily NAV and "
    "reference series, which are not present in the aggregate wire payload",
)
_PARALLEL_COMPARISON_ASSUMPTION = ResultAssumption(
    "comparison.parallel_aggregation",
    "unsupported_without_child_daily_nav_series",
)


def _validate_wire_root(payload: Any) -> Dict[str, Any]:
    if type(payload) is not dict:
        raise TypeError("result payload must be exactly dict")
    actual = set(payload)
    if actual != _PARALLEL_RESULT_WIRE_KEYS:
        missing = sorted(_PARALLEL_RESULT_WIRE_KEYS - actual)
        unknown = sorted(actual - _PARALLEL_RESULT_WIRE_KEYS)
        raise ValueError(
            "parallel result wire keys mismatch: "
            f"missing={missing}, unknown={unknown}"
        )
    if payload["schema"] != _PARALLEL_RESULT_WIRE_SCHEMA:
        raise ValueError("parallel result wire schema mismatch")
    if (
        type(payload["schema_version"]) is not int
        or payload["schema_version"]
        != _PARALLEL_RESULT_WIRE_SCHEMA_VERSION
    ):
        raise ValueError("parallel result wire schema_version mismatch")
    return payload


def _validate_observation_ids(
    values: Any,
    contract: ResultContract,
    *,
    field: str,
) -> tuple[str, ...]:
    """Validate an ordered, exact daily observation scope."""

    if type(values) is not list:
        raise TypeError(f"{field} must be exactly list")
    normalized = []
    for index, value in enumerate(values):
        if type(value) is not str:
            raise TypeError(f"{field}[{index}] must be exactly str")
        normalized_value = normalize_session_date(
            value, field=f"{field}[{index}]"
        )
        if value != normalized_value:
            raise ValueError(f"{field}[{index}] must use YYYY-MM-DD")
        normalized.append(normalized_value)
    observation_ids = tuple(normalized)
    if observation_ids != tuple(sorted(observation_ids)):
        raise ValueError(f"{field} must be strictly increasing")
    if len(set(observation_ids)) != len(observation_ids):
        raise ValueError(f"{field} must not contain duplicate dates")

    coverage = contract.data_coverage
    interval = contract.actual_interval
    if coverage is None:
        if observation_ids:
            raise ValueError(
                f"{field} must be empty when result coverage is absent"
            )
        return observation_ids
    if len(observation_ids) != coverage.actual_observations:
        raise ValueError(
            f"{field} length must equal data_coverage.actual_observations"
        )
    if observation_ids:
        if interval is None:
            raise ValueError(
                f"{field} requires result_contract.actual_interval"
            )
        if (
            observation_ids[0] != interval.start_date
            or observation_ids[-1] != interval.end_date
        ):
            raise ValueError(
                f"{field} endpoints must equal result_contract.actual_interval"
            )
    return observation_ids


def _result_observation_ids(result: BacktestResult) -> tuple[str, ...]:
    if type(result.daily_values) is not pd.DataFrame:
        raise TypeError("result.daily_values must be exactly DataFrame")
    contract = result.result_contract
    raw_values = list(result.daily_values.index)
    normalized = []
    for index, value in enumerate(raw_values):
        if isinstance(value, pd.Timestamp) and value.tz is not None:
            raise ValueError("daily_values.index must be timezone-naive")
        normalized.append(
            normalize_session_date(
                value, field=f"daily_values.index[{index}]"
            )
        )
    return _validate_observation_ids(
        normalized,
        contract,
        field="observation_ids",
    )


def _ordered_text_hash(values: tuple[str, ...]) -> str:
    payload = json.dumps(
        values,
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def _serialize_backtest_result_wire(
    result: BacktestResult,
    *,
    require_recorded_target: bool = False,
) -> Dict[str, Any]:
    """Serialize raw result state without display rounding or percentages."""

    if type(result) is not BacktestResult:
        raise TypeError("result must be exactly BacktestResult")
    result._validate_target_execution()
    result._validate_cash_audit()
    contract = result.result_contract
    if type(contract) is not ResultContract:
        raise TypeError("result.result_contract must be exactly ResultContract")
    target = result.target_execution
    if target is not None and type(target) is not TargetExecutionBundle:
        raise TypeError(
            "result.target_execution must be exactly "
            "TargetExecutionBundle or None"
        )
    if require_recorded_target and target is None:
        raise ValueError("current result must record target_execution")
    cash_audit = result.cash_audit
    if cash_audit is not None and type(cash_audit) is not CashAuditBundle:
        raise TypeError(
            "result.cash_audit must be exactly CashAuditBundle or None"
        )
    observation_ids = _result_observation_ids(result)

    payload = {
        "annual_return": result.annual_return,
        "cash_audit": (
            None if cash_audit is None else cash_audit.to_dict()
        ),
        "comparisons": None,
        "end_date": result.end_date,
        "final_value": result.final_value,
        "initial_cash": result.initial_cash,
        "max_drawdown": result.max_drawdown,
        "max_drawdown_close_nav": result.max_drawdown_close_nav,
        "max_drawdown_intraday_high_to_low": (
            result.max_drawdown_intraday_high_to_low
        ),
        "max_drawdown_intraday_low_nav": (
            result.max_drawdown_intraday_low_nav
        ),
        "observation_ids": list(observation_ids),
        "reference_total_return_excess": None,
        "result_contract": contract.to_dict(),
        "schema": _PARALLEL_RESULT_WIRE_SCHEMA,
        "schema_version": _PARALLEL_RESULT_WIRE_SCHEMA_VERSION,
        "sharpe_ratio": result.sharpe_ratio,
        "start_date": result.start_date,
        "symbol": result.symbol,
        "target_execution": None if target is None else target.to_dict(),
        "total_return": result.total_return,
        "trade_count": result.trade_count,
        "win_rate": result.win_rate,
    }
    _validate_wire_root(payload)
    _parse_wire_contract(payload)
    _parse_wire_target_execution(
        payload,
        require_recorded=require_recorded_target,
    )
    _parse_wire_cash_audit(payload)
    _validate_observation_ids(
        payload["observation_ids"],
        contract,
        field="observation_ids",
    )
    for field_name in (
        "annual_return",
        "final_value",
        "initial_cash",
        "max_drawdown",
        "max_drawdown_close_nav",
        "max_drawdown_intraday_low_nav",
        "total_return",
    ):
        _wire_number(payload, field_name)
    for field_name in (
        "max_drawdown_intraday_high_to_low",
        "sharpe_ratio",
        "win_rate",
    ):
        _wire_number(payload, field_name, optional=True)
    for field_name in ("symbol", "start_date", "end_date"):
        _wire_text(payload, field_name)
    trade_count = payload["trade_count"]
    if type(trade_count) is not int or trade_count < 0:
        raise TypeError("trade_count must be a non-negative int")
    return payload


def _parse_wire_contract(payload: Any) -> ResultContract:
    """Parse the untrusted, pickle-safe result contract at a process boundary."""
    if type(payload) is not dict:
        raise TypeError("result payload must be exactly dict")
    if "result_contract" not in payload:
        raise ValueError("result payload is missing result_contract")
    return ResultContract.from_dict(payload["result_contract"])


def _parse_wire_target_execution(
    payload: Dict[str, Any],
    *,
    require_recorded: bool = False,
) -> Optional[TargetExecutionBundle]:
    """Preserve typed target evidence across the process boundary."""

    if "target_execution" not in payload:
        # Legacy child payloads predate the target result contract.
        if require_recorded:
            raise ValueError(
                "current result payload is missing target_execution"
            )
        return None
    value = payload["target_execution"]
    if value is None:
        if require_recorded:
            raise ValueError(
                "current result payload must record target_execution"
            )
        return None
    return TargetExecutionBundle.from_dict(value)


def _parse_wire_cash_audit(
    payload: Dict[str, Any],
) -> Optional[CashAuditBundle]:
    if "cash_audit" not in payload:
        raise ValueError("result payload is missing cash_audit")
    value = payload["cash_audit"]
    if value is None:
        return None
    return CashAuditBundle.from_dict(value)


def _reject_unscoped_wire_comparison(payload: Dict[str, Any]) -> None:
    """Refuse to silently retain or discard a child comparison leg.

    The v2 wire retains ordered observation dates for ranking compatibility,
    but intentionally omits child daily NAV and reference-level series.  A
    non-null comparison or excess value is therefore rejected instead of
    being copied onto an unverifiable aggregate result.
    """

    if payload.get("comparisons") is not None:
        raise ValueError(
            "PARALLEL_COMPARISON_SERIES_UNAVAILABLE: non-null comparisons cannot "
            "be preserved without the child's daily NAV/reference series"
        )
    if payload.get("reference_total_return_excess") is not None:
        raise ValueError(
            "PARALLEL_COMPARISON_SERIES_UNAVAILABLE: a numeric reference excess "
            "cannot be preserved without its daily reference series"
        )


def _with_parallel_comparison_disclosure(
    contract: ResultContract,
) -> ResultContract:
    warning_by_code = {warning.code: warning for warning in contract.warnings}
    existing_warning = warning_by_code.get(
        _PARALLEL_COMPARISON_WARNING.code
    )
    if (
        existing_warning is not None
        and existing_warning != _PARALLEL_COMPARISON_WARNING
    ):
        raise ValueError(
            "conflicting PARALLEL_COMPARISON_NOT_GENERATED warning"
        )
    warnings = contract.warnings
    if existing_warning is None:
        warnings = warnings + (_PARALLEL_COMPARISON_WARNING,)

    assumption_by_key = {
        assumption.key: assumption for assumption in contract.assumptions
    }
    existing_assumption = assumption_by_key.get(
        _PARALLEL_COMPARISON_ASSUMPTION.key
    )
    if (
        existing_assumption is not None
        and existing_assumption != _PARALLEL_COMPARISON_ASSUMPTION
    ):
        raise ValueError(
            "conflicting comparison.parallel_aggregation assumption"
        )
    assumptions = contract.assumptions
    if existing_assumption is None:
        assumptions = assumptions + (_PARALLEL_COMPARISON_ASSUMPTION,)

    return ResultContract(
        status=contract.status,
        reason=contract.reason,
        warnings=warnings,
        assumptions=assumptions,
        actual_interval=contract.actual_interval,
        data_coverage=contract.data_coverage,
        semantics_version=contract.semantics_version,
        schema_version=contract.schema_version,
    )


def _wire_number(
    payload: Dict[str, Any],
    field_name: str,
    *,
    optional: bool = False,
) -> Optional[float]:
    if field_name not in payload:
        raise ValueError(f"result payload is missing {field_name}")
    value = payload[field_name]
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        suffix = " or None" if optional else ""
        raise TypeError(f"{field_name} must be a finite real number{suffix}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


def _wire_text(payload: Dict[str, Any], field_name: str) -> str:
    if field_name not in payload:
        raise ValueError(f"result payload is missing {field_name}")
    value = payload[field_name]
    if type(value) is not str or not value:
        raise TypeError(f"{field_name} must be a non-empty str")
    return value


def _restore_backtest_result(
    expected_symbol: str,
    payload: Any,
) -> BacktestResult:
    """Restore one rankable result without inferring legacy/missing fields."""
    # Keep the dedicated missing-contract diagnostic used by callers, then
    # enforce the complete versioned envelope and every remaining field.
    contract = _parse_wire_contract(payload)
    payload = _validate_wire_root(payload)
    _reject_unscoped_wire_comparison(payload)
    if not contract.is_rankable:
        reason = contract.reason
        reason_text = (
            "missing terminal reason"
            if reason is None
            else f"{reason.code}: {reason.message}"
        )
        raise ValueError(
            f"status={contract.status.value} is not rankable; "
            f"reason={reason_text}"
        )

    symbol = _wire_text(payload, "symbol")
    if symbol != expected_symbol:
        raise ValueError(
            f"result symbol mismatch: expected {expected_symbol!r}, "
            f"got {symbol!r}"
        )

    trade_count = payload.get("trade_count")
    if type(trade_count) is not int or trade_count < 0:
        raise TypeError("trade_count must be a non-negative int")

    max_drawdown_intraday_high_to_low = _wire_number(
        payload,
        "max_drawdown_intraday_high_to_low",
        optional=True,
    )
    win_rate = _wire_number(payload, "win_rate", optional=True)
    sharpe_ratio = _wire_number(payload, "sharpe_ratio", optional=True)

    disclosed_contract = _with_parallel_comparison_disclosure(contract)
    observation_ids = _validate_observation_ids(
        payload["observation_ids"],
        contract,
        field="observation_ids",
    )
    target_execution = _parse_wire_target_execution(payload)
    cash_audit = _parse_wire_cash_audit(payload)
    trades = (
        []
        if cash_audit is None
        else cash_replay_trade_records(cash_audit)
    )
    return BacktestResult(
        symbol=symbol,
        start_date=_wire_text(payload, "start_date"),
        end_date=_wire_text(payload, "end_date"),
        initial_cash=float(_wire_number(payload, "initial_cash")),
        final_value=float(_wire_number(payload, "final_value")),
        total_return=float(_wire_number(payload, "total_return")),
        annual_return=float(_wire_number(payload, "annual_return")),
        max_drawdown=float(_wire_number(payload, "max_drawdown")),
        trade_count=trade_count,
        win_rate=win_rate,
        sharpe_ratio=sharpe_ratio,
        max_drawdown_close_nav=float(
            _wire_number(payload, "max_drawdown_close_nav")
        ),
        max_drawdown_intraday_low_nav=float(
            _wire_number(payload, "max_drawdown_intraday_low_nav")
        ),
        max_drawdown_intraday_high_to_low=(
            None
            if max_drawdown_intraday_high_to_low is None
            else max_drawdown_intraday_high_to_low
        ),
        daily_values=pd.DataFrame(
            index=pd.to_datetime(observation_ids)
        ),
        trades=trades,
        result_contract=disclosed_contract,
        target_execution=target_execution,
        cash_audit=cash_audit,
    )


def _external_error(error: Any) -> str:
    detail = str(error).strip()
    if not detail:
        detail = "child task returned an empty error message"
    return f"RUN_FAILED: {detail}"


@dataclass
class ParallelResult:
    """并行回测汇总结果"""
    start_date: str
    end_date: str
    initial_cash: float
    total_symbols: int
    success_count: int
    failed_count: int

    # 汇总统计
    avg_return: float  # 平均收益率
    avg_annual_return: float  # 平均年化收益率
    avg_max_drawdown: float  # 平均最大回撤
    avg_sharpe: Optional[float]  # 平均夏普比率；无有效样本时为 None
    avg_win_rate: Optional[float]  # 平均胜率；无已平仓样本时为 None

    # 排行榜
    top_performers: List[Dict] = field(default_factory=list)  # 收益最高
    worst_performers: List[Dict] = field(default_factory=list)  # 收益最低

    # 详细结果
    results: Dict[str, BacktestResult] = field(default_factory=dict)
    errors: Dict[str, str] = field(default_factory=dict)

    # The caller may supply point-in-time universe evidence when symbol
    # selection happens outside ParallelRunner (for example in the GUI).
    # Keep new compatibility fields after all legacy positional fields.
    universe_assumptions: Dict[str, str] = field(default_factory=dict)
    universe_warnings: List[str] = field(default_factory=list)
    ranking_scope: Optional[tuple[str, str, int, int]] = None
    ranking_observation_ids: tuple[str, ...] = ()
    ranking_error: Optional[str] = None
    requested_symbols: tuple[str, ...] = ()

    @property
    def is_rankable(self) -> bool:
        """Whether rankings cover the complete requested symbol universe."""

        observation_ids = self.ranking_observation_ids
        scope = self.ranking_scope
        scope_is_exact = (
            scope is not None
            and bool(observation_ids)
            and observation_ids == tuple(sorted(set(observation_ids)))
            and scope == (
                observation_ids[0],
                observation_ids[-1],
                len(observation_ids),
                len(observation_ids),
            )
        )
        return (
            self.total_symbols > 0
            and self.failed_count == 0
            and self.success_count == self.total_symbols
            and scope_is_exact
            and self.ranking_error is None
        )

    @property
    def comparisons(self):
        """No aggregate bundle exists without exact child daily scopes."""

        return None

    @property
    def comparison_status(self) -> ComparisonStatus:
        return ComparisonStatus.UNAVAILABLE

    @property
    def comparison_reason(self) -> ComparisonReason:
        return _PARALLEL_COMPARISON_REASON

    @property
    def comparison_warnings(self):
        return (_PARALLEL_COMPARISON_WARNING,)

    @property
    def comparison_assumptions(self):
        return (_PARALLEL_COMPARISON_ASSUMPTION,)

    def to_dict(self) -> Dict:
        requested_symbols = self.requested_symbols or tuple(dict.fromkeys((
            *self.results.keys(),
            *self.errors.keys(),
        )))
        return {
            'start_date': self.start_date,
            'end_date': self.end_date,
            'initial_cash': self.initial_cash,
            'total_symbols': self.total_symbols,
            'success_count': self.success_count,
            'failed_count': self.failed_count,
            'avg_return': round(self.avg_return * 100, 2),
            'avg_annual_return': round(self.avg_annual_return * 100, 2),
            'avg_max_drawdown': round(self.avg_max_drawdown * 100, 2),
            'avg_sharpe': (
                None
                if self.avg_sharpe is None
                else round(self.avg_sharpe, 3)
            ),
            'avg_win_rate': (
                None
                if self.avg_win_rate is None
                else round(self.avg_win_rate * 100, 2)
            ),
            'top_performers': self.top_performers,
            'worst_performers': self.worst_performers,
            'errors': dict(self.errors),
            'universe_disclosure': {
                'assumptions': dict(self.universe_assumptions),
                'warnings': list(self.universe_warnings),
                'symbols': list(requested_symbols),
                'symbols_sha256': (
                    None
                    if not requested_symbols
                    else _ordered_text_hash(requested_symbols)
                ),
            },
            'is_rankable': self.is_rankable,
            'ranking_disclosure': {
                'scope': (
                    None
                    if self.ranking_scope is None
                    else {
                        'actual_start': self.ranking_scope[0],
                        'actual_end': self.ranking_scope[1],
                        'expected_observations': self.ranking_scope[2],
                        'actual_observations': self.ranking_scope[3],
                    }
                ),
                'error': self.ranking_error,
                'observation_count': len(self.ranking_observation_ids),
                'observation_ids_sha256': (
                    None
                    if not self.ranking_observation_ids
                    else _ordered_text_hash(
                        self.ranking_observation_ids
                    )
                ),
                'scope_evidence': 'ordered_observation_ids_sha256',
            },
            'comparisons': self.comparisons,
            'comparison_disclosure': {
                'status': self.comparison_status.value,
                'reason': self.comparison_reason.to_dict(),
                'warnings': [
                    warning.to_dict()
                    for warning in self.comparison_warnings
                ],
                'assumptions': [
                    assumption.to_dict()
                    for assumption in self.comparison_assumptions
                ],
            },
        }

    def to_dataframe(self) -> pd.DataFrame:
        """转换为 DataFrame 便于分析"""
        rows = []
        for symbol, result in self.results.items():
            rows.append(result.to_dict())
        return pd.DataFrame(rows)


def _compile_strategy_in_subprocess(code: str):
    """
    在子进程中编译策略代码（委托 cli.runner.compile_strategy 单一真源）

    历史P1：此处曾是旧注入逻辑的逐字拷贝——CLI 修了"类式策略静默
    no-op"后，本拷贝里同型缺陷仍存活。现统一委托，杜绝分叉。
    """
    from ..cli.runner import compile_strategy
    return compile_strategy(code, strategy_kind='single')


def _run_single_backtest(args: tuple) -> tuple:
    """
    单股票回测任务（子进程执行）

    Args:
        args: (symbol, strategy_code, engine_kwargs)
            engine_kwargs 为 BacktestEngine 除 symbol 外的构造参数（可序列化）

    Returns:
        (symbol, result_dict, error_msg)
    """
    (symbol, strategy_code, engine_kwargs) = args

    try:
        # 在子进程中编译策略
        strategy_class = _compile_strategy_in_subprocess(strategy_code)
        strategy = strategy_class()

        # 创建引擎并运行（传入交易成本参数）
        engine = BacktestEngine(symbol=symbol, **engine_kwargs)
        result = engine.run(strategy)

        # 子进程输出是不可信边界；在发送前先验证一次，父进程聚合时再验证。
        payload = _serialize_backtest_result_wire(
            result,
            require_recorded_target=True,
        )
        return (symbol, payload, None)

    except Exception as e:
        logger.error(f"Backtest failed for {symbol}: {e}")
        return (symbol, None, f"{type(e).__name__}: {e}")


class ParallelRunner:
    """
    并行回测执行器

    使用多进程并行执行多股票回测

    Usage:
        runner = ParallelRunner(
            symbols=['000001.SZ', '000002.SZ', ...],
            start_date='20250101',
            end_date='20250601',
            initial_cash=100000,
            max_workers=8
        )
        result = runner.run(MyStrategy, param1=1, param2=2)
    """

    def __init__(
        self,
        symbols: List[str],
        start_date: str,
        end_date: str,
        initial_cash: float = 100000.0,
        freq: str = 'minute',
        max_workers: Optional[int] = None,
        slippage: float = 0.001,
        commission_rate: float = 0.00025,
        stamp_duty: float = 0.001,
        min_commission: float = 5.0,
        lot_size: int = 100,
        strategy_price_mode: str = 'hfq',
        execution_price_mode: str = 'raw',
        open_buy_resize_mode: str = 'auto',
        liquidity_cap_ratio: float = 0.8,
        limit_pct_overrides: Optional[Dict[str, float]] = None,
        open_buy_fill_mode: str = 'open+slip',
        open_buy_sizing: str = 'limit_up',
        t0_overrides=None,
        trading_days_per_year: int = 252,
        risk_free_rate: float = 0.03,
        progress_callback=None,
        stop_check=None,
        daily_auction_liquidity: Optional[
            DailyAuctionLiquidityPolicy
        ] = None,
        transfer_fee_rate: float = 0.0,
        universe_assumptions: Optional[Dict[str, str]] = None,
        universe_warnings: Optional[List[str]] = None,
        data_root=None,
    ):
        """
        Args:
            symbols: 股票代码列表
            start_date: 开始日期 (YYYYMMDD)
            end_date: 结束日期 (YYYYMMDD)
            initial_cash: 每只股票的初始资金
            freq: 回测频率 ('minute' 或 'daily')
            max_workers: 最大并行数，默认为 CPU 核心数
            slippage: 滑点
            commission_rate: 佣金率
            stamp_duty: 印花税
            transfer_fee_rate: 双边过户费率 (默认0；不做历史自动切换)
            min_commission: 单笔最低佣金 (默认5元)
            lot_size: 每手股数 (默认100)
            open_buy_resize_mode: 开盘买单缩量模式 ('auto'/'legacy')
            liquidity_cap_ratio: 单根K线最大可吃成交额比例 (默认0.8)
            limit_pct_overrides: 涨跌停幅度覆盖表 {代码: 幅度}
            daily_auction_liquidity: 日线开/收盘集合竞价的显式流动性上限
            progress_callback: 进度回调函数 (current, total, message)
            stop_check: 停止检查函数，返回True时停止
        """
        if (
            daily_auction_liquidity is not None
            and type(daily_auction_liquidity)
            is not DailyAuctionLiquidityPolicy
        ):
            raise TypeError(
                "daily_auction_liquidity must be exactly "
                "DailyAuctionLiquidityPolicy or None"
            )
        # Results, errors and process futures are keyed by symbol.  Accepting
        # duplicates would silently collapse tasks while total_symbols kept
        # counting the original list, making success + failed != total.
        symbol_list = list(symbols)
        seen = set()
        duplicates = []
        for symbol in symbol_list:
            if symbol in seen and symbol not in duplicates:
                duplicates.append(symbol)
            seen.add(symbol)
        if duplicates:
            raise ValueError(
                f"symbols must be unique; duplicates={duplicates!r}")
        # Snapshot caller-owned input so later list mutation cannot invalidate
        # the uniqueness and aggregation invariants.
        self.symbols = symbol_list
        expected_start = normalize_session_date(
            start_date, field="start_date"
        )
        expected_end = normalize_session_date(end_date, field="end_date")
        if expected_start > expected_end:
            raise ValueError("start_date must be on or before end_date")
        if (
            isinstance(initial_cash, bool)
            or not isinstance(initial_cash, Real)
            or not math.isfinite(float(initial_cash))
            or float(initial_cash) <= 0
        ):
            raise ValueError("initial_cash must be a finite positive number")
        self.start_date = expected_start.replace("-", "")
        self.end_date = expected_end.replace("-", "")
        self.initial_cash = float(initial_cash)
        self._expected_start_date = expected_start
        self._expected_end_date = expected_end
        self.freq = freq
        self.max_workers = max_workers or mp.cpu_count()
        self.slippage = slippage
        self.commission_rate = commission_rate
        self.stamp_duty = stamp_duty
        self.transfer_fee_rate = transfer_fee_rate
        self.min_commission = min_commission
        self.lot_size = lot_size
        if strategy_price_mode not in ('hfq', 'raw'):
            raise ValueError("strategy_price_mode must be 'hfq' or 'raw'")
        if execution_price_mode not in ('hfq', 'raw'):
            raise ValueError("execution_price_mode must be 'hfq' or 'raw'")
        self.strategy_price_mode = strategy_price_mode
        self.execution_price_mode = execution_price_mode
        self.open_buy_resize_mode = open_buy_resize_mode
        self.liquidity_cap_ratio = liquidity_cap_ratio
        self.limit_pct_overrides = limit_pct_overrides
        self.open_buy_fill_mode = open_buy_fill_mode
        self.open_buy_sizing = open_buy_sizing
        self.t0_overrides = set(t0_overrides) if t0_overrides else None
        self.trading_days_per_year = trading_days_per_year
        self.risk_free_rate = risk_free_rate
        self.daily_auction_liquidity = daily_auction_liquidity
        self.progress_callback = progress_callback
        self.stop_check = stop_check
        self.universe_assumptions = dict(universe_assumptions or {})
        self.universe_warnings = list(universe_warnings or [])
        # Pass one explicit local dataset root to every child engine.
        self.data_root = data_root

    def _engine_kwargs(self) -> Dict:
        """BacktestEngine 构造参数（除 symbol 外，全部可序列化跨进程）"""
        return {
            'start_date': self.start_date,
            'end_date': self.end_date,
            'initial_cash': self.initial_cash,
            'freq': self.freq,
            'slippage': self.slippage,
            'commission_rate': self.commission_rate,
            'stamp_duty': self.stamp_duty,
            'transfer_fee_rate': self.transfer_fee_rate,
            'min_commission': self.min_commission,
            'lot_size': self.lot_size,
            'strategy_price_mode': self.strategy_price_mode,
            'execution_price_mode': self.execution_price_mode,
            'open_buy_resize_mode': self.open_buy_resize_mode,
            'liquidity_cap_ratio': self.liquidity_cap_ratio,
            'limit_pct_overrides': self.limit_pct_overrides,
            'open_buy_fill_mode': self.open_buy_fill_mode,
            'open_buy_sizing': self.open_buy_sizing,
            't0_overrides': self.t0_overrides,
            'trading_days_per_year': self.trading_days_per_year,
            'risk_free_rate': self.risk_free_rate,
            'daily_auction_liquidity': self.daily_auction_liquidity,
            'data_root': self.data_root,
        }

    def run(
        self,
        strategy_code: str,
    ) -> ParallelResult:
        """
        运行并行回测

        Args:
            strategy_code: 策略代码字符串（在子进程中编译）

        Returns:
            ParallelResult
        """
        logger.info(f"Starting parallel backtest: {len(self.symbols)} symbols, {self.max_workers} workers")

        # 准备任务参数（包含交易成本）
        engine_kwargs = self._engine_kwargs()
        tasks = [
            (symbol, strategy_code, engine_kwargs)
            for symbol in self.symbols
        ]

        # 存储结果
        results: Dict[str, Dict] = {}
        errors: Dict[str, str] = {}
        stopped = False

        # 使用进程池执行
        with ProcessPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {executor.submit(_run_single_backtest, task): task[0]
                      for task in tasks}

            completed = 0
            total = len(self.symbols)

            for future in as_completed(futures):
                # 检查是否需要停止
                if self.stop_check and self.stop_check():
                    stopped = True
                    logger.info("Stop requested, cancelling remaining tasks...")
                    for f in futures:
                        f.cancel()
                    break

                symbol = futures[future]
                completed += 1

                try:
                    sym, result_dict, error = future.result()
                    if sym != symbol:
                        mismatch = (
                            "RESULT_SYMBOL_MISMATCH: child returned "
                            f"{sym!r} for requested symbol {symbol!r}"
                        )
                        errors[symbol] = mismatch
                        logger.warning(
                            f"[{completed}/{total}] {symbol} failed: {mismatch}"
                        )
                    elif error:
                        errors[symbol] = _external_error(error)
                        logger.warning(
                            f"[{completed}/{total}] {symbol} failed: {error}"
                        )
                    else:
                        # 聚合器会严格解析；此处不先解引用未验证字段。
                        results[symbol] = result_dict
                        logger.info(
                            f"[{completed}/{total}] {symbol} returned a result payload"
                        )
                except Exception as e:
                    errors[symbol] = _external_error(
                        f"{type(e).__name__}: {e}"
                    )
                    logger.error(f"[{completed}/{total}] {symbol} error: {e}")

                # 进度回调 (节流：每10只股票或最后一只时更新)
                if self.progress_callback:
                    should_update = (completed % 10 == 0 or completed == total)
                    if should_update:
                        pending = total - completed
                        running = min(self.max_workers, pending)
                        msg = f"已完成: {completed} | 运行中: {running} | 待处理: {pending}"
                        self.progress_callback(completed, total, msg)

        if stopped:
            for symbol in self.symbols:
                if symbol not in results and symbol not in errors:
                    errors[symbol] = _STOPPED_ERROR

        # 汇总结果
        aggregate = self._aggregate_results(results, errors)
        if stopped:
            logger.info(
                "Parallel backtest stopped. "
                f"Success: {aggregate.success_count}, "
                f"Failed/canceled: {aggregate.failed_count}"
            )
        else:
            logger.info(
                "Parallel backtest finished. "
                f"Success: {aggregate.success_count}, "
                f"Failed: {aggregate.failed_count}"
            )
        return aggregate

    def run_sequential(
        self,
        strategy_class: Type[Strategy],
        **strategy_kwargs
    ) -> ParallelResult:
        """
        顺序执行回测（调试用）

        Args:
            strategy_class: 策略类
            **strategy_kwargs: 策略构造参数

        Returns:
            ParallelResult
        """
        if (
            not isinstance(strategy_class, type)
            or not issubclass(strategy_class, Strategy)
        ):
            raise TypeError(
                "parallel sequential mode requires a Strategy subclass"
            )
        logger.info(f"Starting sequential backtest: {len(self.symbols)} symbols")

        results: Dict[str, Dict] = {}
        errors: Dict[str, str] = {}

        for i, symbol in enumerate(self.symbols, 1):
            if self.stop_check and self.stop_check():
                for remaining_symbol in self.symbols[i - 1:]:
                    errors.setdefault(remaining_symbol, _STOPPED_ERROR)
                break
            try:
                strategy = strategy_class(**strategy_kwargs)
                engine = BacktestEngine(symbol=symbol, **self._engine_kwargs())
                result = engine.run(strategy)
                payload = _serialize_backtest_result_wire(
                    result,
                    require_recorded_target=True,
                )
                results[symbol] = payload
                logger.info(f"[{i}/{len(self.symbols)}] {symbol} completed: "
                           f"return={result.total_return*100:.2f}%")
            except Exception as e:
                errors[symbol] = _external_error(
                    f"{type(e).__name__}: {e}"
                )
                logger.warning(f"[{i}/{len(self.symbols)}] {symbol} failed: {e}")

        return self._aggregate_results(results, errors)

    def _aggregate_results(
        self,
        results: Dict[str, Any],
        errors: Dict[str, str]
    ) -> ParallelResult:
        """严格恢复并汇总结果；只有 SUCCESS contract 可比较。"""
        accepted: Dict[str, BacktestResult] = {}
        normalized_errors: Dict[str, str] = {}

        for symbol in self.symbols:
            if symbol in errors:
                normalized_errors[symbol] = str(errors[symbol])
                continue
            if symbol not in results:
                normalized_errors[symbol] = _MISSING_RESULT_ERROR
                continue
            try:
                restored = _restore_backtest_result(
                    symbol,
                    results[symbol],
                )
                child_start = normalize_session_date(
                    restored.start_date, field="child.start_date"
                )
                child_end = normalize_session_date(
                    restored.end_date, field="child.end_date"
                )
                mismatches = []
                if child_start != self._expected_start_date:
                    mismatches.append(
                        f"start_date={child_start!r} expected="
                        f"{self._expected_start_date!r}"
                    )
                if child_end != self._expected_end_date:
                    mismatches.append(
                        f"end_date={child_end!r} expected="
                        f"{self._expected_end_date!r}"
                    )
                if restored.initial_cash != self.initial_cash:
                    mismatches.append(
                        f"initial_cash={restored.initial_cash!r} expected="
                        f"{self.initial_cash!r}"
                    )
                if mismatches:
                    raise ValueError(
                        "RUN_ENVELOPE_MISMATCH: " + "; ".join(mismatches)
                    )
                accepted[symbol] = restored
            except Exception as exc:
                normalized_errors[symbol] = (
                    "INVALID_OR_NON_RANKABLE_RESULT: "
                    f"{type(exc).__name__}: {exc}"
                )

        success_count = len(accepted)
        failed_count = len(normalized_errors)

        if success_count == 0:
            return ParallelResult(
                start_date=self.start_date,
                end_date=self.end_date,
                initial_cash=self.initial_cash,
                total_symbols=len(self.symbols),
                success_count=0,
                failed_count=failed_count,
                avg_return=0,
                avg_annual_return=0,
                avg_max_drawdown=0,
                avg_sharpe=None,
                avg_win_rate=None,
                universe_assumptions=self.universe_assumptions,
                universe_warnings=self.universe_warnings,
                errors=normalized_errors,
                ranking_error=(
                    "NO_RANKABLE_RESULTS: no child result passed the strict "
                    "result contract"
                ),
                requested_symbols=tuple(self.symbols),
            )

        scope_by_symbol = {}
        observation_scope_by_symbol = {}
        truncated_symbols = []
        for symbol, result in accepted.items():
            contract = result.result_contract
            interval = contract.actual_interval
            coverage = contract.data_coverage
            scope_by_symbol[symbol] = (
                interval.start_date,
                interval.end_date,
                coverage.expected_observations,
                coverage.actual_observations,
            )
            observation_scope_by_symbol[symbol] = (
                _result_observation_ids(result)
            )
            if any(
                warning.code == "WINDOW_TRUNCATED"
                for warning in contract.warnings
            ):
                truncated_symbols.append(symbol)

        unique_scopes = set(scope_by_symbol.values())
        unique_observation_scopes = set(
            observation_scope_by_symbol.values()
        )
        ranking_scope = None
        ranking_observation_ids = ()
        ranking_error = None
        if normalized_errors:
            ranking_error = (
                "INCOMPLETE_SYMBOL_COVERAGE: one or more requested symbols "
                "did not produce a rankable SUCCESS result"
            )
        elif truncated_symbols:
            ordered = sorted(truncated_symbols)
            sample = ",".join(ordered[:10])
            ranking_error = (
                "REQUESTED_WINDOW_TRUNCATED: child results do not cover the "
                f"complete requested window; symbols_sample={sample}; "
                f"symbol_count={len(ordered)}"
            )
        elif len(unique_observation_scopes) != 1:
            ordered = sorted(observation_scope_by_symbol)
            details = "; ".join(
                f"{symbol}={_ordered_text_hash(observation_scope_by_symbol[symbol])}"
                for symbol in ordered[:10]
            )
            ranking_error = (
                "INCONSISTENT_CHILD_SCOPE: SUCCESS child results have "
                "different ordered observation-date scopes; "
                f"sha256_sample={details}; symbol_count={len(ordered)}"
            )
        elif len(unique_scopes) != 1:
            ranking_error = (
                "INCONSISTENT_CHILD_SCOPE: identical observation dates have "
                "inconsistent actual interval/coverage metadata"
            )
        else:
            ranking_scope = next(iter(unique_scopes))
            ranking_observation_ids = next(iter(unique_observation_scopes))

        if ranking_error is not None:
            return ParallelResult(
                start_date=self.start_date,
                end_date=self.end_date,
                initial_cash=self.initial_cash,
                total_symbols=len(self.symbols),
                success_count=success_count,
                failed_count=failed_count,
                avg_return=0.0,
                avg_annual_return=0.0,
                avg_max_drawdown=0.0,
                avg_sharpe=None,
                avg_win_rate=None,
                universe_assumptions=self.universe_assumptions,
                universe_warnings=self.universe_warnings,
                results=accepted,
                errors=normalized_errors,
                ranking_error=ranking_error,
                requested_symbols=tuple(self.symbols),
            )

        # 计算平均指标
        returns = [result.total_return for result in accepted.values()]
        annual_returns = [
            result.annual_return for result in accepted.values()
        ]
        max_drawdowns = [
            result.max_drawdown for result in accepted.values()
        ]
        sharpes = [
            result.sharpe_ratio
            for result in accepted.values()
            if result.sharpe_ratio is not None
        ]
        win_rates = [
            result.win_rate
            for result in accepted.values()
            if result.win_rate is not None
        ]

        avg_return = sum(returns) / len(returns)
        avg_annual_return = sum(annual_returns) / len(annual_returns)
        avg_max_drawdown = sum(max_drawdowns) / len(max_drawdowns)
        avg_sharpe = sum(sharpes) / len(sharpes) if sharpes else None
        avg_win_rate = sum(win_rates) / len(win_rates) if win_rates else None

        # 排序获取 Top/Worst
        sorted_results = sorted(
            accepted.items(),
            key=lambda item: item[1].total_return,
            reverse=True,
        )

        top_performers = [
            {
                'symbol': symbol,
                'return': round(result.total_return * 100, 2),
                'sharpe': result.sharpe_ratio,
            }
            for symbol, result in sorted_results[:10]
        ]
        worst_performers = [
            {
                'symbol': symbol,
                'return': round(result.total_return * 100, 2),
                'sharpe': result.sharpe_ratio,
            }
            for symbol, result in sorted_results[-10:]
        ]

        return ParallelResult(
            start_date=self.start_date,
            end_date=self.end_date,
            initial_cash=self.initial_cash,
            total_symbols=len(self.symbols),
            success_count=success_count,
            failed_count=failed_count,
            avg_return=avg_return,
            avg_annual_return=avg_annual_return,
            avg_max_drawdown=avg_max_drawdown,
            avg_sharpe=avg_sharpe,
            avg_win_rate=avg_win_rate,
            universe_assumptions=self.universe_assumptions,
            universe_warnings=self.universe_warnings,
            top_performers=top_performers,
            worst_performers=worst_performers,
            results=accepted,
            errors=normalized_errors,
            ranking_scope=ranking_scope,
            ranking_observation_ids=ranking_observation_ids,
            requested_symbols=tuple(self.symbols),
        )
