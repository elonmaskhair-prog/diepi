"""
欢迎页面

程序入口页面，提供新建回测和查看历史记录的入口
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFrame
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont

from ..styles import Colors, Fonts
from ... import __version__


class WelcomeScreen(QWidget):
    """
    欢迎页面

    提供两个主要入口:
    - 新建回测: 进入策略编辑器
    - 历史记录: 查看保存的回测结果

    Signals:
        new_backtest: 点击新建回测
        view_history: 点击历史记录
    """

    new_backtest = Signal()
    view_history = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 40, 40, 40)

        # 垂直居中
        layout.addStretch(2)

        # ==================== 标题区域 ====================
        title_container = QWidget()
        title_layout = QVBoxLayout(title_container)
        title_layout.setSpacing(12)
        title_layout.setAlignment(Qt.AlignCenter)

        # 主标题 - 放大3倍 (48*3=144)
        title_label = QLabel("dieΠ")
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet(f"""
            font-size: 64px;
            font-weight: 700;
            color: {Colors.TEXT_PRIMARY};
            letter-spacing: 6px;
        """)
        title_layout.addWidget(title_label)

        # 副标题
        subtitle_label = QLabel("量化回测系统")
        subtitle_label.setAlignment(Qt.AlignCenter)
        subtitle_label.setStyleSheet(f"""
            font-size: 24px;
            color: {Colors.TEXT_SECONDARY};
            letter-spacing: 12px;
        """)
        title_layout.addWidget(subtitle_label)

        layout.addWidget(title_container)

        layout.addSpacing(60)

        # ==================== 按钮区域 ====================
        btn_container = QWidget()
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.setSpacing(40)
        btn_layout.setAlignment(Qt.AlignCenter)

        # 新建回测按钮
        self.new_btn = self._create_main_button(
            title="新建回测",
            subtitle="策略代码 / signals / combo 回测",
            icon_text="+"
        )
        self.new_btn.clicked.connect(self.new_backtest.emit)
        btn_layout.addWidget(self.new_btn)

        # 历史记录按钮
        self.history_btn = self._create_main_button(
            title="历史记录",
            subtitle="查看已保存的回测结果",
            icon_text="☰"
        )
        self.history_btn.clicked.connect(self.view_history.emit)
        btn_layout.addWidget(self.history_btn)

        layout.addWidget(btn_container)

        layout.addStretch(3)

        # ==================== 底部版本信息 ====================
        version_label = QLabel(f"v{__version__}")
        version_label.setAlignment(Qt.AlignCenter)
        version_label.setStyleSheet(f"""
            font-size: 12px;
            color: {Colors.TEXT_MUTED};
        """)
        layout.addWidget(version_label)

    def _create_main_button(self, title: str, subtitle: str, icon_text: str) -> QPushButton:
        """创建主入口按钮。"""
        btn = QPushButton()
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(300, 220)

        # 使用布局来组织按钮内容
        btn_layout = QVBoxLayout(btn)
        btn_layout.setContentsMargins(24, 24, 24, 24)
        btn_layout.setSpacing(12)
        btn_layout.setAlignment(Qt.AlignCenter)

        # 图标
        icon_label = QLabel(icon_text)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setStyleSheet(f"""
            font-size: 40px;
            color: {Colors.ACCENT_BLUE};
        """)
        icon_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        btn_layout.addWidget(icon_label)

        # 标题
        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignCenter)
        title_label.setStyleSheet(f"""
            font-size: 24px;
            font-weight: 600;
            color: {Colors.TEXT_PRIMARY};
        """)
        title_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        btn_layout.addWidget(title_label)

        # 副标题
        subtitle_label = QLabel(subtitle)
        subtitle_label.setAlignment(Qt.AlignCenter)
        subtitle_label.setWordWrap(True)
        subtitle_label.setStyleSheet(f"""
            font-size: 14px;
            color: {Colors.TEXT_SECONDARY};
        """)
        subtitle_label.setAttribute(Qt.WA_TransparentForMouseEvents)
        btn_layout.addWidget(subtitle_label)

        # 按钮样式
        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.BG_SECONDARY};
                border: 3px solid {Colors.BORDER};
                border-radius: 12px;
            }}
            QPushButton:hover {{
                background-color: {Colors.BG_TERTIARY};
                border-color: {Colors.ACCENT_BLUE};
            }}
            QPushButton:pressed {{
                background-color: {Colors.BG_HOVER};
            }}
        """)

        return btn
