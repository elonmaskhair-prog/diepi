"""
K线图独立弹窗
"""

from PySide6.QtWidgets import (
    QApplication, QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont

from ..styles import Colors, Styles
from .kline_chart import KLineChart


class KLineDialog(QDialog):
    """
    K线图独立弹窗
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("K线图")

        # 获取屏幕尺寸，设置窗口为纵向满屏、横向半屏
        screen = QApplication.primaryScreen().geometry()
        width = screen.width() // 2  # 横向50%
        height = screen.height() - 80  # 纵向满屏（减去任务栏）

        self.setMinimumSize(900, 600)
        self.resize(width, height)
        self.setStyleSheet(f"background-color: {Colors.BG_PRIMARY};")
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # 标题栏
        header = QHBoxLayout()
        self.title_label = QLabel("K线图")
        self.title_label.setStyleSheet(f"""
            font-size: 16px;
            font-weight: bold;
            color: {Colors.TEXT_PRIMARY};
        """)
        header.addWidget(self.title_label)
        header.addStretch()

        close_btn = QPushButton("关闭")
        close_btn.setStyleSheet(Styles.BTN_SECONDARY)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(self.close)
        header.addWidget(close_btn)

        layout.addLayout(header)

        # K线图组件
        self.kline_chart = KLineChart()
        layout.addWidget(self.kline_chart, stretch=1)

        # 操作提示
        hint_label = QLabel("滚轮: 纵向缩放 | Shift+滚轮: 横向缩放 | 拖动: 平移")
        hint_label.setStyleSheet(f"""
            color: {Colors.TEXT_MUTED};
            font-size: 11px;
            padding: 4px 0;
        """)
        hint_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(hint_label)

    def show_stock(
        self,
        symbol: str,
        name: str,
        kline_df,
        trades: list,
        *,
        focus_date: str = None,
    ):
        """
        显示股票K线

        Args:
            symbol: 股票代码
            name: 股票名称
            kline_df: K线数据DataFrame
            trades: 交易记录列表
        """
        # 更新标题
        profit = sum(t.get('profit', 0) for t in trades if t.get('direction') == 'SELL')
        if profit >= 0:
            title = f"{symbol} {name}  盈利: +{profit:,.0f}"
            color = Colors.ACCENT_GREEN
        else:
            title = f"{symbol} {name}  亏损: {profit:,.0f}"
            color = Colors.ACCENT_RED

        self.title_label.setText(title)
        self.title_label.setStyleSheet(f"""
            font-size: 16px;
            font-weight: bold;
            color: {color};
        """)

        # 设置K线数据（symbol 用于分钟数据下钻定位标的）
        self.kline_chart.set_data(kline_df, trades, symbol=symbol)
        if focus_date:
            self.kline_chart.focus_date(focus_date)

        # 显示弹窗
        self.show()
        self.raise_()
        self.activateWindow()
