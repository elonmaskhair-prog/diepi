"""
UI 模块

PySide6 桌面应用

Phase 6 新架构:
- EditorScreen: 策略编辑器
- ResultScreen: 回测结果
- QStackedWidget 切换 Screen
"""

from .main_window import MainWindow, run_app
from .screens import EditorScreen, ResultScreen
from .widgets import CodeEditor, ConfigPanel, ApiPanel
from .worker import BacktestWorker

__all__ = [
    'MainWindow',
    'run_app',
    'EditorScreen',
    'ResultScreen',
    'CodeEditor',
    'ConfigPanel',
    'ApiPanel',
    'BacktestWorker',
]
