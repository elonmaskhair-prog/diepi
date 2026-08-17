"""
缠论量价背离策略 (日线买入 + 分钟卖出)

策略逻辑:
- 日线买入信号: 底背离 (价格创新低，但成交量萎缩)
- 分钟卖出信号: 顶背离 (价格创新高，但成交量萎缩) 或止损
- T+1 限制: 当日买入的股票次日才能卖出

使用:
    from diepi.backtest.strategy import PortfolioStrategy
    from diepi.backtest.engine import PortfolioEngine

    engine = PortfolioEngine(..., freq='minute')
    result = engine.run(ChanlunDivergenceStrategy())
"""

import pandas as pd
import numpy as np
from diepi.backtest.strategy import PortfolioStrategy


class ChanlunDivergenceStrategy(PortfolioStrategy):
    """缠论量价背离策略"""

    # ==================== 参数 ====================
    DAILY_LOOKBACK = 10       # 日线回看天数
    VOL_SHRINK_RATIO = 0.7    # 量萎缩比例
    BUY_PERCENT = 0.20        # 每次买入仓位

    MIN_PROFIT_PCT = 0.02     # 最低止盈 2%
    MAX_PROFIT_PCT = 0.05     # 最高止盈 5%
    MIN_VOL_SHRINK = 0.6      # 分钟量萎缩阈值
    STOP_LOSS_PCT = -0.05     # 止损 -5%

    def on_init(self, ctx):
        """初始化"""
        print("=" * 60)
        print("缠论量价背离策略 (日线买入 + 分钟卖出)")
        print("=" * 60)
        print(f"日线回看: {self.DAILY_LOOKBACK} 天")
        print(f"量萎缩阈值: {self.VOL_SHRINK_RATIO}")
        print(f"止盈: {self.MIN_PROFIT_PCT*100}% ~ {self.MAX_PROFIT_PCT*100}%")
        print(f"止损: {self.STOP_LOSS_PCT*100}%")
        print("=" * 60)

        self.daily_signals = {}   # {symbol: bool}
        self.buy_prices = {}      # {symbol: float}
        self.minute_highs = {}    # {symbol: float}
        self.minute_vols = {}     # {symbol: list}

    def on_before_market_open(self, ctx):
        """
        盘前: 检查日线底背离信号
        返回需要加载分钟数据的股票列表
        """
        # 获取股票池 (注意: 在盘前用 get_stock_pool, 不是 get_active_symbols)
        symbols = ctx.get_stock_pool()

        for symbol in symbols:
            # 获取日线数据
            daily = ctx.get_daily(symbol, days=self.DAILY_LOOKBACK + 5)

            if daily is None or daily.empty:
                continue

            # 检测日线底背离
            has_signal = self._detect_daily_divergence(daily)
            self.daily_signals[symbol] = has_signal

            if has_signal:
                print(f"[{ctx.current_date}] {symbol} 日线底背离信号")

        # 返回需要关注的股票: 有信号的 + 已持仓的
        positions = ctx.get_positions() or {}
        held = set(s for s, p in positions.items() if p and p.shares > 0)
        signals = set(s for s, v in self.daily_signals.items() if v)

        # sorted 保证确定性（set 迭代序随进程哈希种子变化，现金竞争下结果不可复现）
        return sorted(held | signals) if (held | signals) else symbols[:3]

    def on_minute(self, ctx, bars):
        """
        分钟回调

        逻辑:
        1. 开盘时段 (首根K线): 有日线信号则买入
        2. 盘中: 检查止损/止盈/顶背离
        """
        current_time = ctx.current_time
        trade_date = ctx.current_date

        # 使用 is_first_bar 判断开盘时段 (适配所有频率: 1min/5min/15min/30min/60min)
        is_open = ctx.is_first_bar

        for symbol in bars.symbols():
            bar = bars.get(symbol)
            if bar is None:
                continue

            pos = ctx.get_position(symbol)
            has_position = pos is not None and pos.shares > 0

            # === 开盘买入 ===
            if is_open and self.daily_signals.get(symbol, False):
                if not has_position:
                    ctx.buy_at_market(symbol, percent=self.BUY_PERCENT, current_price=bar.close)
                    self.buy_prices[symbol] = bar.close
                    self.minute_highs[symbol] = bar.high
                    self.minute_vols[symbol] = [bar.vol]
                    self.daily_signals[symbol] = False
                    print(f"[{trade_date} {current_time}] 买入 {symbol} @ {bar.close:.2f}")
                    continue

            # === 持仓管理 ===
            if has_position:
                # T+1 检查: 只有 available_shares > 0 才能卖出
                if pos.available_shares <= 0:
                    continue

                buy_price = self.buy_prices.get(symbol, pos.avg_cost)

                # 更新分钟数据
                if symbol not in self.minute_highs:
                    self.minute_highs[symbol] = bar.high
                    self.minute_vols[symbol] = []

                self.minute_highs[symbol] = max(self.minute_highs[symbol], bar.high)
                self.minute_vols[symbol].append(bar.vol)

                # 保持最近30分钟
                if len(self.minute_vols[symbol]) > 30:
                    self.minute_vols[symbol] = self.minute_vols[symbol][-30:]

                # 盈亏
                profit_pct = (bar.close - buy_price) / buy_price if buy_price > 0 else 0

                # 止损
                if profit_pct <= self.STOP_LOSS_PCT:
                    ctx.sell_at_market(symbol, percent=1.0)
                    print(f"[{trade_date} {current_time}] 止损 {symbol} @ {bar.close:.2f} ({profit_pct*100:.1f}%)")
                    self._clean_state(symbol)
                    continue

                # 顶背离/止盈
                if self._detect_minute_divergence(symbol, bar.close, buy_price):
                    ctx.sell_at_market(symbol, percent=1.0)
                    print(f"[{trade_date} {current_time}] 卖出 {symbol} @ {bar.close:.2f} ({profit_pct*100:.1f}%)")
                    self._clean_state(symbol)

    def on_after_market_close(self, ctx):
        """盘后: 重置分钟数据"""
        self.minute_highs = {}
        self.minute_vols = {}

    # ==================== 辅助方法 ====================

    def _detect_daily_divergence(self, daily_df: pd.DataFrame) -> bool:
        """检测日线底背离"""
        if daily_df is None or len(daily_df) < self.DAILY_LOOKBACK:
            return False

        recent = daily_df.tail(self.DAILY_LOOKBACK)

        today_close = recent['close'].iloc[-1]
        vol_col = 'vol' if 'vol' in recent.columns else 'volume'
        today_vol = recent[vol_col].iloc[-1]

        prev_low = recent['close'].iloc[:-1].min()
        avg_vol = recent[vol_col].iloc[:-1].mean()

        # 底背离: 价格新低 + 量萎缩
        return today_close <= prev_low and today_vol < avg_vol * self.VOL_SHRINK_RATIO

    def _detect_minute_divergence(self, symbol: str, current_price: float, buy_price: float) -> bool:
        """检测分钟顶背离"""
        vols = self.minute_vols.get(symbol, [])
        if len(vols) < 5:
            return False

        profit_pct = (current_price - buy_price) / buy_price if buy_price > 0 else 0

        # 未达到最低盈利
        if profit_pct < self.MIN_PROFIT_PCT:
            return False

        # 达到最高止盈，直接卖出
        if profit_pct >= self.MAX_PROFIT_PCT:
            return True

        # 顶背离: 价格接近新高 + 量萎缩
        minute_high = self.minute_highs.get(symbol, current_price)
        near_high = current_price >= minute_high * 0.99

        current_vol = vols[-1]
        avg_vol = np.mean(vols[:-1]) if len(vols) > 1 else current_vol
        vol_shrink = current_vol < avg_vol * self.MIN_VOL_SHRINK

        return near_high and vol_shrink

    def _is_open_period(self, current_time) -> bool:
        """判断是否是开盘时段"""
        if hasattr(current_time, 'hour'):
            return current_time.hour == 9 and current_time.minute <= 35
        time_str = str(current_time)
        return any(t in time_str for t in ['09:30', '09:31', '09:32', '09:33', '09:34', '09:35'])

    def _clean_state(self, symbol: str):
        """清理状态"""
        self.buy_prices.pop(symbol, None)
        self.minute_highs.pop(symbol, None)
        self.minute_vols.pop(symbol, None)
        self.daily_signals.pop(symbol, None)
