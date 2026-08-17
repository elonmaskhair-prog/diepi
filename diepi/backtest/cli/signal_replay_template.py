# -*- coding: utf-8 -*-
"""内置清单重放策略（CLI --signals 入口的执行体）

把预计算的交易清单交给引擎执行，只借用撮合/仿真层的真实性
（T+1、涨跌停、流动性帽、费用、资金竞争）。

支持两种清单格式（--signals-format auto 时按列名自动识别）：

1) 目标权重型（推荐，闭环自愈）：date,symbol,target_weight
   - 每行 = 当日该标的的目标仓位（占总资产比例）
   - 三态语义：weight>0=持有到该比例；weight==0=显式清仓；
     当日无该标的行=无指令保留现状（不得从缺席推断卖出）
   - 经 ctx.order_target_percent 落地（开盘竞价，卖先买后资金闭环）

2) 动作型（开环，表达力全）：date,symbol,action[,percent|shares|amount]
   - action ∈ {buy, sell}（开盘竞价单）
   - 数量三选一列，全缺时 buy 拒单、sell 默认全部可卖

日期格式 YYYYMMDD（允许带连字符，自动剥离）。
防前视：仅在 ctx.current_date == 清单日时下单；未执行的清单日期
（非交易日/超出回测窗口）在结束时汇总告警。
"""

import sys

from diepi.backtest.cli.signal_input import SignalReplayInput


# 由 CLI/GUI 在执行策略前注入。正式执行边界是冻结对象，不是文件路径。
SIGNALS_INPUT = None
# 保留旧变量只为给手工注入者明确迁移错误；模板不会再打开路径。
SIGNALS_FILE = ''
SIGNALS_FORMAT = 'auto'

_STATE = {
    'input': None,
    'fmt': None,        # 'target' | 'action'
    'executed_dates': set(),
}


def on_init(ctx):
    if type(SIGNALS_INPUT) is not SignalReplayInput:
        legacy = (
            "；不再接受 SIGNALS_FILE 路径，请改用 diepi run --signals"
            if SIGNALS_FILE else ""
        )
        raise ValueError("清单重放策略需要冻结 SIGNALS_INPUT" + legacy)
    _STATE['input'] = SIGNALS_INPUT
    _STATE['fmt'] = SIGNALS_INPUT.signal_format
    _STATE['executed_dates'] = set()
    for warning in SIGNALS_INPUT.warnings:
        print("警告: " + warning, file=sys.stderr)
    print(
        f"清单重放: {SIGNALS_INPUT.source_name} "
        f"格式={SIGNALS_INPUT.signal_format} "
        f"共 {len(SIGNALS_INPUT.instructions)} 条指令 / "
        f"{len(SIGNALS_INPUT.dates)} 个清单日",
        file=sys.stderr,
    )


def on_before_market_open(ctx):
    today = ctx.current_date
    frozen = _STATE['input']
    if type(frozen) is not SignalReplayInput:
        raise RuntimeError("signal replay was not initialized")
    rows = frozen.rows_for(today)
    if rows:
        _STATE['executed_dates'].add(today)

    fmt = _STATE['fmt']
    if fmt == 'target' and rows:
        # 整日一次 rebalance：三态语义 + 先减后加 + 权重和复核，
        # 与逐行 order_target_percent 等价但把当日视为一个声明式快照
        ctx.rebalance({r.symbol: r.target_weight for r in rows})
    for row in (rows if fmt != 'target' else []):
        symbol = row.symbol
        # 动作型
        action = row.action
        qty = {
            key: value
            for key, value in (
                ('percent', row.percent),
                ('shares', row.shares),
                ('amount', row.amount),
            )
            if value is not None
        }
        if action == 'buy':
            ctx.buy_at_open(symbol, **qty)
        elif action == 'sell':
            ctx.sell_at_open(symbol, **{k: v for k, v in qty.items()
                                        if k in ('percent', 'shares')})
        else:
            print(f"警告: 未知 action '{action}' ({today} {symbol})，跳过",
                  file=sys.stderr)

    # 引擎只为返回的标的加载行情：今日清单标的 + 持仓标的
    held = [s for s, p in ctx.get_positions().items()
            if getattr(p, 'shares', 0) > 0]
    return sorted({r.symbol for r in rows} | set(held))


def on_finish(ctx):
    frozen = _STATE['input']
    if type(frozen) is not SignalReplayInput:
        return
    missed = sorted(set(frozen.dates) - _STATE['executed_dates'])
    if missed:
        shown = ', '.join(missed[:10]) + ('...' if len(missed) > 10 else '')
        message = (
            f"{len(missed)} 个清单日期未被执行（非交易日或超出回测窗口）: "
            f"{shown}"
        )
        ctx.add_result_warning("UNCONSUMED_SIGNAL_DATES", message)
        print(
            "警告: " + message,
            file=sys.stderr)
