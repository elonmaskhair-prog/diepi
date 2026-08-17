"""
回测结果界面

Screen 2: 显示回测结果
"""

from typing import List, Dict
import math
import json

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QFrame, QTabWidget, QProgressBar, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QGroupBox, QGridLayout, QComboBox,
    QScrollArea, QSplitter, QMessageBox, QApplication, QDateEdit
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from datetime import datetime, timedelta
import pandas as pd

from ..styles import Colors, Fonts, Styles
from ...comparison import (
    ComparisonBundle,
    ComparisonStatus,
    ReferenceIndexResult,
)
from ...engine.parallel_runner import ParallelResult
from ...result_contract import ResultContract
from ...data.source_evidence import (
    artifact_price_mode,
    artifact_symbols,
    display_price_mode,
    load_verified_display_daily_source,
    verify_display_daily_source,
)

# 尝试导入 pyqtgraph
try:
    import pyqtgraph as pg
    from pyqtgraph import AxisItem
    HAS_PYQTGRAPH = True
except ImportError:
    HAS_PYQTGRAPH = False


def _result_initial_cash(result) -> float:
    """Read the engine's initial-NAV truth instead of reverse engineering it."""
    try:
        initial_cash = float(result.initial_cash)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("result.initial_cash must be numeric") from exc
    if not math.isfinite(initial_cash) or initial_cash <= 0:
        raise ValueError("result.initial_cash must be finite and positive")
    return initial_cash


def _normalize_nav(values, initial_cash: float) -> List[float]:
    """Normalize every close NAV against the pre-window initial NAV."""
    if not math.isfinite(float(initial_cash)) or float(initial_cash) <= 0:
        raise ValueError("initial_cash must be finite and positive")
    normalized = []
    for value in values:
        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("NAV values must be finite")
        normalized.append(numeric / float(initial_cash))
    return normalized


def format_result_contract(contract) -> str:
    """Format every user-relevant ResultContract field without inference."""
    if type(contract) is not ResultContract:
        return (
            "状态: LEGACY_UNCLASSIFIED\n"
            "可排名: 否\n"
            "原因: 缺少已验证的 ResultContract\n"
            "实际区间: 不可用\n"
            "覆盖率: 不可用\n"
            "警告: 结果来源无法按当前契约分类"
        )

    reason = (
        "无"
        if contract.reason is None
        else f"{contract.reason.code}: {contract.reason.message}"
    )
    interval = (
        "不可用"
        if contract.actual_interval is None
        else (
            f"{contract.actual_interval.start_date} ~ "
            f"{contract.actual_interval.end_date}"
        )
    )
    coverage = contract.data_coverage
    coverage_text = (
        "不可用"
        if coverage is None
        else (
            f"{coverage.actual_observations}/"
            f"{coverage.expected_observations} "
            f"({coverage.ratio:.2%})，缺失 "
            f"{coverage.missing_observations}"
        )
    )
    warnings = (
        "无"
        if not contract.warnings
        else "\n".join(
            f"  - {item.code}: {item.message}"
            for item in contract.warnings
        )
    )
    assumptions = (
        "无"
        if not contract.assumptions
        else "\n".join(
            f"  - {item.key}: {item.value}"
            for item in contract.assumptions
        )
    )
    return (
        f"状态: {contract.status.value}\n"
        f"可排名: {'是' if contract.is_rankable else '否'}\n"
        f"原因: {reason}\n"
        f"实际区间: {interval}\n"
        f"覆盖率: {coverage_text}\n"
        f"警告: {warnings}\n"
        f"假设: {assumptions}"
    )


def format_parallel_result(result: ParallelResult) -> str:
    """Describe aggregate evidence without inventing a ResultContract."""
    scope = result.ranking_scope
    if scope is None:
        interval = "不可用"
        coverage = "不可用"
    else:
        interval = f"{scope[0]} ~ {scope[1]}"
        coverage = f"{scope[3]}/{scope[2]}"
    reason = result.ranking_error or "无"
    warnings = (
        "无" if not result.universe_warnings
        else "\n".join(f"  - {item}" for item in result.universe_warnings)
    )
    assumptions = (
        "无" if not result.universe_assumptions
        else "\n".join(
            f"  - {key}: {value}"
            for key, value in sorted(result.universe_assumptions.items())
        )
    )
    return (
        "状态: 独立聚合（无聚合 ResultContract）\n"
        f"可排名: {'是' if result.is_rankable else '否'}\n"
        f"原因: {reason}\n"
        f"标的覆盖: {result.success_count}/{result.total_symbols}，"
        f"失败 {result.failed_count}\n"
        f"实际区间: {interval}\n"
        f"观察覆盖: {coverage}\n"
        f"警告: {warnings}\n"
        f"假设: {assumptions}"
    )


def _validated_comparison_view(result):
    """Expose only engine-attached, typed, scope-validated comparison data."""
    bundle = getattr(result, 'comparisons', None)
    if type(bundle) is not ComparisonBundle:
        return None, "不可用：引擎未提供 ComparisonBundle"
    reference = bundle.reference_index_total_return
    if type(reference) is not ReferenceIndexResult:
        return None, "不可用：ComparisonBundle 未包含总收益指数"
    if reference.status is not ComparisonStatus.SUCCESS:
        reason = reference.reason
        detail = (
            reference.status.value
            if reason is None
            else f"{reference.status.value} / {reason.code}: {reason.message}"
        )
        return None, f"不可用：{detail}"
    excess = getattr(result, 'reference_total_return_excess', None)
    if excess is None:
        return None, "不可用：策略终态或观察区间与比较证据不完全一致"
    series = reference.series
    values = tuple(series.normalized_nav)
    if len(values) != len(bundle.scope.observation_ids):
        return None, "不可用：比较曲线长度与已验证观察区间不一致"
    return {
        'code': reference.spec.code,
        'source_id': reference.spec.source_id,
        'source_version': reference.spec.source_version,
        'values': values,
        'reference_return': reference.total_return,
        'excess_return': float(excess),
    }, None


class DateAxisItem(AxisItem):
    """自定义日期轴 - 只显示每月第一个交易日"""

    def __init__(self, dates, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dates = dates  # 日期列表 (datetime)
        self._month_first_indices = self._find_month_first()

    def _find_month_first(self):
        """找出每月第一个交易日的索引"""
        indices = {}
        for i, dt in enumerate(self.dates):
            key = (dt.year, dt.month)
            if key not in indices:
                indices[key] = i
        return list(indices.values())

    def tickStrings(self, values, scale, spacing):
        """生成刻度标签"""
        result = []
        for v in values:
            idx = int(v)
            if 0 <= idx < len(self.dates):
                if idx in self._month_first_indices:
                    dt = self.dates[idx]
                    result.append(dt.strftime('%Y-%m'))
                else:
                    result.append('')
            else:
                result.append('')
        return result

    def tickValues(self, minVal, maxVal, size):
        """返回刻度值 - 只返回月初的位置"""
        ticks = []
        for idx in self._month_first_indices:
            if minVal <= idx <= maxVal:
                ticks.append(idx)
        return [(1, ticks)]  # spacing=1


class ResultScreen(QWidget):
    """
    回测结果界面

    Signals:
        back_to_editor: 返回编辑器
        stop_backtest: 停止回测
        save_result: 保存回测结果
    """

    back_to_editor = Signal()
    stop_backtest = Signal()
    save_result = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._market_config = {}
        self._market_provenance = None
        self._market_data_root = None
        self._historical_artifact_view = False
        self._market_fingerprint_required = False
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        # ==================== 主分割器（垂直方向） ====================
        main_splitter = QSplitter(Qt.Vertical)
        main_splitter.setHandleWidth(6)
        main_splitter.setStyleSheet(f"""
            QSplitter::handle {{
                background-color: {Colors.BORDER};
            }}
            QSplitter::handle:hover {{
                background-color: {Colors.ACCENT_BLUE};
            }}
        """)

        # ==================== 顶部统计摘要 ====================
        stats_frame = QFrame()
        stats_frame.setStyleSheet(f"""
            QFrame {{
                background-color: {Colors.BG_SECONDARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)
        stats_main_layout = QVBoxLayout(stats_frame)
        stats_main_layout.setSpacing(8)
        stats_main_layout.setContentsMargins(16, 12, 16, 12)

        # 使用正常字号；Qt 根据系统 DPI 缩放。
        stats_title = QLabel("统计摘要")
        stats_title.setStyleSheet(f"""
            font-size: 18px;
            font-weight: 600;
            color: {Colors.TEXT_PRIMARY};
            background: transparent;
        """)
        stats_main_layout.addWidget(stats_title)

        self.contract_summary = QLabel(
            "状态: 尚无结果\n可排名: 否\n实际区间/覆盖率: 不可用"
        )
        self.contract_summary.setWordWrap(True)
        self.contract_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.contract_summary.setStyleSheet(f"""
            color: {Colors.TEXT_SECONDARY};
            background-color: {Colors.BG_TERTIARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 4px;
            padding: 8px;
            font-size: 12px;
        """)
        stats_main_layout.addWidget(self.contract_summary)

        self.stat_labels = {}
        stats = [
            ('total_return', '总收益率'),
            ('annual_return', '年化收益'),
            ('max_drawdown', '最大回撤'),
            ('sharpe_ratio', '夏普比率'),
            ('trade_count', '交易次数'),
            ('final_value', '最终资产'),
            ('benchmark_return', '基准收益'),
            ('excess_return', '超额收益'),
        ]

        # 分成2行，每行4个
        rows_data = [stats[0:4], stats[4:8]]

        for row_stats in rows_data:
            row_layout = QHBoxLayout()
            row_layout.setSpacing(24)

            for key, name in row_stats:
                # 每个统计项使用QFrame确保样式正确
                item_frame = QFrame()
                item_frame.setStyleSheet("background: transparent; border: none;")
                item_layout = QVBoxLayout(item_frame)
                item_layout.setContentsMargins(0, 0, 0, 0)
                item_layout.setSpacing(4)

                # 指标名称
                name_label = QLabel(name)
                name_label.setStyleSheet(f"""
                    font-size: 12px;
                    color: {Colors.TEXT_SECONDARY};
                    background: transparent;
                    border: none;
                """)
                item_layout.addWidget(name_label)

                # 指标数值
                value_label = QLabel("--")
                value_label.setStyleSheet(f"""
                    font-weight: 600;
                    font-size: 24px;
                    color: {Colors.TEXT_PRIMARY};
                    background: transparent;
                    border: none;
                """)
                self.stat_labels[key] = value_label
                item_layout.addWidget(value_label)

                row_layout.addWidget(item_frame, 1)

            stats_main_layout.addLayout(row_layout)

        stats_frame.setMinimumHeight(220)
        main_splitter.addWidget(stats_frame)

        # ==================== 资产曲线 ====================
        chart_frame = QFrame()
        chart_frame.setStyleSheet(f"""
            QFrame {{
                background-color: {Colors.BG_SECONDARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)
        chart_layout = QVBoxLayout(chart_frame)
        chart_layout.setContentsMargins(12, 12, 12, 12)

        # 比较数据只消费引擎输出的 ComparisonBundle，不在 GUI 拉取或补值。
        benchmark_layout = QHBoxLayout()
        benchmark_layout.addStretch()
        benchmark_label = QLabel("比较证据:")
        benchmark_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 12px;")
        benchmark_layout.addWidget(benchmark_label)

        self.benchmark_combo = QComboBox()
        self.benchmark_combo.addItem("仅使用引擎已验证比较", "")
        self.benchmark_combo.setEnabled(False)
        self.benchmark_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {Colors.BG_TERTIARY};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 4px 8px;
                min-width: 150px;
            }}
            QComboBox:hover {{
                border-color: {Colors.ACCENT_BLUE};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 20px;
            }}
            QComboBox QAbstractItemView {{
                background-color: {Colors.BG_SECONDARY};
                color: {Colors.TEXT_PRIMARY};
                selection-background-color: {Colors.ACCENT_BLUE};
            }}
        """)
        benchmark_layout.addWidget(self.benchmark_combo)

        self.comparison_status_label = QLabel("不可用：尚无结果")
        self.comparison_status_label.setWordWrap(True)
        self.comparison_status_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: 12px;"
        )
        benchmark_layout.addWidget(self.comparison_status_label, 1)

        chart_layout.addLayout(benchmark_layout)

        if HAS_PYQTGRAPH:
            self.chart_widget = pg.PlotWidget()
            self.chart_widget.setBackground(Colors.BG_DARK)
            self.chart_widget.showGrid(x=True, y=True, alpha=0.2)
            self.chart_widget.setLabel('left', '资产', units='元', color=Colors.TEXT_SECONDARY)
            self.chart_widget.setLabel('bottom', '日期', color=Colors.TEXT_SECONDARY)
            self.chart_widget.getAxis('left').setTextPen(Colors.TEXT_SECONDARY)
            self.chart_widget.getAxis('bottom').setTextPen(Colors.TEXT_SECONDARY)
            chart_layout.addWidget(self.chart_widget)
        else:
            self.chart_widget = QLabel("需要安装 pyqtgraph 才能显示图表\npip install pyqtgraph")
            self.chart_widget.setAlignment(Qt.AlignCenter)
            self.chart_widget.setStyleSheet(f"color: {Colors.TEXT_MUTED}; padding: 50px; background: transparent;")
            chart_layout.addWidget(self.chart_widget)

        self.drawdown_status_label = QLabel("回撤曲线: 尚无结果")
        self.drawdown_status_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: 12px;"
        )
        chart_layout.addWidget(self.drawdown_status_label)
        if HAS_PYQTGRAPH:
            self.drawdown_widget = pg.PlotWidget()
            self.drawdown_widget.setBackground(Colors.BG_DARK)
            self.drawdown_widget.showGrid(x=True, y=True, alpha=0.15)
            self.drawdown_widget.setLabel(
                'left', '收盘净值回撤', units='%',
                color=Colors.TEXT_SECONDARY,
            )
            self.drawdown_widget.getAxis('left').setTextPen(
                Colors.TEXT_SECONDARY
            )
            self.drawdown_widget.getAxis('bottom').setTextPen(
                Colors.TEXT_SECONDARY
            )
            self.drawdown_widget.setMinimumHeight(80)
            self.drawdown_widget.setMaximumHeight(150)
            chart_layout.addWidget(self.drawdown_widget)
        else:
            self.drawdown_widget = QLabel("需要 pyqtgraph 才能显示回撤曲线")
            self.drawdown_widget.setAlignment(Qt.AlignCenter)
            chart_layout.addWidget(self.drawdown_widget)

        chart_frame.setMinimumHeight(150)
        main_splitter.addWidget(chart_frame)

        # ==================== 标签页 ====================
        self.tabs = QTabWidget()
        tabs = self.tabs

        # 交易记录 - 增加盈亏列
        # 交易记录 - 增加盈亏列
        trades_tab = QWidget()
        trades_layout = QVBoxLayout(trades_tab)
        trades_layout.setContentsMargins(4, 4, 4, 4)

        # 导航条
        self.trades_date_edit, trades_nav = self._create_nav_bar(
            "跳转日期", self._on_jump_trades
        )
        trades_layout.addWidget(trades_nav)

        self.trades_table = QTableWidget()
        self.trades_table.setColumnCount(9)
        self.trades_table.setHorizontalHeaderLabels([
            '日期', '股票', '方向', '数量', '价格', '金额', '盈亏', '盈亏%', '备注'
        ])
        self.trades_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.trades_table.setAlternatingRowColors(True)
        self.trades_table.verticalHeader().setVisible(False)
        self.trades_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.trades_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.trades_table.doubleClicked.connect(
            self._on_trade_double_clicked
        )
        trades_layout.addWidget(self.trades_table)

        # 加载全部按钮
        self.btn_load_all_trades = QPushButton("显示全部交易记录")
        self.btn_load_all_trades.setStyleSheet(Styles.BTN_SECONDARY)
        self.btn_load_all_trades.setCursor(Qt.PointingHandCursor)
        self.btn_load_all_trades.clicked.connect(self._on_load_all_trades)
        self.btn_load_all_trades.setVisible(False)
        trades_layout.addWidget(self.btn_load_all_trades)

        tabs.addTab(trades_tab, "交易记录")

        # 每日持仓
        # 每日持仓
        pos_tab = QWidget()
        pos_layout = QVBoxLayout(pos_tab)
        pos_layout.setContentsMargins(4, 4, 4, 4)

        # 导航条
        self.pos_date_edit, pos_nav = self._create_nav_bar(
            "跳转日期", self._on_jump_positions
        )
        pos_layout.addWidget(pos_nav)

        self.position_table = QTableWidget()
        self.position_table.setColumnCount(7)
        self.position_table.setHorizontalHeaderLabels([
            '日期', '股票', '持仓', '成本', '现价', '市值', '盈亏'
        ])
        self.position_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.position_table.setAlternatingRowColors(True)
        self.position_table.verticalHeader().setVisible(False)
        pos_layout.addWidget(self.position_table)

        # 加载全部按钮
        self.btn_load_all_positions = QPushButton("显示全部持仓历史 (数据量大可能造成卡顿)")
        self.btn_load_all_positions.setStyleSheet(Styles.BTN_SECONDARY)
        self.btn_load_all_positions.setCursor(Qt.PointingHandCursor)
        self.btn_load_all_positions.clicked.connect(self._on_load_all_positions)
        self.btn_load_all_positions.setVisible(False)
        pos_layout.addWidget(self.btn_load_all_positions)

        tabs.addTab(pos_tab, "每日持仓")

        # 订单/执行事件只展示引擎写入的不可变 journal，不推测缺失状态。
        events_tab = QWidget()
        events_layout = QVBoxLayout(events_tab)
        events_layout.setContentsMargins(4, 4, 4, 4)
        self.events_status_label = QLabel("尚无执行事件证据")
        self.events_status_label.setWordWrap(True)
        self.events_status_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: 12px;"
        )
        events_layout.addWidget(self.events_status_label)
        self.events_table = QTableWidget()
        self.events_table.setColumnCount(6)
        self.events_table.setHorizontalHeaderLabels([
            '模拟时间', '事件', '股票', '订单ID', '原因', '原始详情'
        ])
        self.events_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.events_table.setAlternatingRowColors(True)
        self.events_table.verticalHeader().setVisible(False)
        self.events_table.setEditTriggers(QTableWidget.NoEditTriggers)
        events_layout.addWidget(self.events_table)
        tabs.addTab(events_tab, "订单事件")

        # K线图标签页
        kline_tab = QWidget()
        kline_layout = QVBoxLayout(kline_tab)
        kline_layout.setContentsMargins(8, 8, 8, 8)
        kline_layout.setSpacing(8)

        self.kline_evidence_label = QLabel(
            "K线证据: 尚无结果。历史 Artifact 必须与当前本地行情指纹一致。"
        )
        self.kline_evidence_label.setWordWrap(True)
        self.kline_evidence_label.setTextInteractionFlags(
            Qt.TextSelectableByMouse
        )
        self.kline_evidence_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; "
            f"background-color: {Colors.BG_TERTIARY}; "
            f"border: 1px solid {Colors.BORDER}; padding: 6px;"
        )
        kline_layout.addWidget(self.kline_evidence_label)

        # 股票选择下拉框
        stock_selector_layout = QHBoxLayout()
        stock_label = QLabel("点击股票打开K线图:")
        stock_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 12px;")
        stock_selector_layout.addWidget(stock_label)
        stock_selector_layout.addStretch()
        kline_layout.addLayout(stock_selector_layout)

        # 股票列表 (使用表格代替下拉框，点击打开弹窗)
        self.stock_table = QTableWidget()
        self.stock_table.setColumnCount(4)
        self.stock_table.setHorizontalHeaderLabels(['股票代码', '名称', '盈亏', '交易次数'])
        self.stock_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.stock_table.setAlternatingRowColors(True)
        self.stock_table.verticalHeader().setVisible(False)
        self.stock_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.stock_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.stock_table.doubleClicked.connect(self._on_stock_double_clicked)
        kline_layout.addWidget(self.stock_table, stretch=1)

        self.stock_trade_label = QLabel("个股成交明细: 尚未选择标的")
        self.stock_trade_label.setStyleSheet(
            f"color: {Colors.TEXT_SECONDARY}; font-size: 12px;"
        )
        kline_layout.addWidget(self.stock_trade_label)
        self.stock_trade_table = QTableWidget()
        self.stock_trade_table.setColumnCount(7)
        self.stock_trade_table.setHorizontalHeaderLabels([
            '日期', '方向', '数量', '价格', '金额', '盈亏', '备注'
        ])
        self.stock_trade_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.stock_trade_table.setAlternatingRowColors(True)
        self.stock_trade_table.verticalHeader().setVisible(False)
        self.stock_trade_table.setEditTriggers(QTableWidget.NoEditTriggers)
        kline_layout.addWidget(self.stock_trade_table, stretch=1)

        # K线弹窗
        from ..widgets.kline_dialog import KLineDialog
        self.kline_dialog = KLineDialog(self)

        self._kline_tab_index = tabs.addTab(kline_tab, "K线图")

        # 收益归因标签页
        attribution_tab = QWidget()
        attribution_layout = QVBoxLayout(attribution_tab)
        attribution_layout.setContentsMargins(8, 8, 8, 8)
        attribution_layout.setSpacing(12)

        # 按年份
        year_group = QGroupBox("按年份")
        year_layout = QVBoxLayout(year_group)
        self.year_table = QTableWidget()
        self.year_table.setColumnCount(4)
        self.year_table.setHorizontalHeaderLabels(['年份', '盈亏额', '盈亏%', '交易次数'])
        self.year_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.year_table.setAlternatingRowColors(True)
        self.year_table.verticalHeader().setVisible(False)
        year_layout.addWidget(self.year_table)
        attribution_layout.addWidget(year_group)

        # 按月份
        month_group = QGroupBox("按月份")
        month_layout = QVBoxLayout(month_group)
        self.month_table = QTableWidget()
        self.month_table.setColumnCount(4)
        self.month_table.setHorizontalHeaderLabels(['月份', '盈亏额', '盈亏%', '交易次数'])
        self.month_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.month_table.setAlternatingRowColors(True)
        self.month_table.verticalHeader().setVisible(False)
        month_layout.addWidget(self.month_table)
        attribution_layout.addWidget(month_group)

        # 按股票
        stock_group = QGroupBox("按股票")
        stock_layout = QVBoxLayout(stock_group)
        self.stock_attr_table = QTableWidget()
        self.stock_attr_table.setColumnCount(6)
        self.stock_attr_table.setHorizontalHeaderLabels(['股票', '名称', '盈亏额', '盈亏%', '交易次数', '胜率'])
        self.stock_attr_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.stock_attr_table.setAlternatingRowColors(True)
        self.stock_attr_table.verticalHeader().setVisible(False)
        stock_layout.addWidget(self.stock_attr_table)
        attribution_layout.addWidget(stock_group)

        tabs.addTab(attribution_tab, "收益归因")

        # 股票排行 (独立测试模式专用)
        self.parallel_table = QTableWidget()
        self.parallel_table.setColumnCount(7)
        self.parallel_table.setHorizontalHeaderLabels([
            '股票', '收益率', '年化', '最大回撤', '夏普', '交易次数', '胜率'
        ])
        self.parallel_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.parallel_table.setAlternatingRowColors(True)
        self.parallel_table.verticalHeader().setVisible(False)
        self.parallel_table.setSortingEnabled(True)  # 支持点击排序
        self.parallel_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.parallel_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.parallel_table.doubleClicked.connect(
            self._on_parallel_double_clicked
        )
        tabs.addTab(self.parallel_table, "股票排行")

        # 运行日志使用正常字号并交由系统 DPI 缩放。
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet(f"""
            QTextEdit {{
                font-family: {Fonts.FAMILY_MONO};
                font-size: 12px;
                background-color: {Colors.BG_DARK};
                color: {Colors.TEXT_PRIMARY};
                border: none;
                border-radius: 6px;
                padding: 16px;
            }}
        """)
        tabs.addTab(self.log_text, "运行日志")

        tabs.setMinimumHeight(150)
        main_splitter.addWidget(tabs)

        # 设置分割器初始比例 (统计:图表:标签页 = 2:3:2)
        main_splitter.setSizes([280, 300, 200])

        layout.addWidget(main_splitter, stretch=1)

        # ==================== 进度条 ====================
        progress_layout = QHBoxLayout()

        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        progress_layout.addWidget(self.progress_bar, stretch=1)

        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 12px;")
        progress_layout.addWidget(self.progress_label)

        layout.addLayout(progress_layout)

        # ==================== 底部按钮 ====================
        btn_layout = QHBoxLayout()

        self.back_btn = QPushButton("返回编辑")
        self.back_btn.setStyleSheet(Styles.BTN_SECONDARY)
        self.back_btn.setCursor(Qt.PointingHandCursor)
        self.back_btn.clicked.connect(self.back_to_editor.emit)
        btn_layout.addWidget(self.back_btn)

        self.back_parallel_btn = QPushButton("返回独立聚合")
        self.back_parallel_btn.setStyleSheet(Styles.BTN_SECONDARY)
        self.back_parallel_btn.setCursor(Qt.PointingHandCursor)
        self.back_parallel_btn.clicked.connect(
            self._on_back_parallel_result
        )
        self.back_parallel_btn.setVisible(False)
        btn_layout.addWidget(self.back_parallel_btn)

        btn_layout.addStretch()

        # 保存记录按钮
        self.save_btn = QPushButton("保存记录")
        self.save_btn.setStyleSheet(Styles.BTN_PRIMARY)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self.save_result.emit)
        self.save_btn.setEnabled(False)  # 默认禁用，回测完成后启用
        btn_layout.addWidget(self.save_btn)

        self.stop_btn = QPushButton("停止回测")
        self.stop_btn.setStyleSheet(Styles.BTN_DANGER)
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.clicked.connect(self.stop_backtest.emit)
        btn_layout.addWidget(self.stop_btn)

        layout.addLayout(btn_layout)

    def set_running(self, running: bool):
        """设置运行状态"""
        self.progress_bar.setVisible(running)
        self.stop_btn.setEnabled(running)
        self.back_btn.setEnabled(not running)
        # RunArtifact v1 同时支持组合与独立聚合结果。
        if not running and hasattr(self, '_current_result') and self._current_result is not None:
            self.save_btn.setEnabled(True)
        else:
            self.save_btn.setEnabled(False)

    def update_progress(self, current: int, total: int, message: str = ""):
        """更新进度"""
        if total > 0:
            self.progress_bar.setValue(int(current / total * 100))
        self.progress_label.setText(message)

    def display_result(self, result):
        """
        显示回测结果

        Args:
            result: PortfolioResult 或 ParallelResult 对象
        """
        # 检查结果类型
        from diepi.futures.result import FuturesResult

        if isinstance(result, FuturesResult):
            self._display_futures_result(result)
        elif isinstance(result, ParallelResult):
            self._display_parallel_result(result)
        else:
            self._display_portfolio_result(result)

    def set_market_data_context(
        self,
        *,
        config: dict,
        data_root,
        provenance=None,
        historical_artifact: bool,
        fingerprint_required=None,
    ) -> None:
        """Configure the optional local K-line view without changing results."""

        if type(config) is not dict:
            raise TypeError("market-data view config must be exactly dict")
        if type(historical_artifact) is not bool:
            raise TypeError("historical_artifact must be exactly bool")
        if fingerprint_required is None:
            fingerprint_required = historical_artifact
        if type(fingerprint_required) is not bool:
            raise TypeError("fingerprint_required must be exactly bool")
        self._market_config = dict(config)
        self._market_provenance = provenance
        self._market_data_root = data_root
        self._historical_artifact_view = historical_artifact
        self._market_fingerprint_required = fingerprint_required
        mode = artifact_price_mode(config)
        if fingerprint_required:
            if mode is None:
                text = (
                    "K线证据: 结果仍可查看；本次结果未记录 "
                    "price_mode，K线与成交叠加已禁用。"
                )
            else:
                source_label = (
                    "历史 Artifact" if historical_artifact else "本次运行"
                )
                text = (
                    f"K线证据: {source_label} · {mode}；选择标的后将用当前 "
                    "DATA_ROOT 做文件级 SHA-256 重验。"
                )
        else:
            text = (
                f"K线来源: 本次运行的当前本地数据 · {mode or '未知口径'}；"
                "保存为 Artifact 后会记录可重验文件指纹。"
            )
        self.kline_evidence_label.setText(text)

    def _kline_price_mode(self, symbol: str):
        """Return a proven display lane, or fail closed with a visible reason."""

        mode = artifact_price_mode(self._market_config)
        if not self._market_fingerprint_required:
            if mode is None:
                message = "当前运行未记录 price_mode，K线已禁用"
                self.kline_evidence_label.setText("K线证据: " + message)
                self.add_log(message)
                return None
            lane = display_price_mode(mode)
            self.kline_evidence_label.setText(
                f"K线来源: 本次运行的当前本地 {lane} 数据（尚非历史重验证明）"
            )
            return lane

        verification = verify_display_daily_source(
            self._market_provenance,
            data_root=self._market_data_root,
            symbol=symbol,
            price_mode=mode,
            scope_symbols=artifact_symbols(self._market_config),
        )
        prefix = "K线证据: 已通过" if verification.verified else (
            "K线证据: 未通过；已验证结果仍可查看，K线与成交叠加已禁用"
        )
        self.kline_evidence_label.setText(
            f"{prefix}。{verification.message}"
        )
        self.add_log(self.kline_evidence_label.text())
        return verification.price_mode if verification.verified else None

    def set_artifact_trust(
        self, *, artifact_format: str, verified: bool, rankable: bool
    ) -> None:
        """Display directory-level trust separately from engine result status."""
        if type(artifact_format) is not str:
            raise TypeError("artifact_format must be exactly str")
        if type(verified) is not bool or type(rankable) is not bool:
            raise TypeError("verified/rankable must be exactly bool")
        trust = (
            f"产物: {artifact_format} | "
            f"verified: {'是' if verified else '否'} | "
            f"产物可排名: {'是' if rankable else '否'}"
        )
        self.contract_summary.setText(
            trust + "\n" + self.contract_summary.text()
        )

    def _display_portfolio_result(self, result):
        """显示组合投资模式结果"""
        # 保存结果；所有终态（含 PARTIAL/INVALID/CANCELED）均可展示。
        self._current_result = result
        self._is_parallel_result = False
        self.contract_summary.setText(
            format_result_contract(getattr(result, 'result_contract', None))
        )
        self._comparison_view, comparison_error = (
            _validated_comparison_view(result)
        )
        
        # 初始化数据提供者用于查询股票名称
        from ...data import DataProvider
        try:
            requested_mode = artifact_price_mode(self._market_config) or 'dual'
            lane = display_price_mode(requested_mode)
            strategy_lane = 'hfq' if requested_mode == 'dual' else lane
            self._data_provider = DataProvider(
                data_root=self._market_data_root,
                price_mode=strategy_lane,
                execution_price_mode=lane,
            )
        except Exception:
            # 股票名称是辅助展示，不能阻止终态证据进入结果页。
            self._data_provider = None

        # 设置日期选择器范围
        if hasattr(result, 'daily_values') and not result.daily_values.empty:
            dates = result.daily_values.index
            if not dates.empty:
                min_date = pd.Timestamp(dates[0])
                max_date = pd.Timestamp(dates[-1])
                
                # QDate 需要 yyyy, mm, dd
                from PySide6.QtCore import QDate
                q_min = QDate(min_date.year, min_date.month, min_date.day)
                q_max = QDate(max_date.year, max_date.month, max_date.day)
                
                # 设置范围
                self.trades_date_edit.setDateRange(q_min, q_max)
                self.pos_date_edit.setDateRange(q_min, q_max)
                
                # 默认显示结束日期
                self.trades_date_edit.setDate(q_max)
                self.pos_date_edit.setDate(q_max)

        # 更新统计指标
        self.stat_labels['total_return'].setText(f"{result.total_return * 100:.2f}%")
        self.stat_labels['annual_return'].setText(f"{result.annual_return * 100:.2f}%")
        self.stat_labels['max_drawdown'].setText(f"{result.max_drawdown * 100:.2f}%")
        self.stat_labels['sharpe_ratio'].setText(
            "N/A" if result.sharpe_ratio is None
            else f"{result.sharpe_ratio:.3f}"
        )
        self.stat_labels['trade_count'].setText(str(result.trade_count))
        self.stat_labels['final_value'].setText(f"{result.final_value:,.0f}")

        if self._comparison_view is None:
            self.stat_labels['benchmark_return'].setText("不可用")
            self.stat_labels['excess_return'].setText("不可用")
            self.comparison_status_label.setText(comparison_error)
        else:
            view = self._comparison_view
            reference_return = view['reference_return']
            excess_return = view['excess_return']
            self.stat_labels['benchmark_return'].setText(
                f"{reference_return * 100:.2f}%")
            self.stat_labels['excess_return'].setText(
                f"{excess_return * 100:.2f}%")
            self.comparison_status_label.setText(
                "可用：引擎已验证总收益指数 "
                f"{view['code']} · {view['source_id']}@"
                f"{view['source_version']}"
            )

        # 根据收益率设置颜色
        if result.total_return >= 0:
            self.stat_labels['total_return'].setStyleSheet(f"""
                font-weight: 600; font-size: 24px; color: {Colors.ACCENT_GREEN};
                background: transparent; border: none;
            """)
        else:
            self.stat_labels['total_return'].setStyleSheet(f"""
                font-weight: 600; font-size: 24px; color: {Colors.ACCENT_RED};
                background: transparent; border: none;
            """)

        # 绘制资产曲线 - 带日期轴和十字光标
        if HAS_PYQTGRAPH and hasattr(result, 'daily_values') and not result.daily_values.empty:
            df = result.daily_values
            self._setup_chart_with_crosshair(df)
            self._setup_drawdown_chart(df)
        elif HAS_PYQTGRAPH:
            self.chart_widget.clear()
            self.drawdown_widget.clear()
            self.drawdown_status_label.setText("回撤曲线: 结果无每日净值")

        # 填充交易记录
        self._fill_trades_table(result.trades)
        self._fill_execution_events(result)

        # 填充持仓历史 (传入 daily_values 用于计算总资产)
        if hasattr(result, 'position_history'):
            daily_values = result.daily_values if hasattr(result, 'daily_values') else None
            self._fill_position_table(result.position_history, daily_values)

        # 填充K线图股票表格
        self._populate_stock_table()

        # 填充收益归因表格
        initial_capital = _result_initial_cash(result)
        self._fill_attribution_tables(result.trades, initial_capital)

    def _display_parallel_result(self, result: ParallelResult):
        """显示独立测试模式结果"""
        self._current_result = result
        self._is_parallel_result = True
        self._comparison_view = None
        self.contract_summary.setText(format_parallel_result(result))
        reason = result.comparison_reason
        self.comparison_status_label.setText(
            f"不可用：{reason.code}: {reason.message}"
        )

        # 更新统计指标 (显示平均值)
        self.stat_labels['total_return'].setText(f"{result.avg_return * 100:.2f}%")
        self.stat_labels['annual_return'].setText(f"{result.avg_annual_return * 100:.2f}%")
        self.stat_labels['max_drawdown'].setText(f"{result.avg_max_drawdown * 100:.2f}%")
        self.stat_labels['sharpe_ratio'].setText(
            "N/A" if result.avg_sharpe is None
            else f"{result.avg_sharpe:.3f}"
        )
        self.stat_labels['trade_count'].setText(f"{result.success_count}/{result.total_symbols}")
        self.stat_labels['final_value'].setText(f"平均: {result.initial_cash * (1 + result.avg_return):,.0f}")

        # 根据平均收益率设置颜色
        if result.avg_return >= 0:
            self.stat_labels['total_return'].setStyleSheet(f"""
                font-weight: 600; font-size: 24px; color: {Colors.ACCENT_GREEN};
                background: transparent; border: none;
            """)
        else:
            self.stat_labels['total_return'].setStyleSheet(f"""
                font-weight: 600; font-size: 24px; color: {Colors.ACCENT_RED};
                background: transparent; border: none;
            """)

        # 基准收益不适用于独立测试
        self.stat_labels['benchmark_return'].setText("不可用")
        self.stat_labels['excess_return'].setText("不可用")

        # 清空组合模式的表格
        self.trades_table.setRowCount(0)
        self.position_table.setRowCount(0)
        self.stock_table.setRowCount(0)
        self.stock_trade_table.setRowCount(0)
        self.events_table.setRowCount(0)
        self.events_status_label.setText(
            "独立聚合没有统一执行事件；双击股票查看 child 结果。"
        )

        # 清空资产曲线 (独立测试没有统一的资产曲线)
        if HAS_PYQTGRAPH:
            self.chart_widget.clear()
            self.drawdown_widget.clear()
            self.drawdown_status_label.setText("回撤曲线: 独立聚合无统一净值")
            # 显示提示文字
            text_item = pg.TextItem(
                "独立测试模式无统一资产曲线\n请在股票排行中查看详情",
                anchor=(0.5, 0.5),
                color=Colors.TEXT_SECONDARY
            )
            text_item.setFont(QFont('Microsoft YaHei', 14))
            self.chart_widget.addItem(text_item)

        # 填充股票排行表格
        self._fill_parallel_results_table(result)

        # 即使失败标的不可排名，也把可诊断错误显式带到结果页。
        if result.errors:
            self.add_log("独立模式失败/取消标的:")
            for symbol, error in sorted(result.errors.items()):
                self.add_log(f"  {symbol}: {error}")

    def _display_futures_result(self, result):
        """Show a bounded read-only summary for experimental futures v1."""

        self._current_result = result
        self._is_parallel_result = False
        self._comparison_view = None
        self.contract_summary.setText(
            "引擎: 股指期货日线近似研究（实验性，只读）\n"
            + format_result_contract(getattr(result, 'result_contract', None))
        )
        self.stat_labels['total_return'].setText(
            f"{float(result.total_return) * 100:.2f}%"
        )
        self.stat_labels['annual_return'].setText(
            f"{float(result.cagr) * 100:.2f}%"
        )
        self.stat_labels['max_drawdown'].setText(
            f"{float(result.max_drawdown_close) * 100:.2f}%"
        )
        self.stat_labels['sharpe_ratio'].setText(
            "N/A" if result.sharpe is None else f"{float(result.sharpe):.3f}"
        )
        self.stat_labels['trade_count'].setText(str(result.trade_count))
        self.stat_labels['final_value'].setText(f"{float(result.final_nav):,.0f}")
        self.stat_labels['benchmark_return'].setText("不可用")
        self.stat_labels['excess_return'].setText("不可用")
        self.comparison_status_label.setText(
            "不可用：实验性股指期货引擎不使用现金组合基准比较"
        )
        self.trades_table.setRowCount(0)
        self.position_table.setRowCount(0)
        self.parallel_table.setRowCount(0)
        self.stock_table.setRowCount(0)
        self.stock_trade_table.setRowCount(0)
        self.events_table.setRowCount(0)
        self.events_status_label.setText(
            "期货 v1 仅显示专用摘要；现金订单事件和个股下钻不适用。"
        )
        self.kline_evidence_label.setText(
            "K线证据: 现金 GUI 不为实验性期货 Artifact 加载或叠加本地行情。"
        )
        if HAS_PYQTGRAPH:
            self.chart_widget.clear()
            self.drawdown_widget.clear()
        self.drawdown_status_label.setText(
            "回撤曲线: 期货 v1 专用图表尚未接入；上方显示收盘净值最大回撤。"
        )
        self.add_log(
            "已按只读模式载入实验性股指期货 Artifact："
            f"{result.product}，{result.start_date}..{result.end_date}，"
            f"daily_nav={len(result.daily_nav)} 行，trades={len(result.trades)} 行。"
        )

    def _fill_parallel_results_table(self, result: ParallelResult):
        """填充独立测试结果排行表格"""
        # 暂时禁用排序以提高填充效率
        self.parallel_table.setSortingEnabled(False)

        # 获取结果并按收益率排序
        results_list = []
        for symbol, res in result.results.items():
            results_list.append({
                'symbol': symbol,
                'total_return': res.total_return,
                'annual_return': res.annual_return,
                'max_drawdown': res.max_drawdown,
                'sharpe_ratio': res.sharpe_ratio,
                'trade_count': res.trade_count,
                'win_rate': res.win_rate,
            })

        # 按收益率降序排序
        results_list.sort(key=lambda x: x['total_return'], reverse=True)

        self.parallel_table.setRowCount(len(results_list))

        for i, res in enumerate(results_list):
            # 股票代码
            self.parallel_table.setItem(i, 0, QTableWidgetItem(res['symbol']))

            # 收益率
            return_item = QTableWidgetItem(f"{res['total_return'] * 100:.2f}%")
            if res['total_return'] >= 0:
                return_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                return_item.setForeground(QColor(Colors.ACCENT_RED))
            self.parallel_table.setItem(i, 1, return_item)

            # 年化
            annual_item = QTableWidgetItem(f"{res['annual_return'] * 100:.2f}%")
            if res['annual_return'] >= 0:
                annual_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                annual_item.setForeground(QColor(Colors.ACCENT_RED))
            self.parallel_table.setItem(i, 2, annual_item)

            # 最大回撤
            dd_item = QTableWidgetItem(f"{res['max_drawdown'] * 100:.2f}%")
            dd_item.setForeground(QColor(Colors.ACCENT_RED))
            self.parallel_table.setItem(i, 3, dd_item)

            # 夏普比率
            sharpe = res['sharpe_ratio']
            sharpe_item = QTableWidgetItem(
                "N/A" if sharpe is None else f"{sharpe:.3f}"
            )
            if sharpe is not None and sharpe >= 1:
                sharpe_item.setForeground(QColor(Colors.ACCENT_GREEN))
            elif sharpe is not None and sharpe < 0:
                sharpe_item.setForeground(QColor(Colors.ACCENT_RED))
            self.parallel_table.setItem(i, 4, sharpe_item)

            # 交易次数
            self.parallel_table.setItem(i, 5, QTableWidgetItem(str(res['trade_count'])))

            # 胜率
            win_rate = res['win_rate']
            win_item = QTableWidgetItem(
                "N/A" if win_rate is None else f"{win_rate * 100:.1f}%"
            )
            if win_rate is not None and win_rate >= 0.5:
                win_item.setForeground(QColor(Colors.ACCENT_GREEN))
            self.parallel_table.setItem(i, 6, win_item)

        # 重新启用排序
        self.parallel_table.setSortingEnabled(True)

    def _on_parallel_double_clicked(self, index):
        """Open one fully recorded child result from the aggregate artifact."""

        aggregate = getattr(self, '_current_result', None)
        if not isinstance(aggregate, ParallelResult):
            return
        symbol_item = self.parallel_table.item(index.row(), 0)
        if symbol_item is None:
            return
        symbol = symbol_item.text()
        child = aggregate.results.get(symbol)
        if child is None:
            return
        self._parallel_parent_result = aggregate
        self._parallel_parent_save_enabled = self.save_btn.isEnabled()
        self._display_portfolio_result(child)
        self.contract_summary.setText(
            f"独立 child: {symbol}\n" + self.contract_summary.text()
        )
        self.back_parallel_btn.setVisible(True)
        # Child belongs to the aggregate artifact and is not a separately
        # publishable GUI run through the current Artifact API.
        self.save_btn.setEnabled(False)

    def _on_back_parallel_result(self):
        aggregate = getattr(self, '_parallel_parent_result', None)
        if not isinstance(aggregate, ParallelResult):
            return
        save_enabled = bool(getattr(
            self, '_parallel_parent_save_enabled', False
        ))
        self._display_parallel_result(aggregate)
        self.back_parallel_btn.setVisible(False)
        self.save_btn.setEnabled(save_enabled)
        self._parallel_parent_result = None

    def _fill_trades_table(self, trades: list, limit: int = 2000):
        """
        填充交易记录表

        Args:
            trades: 交易列表
            limit: 最大显示行数，超过显示加载按钮
        """
        # 检查是否需要自动限制
        total_count = len(trades)
        display_trades = trades
        is_truncated = False

        if limit > 0 and total_count > limit:
            display_trades = trades[-limit:]  # 显示最后 limit 条
            is_truncated = True

        # 禁用更新，批量插入后再启用（避免大量数据卡死 UI）
        self.trades_table.setUpdatesEnabled(False)
        self.trades_table.setRowCount(len(display_trades))

        # 反转显示 (最新的在上面)
        for i, trade in enumerate(reversed(display_trades)):
            # 日期 (YYYYMMDD -> YYYY-MM-DD)
            time_str = trade.get('time', '')
            if len(time_str) == 8:
                time_str = f"{time_str[:4]}-{time_str[4:6]}-{time_str[6:]}"
            date_item = QTableWidgetItem(time_str)
            date_item.setData(Qt.UserRole, {
                'symbol': str(trade.get('symbol', '')),
                'date': str(trade.get('time', trade.get('date', ''))),
            })
            self.trades_table.setItem(i, 0, date_item)
            self.trades_table.setItem(i, 1, QTableWidgetItem(trade.get('symbol', '')))

            direction = trade.get('direction', '')
            direction_item = QTableWidgetItem('买入' if direction == 'BUY' else '卖出')
            if direction == 'BUY':
                direction_item.setForeground(QColor(Colors.ACCENT_RED))
            else:
                direction_item.setForeground(QColor(Colors.ACCENT_GREEN))
            self.trades_table.setItem(i, 2, direction_item)

            self.trades_table.setItem(i, 3, QTableWidgetItem(str(trade.get('shares', 0))))
            self.trades_table.setItem(i, 4, QTableWidgetItem(f"{trade.get('price', 0):.2f}"))
            self.trades_table.setItem(i, 5, QTableWidgetItem(f"{trade.get('amount', 0):,.0f}"))

            # 盈亏 (仅卖出显示)
            profit = trade.get('profit', 0)
            profit_pct = trade.get('profit_pct', 0)
            if direction == 'SELL':
                profit_item = QTableWidgetItem(f"{profit:,.0f}")
                pct_item = QTableWidgetItem(f"{profit_pct * 100:.2f}%")
                if profit >= 0:
                    profit_item.setForeground(QColor(Colors.ACCENT_GREEN))
                    pct_item.setForeground(QColor(Colors.ACCENT_GREEN))
                else:
                    profit_item.setForeground(QColor(Colors.ACCENT_RED))
                    pct_item.setForeground(QColor(Colors.ACCENT_RED))
            else:
                profit_item = QTableWidgetItem('-')
                pct_item = QTableWidgetItem('-')

            self.trades_table.setItem(i, 6, profit_item)
            self.trades_table.setItem(i, 7, pct_item)
            self.trades_table.setItem(i, 8, QTableWidgetItem(trade.get('note', '')))

        # 重新启用更新
        self.trades_table.setUpdatesEnabled(True)

        # 控制“加载全部”按钮
        if is_truncated:
            self.btn_load_all_trades.setText(f"显示全部交易记录 (共 {total_count} 条，当前显示最近 {limit} 条)")
            self.btn_load_all_trades.setVisible(True)
        else:
            self.btn_load_all_trades.setVisible(False)

    def _on_load_all_trades(self):
        """加载全部交易记录"""
        if hasattr(self, '_current_result') and self._current_result:
            # 传入 limit=0 表示不限制
            self._fill_trades_table(self._current_result.trades, limit=0)

    def _on_trade_double_clicked(self, index):
        """Open the exact traded symbol and focus its execution date."""

        item = self.trades_table.item(index.row(), 0)
        details = item.data(Qt.UserRole) if item is not None else None
        if not isinstance(details, dict):
            return
        symbol = str(details.get('symbol', '')).strip()
        trade_date = str(details.get('date', '')).replace('-', '')[:8]
        if not symbol:
            return
        self.tabs.setCurrentIndex(self._kline_tab_index)
        self._populate_stock_trade_details(symbol)
        self._open_kline_dialog(symbol, focus_date=trade_date or None)

    def _populate_stock_trade_details(self, symbol: str) -> None:
        """Show verbatim trade records for one selected symbol."""

        result = getattr(self, '_current_result', None)
        trades = [] if result is None else [
            trade for trade in getattr(result, 'trades', ())
            if trade.get('symbol') == symbol
        ]
        self.stock_trade_label.setText(
            f"个股成交明细: {symbol} · {len(trades)} 笔"
        )
        self.stock_trade_table.setRowCount(len(trades))
        for row, trade in enumerate(reversed(trades)):
            raw_date = str(trade.get('time', trade.get('date', '')))
            date_text = raw_date
            if len(raw_date) == 8:
                date_text = (
                    f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
                )
            direction = trade.get('direction', '')
            values = (
                date_text,
                '买入' if direction == 'BUY' else '卖出',
                str(trade.get('shares', 0)),
                f"{trade.get('price', 0):.4f}",
                f"{trade.get('amount', 0):,.0f}",
                (
                    '-'
                    if direction != 'SELL'
                    else f"{trade.get('profit', 0):,.0f}"
                ),
                str(trade.get('note', '')),
            )
            for column, value in enumerate(values):
                self.stock_trade_table.setItem(
                    row, column, QTableWidgetItem(value)
                )

    def _fill_execution_events(self, result, limit: int = 5000) -> None:
        """Display only recorded journal facts; never infer an order state."""

        journal = getattr(result, 'event_journal', None)
        if journal is None:
            self.events_table.setRowCount(0)
            self.events_status_label.setText(
                "此结果没有 execution event journal；不推测订单状态或拒绝原因。"
            )
            return
        events = list(journal)
        shown = events[-limit:] if limit > 0 else events
        self.events_table.setRowCount(len(shown))
        for row, event in enumerate(shown):
            wire = event.to_dict()
            payload = wire['payload']
            values = (
                wire['simulated_time'],
                wire['event_type'],
                str(payload.get('symbol', '')),
                str(payload.get('order_id', '')),
                str(payload.get('reason') or ''),
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True,
                    separators=(',', ':'),
                ),
            )
            for column, value in enumerate(values):
                self.events_table.setItem(
                    row, column, QTableWidgetItem(value)
                )
        suffix = (
            f"；仅显示最近 {limit} 条" if len(events) > len(shown) else ""
        )
        self.events_status_label.setText(
            f"执行事件: {len(events)} 条；内容直接来自已保存 journal{suffix}。"
        )

    def _fill_position_table(self, positions: list, daily_values: pd.DataFrame = None, day_limit: int = 20):
        """
        填充持仓表

        Args:
            positions: 持仓历史列表
            daily_values: 每日净值DataFrame (包含 cash, market_value, total_value)
            day_limit: 显示最近多少天的数据，0表示全部
        """
        # 按日期分组
        from collections import defaultdict
        date_positions = defaultdict(list)
        for pos in positions:
            date_positions[pos.get('date', '')].append(pos)

        # 获取所有日期并排序
        sorted_dates = sorted(date_positions.keys())
        total_dates_count = len(sorted_dates)
        is_truncated = False

        # 应用日期限制
        display_dates = sorted_dates
        if day_limit > 0 and total_dates_count > day_limit:
            display_dates = sorted_dates[-day_limit:]
            is_truncated = True

        # 计算总行数 (持仓记录数 + 每天一个汇总行)
        total_rows = 0
        for date in display_dates:
            total_rows += len(date_positions[date]) + 1  # 股票行 + 1个汇总行

        # 禁用更新，批量插入后再启用（避免大量数据卡死 UI）
        self.position_table.setUpdatesEnabled(False)
        self.position_table.setRowCount(total_rows)

        row_idx = 0
        # 反转显示 (最新的日期在上面)
        for date in reversed(display_dates):
            pos_list = date_positions[date]

            # 格式化日期
            date_str = date
            if len(date) == 8:
                date_str = f"{date[:4]}-{date[4:6]}-{date[6:]}"

            # 1. 先添加总资产汇总行 (放在该日最上方)
            daily_market_value = sum(p.get('market_value', 0) for p in pos_list)
            daily_profit = sum(p.get('profit', 0) for p in pos_list)

            # 从 daily_values 获取现金和总资产
            cash = 0
            total_asset = daily_market_value
            if daily_values is not None and not daily_values.empty:
                try:
                    date_dt = pd.to_datetime(date)
                    if date_dt in daily_values.index:
                        row = daily_values.loc[date_dt]
                        cash = row.get('cash', 0)
                        total_asset = row.get('total_value', daily_market_value + cash)
                except:
                    pass

            summary_item = QTableWidgetItem(date_str)
            summary_item.setFont(QFont('', -1, QFont.Bold))
            summary_item.setBackground(QColor(Colors.BG_TERTIARY))
            self.position_table.setItem(row_idx, 0, summary_item)

            total_label = QTableWidgetItem('【总资产】')
            total_label.setFont(QFont('', -1, QFont.Bold))
            total_label.setForeground(QColor(Colors.ACCENT_BLUE))
            total_label.setBackground(QColor(Colors.BG_TERTIARY))
            self.position_table.setItem(row_idx, 1, total_label)

            cash_item = QTableWidgetItem(f"现金: {cash:,.0f}")
            cash_item.setFont(QFont('', -1, QFont.Bold))
            cash_item.setBackground(QColor(Colors.BG_TERTIARY))
            self.position_table.setItem(row_idx, 2, cash_item)

            mv_label = QTableWidgetItem(f"股票: {daily_market_value:,.0f}")
            mv_label.setFont(QFont('', -1, QFont.Bold))
            mv_label.setBackground(QColor(Colors.BG_TERTIARY))
            self.position_table.setItem(row_idx, 3, mv_label)

            # 空列
            empty_item = QTableWidgetItem('')
            empty_item.setBackground(QColor(Colors.BG_TERTIARY))
            self.position_table.setItem(row_idx, 4, empty_item)

            total_item = QTableWidgetItem(f"{total_asset:,.0f}")
            total_item.setFont(QFont('', -1, QFont.Bold))
            total_item.setForeground(QColor(Colors.ACCENT_BLUE))
            total_item.setBackground(QColor(Colors.BG_TERTIARY))
            self.position_table.setItem(row_idx, 5, total_item)

            total_profit_item = QTableWidgetItem(f"{daily_profit:,.0f}")
            total_profit_item.setFont(QFont('', -1, QFont.Bold))
            if daily_profit >= 0:
                total_profit_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                total_profit_item.setForeground(QColor(Colors.ACCENT_RED))
            total_profit_item.setBackground(QColor(Colors.BG_TERTIARY))
            self.position_table.setItem(row_idx, 6, total_profit_item)

            row_idx += 1

            # 2. 填充该日期的持仓详情
            for pos in pos_list:
                # 只有第一行显示日期，或者是每天的第一行 (这里因为有汇总行，所以持仓行不显示日期，保持整洁)
                self.position_table.setItem(row_idx, 0, QTableWidgetItem("")) 
                
                self.position_table.setItem(row_idx, 1, QTableWidgetItem(pos.get('symbol', '')))
                self.position_table.setItem(row_idx, 2, QTableWidgetItem(str(pos.get('shares', 0))))
                self.position_table.setItem(row_idx, 3, QTableWidgetItem(f"{pos.get('cost', 0):.2f}"))
                self.position_table.setItem(row_idx, 4, QTableWidgetItem(f"{pos.get('price', 0):.2f}"))
                self.position_table.setItem(row_idx, 5, QTableWidgetItem(f"{pos.get('market_value', 0):,.0f}"))

                profit = pos.get('profit', 0)
                profit_item = QTableWidgetItem(f"{profit:,.0f}")
                if profit >= 0:
                    profit_item.setForeground(QColor(Colors.ACCENT_GREEN))
                else:
                    profit_item.setForeground(QColor(Colors.ACCENT_RED))
                self.position_table.setItem(row_idx, 6, profit_item)

                row_idx += 1

        # 重新启用更新
        self.position_table.setUpdatesEnabled(True)

        # 控制“加载全部”按钮
        if is_truncated:
            self.btn_load_all_positions.setText(f"显示全部持仓历史 (共 {total_dates_count} 天，当前显示最近 {day_limit} 天)")
            self.btn_load_all_positions.setVisible(True)
        else:
            self.btn_load_all_positions.setVisible(False)

    def _on_load_all_positions(self):
        """加载全部持仓"""
        if hasattr(self, '_current_result') and self._current_result:
            # 弹出确认框 (如果天数特别多)
            dates_count = len(set(p['date'] for p in self._current_result.position_history))
            if dates_count > 100:
                reply = QMessageBox.question(self, "确认加载", 
                                        f"持仓历史包含 {dates_count} 天的数据，全部加载可能会卡顿几秒钟。\n\n是否继续？",
                                        QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
                if reply == QMessageBox.No:
                    return

            # 显示 loading 状态
            self.btn_load_all_positions.setText("正在加载中...")
            self.btn_load_all_positions.setEnabled(False)
            QApplication.processEvents()

            try:
                daily_values = self._current_result.daily_values if hasattr(self._current_result, 'daily_values') else None
                # day_limit=0 表示全部
                self._fill_position_table(self._current_result.position_history, daily_values, day_limit=0)
            finally:
                self.btn_load_all_positions.setEnabled(True)

    def _setup_chart_with_crosshair(self, df: pd.DataFrame):
        """
        设置带十字光标的资产曲线图

        Args:
            df: 每日净值 DataFrame (index=date, columns=[cash, market_value, total_value])
        """
        if not HAS_PYQTGRAPH:
            return

        # 比较曲线只能来自已验证 ComparisonBundle，绝不在 UI 补值。
        comparison_view = getattr(self, '_comparison_view', None)
        benchmark_values = (
            None if comparison_view is None
            else list(comparison_view['values'])
        )
        if benchmark_values is not None and len(benchmark_values) != len(df):
            benchmark_values = None
            self.comparison_status_label.setText(
                "不可用：比较曲线与策略曲线长度不一致"
            )
            self.stat_labels['benchmark_return'].setText("不可用")
            self.stat_labels['excess_return'].setText("不可用")

        # 获取日期列表
        dates = df.index.tolist()
        x = list(range(len(df)))
        y = df['total_value'].tolist()

        # 计算归一化值用于对比显示
        initial_value = _result_initial_cash(self._current_result)
        y_normalized = _normalize_nav(y, initial_value)

        # 保存数据供十字光标使用
        self._chart_data = {
            'dates': dates,
            'x': x,
            'y': y,
            'y_normalized': y_normalized,
            'cash': df['cash'].tolist(),
            'market_value': df['market_value'].tolist(),
            'total_value': df['total_value'].tolist(),
            'benchmark_values': benchmark_values,
        }

        # 创建新的 PlotWidget 替换原有的
        # 注意：需要重新创建 PlotWidget 以使用自定义坐标轴
        chart_group = self.chart_widget.parent()
        if chart_group is None:
            return

        # 获取布局
        layout = chart_group.layout()
        if layout is None:
            return

        # 移除旧的图表
        layout.removeWidget(self.chart_widget)
        self.chart_widget.deleteLater()

        # 创建带自定义日期轴的新图表
        date_axis = DateAxisItem(dates, orientation='bottom')
        self.chart_widget = pg.PlotWidget(axisItems={'bottom': date_axis})
        self.chart_widget.setBackground(Colors.BG_DARK)
        self.chart_widget.showGrid(x=True, y=True, alpha=0.15)
        self.chart_widget.getAxis('bottom').setTextPen(Colors.TEXT_SECONDARY)

        # 根据是否有基准决定显示模式
        if benchmark_values:
            # 有基准时使用归一化比较模式
            self.chart_widget.setLabel('left', '收益率', units='', color=Colors.TEXT_SECONDARY)
            self.chart_widget.getAxis('left').setTextPen(Colors.TEXT_SECONDARY)

            # 绘制策略曲线 (蓝色实线，加粗)
            self.chart_widget.plot(x, y_normalized, pen=pg.mkPen(color=Colors.ACCENT_BLUE, width=3), name='策略')

            # 绘制基准曲线 (橙色虚线)
            self.chart_widget.plot(x, benchmark_values, pen=pg.mkPen(color='#FF9800', width=1.5, style=Qt.DashLine), name='基准')

            # 添加图例 (左上角)
            legend = self.chart_widget.addLegend(offset=(10, 10))
            legend.setParentItem(self.chart_widget.graphicsItem())
        else:
            # 无基准时显示绝对值
            self.chart_widget.setLabel('left', '资产', units='元', color=Colors.TEXT_SECONDARY)
            self.chart_widget.getAxis('left').setTextPen(Colors.TEXT_SECONDARY)

            # 绘制曲线 (带name以便在有基准时能正确显示图例)
            self.chart_widget.plot(x, y, pen=pg.mkPen(color=Colors.ACCENT_BLUE, width=3), name='策略')

        self.chart_widget.setLabel('bottom', '日期', color=Colors.TEXT_SECONDARY)

        # 添加十字光标
        self._vLine = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(Colors.ACCENT_GREEN, width=1))
        self._hLine = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(Colors.ACCENT_GREEN, width=1))
        self.chart_widget.addItem(self._vLine, ignoreBounds=True)
        self.chart_widget.addItem(self._hLine, ignoreBounds=True)

        # 添加提示文本
        self._tooltip = pg.TextItem(anchor=(0, 1), color=Colors.TEXT_PRIMARY)
        self._tooltip.setFont(QFont('Microsoft YaHei', 10))
        self.chart_widget.addItem(self._tooltip)

        # 连接鼠标移动信号
        self.chart_widget.scene().sigMouseMoved.connect(self._on_mouse_moved)

        # 保持资产曲线位于回撤说明/曲线之前。
        layout.insertWidget(1, self.chart_widget)

    def _setup_drawdown_chart(self, df: pd.DataFrame) -> None:
        """Plot the engine-recorded close-NAV drawdown vector only."""

        if not HAS_PYQTGRAPH:
            return
        self.drawdown_widget.clear()
        if 'drawdown_close_nav' not in df.columns:
            self.drawdown_status_label.setText(
                "回撤曲线: 此结果未记录 drawdown_close_nav，不在 GUI 重算"
            )
            return
        try:
            values = [float(value) for value in df['drawdown_close_nav']]
        except (TypeError, ValueError):
            self.drawdown_status_label.setText(
                "回撤曲线: drawdown_close_nav 数据无效"
            )
            return
        if any(not math.isfinite(value) or value < 0 for value in values):
            self.drawdown_status_label.setText(
                "回撤曲线: drawdown_close_nav 含非法值"
            )
            return
        x = list(range(len(values)))
        underwater = [-value * 100.0 for value in values]
        self.drawdown_widget.plot(
            x,
            underwater,
            pen=pg.mkPen(color=Colors.ACCENT_RED, width=2),
            fillLevel=0,
            brush=pg.mkBrush(244, 67, 54, 60),
        )
        self.drawdown_status_label.setText(
            "回撤曲线: 引擎记录的收盘净值回撤（0 以下为回撤幅度）"
        )

    def _on_mouse_moved(self, pos):
        """处理鼠标移动事件"""
        if not hasattr(self, '_chart_data') or not HAS_PYQTGRAPH:
            return

        # 获取鼠标在图表中的位置
        if self.chart_widget.sceneBoundingRect().contains(pos):
            mouse_point = self.chart_widget.plotItem.vb.mapSceneToView(pos)
            x_idx = int(round(mouse_point.x()))

            data = self._chart_data
            if 0 <= x_idx < len(data['dates']):
                # 更新提示信息
                dt = data['dates'][x_idx]
                date_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)
                total = data['total_value'][x_idx]
                cash = data['cash'][x_idx]
                market_value = data['market_value'][x_idx]

                # 检查是否有基准数据
                benchmark_values = data.get('benchmark_values')
                if benchmark_values:
                    # 有基准时显示归一化收益率
                    strategy_return = (data['y_normalized'][x_idx] - 1) * 100
                    benchmark_return = (benchmark_values[x_idx] - 1) * 100
                    tooltip_text = (
                        f"日期: {date_str}\n"
                        f"策略: {strategy_return:+.2f}%\n"
                        f"基准: {benchmark_return:+.2f}%\n"
                        f"─────────\n"
                        f"总资产: {total:,.0f}"
                    )

                    # 更新十字光标位置 (使用归一化值)
                    self._vLine.setPos(x_idx)
                    self._hLine.setPos(data['y_normalized'][x_idx])

                    # 提示框位置
                    view_range = self.chart_widget.viewRange()
                    y_range = view_range[1][1] - view_range[1][0]
                    self._tooltip.setPos(x_idx + 1, data['y_normalized'][x_idx] + y_range * 0.05)
                else:
                    # 无基准时显示绝对值
                    tooltip_text = (
                        f"日期: {date_str}\n"
                        f"总资产: {total:,.0f}\n"
                        f"股票: {market_value:,.0f}\n"
                        f"现金: {cash:,.0f}"
                    )

                    # 更新十字光标位置
                    self._vLine.setPos(x_idx)
                    self._hLine.setPos(total)

                    # 提示框位置
                    view_range = self.chart_widget.viewRange()
                    y_range = view_range[1][1] - view_range[1][0]
                    self._tooltip.setPos(x_idx + 1, total + y_range * 0.05)

                self._tooltip.setText(tooltip_text)

    def update_chart_realtime(self, daily_data):
        """
        实时更新资产曲线 (回测过程中)

        Args:
            daily_data: dict with 'values' (最后100条数据) and 'total_days' (总天数)
                       或 list (向后兼容)
        """
        if not HAS_PYQTGRAPH or not daily_data:
            return

        try:
            # 支持新格式 {values, total_days} 和旧格式 list
            if isinstance(daily_data, dict):
                daily_values = daily_data.get('values', [])
                total_days = daily_data.get('total_days', len(daily_values))
            else:
                daily_values = daily_data
                total_days = len(daily_values)

            if not daily_values:
                return

            # 计算 x 轴偏移：total_days - 数据条数
            x_offset = total_days - len(daily_values)
            x = list(range(x_offset, x_offset + len(daily_values)))
            y = [d['total_value'] for d in daily_values]

            # 首次调用：创建曲线对象
            if not hasattr(self, '_realtime_curve') or self._realtime_curve is None:
                self.chart_widget.clear()
                self._realtime_curve = self.chart_widget.plot(
                    x, y, pen=pg.mkPen(color=Colors.ACCENT_BLUE, width=2)
                )
            else:
                # 增量更新：使用 setData 而非 clear+plot
                self._realtime_curve.setData(x, y)

            # 自动缩放
            self.chart_widget.enableAutoRange()

        except Exception as e:
            pass  # 忽略更新错误

    def update_trades_realtime(self, trades: list):
        """实时更新交易记录"""
        try:
            self.trades_table.setUpdatesEnabled(False)
            self.trades_table.setRowCount(len(trades))
            for i, trade in enumerate(trades):
                time_str = trade.get('time', '')
                if len(time_str) == 8:
                    time_str = f"{time_str[:4]}-{time_str[4:6]}-{time_str[6:]}"
                date_item = QTableWidgetItem(time_str)
                date_item.setData(Qt.UserRole, {
                    'symbol': str(trade.get('symbol', '')),
                    'date': str(
                        trade.get('time', trade.get('date', ''))
                    ),
                })
                self.trades_table.setItem(i, 0, date_item)
                self.trades_table.setItem(i, 1, QTableWidgetItem(trade.get('symbol', '')))

                direction = trade.get('direction', '')
                direction_item = QTableWidgetItem('买入' if direction == 'BUY' else '卖出')
                if direction == 'BUY':
                    direction_item.setForeground(QColor(Colors.ACCENT_RED))
                else:
                    direction_item.setForeground(QColor(Colors.ACCENT_GREEN))
                self.trades_table.setItem(i, 2, direction_item)

                self.trades_table.setItem(i, 3, QTableWidgetItem(str(trade.get('shares', 0))))
                self.trades_table.setItem(i, 4, QTableWidgetItem(f"{trade.get('price', 0):.2f}"))
                self.trades_table.setItem(i, 5, QTableWidgetItem(f"{trade.get('amount', 0):,.0f}"))
                self.trades_table.setItem(i, 6, QTableWidgetItem('-'))
                self.trades_table.setItem(i, 7, QTableWidgetItem('-'))
                self.trades_table.setItem(i, 8, QTableWidgetItem(trade.get('note', '')))
            self.trades_table.setUpdatesEnabled(True)
        except Exception:
            self.trades_table.setUpdatesEnabled(True)
            pass

    def update_positions_realtime(self, positions: list):
        """实时更新持仓"""
        try:
            self.position_table.setUpdatesEnabled(False)
            self.position_table.setRowCount(len(positions))
            for i, pos in enumerate(positions):
                date_str = pos.get('date', '')
                if len(date_str) == 8:
                    date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
                self.position_table.setItem(i, 0, QTableWidgetItem(date_str))
                self.position_table.setItem(i, 1, QTableWidgetItem(pos.get('symbol', '')))
                self.position_table.setItem(i, 2, QTableWidgetItem(str(pos.get('shares', 0))))
                self.position_table.setItem(i, 3, QTableWidgetItem(f"{pos.get('cost', 0):.2f}"))
                self.position_table.setItem(i, 4, QTableWidgetItem(f"{pos.get('price', 0):.2f}"))
                self.position_table.setItem(i, 5, QTableWidgetItem(f"{pos.get('market_value', 0):,.0f}"))

                profit = pos.get('profit', 0)
                profit_item = QTableWidgetItem(f"{profit:,.0f}")
                if profit >= 0:
                    profit_item.setForeground(QColor(Colors.ACCENT_GREEN))
                else:
                    profit_item.setForeground(QColor(Colors.ACCENT_RED))
                self.position_table.setItem(i, 6, profit_item)
            self.position_table.setUpdatesEnabled(True)
        except Exception:
            self.position_table.setUpdatesEnabled(True)
            pass

    def add_log(self, message: str):
        """添加日志"""
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{timestamp}] {message}")

    def clear(self):
        """清空结果"""
        for label in self.stat_labels.values():
            label.setText("--")
            label.setStyleSheet(f"font-weight: 600; font-size: 24px; color: {Colors.TEXT_PRIMARY}; background: transparent; border: none;")

        self.contract_summary.setText(
            "状态: 尚无结果\n可排名: 否\n实际区间/覆盖率: 不可用"
        )
        self.comparison_status_label.setText("不可用：尚无结果")

        if HAS_PYQTGRAPH:
            self.chart_widget.clear()
            self.drawdown_widget.clear()
        self.drawdown_status_label.setText("回撤曲线: 尚无结果")

        self.trades_table.setRowCount(0)
        self.position_table.setRowCount(0)
        self.parallel_table.setRowCount(0)  # 清空股票排行表格
        self.stock_table.setRowCount(0)
        self.stock_trade_table.setRowCount(0)
        self.stock_trade_label.setText("个股成交明细: 尚未选择标的")
        self.events_table.setRowCount(0)
        self.events_status_label.setText("尚无执行事件证据")
        self.kline_evidence_label.setText(
            "K线证据: 尚无结果。历史 Artifact 必须与当前本地行情指纹一致。"
        )
        self.log_text.clear()
        self.progress_bar.setValue(0)
        self.progress_label.setText("")

        # 清除保存的结果
        self._current_result = None
        self._is_parallel_result = False
        self._comparison_view = None
        self._parallel_parent_result = None
        self.back_parallel_btn.setVisible(False)
        self._market_config = {}
        self._market_provenance = None
        self._market_data_root = None
        self._historical_artifact_view = False
        self._market_fingerprint_required = False

        # 重置实时曲线对象
        self._realtime_curve = None

        # 禁用保存按钮
        self.save_btn.setEnabled(False)

    def _populate_stock_table(self):
        """
        填充股票表格

        按盈利排序
        """
        if not hasattr(self, '_current_result') or self._current_result is None:
            return

        trades = self._current_result.trades
        if not trades:
            self.stock_table.setRowCount(0)
            return

        # 禁用更新，批量插入后再启用
        self.stock_table.setUpdatesEnabled(False)

        # 名称查询是可选增强；数据提供者失败不能遮蔽结果终态。
        provider = getattr(self, '_data_provider', None)

        # 统计每只股票的盈亏和交易次数
        stock_stats = {}  # {symbol: {'profit': 0, 'count': 0, 'name': ''}}

        for trade in trades:
            symbol = trade.get('symbol', '')
            if not symbol:
                continue

            if symbol not in stock_stats:
                # 获取股票名称（使用共享的 provider）
                name = ''
                if provider is not None:
                    try:
                        info = provider.get_stock_info(symbol)
                        name = info.get('name', '') if info else ''
                    except Exception:
                        pass
                stock_stats[symbol] = {
                    'profit': 0,
                    'count': 0,
                    'name': name
                }

            stock_stats[symbol]['count'] += 1

            # 只统计卖出的盈亏
            if trade.get('direction') == 'SELL':
                stock_stats[symbol]['profit'] += trade.get('profit', 0)

        # 按盈利排序 (盈利多的在前)
        sorted_stocks = sorted(stock_stats.items(), key=lambda x: (-x[1]['profit'], x[0]))

        # 填充表格
        self.stock_table.setRowCount(len(sorted_stocks))
        self._stock_list = []  # 保存股票列表供双击使用

        for i, (symbol, stats) in enumerate(sorted_stocks):
            self._stock_list.append(symbol)

            self.stock_table.setItem(i, 0, QTableWidgetItem(symbol))
            self.stock_table.setItem(i, 1, QTableWidgetItem(stats['name']))

            profit = stats['profit']
            profit_item = QTableWidgetItem(f"{profit:+,.0f}" if profit != 0 else "0")
            if profit > 0:
                profit_item.setForeground(QColor(Colors.ACCENT_GREEN))
            elif profit < 0:
                profit_item.setForeground(QColor(Colors.ACCENT_RED))
            self.stock_table.setItem(i, 2, profit_item)

            self.stock_table.setItem(i, 3, QTableWidgetItem(str(stats['count'])))

        # 重新启用更新
        self.stock_table.setUpdatesEnabled(True)

    def _get_stock_name(self, symbol: str) -> str:
        """获取股票名称"""
        try:
            # 优先使用已保存的 provider
            if hasattr(self, '_data_provider') and self._data_provider:
                provider = self._data_provider
            else:
                from ...data import DataProvider
                provider = DataProvider()
                
            info = provider.get_stock_info(symbol)
            if hasattr(info, 'name'):
                return info['name']
            elif 'name' in info:
                return info['name']
        except:
            pass
        return ''

    def _on_stock_double_clicked(self, index):
        """双击股票打开K线弹窗"""
        row = index.row()
        if not hasattr(self, '_stock_list') or row >= len(self._stock_list):
            return

        symbol = self._stock_list[row]
        self._populate_stock_trade_details(symbol)
        self._open_kline_dialog(symbol)

    def _open_kline_dialog(self, symbol: str, *, focus_date: str = None):
        """
        打开K线弹窗

        Args:
            symbol: 股票代码
        """
        if not hasattr(self, '_current_result') or self._current_result is None:
            return False

        price_mode = self._kline_price_mode(symbol)
        if price_mode is None:
            return False

        try:
            from ...data import DataProvider
            provider = None
            if not self._market_fingerprint_required:
                provider = DataProvider(
                    data_root=self._market_data_root,
                    price_mode=price_mode,
                    execution_price_mode=price_mode,
                )

            # 获取回测日期范围
            result = self._current_result
            df = result.daily_values

            dates = df.index.tolist()
            if not dates:
                return False

            start_date = dates[0].strftime('%Y%m%d') if hasattr(dates[0], 'strftime') else str(dates[0])[:8].replace('-', '')
            end_date = dates[-1].strftime('%Y%m%d') if hasattr(dates[-1], 'strftime') else str(dates[-1])[:8].replace('-', '')

            # 获取K线数据 - 多取30天历史数据用于计算均线
            # 先尝试获取更早的数据
            from datetime import datetime, timedelta
            try:
                start_dt = datetime.strptime(start_date, '%Y%m%d')
                extended_start = (start_dt - timedelta(days=60)).strftime('%Y%m%d')  # 多取60天
            except:
                extended_start = start_date

            if self._market_fingerprint_required:
                kline_df, verification = load_verified_display_daily_source(
                    self._market_provenance,
                    data_root=self._market_data_root,
                    symbol=symbol,
                    price_mode=artifact_price_mode(self._market_config),
                    scope_symbols=artifact_symbols(self._market_config),
                    start=extended_start,
                    end=end_date,
                )
                self.kline_evidence_label.setText(
                    "K线证据: 已通过。"
                    f"{verification.message}；分钟下钻未纳入指纹，已禁用。"
                )
            else:
                kline_df = provider.get_daily(
                    symbol,
                    start=extended_start,
                    end=end_date,
                    price_mode=price_mode,
                )

            if kline_df.empty:
                self.add_log(f"未找到 {symbol} 的K线数据")
                return False

            # 重命名列以适配 KLineChart
            kline_df = kline_df.reset_index()
            if 'trade_date' not in kline_df.columns and 'index' in kline_df.columns:
                kline_df = kline_df.rename(columns={'index': 'trade_date'})
            if 'vol' not in kline_df.columns:
                # raw-minimal 的 vol 是可选字段；以 0 明示“未提供成交量”。
                kline_df['vol'] = 0.0

            # 获取该股票的交易记录
            stock_trades = [t for t in result.trades if t.get('symbol') == symbol]

            # 获取股票名称
            name = self._get_stock_name(symbol)

            # 打开弹窗
            self.kline_dialog.kline_chart.set_data_provider(
                provider,
                price_mode=price_mode if provider is not None else None,
            )
            self.kline_dialog.show_stock(
                symbol,
                name,
                kline_df,
                stock_trades,
                focus_date=focus_date,
            )
            return True

        except Exception as e:
            import traceback
            traceback.print_exc()
            if self._market_fingerprint_required:
                self.kline_evidence_label.setText(
                    "K线证据: 未通过；结果仍可查看，K线与成交叠加已禁用。"
                    f"严格读取失败: {e}"
                )
            self.add_log(f"加载K线数据失败: {e}")
            return False

    def _fill_attribution_tables(self, trades: List, initial_capital: float):
        """
        填充收益归因表格

        Args:
            trades: 交易记录列表
            initial_capital: 初始资金
        """
        from ...engine.attribution import calculate_attribution

        # 获取股票名称
        stock_names = {}
        for trade in trades:
            symbol = trade.get('symbol', '')
            if symbol and symbol not in stock_names:
                stock_names[symbol] = self._get_stock_name(symbol)

        # 计算归因
        attribution = calculate_attribution(trades, initial_capital, stock_names)

        # 填充年份表
        year_df = attribution['by_year']
        self.year_table.setRowCount(len(year_df))
        for i, (_, row) in enumerate(year_df.iterrows()):
            self.year_table.setItem(i, 0, QTableWidgetItem(str(row['year'])))

            profit_item = QTableWidgetItem(f"{row['profit']:,.0f}")
            if row['profit'] >= 0:
                profit_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                profit_item.setForeground(QColor(Colors.ACCENT_RED))
            self.year_table.setItem(i, 1, profit_item)

            pct_item = QTableWidgetItem(f"{row['profit_pct'] * 100:.2f}%")
            if row['profit_pct'] >= 0:
                pct_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                pct_item.setForeground(QColor(Colors.ACCENT_RED))
            self.year_table.setItem(i, 2, pct_item)

            self.year_table.setItem(i, 3, QTableWidgetItem(str(row['trade_count'])))

        # 填充月份表
        month_df = attribution['by_month']
        self.month_table.setRowCount(len(month_df))
        for i, (_, row) in enumerate(month_df.iterrows()):
            self.month_table.setItem(i, 0, QTableWidgetItem(str(row['month'])))

            profit_item = QTableWidgetItem(f"{row['profit']:,.0f}")
            if row['profit'] >= 0:
                profit_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                profit_item.setForeground(QColor(Colors.ACCENT_RED))
            self.month_table.setItem(i, 1, profit_item)

            pct_item = QTableWidgetItem(f"{row['profit_pct'] * 100:.2f}%")
            if row['profit_pct'] >= 0:
                pct_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                pct_item.setForeground(QColor(Colors.ACCENT_RED))
            self.month_table.setItem(i, 2, pct_item)

            self.month_table.setItem(i, 3, QTableWidgetItem(str(row['trade_count'])))

        # 填充股票表
        stock_df = attribution['by_stock']
        self.stock_attr_table.setRowCount(len(stock_df))
        for i, (_, row) in enumerate(stock_df.iterrows()):
            self.stock_attr_table.setItem(i, 0, QTableWidgetItem(str(row['symbol'])))
            self.stock_attr_table.setItem(i, 1, QTableWidgetItem(str(row['name'])))

            profit_item = QTableWidgetItem(f"{row['profit']:,.0f}")
            if row['profit'] >= 0:
                profit_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                profit_item.setForeground(QColor(Colors.ACCENT_RED))
            self.stock_attr_table.setItem(i, 2, profit_item)

            pct_item = QTableWidgetItem(f"{row['profit_pct'] * 100:.2f}%")
            if row['profit_pct'] >= 0:
                pct_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                pct_item.setForeground(QColor(Colors.ACCENT_RED))
            self.stock_attr_table.setItem(i, 3, pct_item)

            self.stock_attr_table.setItem(i, 4, QTableWidgetItem(str(row['trade_count'])))
            win_rate = row['win_rate']
            win_rate_text = (
                "N/A" if pd.isna(win_rate) else f"{win_rate * 100:.1f}%"
            )
            self.stock_attr_table.setItem(
                i, 5, QTableWidgetItem(win_rate_text)
            )

    def get_current_result(self):
        """获取当前回测结果"""
        if hasattr(self, '_current_result'):
            return self._current_result
        return None

    def is_parallel_result(self) -> bool:
        """检查当前是否是独立测试结果"""
        return getattr(self, '_is_parallel_result', False)

    def _create_nav_bar(self, label_text: str, callback):
        """创建导航条"""
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        
        label = QLabel(label_text)
        layout.addWidget(label)
        
        date_edit = QDateEdit()
        date_edit.setCalendarPopup(True)
        date_edit.setDisplayFormat("yyyy-MM-dd")
        date_edit.setDate(datetime.now())
        layout.addWidget(date_edit)
        
        btn_jump = QPushButton("跳转")
        btn_jump.setStyleSheet(Styles.BTN_PRIMARY)
        btn_jump.setCursor(Qt.PointingHandCursor)
        btn_jump.setFixedWidth(60)
        btn_jump.clicked.connect(callback)
        layout.addWidget(btn_jump)
        
        layout.addStretch()
        
        return date_edit, container

    def _on_jump_trades(self):
        """跳转到指定日期的交易记录"""
        if not hasattr(self, '_current_result') or not self._current_result:
            return
            
        target_date = self.trades_date_edit.date().toString("yyyyMMdd")
        # 查找目标日期附近的交易
        trades = self._current_result.trades
        
        # 筛选 <= target_date 的交易
        filtered = [t for t in trades if t.get('time', '')[:8] <= target_date]
        
        if not filtered:
            QMessageBox.information(self, "提示", f"未找到 {target_date} 及之前的交易记录")
            return
            
        # 显示最后 2000 条
        self._fill_trades_table(filtered, limit=2000)
        
    def _on_jump_positions(self):
        """跳转到指定日期的持仓"""
        if not hasattr(self, '_current_result') or not self._current_result:
            return
            
        target_date = self.pos_date_edit.date().toString("yyyyMMdd")
        
        # 验证日期是否存在于数据中
        if hasattr(self._current_result, 'position_history'):
            positions = self._current_result.position_history
            daily_values = self._current_result.daily_values if hasattr(self._current_result, 'daily_values') else None
            
            # 筛选 <= target_date 的持仓
            valid_positions = [p for p in positions if p.get('date', '') <= target_date]
            
            if not valid_positions:
                 QMessageBox.information(self, "提示", f"未找到 {target_date} 及之前的持仓记录")
                 return
                 
            # 传递 limit=20，_fill_position_table 会取 valid_positions 中最大的20个日期
            self._fill_position_table(valid_positions, daily_values, day_limit=20)
