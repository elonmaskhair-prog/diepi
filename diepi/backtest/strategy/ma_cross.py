"""Single-symbol MA5/MA20 strict crossover strategy."""

from .base import Strategy, BarData
from ...examples.ma_cross_strategy import moving_average_cross


class MaCrossStrategy(Strategy):
    """
    MA5/MA20 严格交叉策略。

    参数:
        fast_period: 快线周期，默认 5
        slow_period: 慢线周期，默认 20
        target_weight: 金叉后的目标仓位，默认 0.95
    """

    def on_before_market_open(self, ctx) -> None:
        """盘前下单"""
        symbol = ctx.current_symbol
        if not symbol:
            return

        fast_period = self.get_param('fast_period', 5)
        slow_period = self.get_param('slow_period', 20)
        target_weight = self.get_param('target_weight', 0.95)

        df = ctx.get_daily(symbol, days=slow_period + 1)
        if df is None or len(df) < slow_period + 1:
            return

        crossed_up, crossed_down = moving_average_cross(
            df['close'], fast_period, slow_period
        )

        pos = ctx.get_position(symbol)
        has_position = pos is not None and pos.shares > 0

        if has_position:
            if crossed_down:
                ctx.sell_at_open(symbol, percent=1.0)
        else:
            if crossed_up:
                ctx.buy_at_open(symbol, percent=target_weight)

    def on_day(self, ctx, bar: BarData) -> None:
        """每日回调 - 买卖已在盘前完成"""
        pass
