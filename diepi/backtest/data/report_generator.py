"""
HTML 回测报告生成器

生成可在浏览器中查看的独立HTML报告
"""

import os
from datetime import datetime
from typing import Dict, List
import html
import pandas as pd

from ..engine.portfolio_engine import PortfolioResult


class ReportGenerator:
    """
    HTML 报告生成器

    生成包含以下内容的报告:
    1. 回测摘要（统计指标）
    2. 资产曲线图（SVG）
    3. 收益归因（按月、按股票）
    4. 策略代码（折叠显示）
    """

    @classmethod
    def generate(cls, folder_path: str, result: PortfolioResult, config: dict, code: str):
        """
        生成 HTML 报告

        Args:
            folder_path: 保存目录
            result: 回测结果
            config: 回测配置
            code: 策略代码
        """
        html_content = cls._build_html(result, config, code)

        with open(os.path.join(folder_path, "回测报告.html"), "w", encoding="utf-8") as f:
            f.write(html_content)

    @classmethod
    def _build_html(cls, result: PortfolioResult, config: dict, code: str) -> str:
        """构建 HTML 内容"""

        # 格式化日期
        start_date = cls._format_date(result.start_date)
        end_date = cls._format_date(result.end_date)

        # 收益颜色
        return_color = "#4CAF50" if result.total_return >= 0 else "#F44336"
        annual_color = "#4CAF50" if result.annual_return >= 0 else "#F44336"
        sharpe = result.sharpe_ratio
        sharpe_text = "N/A" if sharpe is None else f"{sharpe:.3f}"
        sharpe_class = (
            "neutral" if sharpe is None
            else "positive" if sharpe >= 1
            else "negative" if sharpe < 0
            else "neutral"
        )
        contract = getattr(result, "result_contract", None)
        result_status = (
            "LEGACY_UNCLASSIFIED"
            if contract is None else contract.status.value
        )
        status_class = "positive" if result_status == "SUCCESS" else "negative"

        # 生成资产曲线 SVG
        chart_svg = cls._generate_chart_svg(result)

        # 生成按月归因表格
        monthly_table = cls._generate_monthly_table(result)

        # 生成按股票归因表格
        stock_table = cls._generate_stock_table(result)

        # 转义代码
        escaped_code = html.escape(code)

        html_template = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>回测报告 - {start_date} ~ {end_date}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Microsoft YaHei", sans-serif;
            background: #1a1a2e;
            color: #e0e0e0;
            line-height: 1.6;
            padding: 20px;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        h1 {{
            text-align: center;
            color: #fff;
            margin-bottom: 10px;
            font-size: 24px;
        }}
        .subtitle {{
            text-align: center;
            color: #888;
            margin-bottom: 30px;
            font-size: 14px;
        }}
        .section {{
            background: #16213e;
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 20px;
        }}
        .section-title {{
            font-size: 16px;
            font-weight: 600;
            color: #4fc3f7;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 1px solid #2a3f5f;
        }}
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 15px;
        }}
        .stat-item {{
            background: #1a1a2e;
            padding: 15px;
            border-radius: 6px;
            text-align: center;
        }}
        .stat-label {{
            font-size: 12px;
            color: #888;
            margin-bottom: 5px;
        }}
        .stat-value {{
            font-size: 20px;
            font-weight: 600;
        }}
        .positive {{ color: #4CAF50; }}
        .negative {{ color: #F44336; }}
        .neutral {{ color: #fff; }}
        .chart-container {{
            width: 100%;
            overflow-x: auto;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        th, td {{
            padding: 10px 12px;
            text-align: left;
            border-bottom: 1px solid #2a3f5f;
        }}
        th {{
            background: #1a1a2e;
            color: #4fc3f7;
            font-weight: 600;
        }}
        tr:hover {{
            background: #1a1a2e;
        }}
        .code-section {{
            margin-top: 20px;
        }}
        .code-toggle {{
            background: #2a3f5f;
            color: #fff;
            border: none;
            padding: 10px 20px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 14px;
        }}
        .code-toggle:hover {{
            background: #3a4f6f;
        }}
        .code-content {{
            display: none;
            margin-top: 15px;
            background: #0d1117;
            border-radius: 6px;
            padding: 15px;
            overflow-x: auto;
        }}
        .code-content.show {{
            display: block;
        }}
        pre {{
            font-family: "Cascadia Code", "Consolas", monospace;
            font-size: 13px;
            line-height: 1.5;
            color: #c9d1d9;
            white-space: pre-wrap;
            word-wrap: break-word;
        }}
        .footer {{
            text-align: center;
            color: #666;
            font-size: 12px;
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #2a3f5f;
        }}
        @media (max-width: 768px) {{
            .stats-grid {{
                grid-template-columns: repeat(2, 1fr);
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>回测报告</h1>
        <p class="subtitle">回测区间: {start_date} ~ {end_date} | 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>

        <!-- 回测摘要 -->
        <div class="section">
            <div class="section-title">回测摘要</div>
            <div class="stats-grid">
                <div class="stat-item">
                    <div class="stat-label">初始资金</div>
                    <div class="stat-value neutral">{result.initial_cash:,.0f}</div>
                </div>
                <div class="stat-item">
                    <div class="stat-label">最终资产</div>
                    <div class="stat-value neutral">{result.final_value:,.0f}</div>
                </div>
                <div class="stat-item">
                    <div class="stat-label">总收益率</div>
                    <div class="stat-value" style="color:{return_color}">{result.total_return*100:+.2f}%</div>
                </div>
                <div class="stat-item">
                    <div class="stat-label">年化收益</div>
                    <div class="stat-value" style="color:{annual_color}">{result.annual_return*100:+.2f}%</div>
                </div>
                <div class="stat-item">
                    <div class="stat-label">最大回撤</div>
                    <div class="stat-value negative">{result.max_drawdown*100:.2f}%</div>
                </div>
                <div class="stat-item">
                    <div class="stat-label">夏普比率</div>
                    <div class="stat-value {sharpe_class}">{sharpe_text}</div>
                </div>
                <div class="stat-item">
                    <div class="stat-label">交易次数</div>
                    <div class="stat-value neutral">{result.trade_count}</div>
                </div>
                <div class="stat-item">
                    <div class="stat-label">基准收益</div>
                    <div class="stat-value {"positive" if result.benchmark_return >= 0 else "negative"}">{result.benchmark_return*100:+.2f}%</div>
                </div>
                <div class="stat-item">
                    <div class="stat-label">结果状态</div>
                    <div class="stat-value {status_class}">{result_status}</div>
                </div>
            </div>
        </div>

        <!-- 资产曲线 -->
        <div class="section">
            <div class="section-title">资产曲线</div>
            <div class="chart-container">
                {chart_svg}
            </div>
        </div>

        <!-- 按月归因 -->
        <div class="section">
            <div class="section-title">按月收益归因</div>
            {monthly_table}
        </div>

        <!-- 按股票归因 -->
        <div class="section">
            <div class="section-title">按股票收益归因</div>
            {stock_table}
        </div>

        <!-- 策略代码 -->
        <div class="section code-section">
            <div class="section-title">策略代码</div>
            <button class="code-toggle" onclick="toggleCode()">显示/隐藏代码</button>
            <div class="code-content" id="codeContent">
                <pre>{escaped_code}</pre>
            </div>
        </div>

        <div class="footer">
            由 dieΠ（带派）回测系统生成
        </div>
    </div>

    <script>
        function toggleCode() {{
            var content = document.getElementById('codeContent');
            content.classList.toggle('show');
        }}
    </script>
</body>
</html>'''

        return html_template

    @classmethod
    def _format_date(cls, date_str: str) -> str:
        """格式化日期字符串"""
        if len(date_str) == 8:
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        return date_str

    @classmethod
    def _generate_chart_svg(cls, result: PortfolioResult) -> str:
        """生成资产曲线 SVG"""
        if result.daily_values is None or result.daily_values.empty:
            return '<p style="color:#888;text-align:center;">无数据</p>'

        df = result.daily_values
        values = df['total_value'].tolist()

        if len(values) < 2:
            return '<p style="color:#888;text-align:center;">数据不足</p>'

        # SVG 尺寸
        width = 1000
        height = 300
        padding = 50

        # 计算坐标
        min_val = min(values) * 0.98
        max_val = max(values) * 1.02
        val_range = max_val - min_val if max_val != min_val else 1

        points = []
        for i, val in enumerate(values):
            x = padding + (i / (len(values) - 1)) * (width - 2 * padding)
            y = height - padding - ((val - min_val) / val_range) * (height - 2 * padding)
            points.append(f"{x:.1f},{y:.1f}")

        path_data = "M " + " L ".join(points)

        # 生成 Y 轴刻度
        y_ticks = []
        for i in range(5):
            val = min_val + (val_range * i / 4)
            y = height - padding - (i / 4) * (height - 2 * padding)
            y_ticks.append(f'<text x="{padding-10}" y="{y:.1f}" text-anchor="end" fill="#888" font-size="11">{val:,.0f}</text>')
            y_ticks.append(f'<line x1="{padding}" y1="{y:.1f}" x2="{width-padding}" y2="{y:.1f}" stroke="#2a3f5f" stroke-dasharray="3,3"/>')

        # 生成 X 轴刻度（每月第一天）
        x_ticks = []
        dates = df.index.tolist()
        shown_months = set()
        for i, date in enumerate(dates):
            if hasattr(date, 'month'):
                month_key = (date.year, date.month)
                if month_key not in shown_months:
                    shown_months.add(month_key)
                    x = padding + (i / (len(dates) - 1)) * (width - 2 * padding)
                    label = date.strftime('%m/%d')
                    x_ticks.append(f'<text x="{x:.1f}" y="{height-padding+20}" text-anchor="middle" fill="#888" font-size="10">{label}</text>')

        svg = f'''<svg width="100%" height="{height}" viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet">
            <!-- Y轴刻度 -->
            {"".join(y_ticks)}
            <!-- X轴刻度 -->
            {"".join(x_ticks)}
            <!-- 曲线 -->
            <path d="{path_data}" fill="none" stroke="#4fc3f7" stroke-width="2"/>
            <!-- 起始点 -->
            <circle cx="{points[0].split(',')[0]}" cy="{points[0].split(',')[1]}" r="4" fill="#4fc3f7"/>
            <!-- 终点 -->
            <circle cx="{points[-1].split(',')[0]}" cy="{points[-1].split(',')[1]}" r="4" fill="{"#4CAF50" if result.total_return >= 0 else "#F44336"}"/>
        </svg>'''

        return svg

    @classmethod
    def _generate_monthly_table(cls, result: PortfolioResult) -> str:
        """生成按月归因表格"""
        if not result.trades:
            return '<p style="color:#888;">无交易记录</p>'

        # 按月统计
        from ..engine.attribution import calculate_attribution

        monthly = calculate_attribution(
            result.trades, result.initial_cash
        )['by_month']
        if monthly.empty:
            return '<p style="color:#888;">无卖出记录</p>'

        # 生成表格
        rows = []
        for _, stats in monthly.iterrows():
            month_fmt = str(stats['month'])
            profit = stats['profit']
            profit_pct = stats['profit_pct'] * 100
            color = "positive" if profit >= 0 else "negative"

            rows.append(f'''<tr>
                <td>{month_fmt}</td>
                <td class="{color}">{profit:+,.0f}</td>
                <td class="{color}">{profit_pct:+.2f}%</td>
                <td>{int(stats['trade_count'])}</td>
            </tr>''')

        return f'''<table>
            <thead>
                <tr><th>月份</th><th>盈亏</th><th>盈亏%</th><th>交易次数</th></tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
        </table>'''

    @classmethod
    def _generate_stock_table(cls, result: PortfolioResult) -> str:
        """生成按股票归因表格"""
        if not result.trades:
            return '<p style="color:#888;">无交易记录</p>'

        # 按股票统计
        from ..engine.attribution import calculate_attribution

        symbols = sorted({
            str(trade.get('symbol', ''))
            for trade in result.trades
            if trade.get('symbol')
        })
        stock_names = {symbol: cls._get_stock_name(symbol) for symbol in symbols}
        stocks = calculate_attribution(
            result.trades,
            result.initial_cash,
            stock_names,
        )['by_stock']
        if stocks.empty:
            return '<p style="color:#888;">无交易记录</p>'

        # 生成表格
        rows = []
        for _, stats in stocks.iterrows():
            symbol = str(stats['symbol'])
            profit = stats['profit']
            profit_pct = stats['profit_pct'] * 100
            color = "positive" if profit >= 0 else "negative"
            trade_count = int(stats['trade_count'])
            win_rate = stats['win_rate']
            win_rate_text = (
                "N/A" if pd.isna(win_rate) else f"{win_rate * 100:.0f}%"
            )

            name = str(stats['name'])

            rows.append(f'''<tr>
                <td>{symbol}</td>
                <td>{name}</td>
                <td class="{color}">{profit:+,.0f}</td>
                <td class="{color}">{profit_pct:+.2f}%</td>
                <td>{trade_count}</td>
                <td>{win_rate_text}</td>
            </tr>''')

        return f'''<table>
            <thead>
                <tr><th>股票代码</th><th>名称</th><th>盈亏</th><th>盈亏%</th><th>交易次数</th><th>胜率</th></tr>
            </thead>
            <tbody>{"".join(rows)}</tbody>
        </table>'''

    @classmethod
    def _get_stock_name(cls, symbol: str) -> str:
        """获取股票名称"""
        try:
            from ..data import DataProvider
            provider = DataProvider()
            info = provider.get_stock_info(symbol)
            if info and 'name' in info:
                return info['name']
        except:
            pass
        return ''
