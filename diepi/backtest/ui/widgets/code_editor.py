"""
Python 代码编辑器

带语法高亮和行号的代码编辑器
"""

from PySide6.QtWidgets import QPlainTextEdit, QWidget, QTextEdit
from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import (
    QFont, QColor, QTextCharFormat, QSyntaxHighlighter,
    QPainter, QTextFormat, QPalette
)
import re

from ..styles import Colors, Fonts, Styles


class PythonHighlighter(QSyntaxHighlighter):
    """Python 语法高亮器"""

    KEYWORDS = [
        'and', 'as', 'assert', 'break', 'class', 'continue', 'def',
        'del', 'elif', 'else', 'except', 'False', 'finally', 'for',
        'from', 'global', 'if', 'import', 'in', 'is', 'lambda', 'None',
        'nonlocal', 'not', 'or', 'pass', 'raise', 'return', 'True',
        'try', 'while', 'with', 'yield',
    ]

    BUILTINS = [
        'abs', 'all', 'any', 'bin', 'bool', 'bytes', 'callable', 'chr',
        'dict', 'dir', 'divmod', 'enumerate', 'filter', 'float', 'format',
        'frozenset', 'getattr', 'globals', 'hasattr', 'hash', 'hex', 'id',
        'input', 'int', 'isinstance', 'issubclass', 'iter', 'len', 'list',
        'locals', 'map', 'max', 'min', 'next', 'object', 'oct', 'open',
        'ord', 'pow', 'print', 'property', 'range', 'repr', 'reversed',
        'round', 'set', 'setattr', 'slice', 'sorted', 'staticmethod', 'str',
        'sum', 'super', 'tuple', 'type', 'vars', 'zip',
    ]

    def __init__(self, document):
        super().__init__(document)
        self._init_formats()
        self._init_rules()

    def _init_formats(self):
        """初始化格式"""
        # 关键字 - 橙色
        self.keyword_format = QTextCharFormat()
        self.keyword_format.setForeground(QColor('#CC7832'))
        self.keyword_format.setFontWeight(QFont.Bold)

        # 内置函数 - 紫色
        self.builtin_format = QTextCharFormat()
        self.builtin_format.setForeground(QColor('#8888C6'))

        # 字符串 - 绿色
        self.string_format = QTextCharFormat()
        self.string_format.setForeground(QColor('#6A8759'))

        # 注释 - 灰色斜体
        self.comment_format = QTextCharFormat()
        self.comment_format.setForeground(QColor('#808080'))
        self.comment_format.setFontItalic(True)

        # 函数定义 - 黄色
        self.function_format = QTextCharFormat()
        self.function_format.setForeground(QColor('#FFC66D'))

        # 类定义 - 黄色加粗
        self.class_format = QTextCharFormat()
        self.class_format.setForeground(QColor('#FFC66D'))
        self.class_format.setFontWeight(QFont.Bold)

        # 数字 - 蓝色
        self.number_format = QTextCharFormat()
        self.number_format.setForeground(QColor('#6897BB'))

        # 装饰器 - 黄色
        self.decorator_format = QTextCharFormat()
        self.decorator_format.setForeground(QColor('#BBB529'))

    def _init_rules(self):
        """初始化规则"""
        self.rules = []

        # 关键字
        keyword_pattern = r'\b(' + '|'.join(self.KEYWORDS) + r')\b'
        self.rules.append((re.compile(keyword_pattern), self.keyword_format))

        # 内置函数
        builtin_pattern = r'\b(' + '|'.join(self.BUILTINS) + r')\b'
        self.rules.append((re.compile(builtin_pattern), self.builtin_format))

        # 装饰器
        self.rules.append((re.compile(r'@\w+'), self.decorator_format))

        # 函数定义
        self.rules.append((re.compile(r'\bdef\s+(\w+)'), self.function_format))

        # 类定义
        self.rules.append((re.compile(r'\bclass\s+(\w+)'), self.class_format))

        # 数字
        self.rules.append((re.compile(r'\b[0-9]+\.?[0-9]*\b'), self.number_format))

        # 单引号字符串
        self.rules.append((re.compile(r"'[^'\\]*(\\.[^'\\]*)*'"), self.string_format))

        # 双引号字符串
        self.rules.append((re.compile(r'"[^"\\]*(\\.[^"\\]*)*"'), self.string_format))

        # 注释
        self.rules.append((re.compile(r'#.*'), self.comment_format))

    def highlightBlock(self, text):
        """高亮文本块"""
        for pattern, fmt in self.rules:
            for match in pattern.finditer(text):
                start = match.start()
                length = match.end() - match.start()
                self.setFormat(start, length, fmt)


class LineNumberArea(QWidget):
    """行号区域"""

    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)


class CodeEditor(QPlainTextEdit):
    """
    代码编辑器

    功能:
    - Python 语法高亮
    - 行号显示
    - 当前行高亮
    - Tab 缩进 (4空格)
    """

    PORTFOLIO_TEMPLATE = '''# ═══════════════════════════════════════════════════════════════
# 组合策略模板（PortfolioStrategy 契约）
# ═══════════════════════════════════════════════════════════════
#
# 使用说明：
# - 日线模式 (freq='daily'): 交易逻辑写在 on_day() 中
# - 分钟模式 (freq='minute'): 交易逻辑写在 on_minute() 中
# - 两者互斥，不会同时调用
#
# 更多案例请参考仓库 examples/ 目录

# ==================== 策略参数 ====================
# 在这里定义你的参数，方便调整

STOP_LOSS = -0.05        # 止损线 (亏损5%卖出)
STOP_PROFIT = 0.10       # 止盈线 (盈利10%卖出)
MAX_HOLD_DAYS = 5        # 最大持仓天数


def on_init(ctx):
    """
    策略初始化 - 回测开始时执行一次

    ctx: 账户上下文对象

    用途：初始化变量、打印初始状态
    """
    print(f"策略初始化，初始资金: {ctx.get_cash():,.0f}")


def on_before_market_open(ctx):
    """
    盘前回调 - 每个交易日开盘前执行

    ctx: 账户上下文对象

    用途：选股、筛选今日要交易的股票
    返回值：股票列表 或 None (None表示使用全部股票池)
    """
    return None


def on_day(ctx, bars):
    """
    日线回调 - 日线模式下每个交易日执行一次

    ctx: 账户上下文对象
    bars: 今日所有股票的行情数据 (PortfolioBarData)
        - bars.symbols(): 获取股票列表
        - bars.get('000001.SZ'): 获取单只股票行情

    用途：日线级别的交易决策
    """
    today = ctx.current_date

    # ========== 1. 检查持仓 ==========
    for symbol, pos in ctx.get_positions().items():
        if pos.shares == 0:
            continue

        # pos.profit_pct: 盈亏比例 (框架自动计算)
        # pos.hold_days: 持有天数 (框架自动计算)

        # TODO: 添加你的卖出逻辑
        pass

    # ========== 2. 寻找买入机会 ==========
    for symbol in bars.symbols():
        bar = bars.get(symbol)
        if bar is None:
            continue

        # bar.open, bar.high, bar.low, bar.close, bar.vol

        # TODO: 添加你的买入逻辑
        pass


def on_minute(ctx, bars):
    """
    分钟回调 - 分钟模式下每分钟执行一次 (约240次/天)

    ctx: 账户上下文对象
    bars: 当前分钟所有股票的行情数据

    用途：分钟级别的交易决策 (日内交易)

    注意：分钟模式下 on_day() 不会被调用！
    """
    # TODO: 如果需要分钟级策略，在这里添加逻辑
    pass


def on_after_market_close(ctx):
    """
    盘后回调 - 每个交易日收盘后执行

    ctx: 账户上下文对象

    用途：统计、记录、日志输出
    """
    total = ctx.get_total_asset()
    cash = ctx.get_cash()
    print(f"收盘 | 总资产: {total:,.0f} | 现金: {cash:,.0f}")
'''

    SINGLE_TEMPLATE = '''# ═══════════════════════════════════════════════════════════════
# 单标的策略模板（Strategy 契约）
# ═══════════════════════════════════════════════════════════════
#
# 独立模式会为股票池中的每只股票启动一份单标的回测。
# 日线模式使用 on_day(ctx, bar)，分钟模式使用 on_minute(ctx, bar)。
# bar 是当前股票的一条 BarData，不是 PortfolioBarData。


def on_init(ctx):
    """每只股票的独立账户初始化一次。"""
    print(f"策略初始化，初始资金: {ctx.get_cash():,.0f}")


def on_before_market_open(ctx):
    """当前股票开盘前回调；无需返回股票列表。"""
    pass


def on_day(ctx, bar):
    """日线回调：bar 仅代表当前股票。"""
    # bar.open, bar.high, bar.low, bar.close, bar.vol
    # TODO: 添加单标的交易逻辑
    pass


def on_minute(ctx, bar):
    """分钟回调：与 on_day 按 freq 二选一执行。"""
    # TODO: 添加单标的分钟交易逻辑
    pass


def on_after_market_close(ctx):
    """当前股票收盘后的回调。"""
    print(f"收盘 | 现金: {ctx.get_cash():,.0f}")
'''

    # 兼容既有调用方；GUI 默认仍为组合模式。
    DEFAULT_TEMPLATE = PORTFOLIO_TEMPLATE

    def __init__(self, parent=None):
        super().__init__(parent)

        # 使用正常点数；由 Qt/系统 DPI 负责缩放。
        font = QFont('Cascadia Code', 12)
        font.setFixedPitch(True)
        self.setFont(font)

        # 设置 Tab 宽度 (4 空格)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * 4)

        # 设置深色主题 - 更美观的样式
        self.setStyleSheet(Styles.CODE_EDITOR)

        # 行号区域
        self.line_number_area = LineNumberArea(self)

        # 语法高亮
        self.highlighter = PythonHighlighter(self.document())

        # 连接信号
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self.highlight_current_line)

        # 初始化
        self.update_line_number_area_width(0)
        self.highlight_current_line()

        # 默认代码模板
        self.setPlainText(self.DEFAULT_TEMPLATE)

    def line_number_area_width(self):
        """计算行号区域宽度"""
        digits = len(str(max(1, self.blockCount())))
        space = 10 + self.fontMetrics().horizontalAdvance('9') * digits
        return space

    def update_line_number_area_width(self, _):
        """更新行号区域宽度"""
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy):
        """更新行号区域"""
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(
                0, rect.y(),
                self.line_number_area.width(), rect.height()
            )

        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event):
        """调整大小时更新行号区域"""
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.line_number_area.setGeometry(
            QRect(cr.left(), cr.top(),
                  self.line_number_area_width(), cr.height())
        )

    def highlight_current_line(self):
        """高亮当前行"""
        extra_selections = []

        if not self.isReadOnly():
            selection = QTextEdit.ExtraSelection()
            line_color = QColor(Colors.BG_SECONDARY)
            selection.format.setBackground(line_color)
            selection.format.setProperty(QTextFormat.FullWidthSelection, True)
            selection.cursor = self.textCursor()
            selection.cursor.clearSelection()
            extra_selections.append(selection)

        self.setExtraSelections(extra_selections)

    def line_number_area_paint_event(self, event):
        """绘制行号"""
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor(Colors.BG_DARK))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(
            self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                painter.setPen(QColor(Colors.TEXT_MUTED))
                painter.drawText(
                    0, top,
                    self.line_number_area.width() - 5,
                    self.fontMetrics().height(),
                    Qt.AlignRight, number
                )

            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1

    def keyPressEvent(self, event):
        """处理按键事件"""
        # Tab 转换为空格
        if event.key() == Qt.Key_Tab:
            self.insertPlainText('    ')
            return

        super().keyPressEvent(event)

    def get_code(self) -> str:
        """获取代码"""
        return self.toPlainText()

    def set_code(self, code: str) -> None:
        """设置代码"""
        self.setPlainText(code)

    @classmethod
    def template_for_strategy_kind(cls, strategy_kind: str) -> str:
        """Return the template matching one explicit engine contract."""
        if strategy_kind == 'portfolio':
            return cls.PORTFOLIO_TEMPLATE
        if strategy_kind == 'single':
            return cls.SINGLE_TEMPLATE
        raise ValueError(f"unsupported strategy_kind: {strategy_kind!r}")

    def reset_to_template(self, strategy_kind: str = 'portfolio') -> None:
        """重置为当前策略契约对应的模板。"""
        self.setPlainText(self.template_for_strategy_kind(strategy_kind))
