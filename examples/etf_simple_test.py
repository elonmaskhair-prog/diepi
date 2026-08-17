"""
简单ETF测试策略 - 验证框架是否支持ETF交易
"""

def on_init(ctx):
    print("=" * 60)
    print("ETF简单测试策略")
    print("=" * 60)

def on_before_market_open(ctx):
    trade_date = str(ctx.current_date)
    pool = ctx.get_stock_pool()

    # 只在特定日期交易
    if trade_date == '20150105':
        print(f"[{trade_date}] 尝试买入 510300.SH")
        order = ctx.buy_at_open('510300.SH', percent=0.2)
        print(f"[{trade_date}] 订单: {order}")

    # 2015-01-09 是交易日；原示例的 2015-01-10 为周六，卖出分支永远不会执行。
    if trade_date == '20150109':
        pos = ctx.get_position('510300.SH')
        print(f"[{trade_date}] 当前持仓: {pos}")
        if pos and pos.shares > 0:
            print(f"[{trade_date}] 尝试卖出 510300.SH")
            order = ctx.sell_at_open('510300.SH', percent=1.0)
            print(f"[{trade_date}] 订单: {order}")

    # 返回股票池，让引擎加载这些股票的数据
    return pool

def on_day(ctx, bars):
    pass

def on_after_market_close(ctx):
    pass
