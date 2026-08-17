"""
接口速查面板

下拉框选择类型，显示完整接口列表和入参说明
点击即可插入代码
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QComboBox,
    QScrollArea, QFrame
)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QFont, QCursor

from ..styles import Colors, Fonts


class ClickableApiItem(QFrame):
    """可点击的API项"""

    clicked = Signal(str)

    def __init__(self, code: str, desc: str, params: str = "", parent=None):
        super().__init__(parent)
        self.code = code
        self.setCursor(QCursor(Qt.PointingHandCursor))
        self._init_ui(code, desc, params)

    def _init_ui(self, code: str, desc: str, params: str):
        self.setStyleSheet(f"""
            ClickableApiItem {{
                background-color: transparent;
                border: none;
                border-bottom: 1px solid {Colors.BORDER};
                padding: 12px 4px;
            }}
            ClickableApiItem:hover {{
                background-color: {Colors.BG_TERTIARY};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 14, 12, 14)
        layout.setSpacing(8)

        # 紧凑字号，避免接口栏压缩主编辑区。
        code_label = QLabel(code)
        code_label.setStyleSheet(f"""
            font-family: 'Cascadia Code', 'Consolas', monospace;
            font-size: 14px;
            font-weight: 500;
            color: {Colors.ACCENT_BLUE};
            background: transparent;
        """)
        code_label.setWordWrap(True)
        layout.addWidget(code_label)

        # 描述
        desc_label = QLabel(desc)
        desc_label.setStyleSheet(f"""
            font-size: 12px;
            color: {Colors.TEXT_SECONDARY};
            background: transparent;
        """)
        layout.addWidget(desc_label)

        # 参数说明（如果有）
        if params:
            params_label = QLabel(params)
            params_label.setStyleSheet(f"""
                font-size: 11px;
                color: {Colors.TEXT_MUTED};
                background: transparent;
                padding-left: 16px;
            """)
            params_label.setWordWrap(True)
            layout.addWidget(params_label)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.code)
        super().mousePressEvent(event)


class ApiPanel(QWidget):
    """
    接口速查面板

    Signals:
        api_clicked: 点击接口时发出 (接口代码)
    """

    api_clicked = Signal(str)

    # API 分类定义 - 包含代码、描述、参数说明
    API_CATEGORIES = {
        "交易接口": [
            ("ctx.buy_at_open(symbol, percent=0.1)",
             "开盘价买入（仅盘前可用）",
             "symbol: 股票代码 | percent: 占可用现金比例(执行时点,详见文档)"),
            ("ctx.sell_at_open(symbol, percent=1.0)",
             "开盘价卖出（仅盘前可用）",
             "symbol: 股票代码 | percent: 占持仓比例"),
            ("ctx.buy_at_market(symbol, percent=0.1)",
             "市价买入",
             "symbol | amount/shares/percent 三选一"),
            ("ctx.sell_at_market(symbol, percent=1.0)",
             "市价卖出",
             "symbol | shares/percent 二选一"),
            ("ctx.buy_at_price(symbol, price, shares=100)",
             "限价买入",
             "price: 限价(必填) | shares: 股数"),
            ("ctx.sell_at_price(symbol, price, shares=100)",
             "限价卖出",
             "price: 限价(必填) | shares: 股数"),
        ],
        "止损止盈": [
            ("ctx.sell_stop_loss(symbol, price, percent=1.0)",
             "止损卖出（价格下穿触发）",
             "price: 止损价 | 当价格<=止损价时触发"),
            ("ctx.sell_stop_profit(symbol, price, percent=1.0)",
             "止盈卖出（价格上穿触发）",
             "price: 止盈价 | 当价格>=止盈价时触发"),
            ("ctx.buy_stop(symbol, price, percent=0.1)",
             "突破买入（价格上穿触发）",
             "price: 触发价 | 当价格>=触发价时买入"),
            ("ctx.cancel_order(order_id)",
             "取消指定订单",
             "order_id: 订单ID"),
            ("ctx.cancel_orders(symbol, side='sell')",
             "批量取消订单",
             "symbol: 股票 | side: 'buy'/'sell'/None"),
            ("ctx.cancel_all_orders()",
             "取消所有未完成订单",
             ""),
        ],
        "持仓查询": [
            ("ctx.get_position(symbol)",
             "获取单个持仓",
             "返回 Position 对象或 None"),
            ("ctx.get_positions()",
             "获取所有持仓",
             "返回 Dict[symbol, Position]"),
            ("pos.shares",
             "持仓股数",
             ""),
            ("pos.available_shares",
             "可卖股数",
             "T+1规则，买入当日不可卖"),
            ("pos.avg_cost",
             "平均成本价",
             "含手续费"),
            ("pos.market_value",
             "持仓市值",
             ""),
            ("pos.profit_pct",
             "盈亏比例",
             "正数盈利，负数亏损"),
            ("pos.hold_days",
             "持有天数",
             "自然日计算，买入当天=0"),
            ("pos.entry_date",
             "首次买入日期",
             "格式 YYYYMMDD"),
        ],
        "资金查询": [
            ("ctx.get_cash()",
             "可用现金",
             ""),
            ("ctx.get_total_asset()",
             "总资产",
             "现金 + 股票市值"),
        ],
        "数据接口": [
            ("ctx.get_daily(days=5)",
             "获取最近N个交易日日线",
             "返回 DataFrame [open,high,low,close,vol]"),
            ("ctx.get_daily(start_date='20250101')",
             "从指定日期到当前的日线",
             "累计窗口模式"),
            ("ctx.get_daily(start_date, end_date)",
             "指定区间日线",
             "自动截断到可见边界"),
            ("ctx.get_minute(days=2)",
             "获取最近N天分钟线",
             "盘中包含当日已完成K线"),
            ("ctx.get_cyq(days=1)",
             "筹码分布",
             "返回 [price, percent]"),
            ("ctx.get_moneyflow(days=5)",
             "资金流向",
             "大单/中单/小单 买卖额"),
            ("ctx.get_margin(days=5)",
             "融资融券",
             "融资余额、融券余额等"),
            ("ctx.get_basic(days=5)",
             "基本面数据",
             "PE/PB/市值/换手率等"),
        ],
        "K线数据 (bars)": [
            ("bars.get(symbol)",
             "获取指定股票K线",
             "返回 BarData 对象"),
            ("bars.symbols()",
             "当日所有股票代码列表",
             ""),
            ("bar.open / bar.high / bar.low / bar.close",
             "OHLC价格",
             ""),
            ("bar.vol",
             "成交量",
             ""),
            ("bar.amount",
             "成交额",
             ""),
            ("bar.pre_close",
             "昨收价",
             "用于计算涨跌幅"),
        ],
        "时间工具": [
            ("ctx.current_date",
             "当前回测日期",
             "格式 YYYYMMDD"),
            ("ctx.is_trade_day(date)",
             "判断是否交易日",
             "date可选，默认当前日期"),
            ("ctx.get_prev_trade_day(date, n=1)",
             "获取前N个交易日",
             "返回 YYYYMMDD"),
            ("ctx.get_next_trade_day(date, n=1)",
             "获取后N个交易日",
             "返回 YYYYMMDD"),
        ],
        "股票池": [
            ("ctx.get_stock_pool()",
             "获取当前股票池",
             "返回股票代码列表"),
            ("on_before_market_open 返回值",
             "动态设置当日股票池",
             "返回 None 使用全部，返回列表使用指定"),
        ],
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 12, 8, 12)
        layout.setSpacing(8)

        # 标题
        title = QLabel("接口速查")
        title.setStyleSheet(f"""
            font-size: 18px;
            font-weight: 600;
            color: {Colors.TEXT_PRIMARY};
        """)
        layout.addWidget(title)

        # 提示
        hint = QLabel("选择分类，点击插入代码")
        hint.setStyleSheet(f"""
            color: {Colors.TEXT_MUTED};
            font-size: 12px;
            margin-bottom: 12px;
        """)
        layout.addWidget(hint)

        # 分类下拉框 - 30px
        self.category_combo = QComboBox()
        self.category_combo.setStyleSheet(f"""
            QComboBox {{
                background-color: {Colors.BG_TERTIARY};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 12px 20px;
                font-size: 13px;
                font-weight: 500;
            }}
            QComboBox:hover {{
                border-color: {Colors.ACCENT_BLUE};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 36px;
            }}
            QComboBox::down-arrow {{
                image: none;
                border-left: 8px solid transparent;
                border-right: 8px solid transparent;
                border-top: 10px solid {Colors.TEXT_SECONDARY};
            }}
            QComboBox QAbstractItemView {{
                background-color: {Colors.BG_SECONDARY};
                color: {Colors.TEXT_PRIMARY};
                selection-background-color: {Colors.ACCENT_BLUE};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 6px;
                font-size: 12px;
            }}
        """)

        for category in self.API_CATEGORIES.keys():
            self.category_combo.addItem(category)

        self.category_combo.currentTextChanged.connect(self._on_category_changed)
        layout.addWidget(self.category_combo)

        # 滚动区域（只有垂直滚动条）
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet(f"""
            QScrollArea {{
                background: transparent;
                border: none;
            }}
            QScrollBar:vertical {{
                background-color: {Colors.BG_PRIMARY};
                width: 8px;
                border-radius: 4px;
            }}
            QScrollBar::handle:vertical {{
                background-color: {Colors.BG_TERTIARY};
                border-radius: 4px;
                min-height: 30px;
            }}
            QScrollBar::handle:vertical:hover {{
                background-color: {Colors.BG_HOVER};
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
        """)

        # 内容容器
        self.content_widget = QWidget()
        self.content_widget.setStyleSheet("background: transparent;")
        self.content_layout = QVBoxLayout(self.content_widget)
        self.content_layout.setContentsMargins(0, 8, 0, 0)
        self.content_layout.setSpacing(0)

        self.scroll.setWidget(self.content_widget)
        layout.addWidget(self.scroll, stretch=1)

        # 初始显示第一个分类
        self._on_category_changed(self.category_combo.currentText())

    def _on_category_changed(self, category: str):
        """分类改变时更新接口列表"""
        # 清空现有内容
        while self.content_layout.count():
            item = self.content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # 添加新内容
        apis = self.API_CATEGORIES.get(category, [])
        for api_data in apis:
            code = api_data[0]
            desc = api_data[1] if len(api_data) > 1 else ""
            params = api_data[2] if len(api_data) > 2 else ""

            item = ClickableApiItem(code, desc, params)
            item.clicked.connect(self._on_api_clicked)
            self.content_layout.addWidget(item)

        # 添加弹性空间
        self.content_layout.addStretch()

    def _on_api_clicked(self, code: str):
        """点击接口时发出信号"""
        self.api_clicked.emit(code)
