"""
历史记录对话框

显示已保存的回测记录列表，支持查看代码、查看结果、删除
"""

import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTableWidget, QTableWidgetItem, QHeaderView, QMessageBox,
    QAbstractItemView, QPlainTextEdit, QWidget
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor

from ..styles import Colors, Styles
from ..screens.result_screen import format_result_contract
from ..worker import (
    GUI_TRACEBACK_MAX_BYTES,
    format_run_error_summary,
    load_gui_run,
    resolve_gui_results_root,
)


def _result_metric(result, portfolio_name: str, parallel_name: str, default=0):
    return getattr(
        result,
        parallel_name if hasattr(result, parallel_name) else portfolio_name,
        default,
    )


def _history_record_from_loaded(loaded) -> dict:
    """Project display metadata while preserving the loader's trust decision."""
    result = loaded.result
    contract = loaded.result_contract if loaded.artifact_verified else None
    if loaded.artifact_verified:
        status = contract.status.value
        rankable = bool(loaded.is_rankable)
        details_contract = contract
    else:
        status = 'LEGACY_UNVERIFIED'
        rankable = False
        details_contract = None

    created = loaded.created_at_utc
    save_time = created
    if created:
        try:
            save_time = datetime.fromisoformat(
                created[:-1] + '+00:00').strftime('%Y-%m-%d %H:%M:%S UTC')
        except ValueError:
            pass
    return {
        'path': str(loaded.root),
        'folder_name': loaded.root.name,
        'artifact_format': (
            'RunArtifact v1' if loaded.artifact_verified else 'legacy'
        ),
        'verified': bool(loaded.artifact_verified),
        'save_time': save_time or loaded.root.name,
        'start_date': str(getattr(result, 'start_date', '')),
        'end_date': str(getattr(result, 'end_date', '')),
        'total_return': float(_result_metric(
            result, 'total_return', 'avg_return', 0.0)),
        'annual_return': float(_result_metric(
            result, 'annual_return', 'avg_annual_return', 0.0)),
        'trade_count': int(_result_metric(
            result, 'trade_count', 'success_count', 0)),
        'result_status': status,
        'rankable': rankable,
        'contract': details_contract,
        'engine_kind': loaded.engine_kind,
        'run_error': loaded.run_error if loaded.artifact_verified else None,
        'traceback_text': (
            loaded.traceback_text if loaded.artifact_verified else ''
        ),
        'traceback_truncated': bool(
            loaded.traceback_truncated if loaded.artifact_verified else False
        ),
    }


def discover_history_records(results_root) -> list[dict]:
    """Verify v1 entries and explicitly load legacy entries as untrusted."""
    root = resolve_gui_results_root(results_root)
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError(f"结果根目录不是目录: {root}")
    records = []
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            loaded = load_gui_run(entry)
            records.append(_history_record_from_loaded(loaded))
        except Exception:
            # Invalid/tampered folders are not presented as usable history.
            continue
    records.sort(key=lambda item: item['save_time'], reverse=True)
    return records


def delete_history_record(folder_path, *, results_root) -> bool:
    """Delete one verified direct child without following links."""
    root = resolve_gui_results_root(results_root).resolve()
    candidate = Path(folder_path).absolute()
    try:
        if candidate.parent.resolve() != root:
            return False
        entry = candidate.lstat()
        if candidate.is_symlink() or not candidate.is_dir():
            return False
        # It must parse as either a valid v1 artifact or a valid legacy result.
        load_gui_run(candidate)
        verified = candidate.lstat()
        if (
            verified.st_dev != entry.st_dev
            or verified.st_ino != entry.st_ino
            or candidate.is_symlink()
        ):
            return False
        shutil.rmtree(candidate)
        return True
    except Exception:
        return False


class FailedRunDialog(QDialog):
    """Copyable, read-only diagnostics for one verified failed artifact."""

    def __init__(self, loaded, parent=None):
        if not loaded.artifact_verified or loaded.run_error is None:
            raise ValueError(
                'failed-run diagnostics require a verified RunError'
            )
        super().__init__(parent)
        self.setWindowTitle("已验证的失败运行诊断")
        self.setModal(True)
        self.resize(760, 520)

        layout = QVBoxLayout(self)
        heading = QLabel(
            "RunArtifact v1 已通过完整性验证；该 FAILED 运行不可排名。"
        )
        heading.setWordWrap(True)
        layout.addWidget(heading)

        self.error_summary = QPlainTextEdit()
        self.error_summary.setObjectName('run_error_summary')
        self.error_summary.setReadOnly(True)
        self.error_summary.setMaximumHeight(150)
        self.error_summary.setPlainText(
            "状态: FAILED\n可排名: 否\n"
            + format_run_error_summary(
                loaded.run_error, engine_kind=loaded.engine_kind
            )
        )
        layout.addWidget(self.error_summary)

        trace_note = (
            "Traceback：来自已验证的 Artifact 成员；"
            f"界面最多显示 {GUI_TRACEBACK_MAX_BYTES} UTF-8 bytes。"
        )
        if loaded.traceback_truncated:
            trace_note += " 原文超过上限，以下仅为已验证前缀。"
        elif not loaded.traceback_text:
            trace_note += " 此运行未提供 traceback 文本。"
        self.traceback_label = QLabel(trace_note)
        self.traceback_label.setWordWrap(True)
        layout.addWidget(self.traceback_label)

        self.traceback_view = QPlainTextEdit()
        self.traceback_view.setObjectName('verified_traceback_view')
        self.traceback_view.setReadOnly(True)
        self.traceback_view.setPlainText(loaded.traceback_text)
        layout.addWidget(self.traceback_view, 1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.copy_traceback_btn = QPushButton("复制 traceback")
        self.copy_traceback_btn.setEnabled(bool(loaded.traceback_text))
        self.copy_traceback_btn.clicked.connect(self._copy_traceback)
        buttons.addWidget(self.copy_traceback_btn)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

    def _copy_traceback(self):
        self.traceback_view.selectAll()
        self.traceback_view.copy()


class HistoryDialog(QDialog):
    """
    历史记录对话框

    显示所有保存的回测记录，支持:
    - 双击或点击"查看结果"进入结果页面
    - 点击"查看代码"进入编辑器页面（加载代码和配置）
    - 删除记录

    Signals:
        view_result: 查看结果 (folder_path)
        view_code: 查看代码 (folder_path)
    """

    view_result = Signal(str)
    view_code = Signal(str)

    def __init__(self, parent=None, *, results_root=None):
        super().__init__(parent)
        self.results_root = resolve_gui_results_root(results_root)
        self.setWindowTitle("历史回测记录")
        self.setMinimumSize(700, 500)
        self.setModal(True)

        self._records = []
        self._init_ui()
        self._load_records()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # 设置对话框样式
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {Colors.BG_PRIMARY};
            }}
        """)

        # ==================== 标题 ====================
        title_label = QLabel("历史回测记录")
        title_label.setStyleSheet(f"""
            font-size: 18px;
            font-weight: 600;
            color: {Colors.TEXT_PRIMARY};
        """)
        layout.addWidget(title_label)

        # ==================== 表格 ====================
        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            "保存时间", "格式/验证", "状态", "可排名", "收益率",
            "年化收益", "配置区间", "交易次数"
        ])

        # 表格样式
        self.table.setStyleSheet(Styles.TABLE + f"""
            QTableWidget {{
                background-color: {Colors.BG_SECONDARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 8px;
            }}
        """)

        # 选择整行
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)

        # 表头设置
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in range(1, 8):
            header.setSectionResizeMode(column, QHeaderView.Fixed)
        self.table.setColumnWidth(1, 120)
        self.table.setColumnWidth(2, 120)
        self.table.setColumnWidth(3, 70)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 90)
        self.table.setColumnWidth(6, 140)
        self.table.setColumnWidth(7, 80)

        # 禁止编辑
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        # 双击查看结果
        self.table.doubleClicked.connect(self._on_view_result)
        self.table.currentCellChanged.connect(self._update_contract_details)

        layout.addWidget(self.table)

        self.contract_details = QLabel(
            "请选择一条记录查看状态、原因、警告、实际区间与覆盖率"
        )
        self.contract_details.setWordWrap(True)
        self.contract_details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.contract_details.setStyleSheet(f"""
            color: {Colors.TEXT_SECONDARY};
            background-color: {Colors.BG_SECONDARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 6px;
            padding: 10px;
        """)
        layout.addWidget(self.contract_details)

        self.traceback_label = QLabel()
        self.traceback_label.setWordWrap(True)
        self.traceback_label.hide()
        layout.addWidget(self.traceback_label)

        self.traceback_view = QPlainTextEdit()
        self.traceback_view.setObjectName('history_verified_traceback_view')
        self.traceback_view.setReadOnly(True)
        self.traceback_view.setMaximumHeight(180)
        self.traceback_view.hide()
        layout.addWidget(self.traceback_view)

        # ==================== 按钮栏 ====================
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)

        # 删除按钮
        self.delete_btn = QPushButton("删除")
        self.delete_btn.setStyleSheet(Styles.BTN_DANGER)
        self.delete_btn.setCursor(Qt.PointingHandCursor)
        self.delete_btn.clicked.connect(self._on_delete)
        btn_layout.addWidget(self.delete_btn)

        btn_layout.addStretch()

        # 查看代码按钮
        self.code_btn = QPushButton("查看代码")
        self.code_btn.setStyleSheet(Styles.BTN_SECONDARY)
        self.code_btn.setCursor(Qt.PointingHandCursor)
        self.code_btn.clicked.connect(self._on_view_code)
        btn_layout.addWidget(self.code_btn)

        # 查看结果按钮
        self.result_btn = QPushButton("查看结果")
        self.result_btn.setStyleSheet(Styles.BTN_PRIMARY)
        self.result_btn.setCursor(Qt.PointingHandCursor)
        self.result_btn.clicked.connect(self._on_view_result)
        btn_layout.addWidget(self.result_btn)

        layout.addLayout(btn_layout)

    def _load_records(self):
        """加载历史记录列表"""
        self._records = discover_history_records(self.results_root)
        self.table.clearSpans()
        self.table.setRowCount(len(self._records))

        for row, record in enumerate(self._records):
            # 保存时间
            time_item = QTableWidgetItem(record.get('save_time', ''))
            time_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 0, time_item)

            verified = bool(record.get('verified', False))
            trust_item = QTableWidgetItem(
                'v1 · verified' if verified else 'legacy · 未验证')
            trust_item.setTextAlignment(Qt.AlignCenter)
            trust_item.setForeground(QColor(
                Colors.ACCENT_GREEN if verified else Colors.ACCENT_RED
            ))
            self.table.setItem(row, 1, trust_item)

            # 终态与可排名性是结果可信度的一等信息。
            status = str(record.get('result_status', 'LEGACY_UNCLASSIFIED'))
            status_item = QTableWidgetItem(status)
            status_item.setTextAlignment(Qt.AlignCenter)
            if status == 'SUCCESS':
                status_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                status_item.setForeground(QColor(Colors.ACCENT_RED))
            self.table.setItem(row, 2, status_item)

            rankable = bool(record.get('rankable', False))
            rankable_item = QTableWidgetItem('是' if rankable else '否')
            rankable_item.setTextAlignment(Qt.AlignCenter)
            rankable_item.setForeground(QColor(
                Colors.ACCENT_GREEN if rankable else Colors.ACCENT_RED
            ))
            self.table.setItem(row, 3, rankable_item)

            # 收益率 (带颜色) - total_return 存储为小数 (如 0.15 = 15%)
            total_return = record.get('total_return', 0)
            return_text = f"{total_return * 100:+.2f}%"
            return_item = QTableWidgetItem(return_text)
            return_item.setTextAlignment(Qt.AlignCenter)
            if total_return >= 0:
                return_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                return_item.setForeground(QColor(Colors.ACCENT_RED))
            self.table.setItem(row, 4, return_item)

            # 年化收益 (带颜色) - annual_return 存储为小数
            annual_return = record.get('annual_return', 0)
            annual_text = f"{annual_return * 100:+.2f}%"
            annual_item = QTableWidgetItem(annual_text)
            annual_item.setTextAlignment(Qt.AlignCenter)
            if annual_return >= 0:
                annual_item.setForeground(QColor(Colors.ACCENT_GREEN))
            else:
                annual_item.setForeground(QColor(Colors.ACCENT_RED))
            self.table.setItem(row, 5, annual_item)

            # 回测区间
            start_date = record.get('start_date', '')
            end_date = record.get('end_date', '')
            # 格式化日期显示: 20240101 -> 2024/01/01
            if len(start_date) == 8:
                start_date = f"{start_date[:4]}/{start_date[4:6]}/{start_date[6:]}"
            if len(end_date) == 8:
                end_date = f"{end_date[:4]}/{end_date[4:6]}/{end_date[6:]}"
            date_range = f"{start_date} - {end_date}"
            date_item = QTableWidgetItem(date_range)
            date_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 6, date_item)

            # 交易次数
            trade_count = record.get('trade_count', 0)
            trade_item = QTableWidgetItem(str(trade_count))
            trade_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 7, trade_item)

        # 如果没有记录，显示提示
        if not self._records:
            self._clear_traceback_details()
            self.table.setRowCount(1)
            empty_item = QTableWidgetItem("暂无保存的回测记录")
            empty_item.setTextAlignment(Qt.AlignCenter)
            empty_item.setForeground(QColor(Colors.TEXT_MUTED))
            self.table.setItem(0, 0, empty_item)
            self.table.setSpan(0, 0, 1, 8)
            self.contract_details.setText("暂无已保存的回测记录")
        else:
            self.table.selectRow(0)
            self._update_contract_details(0, 0, -1, -1)

    def _update_contract_details(
        self, current_row: int, _current_column: int,
        _previous_row: int, _previous_column: int,
    ) -> None:
        """Load the validated contract metadata for the selected artifact."""
        if current_row < 0 or current_row >= len(self._records):
            self._clear_traceback_details()
            self.contract_details.setText(
                "请选择一条记录查看状态、原因、警告、实际区间与覆盖率"
            )
            return
        record = self._records[current_row]
        if not record.get('verified', False):
            self._clear_traceback_details()
            self.contract_details.setText(
                "格式: legacy\n"
                "verified: 否\n"
                "状态: LEGACY_UNVERIFIED\n"
                "可排名: 否\n"
                "原因: 旧目录没有 RunArtifact v1 完整性清单；不会自动升级\n"
                "实际区间/覆盖率: 不作为已验证证据"
            )
            return
        details = (
            "格式: RunArtifact v1\nverified: 是\n" +
            format_result_contract(record.get('contract'))
        )
        run_error = record.get('run_error')
        if run_error is not None:
            details += (
                "\n\n结构化 RunError:\n"
                + format_run_error_summary(
                    run_error,
                    engine_kind=record.get('engine_kind', ''),
                )
            )
        self.contract_details.setText(details)
        self._show_traceback_details(record)

    def _clear_traceback_details(self) -> None:
        self.traceback_label.clear()
        self.traceback_label.hide()
        self.traceback_view.clear()
        self.traceback_view.hide()

    def _show_traceback_details(self, record: dict) -> None:
        run_error = record.get('run_error')
        if run_error is None:
            self._clear_traceback_details()
            return
        text = record.get('traceback_text', '')
        note = (
            "Traceback：来自已验证的 Artifact 成员；"
            f"界面最多显示 {GUI_TRACEBACK_MAX_BYTES} UTF-8 bytes。"
        )
        if record.get('traceback_truncated', False):
            note += " 原文超过上限，以下仅为已验证前缀。"
        elif not text:
            note += " 此运行未提供 traceback 文本。"
        self.traceback_label.setText(note)
        self.traceback_label.show()
        self.traceback_view.setPlainText(text)
        self.traceback_view.setVisible(bool(text))

    def _get_selected_record(self):
        """获取选中的记录"""
        row = self.table.currentRow()
        if row < 0 or row >= len(self._records):
            return None
        return self._records[row]

    def _on_view_result(self):
        """查看结果"""
        record = self._get_selected_record()
        if not record:
            QMessageBox.warning(self, "提示", "请选择一条记录")
            return

        self.view_result.emit(record['path'])
        self.accept()

    def _on_view_code(self):
        """查看代码"""
        record = self._get_selected_record()
        if not record:
            QMessageBox.warning(self, "提示", "请选择一条记录")
            return

        self.view_code.emit(record['path'])
        self.accept()

    def _on_delete(self):
        """删除记录"""
        record = self._get_selected_record()
        if not record:
            QMessageBox.warning(self, "提示", "请选择一条记录")
            return

        # 确认删除
        reply = QMessageBox.question(
            self, "确认删除",
            f"确定要删除这条回测记录吗？\n\n保存时间: {record.get('save_time', '')}\n收益率: {record.get('total_return', 0) * 100:.2f}%\n\n此操作不可恢复！",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )

        if reply == QMessageBox.Yes:
            success = delete_history_record(
                record['path'], results_root=self.results_root)
            if success:
                QMessageBox.information(self, "成功", "记录已删除")
                self._load_records()  # 重新加载列表
            else:
                QMessageBox.critical(self, "错误", "删除失败，请检查文件权限")

    def refresh(self):
        """刷新记录列表"""
        self._load_records()
