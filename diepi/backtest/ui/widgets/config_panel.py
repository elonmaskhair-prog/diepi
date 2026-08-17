"""
配置面板

回测参数配置组件
"""

import json
import math
from datetime import datetime, timedelta
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QLabel,
    QLineEdit, QSpinBox, QDoubleSpinBox, QRadioButton,
    QButtonGroup, QPushButton, QTextEdit, QScrollArea, QFrame,
    QDateEdit, QComboBox, QFileDialog, QMessageBox
)
from PySide6.QtCore import Signal, Qt, QDate
from PySide6.QtGui import QFont

from ..styles import Colors, Fonts, Styles
from diepi.runtime import RuntimePaths


_GUI_DOUBLE_DECIMALS = 17
_GUI_DOUBLE_LIMIT = 1e300


class _CompactDoubleSpinBox(QDoubleSpinBox):
    """Keep full double precision without displaying padded zeroes."""

    def textFromValue(self, value):
        return repr(float(value))


class ConfigPanel(QWidget):
    """
    配置面板

    包含:
    - 股票池配置 (指定/全市场)
    - 日期范围
    - 资金设置
    - 交易成本
    - 回测频率
    """

    config_changed = Signal()
    strategy_kind_changed = Signal(str)
    input_mode_changed = Signal(str)

    def __init__(self, parent=None, *, data_root=None):
        super().__init__(parent)
        self._initial_data_root = data_root
        self._init_ui()

    def _init_ui(self):
        # 主布局使用滚动区域
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QFrame.NoFrame)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # ==================== 本地数据根 ====================
        data_group = QGroupBox("本地数据")
        data_layout = QVBoxLayout(data_group)
        self.data_root_edit = QLineEdit()
        default_data_root = RuntimePaths.resolve(
            data_root=self._initial_data_root,
            require_data_root=False,
        ).data_root
        self.data_root_edit.setText(str(default_data_root))
        self.data_root_edit.setPlaceholderText("包含 parquet/ 的本地数据根目录")
        self.data_root_edit.textChanged.connect(self._emit_config_changed)
        data_layout.addWidget(self.data_root_edit)

        data_actions = QHBoxLayout()
        self.data_root_browse_btn = QPushButton("浏览…")
        self.data_root_browse_btn.clicked.connect(self._on_browse_data_root)
        data_actions.addWidget(self.data_root_browse_btn)
        data_hint = QLabel("路径仅用于本机运行；Artifact 只记录外部数据声明")
        data_hint.setWordWrap(True)
        data_hint.setStyleSheet(f"color: {Colors.TEXT_MUTED}; font-size: 11px;")
        data_actions.addWidget(data_hint, 1)
        data_layout.addLayout(data_actions)

        layout.addWidget(data_group)

        # ==================== 回测输入（三选一） ====================
        input_group = QGroupBox("回测输入（三选一）")
        input_layout = QVBoxLayout(input_group)
        self.input_mode_combo = QComboBox()
        self.input_mode_combo.addItem("策略代码", "strategy")
        self.input_mode_combo.addItem("signals CSV", "signals")
        self.input_mode_combo.addItem("冻结 combo", "combo")
        input_layout.addWidget(self.input_mode_combo)

        self.strategy_frame = QFrame()
        strategy_layout = QVBoxLayout(self.strategy_frame)
        strategy_layout.setContentsMargins(0, 0, 0, 0)
        strategy_layout.addWidget(QLabel("策略参数覆盖（JSON）"))
        self.strategy_params_edit = QLineEdit()
        self.strategy_params_edit.setPlaceholderText(
            '{"LOOKBACK":20,"USE_FILTER":true}；可留空'
        )
        self.strategy_params_edit.setToolTip(
            "对应 CLI --param；只接受标识符键和 bool/int/有限float/string。"
            "参数在策略代码执行后注入，import 时已计算的派生常量不会重算。"
        )
        self.strategy_params_edit.textChanged.connect(
            self._emit_config_changed
        )
        strategy_layout.addWidget(self.strategy_params_edit)
        input_layout.addWidget(self.strategy_frame)

        self.signals_frame = QFrame()
        signals_layout = QVBoxLayout(self.signals_frame)
        signals_layout.setContentsMargins(0, 0, 0, 0)
        signals_label = QLabel("信号清单")
        signals_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
        signals_layout.addWidget(signals_label)
        signals_path_row = QHBoxLayout()
        self.signals_file_edit = QLineEdit()
        self.signals_file_edit.setPlaceholderText(
            "date,symbol,target_weight 或 date,symbol,action"
        )
        self.signals_file_edit.textChanged.connect(self._emit_config_changed)
        signals_path_row.addWidget(self.signals_file_edit, 1)
        self.signals_browse_btn = QPushButton("选择 CSV…")
        self.signals_browse_btn.clicked.connect(self._on_browse_signals_file)
        signals_path_row.addWidget(self.signals_browse_btn)
        signals_layout.addLayout(signals_path_row)
        signals_format_row = QHBoxLayout()
        signals_format_row.addWidget(QLabel("清单格式"))
        self.signals_format_combo = QComboBox()
        self.signals_format_combo.addItem("自动识别", "auto")
        self.signals_format_combo.addItem("目标权重 target", "target")
        self.signals_format_combo.addItem("动作 action", "action")
        self.signals_format_combo.currentIndexChanged.connect(
            self._emit_config_changed
        )
        signals_format_row.addWidget(self.signals_format_combo, 1)
        signals_layout.addLayout(signals_format_row)
        signals_hint = QLabel(
            "仅支持组合投资；运行前会冻结并校验完整 CSV，Artifact 保存同一份字节。"
        )
        signals_hint.setWordWrap(True)
        signals_hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: 11px;"
        )
        signals_layout.addWidget(signals_hint)
        input_layout.addWidget(self.signals_frame)

        self.combo_frame = QFrame()
        combo_layout = QVBoxLayout(self.combo_frame)
        combo_layout.setContentsMargins(0, 0, 0, 0)
        combo_label = QLabel("冻结组合信号")
        combo_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY};")
        combo_layout.addWidget(combo_label)
        self.combo_bundle_edit = QLineEdit()
        self.combo_bundle_edit.setPlaceholderText(
            "包含 targets / close_sells / daily 的 combo 目录"
        )
        self.combo_bundle_edit.textChanged.connect(self._emit_config_changed)
        combo_layout.addWidget(self.combo_bundle_edit)
        combo_actions = QHBoxLayout()
        self.combo_bundle_browse_btn = QPushButton("选择 combo…")
        self.combo_bundle_browse_btn.clicked.connect(
            self._on_browse_combo_bundle
        )
        combo_actions.addWidget(self.combo_bundle_browse_btn)
        self.combo_tag_edit = QLineEdit()
        self.combo_tag_edit.setPlaceholderText("旧式目录 tag（通常可留空）")
        self.combo_tag_edit.textChanged.connect(self._emit_config_changed)
        combo_actions.addWidget(self.combo_tag_edit, 1)
        combo_layout.addLayout(combo_actions)
        combo_hint = QLabel(
            "设置后使用正式因果回放：盘前目标调仓；开盘后提交当日收盘退出。"
        )
        combo_hint.setWordWrap(True)
        combo_hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: 11px;"
        )
        combo_layout.addWidget(combo_hint)
        input_layout.addWidget(self.combo_frame)
        self.input_mode_combo.currentIndexChanged.connect(
            self._on_input_mode_changed
        )
        self._on_input_mode_changed(emit=False)
        layout.addWidget(input_group)

        # ==================== 价格口径 ====================
        price_group = QGroupBox("价格口径")
        price_layout = QVBoxLayout(price_group)
        self.price_mode_combo = QComboBox()
        self.price_mode_combo.addItem(
            "双轨：策略后复权 / 撮合原始价（推荐）", "dual"
        )
        self.price_mode_combo.addItem(
            "原始价单轨（raw-minimal）", "raw"
        )
        self.price_mode_combo.addItem("后复权单轨", "hfq")
        self.price_mode_combo.currentIndexChanged.connect(
            self._emit_config_changed
        )
        price_layout.addWidget(self.price_mode_combo)
        price_hint = QLabel(
            "raw 只需原始日线；dual 更适合正式研究，信号使用后复权价、成交使用原始价。"
        )
        price_hint.setWordWrap(True)
        price_hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: 11px;"
        )
        price_layout.addWidget(price_hint)
        layout.addWidget(price_group)

        # ==================== 日线集合竞价容量 ====================
        auction_group = QGroupBox("日线集合竞价容量")
        auction_layout = QVBoxLayout(auction_group)
        self._auction_controls = {}
        for window, label in (("open", "开盘"), ("close", "收盘")):
            row = QHBoxLayout()
            name = QLabel(label)
            name.setMinimumWidth(45)
            row.addWidget(name)
            mode_combo = QComboBox()
            mode_combo.addItem("前日成交额比例", "previous_day_ratio")
            mode_combo.addItem("固定可成交金额", "fixed_yuan")
            mode_combo.addItem("未配置（使用时会报错）", "unconfigured")
            row.addWidget(mode_combo, 1)
            value_spin = _CompactDoubleSpinBox()
            value_spin.setDecimals(_GUI_DOUBLE_DECIMALS)
            value_spin.setMinimumWidth(130)
            row.addWidget(value_spin)
            self._auction_controls[window] = (mode_combo, value_spin)
            mode_combo.currentIndexChanged.connect(
                lambda _index, side=window: self._on_auction_mode_changed(side)
            )
            value_spin.valueChanged.connect(self._emit_config_changed)
            auction_layout.addLayout(row)
            self._on_auction_mode_changed(window, emit=False)

        auction_hint = QLabel(
            "默认 10% 是醒目的建模起点，不是交易所事实；请按策略容量修改。"
            "日线策略使用 OPEN/CLOSE 时必须为对应窗口显式配置。"
        )
        auction_hint.setWordWrap(True)
        auction_hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: 11px;"
        )
        auction_layout.addWidget(auction_hint)
        layout.addWidget(auction_group)

        # ==================== 股票池配置 ====================
        pool_group = QGroupBox("股票池")
        pool_layout = QVBoxLayout(pool_group)
        pool_layout.setSpacing(10)

        # 来源选择 (只保留指定和全市场)
        self.pool_specified = QRadioButton("指定股票")
        self.pool_all = QRadioButton("全市场")
        self.pool_specified.setChecked(True)

        pool_btn_group = QButtonGroup(self)
        pool_btn_group.addButton(self.pool_specified)
        pool_btn_group.addButton(self.pool_all)

        pool_radio_layout = QHBoxLayout()
        pool_radio_layout.addWidget(self.pool_specified)
        pool_radio_layout.addWidget(self.pool_all)
        pool_radio_layout.addStretch()
        
        # 导入文件按钮
        self.btn_import = QPushButton("从文件导入")
        self.btn_import.setCursor(Qt.PointingHandCursor)
        self.btn_import.clicked.connect(self._on_import_file)
        pool_radio_layout.addWidget(self.btn_import)
        
        pool_layout.addLayout(pool_radio_layout)

        # 指定股票输入 - 更大的输入框和字体
        self.symbols_edit = QTextEdit()
        self.symbols_edit.setPlaceholderText(
            "输入股票代码，每行一个\n"
            "例如:\n"
            "000001.SZ\n"
            "000002.SZ\n"
            "600000.SH"
        )
        self.symbols_edit.setMinimumHeight(130)
        self.symbols_edit.setText("000001.SZ\n000002.SZ\n600000.SH")
        self.symbols_edit.setFont(QFont('Consolas', 12))
        pool_layout.addWidget(self.symbols_edit)

        # 连接信号
        self.pool_specified.toggled.connect(self._on_pool_source_changed)
        self.pool_all.toggled.connect(self._on_pool_source_changed)

        layout.addWidget(pool_group)

        # ==================== 日期配置 ====================
        date_group = QGroupBox("日期范围")
        date_layout = QVBoxLayout(date_group)
        date_layout.setSpacing(10)

        # 开始日期
        start_layout = QHBoxLayout()
        start_label = QLabel("开始")
        start_label.setMinimumWidth(45)
        start_layout.addWidget(start_label)
        self.start_date = QDateEdit()
        self.start_date.setCalendarPopup(True)  # 启用日历弹出
        self.start_date.setDisplayFormat("yyyy-MM-dd")
        self.start_date.setMinimumDate(QDate(2010, 1, 1))
        self.start_date.setMaximumDate(QDate.currentDate())
        start_layout.addWidget(self.start_date)
        date_layout.addLayout(start_layout)

        # 结束日期
        end_layout = QHBoxLayout()
        end_label = QLabel("结束")
        end_label.setMinimumWidth(45)
        end_layout.addWidget(end_label)
        self.end_date = QDateEdit()
        self.end_date.setCalendarPopup(True)  # 启用日历弹出
        self.end_date.setDisplayFormat("yyyy-MM-dd")
        self.end_date.setMinimumDate(QDate(2010, 1, 1))
        self.end_date.setMaximumDate(QDate.currentDate())
        end_layout.addWidget(self.end_date)
        date_layout.addLayout(end_layout)

        # 快捷按钮 - 字体放大一倍
        quick_layout = QHBoxLayout()
        quick_layout.setSpacing(8)
        btn_1m = QPushButton("近1月")
        btn_3m = QPushButton("近3月")
        btn_6m = QPushButton("近6月")
        btn_1y = QPushButton("近1年")

        quick_btn_style = f"""
            QPushButton {{
                background-color: {Colors.BG_SECONDARY};
                color: {Colors.TEXT_PRIMARY};
                font-size: 22px;
                padding: 8px 12px;
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
            }}
            QPushButton:hover {{
                background-color: {Colors.BG_TERTIARY};
                color: {Colors.TEXT_PRIMARY};
            }}
        """
        for btn in [btn_1m, btn_3m, btn_6m, btn_1y]:
            btn.setStyleSheet(quick_btn_style)
            btn.setCursor(Qt.PointingHandCursor)

        btn_1m.clicked.connect(lambda: self._set_period(30))
        btn_3m.clicked.connect(lambda: self._set_period(90))
        btn_6m.clicked.connect(lambda: self._set_period(180))
        btn_1y.clicked.connect(lambda: self._set_period(365))

        quick_layout.addWidget(btn_1m)
        quick_layout.addWidget(btn_3m)
        quick_layout.addWidget(btn_6m)
        quick_layout.addWidget(btn_1y)
        date_layout.addLayout(quick_layout)

        # 设置默认日期 (近3月)
        self._set_period(90)

        layout.addWidget(date_group)

        # ==================== 资金配置 ====================
        capital_group = QGroupBox("资金设置")
        capital_layout = QVBoxLayout(capital_group)

        cash_layout = QHBoxLayout()
        cash_label = QLabel("初始资金")
        cash_label.setMinimumWidth(60)
        cash_layout.addWidget(cash_label)
        self.initial_cash = _CompactDoubleSpinBox()
        self.initial_cash.setDecimals(_GUI_DOUBLE_DECIMALS)
        self.initial_cash.setRange(0.0, _GUI_DOUBLE_LIMIT)
        self.initial_cash.setValue(1_000_000.0)
        self.initial_cash.setSingleStep(100_000.0)
        self.initial_cash.setSuffix(" 元")
        self.initial_cash.setGroupSeparatorShown(True)
        self.initial_cash.valueChanged.connect(self._emit_config_changed)
        cash_layout.addWidget(self.initial_cash)
        capital_layout.addLayout(cash_layout)

        layout.addWidget(capital_group)

        # ==================== 交易成本 ====================
        cost_group = QGroupBox("交易成本")
        cost_layout = QVBoxLayout(cost_group)
        cost_layout.setSpacing(8)

        # 滑点 - 默认千分之一
        slippage_layout = QHBoxLayout()
        slippage_label = QLabel("滑点")
        slippage_label.setMinimumWidth(60)
        slippage_layout.addWidget(slippage_label)
        self.slippage = _CompactDoubleSpinBox()
        self.slippage.setDecimals(_GUI_DOUBLE_DECIMALS)
        self.slippage.setRange(0.0, 100.0)
        self.slippage.setValue(0.1)  # 显示百分比
        self.slippage.setSingleStep(0.01)
        self.slippage.setSuffix(" %")
        self.slippage.valueChanged.connect(self._emit_config_changed)
        slippage_layout.addWidget(self.slippage)
        cost_layout.addLayout(slippage_layout)

        # 佣金 - 默认万分之2.5
        commission_layout = QHBoxLayout()
        commission_label = QLabel("佣金")
        commission_label.setMinimumWidth(60)
        commission_layout.addWidget(commission_label)
        self.commission = _CompactDoubleSpinBox()
        self.commission.setDecimals(_GUI_DOUBLE_DECIMALS)
        self.commission.setRange(0.0, _GUI_DOUBLE_LIMIT)
        self.commission.setValue(2.5)  # 显示万分比
        self.commission.setSingleStep(0.1)
        self.commission.setSuffix(" ‱")
        self.commission.valueChanged.connect(self._emit_config_changed)
        commission_layout.addWidget(self.commission)
        cost_layout.addLayout(commission_layout)

        # 印花税 - 自动品种/日期规则或显式固定值
        stamp_layout = QHBoxLayout()
        stamp_label = QLabel("印花税")
        stamp_label.setMinimumWidth(60)
        stamp_layout.addWidget(stamp_label)
        self.stamp_duty_mode = QComboBox()
        self.stamp_duty_mode.addItem("自动", "auto")
        self.stamp_duty_mode.addItem("固定", "fixed")
        self.stamp_duty_mode.currentIndexChanged.connect(
            self._on_stamp_duty_mode_changed
        )
        stamp_layout.addWidget(self.stamp_duty_mode)
        self.stamp_duty = _CompactDoubleSpinBox()
        self.stamp_duty.setDecimals(_GUI_DOUBLE_DECIMALS)
        self.stamp_duty.setRange(0.0, _GUI_DOUBLE_LIMIT)
        self.stamp_duty.setValue(0.1)  # 显示百分比
        self.stamp_duty.setSingleStep(0.01)
        self.stamp_duty.setSuffix(" %")
        self.stamp_duty.valueChanged.connect(self._emit_config_changed)
        stamp_layout.addWidget(self.stamp_duty)
        cost_layout.addLayout(stamp_layout)

        # 双边过户费 - 显式建模，不做隐藏的历史切换
        transfer_layout = QHBoxLayout()
        transfer_label = QLabel("过户费")
        transfer_label.setMinimumWidth(60)
        transfer_layout.addWidget(transfer_label)
        self.transfer_fee_rate = _CompactDoubleSpinBox()
        self.transfer_fee_rate.setDecimals(_GUI_DOUBLE_DECIMALS)
        self.transfer_fee_rate.setRange(0.0, _GUI_DOUBLE_LIMIT)
        self.transfer_fee_rate.setValue(0.0)
        self.transfer_fee_rate.setSingleStep(0.01)
        self.transfer_fee_rate.setSuffix(" ‱")
        self.transfer_fee_rate.setToolTip("双边过户费率；0 表示不收取")
        self.transfer_fee_rate.valueChanged.connect(self._emit_config_changed)
        transfer_layout.addWidget(self.transfer_fee_rate)
        cost_layout.addLayout(transfer_layout)
        self._on_stamp_duty_mode_changed(emit=False)

        # 最低佣金 - 默认5元
        min_comm_layout = QHBoxLayout()
        min_comm_label = QLabel("最低佣金")
        min_comm_label.setMinimumWidth(60)
        min_comm_layout.addWidget(min_comm_label)
        self.min_commission = _CompactDoubleSpinBox()
        self.min_commission.setDecimals(_GUI_DOUBLE_DECIMALS)
        self.min_commission.setRange(0.0, _GUI_DOUBLE_LIMIT)
        self.min_commission.setValue(5.0)
        self.min_commission.setSingleStep(0.5)
        self.min_commission.setSuffix(" 元")
        self.min_commission.valueChanged.connect(self._emit_config_changed)
        min_comm_layout.addWidget(self.min_commission)
        cost_layout.addLayout(min_comm_layout)

        layout.addWidget(cost_group)

        # ==================== 执行与指标假设 ====================
        execution_group = QGroupBox("执行与指标假设")
        execution_layout = QVBoxLayout(execution_group)
        execution_layout.setSpacing(8)

        lot_row = QHBoxLayout()
        lot_row.addWidget(QLabel("普通证券申报手数"))
        self.lot_size = QSpinBox()
        self.lot_size.setRange(1, 2_000_000_000)
        self.lot_size.setValue(100)
        self.lot_size.valueChanged.connect(self._emit_config_changed)
        lot_row.addWidget(self.lot_size, 1)
        execution_layout.addLayout(lot_row)

        liquidity_row = QHBoxLayout()
        liquidity_row.addWidget(QLabel("连续K线参与率"))
        self.liquidity_cap_ratio = _CompactDoubleSpinBox()
        self.liquidity_cap_ratio.setRange(0.0, 100.0)
        self.liquidity_cap_ratio.setDecimals(_GUI_DOUBLE_DECIMALS)
        self.liquidity_cap_ratio.setSingleStep(1.0)
        self.liquidity_cap_ratio.setValue(80.0)
        self.liquidity_cap_ratio.setSuffix(" %")
        self.liquidity_cap_ratio.setToolTip(
            "仅用于普通连续 K 线成交额；开/收盘集合竞价使用上方独立容量。"
        )
        self.liquidity_cap_ratio.valueChanged.connect(
            self._emit_config_changed
        )
        liquidity_row.addWidget(self.liquidity_cap_ratio, 1)
        execution_layout.addLayout(liquidity_row)

        resize_row = QHBoxLayout()
        resize_row.addWidget(QLabel("买单缩量"))
        self.open_buy_resize_mode = QComboBox()
        self.open_buy_resize_mode.addItem("自动", "auto")
        self.open_buy_resize_mode.addItem("旧口径", "legacy")
        self.open_buy_resize_mode.currentIndexChanged.connect(
            self._emit_config_changed
        )
        resize_row.addWidget(self.open_buy_resize_mode, 1)
        execution_layout.addLayout(resize_row)

        fill_row = QHBoxLayout()
        fill_row.addWidget(QLabel("开盘成交价"))
        self.open_buy_fill_mode = QComboBox()
        self.open_buy_fill_mode.addItem("开盘价 + 滑点", "open+slip")
        self.open_buy_fill_mode.addItem("仅开盘价（旧口径）", "open")
        self.open_buy_fill_mode.setToolTip(
            "仅在买单缩量=自动时生效；legacy 执行分支不会读取本项。"
        )
        self.open_buy_fill_mode.currentIndexChanged.connect(
            self._emit_config_changed
        )
        fill_row.addWidget(self.open_buy_fill_mode, 1)
        execution_layout.addLayout(fill_row)

        sizing_row = QHBoxLayout()
        sizing_row.addWidget(QLabel("委托量折算"))
        self.open_buy_sizing = QComboBox()
        self.open_buy_sizing.addItem("涨停价", "limit_up")
        self.open_buy_sizing.addItem("成交价（旧口径）", "fill")
        self.open_buy_sizing.setToolTip(
            "仅在买单缩量=自动时生效；legacy 执行分支不会读取本项。"
        )
        self.open_buy_sizing.currentIndexChanged.connect(
            self._emit_config_changed
        )
        sizing_row.addWidget(self.open_buy_sizing, 1)
        execution_layout.addLayout(sizing_row)

        limit_row = QVBoxLayout()
        limit_row.addWidget(QLabel("涨跌停覆盖（JSON）"))
        self.limit_pct_overrides_edit = QLineEdit()
        self.limit_pct_overrides_edit.setPlaceholderText(
            '{"688001":0.20,"159781.SZ":0.20}；仅6位代码/完整symbol'
        )
        self.limit_pct_overrides_edit.textChanged.connect(
            self._emit_config_changed
        )
        limit_row.addWidget(self.limit_pct_overrides_edit)
        execution_layout.addLayout(limit_row)

        t0_row = QVBoxLayout()
        t0_row.addWidget(QLabel("T+0 代码/前缀"))
        self.t0_overrides_edit = QLineEdit()
        self.t0_overrides_edit.setPlaceholderText(
            "逗号分隔，例如 511,513,159001.SZ；留空使用默认"
        )
        self.t0_overrides_edit.textChanged.connect(self._emit_config_changed)
        t0_row.addWidget(self.t0_overrides_edit)
        execution_layout.addLayout(t0_row)

        annual_row = QHBoxLayout()
        annual_row.addWidget(QLabel("年化交易日"))
        self.trading_days_per_year = QSpinBox()
        self.trading_days_per_year.setRange(1, 10_000)
        self.trading_days_per_year.setValue(252)
        self.trading_days_per_year.valueChanged.connect(
            self._emit_config_changed
        )
        annual_row.addWidget(self.trading_days_per_year, 1)
        execution_layout.addLayout(annual_row)

        risk_row = QHBoxLayout()
        risk_row.addWidget(QLabel("无风险利率"))
        self.risk_free_rate = _CompactDoubleSpinBox()
        self.risk_free_rate.setDecimals(_GUI_DOUBLE_DECIMALS)
        self.risk_free_rate.setRange(-_GUI_DOUBLE_LIMIT, _GUI_DOUBLE_LIMIT)
        self.risk_free_rate.setSingleStep(0.1)
        self.risk_free_rate.setValue(3.0)
        self.risk_free_rate.setSuffix(" %")
        self.risk_free_rate.valueChanged.connect(self._emit_config_changed)
        risk_row.addWidget(self.risk_free_rate, 1)
        execution_layout.addLayout(risk_row)

        execution_hint = QLabel(
            "这些字段会进入 Artifact 并在 CLI/GUI 重跑时完整恢复；"
            "JSON 或代码列表无效时运行前会拒绝。"
        )
        execution_hint.setWordWrap(True)
        execution_hint.setStyleSheet(
            f"color: {Colors.TEXT_MUTED}; font-size: 11px;"
        )
        execution_layout.addWidget(execution_hint)
        self._execution_controls = (
            self.lot_size,
            self.liquidity_cap_ratio,
            self.open_buy_resize_mode,
            self.open_buy_fill_mode,
            self.open_buy_sizing,
            self.limit_pct_overrides_edit,
            self.t0_overrides_edit,
            self.trading_days_per_year,
            self.risk_free_rate,
        )
        layout.addWidget(execution_group)

        # ==================== 回测频率 ====================
        freq_group = QGroupBox("回测频率")
        freq_layout = QHBoxLayout(freq_group)

        self.freq_daily = QRadioButton("日线")
        self.freq_minute = QRadioButton("分钟")
        self.freq_daily.setChecked(True)

        freq_btn_group = QButtonGroup(self)
        freq_btn_group.addButton(self.freq_daily)
        freq_btn_group.addButton(self.freq_minute)

        freq_layout.addWidget(self.freq_daily)
        freq_layout.addWidget(self.freq_minute)
        freq_layout.addStretch()

        layout.addWidget(freq_group)

        # ==================== 回测模式 ====================
        mode_group = QGroupBox("回测模式")
        mode_layout = QVBoxLayout(mode_group)
        mode_layout.setSpacing(8)

        # 模式选择
        self.mode_portfolio = QRadioButton("组合投资 (共享资金池)")
        self.mode_independent = QRadioButton("独立测试 (每股独立资金)")
        self.mode_portfolio.setChecked(True)  # 默认组合模式

        mode_btn_group = QButtonGroup(self)
        mode_btn_group.addButton(self.mode_portfolio)
        mode_btn_group.addButton(self.mode_independent)

        mode_layout.addWidget(self.mode_portfolio)
        mode_layout.addWidget(self.mode_independent)

        # 并行进程数 (仅独立模式可用)
        workers_layout = QHBoxLayout()
        workers_label = QLabel("并行进程")
        workers_label.setMinimumWidth(60)
        workers_layout.addWidget(workers_label)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setValue(12)  # 默认12核 (16核的80%)
        self.workers_spin.setSuffix(" 核")
        self.workers_spin.setEnabled(False)  # 默认禁用，组合模式不需要
        workers_layout.addWidget(self.workers_spin)
        mode_layout.addLayout(workers_layout)

        # 连接信号
        self.mode_independent.toggled.connect(self._on_mode_changed)

        layout.addWidget(mode_group)

        # 弹性空间
        layout.addStretch()

        scroll.setWidget(content)
        main_layout.addWidget(scroll)

    def _emit_config_changed(self, *_args):
        """Adapt value-bearing Qt signals to the no-argument public signal."""

        self.config_changed.emit()

    @staticmethod
    def _set_exact_double(
        control,
        value,
        *,
        name: str,
        scale: float = 1.0,
        minimum=None,
        maximum=None,
        minimum_inclusive: bool = True,
        maximum_inclusive: bool = True,
    ) -> float:
        """Set a numeric control only when its engine value round-trips."""

        if type(value) not in (int, float):
            raise ValueError(f'{name} must be a finite number')
        try:
            number = float(value)
        except (OverflowError, TypeError, ValueError):
            raise ValueError(f'{name} must be a finite number') from None
        if not math.isfinite(number):
            raise ValueError(f'{name} must be a finite number')
        if minimum is not None and (
            number < minimum
            or (not minimum_inclusive and number == minimum)
        ):
            bracket = '[' if minimum_inclusive else '('
            raise ValueError(f'{name} is outside {bracket}{minimum}, ...')
        if maximum is not None and (
            number > maximum
            or (not maximum_inclusive and number == maximum)
        ):
            bracket = ']' if maximum_inclusive else ')'
            raise ValueError(f'{name} is outside ..., {maximum}{bracket}')
        displayed = number * scale
        if (
            not math.isfinite(displayed)
            or displayed < control.minimum()
            or displayed > control.maximum()
        ):
            raise ValueError(f'{name} is outside the GUI representable range')
        control.setValue(displayed)
        restored = control.value() / scale
        if restored != value:
            raise ValueError(
                f'{name} cannot be represented exactly by the GUI'
            )
        return number

    def _on_pool_source_changed(self):
        """股票池来源改变"""
        self.symbols_edit.setEnabled(self.pool_specified.isChecked())
        self.config_changed.emit()

    def _on_mode_changed(self, checked: bool):
        """回测模式改变"""
        # 独立测试模式时启用并行进程数
        self.workers_spin.setEnabled(checked)
        if checked and self.input_mode_combo.currentData() != 'strategy':
            self.input_mode_combo.setCurrentIndex(
                self.input_mode_combo.findData('strategy')
            )
        self.input_mode_combo.setEnabled(not checked)
        self.strategy_frame.setEnabled(not checked)
        self.strategy_kind_changed.emit('single' if checked else 'portfolio')
        self.config_changed.emit()

    def _on_input_mode_changed(self, _index=None, *, emit: bool = True):
        """Expose exactly one input contract at a time."""

        mode = self.input_mode_combo.currentData() or 'strategy'
        independent = getattr(self, 'mode_independent', None)
        self.strategy_frame.setEnabled(
            mode == 'strategy'
            and not (independent is not None and independent.isChecked())
        )
        self.signals_frame.setEnabled(mode == 'signals')
        self.combo_frame.setEnabled(mode == 'combo')
        if emit:
            self.input_mode_changed.emit(mode)
            self.config_changed.emit()

    def _on_stamp_duty_mode_changed(
        self, _index=None, *, emit: bool = True
    ):
        """Keep ``auto`` distinct from a numeric zero/fixed rate."""

        self.stamp_duty.setEnabled(
            self.stamp_duty_mode.currentData() == 'fixed'
        )
        if emit:
            self.config_changed.emit()

    def _on_auction_mode_changed(self, window: str, *, emit: bool = True):
        """Keep each auction value editor explicit about units and meaning."""

        combo, spin = self._auction_controls[window]
        mode = combo.currentData()
        spin.blockSignals(True)
        if mode == 'previous_day_ratio':
            spin.setEnabled(True)
            spin.setDecimals(_GUI_DOUBLE_DECIMALS)
            spin.setRange(0.0, 100.0)
            spin.setSingleStep(1.0)
            spin.setSuffix(" %")
            if spin.value() <= 0.0 or spin.value() > 100.0:
                spin.setValue(10.0)
        elif mode == 'fixed_yuan':
            spin.setEnabled(True)
            spin.setDecimals(_GUI_DOUBLE_DECIMALS)
            spin.setRange(0.0, _GUI_DOUBLE_LIMIT)
            spin.setSingleStep(100_000.0)
            spin.setSuffix(" 元")
            if spin.value() <= 100.0:
                spin.setValue(1_000_000.0)
        else:
            spin.setEnabled(False)
            spin.setSuffix("")
        spin.blockSignals(False)
        if emit:
            self.config_changed.emit()

    def _auction_config(self) -> dict:
        values = {
            'daily_open_cap_yuan': None,
            'daily_close_cap_yuan': None,
            'daily_open_previous_day_ratio': None,
            'daily_close_previous_day_ratio': None,
        }
        for window, (combo, spin) in self._auction_controls.items():
            mode = combo.currentData()
            if mode == 'fixed_yuan':
                values[f'daily_{window}_cap_yuan'] = spin.value()
            elif mode == 'previous_day_ratio':
                values[f'daily_{window}_previous_day_ratio'] = (
                    spin.value() / 100.0
                )
        return values

    def _on_browse_data_root(self):
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择本地数据根目录",
            self.data_root_edit.text().strip(),
        )
        if selected:
            self.data_root_edit.setText(selected)

    def _on_browse_signals_file(self):
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择 signals CSV",
            self.signals_file_edit.text().strip(),
            "CSV 文件 (*.csv);;所有文件 (*)",
        )
        if selected:
            self.signals_file_edit.setText(selected)

    def _on_browse_combo_bundle(self):
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择冻结组合信号目录",
            self.combo_bundle_edit.text().strip(),
        )
        if selected:
            self.combo_bundle_edit.setText(selected)

    def _set_period(self, days: int):
        """设置时间周期"""
        end = datetime.now()
        start = end - timedelta(days=days)
        self.start_date.setDate(QDate(start.year, start.month, start.day))
        self.end_date.setDate(QDate(end.year, end.month, end.day))

    def _limit_pct_overrides_config(self):
        """Return parsed JSON, preserving invalid text for validation."""

        text = self.limit_pct_overrides_edit.text().strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def _strategy_params_config(self):
        """Return parsed custom params, preserving invalid text for validation."""

        text = self.strategy_params_edit.text().strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def _t0_overrides_config(self):
        values = [
            value.strip()
            for value in self.t0_overrides_edit.text().split(',')
            if value.strip()
        ]
        return list(dict.fromkeys(values)) or None

    def get_config(self) -> dict:
        """
        获取配置

        Returns:
            配置字典
        """
        # 股票池来源
        if self.pool_specified.isChecked():
            pool_source = 'specified'
            symbols = [
                s.strip() for s in self.symbols_edit.toPlainText().split('\n')
                if s.strip()
            ]
        else:
            pool_source = 'all_market'
            symbols = None

        mode = 'independent' if self.mode_independent.isChecked() else 'portfolio'
        input_mode = self.input_mode_combo.currentData() or 'strategy'
        config = {
            'data_root': self.data_root_edit.text().strip(),
            'input_mode': input_mode,
            'strategy_params': (
                self._strategy_params_config()
                if input_mode == 'strategy' and mode == 'portfolio'
                else None
            ),
            'signals_file': (
                self.signals_file_edit.text().strip() or None
                if input_mode == 'signals' else None
            ),
            'signals_format': (
                self.signals_format_combo.currentData()
                if input_mode == 'signals' else None
            ),
            'combo_bundle': (
                self.combo_bundle_edit.text().strip() or None
                if input_mode == 'combo' else None
            ),
            'combo_tag': (
                self.combo_tag_edit.text().strip() or None
                if input_mode == 'combo' else None
            ),
            'pool_source': pool_source,
            'symbols': symbols,
            'industry': None,
            'start_date': self.start_date.date().toString('yyyyMMdd'),
            'end_date': self.end_date.date().toString('yyyyMMdd'),
            'initial_cash': self.initial_cash.value(),
            'slippage': self.slippage.value() / 100,  # 百分比转小数
            'commission': self.commission.value() / 10000,  # 万分比转小数
            'stamp_duty': (
                'auto'
                if self.stamp_duty_mode.currentData() == 'auto'
                else self.stamp_duty.value() / 100
            ),
            'transfer_fee_rate': self.transfer_fee_rate.value() / 10000,
            'min_commission': self.min_commission.value(),  # 元
            'lot_size': self.lot_size.value(),
            'liquidity_cap_ratio': (
                self.liquidity_cap_ratio.value() / 100.0
            ),
            'open_buy_resize_mode': self.open_buy_resize_mode.currentData(),
            'open_buy_fill_mode': self.open_buy_fill_mode.currentData(),
            'open_buy_sizing': self.open_buy_sizing.currentData(),
            'limit_pct_overrides': self._limit_pct_overrides_config(),
            't0_overrides': self._t0_overrides_config(),
            'trading_days_per_year': self.trading_days_per_year.value(),
            'risk_free_rate': self.risk_free_rate.value() / 100.0,
            'freq': 'daily' if self.freq_daily.isChecked() else 'minute',
            'mode': mode,
            'strategy_kind': 'single' if mode == 'independent' else 'portfolio',
            'max_workers': self.workers_spin.value(),
            'price_mode': self.price_mode_combo.currentData(),
        }
        config.update(self._auction_config())
        return config

    def set_mode_enabled(self, enabled: bool) -> None:
        """Prevent contract changes while a worker is active."""
        self.mode_portfolio.setEnabled(enabled)
        self.mode_independent.setEnabled(enabled)
        self.input_mode_combo.setEnabled(
            enabled and not self.mode_independent.isChecked()
        )
        self.strategy_frame.setEnabled(
            enabled
            and not self.mode_independent.isChecked()
            and self.input_mode_combo.currentData() == 'strategy'
        )
        self.signals_frame.setEnabled(
            enabled and self.input_mode_combo.currentData() == 'signals'
        )
        self.combo_frame.setEnabled(
            enabled and self.input_mode_combo.currentData() == 'combo'
        )
        self.price_mode_combo.setEnabled(enabled)
        for control in self._execution_controls:
            control.setEnabled(enabled)
        for combo, spin in self._auction_controls.values():
            combo.setEnabled(enabled)
            spin.setEnabled(enabled and combo.currentData() != 'unconfigured')

    def set_config(self, config: dict) -> None:
        """
        设置配置

        Args:
            config: 配置字典
        """
        if config.get('data_root'):
            self.data_root_edit.setText(str(config['data_root']))
        strategy_params = config.get('strategy_params')
        if strategy_params is None:
            self.strategy_params_edit.clear()
        elif type(strategy_params) is dict:
            self.strategy_params_edit.setText(json.dumps(
                strategy_params,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            ))
        else:
            self.strategy_params_edit.setText(str(strategy_params))
        self.signals_file_edit.setText(str(config.get('signals_file') or ''))
        signals_format = config.get('signals_format') or 'auto'
        signals_format_index = self.signals_format_combo.findData(
            signals_format
        )
        if signals_format_index >= 0:
            self.signals_format_combo.setCurrentIndex(signals_format_index)
        self.combo_bundle_edit.setText(str(config.get('combo_bundle') or ''))
        self.combo_tag_edit.setText(str(config.get('combo_tag') or ''))

        input_mode = config.get('input_mode')
        if input_mode not in {'strategy', 'signals', 'combo'}:
            if config.get('signals_file'):
                input_mode = 'signals'
            elif config.get('combo_bundle'):
                input_mode = 'combo'
            else:
                input_mode = 'strategy'
        input_index = self.input_mode_combo.findData(input_mode)
        if input_index >= 0:
            self.input_mode_combo.setCurrentIndex(input_index)
        self._on_input_mode_changed(emit=False)

        pool_source = config.get('pool_source', 'specified')
        if pool_source == 'specified':
            self.pool_specified.setChecked(True)
            symbols = config.get('symbols', [])
            if symbols:
                self.symbols_edit.setText('\n'.join(symbols))
        else:
            self.pool_all.setChecked(True)

        if config.get('start_date'):
            date_str = config['start_date']
            if type(date_str) is not str:
                raise ValueError('start_date must be YYYYMMDD text')
            date = QDate.fromString(date_str, 'yyyyMMdd')
            if not date.isValid() or date.toString('yyyyMMdd') != date_str:
                raise ValueError('start_date must be valid YYYYMMDD text')
            self.start_date.setDate(date)
            if self.start_date.date().toString('yyyyMMdd') != date_str:
                raise ValueError(
                    'start_date is outside the GUI representable range'
                )

        if config.get('end_date'):
            date_str = config['end_date']
            if type(date_str) is not str:
                raise ValueError('end_date must be YYYYMMDD text')
            date = QDate.fromString(date_str, 'yyyyMMdd')
            if not date.isValid() or date.toString('yyyyMMdd') != date_str:
                raise ValueError('end_date must be valid YYYYMMDD text')
            self.end_date.setDate(date)
            if self.end_date.date().toString('yyyyMMdd') != date_str:
                raise ValueError(
                    'end_date is outside the GUI representable range'
                )

        if config.get('initial_cash') is not None:
            self._set_exact_double(
                self.initial_cash,
                config['initial_cash'],
                name='initial_cash',
                minimum=0.0,
                minimum_inclusive=False,
            )
        if config.get('slippage') is not None:
            self._set_exact_double(
                self.slippage,
                config['slippage'],
                name='slippage',
                scale=100.0,
                minimum=0.0,
                maximum=1.0,
                maximum_inclusive=False,
            )
        if config.get('commission') is not None:
            self._set_exact_double(
                self.commission,
                config['commission'],
                name='commission',
                scale=10_000.0,
                minimum=0.0,
            )
        stamp_duty = config.get('stamp_duty')
        if stamp_duty == 'auto':
            self.stamp_duty_mode.setCurrentIndex(
                self.stamp_duty_mode.findData('auto')
            )
        elif stamp_duty is not None:
            self.stamp_duty_mode.setCurrentIndex(
                self.stamp_duty_mode.findData('fixed')
            )
            self._set_exact_double(
                self.stamp_duty,
                stamp_duty,
                name='stamp_duty',
                scale=100.0,
                minimum=0.0,
            )
        self._on_stamp_duty_mode_changed(emit=False)
        if config.get('transfer_fee_rate') is not None:
            self._set_exact_double(
                self.transfer_fee_rate,
                config['transfer_fee_rate'],
                name='transfer_fee_rate',
                scale=10_000.0,
                minimum=0.0,
            )

        if config.get('min_commission') is not None:
            self._set_exact_double(
                self.min_commission,
                config['min_commission'],
                name='min_commission',
                minimum=0.0,
            )
        lot_size = config.get('lot_size', 100)
        if (
            type(lot_size) is not int
            or not self.lot_size.minimum() <= lot_size <= self.lot_size.maximum()
        ):
            raise ValueError('lot_size is outside the GUI representable range')
        self.lot_size.setValue(lot_size)
        if self.lot_size.value() != lot_size:
            raise ValueError('lot_size cannot be represented exactly by the GUI')
        liquidity_cap_ratio = config.get('liquidity_cap_ratio', 0.8)
        self._set_exact_double(
            self.liquidity_cap_ratio,
            liquidity_cap_ratio,
            name='liquidity_cap_ratio',
            scale=100.0,
            minimum=0.0,
            maximum=1.0,
        )
        for control, key, default in (
            (self.open_buy_resize_mode, 'open_buy_resize_mode', 'auto'),
            (self.open_buy_fill_mode, 'open_buy_fill_mode', 'open+slip'),
            (self.open_buy_sizing, 'open_buy_sizing', 'limit_up'),
        ):
            value = config.get(key, default)
            index = control.findData(value)
            if index < 0:
                raise ValueError(f'unsupported {key}: {value!r}')
            control.setCurrentIndex(index)
        limit_overrides = config.get('limit_pct_overrides')
        if limit_overrides is None:
            self.limit_pct_overrides_edit.clear()
        elif type(limit_overrides) is dict:
            self.limit_pct_overrides_edit.setText(json.dumps(
                limit_overrides,
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
            ))
        else:
            self.limit_pct_overrides_edit.setText(str(limit_overrides))
        t0_overrides = config.get('t0_overrides')
        if t0_overrides is None:
            self.t0_overrides_edit.clear()
        elif isinstance(t0_overrides, (list, tuple, set, frozenset)):
            self.t0_overrides_edit.setText(','.join(
                str(value) for value in t0_overrides
            ))
        else:
            self.t0_overrides_edit.setText(str(t0_overrides))
        trading_days = config.get('trading_days_per_year', 252)
        if (
            type(trading_days) is not int
            or not self.trading_days_per_year.minimum()
            <= trading_days
            <= self.trading_days_per_year.maximum()
        ):
            raise ValueError(
                'trading_days_per_year is outside the GUI representable range'
            )
        self.trading_days_per_year.setValue(trading_days)
        if self.trading_days_per_year.value() != trading_days:
            raise ValueError(
                'trading_days_per_year cannot be represented exactly by the GUI'
            )
        risk_free_rate = config.get('risk_free_rate', 0.03)
        self._set_exact_double(
            self.risk_free_rate,
            risk_free_rate,
            name='risk_free_rate',
            scale=100.0,
        )
        price_mode = config.get('price_mode') or 'dual'
        if price_mode not in {'dual', 'raw', 'hfq'}:
            raise ValueError(f'unsupported price_mode: {price_mode!r}')
        index = self.price_mode_combo.findData(price_mode)
        if index < 0:
            raise ValueError(f'unsupported price_mode: {price_mode!r}')
        self.price_mode_combo.setCurrentIndex(index)
        if self.price_mode_combo.currentData() != price_mode:
            raise ValueError('price_mode cannot be represented exactly by the GUI')

        auction_keys = {
            'daily_open_cap_yuan',
            'daily_close_cap_yuan',
            'daily_open_previous_day_ratio',
            'daily_close_previous_day_ratio',
        }
        if auction_keys.intersection(config):
            for window, (combo, spin) in self._auction_controls.items():
                fixed = config.get(f'daily_{window}_cap_yuan')
                ratio = config.get(
                    f'daily_{window}_previous_day_ratio'
                )
                if fixed is not None and ratio is not None:
                    raise ValueError(
                        f'daily_{window} auction cannot configure both '
                        'fixed capacity and previous-day ratio'
                    )
                if fixed is not None:
                    combo.setCurrentIndex(combo.findData('fixed_yuan'))
                    self._on_auction_mode_changed(window, emit=False)
                    self._set_exact_double(
                        spin,
                        fixed,
                        name=f'daily_{window}_cap_yuan',
                        minimum=0.0,
                        minimum_inclusive=False,
                    )
                elif ratio is not None:
                    combo.setCurrentIndex(
                        combo.findData('previous_day_ratio')
                    )
                    self._on_auction_mode_changed(window, emit=False)
                    self._set_exact_double(
                        spin,
                        ratio,
                        name=f'daily_{window}_previous_day_ratio',
                        scale=100.0,
                        minimum=0.0,
                        maximum=1.0,
                        minimum_inclusive=False,
                    )
                else:
                    combo.setCurrentIndex(combo.findData('unconfigured'))
                    self._on_auction_mode_changed(window, emit=False)
        frequency = config.get('freq', 'daily')
        if frequency not in {'daily', 'minute'}:
            raise ValueError(f'unsupported freq: {frequency!r}')
        if frequency == 'minute':
            self.freq_minute.setChecked(True)
        else:
            self.freq_daily.setChecked(True)
        restored_frequency = (
            'daily' if self.freq_daily.isChecked() else 'minute'
        )
        if restored_frequency != frequency:
            raise ValueError('freq cannot be represented exactly by the GUI')

        # 回测模式
        if config.get('mode') == 'independent':
            self.mode_independent.setChecked(True)
        else:
            self.mode_portfolio.setChecked(True)
        self.input_mode_combo.setEnabled(not self.mode_independent.isChecked())

        if config.get('max_workers'):
            self.workers_spin.setValue(config['max_workers'])

    def _on_import_file(self):
        """从文件导入股票列表"""
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择股票列表文件",
            "",
            "文本文件 (*.txt *.md);;所有文件 (*)"
        )
        
        if not file_path:
            return
            
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                
            # 解析: 按行分割，保留非空行
            symbols = []
            for line in content.split('\n'):
                line = line.strip()
                if line and not line.startswith('#'): # 忽略空行和注释
                    symbols.append(line)
            
            if symbols:
                self.symbols_edit.setText('\n'.join(symbols))
                self.pool_specified.setChecked(True) # 自动切换到指定模式
                QMessageBox.information(self, "导入成功", f"成功导入 {len(symbols)} 只股票")
            else:
                QMessageBox.warning(self, "提示", "文件中未找到有效内容")
                
        except Exception as e:
            QMessageBox.critical(self, "导入失败", str(e))
