"""
ETF 2B 假突破反转策略

来源: Victor Sperandeo "专业投机原理" 2B形态
适配: ETF宽基指数 - 集体情绪超调后修复

信号逻辑 (收盘价版本):
  Day B   : 收盘价创 N 日新低 (假突破发生)
  Day B+1 : 收盘价回到 Day B 收盘价以上 (假突破确认)
  Day B+2 : 次日开盘入场
  出场    : 持有 HOLD_DAYS 天后在开盘离场

优先级 (多标的同时触发):
  取 "假突破深度" 最大者 = (N日低点 - 当日收盘) / N日低点

使用方法:
    diepi examples/etf_2b_reversal.py \
        --symbols 510050.SH,510300.SH,510500.SH,512100.SH,159915.SZ \
        --start 20130101 --end 20251231 --stamp-duty 0 \
        --daily-open-previous-day-ratio 0.1

参数调节 (修改下方常量):
    N_DAYS       : 定义"新低"的回看窗口 (默认20)
    HOLD_DAYS    : 持有天数 (默认5)
    MIN_DROP_PCT : 最小跌破幅度过滤 (默认0, 不过滤)
    MIN_RECOVERY : 最小回复幅度过滤 (默认0, 不过滤)
    POSITION_PCT : 仓位比例 (默认0.98)
"""

import numpy as np
import pandas as pd

# ==================== 策略参数 ====================
N_DAYS = 20           # 新低回看窗口
HOLD_DAYS = 5         # 持有天数，期满后在开盘离场
MIN_DROP_PCT = 0.003  # 最小跌破幅度 (0.3%)
MIN_RECOVERY = 0.003  # 最小回复幅度 (0.3%)
TREND_MA = 60         # 趋势过滤MA (0=关闭)，只有收盘>MA时才交易
POSITION_PCT = 0.98   # 仓位

ETF_SYMBOLS = [
    "510050.SH",  # SZ50
    "510300.SH",  # HS300
    "510500.SH",  # ZZ500
    "512100.SH",  # ZZ1000
    "159915.SZ",  # CYB
]

# ==================== 全局状态 ====================
_position = None        # 当前持仓: {symbol, entry_date, hold_days, days_held, exit_type}
_trade_dates = []


def _check_2b(df: pd.DataFrame) -> dict | None:
    """
    在 on_before_market_open(T) 中调用，df 包含到 T-1 的数据。

    2B 条件:
      T-2 收盘 = N 日收盘新低 (假突破)
      T-1 收盘 > T-2 收盘  (回复确认)

    返回: None(不触发) 或 {'depth': float, 'recovery': float}
    """
    if len(df) < N_DAYS + 2:
        return None

    close = df["close"].values

    close_T1 = close[-1]   # T-1 收盘
    close_T2 = close[-2]   # T-2 收盘 (假突破发生日)

    # T-2 对应的 N 日回看区间: [T-2-N, T-2), 不含 T-2 自身
    window = close[-N_DAYS - 2: -2]
    if len(window) < N_DAYS:
        return None
    n_day_low = float(np.min(window))

    # 条件1: T-2 是 N 日新低
    if close_T2 > n_day_low:
        return None

    # 条件2: T-1 收盘回到 T-2 以上
    if close_T1 <= close_T2:
        return None

    # 可选过滤: 跌破幅度
    depth = (n_day_low - close_T2) / n_day_low if n_day_low > 0 else 0.0
    if depth < MIN_DROP_PCT:
        return None

    # 可选过滤: 回复幅度
    recovery = (close_T1 - close_T2) / close_T2 if close_T2 > 0 else 0.0
    if recovery < MIN_RECOVERY:
        return None

    return {"depth": depth, "recovery": recovery}


def on_init(ctx):
    print("=" * 60)
    print("ETF 2B 假突破反转策略")
    print("=" * 60)
    print(f"  标的:     {', '.join(ETF_SYMBOLS)}")
    print(f"  新低窗口: {N_DAYS} 日")
    print(f"  持有期:   {HOLD_DAYS} 日")
    print("  出场方式: 开盘")
    print(f"  最小跌幅: {MIN_DROP_PCT:.1%}")
    print(f"  最小回复: {MIN_RECOVERY:.1%}")
    print(f"  趋势MA:   {TREND_MA} 日 ({'开启' if TREND_MA > 0 else '关闭'})")
    print(f"  仓位:     {POSITION_PCT:.0%}")
    print("=" * 60)


def on_before_market_open(ctx):
    global _position, _trade_dates

    trade_date = str(ctx.current_date)
    pool = ctx.get_stock_pool()

    if trade_date not in _trade_dates:
        _trade_dates.append(trade_date)

    # ---------- 出场逻辑 ----------
    if _position is not None:
        _position["days_held"] += 1

        if _position["days_held"] >= _position["hold_days"]:
            sym = _position["symbol"]
            pos = ctx.get_position(sym)
            if pos is not None and pos.shares > 0:
                ctx.sell_at_open(sym, percent=1.0)
                print(f"[{trade_date}] 平仓: {sym} 持{_position['days_held']}天")
            _position = None

    if _position is not None:
        return pool

    # ---------- 入场逻辑 ----------
    candidates = []

    for sym in ETF_SYMBOLS:
        if sym not in pool:
            continue
        df = ctx.get_daily(symbol=sym, days=max(N_DAYS + 5, TREND_MA + 5))
        if df is None or len(df) < N_DAYS + 2:
            continue

        signal = _check_2b(df)
        if signal is None:
            continue

        # 趋势过滤: T-1 收盘须 > TREND_MA 日均线
        if TREND_MA > 0:
            if len(df) < TREND_MA + 2:
                continue
            ma = float(df["close"].iloc[-TREND_MA - 1:-1].mean())
            if df["close"].iloc[-1] < ma:
                continue

        candidates.append({
            "symbol": sym,
            "depth": signal["depth"],
            "recovery": signal["recovery"],
        })

    if not candidates:
        return pool

    # 多个信号: 取假突破最深的
    best = max(candidates, key=lambda x: x["depth"])
    sym = best["symbol"]

    ctx.buy_at_open(sym, percent=POSITION_PCT)
    _position = {
        "symbol": sym,
        "entry_date": trade_date,
        "hold_days": HOLD_DAYS,
        "days_held": 1,
        "depth": best["depth"],
        "recovery": best["recovery"],
    }
    print(
        f"[{trade_date}] 开仓: {sym}  "
        f"depth={best['depth']:.2%}  recovery={best['recovery']:.2%}"
    )

    return pool


def on_day(ctx, bars):
    pass


def on_after_market_close(ctx):
    pass
