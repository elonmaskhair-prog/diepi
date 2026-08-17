"""
统一样式系统

定义颜色、字体、组件样式
"""

# ═══════════════════════════════════════════════════════════════
# 颜色定义
# ═══════════════════════════════════════════════════════════════

class Colors:
    """颜色常量"""

    # 背景色
    BG_DARK = "#0d1117"          # 最深背景
    BG_PRIMARY = "#161b22"       # 主背景
    BG_SECONDARY = "#21262d"     # 次级背景 (卡片/面板)
    BG_TERTIARY = "#30363d"      # 第三级背景 (输入框)
    BG_HOVER = "#3c444d"         # 悬停背景

    # 边框
    BORDER = "#30363d"
    BORDER_LIGHT = "#3c444d"
    BORDER_FOCUS = "#58a6ff"

    # 文字
    TEXT_PRIMARY = "#e6edf3"     # 主文字
    TEXT_SECONDARY = "#8b949e"   # 次要文字
    TEXT_MUTED = "#6e7681"       # 淡化文字

    # 强调色
    ACCENT_BLUE = "#58a6ff"      # 蓝色强调
    ACCENT_GREEN = "#3fb950"     # 绿色 (成功/盈利)
    ACCENT_RED = "#f85149"       # 红色 (错误/亏损)
    ACCENT_ORANGE = "#d29922"    # 橙色 (警告)
    ACCENT_PURPLE = "#a371f7"    # 紫色

    # 按钮
    BTN_PRIMARY = "#238636"      # 主按钮 (绿色)
    BTN_PRIMARY_HOVER = "#2ea043"
    BTN_SECONDARY = "#21262d"    # 次按钮
    BTN_SECONDARY_HOVER = "#30363d"
    BTN_DANGER = "#da3633"       # 危险按钮
    BTN_DANGER_HOVER = "#f85149"

    # 图表
    CHART_LINE = "#58a6ff"
    CHART_UP = "#3fb950"
    CHART_DOWN = "#f85149"
    CHART_GRID = "#21262d"


# ═══════════════════════════════════════════════════════════════
# 字体定义
# ═══════════════════════════════════════════════════════════════

class Fonts:
    """字体常量"""

    FAMILY = "'Segoe UI', 'Microsoft YaHei', sans-serif"
    FAMILY_MONO = "'Cascadia Code', 'Consolas', 'Microsoft YaHei', monospace"

    SIZE_SMALL = "11px"
    SIZE_NORMAL = "13px"
    SIZE_LARGE = "15px"
    SIZE_XLARGE = "18px"
    SIZE_TITLE = "22px"


# ═══════════════════════════════════════════════════════════════
# 组件样式
# ═══════════════════════════════════════════════════════════════

class Styles:
    """组件样式表"""

    # ==================== 全局样式 ====================

    GLOBAL = f"""
        QWidget {{
            font-family: {Fonts.FAMILY};
            font-size: {Fonts.SIZE_NORMAL};
            color: {Colors.TEXT_PRIMARY};
        }}

        QToolTip {{
            background-color: {Colors.BG_SECONDARY};
            color: {Colors.TEXT_PRIMARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 6px;
            padding: 6px 10px;
        }}
    """

    # ==================== 按钮样式 ====================

    BTN_PRIMARY = f"""
        QPushButton {{
            background-color: {Colors.BTN_PRIMARY};
            color: white;
            font-weight: 600;
            font-size: {Fonts.SIZE_NORMAL};
            padding: 8px 20px;
            border: none;
            border-radius: 6px;
            min-height: 20px;
        }}
        QPushButton:hover {{
            background-color: {Colors.BTN_PRIMARY_HOVER};
        }}
        QPushButton:pressed {{
            background-color: #196c2e;
        }}
        QPushButton:disabled {{
            background-color: {Colors.BG_TERTIARY};
            color: {Colors.TEXT_MUTED};
        }}
    """

    BTN_SECONDARY = f"""
        QPushButton {{
            background-color: {Colors.BG_SECONDARY};
            color: {Colors.TEXT_PRIMARY};
            font-size: {Fonts.SIZE_NORMAL};
            padding: 8px 16px;
            border: 1px solid {Colors.BORDER};
            border-radius: 6px;
            min-height: 20px;
        }}
        QPushButton:hover {{
            background-color: {Colors.BG_TERTIARY};
            border-color: {Colors.BORDER_LIGHT};
        }}
        QPushButton:pressed {{
            background-color: {Colors.BG_HOVER};
        }}
        QPushButton:disabled {{
            background-color: {Colors.BG_SECONDARY};
            color: {Colors.TEXT_MUTED};
            border-color: {Colors.BORDER};
        }}
    """

    BTN_DANGER = f"""
        QPushButton {{
            background-color: {Colors.BTN_DANGER};
            color: white;
            font-weight: 600;
            font-size: {Fonts.SIZE_NORMAL};
            padding: 8px 20px;
            border: none;
            border-radius: 6px;
            min-height: 20px;
        }}
        QPushButton:hover {{
            background-color: {Colors.BTN_DANGER_HOVER};
        }}
        QPushButton:pressed {{
            background-color: #b62324;
        }}
        QPushButton:disabled {{
            background-color: {Colors.BG_TERTIARY};
            color: {Colors.TEXT_MUTED};
        }}
    """

    BTN_SMALL = f"""
        QPushButton {{
            background-color: {Colors.BG_SECONDARY};
            color: {Colors.TEXT_SECONDARY};
            font-size: {Fonts.SIZE_SMALL};
            padding: 4px 10px;
            border: 1px solid {Colors.BORDER};
            border-radius: 4px;
        }}
        QPushButton:hover {{
            background-color: {Colors.BG_TERTIARY};
            color: {Colors.TEXT_PRIMARY};
        }}
    """

    # ==================== 输入框样式 ====================

    INPUT = f"""
        QLineEdit, QTextEdit, QPlainTextEdit {{
            background-color: {Colors.BG_TERTIARY};
            color: {Colors.TEXT_PRIMARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 6px;
            padding: 8px 12px;
            font-size: {Fonts.SIZE_NORMAL};
            selection-background-color: {Colors.ACCENT_BLUE};
        }}
        QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
            border-color: {Colors.BORDER_FOCUS};
            background-color: {Colors.BG_SECONDARY};
        }}
        QLineEdit:disabled, QTextEdit:disabled {{
            background-color: {Colors.BG_PRIMARY};
            color: {Colors.TEXT_MUTED};
        }}
        QLineEdit::placeholder {{
            color: {Colors.TEXT_MUTED};
        }}
    """

    # ==================== 日期编辑器样式 ====================

    DATE_EDIT = f"""
        QDateEdit {{
            background-color: {Colors.BG_TERTIARY};
            color: {Colors.TEXT_PRIMARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 6px;
            padding: 8px 12px;
            font-size: {Fonts.SIZE_NORMAL};
            font-family: {Fonts.FAMILY_MONO};
        }}
        QDateEdit:focus {{
            border-color: {Colors.BORDER_FOCUS};
            background-color: {Colors.BG_SECONDARY};
        }}
        QDateEdit::drop-down {{
            subcontrol-origin: padding;
            subcontrol-position: center right;
            width: 24px;
            border: none;
            background: transparent;
        }}
        QDateEdit::down-arrow {{
            image: none;
            border-left: 5px solid transparent;
            border-right: 5px solid transparent;
            border-top: 6px solid {Colors.TEXT_SECONDARY};
            margin-right: 8px;
        }}
        QDateEdit::down-arrow:hover {{
            border-top-color: {Colors.ACCENT_BLUE};
        }}

        /* 日历弹出框样式 */
        QCalendarWidget {{
            background-color: {Colors.BG_SECONDARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 8px;
        }}
        QCalendarWidget QWidget {{
            alternate-background-color: {Colors.BG_TERTIARY};
        }}
        QCalendarWidget QAbstractItemView:enabled {{
            background-color: {Colors.BG_SECONDARY};
            color: {Colors.TEXT_PRIMARY};
            selection-background-color: {Colors.ACCENT_BLUE};
            selection-color: white;
        }}
        QCalendarWidget QAbstractItemView:disabled {{
            color: {Colors.TEXT_MUTED};
        }}
        QCalendarWidget QToolButton {{
            color: {Colors.TEXT_PRIMARY};
            background-color: transparent;
            border: none;
            border-radius: 4px;
            padding: 4px 8px;
            margin: 2px;
        }}
        QCalendarWidget QToolButton:hover {{
            background-color: {Colors.BG_TERTIARY};
        }}
        QCalendarWidget QSpinBox {{
            background-color: {Colors.BG_TERTIARY};
            color: {Colors.TEXT_PRIMARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 4px;
            padding: 2px 6px;
        }}
        QCalendarWidget #qt_calendar_navigationbar {{
            background-color: {Colors.BG_PRIMARY};
            border-bottom: 1px solid {Colors.BORDER};
            border-radius: 8px 8px 0 0;
            padding: 4px;
        }}
    """

    # ==================== 数字输入框样式 ====================

    SPINBOX = f"""
        QSpinBox, QDoubleSpinBox {{
            background-color: {Colors.BG_TERTIARY};
            color: {Colors.TEXT_PRIMARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 6px;
            padding: 8px 12px;
            font-size: {Fonts.SIZE_NORMAL};
        }}
        QSpinBox:focus, QDoubleSpinBox:focus {{
            border-color: {Colors.BORDER_FOCUS};
            background-color: {Colors.BG_SECONDARY};
        }}
        QSpinBox::up-button, QDoubleSpinBox::up-button {{
            subcontrol-origin: border;
            subcontrol-position: top right;
            width: 20px;
            border: none;
            background: {Colors.BG_SECONDARY};
            border-left: 1px solid {Colors.BORDER};
            border-top-right-radius: 5px;
        }}
        QSpinBox::down-button, QDoubleSpinBox::down-button {{
            subcontrol-origin: border;
            subcontrol-position: bottom right;
            width: 20px;
            border: none;
            background: {Colors.BG_SECONDARY};
            border-left: 1px solid {Colors.BORDER};
            border-bottom-right-radius: 5px;
        }}
        QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
        QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
            background: {Colors.BG_TERTIARY};
        }}
        QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-bottom: 5px solid {Colors.TEXT_SECONDARY};
        }}
        QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
            border-left: 4px solid transparent;
            border-right: 4px solid transparent;
            border-top: 5px solid {Colors.TEXT_SECONDARY};
        }}
    """

    # ==================== 分组框样式 ====================

    GROUPBOX = f"""
        QGroupBox {{
            background-color: {Colors.BG_SECONDARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 8px;
            margin-top: 8px;
            padding: 8px 12px 8px 12px;
            font-weight: 600;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            left: 12px;
            top: 2px;
            padding: 0 8px;
            background-color: {Colors.BG_SECONDARY};
            color: {Colors.TEXT_PRIMARY};
        }}
    """

    # ==================== 标签页样式 ====================

    TABS = f"""
        QTabWidget::pane {{
            background-color: {Colors.BG_SECONDARY};
            border: 1px solid {Colors.BORDER};
            border-radius: 8px;
            border-top-left-radius: 0;
        }}
        QTabBar::tab {{
            background-color: {Colors.BG_PRIMARY};
            color: {Colors.TEXT_SECONDARY};
            border: 1px solid {Colors.BORDER};
            border-bottom: none;
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
            padding: 8px 20px;
            margin-right: 2px;
        }}
        QTabBar::tab:selected {{
            background-color: {Colors.BG_SECONDARY};
            color: {Colors.TEXT_PRIMARY};
            border-bottom: 2px solid {Colors.ACCENT_BLUE};
        }}
        QTabBar::tab:hover:!selected {{
            background-color: {Colors.BG_TERTIARY};
            color: {Colors.TEXT_PRIMARY};
        }}
    """

    # ==================== 表格样式 ====================

    TABLE = f"""
        QTableWidget {{
            background-color: {Colors.BG_SECONDARY};
            alternate-background-color: {Colors.BG_PRIMARY};
            border: none;
            border-radius: 8px;
            gridline-color: {Colors.BORDER};
        }}
        QTableWidget::item {{
            padding: 8px;
            border-bottom: 1px solid {Colors.BORDER};
        }}
        QTableWidget::item:selected {{
            background-color: {Colors.ACCENT_BLUE};
            color: white;
        }}
        QHeaderView::section {{
            background-color: {Colors.BG_PRIMARY};
            color: {Colors.TEXT_PRIMARY};
            font-weight: 600;
            padding: 10px 8px;
            border: none;
            border-bottom: 2px solid {Colors.BORDER};
            border-right: 1px solid {Colors.BORDER};
        }}
        QHeaderView::section:last {{
            border-right: none;
        }}
    """

    # ==================== 滚动条样式 ====================

    SCROLLBAR = f"""
        QScrollBar:vertical {{
            background-color: {Colors.BG_PRIMARY};
            width: 12px;
            border-radius: 6px;
            margin: 0;
        }}
        QScrollBar::handle:vertical {{
            background-color: {Colors.BG_TERTIARY};
            border-radius: 6px;
            min-height: 30px;
            margin: 2px;
        }}
        QScrollBar::handle:vertical:hover {{
            background-color: {Colors.BG_HOVER};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0;
        }}
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
            background: none;
        }}

        QScrollBar:horizontal {{
            background-color: {Colors.BG_PRIMARY};
            height: 12px;
            border-radius: 6px;
            margin: 0;
        }}
        QScrollBar::handle:horizontal {{
            background-color: {Colors.BG_TERTIARY};
            border-radius: 6px;
            min-width: 30px;
            margin: 2px;
        }}
        QScrollBar::handle:horizontal:hover {{
            background-color: {Colors.BG_HOVER};
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0;
        }}
    """

    # ==================== 进度条样式 ====================

    PROGRESSBAR = f"""
        QProgressBar {{
            background-color: {Colors.BG_TERTIARY};
            border: none;
            border-radius: 4px;
            height: 8px;
            text-align: center;
        }}
        QProgressBar::chunk {{
            background-color: {Colors.ACCENT_BLUE};
            border-radius: 4px;
        }}
    """

    # ==================== 单选框样式 ====================

    RADIO = f"""
        QRadioButton {{
            color: {Colors.TEXT_PRIMARY};
            spacing: 8px;
        }}
        QRadioButton::indicator {{
            width: 18px;
            height: 18px;
            border-radius: 9px;
            border: 2px solid {Colors.BORDER_LIGHT};
            background-color: {Colors.BG_TERTIARY};
        }}
        QRadioButton::indicator:hover {{
            border-color: {Colors.ACCENT_BLUE};
        }}
        QRadioButton::indicator:checked {{
            border-color: {Colors.ACCENT_BLUE};
            background-color: {Colors.ACCENT_BLUE};
        }}
        QRadioButton::indicator:checked::after {{
            background-color: white;
        }}
    """

    # ==================== 列表样式 ====================

    LIST = f"""
        QListWidget {{
            background-color: transparent;
            border: none;
            outline: none;
        }}
        QListWidget::item {{
            padding: 6px 10px;
            border-radius: 4px;
            color: {Colors.TEXT_SECONDARY};
        }}
        QListWidget::item:hover {{
            background-color: {Colors.BG_TERTIARY};
            color: {Colors.TEXT_PRIMARY};
        }}
        QListWidget::item:selected {{
            background-color: {Colors.ACCENT_BLUE};
            color: white;
        }}
    """

    # ==================== 分割线样式 ====================

    SPLITTER = f"""
        QSplitter::handle {{
            background-color: {Colors.BORDER};
        }}
        QSplitter::handle:hover {{
            background-color: {Colors.ACCENT_BLUE};
        }}
        QSplitter::handle:horizontal {{
            width: 2px;
        }}
        QSplitter::handle:vertical {{
            height: 2px;
        }}
    """

    # ==================== 代码编辑器样式 ====================

    CODE_EDITOR = f"""
        QPlainTextEdit {{
            background-color: {Colors.BG_DARK};
            color: #e6edf3;
            border: 1px solid {Colors.BORDER};
            border-radius: 8px;
            selection-background-color: #264f78;
            padding: 8px;
            font-family: {Fonts.FAMILY_MONO};
            font-size: 24px;
        }}
        QPlainTextEdit:focus {{
            border-color: {Colors.BORDER_FOCUS};
        }}
    """

    # ==================== 调试面板样式 ====================

    DEBUG_OUTPUT = f"""
        QPlainTextEdit {{
            background-color: {Colors.BG_DARK};
            color: {Colors.ACCENT_GREEN};
            border: 1px solid {Colors.BORDER};
            border-radius: 6px;
            padding: 8px;
            font-family: {Fonts.FAMILY_MONO};
            font-size: 12px;
        }}
    """

    # ==================== 状态栏样式 ====================

    STATUSBAR = f"""
        QStatusBar {{
            background-color: {Colors.BG_PRIMARY};
            color: {Colors.TEXT_SECONDARY};
            border-top: 1px solid {Colors.BORDER};
        }}
        QStatusBar::item {{
            border: none;
        }}
    """


def get_app_stylesheet():
    """获取完整的应用样式表"""
    return f"""
        {Styles.GLOBAL}
        {Styles.INPUT}
        {Styles.DATE_EDIT}
        {Styles.SPINBOX}
        {Styles.GROUPBOX}
        {Styles.TABS}
        {Styles.TABLE}
        {Styles.SCROLLBAR}
        {Styles.PROGRESSBAR}
        {Styles.RADIO}
        {Styles.SPLITTER}
        {Styles.STATUSBAR}
    """
