"""
回测工作线程

在后台执行回测
"""

import sys
import io
import json
import math
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4
from PySide6.QtCore import QThread, Signal

from diepi.artifacts import (
    ArtifactPayload,
    ArtifactStore,
    EngineKind,
    RunProvenance,
    RunOutcome,
    SourceFingerprint,
    build_parallel_outcome,
)
from diepi.runtime import RuntimePaths

from ..engine import PortfolioEngine
from ..engine.parallel_runner import ParallelResult, ParallelRunner
from ..engine.portfolio_engine import PortfolioResult
from ..data.data_provider import DataProvider
from ..data.stock_pool import PoolSource, StockPool
from ..data.source_evidence import (
    DynamicMarketDataFingerprintTracker,
    collect_market_data_fingerprints,
    normalize_price_mode,
)
from ..liquidity import build_daily_auction_liquidity_policy


GUI_TRACEBACK_MAX_BYTES = 256 * 1024


def _resolve_independent_universe(
    pool_source: PoolSource,
    pool_symbols,
    start_date: str,
    end_date: str,
    *,
    data_root=None,
    stock_pool: StockPool = None,
):
    """Resolve a GUI independent-mode universe without current-snapshot bias."""
    if pool_symbols:
        return list(pool_symbols), {}, []
    if pool_source is not PoolSource.ALL_MARKET:
        return [], {}, []

    resolver = stock_pool or StockPool(DataProvider(data_root=data_root))
    symbols = resolver.get_pool(
        source=PoolSource.ALL_MARKET,
        start_date=start_date,
        end_date=end_date,
        exclude_st=False,
    )
    assumptions = {
        "universe.membership": "listing_interval_window_union",
        "universe.listing_interval": "list_date_inclusive_delist_date_exclusive",
        "universe.historical_st_filter": "disabled_no_point_in_time_source",
    }
    warnings = [
        "历史 ST 状态无点时数据源；独立模式全市场池包含窗口内全部上市标的，"
        "未按今天的名称倒推历史 ST。"
    ]
    return symbols, assumptions, warnings


def _independent_rankability_error(result: ParallelResult):
    """Return a fail-closed GUI message for incomplete universe coverage."""

    if result.is_rankable:
        return None
    detail = result.ranking_error or (
        "INCOMPLETE_SYMBOL_COVERAGE: result counts do not prove complete "
        "coverage"
    )
    return (
        "独立模式未完整覆盖请求股票池，结果不可排名："
        f"成功 {result.success_count}/{result.total_symbols}，"
        f"失败 {result.failed_count}；{detail}。"
        "请补齐数据或修复失败标的后重跑。"
    )


def _strategy_kind_for_mode(mode: str) -> str:
    """Map the GUI engine mode to its one valid strategy contract."""
    if mode == 'portfolio':
        return 'portfolio'
    if mode == 'independent':
        return 'single'
    raise ValueError(f"不支持的回测模式: {mode!r}")


def _gui_input_mode(config: dict) -> str:
    """Resolve and enforce the GUI's one-of-three input contract."""

    if type(config) is not dict:
        raise TypeError("GUI config must be exactly dict")
    explicit = config.get('input_mode')
    if explicit is not None and explicit not in {'strategy', 'signals', 'combo'}:
        raise ValueError(f"unsupported GUI input_mode: {explicit!r}")
    has_signals = bool(str(config.get('signals_file') or '').strip())
    has_combo = bool(str(config.get('combo_bundle') or '').strip())
    if has_signals and has_combo:
        raise ValueError("signals CSV 与 combo 不能同时配置")
    inferred = 'signals' if has_signals else ('combo' if has_combo else 'strategy')
    mode = explicit or inferred
    if mode == 'strategy' and (has_signals or has_combo):
        raise ValueError("策略代码模式不能同时携带 signals/combo 输入")
    if mode == 'signals' and (not has_signals or has_combo):
        raise ValueError("signals 模式需要且只能配置 signals_file")
    if mode == 'combo' and (not has_combo or has_signals):
        raise ValueError("combo 模式需要且只能配置 combo_bundle")
    return mode


_RESERVED_TEMPLATE_PARAMS = frozenset({
    'SIGNALS_INPUT',
    'SIGNALS_FILE',
    'COMBO_BUNDLE',
})


class _GuiRerunUnsupported(ValueError):
    """A verified result is viewable but cannot be restored without drift."""


_CLI_RUN_PARAMETER_KEYS = frozenset({
    'initial_cash',
    'freq',
    'slippage',
    'commission',
    'stamp_duty',
    'transfer_fee_rate',
    'min_commission',
    'lot_size',
    'liquidity_cap_ratio',
    'daily_open_cap_yuan',
    'daily_close_cap_yuan',
    'daily_open_previous_day_ratio',
    'daily_close_previous_day_ratio',
    'limit_pct_overrides',
    'open_buy_resize_mode',
    'open_buy_fill_mode',
    'open_buy_sizing',
    't0_overrides',
    'trading_days_per_year',
    'risk_free_rate',
    'price_mode',
    'pool_symbols',
    'strategy_params',
})


def _normalize_custom_strategy_params(value) -> dict:
    """Validate the JSON-safe scalar contract shared with CLI ``--param``."""

    if value is None:
        return {}
    if type(value) is not dict:
        raise ValueError('strategy_params must be a JSON object')
    normalized = {}
    for key, item in value.items():
        if type(key) is not str or not key.isidentifier():
            raise ValueError(
                'strategy_params keys must be valid Python identifiers'
            )
        if key in _RESERVED_TEMPLATE_PARAMS:
            raise ValueError(f'strategy_params key {key!r} is reserved')
        if type(item) is float and not math.isfinite(item):
            raise ValueError(
                f'strategy_params[{key!r}] must be a finite scalar'
            )
        if type(item) not in (bool, int, float, str):
            raise ValueError(
                f'strategy_params[{key!r}] must be bool/int/float/string'
            )
        normalized[key] = item
    return normalized


def _price_mode_engine_kwargs(price_mode: str) -> dict:
    """Map one public GUI price mode to explicit engine lanes."""

    mode = normalize_price_mode(price_mode)
    return {
        'dual': {
            'strategy_price_mode': 'hfq',
            'execution_price_mode': 'raw',
        },
        'hfq': {
            'strategy_price_mode': 'hfq',
            'execution_price_mode': 'hfq',
        },
        'raw': {
            'strategy_price_mode': 'raw',
            'execution_price_mode': 'raw',
        },
    }[mode]


def _gui_config_from_artifact(config: dict) -> dict:
    """Adapt a CLI ``run`` config to the flat GUI editor contract."""

    restored = dict(config)
    parameters = restored.get('parameters')
    if restored.get('command') != 'run' or type(parameters) is not dict:
        return restored
    unknown_parameters = sorted(set(parameters) - _CLI_RUN_PARAMETER_KEYS)
    if unknown_parameters:
        raise _GuiRerunUnsupported(
            'artifact contains execution parameters unknown to this GUI: '
            + ', '.join(unknown_parameters)
        )
    symbols = parameters.get('pool_symbols')
    realized_symbols = restored.get('realized_symbols')
    if realized_symbols is not None:
        if type(realized_symbols) is not list or any(
            type(value) is not str or not value.strip()
            for value in realized_symbols
        ):
            raise ValueError(
                'artifact realized_symbols must be a list of non-empty strings'
            )
        realized_symbols = list(dict.fromkeys(
            value.strip() for value in realized_symbols
        ))
    strategy_params = parameters.get('strategy_params')
    if strategy_params is None:
        strategy_params = {}
    if type(strategy_params) is not dict:
        raise ValueError('artifact strategy_params must be a mapping')
    has_signals = any(
        key in strategy_params
        for key in ('SIGNALS_INPUT', 'SIGNALS_FILE')
    )
    has_combo = 'COMBO_BUNDLE' in strategy_params
    if has_signals and has_combo:
        raise ValueError('artifact cannot contain both signals and combo input')
    inferred_input_mode = (
        'signals' if has_signals else ('combo' if has_combo else 'strategy')
    )
    declared_input_mode = restored.get('input_mode')
    if declared_input_mode is not None and declared_input_mode not in {
        'strategy', 'signals', 'combo'
    }:
        raise ValueError('artifact input_mode is unsupported')
    if (
        declared_input_mode is not None
        and declared_input_mode != inferred_input_mode
    ):
        raise ValueError(
            'artifact input_mode conflicts with strategy_params'
        )
    input_mode = declared_input_mode or inferred_input_mode
    if (
        'SIGNALS_INPUT' in strategy_params
        and 'SIGNALS_FILE' in strategy_params
    ):
        raise ValueError(
            'artifact cannot contain both SIGNALS_INPUT and SIGNALS_FILE'
        )
    if has_signals:
        signal_key = (
            'SIGNALS_INPUT'
            if 'SIGNALS_INPUT' in strategy_params
            else 'SIGNALS_FILE'
        )
        if strategy_params[signal_key] != 'inputs/signals.csv':
            raise ValueError(
                f'artifact {signal_key} must be exactly '
                "'inputs/signals.csv'"
            )
        signal_format = strategy_params.get('SIGNALS_FORMAT', 'auto')
        if signal_format not in {'auto', 'target', 'action'}:
            raise ValueError('artifact SIGNALS_FORMAT is unsupported')
        custom_strategy_params = None
    elif has_combo:
        if strategy_params['COMBO_BUNDLE'] != (
            'inputs/combo/diepi_combo.json'
        ):
            raise ValueError(
                'artifact COMBO_BUNDLE must use its canonical member path'
            )
        combo_tag = strategy_params.get('COMBO_TAG')
        if combo_tag is not None and type(combo_tag) is not str:
            raise ValueError('artifact COMBO_TAG must be a string or null')
        custom_strategy_params = None
    else:
        custom_strategy_params = _normalize_custom_strategy_params(
            strategy_params
        )

    def positive_int(
        name: str, default: int, *, maximum: int = None
    ) -> int:
        value = parameters.get(name, default)
        if (
            type(value) is not int
            or value <= 0
            or (maximum is not None and value > maximum)
        ):
            raise ValueError(f'artifact {name} must be a positive integer')
        return value

    def finite_number(name: str, default: float) -> float:
        value = parameters.get(name, default)
        if type(value) not in (int, float) or not math.isfinite(float(value)):
            raise ValueError(f'artifact {name} must be a finite number')
        return float(value)

    initial_cash = finite_number('initial_cash', 1_000_000.0)
    if initial_cash <= 0.0:
        raise ValueError('artifact initial_cash must be positive')
    slippage = finite_number('slippage', 0.001)
    if not 0.0 <= slippage < 1.0:
        raise ValueError('artifact slippage must be in [0, 1)')
    commission = finite_number('commission', 0.00025)
    if commission < 0.0:
        raise ValueError('artifact commission must be non-negative')
    stamp_duty = parameters.get('stamp_duty', 0.001)
    if stamp_duty != 'auto':
        stamp_duty = finite_number('stamp_duty', 0.001)
        if stamp_duty < 0.0:
            raise ValueError('artifact stamp_duty must be non-negative')
    transfer_fee_rate = finite_number('transfer_fee_rate', 0.0)
    if transfer_fee_rate < 0.0:
        raise ValueError(
            'artifact transfer_fee_rate must be non-negative'
        )
    min_commission = finite_number('min_commission', 5.0)
    if min_commission < 0.0:
        raise ValueError('artifact min_commission must be non-negative')
    liquidity_cap_ratio = finite_number('liquidity_cap_ratio', 0.8)
    if not 0.0 <= liquidity_cap_ratio <= 1.0:
        raise ValueError(
            'artifact liquidity_cap_ratio must be in [0, 1]'
        )
    limit_pct_overrides = parameters.get('limit_pct_overrides')
    if limit_pct_overrides is not None:
        if type(limit_pct_overrides) is not dict:
            raise ValueError(
                'artifact limit_pct_overrides must be a mapping or null'
            )
        normalized_limits = {}
        for symbol, value in limit_pct_overrides.items():
            if type(symbol) is not str or not symbol.strip():
                raise ValueError(
                    'artifact limit_pct_overrides keys must be non-empty strings'
                )
            if (
                type(value) not in (int, float)
                or not math.isfinite(float(value))
                or not 0.0 < float(value) <= 1.0
            ):
                raise ValueError(
                    'artifact limit_pct_overrides values must be in (0, 1]'
                )
            normalized_symbol = symbol.strip()
            if normalized_symbol in normalized_limits:
                raise ValueError(
                    'artifact limit_pct_overrides keys collide after trimming'
                )
            normalized_limits[normalized_symbol] = float(value)
        limit_pct_overrides = normalized_limits or None
    t0_overrides = parameters.get('t0_overrides')
    if t0_overrides is not None:
        if type(t0_overrides) is not list or any(
            type(value) is not str or not value.strip()
            for value in t0_overrides
        ):
            raise ValueError(
                'artifact t0_overrides must be a list of non-empty strings'
            )
        t0_overrides = list(dict.fromkeys(
            value.strip() for value in t0_overrides
        )) or None
    open_buy_resize_mode = parameters.get('open_buy_resize_mode', 'auto')
    if open_buy_resize_mode not in {'auto', 'legacy'}:
        raise ValueError('artifact open_buy_resize_mode is unsupported')
    open_buy_fill_mode = parameters.get('open_buy_fill_mode', 'open+slip')
    if open_buy_fill_mode not in {'open+slip', 'open'}:
        raise ValueError('artifact open_buy_fill_mode is unsupported')
    open_buy_sizing = parameters.get('open_buy_sizing', 'limit_up')
    if open_buy_sizing not in {'limit_up', 'fill'}:
        raise ValueError('artifact open_buy_sizing is unsupported')
    risk_free_rate = finite_number('risk_free_rate', 0.03)
    if not -1e298 <= risk_free_rate <= 1e298:
        raise ValueError(
            'artifact risk_free_rate exceeds the GUI representable range'
        )
    auction_values = {}
    for window in ('open', 'close'):
        fixed_name = f'daily_{window}_cap_yuan'
        ratio_name = f'daily_{window}_previous_day_ratio'
        fixed = parameters.get(fixed_name)
        ratio = parameters.get(ratio_name)
        if fixed is not None and ratio is not None:
            raise ValueError(
                f'artifact daily {window} auction cannot configure both '
                'fixed capacity and previous-day ratio'
            )
        if fixed is not None:
            fixed = finite_number(fixed_name, 0.0)
            if fixed <= 0.0:
                raise ValueError(f'artifact {fixed_name} must be positive')
        if ratio is not None:
            ratio = finite_number(ratio_name, 0.0)
            if not 0.0 < ratio <= 1.0:
                raise ValueError(
                    f'artifact {ratio_name} must be in (0, 1]'
                )
        auction_values[fixed_name] = fixed
        auction_values[ratio_name] = ratio
    return {
        'data_source': 'external_local_data',
        'input_mode': input_mode,
        'signals_file': 'inputs/signals.csv' if has_signals else None,
        'signals_format': (
            signal_format
            if has_signals else None
        ),
        'combo_bundle': 'inputs/combo' if has_combo else None,
        'combo_tag': strategy_params.get('COMBO_TAG') if has_combo else None,
        'strategy_params': custom_strategy_params,
        'pool_source': 'specified' if symbols else 'all_market',
        'symbols': None if symbols is None else list(symbols),
        'realized_symbols': realized_symbols,
        'industry': None,
        'start_date': restored.get('requested_start_date'),
        'end_date': restored.get('requested_end_date'),
        'initial_cash': initial_cash,
        'slippage': slippage,
        'commission': commission,
        'stamp_duty': stamp_duty,
        'transfer_fee_rate': transfer_fee_rate,
        'min_commission': min_commission,
        'lot_size': positive_int(
            'lot_size', 100, maximum=2_000_000_000
        ),
        'liquidity_cap_ratio': liquidity_cap_ratio,
        'limit_pct_overrides': limit_pct_overrides,
        'open_buy_resize_mode': open_buy_resize_mode,
        'open_buy_fill_mode': open_buy_fill_mode,
        'open_buy_sizing': open_buy_sizing,
        't0_overrides': t0_overrides,
        'trading_days_per_year': positive_int(
            'trading_days_per_year', 252, maximum=10_000
        ),
        'risk_free_rate': risk_free_rate,
        'freq': parameters.get('freq', 'daily'),
        'mode': 'portfolio',
        'strategy_kind': 'portfolio',
        'price_mode': parameters.get('price_mode') or 'dual',
        **auction_values,
    }


def _resolve_canonical_artifact_member(
    artifact_root: Path,
    configured_path,
    *,
    canonical_path: str,
    label: str,
) -> Path:
    """Resolve one fixed artifact member without Windows path escapes."""

    if not isinstance(artifact_root, Path):
        raise TypeError('artifact_root must be a pathlib.Path')
    if type(configured_path) is not str or configured_path != canonical_path:
        raise ValueError(
            f'artifact {label} must be exactly {canonical_path!r}'
        )
    member = Path(configured_path)
    if (
        member.is_absolute()
        or member.drive
        or member.root
        or member.anchor
        or '..' in member.parts
    ):
        raise ValueError(
            f'artifact {label} must be a canonical relative path'
        )
    root = artifact_root.resolve(strict=True)
    resolved = root.joinpath(*member.parts).resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f'artifact {label} resolves outside the artifact root'
        ) from exc
    return resolved


def _require_artifact_role_path(loaded, role: str, path: str) -> bytes:
    """Bind a replay adapter path to its verified manifest role."""

    descriptor = loaded.manifest.artifacts.get(role)
    if descriptor is None or descriptor.path != path:
        raise ValueError(
            f'artifact role {role!r} must be stored at {path!r}'
        )
    return loaded.read_bytes(role)


@dataclass(frozen=True)
class StrategyCheckResult:
    """Compile-only GUI syntax/contract check result."""

    strategy_kind: str
    strategy_class_name: str


@dataclass(frozen=True)
class GuiLoadedRun:
    """GUI view of either a verified v1 artifact or read-only legacy run."""

    root: Path
    result: object
    config: dict
    strategy_source: str
    artifact_format: str
    artifact_verified: bool
    is_rankable: bool
    result_contract: object = None
    created_at_utc: str = ''
    provenance: object = None
    signals_replay_input: object = None
    combo_replay_bundle: object = None
    rerun_block_reason: str = None
    engine_kind: str = ''
    run_error: object = None
    traceback_text: str = ''
    traceback_truncated: bool = False


def format_run_error_summary(run_error, *, engine_kind: str = '') -> str:
    """Format verified RunError fields without treating them as result data."""

    if run_error is None:
        return "结构化错误: 不可用"
    category = getattr(run_error.category, 'value', str(run_error.category))
    engine = engine_kind or 'unknown'
    return (
        f"引擎: {engine}\n"
        f"错误代码: {run_error.code}\n"
        f"类别: {category}\n"
        f"阶段: {run_error.phase}\n"
        f"异常类型: {run_error.exception_type}\n"
        f"消息: {run_error.message}"
    )


def _verified_traceback_text(loaded) -> tuple[str, bool]:
    """Return only manifest-verified traceback bytes, capped for GUI display."""

    run_error = loaded.outcome.error
    if run_error is None or run_error.traceback_role is None:
        return '', False
    payload = loaded.read_bytes(run_error.traceback_role)
    # Validate the complete member as UTF-8 before deriving a display prefix.
    payload.decode('utf-8')
    if len(payload) <= GUI_TRACEBACK_MAX_BYTES:
        return payload.decode('utf-8'), False
    prefix = payload[:GUI_TRACEBACK_MAX_BYTES].decode('utf-8', errors='ignore')
    return prefix, True


def resolve_gui_results_root(results_root=None) -> Path:
    """Resolve GUI output only through explicit/runtime path configuration."""
    return RuntimePaths.resolve(
        results_root=results_root,
        require_data_root=False,
    ).results_root


def _gui_artifact_target(results_root: Path) -> Path:
    """Return a portable, collision-resistant immutable run target."""
    if not isinstance(results_root, Path):
        raise TypeError("results_root must be a pathlib.Path")
    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    return results_root / f"gui-{timestamp}-{uuid4().hex[:12]}"


def _gui_run_outcome(result) -> RunOutcome:
    """Wrap one supported GUI result without synthesizing engine evidence."""
    if type(result) is PortfolioResult:
        return RunOutcome.from_result(
            result, engine_kind=EngineKind.CASH_PORTFOLIO)
    if type(result) is ParallelResult:
        return build_parallel_outcome(result)
    raise TypeError(
        "GUI artifacts support exactly PortfolioResult or ParallelResult"
    )


def save_gui_run(
    result,
    config: dict,
    strategy_source: str,
    *,
    results_root,
    market_data_fingerprints=(),
    signals_artifact_inputs=(),
    combo_artifact_inputs=(),
) -> Path:
    """Atomically publish one verified RunArtifact v1 from the GUI."""
    if type(config) is not dict:
        raise TypeError("config must be exactly dict")
    if type(strategy_source) is not str:
        raise TypeError("strategy_source must be exactly str")
    if type(market_data_fingerprints) is not tuple:
        raise TypeError("market_data_fingerprints must be exactly tuple")
    if any(
        type(source) is not SourceFingerprint
        for source in market_data_fingerprints
    ):
        raise TypeError(
            "market_data_fingerprints must contain SourceFingerprint values"
        )
    if type(combo_artifact_inputs) is not tuple:
        raise TypeError("combo_artifact_inputs must be exactly tuple")
    if type(signals_artifact_inputs) is not tuple:
        raise TypeError("signals_artifact_inputs must be exactly tuple")
    root = resolve_gui_results_root(results_root)
    outcome = _gui_run_outcome(result)
    portable_config = dict(config)
    portable_config.pop('_signals_replay_input', None)
    portable_config.pop('_combo_replay_bundle', None)
    portable_config.setdefault('price_mode', 'dual')
    input_mode = _gui_input_mode(portable_config)
    extra_payloads = ()
    signals_source = None
    # These are captured before engine.run and revalidated immediately after
    # it.  Saving must never take its first market-data snapshot here.
    source_fingerprints = list(market_data_fingerprints)
    if input_mode == 'signals':
        expected = ('signals_source', 'inputs/signals.csv')
        if len(signals_artifact_inputs) != 1:
            raise ValueError(
                'signals run is missing its immutable runtime input snapshot'
            )
        item = signals_artifact_inputs[0]
        if (
            type(item) is not tuple
            or len(item) != 3
            or item[:2] != expected
            or type(item[2]) is not bytes
        ):
            raise ValueError(
                'signals runtime input snapshot has an invalid member'
            )
        signals_source = item[2]
        portable_config['input_mode'] = 'signals'
        portable_config['signals_file'] = 'inputs/signals.csv'
        portable_config['combo_bundle'] = None
        portable_config['combo_tag'] = None
        source_fingerprints.append(SourceFingerprint.from_bytes(
            kind='signal_input',
            logical_path='inputs/signals.csv',
            payload=signals_source,
        ))
    elif signals_artifact_inputs:
        raise ValueError(
            'signals runtime input snapshot was provided for a non-signals run'
        )

    combo_path = portable_config.get('combo_bundle')
    if input_mode == 'combo':
        expected_members = (
            ('combo_manifest_source', 'inputs/combo/diepi_combo.json'),
            ('combo_targets_source', 'inputs/combo/targets.csv'),
            ('combo_close_sells_source', 'inputs/combo/close_sells.csv'),
            ('combo_daily_source', 'inputs/combo/daily.csv'),
        )
        if len(combo_artifact_inputs) != len(expected_members):
            raise ValueError(
                'combo run is missing its immutable runtime input snapshot'
            )
        normalized_inputs = []
        for item, expected in zip(combo_artifact_inputs, expected_members):
            if (
                type(item) is not tuple
                or len(item) != 3
                or item[:2] != expected
                or type(item[2]) is not bytes
            ):
                raise ValueError(
                    'combo runtime input snapshot has an invalid member'
                )
            normalized_inputs.append(item)
        try:
            manifest = json.loads(normalized_inputs[0][2].decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                'combo runtime manifest snapshot is invalid'
            ) from exc
        scope = manifest.get('scope') if type(manifest) is dict else None
        if type(scope) is not dict:
            raise ValueError('combo runtime manifest snapshot lacks scope')
        start_date = str(portable_config.get('start_date') or '')
        end_date = str(portable_config.get('end_date') or '')
        if (
            start_date < str(scope.get('start_date') or '')
            or end_date > str(scope.get('end_date') or '')
        ):
            raise ValueError(
                'GUI combo dates exceed the runtime snapshot scope'
            )
        configured_symbols = portable_config.get('symbols')
        if configured_symbols is not None and set(configured_symbols) != set(
            scope.get('symbols') or ()
        ):
            raise ValueError(
                'GUI combo symbols must exactly match the frozen bundle scope'
            )
        portable_config['combo_bundle'] = 'inputs/combo'
        portable_config['signals_file'] = None
        portable_config['signals_format'] = None
        portable_config['combo_tag'] = manifest.get('tag')
        extra_payloads = tuple(
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
            for role, path, payload in normalized_inputs
        )
        source_fingerprints.extend(
            SourceFingerprint.from_bytes(
                    kind='combo_input',
                    logical_path=path,
                    payload=payload,
                )
                for _, path, payload in normalized_inputs
        )
    elif combo_artifact_inputs:
        raise ValueError(
            'combo runtime input snapshot was provided for a non-combo run'
        )
    elif combo_path:
        raise ValueError('combo_bundle is configured outside combo input mode')
    if 'data_root' in portable_config:
        portable_config.pop('data_root')
        portable_config['data_source'] = 'external_local_data'
    return ArtifactStore.save(
        outcome,
        _gui_artifact_target(root),
        config=portable_config,
        provenance=RunProvenance.build(sources=source_fingerprints),
        strategy_source=strategy_source,
        signals_source=signals_source,
        extra_payloads=extra_payloads,
    )


def load_gui_run(folder_path) -> GuiLoadedRun:
    """Load v1 with verification, or legacy read-only without trust upgrade."""
    root = Path(folder_path).absolute()
    if (root / 'manifest.json').is_file():
        loaded = ArtifactStore.load(root)
        try:
            source = loaded.read_bytes('strategy_source').decode('utf-8')
        except KeyError:
            source = ''
        except UnicodeDecodeError as exc:
            raise ValueError("strategy_source must use UTF-8") from exc
        if loaded.outcome.error is not None:
            traceback_text, traceback_truncated = _verified_traceback_text(
                loaded
            )
            return GuiLoadedRun(
                root=loaded.root,
                result=None,
                config=dict(loaded.config),
                strategy_source=source,
                artifact_format='RunArtifact v1',
                artifact_verified=loaded.artifact_verified,
                is_rankable=False,
                result_contract=loaded.outcome.result_contract,
                created_at_utc=loaded.manifest.created_at_utc,
                provenance=loaded.provenance,
                rerun_block_reason=(
                    'FAILED RunArtifact is diagnostic-only and cannot be '
                    'rerun from the GUI'
                ),
                engine_kind=loaded.outcome.engine_kind.value,
                run_error=loaded.outcome.error,
                traceback_text=traceback_text,
                traceback_truncated=traceback_truncated,
            )
        if loaded.outcome.engine_kind is EngineKind.INDEX_FUTURES:
            return GuiLoadedRun(
                root=loaded.root,
                result=loaded.result,
                config=dict(loaded.config),
                strategy_source=source,
                artifact_format='RunArtifact v1',
                artifact_verified=loaded.artifact_verified,
                is_rankable=loaded.is_rankable,
                result_contract=loaded.outcome.result_contract,
                created_at_utc=loaded.manifest.created_at_utc,
                provenance=loaded.provenance,
                engine_kind=loaded.outcome.engine_kind.value,
                rerun_block_reason=(
                    'index_futures Artifact is available as a read-only '
                    'experimental summary; the cash GUI cannot rerun it'
                ),
            )
        try:
            restored_config = _gui_config_from_artifact(dict(loaded.config))
        except _GuiRerunUnsupported as exc:
            return GuiLoadedRun(
                root=loaded.root,
                result=loaded.result,
                config=dict(loaded.config),
                strategy_source=source,
                artifact_format='RunArtifact v1',
                artifact_verified=loaded.artifact_verified,
                is_rankable=loaded.is_rankable,
                result_contract=loaded.outcome.result_contract,
                created_at_utc=loaded.manifest.created_at_utc,
                provenance=loaded.provenance,
                engine_kind=loaded.outcome.engine_kind.value,
                rerun_block_reason=str(exc),
            )
        input_mode = _gui_input_mode(restored_config)
        rerun_block_reason = None
        if input_mode in {'signals', 'combo'}:
            template_name = (
                'signal_replay_template.py'
                if input_mode == 'signals'
                else 'combo_replay_template.py'
            )
            installed_source = (
                Path(__file__).resolve().parents[1]
                / 'cli'
                / template_name
            ).read_text(encoding='utf-8')
            if source != installed_source:
                rerun_block_reason = (
                    f'verified {input_mode} replay template differs from '
                    'the installed diepi version'
                )
        if 'strategy_params' in restored_config:
            if input_mode == 'strategy':
                custom_params = _normalize_custom_strategy_params(
                    restored_config.get('strategy_params')
                )
                if (
                    restored_config.get('mode') == 'independent'
                    and custom_params
                ):
                    raise ValueError(
                        'independent GUI artifacts cannot carry strategy_params'
                    )
                restored_config['strategy_params'] = custom_params or None
            elif restored_config.get('strategy_params') not in (None, {}):
                raise ValueError(
                    f'{input_mode} artifacts cannot carry custom strategy_params'
                )
        signals_replay_input = None
        combo_replay_bundle = None
        signals_member = restored_config.get('signals_file')
        if input_mode == 'signals':
            signals_payload = _require_artifact_role_path(
                loaded, 'signals_source', 'inputs/signals.csv'
            )
            restored_config['signals_file'] = str(
                _resolve_canonical_artifact_member(
                    loaded.root,
                    signals_member,
                    canonical_path='inputs/signals.csv',
                    label='signals_file',
                )
            )
            from ..cli.signal_input import SignalReplayInput

            signals_replay_input = SignalReplayInput.from_bytes(
                signals_payload,
                signal_format=(
                    restored_config.get('signals_format') or 'auto'
                ),
                source_name='signals.csv',
            )
            restored_config['signals_format'] = (
                signals_replay_input.signal_format
            )
            restored_config['_signals_replay_input'] = signals_replay_input
        combo_member = restored_config.get('combo_bundle')
        if input_mode == 'combo':
            verified_combo_inputs = []
            for role, path in (
                ('combo_manifest_source', 'inputs/combo/diepi_combo.json'),
                ('combo_targets_source', 'inputs/combo/targets.csv'),
                ('combo_close_sells_source', 'inputs/combo/close_sells.csv'),
                ('combo_daily_source', 'inputs/combo/daily.csv'),
            ):
                verified_combo_inputs.append((
                    role,
                    path,
                    _require_artifact_role_path(loaded, role, path),
                ))
            combo_root = _resolve_canonical_artifact_member(
                loaded.root,
                combo_member,
                canonical_path='inputs/combo',
                label='combo_bundle',
            )
            restored_config['combo_bundle'] = str(combo_root)
            from ..cli.combo_bundle import load_combo_bundle

            combo_replay_bundle = load_combo_bundle(
                combo_root,
                tag=restored_config.get('combo_tag') or None,
            )
            if combo_replay_bundle.artifact_inputs() != tuple(
                verified_combo_inputs
            ):
                raise ValueError(
                    'combo replay snapshot differs from verified Artifact bytes'
                )
            restored_config['_combo_replay_bundle'] = combo_replay_bundle
        return GuiLoadedRun(
            root=loaded.root,
            result=loaded.result,
            config=restored_config,
            strategy_source=source,
            artifact_format='RunArtifact v1',
            artifact_verified=loaded.artifact_verified,
            is_rankable=loaded.is_rankable,
            result_contract=loaded.outcome.result_contract,
            created_at_utc=loaded.manifest.created_at_utc,
            provenance=loaded.provenance,
            signals_replay_input=signals_replay_input,
            combo_replay_bundle=combo_replay_bundle,
            rerun_block_reason=rerun_block_reason,
            engine_kind=loaded.outcome.engine_kind.value,
        )

    loaded = ArtifactStore.load_legacy(root)
    return GuiLoadedRun(
        root=loaded.root,
        result=loaded.result,
        config=dict(loaded.config),
        strategy_source=loaded.strategy_source,
        artifact_format='legacy',
        artifact_verified=False,
        is_rankable=False,
        result_contract=None,
        created_at_utc='',
        provenance=None,
    )


class OutputCapture:
    """捕获 print 输出"""

    def __init__(self, callback):
        self.callback = callback
        self._buffer = ""

    def write(self, text):
        if text.strip():  # 忽略空行
            self.callback(text.rstrip())
        return len(text)

    def flush(self):
        pass


class BacktestWorker(QThread):
    """
    回测工作线程

    在后台执行回测，通过信号与 UI 通信

    Signals:
        progress: 进度更新 (current, total, message)
        log: 日志信息 (message)
        finished: 回测完成 (result)
        error: 错误信息 (message)
        daily_update: 每日净值更新 (daily_values_list)
    """

    progress = Signal(int, int, str)
    log = Signal(str)
    finished = Signal(object)
    error = Signal(str)
    daily_update = Signal(object)  # 每日净值更新
    trades_update = Signal(object)  # 交易记录更新
    positions_update = Signal(object)  # 持仓更新
    debug_output = Signal(str)  # 调试输出 (策略中的print)

    def __init__(self, code: str, config: dict):
        """
        Args:
            code: 策略代码
            config: 回测配置
        """
        super().__init__()
        self.code = code
        self.config = config
        self._stop_requested = False
        self._engine = None  # 保存引擎引用
        self.market_data_fingerprints = ()
        self.signals_artifact_inputs = ()
        self.combo_artifact_inputs = ()
        self.artifact_strategy_source = code

        # 信号节流 - 平衡UI响应和更新频率
        self._last_progress_time = 0.0
        self._last_daily_time = 0.0
        self._throttle_interval = 0.3  # 最小发送间隔（秒）- 300ms足够UI处理

    def _daily_auction_liquidity(self):
        return build_daily_auction_liquidity_policy(
            open_fixed_yuan=self.config.get('daily_open_cap_yuan'),
            close_fixed_yuan=self.config.get('daily_close_cap_yuan'),
            open_previous_day_ratio=self.config.get(
                'daily_open_previous_day_ratio'),
            close_previous_day_ratio=self.config.get(
                'daily_close_previous_day_ratio'),
        )

    def _price_mode_kwargs(self):
        return _price_mode_engine_kwargs(
            self.config.get('price_mode', 'dual')
        )

    def _capture_market_data_fingerprints(self, symbols):
        return collect_market_data_fingerprints(
            self.config.get('data_root'),
            symbols=symbols,
            price_mode=self.config.get('price_mode', 'dual'),
            frequency=self.config.get('freq', 'daily'),
            start_date=self.config.get('start_date'),
            end_date=self.config.get('end_date'),
        )

    def _confirm_market_data_stable(self, before, symbols) -> None:
        after = self._capture_market_data_fingerprints(symbols)
        if after != before:
            raise OSError(
                "MARKET_DATA_CHANGED_DURING_RUN: 行情文件在回测期间发生变化，"
                "结果不会绑定到另一个数据版本"
            )
        self.market_data_fingerprints = before

    def run(self):
        """执行回测"""
        # 保存原始 stdout
        original_stdout = sys.stdout

        # 创建输出捕获器
        output_capture = OutputCapture(self._on_debug_output)

        try:
            self.log.emit("正在编译策略代码...")

            mode = self.config.get('mode', 'portfolio')
            input_mode = _gui_input_mode(self.config)
            strategy_kind = _strategy_kind_for_mode(mode)
            configured_kind = self.config.get('strategy_kind', strategy_kind)
            if configured_kind != strategy_kind:
                raise ValueError(
                    "回测模式与策略契约不一致: "
                    f"mode={mode!r}, strategy_kind={configured_kind!r}"
                )

            strategy_params = None
            executable_code = self.code
            if input_mode in {'signals', 'combo'} and mode != 'portfolio':
                raise ValueError(f'{input_mode} 输入只支持组合投资模式')
            configured_custom_params = self.config.get('strategy_params')
            if input_mode == 'strategy':
                custom_params = _normalize_custom_strategy_params(
                    configured_custom_params
                )
                if mode == 'independent' and custom_params:
                    raise ValueError(
                        '独立测试模式不支持策略参数覆盖；请使用组合投资模式'
                    )
                strategy_params = custom_params or None
                self.config['strategy_params'] = custom_params or None
            elif configured_custom_params not in (None, {}):
                raise ValueError(
                    f'{input_mode} 输入不能携带自定义 strategy_params'
                )

            if input_mode == 'signals':
                from ..cli.signal_input import (
                    SignalReplayInput,
                    load_signal_replay_input,
                )

                frozen = self.config.get('_signals_replay_input')
                if frozen is None:
                    frozen = load_signal_replay_input(
                        self.config['signals_file'],
                        signal_format=(
                            self.config.get('signals_format') or 'auto'
                        ),
                    )
                elif type(frozen) is not SignalReplayInput:
                    raise ValueError(
                        'signals runtime snapshot has an invalid type'
                    )
                frozen = frozen.revalidated()
                requested_format = (
                    self.config.get('signals_format') or 'auto'
                )
                if requested_format not in {'auto', frozen.signal_format}:
                    raise ValueError(
                        'signals format conflicts with the frozen runtime '
                        'snapshot'
                    )
                self.signals_artifact_inputs = tuple(
                    (role, path, bytes(payload))
                    for role, path, payload in frozen.artifact_inputs()
                )
                executable_code = (
                    Path(__file__).resolve().parents[1]
                    / 'cli'
                    / 'signal_replay_template.py'
                ).read_text(encoding='utf-8')
                strategy_params = {'SIGNALS_INPUT': frozen}
                self.config['input_mode'] = 'signals'
                self.config['pool_source'] = 'specified'
                self.config['symbols'] = list(frozen.symbols)
                self.config['signals_file'] = str(
                    Path(self.config['signals_file']).resolve()
                )
                self.config['signals_format'] = frozen.signal_format
                for warning in frozen.warnings:
                    self.log.emit(f'清单警告: {warning}')
                self.log.emit(
                    '输入模式: 冻结 signals 清单回放 '
                    f'({frozen.signal_format}, '
                    f'{len(frozen.instructions)} 条指令 / '
                    f'{len(frozen.dates)} 个清单日)'
                )
            elif input_mode == 'combo':
                combo_path = self.config.get('combo_bundle')
                if mode != 'portfolio':
                    raise ValueError('冻结 combo 只支持组合投资模式')
                from ..cli.combo_bundle import (
                    ComboReplayBundle,
                    load_combo_bundle,
                )

                bundle = self.config.get('_combo_replay_bundle')
                if bundle is None:
                    bundle = load_combo_bundle(
                        combo_path, tag=self.config.get('combo_tag') or None
                    )
                elif type(bundle) is not ComboReplayBundle:
                    raise ValueError('combo runtime snapshot has an invalid type')
                requested_tag = self.config.get('combo_tag') or None
                if requested_tag is not None and requested_tag != bundle.tag:
                    raise ValueError(
                        'combo tag conflicts with the frozen runtime snapshot'
                    )
                bundle = bundle.revalidated()
                bundle.validate_requested_scope(
                    self.config['start_date'], self.config['end_date']
                )
                self.combo_artifact_inputs = tuple(
                    (role, path, bytes(payload))
                    for role, path, payload in bundle.artifact_inputs()
                )
                executable_code = (
                    Path(__file__).resolve().parents[1]
                    / 'cli'
                    / 'combo_replay_template.py'
                ).read_text(encoding='utf-8')
                strategy_params = {'COMBO_BUNDLE': bundle}
                self.config['input_mode'] = 'combo'
                self.config['pool_source'] = 'specified'
                self.config['symbols'] = list(bundle.symbols)
                self.config['combo_bundle'] = str(bundle.root)
                self.config['combo_tag'] = bundle.tag
                self.log.emit(
                    '输入模式: 冻结 combo 因果回放 '
                    f'({bundle.tag}, {bundle.start_date}..{bundle.end_date})'
                )
            else:
                self.config['input_mode'] = 'strategy'

            # 按引擎模式编译，组合策略与单标的策略不能混用。
            self.artifact_strategy_source = executable_code
            strategy_class = self._compile_strategy(
                executable_code,
                strategy_kind=strategy_kind,
                strategy_params=strategy_params,
            )

            self.log.emit(f"策略编译成功 ({strategy_kind})")

            if self.config.get('_syntax_only', False):
                self.finished.emit(StrategyCheckResult(
                    strategy_kind=strategy_kind,
                    strategy_class_name=strategy_class.__name__,
                ))
                return

            # 重定向 stdout 以捕获 print 输出
            sys.stdout = output_capture

            # 解析股票池配置
            pool_source = PoolSource.SPECIFIED
            pool_symbols = None
            pool_industry = None

            if self.config['pool_source'] == 'specified':
                pool_source = PoolSource.SPECIFIED
                pool_symbols = self.config['symbols']
            elif self.config['pool_source'] == 'industry':
                pool_source = PoolSource.INDUSTRY
                pool_industry = self.config['industry']
            else:
                pool_source = PoolSource.ALL_MARKET

            self.log.emit(f"股票池来源: {pool_source.value}")
            if pool_symbols:
                self.log.emit(f"股票列表: {', '.join(pool_symbols[:5])}{'...' if len(pool_symbols) > 5 else ''}")

            if mode == 'independent':
                # 独立测试模式 - 使用 ParallelRunner（不需要编译策略，直接传代码到子进程）
                self._run_independent_mode(pool_source, pool_symbols)
            else:
                # 组合投资模式 - 使用 PortfolioEngine
                self._run_portfolio_mode(strategy_class, pool_source, pool_symbols, pool_industry)

        except SyntaxError as e:
            self.error.emit(f"语法错误: {e}")
        except Exception as e:
            tb = traceback.format_exc()
            self.error.emit(f"回测出错: {e}\n\n{tb}")
        finally:
            # 恢复原始 stdout
            sys.stdout = original_stdout

    def _run_portfolio_mode(self, strategy_class, pool_source, pool_symbols, pool_industry):
        """组合投资模式 - 使用 PortfolioEngine"""
        self.log.emit("回测模式: 组合投资 (共享资金池)")

        dynamic_fingerprint_tracker = DynamicMarketDataFingerprintTracker(
            self.config.get('data_root'),
            configured_symbols=(
                pool_symbols if pool_source is PoolSource.SPECIFIED else ()
            ),
            price_mode=self.config.get('price_mode', 'dual'),
            frequency=self.config.get('freq', 'daily'),
            start_date=self.config.get('start_date'),
            end_date=self.config.get('end_date'),
            allow_incomplete_sources=(
                pool_source is not PoolSource.SPECIFIED
            ),
        )

        # 创建回测引擎
        self._engine = PortfolioEngine(
            start_date=self.config['start_date'],
            end_date=self.config['end_date'],
            initial_cash=self.config['initial_cash'],
            freq=self.config['freq'],
            pool_source=pool_source,
            pool_symbols=pool_symbols,
            pool_industry=pool_industry,
            slippage=self.config['slippage'],
            commission_rate=self.config['commission'],
            stamp_duty=self.config.get('stamp_duty', 0.001),
            transfer_fee_rate=self.config.get('transfer_fee_rate', 0.0),
            min_commission=self.config.get('min_commission', 5.0),
            lot_size=self.config.get('lot_size', 100),
            open_buy_resize_mode=self.config.get('open_buy_resize_mode', 'auto'),
            liquidity_cap_ratio=self.config.get('liquidity_cap_ratio', 0.8),
            daily_auction_liquidity=self._daily_auction_liquidity(),
            limit_pct_overrides=self.config.get('limit_pct_overrides'),
            open_buy_fill_mode=self.config.get('open_buy_fill_mode', 'open+slip'),
            open_buy_sizing=self.config.get('open_buy_sizing', 'limit_up'),
            t0_overrides=self.config.get('t0_overrides'),
            trading_days_per_year=self.config.get(
                'trading_days_per_year', 252
            ),
            risk_free_rate=self.config.get('risk_free_rate', 0.03),
            max_days=self.config.get('max_days'),
            progress_callback=self._on_progress,
            stop_check=self._should_stop,
            daily_callback=self._on_daily_update,
            # UI刷新间隔（默认10天，配置里可自行覆盖）
            update_interval=self.config.get('update_interval', 10),
            data_root=self.config['data_root'],
            market_data_observer=(
                dynamic_fingerprint_tracker.observe
                if dynamic_fingerprint_tracker is not None
                else None
            ),
            **self._price_mode_kwargs(),
        )

        # 创建策略实例
        strategy = strategy_class()

        self.log.emit(f"开始回测: {self.config['start_date']} ~ {self.config['end_date']}")
        self.log.emit(f"初始资金: {self.config['initial_cash']:,} 元")
        self.log.emit(f"回测频率: {self.config['freq']}")
        self.log.emit(f"价格模式: {self.config.get('price_mode', 'dual')}")

        evidence_symbols = (
            pool_symbols if pool_source is PoolSource.SPECIFIED else None
        )
        before_fingerprints = self._capture_market_data_fingerprints(
            evidence_symbols
        )

        # 运行回测
        result = self._engine.run(strategy)
        self._confirm_market_data_stable(
            before_fingerprints, evidence_symbols
        )
        dynamic_fingerprints = dynamic_fingerprint_tracker.verify_stable()
        self.market_data_fingerprints = tuple(sorted(
            {
                (source.kind, source.logical_path): source
                for source in (
                    self.market_data_fingerprints
                    + dynamic_fingerprints
                )
            }.values(),
            key=lambda source: (source.kind, source.logical_path),
        ))
        self.config['realized_symbols'] = list(
            getattr(self._engine, 'realized_market_symbols', ())
            or tuple(pool_symbols or ())
        )

        if result is None:
            raise RuntimeError("PortfolioEngine.run() 未返回结果对象")

        contract = getattr(result, 'result_contract', None)
        status = (
            contract.status.value if contract is not None
            else 'LEGACY_UNCLASSIFIED'
        )
        rankable = bool(getattr(result, 'is_rankable', False))
        self.log.emit(
            f"回测终态: {status} | 可排名: {'是' if rankable else '否'}"
        )
        if contract is not None and contract.reason is not None:
            self.log.emit(
                f"终止原因: {contract.reason.code}: "
                f"{contract.reason.message}"
            )
        for warning in getattr(contract, 'warnings', ()):
            self.log.emit(f"结果警告: {warning.code}: {warning.message}")
        if getattr(result, 'error', ''):
            self.log.emit(f"引擎结果诊断: {result.error}")
        self.log.emit(f"最终资产: {result.final_value:,.0f} 元")
        self.log.emit(f"总收益率: {result.total_return * 100:.2f}%")
        # PARTIAL/INVALID/CANCELED/FAILED 都是引擎返回的可诊断终态，
        # 必须交给结果页并允许组合结果按现有 Artifact API 保存。
        self.finished.emit(result)

    def _run_independent_mode(self, pool_source, pool_symbols):
        """独立测试模式 - CPU 多进程并行"""
        import multiprocessing as _mp
        max_workers = self.config.get('max_workers') or _mp.cpu_count()

        self.log.emit(f"回测模式: 独立测试 (每股独立资金)")

        # 获取股票列表
        if pool_source == PoolSource.ALL_MARKET and not pool_symbols:
            self.log.emit("正在获取全市场股票列表...")
        symbols, universe_assumptions, universe_warnings = (
            _resolve_independent_universe(
                pool_source,
                pool_symbols,
                self.config['start_date'],
                self.config['end_date'],
                data_root=self.config['data_root'],
            )
        )
        if pool_source not in (PoolSource.ALL_MARKET, PoolSource.SPECIFIED):
            self.error.emit("独立测试模式需要指定股票或选择全市场")
            return
        if pool_source == PoolSource.ALL_MARKET:
            self.log.emit(f"共 {len(symbols)} 只股票")
        for warning in universe_warnings:
            self.log.emit(f"股票池告警: {warning}")

        if not symbols:
            self.error.emit("股票列表为空")
            return

        self.log.emit(f"开始回测: {self.config['start_date']} ~ {self.config['end_date']}")
        self.log.emit(f"每只股票初始资金: {self.config['initial_cash']:,} 元")
        self.log.emit(f"回测频率: {self.config['freq']}")
        self.log.emit(f"价格模式: {self.config.get('price_mode', 'dual')}")
        self.log.emit(f"股票数量: {len(symbols)}")
        self.log.emit(f"计算模式: CPU 并行 ({max_workers} 进程)")

        runner = ParallelRunner(
            symbols=symbols,
            start_date=self.config['start_date'],
            end_date=self.config['end_date'],
            initial_cash=self.config['initial_cash'],
            freq=self.config['freq'],
            max_workers=max_workers,
            slippage=self.config['slippage'],
            commission_rate=self.config['commission'],
            stamp_duty=self.config.get('stamp_duty', 0.001),
            transfer_fee_rate=self.config.get('transfer_fee_rate', 0.0),
            min_commission=self.config.get('min_commission', 5.0),
            lot_size=self.config.get('lot_size', 100),
            open_buy_resize_mode=self.config.get('open_buy_resize_mode', 'auto'),
            liquidity_cap_ratio=self.config.get('liquidity_cap_ratio', 0.8),
            daily_auction_liquidity=self._daily_auction_liquidity(),
            limit_pct_overrides=self.config.get('limit_pct_overrides'),
            open_buy_fill_mode=self.config.get('open_buy_fill_mode', 'open+slip'),
            open_buy_sizing=self.config.get('open_buy_sizing', 'limit_up'),
            t0_overrides=self.config.get('t0_overrides'),
            trading_days_per_year=self.config.get(
                'trading_days_per_year', 252
            ),
            risk_free_rate=self.config.get('risk_free_rate', 0.03),
            progress_callback=self._on_progress,
            stop_check=self._should_stop,
            universe_assumptions=universe_assumptions,
            universe_warnings=universe_warnings,
            data_root=self.config['data_root'],
            **self._price_mode_kwargs(),
        )

        # Parallel workers cannot call back into this GUI process while they
        # read data.  Freeze and require the complete direct-v1 source set for
        # the resolved universe before spawning them, then verify the same
        # generation after they return.  This applies equally to SPECIFIED and
        # ALL_MARKET; the latter used to publish rankable artifacts with no
        # market-file provenance.
        independent_fingerprint_tracker = DynamicMarketDataFingerprintTracker(
            self.config.get('data_root'),
            configured_symbols=(),
            price_mode=self.config.get('price_mode', 'dual'),
            frequency=self.config.get('freq', 'daily'),
            start_date=self.config.get('start_date'),
            end_date=self.config.get('end_date'),
        )
        independent_fingerprint_tracker.observe(symbols)
        evidence_symbols = symbols
        before_fingerprints = self._capture_market_data_fingerprints(
            evidence_symbols
        )

        # 运行回测 - 传递策略代码字符串
        result = runner.run(self.code)
        self._confirm_market_data_stable(
            before_fingerprints, evidence_symbols
        )
        verified_fingerprints = (
            independent_fingerprint_tracker.verify_stable()
        )
        self.market_data_fingerprints = tuple(sorted(
            {
                (source.kind, source.logical_path): source
                for source in (
                    self.market_data_fingerprints + verified_fingerprints
                )
            }.values(),
            key=lambda source: (source.kind, source.logical_path),
        ))
        self.config['realized_symbols'] = list(symbols)

        rankability_error = _independent_rankability_error(result)
        if rankability_error is not None:
            self.log.emit("回测未完成：未生成平均收益或排行榜")
            self.log.emit(rankability_error)

        # 输出汇总信息
        self.log.emit("=" * 40)
        self.log.emit("回测完成!")
        self.log.emit(f"成功: {result.success_count} | 失败: {result.failed_count}")
        self.log.emit(f"平均收益率: {result.avg_return * 100:.2f}%")
        self.log.emit(f"平均年化: {result.avg_annual_return * 100:.2f}%")
        self.log.emit(
            "平均夏普: " + (
                "N/A" if result.avg_sharpe is None
                else f"{result.avg_sharpe:.3f}"
            )
        )

        if result.top_performers:
            self.log.emit("-" * 40)
            self.log.emit("收益最高 Top 5:")
            for i, p in enumerate(result.top_performers[:5], 1):
                self.log.emit(f"  {i}. {p['symbol']}: {p['return']:.2f}%")

        self.finished.emit(result)

    def _compile_strategy(
        self,
        code: str,
        *,
        strategy_kind: str,
        strategy_params=None,
    ):
        """
        编译策略代码（委托 cli.runner.compile_strategy 单一真源）

        历史P1：此处曾是旧注入逻辑的逐字拷贝——CLI 修了"类式策略静默
        no-op"后，GUI 这份拷贝里同型缺陷仍存活。现统一委托，杜绝分叉。
        """
        from ..cli.runner import compile_strategy
        return compile_strategy(
            code,
            strategy_kind=strategy_kind,
            strategy_params=strategy_params,
        )

    def _on_progress(self, current: int, total: int, message: str):
        """进度回调 - 带时间节流"""
        now = time.time()
        is_final = (current == total)
        if is_final or (now - self._last_progress_time >= self._throttle_interval):
            self._last_progress_time = now
            self.progress.emit(current, total, message)

    def _on_debug_output(self, text: str):
        """调试输出回调 (策略中的 print)"""
        self.debug_output.emit(text)

    def _should_stop(self) -> bool:
        """检查是否应该停止"""
        return self._stop_requested

    def _on_daily_update(self, daily_data: dict):
        """每日更新回调 - 包含净值、交易、持仓（带时间节流）"""
        now = time.time()
        if now - self._last_daily_time < self._throttle_interval:
            return  # 节流：跳过本次更新
        self._last_daily_time = now

        if 'daily_values' in daily_data:
            # 传递 daily_values 和 total_days（用于 x 轴偏移计算）
            self.daily_update.emit({
                'values': daily_data['daily_values'],
                'total_days': daily_data.get('total_days', len(daily_data['daily_values']))
            })
        if 'trades' in daily_data:
            self.trades_update.emit(daily_data['trades'])
        if 'positions' in daily_data:
            self.positions_update.emit(daily_data['positions'])

    def stop(self):
        """请求停止"""
        self._stop_requested = True
        self.log.emit("停止请求已发送...")


class SaveWorker(QThread):
    """
    保存回测结果的工作线程
    """
    finished = Signal(str)   # 成功，返回保存路径
    error = Signal(str)      # 失败，返回错误信息

    def __init__(
        self,
        result,
        config: dict,
        code: str,
        *,
        results_root,
        market_data_fingerprints=(),
        signals_artifact_inputs=(),
        combo_artifact_inputs=(),
    ):
        super().__init__()
        self.result = result
        self.config = config
        self.code = code
        self.results_root = results_root
        self.market_data_fingerprints = market_data_fingerprints
        self.signals_artifact_inputs = signals_artifact_inputs
        self.combo_artifact_inputs = combo_artifact_inputs

    def run(self):
        try:
            folder_path = save_gui_run(
                self.result,
                self.config,
                self.code,
                results_root=self.results_root,
                market_data_fingerprints=self.market_data_fingerprints,
                signals_artifact_inputs=self.signals_artifact_inputs,
                combo_artifact_inputs=self.combo_artifact_inputs,
            )
            self.finished.emit(str(folder_path))
        except Exception as e:
            self.error.emit(str(e))


class LoadWorker(QThread):
    """
    加载回测结果的工作线程 (防止UI卡死)
    """
    finished = Signal(object)  # GuiLoadedRun
    error = Signal(str)

    def __init__(self, folder_path: str):
        super().__init__()
        self.folder_path = folder_path

    def run(self):
        try:
            loaded = load_gui_run(self.folder_path)
            self.finished.emit(loaded)
        except Exception as e:
            self.error.emit(str(e))
