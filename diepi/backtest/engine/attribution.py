"""
收益归因分析

按时间维度和股票维度分析回测盈亏
"""

from typing import Dict, List, Tuple
from collections import defaultdict
import pandas as pd


class AttributionAnalyzer:
    """
    收益归因分析器

    支持三个维度:
    - 按年份
    - 按月份
    - 按股票
    """

    def __init__(self, trades: List[Dict], initial_capital: float = 1000000):
        """
        初始化

        Args:
            trades: 交易记录列表
            initial_capital: 初始资金
        """
        self.trades = trades
        self.initial_capital = initial_capital

    def by_year(self) -> pd.DataFrame:
        """
        按年份统计盈亏

        Returns:
            DataFrame: columns=[year, profit, profit_pct, trade_count]
        """
        year_stats = defaultdict(lambda: {'profit': 0, 'trade_count': 0})

        for trade in self.trades:
            if trade.get('direction') != 'SELL':
                continue

            # 提取年份
            time_str = str(trade.get('time', trade.get('date', '')))
            if len(time_str) >= 4:
                year = time_str[:4]
            else:
                continue

            year_stats[year]['profit'] += trade.get('profit', 0)
            year_stats[year]['trade_count'] += 1

        # 转换为DataFrame
        data = []
        for year in sorted(year_stats.keys()):
            stats = year_stats[year]
            profit = stats['profit']
            # 盈亏比例以初始资金为基准
            profit_pct = profit / self.initial_capital if self.initial_capital > 0 else 0
            data.append({
                'year': year,
                'profit': profit,
                'profit_pct': profit_pct,
                'trade_count': stats['trade_count'],
            })

        return pd.DataFrame(data)

    def by_month(self) -> pd.DataFrame:
        """
        按月份统计盈亏

        Returns:
            DataFrame: columns=[month, profit, profit_pct, trade_count]
        """
        month_stats = defaultdict(lambda: {'profit': 0, 'trade_count': 0})

        for trade in self.trades:
            if trade.get('direction') != 'SELL':
                continue

            # 提取年月
            time_str = str(trade.get('time', trade.get('date', '')))
            if len(time_str) >= 6:
                # 格式化为 YYYY-MM
                month = f"{time_str[:4]}-{time_str[4:6]}"
            else:
                continue

            month_stats[month]['profit'] += trade.get('profit', 0)
            month_stats[month]['trade_count'] += 1

        # 转换为DataFrame
        data = []
        for month in sorted(month_stats.keys()):
            stats = month_stats[month]
            profit = stats['profit']
            profit_pct = profit / self.initial_capital if self.initial_capital > 0 else 0
            data.append({
                'month': month,
                'profit': profit,
                'profit_pct': profit_pct,
                'trade_count': stats['trade_count'],
            })

        return pd.DataFrame(data)

    def by_stock(self, stock_names: Dict[str, str] = None) -> pd.DataFrame:
        """
        按股票统计盈亏

        Args:
            stock_names: 股票名称映射 {symbol: name}

        Returns:
            DataFrame: columns=[symbol, name, profit, profit_pct, trade_count, win_rate]
        """
        stock_names = stock_names or {}
        stock_stats = defaultdict(lambda: {
            'profit': 0,
            'trade_count': 0,
            'win_count': 0,
            'buy_amount': 0,
        })

        # 统计买入金额
        for trade in self.trades:
            symbol = trade.get('symbol', '')
            if not symbol:
                continue

            if trade.get('direction') == 'BUY':
                stock_stats[symbol]['buy_amount'] += trade.get('amount', 0)
            elif trade.get('direction') == 'SELL':
                profit = trade.get('profit', 0)
                stock_stats[symbol]['profit'] += profit
                stock_stats[symbol]['trade_count'] += 1
                if profit > 0:
                    stock_stats[symbol]['win_count'] += 1

        # 转换为DataFrame
        data = []
        for symbol in stock_stats:
            stats = stock_stats[symbol]
            profit = stats['profit']
            trade_count = stats['trade_count']
            buy_amount = stats['buy_amount']

            # 盈亏比例基于买入金额
            profit_pct = profit / buy_amount if buy_amount > 0 else 0
            win_rate = (
                stats['win_count'] / trade_count
                if trade_count > 0 else None
            )

            data.append({
                'symbol': symbol,
                'name': stock_names.get(symbol, ''),
                'profit': profit,
                'profit_pct': profit_pct,
                'trade_count': trade_count,
                'win_rate': win_rate,
            })

        # 按盈利排序
        df = pd.DataFrame(data)
        if not df.empty:
            df = df.sort_values('profit', ascending=False)

        return df

    def summary(self) -> Dict:
        """
        获取汇总统计

        Returns:
            汇总字典
        """
        total_profit = 0
        total_trades = 0
        win_trades = 0

        for trade in self.trades:
            if trade.get('direction') != 'SELL':
                continue

            profit = trade.get('profit', 0)
            total_profit += profit
            total_trades += 1
            if profit > 0:
                win_trades += 1

        return {
            'total_profit': total_profit,
            'total_profit_pct': total_profit / self.initial_capital if self.initial_capital > 0 else 0,
            'total_trades': total_trades,
            'win_trades': win_trades,
            'win_rate': (
                win_trades / total_trades if total_trades > 0 else None
            ),
        }


def calculate_attribution(trades: List[Dict], initial_capital: float = 1000000,
                          stock_names: Dict[str, str] = None) -> Dict[str, pd.DataFrame]:
    """
    计算收益归因

    Args:
        trades: 交易记录列表
        initial_capital: 初始资金
        stock_names: 股票名称映射

    Returns:
        字典包含三个DataFrame: by_year, by_month, by_stock
    """
    analyzer = AttributionAnalyzer(trades, initial_capital)

    return {
        'by_year': analyzer.by_year(),
        'by_month': analyzer.by_month(),
        'by_stock': analyzer.by_stock(stock_names),
        'summary': analyzer.summary(),
    }
