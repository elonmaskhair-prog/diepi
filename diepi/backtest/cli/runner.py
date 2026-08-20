"""
CLI 回测运行器

提供命令行回测的核心功能
"""

import os
import sys
import json
import hashlib
import re
import stat
import tempfile
import time
import contextlib
import traceback
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, Callable

import pandas as pd
from diepi.artifacts import (
    ArtifactPayload,
    ArtifactStore,
    EngineKind,
    ErrorCategory,
    RunError,
    RunOutcome,
    RunProvenance,
    SourceFingerprint,
)
from .. import __version__
from ..config import DATA_ROOT as DEFAULT_DATA_ROOT, RESULTS_DIR
from ..engine import PortfolioEngine
from ..strategy import BarData, PortfolioBarData, PortfolioStrategy, Strategy
from ..data.stock_pool import PoolSource
from ..data.source_evidence import (
    DynamicMarketDataFingerprintTracker,
    collect_market_data_fingerprints,
    collect_trade_calendar_fingerprint,
    require_complete_direct_sources,
)
from ..data.dataset_manifest import DatasetManifest, identify_parquet_file
from ..data.plain_files import DATASET_MANIFEST_MAX_BYTES, read_plain_bytes
from ..liquidity import build_daily_auction_liquidity_policy
from ..result_contract import ResultContract, ResultReason, ResultStatus
from .signal_input import SignalReplayInput


# 默认配置
DEFAULT_START_DATE = '20130101'
DEFAULT_END_DATE = None  # None = 运行当天（此前冻结为拷贝时点的日历值，会静默截断数据）
DEFAULT_INITIAL_CASH = 100_000_000
# CLI and GUI share one output root.  Installed wheels default to the current
# working directory; source checkouts default to the repository root.
DEFAULT_OUTPUT_DIR = Path(RESULTS_DIR)


_WINDOWS_RESERVED_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{i}' for i in range(1, 10)),
    *(f'LPT{i}' for i in range(1, 10)),
}


def _replay_input_console_summary(
    frozen_signals: Optional[SignalReplayInput],
    combo_bundle: Any,
) -> Optional[str]:
    """Return a bounded, path-private summary for built-in replay inputs.

    Replay objects intentionally retain every normalized instruction and their
    source bytes.  Interpolating either dataclass into the default console log
    can therefore disclose the complete research input and produce an
    unbounded line.  The artifact still preserves the exact bytes; this line is
    only a human-facing execution summary.
    """

    if frozen_signals is not None:
        return (
            "signals "
            f"format={frozen_signals.signal_format} "
            f"rows={len(frozen_signals.instructions)} "
            f"dates={len(frozen_signals.dates)}"
            f"({frozen_signals.dates[0]}..{frozen_signals.dates[-1]}) "
            f"symbols={len(frozen_signals.symbols)} "
            f"sha256={frozen_signals.sha256[:12]}"
        )
    if combo_bundle is not None:
        manifest_sha256 = hashlib.sha256(combo_bundle.manifest_bytes()).hexdigest()
        return (
            "combo "
            f"targets={combo_bundle.target_rows} "
            f"close_sells={combo_bundle.close_sell_rows} "
            f"daily={combo_bundle.daily_rows} "
            f"dates={combo_bundle.start_date}..{combo_bundle.end_date} "
            f"symbols={len(combo_bundle.symbols)} "
            f"manifest_sha256={manifest_sha256[:12]}"
        )
    return None


def _validate_index_lock_stat(info: Any, label: str) -> None:
    """Reject lock nodes that could alias or redirect writes outside results."""

    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label}必须是普通文件")
    if bool(
        getattr(info, 'st_file_attributes', 0)
        & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)
    ):
        raise ValueError(f"{label}不能是链接或重解析点")
    if getattr(info, 'st_nlink', 1) != 1:
        raise ValueError(f"{label}不能是硬链接")


def _validate_open_index_lock(lock_path: Path, handle) -> None:
    """Require the path and opened descriptor to identify the same safe file."""

    path_info = os.lstat(lock_path)
    opened_info = os.fstat(handle.fileno())
    _validate_index_lock_stat(path_info, "结果索引锁")
    _validate_index_lock_stat(opened_info, "已打开的结果索引锁")
    if (path_info.st_dev, path_info.st_ino) != (
        opened_info.st_dev,
        opened_info.st_ino,
    ):
        raise ValueError("结果索引锁路径在打开期间发生变化")


def _validate_run_name(value: str) -> str:
    """Require one portable path component for an immutable run directory."""
    if type(value) is not str or not value:
        raise ValueError("run_name 必须是非空字符串")
    if value != value.strip() or value.endswith(('.', ' ')):
        raise ValueError("run_name 不能包含首尾空白或以点结尾")
    if value in {'.', '..'} or re.search(r'[<>:"/\\|?*\x00-\x1f]', value):
        raise ValueError("run_name 必须是安全的单一路径名称，不能含路径分隔符或保留字符")
    if value.split('.', 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise ValueError("run_name 不能使用系统保留名称")
    if value.casefold() == 'index.csv':
        raise ValueError("run_name 不能占用结果目录的 index.csv 索引文件")
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', value):
        raise ValueError(
            "run_name 只能包含 1-128 个 ASCII 字母、数字、点、下划线或连字符，"
            "且首字符必须是字母或数字"
        )
    return value


@contextlib.contextmanager
def _index_lock(output_dir: Path):
    """Serialize the index read/modify/replace transaction across processes."""
    lock_path = output_dir / '.index.csv.lock'
    try:
        existing_info = os.lstat(lock_path)
    except FileNotFoundError:
        existing_info = None
    if existing_info is not None:
        _validate_index_lock_stat(existing_info, "结果索引锁")

    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, 'O_BINARY', 0)
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        handle = os.fdopen(descriptor, 'r+b')
    except Exception:
        os.close(descriptor)
        raise
    locked = False
    try:
        _validate_open_index_lock(lock_path, handle)
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - exercised by the Linux CI matrix
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True
        _validate_open_index_lock(lock_path, handle)
        yield
    finally:
        try:
            if locked:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - exercised by the Linux CI matrix
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _read_index(index_file: Path) -> pd.DataFrame:
    if not index_file.exists():
        return pd.DataFrame()
    frame = pd.read_csv(index_file, dtype={'run_id': str})
    if 'run_id' not in frame.columns:
        raise ValueError("index.csv 缺少 run_id 列")
    return frame


def _ensure_run_id_available(output_dir: Path, run_name: str) -> None:
    index_file = output_dir / 'index.csv'
    with _index_lock(output_dir):
        frame = _read_index(index_file)
        if not frame.empty and run_name in set(frame['run_id']):
            raise FileExistsError(f"运行名称已存在于 index.csv: {run_name}")


def _build_equity_curve_export(
        daily_values: pd.DataFrame, initial_cash: float) -> pd.DataFrame:
    """Project engine-owned return/drawdown vectors without recomputation."""
    df = daily_values.copy()
    if 'date' not in df.columns:
        df = df.reset_index()
    df['date'] = df['date'].astype(str).str.replace('-', '', regex=False)
    df['nav'] = df['total_value'] / initial_cash
    if 'drawdown_close_nav' in df.columns:
        df['drawdown'] = df['drawdown_close_nav']
    if 'intraday_low_total_value' in df.columns:
        df['intraday_low_nav'] = (
            df['intraday_low_total_value'] / initial_cash)
    if 'drawdown_intraday_low_nav' in df.columns:
        df['intraday_low_drawdown'] = df['drawdown_intraday_low_nav']

    export_columns = [
        'date', 'nav', 'total_value', 'cash', 'market_value',
        'daily_return', 'drawdown_close_nav',
        'drawdown_intraday_low_nav', 'drawdown_intraday_high_to_low',
        'drawdown',
    ]
    rename_map = {'market_value': 'stock_value'}
    for source, target in (
        ('intraday_high_market_value', 'intraday_high_stock_value'),
        ('intraday_high_total_value', 'intraday_high_total_value'),
        ('intraday_low_market_value', 'intraday_low_stock_value'),
        ('intraday_low_total_value', 'intraday_low_total_value'),
        ('intraday_low_nav', 'intraday_low_nav'),
        ('intraday_low_drawdown', 'intraday_low_drawdown'),
    ):
        if source in df.columns:
            export_columns.append(source)
            rename_map[source] = target
    return df[[name for name in export_columns if name in df.columns]].rename(
        columns=rename_map)


def _csv_bytes(frame: pd.DataFrame, *, bom: bool = False) -> bytes:
    """Serialize a compatibility CSV in memory before atomic publication."""
    encoding = 'utf-8-sig' if bom else 'utf-8'
    return frame.to_csv(index=False).encode(encoding)


def _source_fingerprints(
    data_root,
    *,
    symbols=None,
    price_mode: str = 'dual',
    frequency: str = 'daily',
    start_date: str = None,
    end_date: str = None,
    verify_manifest_members: bool = True,
) -> tuple:
    """Record dataset and direct market-file identities without leaking paths."""
    if data_root is None:
        return ()
    root = Path(data_root).resolve()
    manifest = root / 'diepi_dataset.json'
    sources = []
    contract = None
    if os.path.lexists(manifest):
        try:
            manifest_bytes = read_plain_bytes(
                manifest,
                root=root,
                max_bytes=DATASET_MANIFEST_MAX_BYTES,
                label="dataset manifest",
            )
            contract = DatasetManifest.from_dict(
                json.loads(manifest_bytes.decode('utf-8'))
            )
        except Exception as exc:
            raise OSError(
                "DATASET_MANIFEST_VERIFICATION_FAILED: the dataset manifest "
                "could not be verified"
            ) from exc
        sources.append(SourceFingerprint.from_bytes(
            kind='dataset_manifest',
            logical_path='diepi_dataset.json',
            payload=manifest_bytes,
        ))
    sources.append(collect_trade_calendar_fingerprint(root))
    scope_symbols = None if symbols is None else tuple(symbols)
    market_sources = collect_market_data_fingerprints(
        root,
        symbols=scope_symbols,
        price_mode=price_mode,
        frequency=frequency,
        start_date=start_date,
        end_date=end_date,
    )
    if scope_symbols is not None:
        for symbol in scope_symbols:
            try:
                require_complete_direct_sources(
                    symbol,
                    price_mode,
                    market_sources,
                    frequency=frequency,
                )
            except ValueError as exc:
                raise OSError(str(exc)) from exc
    sources.extend(market_sources)
    if contract is not None and verify_manifest_members:
        for expected in contract.files:
            # Validate every logical identity once before execution.  The
            # post-run snapshot skips this full-table decode and relies on the
            # exact byte fingerprints above for runtime-reachable inputs.
            try:
                actual = identify_parquet_file(root, expected.path)
            except Exception as exc:
                raise OSError(
                    "DATASET_MANIFEST_VERIFICATION_FAILED: a declared dataset "
                    "member could not be verified"
                ) from exc
            if actual != expected:
                raise OSError(
                    "DATASET_MANIFEST_MEMBER_CHANGED: a declared dataset "
                    "member no longer matches its logical identity"
                )
    return tuple(sorted(
        sources, key=lambda source: (source.kind, source.logical_path)
    ))


def _failed_run_outcome(error: BaseException, engine, phase: str) -> RunOutcome:
    """Build a stable failed envelope for exceptions raised before a result."""
    contract = getattr(engine, 'last_result_contract', None)
    if type(contract) is not ResultContract or contract.status is not ResultStatus.FAILED:
        contract = ResultContract(
            status=ResultStatus.FAILED,
            reason=ResultReason(
                'RUN_FAILED',
                'The CLI run raised an exception before producing a result',
            ),
        )
    category = ErrorCategory.ENGINE
    if isinstance(error, (FileNotFoundError, OSError)):
        category = ErrorCategory.DATA
    elif phase == 'input':
        category = ErrorCategory.INPUT
    elif phase == 'strategy_compile' or isinstance(error, SyntaxError):
        category = ErrorCategory.STRATEGY
    elif phase == 'artifact_save':
        category = ErrorCategory.SYSTEM
    message = str(error).strip() or type(error).__name__
    return RunOutcome(
        engine_kind=EngineKind.CASH_PORTFOLIO,
        result_contract=contract,
        result_role=None,
        error=RunError(
            code='RUN_FAILED',
            category=category,
            phase=phase,
            exception_type=type(error).__name__,
            message=message,
            traceback_role='traceback',
        ),
    )


_TERMINAL_ASSUMPTION_KEYS = (
    "execution.frequency",
    "execution.strategy_price_mode",
    "execution.execution_price_mode",
    "execution.commission_rate",
    "execution.stamp_duty_policy",
    "execution.transfer_fee_rate",
    "execution.slippage_rate",
    "execution.liquidity_cap_ratio",
    "liquidity.daily_open_cap",
    "liquidity.daily_close_cap",
    "corporate_action.adjustment_factor_model",
    "metrics.trading_days_per_year",
    "metrics.risk_free_rate",
)


def _result_contract_lines(contract) -> list[str]:
    """Build the human trust summary shown before performance metrics."""
    if contract is None:
        return [
            "结果状态: LEGACY_UNCLASSIFIED",
            "可排名: 否（缺少 ResultContract）",
        ]

    lines = [
        f"结果状态:     {contract.status.value}",
        f"可排名:       {'是' if contract.is_rankable else '否'}",
    ]
    if contract.actual_interval is not None:
        lines.append(
            "实际区间:     "
            f"{contract.actual_interval.start_date} ~ "
            f"{contract.actual_interval.end_date}"
        )
    if contract.data_coverage is not None:
        coverage = contract.data_coverage
        lines.append(
            "数据覆盖:     "
            f"{coverage.actual_observations}/"
            f"{coverage.expected_observations} "
            f"({coverage.ratio:.2%})"
        )
    if contract.reason is not None:
        lines.append(
            f"终止原因:     {contract.reason.code}: {contract.reason.message}"
        )
    if contract.warnings:
        lines.append("警告:")
        lines.extend(
            f"  - {warning.code}: {warning.message}"
            for warning in contract.warnings
        )
    else:
        lines.append("警告:         无")

    assumptions = {item.key: item.value for item in contract.assumptions}
    selected = [
        (key, assumptions[key])
        for key in _TERMINAL_ASSUMPTION_KEYS
        if key in assumptions
    ]
    if selected:
        lines.append("关键假设:")
        lines.extend(f"  - {key}={value}" for key, value in selected)
    remaining = len(assumptions) - len(selected)
    if remaining > 0:
        lines.append(f"  - 其余 {remaining} 项见 summary.json/result artifact")
    return lines


def compile_strategy(
    code: str,
    strategy_params: Optional[Dict[str, Any]] = None,
    *,
    strategy_kind: str = 'portfolio',
):
    """
    编译策略代码

    Args:
        code: 策略代码字符串
        strategy_params: 策略参数覆盖 {模块级变量名: 值}。在用户代码执行后
            注入模块命名空间，因此策略回调在运行时读取的模块级常量会被覆盖；
            注意：在 import 时刻已经用旧值算好的派生常量不会自动更新。
        strategy_kind: ``portfolio`` 使用 PortfolioStrategy；``single``
            使用 Strategy。默认保持 ``portfolio``，兼容现有 CLI 策略。

    Returns:
        策略类
    """
    if strategy_kind not in {'portfolio', 'single'}:
        raise ValueError(
            "strategy_kind 必须是 'portfolio' 或 'single'"
        )

    strategy_base = (
        PortfolioStrategy if strategy_kind == 'portfolio' else Strategy
    )
    strategy_base_name = strategy_base.__name__

    # 两种类型都显式暴露在执行环境中，用户可以省略 import；最终只接受
    # 与 strategy_kind 匹配的子类，避免组合/单标的契约被静默混用。
    local_vars = {
        'Strategy': Strategy,
        'BarData': BarData,
        'PortfolioStrategy': PortfolioStrategy,
        'PortfolioBarData': PortfolioBarData,
    }

    # 执行用户代码
    exec(code, local_vars)

    # 注入策略参数覆盖（在用户代码执行后，保证覆盖模块级默认值）
    if strategy_params:
        for key in strategy_params:
            if key not in local_vars:
                print(
                    f"警告: --param {key} 在策略文件中不存在同名模块级变量，"
                    f"注入后策略代码可能不会读取它（请检查是否拼写错误）",
                    file=sys.stderr,
                )
        local_vars.update(strategy_params)

    # 构建动态策略类
    class DynamicStrategy(strategy_base):
        pass

    # 注入用户定义的方法
    method_names = [
        'on_init', 'on_before_market_open', 'on_after_open', 'on_day',
        'on_minute', 'on_before_close', 'on_after_market_close', 'on_finish'
    ]

    injected = 0
    for name in method_names:
        if name in local_vars and callable(local_vars[name]):
            func = local_vars[name]
            setattr(DynamicStrategy, name, lambda self, *args, func=func, **kwargs: func(*args, **kwargs))
            injected += 1

    if injected > 0:
        # 函数+类混合文件：函数式回调优先，类会被忽略——显式告警而非静默
        ignored = list(dict.fromkeys(
            v.__name__ for v in local_vars.values()
            if isinstance(v, type) and issubclass(v, strategy_base)
            and v is not strategy_base
        ))
        if ignored:
            print(
                f"警告: 策略文件同时包含模块级回调函数与策略类 {ignored}，"
                f"将使用模块级函数、忽略类。如需使用类请删除同名模块级回调。",
                file=sys.stderr,
            )

    if injected == 0:
        # 历史P1：类式策略文件经 CLI 运行零回调命中 → 静默 no-op（0交易/0收益/exit 0）。
        # 兜底1：文件里恰好定义了一个匹配当前契约的子类 → 直接使用；
        # 兜底2：否则显式报错，告诉用户 CLI 需要模块级函数式写法。
        subclasses = list(dict.fromkeys(
            v for v in local_vars.values()
            if isinstance(v, type) and issubclass(v, strategy_base)
            and v is not strategy_base
        ))
        if len(subclasses) == 1:
            return subclasses[0]
        raise ValueError(
            f"{strategy_kind} 策略文件中没有找到任何模块级回调函数"
            f"（{'/'.join(method_names)}）"
            + (f"，且存在多个 {strategy_base_name} 子类无法自动选择"
               if len(subclasses) > 1 else "")
            + f"。请使用模块级函数式写法，或定义唯一的 {strategy_base_name} 子类；"
              f"当前 strategy_kind={strategy_kind!r}。"
        )

    return DynamicStrategy


def run_backtest(
    strategy_file: str,
    start_date: str = DEFAULT_START_DATE,
    end_date: str = DEFAULT_END_DATE,
    initial_cash: float = DEFAULT_INITIAL_CASH,
    output_dir: Optional[Path] = None,
    run_name: Optional[str] = None,
    freq: str = 'daily',
    slippage: float = 0.001,
    commission: float = 0.00025,
    open_buy_resize_mode: str = 'auto',
    pool_symbols: Optional[list] = None,
    stamp_duty: float = 0.001,
    min_commission: float = 5.0,
    lot_size: int = 100,
    liquidity_cap_ratio: float = 0.8,
    daily_open_cap_yuan: Optional[float] = None,
    daily_close_cap_yuan: Optional[float] = None,
    daily_open_previous_day_ratio: Optional[float] = None,
    daily_close_previous_day_ratio: Optional[float] = None,
    limit_pct_overrides: Optional[Dict[str, float]] = None,
    strategy_params: Optional[Dict[str, Any]] = None,
    open_buy_fill_mode: str = 'open+slip',
    open_buy_sizing: str = 'limit_up',
    t0_overrides=None,
    trading_days_per_year: int = 252,
    risk_free_rate: float = 0.03,
    price_mode: str = 'dual',
    verbose: bool = True,
    transfer_fee_rate: float = 0.0,
    data_root=None,
    stop_check: Optional[Callable[[], bool]] = None,
) -> Dict[str, Any]:
    """
    运行回测

    Args:
        strategy_file: 策略文件路径
        start_date: 开始日期 (YYYYMMDD)
        end_date: 结束日期 (YYYYMMDD)
        initial_cash: 初始资金
        output_dir: 结果保存目录
        run_name: 运行名称（默认自动生成）
        freq: 回测频率 ('daily' 或 'minute')
        slippage: 滑点
        commission: 佣金率
        stamp_duty: 印花税率 (仅卖出；ETF 应设 0)
        transfer_fee_rate: 双边过户费率 (默认0；不做历史自动切换)
        min_commission: 单笔最低佣金 (默认5元)
        lot_size: 每手股数 (默认100)
        liquidity_cap_ratio: 单根K线最大可吃成交额比例 (默认0.8)
        limit_pct_overrides: 涨跌停幅度覆盖表 {代码: 幅度}
        strategy_params: 策略模块级参数覆盖 {变量名: 值}
        verbose: 是否输出详细信息
        stop_check: 可选协作取消回调；返回 True 时引擎发布规范 CANCELED 工件

    Returns:
        回测结果字典
    """
    start_time = time.time()
    # Freeze the same default root that CacheManager would otherwise resolve
    # implicitly. Passing ``None`` through to the engine and provenance
    # collector would let the engine read prices while source verification
    # recorded an empty tuple.
    data_root = Path(
        DEFAULT_DATA_ROOT if data_root is None else data_root
    ).expanduser().resolve()

    # 结束日期默认今天
    if end_date is None:
        end_date = datetime.now().strftime('%Y%m%d')

    # 解析策略文件路径
    strategy_path = Path(strategy_file)
    if not strategy_path.is_absolute():
        strategy_path = Path.cwd() / strategy_path

    if not strategy_path.exists():
        raise FileNotFoundError(f"策略文件不存在: {strategy_path}")

    strategy_name = strategy_path.stem

    # 设置输出目录
    if output_dir is None:
        output_dir = DEFAULT_OUTPUT_DIR
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 生成运行名称
    if run_name is None:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        run_name = f"{strategy_name}_{timestamp}"
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', run_name):
            run_name = f"run_{timestamp}"
    run_name = _validate_run_name(run_name)
    _ensure_run_id_available(output_dir, run_name)

    run_dir = output_dir / run_name
    if os.path.lexists(str(run_dir)):
        raise FileExistsError(f"运行目录已存在，拒绝覆盖: {run_dir}")

    # The final directory is published only by ArtifactStore after every
    # payload, hash and adapter invariant has passed.  Keep all pre-publication
    # evidence in memory so a half-written run can never look complete.
    code = None
    strategy_params = dict(strategy_params or {})
    has_signal_input = 'SIGNALS_INPUT' in strategy_params
    has_combo_input = 'COMBO_BUNDLE' in strategy_params
    if has_signal_input and has_combo_input:
        raise ValueError('SIGNALS_INPUT 与 COMBO_BUNDLE 不能同时使用')
    frozen_signals = strategy_params.get('SIGNALS_INPUT')
    if has_signal_input and type(frozen_signals) is not SignalReplayInput:
        raise TypeError('SIGNALS_INPUT 必须是经过验证的 SignalReplayInput')
    if has_signal_input:
        frozen_signals = frozen_signals.revalidated()
        strategy_params['SIGNALS_INPUT'] = frozen_signals
    if 'SIGNALS_FILE' in strategy_params:
        raise ValueError(
            'SIGNALS_FILE 路径注入不再作为执行边界；请先调用 '
            'load_signal_replay_input() 并传入 SIGNALS_INPUT'
        )
    combo_bundle = strategy_params.get('COMBO_BUNDLE')
    if has_combo_input:
        from .combo_bundle import ComboReplayBundle

        if type(combo_bundle) is not ComboReplayBundle:
            raise TypeError('COMBO_BUNDLE 必须是经过验证的 ComboReplayBundle')
        combo_bundle = combo_bundle.revalidated()
        strategy_params['COMBO_BUNDLE'] = combo_bundle

    if frozen_signals is not None:
        if set(strategy_params) != {'SIGNALS_INPUT'}:
            raise ValueError(
                'signal replay runtime parameters must contain exactly '
                'SIGNALS_INPUT'
            )
        expected_strategy = Path(__file__).with_name(
            'signal_replay_template.py'
        ).resolve(strict=True)
        if strategy_path.resolve(strict=True) != expected_strategy:
            raise ValueError(
                'SIGNALS_INPUT 只能由内置 signal replay template 执行'
            )
        # Continue from the trusted canonical path, not a caller-controlled
        # symlink that could be replaced after the identity check.
        strategy_path = expected_strategy
        strategy_name = expected_strategy.stem
        expected_symbols = frozen_signals.symbols
    elif combo_bundle is not None:
        if set(strategy_params) != {'COMBO_BUNDLE'}:
            raise ValueError(
                'combo replay runtime parameters must contain exactly '
                'COMBO_BUNDLE'
            )
        expected_strategy = Path(__file__).with_name(
            'combo_replay_template.py'
        ).resolve(strict=True)
        if strategy_path.resolve(strict=True) != expected_strategy:
            raise ValueError(
                'COMBO_BUNDLE 只能由内置 combo replay template 执行'
            )
        strategy_path = expected_strategy
        strategy_name = expected_strategy.stem
        combo_bundle.validate_requested_scope(start_date, end_date)
        expected_symbols = combo_bundle.symbols
    else:
        expected_symbols = None

    if expected_symbols is not None:
        if pool_symbols is None:
            pool_symbols = list(expected_symbols)
        else:
            normalized_pool = tuple(sorted(pool_symbols))
            if (
                len(normalized_pool) != len(set(normalized_pool))
                or normalized_pool != expected_symbols
            ):
                raise ValueError(
                    'replay pool_symbols 必须与冻结输入 scope 精确一致'
                )
        # Never retain a caller-owned mutable/list-subclass scope after the
        # check; engine construction and Artifact config use this generation.
        pool_symbols = list(expected_symbols)
    signals_bytes = (
        frozen_signals.source_bytes if frozen_signals is not None else None
    )
    compat_signal_name = (
        frozen_signals.source_name if frozen_signals is not None else None
    )
    combo_payloads = ()
    combo_fingerprints = ()
    pre_run_source_fingerprints = ()
    stable_source_fingerprints = ()
    dynamic_fingerprint_tracker = None
    engine = None
    phase = 'input'
    input_mode = (
        'signals' if frozen_signals is not None
        else ('combo' if combo_bundle is not None else 'strategy')
    )
    artifact_config = {
        'command': 'run',
        'engine_kind': EngineKind.CASH_PORTFOLIO.value,
        'input_mode': input_mode,
        'strategy_name': strategy_name,
        'strategy_file': 'inputs/strategy.py',
        'requested_start_date': start_date,
        'requested_end_date': end_date,
        'parameters': {
            'initial_cash': initial_cash,
            'freq': freq,
            'slippage': slippage,
            'commission': commission,
            'stamp_duty': stamp_duty,
            'transfer_fee_rate': transfer_fee_rate,
            'min_commission': min_commission,
            'lot_size': lot_size,
            'liquidity_cap_ratio': liquidity_cap_ratio,
            'daily_open_cap_yuan': daily_open_cap_yuan,
            'daily_close_cap_yuan': daily_close_cap_yuan,
            'daily_open_previous_day_ratio': daily_open_previous_day_ratio,
            'daily_close_previous_day_ratio': daily_close_previous_day_ratio,
            'limit_pct_overrides': limit_pct_overrides,
            'open_buy_resize_mode': open_buy_resize_mode,
            'open_buy_fill_mode': open_buy_fill_mode,
            'open_buy_sizing': open_buy_sizing,
            't0_overrides': sorted(t0_overrides) if t0_overrides else None,
            'trading_days_per_year': trading_days_per_year,
            'risk_free_rate': risk_free_rate,
            'price_mode': price_mode,
            'pool_symbols': pool_symbols,
            'strategy_params': dict(strategy_params),
        },
    }
    if frozen_signals is not None:
        signal_params = artifact_config['parameters']['strategy_params']
        signal_params['SIGNALS_INPUT'] = 'inputs/signals.csv'
        signal_params['SIGNALS_FORMAT'] = frozen_signals.signal_format
        signal_params['SIGNALS_MANIFEST'] = frozen_signals.manifest_dict()
    if 'COMBO_BUNDLE' in artifact_config['parameters']['strategy_params']:
        from .combo_bundle import ComboReplayBundle

        combo_bundle = strategy_params.get('COMBO_BUNDLE')
        if type(combo_bundle) is not ComboReplayBundle:
            raise TypeError('COMBO_BUNDLE 必须是经过验证的 ComboReplayBundle')
        artifact_config['parameters']['strategy_params']['COMBO_BUNDLE'] = (
            'inputs/combo/diepi_combo.json'
        )
        combo_payloads = tuple(
            ArtifactPayload(
                role=role,
                path=path,
                media_type=(
                    'application/json'
                    if path.endswith('.json')
                    else 'text/csv; charset=utf-8'
                ),
                data=payload,
            )
            for role, path, payload in combo_bundle.artifact_inputs()
        )
        combo_fingerprints = tuple(
            SourceFingerprint.from_bytes(
                kind='combo_input',
                logical_path=path,
                payload=payload,
            )
            for _, path, payload in combo_bundle.artifact_inputs()
        )

    if verbose:
        print("=" * 60)
        print(f"开始回测: {strategy_name}")
        print("=" * 60)
        print(f"策略文件: {strategy_path}")
        print(f"日期范围: {start_date} ~ {end_date}")
        print(f"初始资金: {initial_cash:,.0f}")
        print(f"回测频率: {freq}")
        print("-" * 60)

    try:
        # 读取策略代码
        with open(strategy_path, 'r', encoding='utf-8') as f:
            code = f.read()

        if compat_signal_name is not None:
            snap_name = compat_signal_name
            reserved = {
                'strategy.py', 'summary.json', 'orders.csv',
                'equity_curve.csv', 'positions.csv', 'trades.csv',
                'index.csv', 'manifest.json', 'config.json', 'result.json',
                'provenance.json', 'inputs', 'tables', 'evidence',
            }
            if snap_name.casefold() in reserved:
                snap_name = 'signals_' + snap_name
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', snap_name):
                snap_name = 'signals.csv'
            compat_signal_name = snap_name

        # 编译策略
        phase = 'strategy_compile'
        # 静默模式下把策略代码的 print 重定向到 stderr，保证 stdout 是纯 JSON（agent 可直接解析）
        quiet_redirect = (
            contextlib.redirect_stdout(sys.stderr) if not verbose
            else contextlib.nullcontext()
        )

        if verbose:
            print("编译策略...")
            replay_summary = _replay_input_console_summary(
                frozen_signals, combo_bundle
            )
            if replay_summary is not None:
                print(f"输入摘要: {replay_summary}")
            elif strategy_params:
                print(f"策略参数覆盖: {strategy_params}")
        with quiet_redirect:
            strategy_class = compile_strategy(code, strategy_params=strategy_params)

        # 创建回测引擎
        if verbose:
            print("创建回测引擎...")

        def progress_callback(current, total, message):
            if verbose and current % 100 == 0:
                pct = current * 100 // total if total > 0 else 0
                print(f"\r进度: {pct}% ({current}/{total}) {message}", end='', flush=True)

        pool_source = PoolSource.ALL_MARKET
        if pool_symbols:
            pool_source = PoolSource.SPECIFIED

        daily_auction_liquidity = build_daily_auction_liquidity_policy(
            open_fixed_yuan=daily_open_cap_yuan,
            close_fixed_yuan=daily_close_cap_yuan,
            open_previous_day_ratio=daily_open_previous_day_ratio,
            close_previous_day_ratio=daily_close_previous_day_ratio,
        )

        # 价格模式: dual=策略hfq/撮合raw双轨(默认); hfq/raw=两腿同轨单轨运行
        price_mode_map = {
            'dual': {},
            'hfq': {'strategy_price_mode': 'hfq', 'execution_price_mode': 'hfq'},
            'raw': {'strategy_price_mode': 'raw', 'execution_price_mode': 'raw'},
        }
        price_mode_kwargs = price_mode_map.get(price_mode)
        if price_mode_kwargs is None:
            raise ValueError(f"未知 price_mode: {price_mode}（应为 dual/hfq/raw）")

        dynamic_fingerprint_tracker = DynamicMarketDataFingerprintTracker(
            data_root,
            configured_symbols=(
                pool_symbols if pool_source is PoolSource.SPECIFIED else ()
            ),
            price_mode=price_mode,
            frequency=freq,
            start_date=start_date,
            end_date=end_date,
            allow_incomplete_sources=(
                pool_source is not PoolSource.SPECIFIED
            ),
        )

        phase = 'engine_setup'
        engine = PortfolioEngine(
            start_date=start_date,
            end_date=end_date,
            initial_cash=initial_cash,
            freq=freq,
            pool_source=pool_source,
            pool_symbols=pool_symbols,
            slippage=slippage,
            commission_rate=commission,
            stamp_duty=stamp_duty,
            transfer_fee_rate=transfer_fee_rate,
            min_commission=min_commission,
            lot_size=lot_size,
            open_buy_resize_mode=open_buy_resize_mode,
            liquidity_cap_ratio=liquidity_cap_ratio,
            daily_auction_liquidity=daily_auction_liquidity,
            limit_pct_overrides=limit_pct_overrides,
            open_buy_fill_mode=open_buy_fill_mode,
            open_buy_sizing=open_buy_sizing,
            t0_overrides=t0_overrides,
            trading_days_per_year=trading_days_per_year,
            risk_free_rate=risk_free_rate,
            progress_callback=progress_callback,
            stop_check=stop_check,
            data_root=data_root,
            market_data_observer=(
                dynamic_fingerprint_tracker.observe
                if dynamic_fingerprint_tracker is not None
                else None
            ),
            **price_mode_kwargs,
        )

        # 创建策略实例
        strategy = strategy_class()

        # 运行回测
        if verbose:
            print("运行回测...")
        quiet_redirect_run = (
            contextlib.redirect_stdout(sys.stderr) if not verbose
            else contextlib.nullcontext()
        )
        phase = 'source_verification'
        pre_run_source_fingerprints = _source_fingerprints(
            data_root,
            symbols=pool_symbols,
            price_mode=price_mode,
            frequency=freq,
            start_date=start_date,
            end_date=end_date,
        )
        phase = 'engine_run'
        with quiet_redirect_run:
            result = engine.run(strategy)
        phase = 'data_stability_check'
        post_run_source_fingerprints = _source_fingerprints(
            data_root,
            symbols=pool_symbols,
            price_mode=price_mode,
            frequency=freq,
            start_date=start_date,
            end_date=end_date,
            verify_manifest_members=False,
        )
        if post_run_source_fingerprints != pre_run_source_fingerprints:
            raise OSError(
                "MARKET_DATA_CHANGED_DURING_RUN: explicit market-data "
                "fingerprints differ before and after engine.run"
            )
        # Artifact provenance must bind the pre-run generation that the
        # engine was allowed to read, never a first snapshot taken at save.
        dynamic_fingerprints = (
            dynamic_fingerprint_tracker.verify_stable()
            if dynamic_fingerprint_tracker is not None
            else ()
        )
        stable_source_fingerprints = tuple(sorted(
            {
                (source.kind, source.logical_path): source
                for source in (
                    pre_run_source_fingerprints + dynamic_fingerprints
                )
            }.values(),
            key=lambda source: (source.kind, source.logical_path),
        ))
        artifact_config['realized_symbols'] = list(
            getattr(engine, 'realized_market_symbols', ())
            or tuple(pool_symbols or ())
        )

        if verbose:
            print()  # 换行

        # 计算耗时
        duration = time.time() - start_time

        # 构建结果
        metrics = {
            'total_return': result.total_return,
            'annual_return': result.annual_return,
            'sharpe_ratio': result.sharpe_ratio,
            'max_drawdown': result.max_drawdown,
            'max_drawdown_close_nav': getattr(result, 'max_drawdown_close_nav', result.max_drawdown),
            'max_drawdown_intraday_low_nav': getattr(result, 'max_drawdown_intraday_low_nav', result.max_drawdown),
            'max_drawdown_intraday_high_to_low': getattr(
                result, 'max_drawdown_intraday_high_to_low', None),
            'trade_count': result.trade_count,
            'win_rate': getattr(result, 'win_rate', None),
            'final_asset': result.final_value,
            'initial_cash': initial_cash,
        }

        # 计算卡玛比率
        if result.max_drawdown > 0:
            metrics['calmar_ratio'] = result.annual_return / result.max_drawdown
        else:
            metrics['calmar_ratio'] = 0.0
        metrics['calmar_ratio_close_nav'] = metrics['calmar_ratio']
        intraday_low_mdd = metrics['max_drawdown_intraday_low_nav']
        metrics['calmar_ratio_intraday_low_nav'] = (
            result.annual_return / intraday_low_mdd
            if intraday_low_mdd > 0 else 0.0
        )
        intraday_range_mdd = metrics['max_drawdown_intraday_high_to_low']
        metrics['calmar_ratio_intraday_high_to_low'] = (
            result.annual_return / intraday_range_mdd
            if intraday_range_mdd is not None and intraday_range_mdd > 0
            else None
        )
        execution_stats = getattr(result, 'execution_stats', {}) or {}
        if isinstance(execution_stats, dict):
            metrics.update(execution_stats)

        meta = {
            'strategy_file': 'inputs/strategy.py',
            'strategy_name': strategy_name,
            'start_date': start_date,
            'end_date': end_date,
            'run_time': datetime.now().isoformat(),
            'duration_seconds': round(duration, 2),
            'diepi_version': __version__,
            # 全部生效参数留痕（可复现性：与 strategy.py 快照一起构成完整凭证）
            'parameters': {
                'initial_cash': initial_cash,
                'freq': freq,
                'slippage': slippage,
                'commission': commission,
                'stamp_duty': stamp_duty,
                'transfer_fee_rate': transfer_fee_rate,
                'min_commission': min_commission,
                'lot_size': lot_size,
                'liquidity_cap_ratio': liquidity_cap_ratio,
                'daily_auction_liquidity': (
                    dict(daily_auction_liquidity.assumptions())
                    if daily_auction_liquidity is not None else None
                ),
                'limit_pct_overrides': limit_pct_overrides,
                'open_buy_resize_mode': open_buy_resize_mode,
                'open_buy_fill_mode': open_buy_fill_mode,
                'open_buy_sizing': open_buy_sizing,
                't0_overrides': sorted(t0_overrides) if t0_overrides else None,
                'trading_days_per_year': trading_days_per_year,
                'risk_free_rate': risk_free_rate,
                'price_mode': price_mode,
                'pool_symbols': pool_symbols,
                'strategy_params': artifact_config['parameters']['strategy_params'],
            },
        }

        output = {
            'metrics': metrics,
            'execution_stats': execution_stats,
            'result_contract': (
                result.result_contract.to_dict()
                if getattr(result, 'result_contract', None) is not None
                else None
            ),
            'rankable': bool(getattr(result, 'is_rankable', False)),
            'meta': meta,
        }

        # 保存 summary.json
        # default 兜底：库调用方传 set/Path 等不可序列化参数时（如 t0_overrides
        # 传 set），不能让整场回测跑完后在落盘一步崩掉丢失全部结果。
        # numpy 标量按数值降级（str 会把 np.int64 腐蚀成字符串，复现凭证类型失真）
        def _json_default(o):
            import numpy as np
            if isinstance(o, (set, frozenset)):
                return sorted(o)
            if isinstance(o, np.integer):
                return int(o)
            if isinstance(o, np.floating):
                return float(o)
            if isinstance(o, np.ndarray):
                return o.tolist()
            return str(o)

        summary_output = dict(output)
        summary_output['artifact_verified'] = True
        summary_bytes = (
            json.dumps(
                summary_output,
                indent=2,
                ensure_ascii=False,
                default=_json_default,
                allow_nan=False,
            ) + '\n'
        ).encode('utf-8')
        compat_payloads = [
            ArtifactPayload(
                role='compat_summary',
                path='summary.json',
                media_type='application/json',
                data=summary_bytes,
            ),
            ArtifactPayload(
                role='compat_strategy_source',
                path='strategy.py',
                media_type='text/x-python; charset=utf-8',
                data=code.encode('utf-8'),
            ),
        ]
        if not result.daily_values.empty:
            compat_payloads.append(
                ArtifactPayload(
                    role='compat_equity_curve',
                    path='equity_curve.csv',
                    media_type='text/csv; charset=utf-8',
                    data=_csv_bytes(
                        _build_equity_curve_export(
                            result.daily_values, initial_cash
                        )
                    ),
                )
            )
        if getattr(result, 'trades', None):
            compat_payloads.append(
                ArtifactPayload(
                    role='compat_orders',
                    path='orders.csv',
                    media_type='text/csv; charset=utf-8',
                    data=_csv_bytes(pd.DataFrame(result.trades), bom=True),
                )
            )
        if signals_bytes is not None and compat_signal_name is not None:
            compat_payloads.append(
                ArtifactPayload(
                    role='compat_signals_source',
                    path=compat_signal_name,
                    media_type='text/csv; charset=utf-8',
                    data=signals_bytes,
                )
            )
        compat_payloads.extend(combo_payloads)

        phase = 'artifact_save'
        outcome = RunOutcome.from_result(
            result,
            engine_kind=EngineKind.CASH_PORTFOLIO,
        )
        ArtifactStore.save(
            outcome,
            run_dir,
            config=artifact_config,
            provenance=RunProvenance.from_engine(
                engine,
                sources=stable_source_fingerprints + combo_fingerprints,
            ),
            strategy_source=code,
            signals_source=signals_bytes,
            extra_payloads=tuple(compat_payloads),
        )
        output['artifact_dir'] = str(run_dir.resolve())
        output['artifact_verified'] = True

        # 更新 index.csv
        try:
            _update_index(
                output_dir,
                run_name,
                metrics,
                meta,
                rankable=output['rankable'],
                result_contract=output['result_contract'],
            )
        except Exception as index_error:
            output['index_updated'] = False
            print(
                f"警告: 结果工件已验证保存，但 index.csv 更新失败: {index_error}",
                file=sys.stderr,
            )
        else:
            output['index_updated'] = bool(output['rankable'])

        # 输出结果
        if verbose:
            print("=" * 60)
            print(f"回测完成: {strategy_name}")
            print("=" * 60)
            for line in _result_contract_lines(
                    getattr(result, 'result_contract', None)):
                print(line)
            print("-" * 60)
            print(f"总收益率:     {metrics['total_return'] * 100:.2f}%")
            print(f"年化收益率:   {metrics['annual_return'] * 100:.2f}%")
            sharpe_text = (
                "N/A" if metrics['sharpe_ratio'] is None
                else f"{metrics['sharpe_ratio']:.3f}"
            )
            print(f"夏普比率:     {sharpe_text}")
            print(f"最大回撤:     {metrics['max_drawdown'] * 100:.2f}%")
            print(f"卡玛比率:     {metrics['calmar_ratio']:.3f}")
            print(f"交易次数:     {metrics['trade_count']}")
            print(f"最终资产:     {metrics['final_asset']:,.0f}")
            print("=" * 60)
            print(f"结果已保存: {run_dir}")
            print(f"耗时: {duration:.1f}秒")

        return output

    except Exception as e:
        traceback_text = traceback.format_exc()
        diagnostic_path = None
        # A failed run is also an immutable artifact.  If publication itself
        # was the failure, a second best-effort publication is attempted but
        # the original exception is always preserved.
        if not os.path.lexists(str(run_dir)):
            try:
                failed_outcome = _failed_run_outcome(e, engine, phase)
                failed_summary = {
                    'artifact_verified': True,
                    'error': failed_outcome.error.to_dict(),
                    'meta': {
                        'strategy_file': 'inputs/strategy.py',
                        'strategy_name': strategy_name,
                        'start_date': start_date,
                        'end_date': end_date,
                        'diepi_version': __version__,
                    },
                    'rankable': False,
                    'result_contract': failed_outcome.result_contract.to_dict(),
                }
                failed_payloads = (
                    ArtifactPayload(
                        role='compat_summary',
                        path='summary.json',
                        media_type='application/json',
                        data=(
                            json.dumps(
                                failed_summary,
                                ensure_ascii=False,
                                sort_keys=True,
                                indent=2,
                            ) + '\n'
                        ).encode('utf-8'),
                    ),
                    ArtifactPayload(
                        role='compat_error_log',
                        path='error.log',
                        media_type='text/plain; charset=utf-8',
                        data=(f"回测失败: {e}\n\n" + traceback_text).encode('utf-8'),
                    ),
                ) + combo_payloads
                try:
                    failed_provenance = RunProvenance.from_engine(
                        engine,
                        sources=(
                            stable_source_fingerprints
                            + combo_fingerprints
                        ),
                    ) if engine is not None else RunProvenance.build(
                        sources=(
                            stable_source_fingerprints
                            + combo_fingerprints
                        )
                    )
                except Exception:
                    failed_provenance = RunProvenance.empty()
                ArtifactStore.save(
                    failed_outcome,
                    run_dir,
                    config=artifact_config,
                    provenance=failed_provenance,
                    strategy_source=code,
                    signals_source=signals_bytes,
                    traceback_text=traceback_text,
                    extra_payloads=failed_payloads,
                )
                diagnostic_path = run_dir / 'diagnostics' / 'traceback.txt'
            except Exception as artifact_error:
                print(
                    "诊断工件保存失败: "
                    f"{type(artifact_error).__name__}: {artifact_error}",
                    file=sys.stderr,
                )

        print(f"\n回测失败: {e}", file=sys.stderr)
        if diagnostic_path is not None:
            print(f"诊断工件: {diagnostic_path}", file=sys.stderr)
        raise


def _update_index(
    output_dir: Path,
    run_name: str,
    metrics: Dict,
    meta: Dict,
    *,
    rankable: bool,
    result_contract: Optional[Dict],
) -> None:
    """更新索引文件"""
    # index.csv is the rankable run catalogue.  Diagnostic artifacts for a
    # PARTIAL/INVALID/FAILED/CANCELED run remain in its run directory, but
    # must never leak into downstream comparisons through this index.
    if type(rankable) is not bool:
        raise TypeError("rankable must be exactly bool")
    if not rankable:
        return
    if type(result_contract) is not dict:
        raise TypeError("rankable result requires a result_contract object")
    status = result_contract.get('status')
    if status != 'SUCCESS':
        raise ValueError("rankable result_contract status must be SUCCESS")

    index_file = output_dir / 'index.csv'

    # 构建新行
    row = {
        'run_id': run_name,
        'strategy_name': meta['strategy_name'],
        'start_date': meta['start_date'],
        'end_date': meta['end_date'],
        'annual_return': round(metrics['annual_return'], 4),
        'sharpe_ratio': (
            None if metrics['sharpe_ratio'] is None
            else round(metrics['sharpe_ratio'], 3)
        ),
        'max_drawdown': round(metrics['max_drawdown'], 4),
        'max_drawdown_intraday_low_nav': round(metrics.get('max_drawdown_intraday_low_nav', metrics['max_drawdown']), 4),
        'calmar_ratio': round(metrics.get('calmar_ratio', 0), 3),
        'trade_count': metrics['trade_count'],
        'run_time': meta['run_time'],
        'result_status': status,
        'rankable': True,
    }

    with _index_lock(output_dir):
        df = _read_index(index_file)
        if not df.empty and run_name in set(df['run_id']):
            raise FileExistsError(
                f"index.csv 已存在运行名称，拒绝覆盖: {run_name}")
        df = (
            pd.DataFrame([row]) if df.empty
            else pd.concat([df, pd.DataFrame([row])], ignore_index=True)
        )
        descriptor, temp_name = tempfile.mkstemp(
            prefix='.index.', suffix='.tmp', dir=output_dir)
        os.close(descriptor)
        try:
            df.to_csv(temp_name, index=False)
            os.replace(temp_name, index_file)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
