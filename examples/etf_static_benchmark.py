"""
Static ETF benchmark strategy for official backtest comparisons.

Env vars:
    BENCH_SYMBOLS: comma-separated ETF symbols
    BENCH_WEIGHTS: comma-separated target weights; defaults to equal weight
"""

from __future__ import annotations

import os


BENCH_SYMBOLS = [s.strip() for s in os.environ.get("BENCH_SYMBOLS", "510300.SH").split(",") if s.strip()]
_weights_raw = [w.strip() for w in os.environ.get("BENCH_WEIGHTS", "").split(",") if w.strip()]

if _weights_raw:
    BENCH_WEIGHTS = [float(w) for w in _weights_raw]
else:
    BENCH_WEIGHTS = [1.0 / len(BENCH_SYMBOLS)] * len(BENCH_SYMBOLS)

if not BENCH_SYMBOLS:
    raise RuntimeError("BENCH_SYMBOLS is empty.")
if len(BENCH_WEIGHTS) != len(BENCH_SYMBOLS):
    raise RuntimeError("BENCH_WEIGHTS length must match BENCH_SYMBOLS length.")

_weight_sum = sum(BENCH_WEIGHTS)
if _weight_sum <= 0:
    raise RuntimeError("BENCH_WEIGHTS sum must be positive.")
BENCH_WEIGHTS = [w / _weight_sum for w in BENCH_WEIGHTS]

_base_capital = None
_filled_symbols = set()


def on_init(ctx):
    global _base_capital
    _base_capital = max(float(ctx.get_total_asset()), 0.0)
    print("=" * 72)
    print("Static ETF Benchmark")
    print("=" * 72)
    print(f"Symbols : {BENCH_SYMBOLS}")
    print(f"Weights : {[round(w, 6) for w in BENCH_WEIGHTS]}")
    print(f"Base cap: {_base_capital}")
    print("=" * 72)


def on_before_market_open(ctx):
    global _filled_symbols
    if _base_capital is None or _base_capital <= 0.0:
        return BENCH_SYMBOLS
    positions = ctx.get_positions() or {}
    for symbol in BENCH_SYMBOLS:
        pos = positions.get(symbol)
        if pos is not None and int(getattr(pos, "shares", 0)) > 0:
            _filled_symbols.add(symbol)
    for symbol, weight in zip(BENCH_SYMBOLS, BENCH_WEIGHTS):
        if symbol in _filled_symbols:
            continue
        d1 = ctx.get_daily(symbol, days=1, fields=["close"])
        if d1 is None or d1.empty:
            continue
        amount = _base_capital * float(weight)
        if amount <= 0.0:
            continue
        ctx.buy_at_open(symbol, amount=amount)
    return BENCH_SYMBOLS


def on_day(ctx, bars):
    pass


def on_after_market_close(ctx):
    pass
