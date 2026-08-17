"""
CLI 回测模块

提供命令行回测功能
"""

from .runner import run_backtest, compile_strategy
from .combo_bundle import (
    COMBO_BUNDLE_SCHEMA,
    COMBO_BUNDLE_SCHEMA_VERSION,
    ComboReplayBundle,
    load_combo_bundle,
)
from .signal_input import (
    SIGNAL_CSV_MAX_BYTES,
    SIGNAL_INPUT_SCHEMA,
    SIGNAL_INPUT_SCHEMA_VERSION,
    SIGNAL_MAX_SAFE_SHARES,
    SignalInstruction,
    SignalReplayInput,
    load_signal_replay_input,
)

__all__ = [
    'COMBO_BUNDLE_SCHEMA',
    'COMBO_BUNDLE_SCHEMA_VERSION',
    'ComboReplayBundle',
    'SIGNAL_CSV_MAX_BYTES',
    'SIGNAL_INPUT_SCHEMA',
    'SIGNAL_INPUT_SCHEMA_VERSION',
    'SIGNAL_MAX_SAFE_SHARES',
    'SignalInstruction',
    'SignalReplayInput',
    'compile_strategy',
    'load_combo_bundle',
    'load_signal_replay_input',
    'run_backtest',
]
