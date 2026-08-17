"""
主窗口

回测系统桌面应用主界面
使用 QStackedWidget 切换 Screen
"""

import sys
from pathlib import Path
from typing import Optional
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QStackedWidget, QMessageBox,
    QProgressDialog
)
from PySide6.QtGui import QPalette, QColor
from PySide6.QtCore import Qt, QTimer

from .screens.welcome_screen import WelcomeScreen
from .screens.editor_screen import EditorScreen
from .screens.result_screen import ResultScreen
from .widgets.history_dialog import FailedRunDialog, HistoryDialog
from .worker import (
    BacktestWorker,
    LoadWorker,
    SaveWorker,
    StrategyCheckResult,
    resolve_gui_results_root,
)
from .styles import Colors, get_app_stylesheet
from ..engine.parallel_runner import ParallelResult
from .. import __version__
from diepi.artifacts import RunProvenance
from diepi.runtime import RuntimePaths


def _worker_is_running(worker) -> bool:
    """Safely query a possibly already-deleted Qt worker."""
    if worker is None:
        return False
    try:
        return bool(worker.isRunning())
    except RuntimeError:
        return False


def _prepare_syntax_check_config(config: dict) -> dict:
    """Build a compile-only check that preserves the selected engine mode."""
    if not isinstance(config, dict):
        raise TypeError("syntax-check config must be a dict")
    check_config = dict(config)
    mode = check_config.get('mode', 'portfolio')
    if mode not in {'portfolio', 'independent'}:
        raise ValueError(f"unsupported syntax-check mode: {mode!r}")
    check_config['strategy_kind'] = (
        'single' if mode == 'independent' else 'portfolio'
    )
    # 编译与策略基类检查足以完成“语法检查”，不读取行情、不启动子进程。
    check_config['_syntax_only'] = True
    return check_config


def _syntax_check_summary(result) -> str:
    """Return a type-safe summary for the syntax-check completion dialog."""
    if isinstance(result, StrategyCheckResult):
        label = '组合策略' if result.strategy_kind == 'portfolio' else '单标的策略'
        return (
            f"契约: {label} ({result.strategy_kind})\n"
            f"策略类: {result.strategy_class_name}\n"
            "未读取行情、未执行回测"
        )
    if isinstance(result, ParallelResult):
        return (
            "5天独立测试结果:\n"
            f"成功: {result.success_count}\n"
            f"失败: {result.failed_count}\n"
            f"平均收益率: {result.avg_return * 100:.2f}%"
        )
    try:
        total_return = float(result.total_return)
        trade_count = int(result.trade_count)
    except (AttributeError, TypeError, ValueError) as exc:
        raise TypeError(
            "syntax-check worker returned an unsupported result type") from exc
    return (
        "5天回测结果:\n"
        f"总收益率: {total_return * 100:.2f}%\n"
        f"交易次数: {trade_count}"
    )


def _terminal_result_summary(result) -> str:
    """Return a concise, evidence-based terminal state for GUI chrome."""
    if isinstance(result, ParallelResult):
        return (
            "独立聚合 | "
            f"可排名: {'是' if result.is_rankable else '否'} | "
            f"成功 {result.success_count}/{result.total_symbols}"
        )
    contract = getattr(result, 'result_contract', None)
    if contract is None:
        return "LEGACY_UNCLASSIFIED | 可排名: 否"
    return (
        f"{contract.status.value} | "
        f"可排名: {'是' if contract.is_rankable else '否'}"
    )


def _request_worker_shutdown(workers, timeout_ms: int = 1000) -> bool:
    """Cooperatively stop workers; never destroy a still-running QThread."""
    active = [worker for worker in workers if _worker_is_running(worker)]
    for worker in active:
        stop = getattr(worker, 'stop', None)
        if callable(stop):
            stop()
    for worker in active:
        try:
            worker.wait(timeout_ms)
        except RuntimeError:
            continue
    return not any(_worker_is_running(worker) for worker in active)


class MainWindow(QMainWindow):
    """
    主窗口

    包含三个 Screen:
    - WelcomeScreen: 欢迎页面
    - EditorScreen: 策略编辑器
    - ResultScreen: 回测结果
    """

    def __init__(self, *, results_root=None, data_root=None):
        super().__init__()
        self.runtime_paths = RuntimePaths.resolve(
            results_root=results_root,
            data_root=data_root,
            require_data_root=False,
        )
        self.results_root: Path = self.runtime_paths.results_root
        self.data_root: Path = self.runtime_paths.data_root
        self.worker: Optional[BacktestWorker] = None
        self.load_worker: Optional[LoadWorker] = None
        self.save_worker: Optional[SaveWorker] = None
        self.load_progress: Optional[QProgressDialog] = None
        self.save_progress: Optional[QProgressDialog] = None
        self.failure_dialog: Optional[FailedRunDialog] = None
        self._current_code: str = ""  # 保存当前策略代码
        self._current_config: dict = {}  # 保存当前配置
        self._current_market_data_fingerprints = ()
        self._current_signals_artifact_inputs = ()
        self._current_combo_artifact_inputs = ()
        self._current_artifact_strategy_source = ""
        self._artifact_rerun_block_reason = None

        # UI 更新节流 - 避免频繁更新导致卡顿
        self._pending_progress = None  # (current, total, message)
        self._pending_daily_values = None
        self._chart_updating = False  # 防重叠标志：避免图表渲染重叠
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._flush_pending_updates)
        self._ui_timer.setInterval(500)  # 每500ms刷新一次UI（降低频率避免卡顿）

        self._init_ui()
        self._connect_signals()

    def _init_ui(self):
        """初始化界面"""
        self.setWindowTitle(f"dieΠ（带派） - 本地量化回测系统 v{__version__}")

        # 获取屏幕尺寸，设置窗口为纵向满屏、横向90%
        # Qt6 使用 primaryScreen 获取当前主屏幕几何信息。
        screen = QApplication.primaryScreen().availableGeometry()
        width = int(screen.width() * 0.9)  # 横向90%
        height = int(screen.height() * 0.9)

        self.setMinimumSize(960, 640)
        self.resize(width, height)
        self.move(screen.topLeft())

        # Stacked Widget 用于切换 Screen
        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        # Screen 0: 欢迎页面
        self.welcome_screen = WelcomeScreen()
        self.stack.addWidget(self.welcome_screen)

        # Screen 1: 编辑器
        self.editor_screen = EditorScreen(data_root=self.data_root)
        self.stack.addWidget(self.editor_screen)

        # Screen 2: 结果
        self.result_screen = ResultScreen()
        self.stack.addWidget(self.result_screen)

        # 默认显示欢迎页面
        self.stack.setCurrentWidget(self.welcome_screen)

        self.statusBar().showMessage("就绪")

    def _connect_signals(self):
        """连接信号"""
        # 欢迎页面信号
        self.welcome_screen.new_backtest.connect(self._on_new_backtest)
        self.welcome_screen.view_history.connect(self._on_view_history)

        # 编辑器信号
        self.editor_screen.run_backtest.connect(self._on_run_backtest)
        self.editor_screen.syntax_check.connect(self._on_syntax_check)
        self.editor_screen.back_to_home.connect(self._on_back_to_home)

        # 结果界面信号
        self.result_screen.back_to_editor.connect(self._on_back_to_editor)
        self.result_screen.stop_backtest.connect(self._on_stop_backtest)
        self.result_screen.save_result.connect(self._on_save_result)

    def _on_new_backtest(self):
        """新建回测 - 进入编辑器页面"""
        self._artifact_rerun_block_reason = None
        self.editor_screen.set_rerun_blocked(None)
        self.stack.setCurrentWidget(self.editor_screen)
        self.statusBar().showMessage("新建回测")

    def _on_view_history(self):
        """查看历史记录"""
        dialog = HistoryDialog(self, results_root=self.results_root)
        dialog.view_result.connect(self._on_load_result)
        dialog.view_code.connect(self._on_load_code)
        dialog.exec()

    def _on_back_to_home(self):
        """返回首页"""
        self.stack.setCurrentWidget(self.welcome_screen)
        self.statusBar().showMessage("就绪")

    def _on_load_result(self, folder_path: str):
        """加载历史记录的结果 (异步版)"""
        if _worker_is_running(self.load_worker):
            QMessageBox.warning(self, "加载中", "已有加载任务正在运行")
            return
        # 显示加载进度条
        self.load_progress = QProgressDialog("正在加载回测结果...", None, 0, 0, self)
        self.load_progress.setWindowTitle("加载中")
        self.load_progress.setWindowModality(Qt.WindowModal)
        self.load_progress.setMinimumDuration(0)
        self.load_progress.show()

        # 启动加载线程
        self.load_worker = LoadWorker(folder_path)
        self.load_worker.finished.connect(self._on_load_result_finished)
        self.load_worker.error.connect(self._on_load_error)
        self.load_worker.start()

    def _on_load_result_finished(self, loaded):
        """加载结果完成回调"""
        if self.load_progress:
            self.load_progress.close()
            self.load_progress = None

        if loaded.run_error is not None:
            self._show_failed_run_diagnostics(loaded)
            return
        if loaded.result is None:
            QMessageBox.warning(self, "加载失败", "无法加载回测结果")
            return

        try:
            # 保存代码和配置
            self._current_code = loaded.strategy_source
            self._current_config = loaded.config
            self._current_market_data_fingerprints = ()
            self._current_signals_artifact_inputs = ()
            self._current_combo_artifact_inputs = ()
            self._current_artifact_strategy_source = loaded.strategy_source

            # Result viewing and exact editor restoration are separate trust
            # boundaries.  A verified result remains viewable even when this
            # GUI cannot represent every execution parameter without drift.
            rerun_error = loaded.rerun_block_reason
            previous_config = self.editor_screen.get_config()
            previous_code = self.editor_screen.get_code()
            try:
                if loaded.config:
                    self.editor_screen.set_config(loaded.config)
                self.editor_screen.set_code(loaded.strategy_source)
            except Exception as exc:
                rerun_error = str(exc)
                try:
                    self.editor_screen.set_config(previous_config)
                    self.editor_screen.set_code(previous_code)
                except Exception:
                    # Execution is disabled below, so a failed best-effort
                    # rollback cannot turn partial state into a runnable one.
                    pass
            self._artifact_rerun_block_reason = rerun_error
            self.editor_screen.set_rerun_blocked(rerun_error)

            # 切换到结果页面并显示结果
            self.stack.setCurrentWidget(self.result_screen)
            self.result_screen.clear()
            self.result_screen.set_market_data_context(
                config=loaded.config,
                data_root=self.data_root,
                provenance=loaded.provenance,
                historical_artifact=True,
            )
            self.result_screen.display_result(loaded.result)
            self.result_screen.set_artifact_trust(
                artifact_format=loaded.artifact_format,
                verified=loaded.artifact_verified,
                rankable=loaded.is_rankable,
            )
            self.result_screen.set_running(False)

            if rerun_error:
                warning = (
                    "结果已载入并可以只读查看，但此 GUI 版本无法"
                    f"等价恢复该工件配置：\n{rerun_error}\n\n"
                    "为避免结果漂移，已禁止从编辑器重跑该工件。"
                )
                self.result_screen.add_log(warning.replace('\n', ' '))
                QMessageBox.warning(self, "结果已载入，无法等价重跑", warning)

            # 禁用保存按钮
            self.result_screen.save_btn.setEnabled(False)

            status = (
                "已加载历史记录: "
                f"{loaded.artifact_format} | "
                f"verified={'是' if loaded.artifact_verified else '否'} | "
                f"可排名={'是' if loaded.is_rankable else '否'}"
            )
            if rerun_error:
                status += " | 只读：此 GUI 版本无法等价重跑"
            self.statusBar().showMessage(status)
        except Exception as e:
            self._on_load_error(str(e))

    def _show_failed_run_diagnostics(self, loaded) -> None:
        """Show FAILED evidence without passing it to any result/ranking view."""

        self._current_code = loaded.strategy_source
        self._current_config = loaded.config
        self._current_market_data_fingerprints = ()
        self._current_signals_artifact_inputs = ()
        self._current_combo_artifact_inputs = ()
        self._current_artifact_strategy_source = loaded.strategy_source
        reason = loaded.rerun_block_reason or (
            'FAILED RunArtifact is diagnostic-only'
        )
        self._artifact_rerun_block_reason = reason
        self.editor_screen.set_rerun_blocked(reason)

        if self.failure_dialog is not None:
            self.failure_dialog.close()
        self.failure_dialog = FailedRunDialog(loaded, self)
        self.failure_dialog.open()
        self.statusBar().showMessage(
            "已加载失败运行诊断: RunArtifact v1 | verified=是 | "
            "状态=FAILED | 可排名=否"
        )

    def _on_load_error(self, error_msg: str):
        """加载失败回调"""
        if self.load_progress:
            self.load_progress.close()
            self.load_progress = None
        QMessageBox.critical(self, "加载失败", f"无法加载:\n{error_msg}")

    def _on_load_code(self, folder_path: str):
        """加载历史记录的代码 (异步版)"""
        if _worker_is_running(self.load_worker):
            QMessageBox.warning(self, "加载中", "已有加载任务正在运行")
            return
        self.load_progress = QProgressDialog("正在加载策略代码...", None, 0, 0, self)
        self.load_progress.setWindowTitle("加载中")
        self.load_progress.setWindowModality(Qt.WindowModal)
        self.load_progress.setMinimumDuration(0)
        self.load_progress.show()

        self.load_worker = LoadWorker(folder_path)
        self.load_worker.finished.connect(self._on_load_code_finished)
        self.load_worker.error.connect(self._on_load_error)
        self.load_worker.start()

    def _on_load_code_finished(self, loaded):
        """加载代码完成回调"""
        if self.load_progress:
            self.load_progress.close()
            self.load_progress = None

        if loaded.strategy_source is None:
            QMessageBox.warning(self, "加载失败", "无法加载策略代码")
            return

        previous_config = self.editor_screen.get_config()
        previous_code = self.editor_screen.get_code()
        try:
            if loaded.rerun_block_reason:
                raise ValueError(loaded.rerun_block_reason)
            if loaded.config:
                self.editor_screen.set_config(loaded.config)
            # 模式必须先切换，再把文件内容写入对应草稿。
            self.editor_screen.set_code(loaded.strategy_source)
        except Exception as exc:
            reason = str(exc)
            try:
                self.editor_screen.set_config(previous_config)
                self.editor_screen.set_code(previous_code)
            except Exception:
                pass
            self._artifact_rerun_block_reason = reason
            self.editor_screen.set_rerun_blocked(reason)
            self.stack.setCurrentWidget(self.editor_screen)
            QMessageBox.warning(
                self,
                "代码工件无法等价重跑",
                "此 GUI 版本无法无损恢复该工件配置：\n"
                f"{reason}\n\n编辑器原状态已恢复，并禁止该工件重跑。",
            )
            self.statusBar().showMessage(
                "工件代码未应用：此 GUI 版本无法等价重跑"
            )
            return

        self._artifact_rerun_block_reason = None
        self.editor_screen.set_rerun_blocked(None)
        self.stack.setCurrentWidget(self.editor_screen)
        self.statusBar().showMessage(
            f"已加载策略代码: {loaded.artifact_format}"
        )

    def _on_save_result(self):
        """保存回测结果"""
        if _worker_is_running(self.save_worker):
            QMessageBox.warning(self, "保存中", "已有保存任务正在运行")
            return
        result = self.result_screen.get_current_result()
        if result is None:
            QMessageBox.warning(self, "保存失败", "没有可保存的回测结果")
            return

        # 显示保存进度对话框 (无限模式)
        self.save_progress = QProgressDialog("正在保存回测记录...", None, 0, 0, self)
        self.save_progress.setWindowTitle("保存中")
        self.save_progress.setWindowModality(Qt.WindowModal)
        self.save_progress.setMinimumDuration(0)
        self.save_progress.setCancelButton(None)
        
        # 创建保存线程
        self.save_worker = SaveWorker(
            result,
            self._current_config,
            self._current_artifact_strategy_source,
            results_root=self.results_root,
            market_data_fingerprints=(
                self._current_market_data_fingerprints
            ),
            signals_artifact_inputs=self._current_signals_artifact_inputs,
            combo_artifact_inputs=self._current_combo_artifact_inputs,
        )
        self.save_worker.finished.connect(self._on_save_finished)
        self.save_worker.error.connect(self._on_save_error)
        self.result_screen.save_btn.setEnabled(False)
        
        self.save_progress.show()
        self.save_worker.start()

    def _on_save_finished(self, folder_path: str):
        """保存完成回调"""
        if self.save_progress:
            self.save_progress.close()
            self.save_progress = None
            
        QMessageBox.information(
            self, "保存成功",
            f"回测结果已保存到:\n{folder_path}"
        )
        # 保存后禁用按钮，防止重复保存
        self.result_screen.save_btn.setEnabled(False)

    def _on_save_error(self, error_msg: str):
        """保存失败回调"""
        if self.save_progress:
            self.save_progress.close()
            self.save_progress = None
        if self.result_screen.get_current_result() is not None:
            self.result_screen.save_btn.setEnabled(
                True)
            
        QMessageBox.critical(self, "保存失败", f"无法保存回测结果:\n{error_msg}")

    def _on_run_backtest(self, code: str, config: dict):
        """运行回测"""
        # 保存当前代码和配置
        self._current_code = code
        self._current_config = config
        self._current_market_data_fingerprints = ()
        self._current_signals_artifact_inputs = ()
        self._current_combo_artifact_inputs = ()
        self._current_artifact_strategy_source = code

        # 切换到结果界面
        self.stack.setCurrentWidget(self.result_screen)
        self.result_screen.clear()
        self.result_screen.set_market_data_context(
            config=config,
            data_root=config.get('data_root'),
            provenance=None,
            historical_artifact=False,
        )
        self.result_screen.set_running(True)
        self.result_screen.add_log("开始回测...")
        self.editor_screen.set_running(True)
        self.editor_screen.clear_debug()  # 清空调试面板

        # 创建并启动工作线程
        self.worker = BacktestWorker(code, config)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._on_log)
        self.worker.finished.connect(self._on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.daily_update.connect(self._on_daily_update)
        self.worker.trades_update.connect(self._on_trades_update)
        self.worker.positions_update.connect(self._on_positions_update)
        self.worker.debug_output.connect(self._on_debug_output)  # 调试输出
        self.worker.start()

        # 启动UI更新定时器
        self._ui_timer.start()
        self.statusBar().showMessage("回测运行中...")

    def _on_syntax_check(self, code: str, config: dict):
        """语法检查"""
        # 保存当前代码和配置
        self._current_code = code
        self._current_config = dict(config)
        check_config = _prepare_syntax_check_config(config)

        self.stack.setCurrentWidget(self.result_screen)
        self.result_screen.clear()
        self.result_screen.set_running(True)
        self.result_screen.add_log("语法检查模式: 按当前模式编译策略契约...")
        self.editor_screen.set_running(True)
        self.editor_screen.clear_debug()  # 清空调试面板

        self.worker = BacktestWorker(code, check_config)
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(self._on_log)
        self.worker.finished.connect(self._on_check_finished)
        self.worker.error.connect(self._on_error)
        self.worker.debug_output.connect(self._on_debug_output)  # 调试输出
        self.worker.start()

        # 启动UI更新定时器
        self._ui_timer.start()
        self.statusBar().showMessage("语法检查中...")

    def _on_back_to_editor(self):
        """返回编辑器"""
        self.stack.setCurrentWidget(self.editor_screen)
        self.statusBar().showMessage("就绪")

    def _on_stop_backtest(self):
        """停止回测"""
        if self.worker:
            self.worker.stop()
            self.result_screen.add_log("正在停止...")
            self.statusBar().showMessage("正在停止...")

    def _on_progress(self, current: int, total: int, message: str):
        """进度更新 - 存储待更新数据，由定时器统一刷新"""
        self._pending_progress = (current, total, message)

    def _on_log(self, message: str):
        """日志"""
        self.result_screen.add_log(message)

    def _on_debug_output(self, text: str):
        """调试输出 (策略中的 print)"""
        self.editor_screen.append_debug(text)

    def _on_daily_update(self, daily_data: dict):
        """每日净值更新 - 存储待更新数据，由定时器统一刷新"""
        if daily_data:
            self._pending_daily_values = daily_data  # {values, total_days}

    def _flush_pending_updates(self):
        """定时刷新待更新的UI（由QTimer调用）"""
        # 窗口最小化时跳过UI更新，避免卡顿
        if self.isMinimized():
            return

        # 刷新进度（轻量操作，直接执行）
        if self._pending_progress:
            current, total, message = self._pending_progress
            self.result_screen.update_progress(current, total, message)
            self.statusBar().showMessage(message)
            self._pending_progress = None

        # 刷新图表（带防重叠保护，避免渲染堆积）
        if self._pending_daily_values and not self._chart_updating:
            self._chart_updating = True
            try:
                self.result_screen.update_chart_realtime(self._pending_daily_values)
            finally:
                self._chart_updating = False
                self._pending_daily_values = None

    def _on_trades_update(self, trades: list):
        """交易记录更新"""
        if trades:
            self.result_screen.update_trades_realtime(trades)

    def _on_positions_update(self, positions: list):
        """持仓更新"""
        if positions:
            self.result_screen.update_positions_realtime(positions)

    def _on_finished(self, result):
        """回测完成"""
        # 停止UI更新定时器并刷新最后的更新
        self._ui_timer.stop()
        self._flush_pending_updates()

        fingerprints = tuple(
            getattr(self.worker, 'market_data_fingerprints', ()) or ()
        )
        combo_inputs = tuple(
            getattr(self.worker, 'combo_artifact_inputs', ()) or ()
        )
        signals_inputs = tuple(
            getattr(self.worker, 'signals_artifact_inputs', ()) or ()
        )
        executed_source = getattr(
            self.worker, 'artifact_strategy_source', self._current_code
        )
        self._current_market_data_fingerprints = fingerprints
        self._current_signals_artifact_inputs = signals_inputs
        self._current_combo_artifact_inputs = combo_inputs
        self._current_artifact_strategy_source = executed_source
        self.result_screen.set_market_data_context(
            config=self._current_config,
            data_root=self._current_config.get('data_root'),
            provenance=RunProvenance.build(sources=fingerprints),
            historical_artifact=False,
            fingerprint_required=True,
        )

        terminal = _terminal_result_summary(result)
        self.result_screen.add_log(f"回测终态: {terminal}")
        self.result_screen.display_result(result)
        # 先 display_result 设置 _current_result，再 set_running 启用保存按钮
        self.result_screen.set_running(False)
        self.editor_screen.set_running(False)
        self.statusBar().showMessage(f"回测结束: {terminal}")

    def _on_check_finished(self, result):
        """语法检查完成"""
        # 停止UI更新定时器
        self._ui_timer.stop()
        self._flush_pending_updates()

        self.result_screen.set_running(False)
        self.editor_screen.set_running(False)
        self.result_screen.add_log("语法检查通过!")

        try:
            summary = _syntax_check_summary(result)
        except Exception as exc:
            self._on_error(f"语法检查结果无效: {exc}")
            return

        QMessageBox.information(
            self, "语法检查",
            "语法与策略契约检查通过。\n\n" + summary
        )

        self.stack.setCurrentWidget(self.editor_screen)
        self.statusBar().showMessage("语法检查通过")

    def _on_error(self, error_msg: str):
        """错误"""
        # 停止UI更新定时器
        self._ui_timer.stop()
        self._flush_pending_updates()

        self.result_screen.set_running(False)
        self.editor_screen.set_running(False)
        self.result_screen.add_log(f"错误: {error_msg}")

        QMessageBox.critical(self, "错误", f"回测失败:\n\n{error_msg}")

        self.statusBar().showMessage("回测失败")

    def closeEvent(self, event):
        """关闭窗口时停止工作线程"""
        self._ui_timer.stop()
        if not _request_worker_shutdown(
                (self.worker, self.load_worker, self.save_worker)):
            event.ignore()
            self.statusBar().showMessage("后台任务仍在停止，请稍后再次关闭")
            QMessageBox.warning(
                self,
                "正在停止后台任务",
                "后台任务尚未安全退出，窗口将保持打开。请稍后再次关闭。",
            )
            return
        event.accept()


def apply_dark_theme(app: QApplication):
    """应用深色主题"""
    app.setStyle('Fusion')

    # 设置调色板
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(Colors.BG_PRIMARY))
    palette.setColor(QPalette.WindowText, QColor(Colors.TEXT_PRIMARY))
    palette.setColor(QPalette.Base, QColor(Colors.BG_DARK))
    palette.setColor(QPalette.AlternateBase, QColor(Colors.BG_SECONDARY))
    palette.setColor(QPalette.ToolTipBase, QColor(Colors.BG_SECONDARY))
    palette.setColor(QPalette.ToolTipText, QColor(Colors.TEXT_PRIMARY))
    palette.setColor(QPalette.Text, QColor(Colors.TEXT_PRIMARY))
    palette.setColor(QPalette.Button, QColor(Colors.BG_SECONDARY))
    palette.setColor(QPalette.ButtonText, QColor(Colors.TEXT_PRIMARY))
    palette.setColor(QPalette.BrightText, QColor(Colors.ACCENT_RED))
    palette.setColor(QPalette.Link, QColor(Colors.ACCENT_BLUE))
    palette.setColor(QPalette.Highlight, QColor(Colors.ACCENT_BLUE))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))

    # 禁用状态颜色
    palette.setColor(QPalette.Disabled, QPalette.WindowText, QColor(Colors.TEXT_MUTED))
    palette.setColor(QPalette.Disabled, QPalette.Text, QColor(Colors.TEXT_MUTED))
    palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(Colors.TEXT_MUTED))

    app.setPalette(palette)

    # 应用全局样式表
    app.setStyleSheet(get_app_stylesheet())


def run_app(*, results_root=None, data_root=None):
    """运行应用"""
    app = QApplication(sys.argv)
    app.setApplicationName("dieΠ")
    app.setOrganizationName("diepi")

    # 应用深色主题
    apply_dark_theme(app)

    window = MainWindow(results_root=results_root, data_root=data_root)
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    run_app()
