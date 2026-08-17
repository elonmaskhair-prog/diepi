"""
K线图组件

使用 pyqtgraph 自绘制 K 线图，包含:
- K线图 (蜡烛图)
- 成交量柱状图
- MACD 指标 (12, 26, 9)
- 买卖标记
- 悬浮信息窗
"""

from typing import List, Dict, Optional, Tuple
import bisect
import numpy as np
import pandas as pd

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton
)
from PySide6.QtCore import Qt, Signal, QRectF, QPointF
from PySide6.QtGui import QFont, QPainter, QColor, QPen, QBrush, QPolygonF

from ..styles import Colors

# 尝试导入 pyqtgraph
try:
    import pyqtgraph as pg
    from pyqtgraph import AxisItem, GraphicsObject
    HAS_PYQTGRAPH = True
    # 启用OpenGL加速 - 利用GPU渲染提升性能
    pg.setConfigOptions(useOpenGL=True, antialias=True)
except ImportError:
    HAS_PYQTGRAPH = False


class IndependentScaleViewBox(pg.ViewBox):
    """
    支持独立X/Y轴缩放的ViewBox

    交互方式:
    - 普通滚轮: 缩放Y轴（价格）
    - Shift+滚轮: 缩放X轴（时间）
    - 拖动: 平移
    """

    def wheelEvent(self, ev, axis=None):
        """重写滚轮事件，实现独立轴缩放"""
        # 获取修饰键状态
        modifiers = ev.modifiers() if hasattr(ev, 'modifiers') else Qt.NoModifier

        if modifiers == Qt.ShiftModifier:
            # Shift+滚轮: 缩放X轴（时间）
            super().wheelEvent(ev, axis=0)
        else:
            # 普通滚轮: 缩放Y轴（价格）
            super().wheelEvent(ev, axis=1)


class CandlestickItem(GraphicsObject):
    """
    K线蜡烛图项

    使用 QPainter 绘制蜡烛图，支持视口裁剪优化
    """

    def __init__(self):
        super().__init__()
        self.data = None
        self.picture = None
        self._visible_range = None  # (start_idx, end_idx) 可见范围
        # 缓存numpy数组加速访问
        self._np_data = None

    def set_data(self, data: pd.DataFrame):
        """
        设置K线数据

        Args:
            data: DataFrame with columns [open, high, low, close]
                  index 应为连续整数
        """
        self.data = data
        # 转换为numpy数组加速访问
        self._np_data = data[['open', 'high', 'low', 'close']].values
        self.picture = None
        self._visible_range = None
        self.prepareGeometryChange()
        self.update()

    def set_visible_range(self, start_idx: int, end_idx: int):
        """
        设置可见范围（视口裁剪）

        Args:
            start_idx: 起始索引
            end_idx: 结束索引
        """
        new_range = (max(0, start_idx), min(len(self.data) if self.data is not None else 0, end_idx))
        if new_range != self._visible_range:
            self._visible_range = new_range
            self.picture = None  # 重新生成绘图
            self.update()

    def paint(self, painter, option, widget):
        if self.data is None or self.data.empty:
            return

        if self.picture is None:
            self._generate_picture()

        if self.picture:
            self.picture.play(painter)

    def _generate_picture(self):
        """生成绘图缓存"""
        self.picture = pg.QtGui.QPicture()
        painter = QPainter(self.picture)

        # 颜色定义
        up_color = QColor(Colors.ACCENT_RED)      # 上涨红色
        down_color = QColor(Colors.ACCENT_GREEN)  # 下跌绿色

        w = 0.35  # 蜡烛宽度

        # 确定绘制范围
        if self._visible_range:
            start_idx, end_idx = self._visible_range
        else:
            start_idx, end_idx = 0, len(self._np_data)

        for i in range(start_idx, end_idx):
            o, h, l, c = self._np_data[i]

            if c >= o:
                color = up_color
            else:
                color = down_color

            painter.setPen(pg.mkPen(color, width=1))
            painter.setBrush(QBrush(color))

            # 先绘制影线 (高低价)
            painter.drawLine(QPointF(i, l), QPointF(i, h))

            # 再绘制实体 (开收价)
            body_top = max(o, c)
            body_bottom = min(o, c)
            body_height = body_top - body_bottom

            if body_height < 0.001:
                painter.setPen(pg.mkPen(color, width=2))
                painter.drawLine(QPointF(i - w, c), QPointF(i + w, c))
            else:
                painter.drawRect(QRectF(i - w, body_bottom, w * 2, body_height))

        painter.end()

    def boundingRect(self):
        if self.data is None or self.data.empty:
            return QRectF()

        # 返回数据的边界
        min_y = self.data['low'].min()
        max_y = self.data['high'].max()
        return QRectF(0, min_y, len(self.data), max_y - min_y)


class VolumeBarItem(GraphicsObject):
    """
    成交量柱状图项，支持视口裁剪优化
    """

    def __init__(self):
        super().__init__()
        self.data = None
        self.picture = None
        self._visible_range = None
        self._np_data = None

    def set_data(self, data: pd.DataFrame):
        """
        设置成交量数据

        Args:
            data: DataFrame with columns [open, close, vol]
        """
        self.data = data
        self._np_data = data[['open', 'close', 'vol']].values
        self.picture = None
        self._visible_range = None
        self.prepareGeometryChange()
        self.update()

    def set_visible_range(self, start_idx: int, end_idx: int):
        """设置可见范围"""
        new_range = (max(0, start_idx), min(len(self.data) if self.data is not None else 0, end_idx))
        if new_range != self._visible_range:
            self._visible_range = new_range
            self.picture = None
            self.update()

    def paint(self, painter, option, widget):
        if self.data is None or self.data.empty:
            return

        if self.picture is None:
            self._generate_picture()

        if self.picture:
            self.picture.play(painter)

    def _generate_picture(self):
        """生成绘图缓存"""
        self.picture = pg.QtGui.QPicture()
        painter = QPainter(self.picture)

        up_color = QColor(Colors.ACCENT_RED)
        down_color = QColor(Colors.ACCENT_GREEN)

        w = 0.35

        if self._visible_range:
            start_idx, end_idx = self._visible_range
        else:
            start_idx, end_idx = 0, len(self._np_data)

        for i in range(start_idx, end_idx):
            o, c, vol = self._np_data[i]
            is_up = c >= o

            color = up_color if is_up else down_color
            painter.setPen(pg.mkPen(color, width=1))
            painter.setBrush(QBrush(color))

            painter.drawRect(QRectF(i - w, 0, w * 2, vol))

        painter.end()

    def boundingRect(self):
        if self.data is None or self.data.empty:
            return QRectF()

        max_vol = self.data['vol'].max()
        return QRectF(0, 0, len(self.data), max_vol)


class MACDBarItem(GraphicsObject):
    """
    MACD柱状图项 - 批量绘制优化版，支持视口裁剪

    使用QPicture一次性绘制所有柱子，避免创建大量BarGraphItem对象
    """

    def __init__(self):
        super().__init__()
        self.data = None
        self.picture = None
        self._visible_range = None

    def set_data(self, macd_values: list):
        """
        设置MACD数据

        Args:
            macd_values: MACD柱状值列表
        """
        self.data = macd_values
        self.picture = None
        self._visible_range = None
        self.prepareGeometryChange()
        self.update()

    def set_visible_range(self, start_idx: int, end_idx: int):
        """设置可见范围"""
        new_range = (max(0, start_idx), min(len(self.data) if self.data else 0, end_idx))
        if new_range != self._visible_range:
            self._visible_range = new_range
            self.picture = None
            self.update()

    def paint(self, painter, option, widget):
        if self.data is None or len(self.data) == 0:
            return

        if self.picture is None:
            self._generate_picture()

        if self.picture:
            self.picture.play(painter)

    def _generate_picture(self):
        """生成绘图缓存"""
        self.picture = pg.QtGui.QPicture()
        painter = QPainter(self.picture)

        up_color = QColor(Colors.ACCENT_RED)
        down_color = QColor(Colors.ACCENT_GREEN)

        w = 0.3  # 柱状宽度

        if self._visible_range:
            start_idx, end_idx = self._visible_range
        else:
            start_idx, end_idx = 0, len(self.data)

        for i in range(start_idx, end_idx):
            val = self.data[i]
            color = up_color if val >= 0 else down_color
            painter.setPen(pg.mkPen(color, width=1))
            painter.setBrush(QBrush(color))

            # 绘制柱状图
            if val >= 0:
                painter.drawRect(QRectF(i - w, 0, w * 2, val))
            else:
                painter.drawRect(QRectF(i - w, val, w * 2, -val))

        painter.end()

    def boundingRect(self):
        if self.data is None or len(self.data) == 0:
            return QRectF()

        min_val = min(self.data)
        max_val = max(self.data)
        return QRectF(0, min_val, len(self.data), max_val - min_val)


class TradeMarkerItem(GraphicsObject):
    """
    买卖标记项 (绘制优化版)

    在K线图上显示买卖点 (横线 + 标签)
    使用 QPainter 直接绘制，只绘制可见区域内的标记，大幅提升性能
    """

    def __init__(self):
        super().__init__()
        self.trades = []  # [(x_idx, price, is_buy, info), ...]
        self._visible_range = None
        self._picture = None

    def set_trades(self, trades: List[Dict]):
        """
        设置交易记录

        Args:
            trades: 交易列表，每个元素包含 {x_idx, price, is_buy, ...}
        """
        self.trades = trades
        self._x_indices = [t['x_idx'] for t in self.trades] # 预先生成索引以便二分查找
        self._picture = None
        self._visible_range = None
        self.prepareGeometryChange()
        self.update()

    def set_visible_range(self, start_idx: int, end_idx: int):
        """设置可见范围"""
        new_range = (max(0, start_idx), min(len(self.trades) if self.trades else 0, end_idx))
        # 注意: 这里的 start_idx/end_idx 是X轴索引，但 self.trades 是列表，不能直接用 slice
        # 需要找到 X 轴在范围内的 trades
        # 简单起见，我们暂存 range，在 paint 里过滤
        # 为了避免频繁重绘，只有当 range 变化很大时才清理缓存？
        # 实际上 QPicture 绘制直线很快，直接重绘可能比维护复杂缓存更好
        
        # 记录 X 轴范围
        self._visible_range = (start_idx, end_idx)
        self.update()

    def paint(self, painter, option, widget):
        if not self.trades:
            return

        # 性能优化：只绘制可见范围内的标记
        # 假设 trades 已经按 x_idx 排序 (通常回测结果是按时间排序的)
        # 如果未排序，可能需要全量遍历
        
        # 获取可见范围
        min_x, max_x = 0, float('inf')
        if self._visible_range:
            min_x, max_x = self._visible_range
        elif hasattr(option, 'exposedRect'):
            # 也可以从 exposedRect 获取，但通常由外部 set_visible_range 控制更精确
            min_x = option.exposedRect.left()
            max_x = option.exposedRect.right()

        # 稍微扩大范围以免边缘被裁剪
        min_x -= 1
        max_x += 1

        # 准备画笔 - 使用 width=0 (Cosmetic Pen) 确保总是1像素宽，不受缩放影响
        buy_pen = QPen(QColor(Colors.ACCENT_RED))
        buy_pen.setWidth(0) 
        sell_pen = QPen(QColor(Colors.ACCENT_GREEN))
        sell_pen.setWidth(0)
        
        font = QFont('Arial', 10, QFont.Bold)
        painter.setFont(font)
        
        # 获取变换矩阵以保持文字大小一致 (反向缩放)
        transform = painter.transform()
        y_scale = transform.m22()
        x_scale = transform.m11()
        
        # 避免缩放过小时文字重叠或不可见
        draw_text = x_scale > 2.0 

        # 使用二分查找快速定位可见范围内的交易 (假设 self.trades 已按 x_idx 排序)
        # 提取所有 x_idx 用于查找
        # 注意: 这里假设 self.trades 是有序的。通常回测结果是按时间产生，自然有序。
        # 为了性能，我们不在这里重新提取 keys，而是假设 trades 列表本身按 x_idx 有序
        
        # 找到第一个 >= min_x 的位置
        # 由于 trades 是 dict list, 我们不能直接 bisect。
        # 考虑到性能， paint 频率很高，我们应该在 set_trades 时就分离出 x 轴索引以便查找
        # 或者在这里简单的用 bisect
        
        # 优化: 在 set_trades 预先生成 x_indices 列表
        if not hasattr(self, '_x_indices') or len(self._x_indices) != len(self.trades):
            self._x_indices = [t['x_idx'] for t in self.trades]
            
        start_idx = bisect.bisect_left(self._x_indices, min_x)
        end_idx = bisect.bisect_right(self._x_indices, max_x)
        
        # 只遍历可见部分的切片
        points_to_draw = self.trades[start_idx:end_idx]

        for trade in points_to_draw:
            x = trade['x_idx']
            price = trade['price']
            is_buy = trade['is_buy']
            
            pen = buy_pen if is_buy else sell_pen
            painter.setPen(pen)
            
            # 绘制横线 (x-0.7 到 x+0.3)
            # 长度固定为 1 个 bar 宽
            painter.drawLine(QPointF(x - 0.7, price), QPointF(x + 0.3, price))
            
            # 绘制文字 "B" / "S"
            if draw_text:
                label = "B" if is_buy else "S"
                
                painter.save()
                painter.translate(x - 0.8, price)
                # 反向缩放
                painter.scale(1/x_scale, 1/abs(y_scale) if y_scale != 0 else 1)
                painter.drawText(0, 0, label)
                painter.restore()

    def boundingRect(self):
        if not self.trades:
            return QRectF()

        min_price = min(t['price'] for t in self.trades)
        max_price = max(t['price'] for t in self.trades)
        max_x = max(t['x_idx'] for t in self.trades)
        return QRectF(0, min_price * 0.95, max_x + 1, (max_price - min_price) * 1.1)
    
    def bindPlot(self, plot):
        pass  # 兼容旧接口，不再需要


class DateAxisItem(AxisItem):
    """自定义日期轴"""

    def __init__(self, dates, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dates = dates
        self._month_first_indices = self._find_month_first()

    def _find_month_first(self):
        """找出每月第一个交易日的索引"""
        indices = {}
        for i, dt in enumerate(self.dates):
            if hasattr(dt, 'year'):
                key = (dt.year, dt.month)
            else:
                # 字符串格式 YYYYMMDD
                dt_str = str(dt)[:8]
                key = (dt_str[:4], dt_str[4:6])
            if key not in indices:
                indices[key] = i
        return list(indices.values())

    def tickStrings(self, values, scale, spacing):
        result = []
        for v in values:
            idx = int(v)
            if 0 <= idx < len(self.dates):
                if idx in self._month_first_indices:
                    dt = self.dates[idx]
                    if hasattr(dt, 'strftime'):
                        result.append(dt.strftime('%m-%d'))
                    else:
                        dt_str = str(dt)[:8]
                        result.append(f"{dt_str[4:6]}-{dt_str[6:8]}")
                else:
                    result.append('')
            else:
                result.append('')
        return result

    def tickValues(self, minVal, maxVal, size):
        ticks = []
        for idx in self._month_first_indices:
            if minVal <= idx <= maxVal:
                ticks.append(idx)
        return [(1, ticks)]


class TradeInfoPopup(QFrame):
    """
    交易信息悬浮窗
    """

    closed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.ToolTip | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._init_ui()

    def _init_ui(self):
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {Colors.BG_SECONDARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 6px;
                padding: 8px;
            }}
            QLabel {{
                color: {Colors.TEXT_PRIMARY};
                font-size: 12px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        # 标题行 (买入/卖出 + 关闭按钮)
        title_layout = QHBoxLayout()
        self.title_label = QLabel("买入")
        self.title_label.setStyleSheet(f"font-weight: bold; font-size: 13px; color: {Colors.ACCENT_RED};")
        title_layout.addWidget(self.title_label)
        title_layout.addStretch()

        close_btn = QPushButton("×")
        close_btn.setFixedSize(18, 18)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {Colors.TEXT_MUTED};
                border: none;
                font-size: 16px;
                font-weight: bold;
            }}
            QPushButton:hover {{
                color: {Colors.TEXT_PRIMARY};
            }}
        """)
        close_btn.clicked.connect(self._on_close)
        title_layout.addWidget(close_btn)
        layout.addLayout(title_layout)

        # 信息标签
        self.date_label = QLabel("日期: --")
        self.price_label = QLabel("价格: --")
        self.shares_label = QLabel("数量: --")
        self.amount_label = QLabel("金额: --")

        layout.addWidget(self.date_label)
        layout.addWidget(self.price_label)
        layout.addWidget(self.shares_label)
        layout.addWidget(self.amount_label)

    def show_trade(self, trade: Dict, pos: QPointF):
        """显示交易信息"""
        is_buy = trade.get('is_buy', True)
        self.title_label.setText("买入" if is_buy else "卖出")
        self.title_label.setStyleSheet(
            f"font-weight: bold; font-size: 13px; color: {Colors.ACCENT_RED if is_buy else Colors.ACCENT_GREEN};"
        )

        date_str = trade.get('date', '--')
        if len(str(date_str)) == 8:
            date_str = f"{str(date_str)[:4]}-{str(date_str)[4:6]}-{str(date_str)[6:]}"

        self.date_label.setText(f"日期: {date_str}")
        self.price_label.setText(f"价格: {trade.get('price', 0):.2f}")
        self.shares_label.setText(f"数量: {trade.get('shares', 0):,} 股")
        self.amount_label.setText(f"金额: {trade.get('amount', 0):,.0f} 元")

        # 移动到指定位置
        self.move(int(pos.x()) + 10, int(pos.y()) - 50)
        self.show()

    def _on_close(self):
        self.hide()
        self.closed.emit()


class KLineChart(QWidget):
    """
    K线图组件

    包含三个子图:
    - K线图 (60%)
    - 成交量 (20%)
    - MACD (20%)

    支持:
    - 日线/分钟线双模式切换
    - 双击日线展开当日分钟线

    Signals:
        trade_clicked: 点击买卖标记时发出 (trade_info)
        minute_data_requested: 请求分钟数据时发出 (symbol, date)
    """

    trade_clicked = Signal(dict)
    minute_data_requested = Signal(str, str)  # symbol, date

    def __init__(self, parent=None):
        super().__init__(parent)
        self._data = None
        self._trades = []
        self._symbol = ''  # 当前股票代码
        self._mode = 'daily'  # 'daily' or 'minute'
        self._daily_data = None  # 缓存日线数据
        self._daily_trades = []  # 缓存日线交易
        self._current_date = ''  # 分钟线模式下的日期
        self._pending_minute_date = ''
        self._data_provider = None  # 数据提供者（用于获取分钟数据）
        self._data_provider_price_mode = None
        self._minute_drilldown_enabled = True
        self._init_ui()

    def _init_ui(self):
        if not HAS_PYQTGRAPH:
            layout = QVBoxLayout(self)
            label = QLabel("需要安装 pyqtgraph 才能显示K线图\npip install pyqtgraph")
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet(f"color: {Colors.TEXT_MUTED}; padding: 50px;")
            layout.addWidget(label)
            return

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部标题栏（分钟线模式时显示）
        self._header = QFrame()
        self._header.setStyleSheet(f"""
            QFrame {{
                background-color: {Colors.BG_SECONDARY};
                border-bottom: 1px solid {Colors.BORDER};
                padding: 4px 8px;
            }}
        """)
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(8, 4, 8, 4)
        header_layout.setSpacing(12)

        # 返回按钮
        self._back_btn = QPushButton("← 返回日线")
        self._back_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {Colors.BG_TERTIARY};
                color: {Colors.TEXT_PRIMARY};
                border: 1px solid {Colors.BORDER};
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 12px;
            }}
            QPushButton:hover {{
                background-color: {Colors.ACCENT_BLUE};
                border-color: {Colors.ACCENT_BLUE};
            }}
        """)
        self._back_btn.clicked.connect(self.back_to_daily)
        header_layout.addWidget(self._back_btn)

        # 标题标签
        self._title_label = QLabel()
        self._title_label.setStyleSheet(f"""
            color: {Colors.TEXT_PRIMARY};
            font-size: 13px;
            font-weight: bold;
        """)
        header_layout.addWidget(self._title_label)
        header_layout.addStretch()

        layout.addWidget(self._header)
        self._header.hide()  # 默认隐藏

        # 创建图形布局
        self.graphics_layout = pg.GraphicsLayoutWidget()
        self.graphics_layout.setBackground(Colors.BG_DARK)
        layout.addWidget(self.graphics_layout)

        # 创建三个子图 (暂不添加，等数据到来时创建)
        self.kline_plot = None
        self.volume_plot = None
        self.macd_plot = None

        # K线项
        self.candlestick_item = None
        self.volume_item = None
        self.trade_marker_item = None

        # 悬浮窗
        self.trade_popup = TradeInfoPopup(self)
        self.trade_popup.hide()

        # 十字光标
        self._vLine = None
        self._hLine = None
        self._tooltip = None

    def set_data(self, data: pd.DataFrame, trades: List[Dict] = None, symbol: str = ''):
        """
        设置K线数据

        Args:
            data: DataFrame with columns [trade_date, open, high, low, close, vol]
            trades: 交易记录列表 [{date, direction, price, shares, amount}, ...]
            symbol: 股票代码
        """
        if not HAS_PYQTGRAPH or data is None or data.empty:
            return

        self._data = data.copy()
        self._trades = trades or []
        self._symbol = symbol

        # 日线模式下缓存数据
        if self._mode == 'daily':
            self._daily_data = self._data.copy()
            self._daily_trades = list(self._trades)

        # 确保数据格式正确
        if 'trade_date' in self._data.columns:
            self._data = self._data.sort_values('trade_date')
            dates = self._data['trade_date'].tolist()
        else:
            dates = list(range(len(self._data)))

        # 重置索引
        self._data = self._data.reset_index(drop=True)

        # 计算MACD
        self._calc_macd()

        # 创建图表
        self._create_plots(dates)

        # 绑定交易数据到K线
        self._bindtrades()

    def set_data_provider(self, provider, *, price_mode=None):
        """Set the optional minute provider and its explicit price lane."""
        self._data_provider = provider
        self._data_provider_price_mode = price_mode
        self._minute_drilldown_enabled = provider is not None

    def focus_date(self, trade_date: str, *, window: int = 30) -> bool:
        """Center the daily view on a trade selected in the result ledger."""

        if (
            not HAS_PYQTGRAPH
            or self._data is None
            or self.kline_plot is None
            or 'trade_date' not in self._data.columns
        ):
            return False
        target = str(trade_date).replace('-', '')[:8]
        index = None
        for row, value in enumerate(self._data['trade_date']):
            if str(value).replace('-', '')[:8] == target:
                index = row
                break
        if index is None:
            return False
        radius = max(5, int(window))
        left = max(-0.5, index - radius)
        right = min(len(self._data) - 0.5, index + radius)
        self.kline_plot.setXRange(left, right, padding=0)
        close = float(self._data.iloc[index]['close'])
        if self._vLine is not None:
            self._vLine.setPos(index)
        if self._hLine is not None:
            self._hLine.setPos(close)
        if getattr(self, '_vLine_vol', None) is not None:
            self._vLine_vol.setPos(index)
        if getattr(self, '_vLine_macd', None) is not None:
            self._vLine_macd.setPos(index)
        self._focused_index = index
        return True

    def _calc_macd(self, fast=12, slow=26, signal=9):
        """计算MACD指标"""
        close = self._data['close']

        # EMA
        ema_fast = close.ewm(span=fast, adjust=False).mean()
        ema_slow = close.ewm(span=slow, adjust=False).mean()

        # DIF (MACD line)
        self._data['dif'] = ema_fast - ema_slow

        # DEA (Signal line)
        self._data['dea'] = self._data['dif'].ewm(span=signal, adjust=False).mean()

        # MACD histogram
        self._data['macd'] = (self._data['dif'] - self._data['dea']) * 2

        # 计算昨收价（用于涨跌幅计算）
        self._data['pre_close'] = self._data['close'].shift(1)
        # 第一天用开盘价作为昨收
        self._data.loc[self._data.index[0], 'pre_close'] = self._data.loc[self._data.index[0], 'open']

    def _create_plots(self, dates):
        """创建三个子图"""
        # 清除旧图表
        self.graphics_layout.clear()

        # 为每个子图创建独立的日期轴实例（pyqtgraph要求每个PlotItem有独立的轴）
        date_axis_kline = DateAxisItem(dates, orientation='bottom')
        date_axis_volume = DateAxisItem(dates, orientation='bottom')
        date_axis_macd = DateAxisItem(dates, orientation='bottom')

        # K线图 (60%) - 使用自定义ViewBox实现独立缩放，绑定日期轴
        vb_kline = IndependentScaleViewBox()
        self.kline_plot = self.graphics_layout.addPlot(
            row=0, col=0, viewBox=vb_kline,
            axisItems={'bottom': date_axis_kline}
        )
        self.kline_plot.setLabel('left', '价格')
        self.kline_plot.showGrid(x=True, y=True, alpha=0.15)
        self.kline_plot.getAxis('left').setTextPen(Colors.TEXT_SECONDARY)
        self.kline_plot.setXLink(None)

        # 添加K线
        self.candlestick_item = CandlestickItem()
        self.candlestick_item.set_data(self._data[['open', 'high', 'low', 'close']])
        self.kline_plot.addItem(self.candlestick_item)

        # 添加均线 MA5, MA20（与内置教学示例保持一致）
        x = list(range(len(self._data)))
        close = self._data['close']

        # MA5 - 白色 (min_periods=1 让第1天就开始计算均线)
        ma5 = close.rolling(window=5, min_periods=1).mean()
        self.kline_plot.plot(x, ma5.tolist(), pen=pg.mkPen('#FFFFFF', width=1), name='MA5')

        # MA20 - 黄色 (min_periods=1 让第1天就开始计算均线)
        ma20 = close.rolling(window=20, min_periods=1).mean()
        self.kline_plot.plot(x, ma20.tolist(), pen=pg.mkPen('#FFD700', width=1), name='MA20')

        # 添加均线图例
        self.kline_plot.addLegend(offset=(60, 10))

        # 添加买卖标记
        self.trade_marker_item = TradeMarkerItem()
        self.trade_marker_item.bindPlot(self.kline_plot)  # 绑定以支持文字标签
        self.kline_plot.addItem(self.trade_marker_item)

        # 成交量图 (20%) - 使用自定义ViewBox，绑定日期轴
        self.graphics_layout.nextRow()
        vb_volume = IndependentScaleViewBox()
        self.volume_plot = self.graphics_layout.addPlot(
            row=1, col=0, viewBox=vb_volume,
            axisItems={'bottom': date_axis_volume}
        )
        self.volume_plot.setLabel('left', '成交量')
        self.volume_plot.showGrid(x=True, y=True, alpha=0.15)
        self.volume_plot.getAxis('left').setTextPen(Colors.TEXT_SECONDARY)
        self.volume_plot.setXLink(self.kline_plot)
        
        # 添加成交量
        self.volume_item = VolumeBarItem()

        # 添加成交量柱状图
        self.volume_item = VolumeBarItem()
        self.volume_item.set_data(self._data[['open', 'close', 'vol']])
        self.volume_plot.addItem(self.volume_item)

        # MACD图 (20%) - 使用自定义ViewBox，绑定日期轴
        self.graphics_layout.nextRow()
        vb_macd = IndependentScaleViewBox()
        self.macd_plot = self.graphics_layout.addPlot(
            row=2, col=0, viewBox=vb_macd,
            axisItems={'bottom': date_axis_macd}
        )
        self.macd_plot.setLabel('left', 'MACD')
        self.macd_plot.setLabel('bottom', '日期')
        self.macd_plot.showGrid(x=True, y=True, alpha=0.15)
        self.macd_plot.getAxis('left').setTextPen(Colors.TEXT_SECONDARY)
        self.macd_plot.getAxis('bottom').setTextPen(Colors.TEXT_SECONDARY)
        self.macd_plot.setXLink(self.kline_plot)

        # 绘制MACD
        x = list(range(len(self._data)))
        self.macd_plot.plot(x, self._data['dif'].tolist(), pen=pg.mkPen('#FF9800', width=1), name='DIF')
        self.macd_plot.plot(x, self._data['dea'].tolist(), pen=pg.mkPen('#2196F3', width=1), name='DEA')

        # MACD柱状图 - 使用批量绘制优化
        self.macd_bar_item = MACDBarItem()
        self.macd_bar_item.set_data(self._data['macd'].tolist())
        self.macd_plot.addItem(self.macd_bar_item)

        # 设置高度比例 (6:2:2)
        self.graphics_layout.ci.layout.setRowStretchFactor(0, 6)
        self.graphics_layout.ci.layout.setRowStretchFactor(1, 2)
        self.graphics_layout.ci.layout.setRowStretchFactor(2, 2)

        # 添加十字光标
        self._setup_crosshair()

        # 添加视口变化监听（视口裁剪优化）
        self._setup_viewport_culling()

        # 自动缩放
        self.kline_plot.enableAutoRange()
        self.volume_plot.enableAutoRange()
        self.macd_plot.enableAutoRange()

    def _bindtrades(self):
        """绑定交易数据到标记"""
        if not self._trades or self._data is None:
            return

        dates = self._data['trade_date'].tolist() if 'trade_date' in self._data.columns else []

        if self._mode == 'daily':
            # 日线模式：聚合同一天的交易，显示平均价格
            trade_markers = self._aggregate_daily_trades(dates)
        else:
            # 分钟线模式：显示具体位置
            trade_markers = self._get_minute_trades(dates)

        if trade_markers and self.trade_marker_item:
            self.trade_marker_item.set_trades(trade_markers)

    def _aggregate_daily_trades(self, dates: list) -> List[Dict]:
        """
        聚合日线交易（同一天多笔交易显示平均价）

        Args:
            dates: K线日期列表

        Returns:
            聚合后的交易标记列表
        """
        # 按日期+方向分组
        daily_trades = {}
        for trade in self._trades:
            trade_date = str(trade.get('time', trade.get('date', '')))
            date_key = trade_date.replace('-', '')[:8]  # YYYYMMDD
            direction = trade.get('direction', '')

            key = (date_key, direction)
            if key not in daily_trades:
                daily_trades[key] = {
                    'total_amount': 0,
                    'total_shares': 0,
                    'trades': []
                }

            shares = trade.get('shares', 0)
            price = trade.get('price', 0)
            amount = trade.get('amount', shares * price)

            daily_trades[key]['total_amount'] += amount
            daily_trades[key]['total_shares'] += shares
            daily_trades[key]['trades'].append(trade)

        # 转换为标记列表
        trade_markers = []
        for (date_key, direction), data in daily_trades.items():
            # 计算平均价格
            if data['total_shares'] > 0:
                avg_price = data['total_amount'] / data['total_shares']
            else:
                avg_price = 0

            # 查找日期对应的索引
            x_idx = None
            for i, d in enumerate(dates):
                d_str = str(d).replace('-', '')[:8]
                if d_str == date_key:
                    x_idx = i
                    break

            if x_idx is not None:
                trade_markers.append({
                    'x_idx': x_idx,
                    'price': avg_price,
                    'is_buy': direction == 'BUY',
                    'date': date_key,
                    'shares': data['total_shares'],
                    'amount': data['total_amount'],
                    'trade_count': len(data['trades']),  # 交易笔数
                })

        return trade_markers

    def _get_minute_trades(self, dates: list) -> List[Dict]:
        """
        获取分钟线交易标记（显示具体时间位置）

        Args:
            dates: K线时间列表

        Returns:
            交易标记列表
        """
        trade_markers = []

        for trade in self._trades:
            trade_time = str(trade.get('time', trade.get('date', '')))
            direction = trade.get('direction', '')

            # 查找时间对应的索引
            x_idx = None
            for i, d in enumerate(dates):
                d_str = str(d).replace('-', '').replace(':', '').replace(' ', '')
                t_str = trade_time.replace('-', '').replace(':', '').replace(' ', '')

                # 尝试精确匹配或前缀匹配
                if d_str == t_str or d_str.startswith(t_str[:12]) or t_str.startswith(d_str[:12]):
                    x_idx = i
                    break

            if x_idx is not None:
                trade_markers.append({
                    'x_idx': x_idx,
                    'price': trade.get('price', 0),
                    'is_buy': direction == 'BUY',
                    'date': trade_time,
                    'shares': trade.get('shares', 0),
                    'amount': trade.get('amount', 0),
                })

        return trade_markers

    def _setup_crosshair(self):
        """设置十字光标"""
        # K线图十字光标
        self._vLine = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#888888', width=1))
        self._hLine = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen('#888888', width=1))
        self.kline_plot.addItem(self._vLine, ignoreBounds=True)
        self.kline_plot.addItem(self._hLine, ignoreBounds=True)

        # 成交量图垂直光标线
        self._vLine_vol = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#888888', width=1))
        self.volume_plot.addItem(self._vLine_vol, ignoreBounds=True)

        # MACD图垂直光标线
        self._vLine_macd = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#888888', width=1))
        self.macd_plot.addItem(self._vLine_macd, ignoreBounds=True)

        # K线图提示文本
        self._tooltip = pg.TextItem(anchor=(0, 1), color=Colors.TEXT_PRIMARY)
        self._tooltip.setFont(QFont('Microsoft YaHei', 9))
        self.kline_plot.addItem(self._tooltip)

        # 成交量提示文本
        self._tooltip_vol = pg.TextItem(anchor=(0, 0), color=Colors.TEXT_PRIMARY)
        self._tooltip_vol.setFont(QFont('Microsoft YaHei', 9))
        self.volume_plot.addItem(self._tooltip_vol)

        # MACD提示文本
        self._tooltip_macd = pg.TextItem(anchor=(0, 0), color=Colors.TEXT_PRIMARY)
        self._tooltip_macd.setFont(QFont('Microsoft YaHei', 9))
        self.macd_plot.addItem(self._tooltip_macd)

        # 连接鼠标移动
        self.kline_plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        self.kline_plot.scene().sigMouseClicked.connect(self._on_mouse_clicked)

        # 连接双击事件（用于展开分钟线）
        self.graphics_layout.scene().sigMouseClicked.connect(self._on_mouse_double_clicked)

    def _setup_viewport_culling(self):
        """设置视口裁剪优化"""
        # 监听视口X轴范围变化
        self.kline_plot.sigXRangeChanged.connect(self._on_range_changed)
        # 初始化时更新一次可见范围
        self._update_visible_range()

    def _on_range_changed(self, vb, x_range):
        """视口范围变化时更新可见数据"""
        self._update_visible_range()

    def _update_visible_range(self):
        """更新所有图形项的可见范围"""
        if self._data is None or self.kline_plot is None:
            return

        # 获取当前X轴范围
        x_range = self.kline_plot.viewRange()[0]
        x_min = int(x_range[0])
        x_max = int(x_range[1])

        # 增加缓冲区（左右各多绘制50根，避免滚动时闪烁）
        buffer = 50
        start_idx = max(0, x_min - buffer)
        end_idx = min(len(self._data), x_max + buffer)

        # 更新各个图形项的可见范围
        if self.candlestick_item:
            self.candlestick_item.set_visible_range(start_idx, end_idx)
        if self.volume_item:
            self.volume_item.set_visible_range(start_idx, end_idx)
        if hasattr(self, 'macd_bar_item') and self.macd_bar_item:
            self.macd_bar_item.set_visible_range(start_idx, end_idx)
        if self.trade_marker_item:
            self.trade_marker_item.set_visible_range(start_idx, end_idx)

    def _on_mouse_moved(self, pos):
        """处理鼠标移动"""
        if self._data is None or not self.kline_plot:
            return

        if self.kline_plot.sceneBoundingRect().contains(pos):
            mouse_point = self.kline_plot.vb.mapSceneToView(pos)
            x_idx = int(round(mouse_point.x()))

            if 0 <= x_idx < len(self._data):
                row = self._data.iloc[x_idx]

                # 更新K线图十字光标
                self._vLine.setPos(x_idx)
                self._hLine.setPos(row['close'])

                # 更新成交量图和MACD图的垂直光标线
                self._vLine_vol.setPos(x_idx)
                self._vLine_macd.setPos(x_idx)

                # 格式化日期
                date_str = str(row.get('trade_date', x_idx))
                if len(date_str) == 8:
                    date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"

                # 计算涨跌幅（与昨收比较）
                pre_close = row.get('pre_close', row['open'])
                change = row['close'] - pre_close
                change_pct = (change / pre_close * 100) if pre_close > 0 else 0

                # K线图提示文本（包含涨跌幅）
                tooltip_text = (
                    f"日期: {date_str}\n"
                    f"开: {row['open']:.2f}  高: {row['high']:.2f}\n"
                    f"低: {row['low']:.2f}  收: {row['close']:.2f}\n"
                    f"涨跌: {change:+.2f} ({change_pct:+.2f}%)"
                )
                self._tooltip.setText(tooltip_text)

                # K线图提示位置
                view_range = self.kline_plot.viewRange()
                y_range = view_range[1][1] - view_range[1][0]
                self._tooltip.setPos(x_idx + 1, row['high'] + y_range * 0.02)

                # 成交量提示文本
                vol = row['vol']
                vol_text = f"量: {vol:,.0f}"
                self._tooltip_vol.setText(vol_text)
                vol_range = self.volume_plot.viewRange()
                self._tooltip_vol.setPos(x_idx + 1, vol_range[1][1] * 0.9)

                # MACD提示文本
                dif = row.get('dif', 0)
                dea = row.get('dea', 0)
                macd = row.get('macd', 0)
                macd_text = f"DIF: {dif:.3f}  DEA: {dea:.3f}  MACD: {macd:.3f}"
                self._tooltip_macd.setText(macd_text)
                macd_range = self.macd_plot.viewRange()
                self._tooltip_macd.setPos(x_idx + 1, macd_range[1][1] * 0.9)

    def _on_mouse_clicked(self, event):
        """处理鼠标点击 - 检测买卖标记"""
        if not self.trade_marker_item or not self.trade_marker_item.trades:
            return

        pos = event.scenePos()
        if self.kline_plot.sceneBoundingRect().contains(pos):
            mouse_point = self.kline_plot.vb.mapSceneToView(pos)
            x_idx = int(round(mouse_point.x()))

            # 检查是否点击到买卖标记
            for trade in self.trade_marker_item.trades:
                if abs(trade['x_idx'] - x_idx) < 1:
                    # 点击到标记，显示悬浮窗
                    screen_pos = event.screenPos()
                    self.trade_popup.show_trade(trade, screen_pos)
                    self.trade_clicked.emit(trade)
                    return

        # 点击空白处关闭悬浮窗
        self.trade_popup.hide()

    def clear(self):
        """清空图表"""
        if HAS_PYQTGRAPH:
            self.graphics_layout.clear()
            self._data = None
            self._trades = []

    def _on_mouse_double_clicked(self, event):
        """处理双击事件 - 展开分钟线"""
        # 只在日线模式下响应双击
        if self._mode != 'daily':
            return

        # 检查是否是双击
        if not event.double():
            return

        pos = event.scenePos()
        if self.kline_plot and self.kline_plot.sceneBoundingRect().contains(pos):
            mouse_point = self.kline_plot.vb.mapSceneToView(pos)
            x_idx = int(round(mouse_point.x()))

            if 0 <= x_idx < len(self._data):
                trade_date = str(self._data.iloc[x_idx]['trade_date'])
                self.expand_to_minute(trade_date)

    def expand_to_minute(self, trade_date: str):
        """
        展开到指定日期的分钟线

        Args:
            trade_date: 日期字符串 (YYYYMMDD)
        """
        if not self._symbol:
            return
        if not self._minute_drilldown_enabled:
            self._pending_minute_date = ''
            return
        self._pending_minute_date = trade_date

        # 尝试从数据提供者获取分钟数据
        if self._data_provider:
            try:
                kwargs = {}
                if self._data_provider_price_mode is not None:
                    kwargs['price_mode'] = self._data_provider_price_mode
                minute_df = self._data_provider.get_minute(
                    self._symbol, trade_date, **kwargs
                )
                if minute_df is not None and not minute_df.empty:
                    # 筛选当日交易
                    day_trades = self._get_trades_for_date(trade_date)
                    self._activate_minute_mode(trade_date)
                    self._show_minute_chart(minute_df, day_trades)
                    return
            except Exception as e:
                print(f"获取分钟数据失败: {e}")

        # 如果没有分钟数据，发出请求信号
        self.minute_data_requested.emit(self._symbol, trade_date)

    def _activate_minute_mode(self, trade_date: str) -> None:
        """Commit the UI state only once real minute data is available."""

        self._mode = 'minute'
        self._current_date = trade_date
        self._pending_minute_date = ''
        date_display = trade_date
        if len(trade_date) == 8:
            date_display = (
                f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:]}"
            )
        self._title_label.setText(
            f"{self._symbol} {date_display} 分钟线"
        )
        self._header.show()

    def _get_trades_for_date(self, trade_date: str) -> List[Dict]:
        """获取指定日期的交易记录"""
        day_trades = []
        for t in self._daily_trades:
            t_date = str(t.get('time', t.get('date', '')))
            if t_date.startswith(trade_date):
                day_trades.append(t)
        return day_trades

    def _show_minute_chart(self, minute_df: pd.DataFrame, trades: List[Dict]):
        """显示分钟线图表"""
        # 更新数据
        self._data = minute_df.copy()
        self._trades = trades

        # 确保数据格式正确
        if 'trade_time' in self._data.columns:
            self._data = self._data.sort_values('trade_time')
            # 对于分钟线，trade_date 使用 trade_time
            if 'trade_date' not in self._data.columns:
                self._data['trade_date'] = self._data['trade_time']
            dates = self._data['trade_time'].tolist()
        elif 'trade_date' in self._data.columns:
            self._data = self._data.sort_values('trade_date')
            dates = self._data['trade_date'].tolist()
        else:
            dates = list(range(len(self._data)))

        self._data = self._data.reset_index(drop=True)

        # 计算MACD
        self._calc_macd()

        # 创建图表
        self._create_plots(dates)

        # 绑定交易数据
        self._bindtrades()

    def set_minute_data(self, minute_df: pd.DataFrame, trades: List[Dict] = None):
        """
        设置分钟数据（外部调用，响应 minute_data_requested 信号后）

        Args:
            minute_df: 分钟K线数据
            trades: 当日交易记录
        """
        if minute_df is None or minute_df.empty:
            self._pending_minute_date = ''
            return

        trade_date = self._pending_minute_date or self._current_date
        if not trade_date:
            raise RuntimeError(
                "minute data arrived without a pending drill-down date"
            )
        self._activate_minute_mode(trade_date)
        trades = trades or self._get_trades_for_date(trade_date)
        self._show_minute_chart(minute_df, trades)

    def back_to_daily(self):
        """返回日线模式"""
        if self._mode != 'minute':
            return

        self._mode = 'daily'
        self._current_date = ''
        self._pending_minute_date = ''
        self._header.hide()

        # 恢复日线数据
        if self._daily_data is not None:
            self._data = self._daily_data.copy()
            self._trades = list(self._daily_trades)

            # 确保数据格式正确
            if 'trade_date' in self._data.columns:
                dates = self._data['trade_date'].tolist()
            else:
                dates = list(range(len(self._data)))

            # 重新计算MACD
            self._calc_macd()

            # 创建图表
            self._create_plots(dates)

            # 绑定交易数据
            self._bindtrades()

    @property
    def mode(self) -> str:
        """当前模式: 'daily' 或 'minute'"""
        return self._mode
