"""
策略编辑器界面

Screen 1: 策略编辑和配置
三栏布局: 配置面板 | 代码编辑器 | 接口速查
"""

import json
import math
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSplitter,
    QLabel, QFrame, QMessageBox, QFileDialog, QPlainTextEdit
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from pathlib import Path

from ..widgets.code_editor import CodeEditor
from ..widgets.config_panel import ConfigPanel
from ..widgets.api_panel import ApiPanel
from ..styles import Colors, Fonts, Styles


class EditorScreen(QWidget):
    """
    策略编辑器界面

    三栏布局:
    - 左侧: 配置面板 (股票池/日期/资金/成本)
    - 中间: 代码编辑器
    - 右侧: 接口速查

    Signals:
        run_backtest: 开始回测 (code, config)
        syntax_check: 语法检查 (code, config)
        back_to_home: 返回首页
    """

    run_backtest = Signal(str, dict)
    syntax_check = Signal(str, dict)
    back_to_home = Signal()

    @staticmethod
    def _find_public_sample_data_root():
        """Locate the source/sdist-only public market-data slice, if present."""

        candidates = (
            Path(__file__).resolve().parents[4] / 'examples' / 'market_data_v1' / 'data',
            Path.cwd() / 'examples' / 'market_data_v1' / 'data',
        )
        seen = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if (
                (resolved / 'diepi_dataset.json').is_file()
                and (resolved / 'parquet' / 'timeseries').is_dir()
            ):
                return resolved
        return None

    def __init__(self, parent=None, *, data_root=None):
        super().__init__(parent)
        self._initial_data_root = data_root
        self._running = False
        self._rerun_block_reason = None
        self._signals_replay_input = None
        self._signals_snapshot_path = None
        self._combo_replay_bundle = None
        self._combo_snapshot_path = None
        self._init_ui()
        self._active_strategy_kind = 'portfolio'
        self._strategy_drafts = {
            'portfolio': self.code_editor.get_code(),
            'single': CodeEditor.SINGLE_TEMPLATE,
        }
        self.config_panel.strategy_kind_changed.connect(
            self._on_strategy_kind_changed)
        self.config_panel.input_mode_changed.connect(
            self._on_input_mode_changed)
        self.config_panel.signals_file_edit.textChanged.connect(
            self._on_signals_input_path_changed
        )
        self.config_panel.combo_bundle_edit.textChanged.connect(
            self._on_combo_input_path_changed
        )
        self._on_input_mode_changed(
            self.config_panel.input_mode_combo.currentData()
        )
        self._update_strategy_kind_label()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 使用 Splitter 分割三个面板
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setHandleWidth(3)

        # ==================== 左侧配置面板 ====================
        self.config_panel = ConfigPanel(data_root=self._initial_data_root)
        self.config_panel.setMinimumWidth(320)  # 增加最小宽度以显示完整内容
        # 移除最大宽度限制，允许自由拖动
        self.splitter.addWidget(self.config_panel)
        self.splitter.setCollapsible(0, False) # 禁止折叠左侧面板

        # ==================== 中间代码编辑区 ====================
        center_panel = QWidget()
        center_layout = QVBoxLayout(center_panel)
        center_layout.setContentsMargins(12, 12, 12, 12)
        center_layout.setSpacing(10)

        # 顶部工具栏 (文件操作)
        file_toolbar = QFrame()
        file_toolbar_layout = QHBoxLayout(file_toolbar)
        file_toolbar_layout.setContentsMargins(0, 0, 0, 8)

        # 标题
        self.input_title_label = QLabel("策略代码")
        self.input_title_label.setStyleSheet(f"""
            font-size: 16px;
            font-weight: 600;
            color: {Colors.TEXT_PRIMARY};
        """)
        file_toolbar_layout.addWidget(self.input_title_label)

        self.strategy_kind_label = QLabel()
        self.strategy_kind_label.setStyleSheet(f"""
            color: {Colors.ACCENT_BLUE};
            background-color: {Colors.BG_TERTIARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 4px;
            padding: 2px 8px;
            font-size: 12px;
        """)
        file_toolbar_layout.addWidget(self.strategy_kind_label)

        file_toolbar_layout.addStretch()

        self._public_sample_data_root = self._find_public_sample_data_root()
        self.public_sample_btn = QPushButton("载入公开样例")
        self.public_sample_btn.setStyleSheet(Styles.BTN_SECONDARY)
        if self._public_sample_data_root is None:
            self.public_sample_btn.setToolTip(
                "公开真实行情切片只随源码和 sdist 提供；wheel 不包含行情数据"
            )
            self.public_sample_btn.setEnabled(False)
        else:
            self.public_sample_btn.setToolTip(
                "载入四证券真实切片中的股票+ETF、2026 上半年和 dual 配置"
            )
        self.public_sample_btn.setCursor(Qt.PointingHandCursor)
        self.public_sample_btn.clicked.connect(self._on_load_public_sample)
        file_toolbar_layout.addWidget(self.public_sample_btn)

        self.example_btn = QPushButton("载入 MA5/20 策略")
        self.example_btn.setStyleSheet(Styles.BTN_SECONDARY)
        self.example_btn.setToolTip(
            "只载入内置策略代码并配置 raw/容量假设；不会配置数据根、标的或日期"
        )
        self.example_btn.setCursor(Qt.PointingHandCursor)
        self.example_btn.clicked.connect(self._on_load_ma_example)
        file_toolbar_layout.addWidget(self.example_btn)

        # 打开按钮
        self.open_btn = QPushButton("打开")
        self.open_btn.setStyleSheet(Styles.BTN_SECONDARY)
        self.open_btn.setToolTip("打开本地策略文件 (.py)")
        self.open_btn.setCursor(Qt.PointingHandCursor)
        self.open_btn.clicked.connect(self._on_open)
        file_toolbar_layout.addWidget(self.open_btn)

        # 保存按钮
        self.save_btn = QPushButton("保存")
        self.save_btn.setStyleSheet(Styles.BTN_SECONDARY)
        self.save_btn.setToolTip("保存策略到本地文件")
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self._on_save)
        file_toolbar_layout.addWidget(self.save_btn)

        center_layout.addWidget(file_toolbar)

        # 代码编辑器和调试面板的垂直分割
        editor_splitter = QSplitter(Qt.Vertical)
        editor_splitter.setHandleWidth(3)

        # 代码编辑器
        self.code_editor = CodeEditor()
        editor_splitter.addWidget(self.code_editor)

        # 调试输出面板
        debug_panel = QWidget()
        debug_layout = QVBoxLayout(debug_panel)
        debug_layout.setContentsMargins(0, 8, 0, 0)
        debug_layout.setSpacing(4)

        # 调试面板标题栏
        debug_header = QHBoxLayout()
        debug_label = QLabel("调试输出")
        debug_label.setStyleSheet(f"""
            font-size: 13px;
            font-weight: 600;
            color: {Colors.TEXT_SECONDARY};
        """)
        debug_header.addWidget(debug_label)
        debug_header.addStretch()

        # 清空按钮
        self.clear_debug_btn = QPushButton("清空")
        self.clear_debug_btn.setStyleSheet(Styles.BTN_SMALL)
        self.clear_debug_btn.setCursor(Qt.PointingHandCursor)
        self.clear_debug_btn.clicked.connect(self._on_clear_debug)
        debug_header.addWidget(self.clear_debug_btn)

        debug_layout.addLayout(debug_header)

        # 调试输出跟随系统 DPI，避免在常见屏幕上挤占编辑空间。
        self.debug_output = QPlainTextEdit()
        self.debug_output.setReadOnly(True)
        self.debug_output.setFont(QFont('Cascadia Code', 11))
        self.debug_output.setMaximumBlockCount(1000)  # 限制行数
        self.debug_output.setStyleSheet(f"""
            QPlainTextEdit {{
                background-color: {Colors.BG_DARK};
                color: {Colors.ACCENT_GREEN};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 12px;
                font-family: 'Cascadia Code', 'Consolas', monospace;
                font-size: 12px;
            }}
        """)
        self.debug_output.setPlaceholderText("策略中的 print() 输出会显示在这里...")
        debug_layout.addWidget(self.debug_output)

        editor_splitter.addWidget(debug_panel)

        # 设置编辑器和调试面板的初始比例 (代码:调试 = 70%:30%)
        editor_splitter.setSizes([500, 180])

        center_layout.addWidget(editor_splitter, stretch=1)

        # 底部工具栏
        toolbar = QFrame()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 10, 0, 0)
        toolbar_layout.setSpacing(12)

        # 返回首页按钮
        self.home_btn = QPushButton("返回首页")
        self.home_btn.setStyleSheet(Styles.BTN_SECONDARY)
        self.home_btn.setCursor(Qt.PointingHandCursor)
        self.home_btn.clicked.connect(self.back_to_home.emit)
        toolbar_layout.addWidget(self.home_btn)

        # 重置按钮
        self.reset_btn = QPushButton("重置代码")
        self.reset_btn.setStyleSheet(Styles.BTN_SECONDARY)
        self.reset_btn.setCursor(Qt.PointingHandCursor)
        self.reset_btn.clicked.connect(self._on_reset)
        toolbar_layout.addWidget(self.reset_btn)

        toolbar_layout.addStretch()

        # 语法检查按钮
        self.check_btn = QPushButton("语法检查")
        self.check_btn.setStyleSheet(Styles.BTN_SECONDARY)
        self.check_btn.setToolTip("按当前模式编译并校验策略契约，不运行回测")
        self.check_btn.setCursor(Qt.PointingHandCursor)
        self.check_btn.clicked.connect(self._on_syntax_check)
        toolbar_layout.addWidget(self.check_btn)

        # 运行按钮 - 绿色突出
        self.run_btn = QPushButton("开始回测")
        self.run_btn.setStyleSheet(Styles.BTN_PRIMARY)
        self.run_btn.setCursor(Qt.PointingHandCursor)
        self.run_btn.clicked.connect(self._on_run)
        toolbar_layout.addWidget(self.run_btn)

        center_layout.addWidget(toolbar)

        self.splitter.addWidget(center_panel)

        # ==================== 右侧接口速查 ====================
        self.api_panel = ApiPanel()
        self.api_panel.setMinimumWidth(200)
        # 不设置最大宽度，允许自由拖动
        self.api_panel.api_clicked.connect(self._on_api_clicked)
        self.splitter.addWidget(self.api_panel)

        # 设置初始比例 (配置:代码:接口 = 320:剩余:300)
        self.splitter.setSizes([320, 800, 300])

        layout.addWidget(self.splitter)

    def _on_api_clicked(self, code: str):
        """点击接口时插入代码"""
        cursor = self.code_editor.textCursor()
        cursor.insertText(code)
        self.code_editor.setFocus()

    def _update_strategy_kind_label(self):
        input_mode = self.config_panel.input_mode_combo.currentData()
        if input_mode == 'signals':
            text = "signals 内置回放 · PortfolioStrategy"
        elif input_mode == 'combo':
            text = "combo 内置回放 · PortfolioStrategy"
        else:
            text = (
                "组合策略 · PortfolioStrategy"
                if self._active_strategy_kind == 'portfolio'
                else "单标的策略 · Strategy"
            )
        self.strategy_kind_label.setText(text)

    def _on_input_mode_changed(self, input_mode: str):
        """Make it explicit when the editor draft is not executable input."""

        is_strategy = input_mode == 'strategy'
        self.code_editor.setReadOnly(not is_strategy)
        self.input_title_label.setText(
            "策略代码" if is_strategy else "内置回放策略（编辑器草稿不执行）"
        )
        enabled = is_strategy and not self._running
        self.open_btn.setEnabled(enabled)
        self.save_btn.setEnabled(enabled)
        self.example_btn.setEnabled(enabled)
        self.public_sample_btn.setEnabled(
            enabled and self._public_sample_data_root is not None
        )
        self.reset_btn.setEnabled(enabled)
        self._update_strategy_kind_label()

    def _on_strategy_kind_changed(self, strategy_kind: str):
        """Preserve an independent draft for each incompatible contract."""
        if strategy_kind == self._active_strategy_kind:
            return
        if strategy_kind not in {'portfolio', 'single'}:
            raise ValueError(f"unsupported strategy_kind: {strategy_kind!r}")

        previous = self._active_strategy_kind
        self._strategy_drafts[previous] = self.code_editor.get_code()
        self._active_strategy_kind = strategy_kind
        target = self._strategy_drafts.setdefault(
            strategy_kind,
            CodeEditor.template_for_strategy_kind(strategy_kind),
        )
        self.code_editor.set_code(target)
        self._update_strategy_kind_label()
        self.append_debug(
            "策略模式已切换；上一模式代码已保留，当前显示"
            f"{'组合' if strategy_kind == 'portfolio' else '单标的'}策略草稿。"
        )

    def _on_open(self):
        """打开策略文件"""
        self.config_panel.input_mode_combo.setCurrentIndex(
            self.config_panel.input_mode_combo.findData('strategy')
        )
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "打开策略文件",
            "",
            "Python Files (*.py);;All Files (*)"
        )
        if file_path:
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    code = f.read()
                self.set_code(code)
                self.append_debug(f"已打开: {file_path}")
            except Exception as e:
                QMessageBox.warning(self, "打开失败", f"无法打开文件:\n{e}")

    def _on_load_ma_example(self):
        """Load the packaged canonical example through its public catalog."""

        reply = QMessageBox.question(
            self,
            "载入 MA5/20 策略",
            "这会覆盖当前组合策略代码，并配置 raw 价格模式及开/收盘容量假设；"
            "不会修改数据根、标的或日期。继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        from diepi.examples import get_example

        self.config_panel.mode_portfolio.setChecked(True)
        self.config_panel.set_config({
            'mode': 'portfolio',
            'input_mode': 'strategy',
            'signals_file': None,
            'signals_format': None,
            'price_mode': 'raw',
            'combo_bundle': None,
            'combo_tag': None,
            'daily_open_previous_day_ratio': 0.1,
            'daily_close_previous_day_ratio': 0.1,
        })
        source = get_example('ma-cross').read_source()
        self.set_code(source)
        self.append_debug(
            "已从 diepi.examples 载入 MA5/MA20 严格穿越示例；"
            "价格模式为 raw，开/收盘容量假设均为前日成交额 10%。"
            "数据根、标的和日期保持不变，请按自己的数据检查。"
        )

    def _on_load_public_sample(self):
        """Load the complete source/sdist public-data onboarding preset."""

        data_root = self._public_sample_data_root
        if data_root is None:
            QMessageBox.information(
                self,
                "公开样例不可用",
                "当前安装不包含真实行情切片。请使用源码或 sdist，并在项目根目录"
                "打开 GUI；wheel 按发布边界不附带行情数据。",
            )
            return
        reply = QMessageBox.question(
            self,
            "载入公开样例",
            "这会覆盖当前组合策略代码，并载入公开真实切片的股票+ETF、日期、"
            "dual 价格轨和执行假设。继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        from diepi.examples import get_example

        self.config_panel.mode_portfolio.setChecked(True)
        self.config_panel.set_config({
            'data_root': str(data_root),
            'mode': 'portfolio',
            'input_mode': 'strategy',
            'signals_file': None,
            'signals_format': None,
            'combo_bundle': None,
            'combo_tag': None,
            'pool_source': 'specified',
            'symbols': ['600000.SH', '510300.SH'],
            'start_date': '20260101',
            'end_date': '20260630',
            'freq': 'daily',
            'price_mode': 'dual',
            'stamp_duty': 'auto',
            'daily_open_previous_day_ratio': 0.1,
            'daily_close_previous_day_ratio': 0.1,
        })
        self.set_code(get_example('ma-cross').read_source())
        self.append_debug(
            "已载入公开真实行情切片：600000.SH + 510300.SH，"
            "20260101..20260630，dual，印花税 auto；"
            "开/收盘容量假设均为前日成交额 10%。"
        )

    def _on_save(self):
        """保存策略文件"""
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "保存策略文件",
            "my_strategy.py",
            "Python Files (*.py);;All Files (*)"
        )
        if file_path:
            try:
                code = self.code_editor.get_code()
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(code)
                self.append_debug(f"已保存: {file_path}")
                QMessageBox.information(self, "保存成功", f"策略已保存到:\n{file_path}")
            except Exception as e:
                QMessageBox.warning(self, "保存失败", f"无法保存文件:\n{e}")

    def _on_clear_debug(self):
        """清空调试输出"""
        self.debug_output.clear()

    def append_debug(self, text: str):
        """追加调试输出"""
        self.debug_output.appendPlainText(text)
        # 滚动到底部
        scrollbar = self.debug_output.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear_debug(self):
        """清空调试输出"""
        self.debug_output.clear()

    def _on_reset(self):
        """重置代码"""
        reply = QMessageBox.question(
            self, "确认重置",
            "确定要重置代码为默认模板吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            self.code_editor.reset_to_template(self._active_strategy_kind)
            self._strategy_drafts[
                self._active_strategy_kind] = self.code_editor.get_code()

    def _on_syntax_check(self):
        """语法检查"""
        code = self.code_editor.get_code()
        config = self.get_config()

        # 验证配置
        if not self._validate_config(config):
            return

        self.syntax_check.emit(code, config)

    def _on_run(self):
        """运行回测"""
        code = self.code_editor.get_code()
        config = self.get_config()

        # 验证配置
        if not self._validate_config(config):
            return

        self.run_backtest.emit(code, config)

    def _validate_config(self, config: dict) -> bool:
        """验证配置"""
        data_root_text = str(config.get('data_root', '')).strip()
        if not data_root_text:
            QMessageBox.warning(
                self, "错误",
                "请选择本地数据根目录。可先运行 `diepi doctor` 检查路径。",
            )
            return False
        data_root = Path(data_root_text).expanduser()
        if not data_root.is_dir():
            QMessageBox.warning(
                self, "错误",
                "本地数据根目录不存在或不是目录:\n"
                f"{data_root}\n\n可运行 `diepi doctor --data-root <目录>` 诊断。",
            )
            return False
        config['data_root'] = str(data_root.resolve())

        try:
            lot_size = config.get('lot_size', 100)
            if type(lot_size) is not int or lot_size <= 0:
                raise ValueError("基础手数必须是正整数")
            liquidity = config.get('liquidity_cap_ratio', 0.8)
            if (
                type(liquidity) not in (int, float)
                or not math.isfinite(float(liquidity))
                or not 0.0 <= float(liquidity) <= 1.0
            ):
                raise ValueError("单根容量比例必须在 [0,1]")
            config['liquidity_cap_ratio'] = float(liquidity)
            for key, allowed in (
                ('open_buy_resize_mode', {'auto', 'legacy'}),
                ('open_buy_fill_mode', {'open+slip', 'open'}),
                ('open_buy_sizing', {'limit_up', 'fill'}),
            ):
                if config.get(key) not in allowed:
                    raise ValueError(f"{key} 取值无效")

            limit_overrides = config.get('limit_pct_overrides')
            if isinstance(limit_overrides, str):
                try:
                    limit_overrides = json.loads(limit_overrides)
                except json.JSONDecodeError as exc:
                    raise ValueError("涨跌停覆盖必须是有效 JSON") from exc
            if limit_overrides is not None:
                if type(limit_overrides) is not dict:
                    raise ValueError("涨跌停覆盖必须是 JSON 对象")
                normalized_limits = {}
                for symbol, value in limit_overrides.items():
                    if type(symbol) is not str or not symbol.strip():
                        raise ValueError("涨跌停覆盖代码不能为空")
                    if (
                        type(value) not in (int, float)
                        or not math.isfinite(float(value))
                        or not 0.0 < float(value) <= 1.0
                    ):
                        raise ValueError("涨跌停覆盖幅度必须在 (0,1]")
                    normalized_symbol = symbol.strip()
                    if normalized_symbol in normalized_limits:
                        raise ValueError("涨跌停覆盖代码去空格后重复")
                    normalized_limits[normalized_symbol] = float(value)
                limit_overrides = normalized_limits or None
            config['limit_pct_overrides'] = limit_overrides

            t0_overrides = config.get('t0_overrides')
            if t0_overrides is not None:
                if type(t0_overrides) is not list or any(
                    type(value) is not str or not value.strip()
                    for value in t0_overrides
                ):
                    raise ValueError("T+0 覆盖必须是非空代码列表")
                t0_overrides = list(dict.fromkeys(
                    value.strip() for value in t0_overrides
                ))
            config['t0_overrides'] = t0_overrides or None

            trading_days = config.get('trading_days_per_year', 252)
            if type(trading_days) is not int or trading_days <= 0:
                raise ValueError("年化交易日必须是正整数")
            risk_free_rate = config.get('risk_free_rate', 0.03)
            if (
                type(risk_free_rate) not in (int, float)
                or not math.isfinite(float(risk_free_rate))
            ):
                raise ValueError("无风险利率必须是有限数值")
            config['risk_free_rate'] = float(risk_free_rate)
        except ValueError as exc:
            QMessageBox.warning(self, "执行假设无效", str(exc))
            return False

        input_mode = config.get('input_mode', 'strategy')
        if input_mode not in {'strategy', 'signals', 'combo'}:
            QMessageBox.warning(self, "错误", f"未知回测输入模式: {input_mode!r}")
            return False
        if input_mode != 'strategy' and config.get('mode') != 'portfolio':
            QMessageBox.warning(
                self,
                "错误",
                "signals CSV 与冻结 combo 只支持组合投资模式；"
                "请先切换到组合投资。",
            )
            return False

        raw_strategy_params = config.get('strategy_params')
        if input_mode == 'strategy':
            if isinstance(raw_strategy_params, str):
                try:
                    raw_strategy_params = json.loads(raw_strategy_params)
                except json.JSONDecodeError as exc:
                    QMessageBox.warning(
                        self,
                        "策略参数无效",
                        f"策略参数必须是有效 JSON 对象: {exc}",
                    )
                    return False
            try:
                from ..worker import _normalize_custom_strategy_params

                normalized_params = _normalize_custom_strategy_params(
                    raw_strategy_params
                )
            except ValueError as exc:
                QMessageBox.warning(self, "策略参数无效", str(exc))
                return False
            if config.get('mode') == 'independent' and normalized_params:
                QMessageBox.warning(
                    self,
                    "策略参数不支持",
                    "独立测试的子进程契约不支持参数覆盖；"
                    "请清空参数或切换到组合投资。",
                )
                return False
            config['strategy_params'] = normalized_params or None
        else:
            if raw_strategy_params not in (None, {}):
                QMessageBox.warning(
                    self,
                    "输入参数冲突",
                    f"{input_mode} 输入不能携带自定义策略参数。",
                )
                return False
            config['strategy_params'] = None

        signals_text = str(config.get('signals_file') or '').strip()
        if input_mode == 'signals':
            if not signals_text:
                QMessageBox.warning(self, "错误", "请选择 signals CSV 文件。")
                return False
            try:
                from ...cli.signal_input import (
                    SignalReplayInput,
                    load_signal_replay_input,
                )

                frozen = config.get('_signals_replay_input')
                if frozen is None:
                    frozen = load_signal_replay_input(
                        Path(signals_text),
                        signal_format=config.get('signals_format') or 'auto',
                    )
                elif type(frozen) is not SignalReplayInput:
                    raise TypeError(
                        'signals runtime snapshot has an invalid type'
                    )
                else:
                    frozen = frozen.revalidated()
                    requested_format = (
                        config.get('signals_format') or 'auto'
                    )
                    if requested_format not in {
                        'auto', frozen.signal_format
                    }:
                        raise ValueError(
                            'signals format conflicts with the verified '
                            'runtime snapshot'
                        )
            except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
                QMessageBox.warning(self, "signals 校验失败", str(exc))
                return False
            config['signals_file'] = str(Path(signals_text).resolve())
            config['signals_format'] = frozen.signal_format
            config['pool_source'] = 'specified'
            config['symbols'] = list(frozen.symbols)
            config['_signals_replay_input'] = frozen
            self.append_debug(
                "已验证 signals CSV；worker 将冻结同一输入并使用内置因果回放，"
                "当前代码编辑器草稿不会执行。"
            )

        combo_text = str(config.get('combo_bundle') or '').strip()
        if input_mode == 'combo':
            if not combo_text:
                QMessageBox.warning(self, "错误", "请选择冻结 combo 目录。")
                return False
            try:
                from ...cli.combo_bundle import (
                    ComboReplayBundle,
                    load_combo_bundle,
                )

                bundle = config.get('_combo_replay_bundle')
                if bundle is None:
                    bundle = load_combo_bundle(
                        Path(combo_text), tag=config.get('combo_tag') or None
                    )
                elif type(bundle) is not ComboReplayBundle:
                    raise TypeError('combo runtime snapshot has an invalid type')
                else:
                    bundle = bundle.revalidated()
                    requested_tag = config.get('combo_tag') or None
                    if (
                        requested_tag is not None
                        and requested_tag != bundle.tag
                    ):
                        raise ValueError(
                            'combo tag conflicts with the verified runtime '
                            'snapshot'
                        )
                bundle.validate_requested_scope(
                    config['start_date'], config['end_date']
                )
            except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
                QMessageBox.warning(self, "combo 校验失败", str(exc))
                return False
            config['combo_bundle'] = str(bundle.root)
            config['combo_tag'] = bundle.tag
            config['pool_source'] = 'specified'
            config['symbols'] = list(bundle.symbols)
            config['_combo_replay_bundle'] = bundle
            self.append_debug(
                "已验证冻结 combo；运行时将使用内置因果回放策略，"
                "当前代码编辑器内容不会作为执行策略。"
            )

        if not config['start_date'] or not config['end_date']:
            QMessageBox.warning(self, "错误", "请设置日期范围")
            return False

        if config['pool_source'] == 'specified' and input_mode == 'strategy':
            if not config['symbols']:
                QMessageBox.warning(self, "错误", "请输入股票代码")
                return False

        return True

    def get_code(self) -> str:
        """获取代码"""
        return self.code_editor.get_code()

    def get_config(self) -> dict:
        """获取配置"""
        config = self.config_panel.get_config()
        if (
            config.get('input_mode') == 'signals'
            and self._signals_replay_input is not None
            and config.get('signals_file') == self._signals_snapshot_path
        ):
            config['_signals_replay_input'] = self._signals_replay_input
        if (
            config.get('input_mode') == 'combo'
            and self._combo_replay_bundle is not None
            and config.get('combo_bundle') == self._combo_snapshot_path
        ):
            config['_combo_replay_bundle'] = self._combo_replay_bundle
        return config

    def _on_signals_input_path_changed(self, value):
        if (
            self._signals_replay_input is not None
            and str(value).strip() != self._signals_snapshot_path
        ):
            self._signals_replay_input = None
            self._signals_snapshot_path = None

    def _on_combo_input_path_changed(self, value):
        if (
            self._combo_replay_bundle is not None
            and str(value).strip() != self._combo_snapshot_path
        ):
            self._combo_replay_bundle = None
            self._combo_snapshot_path = None

    def _set_runtime_input_snapshots(
        self, *, signals_input=None, combo_bundle=None
    ):
        if signals_input is not None and combo_bundle is not None:
            raise ValueError('only one runtime input snapshot may be active')
        self._signals_replay_input = signals_input
        self._signals_snapshot_path = (
            self.config_panel.signals_file_edit.text().strip()
            if signals_input is not None else None
        )
        self._combo_replay_bundle = combo_bundle
        self._combo_snapshot_path = (
            self.config_panel.combo_bundle_edit.text().strip()
            if combo_bundle is not None else None
        )

    def set_running(self, running: bool):
        """设置运行状态"""
        self._running = bool(running)
        rerun_blocked = bool(getattr(self, '_rerun_block_reason', None))
        self.run_btn.setEnabled(not running and not rerun_blocked)
        self.check_btn.setEnabled(not running and not rerun_blocked)
        self.home_btn.setEnabled(not running)
        self.config_panel.set_mode_enabled(not running)
        self._on_input_mode_changed(
            self.config_panel.input_mode_combo.currentData()
        )

    def set_rerun_blocked(self, reason=None):
        """Disable execution when an Artifact cannot be restored exactly."""

        self._rerun_block_reason = str(reason).strip() if reason else None
        if self._rerun_block_reason:
            tip = (
                '结果已载入，但此 GUI 版本无法等价恢复该工件配置：'
                + self._rerun_block_reason
            )
            self.run_btn.setToolTip(tip)
            self.check_btn.setToolTip(tip)
        else:
            self.run_btn.setToolTip('')
            self.check_btn.setToolTip(
                '按当前模式编译并校验策略契约，不运行回测'
            )
        self.set_running(self._running)

    def set_code(self, code: str):
        """设置代码"""
        self.code_editor.set_code(code)
        self._strategy_drafts[self._active_strategy_kind] = code

    def set_config(self, config: dict):
        """设置配置"""
        signals_input = config.get('_signals_replay_input')
        combo_bundle = config.get('_combo_replay_bundle')
        self._set_runtime_input_snapshots()
        self.config_panel.set_config(config)
        self._set_runtime_input_snapshots(
            signals_input=signals_input,
            combo_bundle=combo_bundle,
        )
        self._on_input_mode_changed(
            self.config_panel.input_mode_combo.currentData()
        )
