"""Minimal MA5/MA20 crossover strategy for diePi.

The callback runs before market open on trading day T.  ``ctx.get_daily`` can
therefore see completed bars only through T-1.  A crossover between T-2 and
T-1 creates an order for T's opening window; no same-day close is inspected.

This is an educational strategy, not investment advice.  It intentionally
uses a single strict crossover event: a rejected order is not retried merely
because the fast average remains above or below the slow average.
"""

FAST_PERIOD = 5
SLOW_PERIOD = 20
TARGET_WEIGHT = 0.95


def moving_average_cross(close, fast_period=FAST_PERIOD, slow_period=SLOW_PERIOD):
    """Return ``(crossed_up, crossed_down)`` for the last two observations."""

    if fast_period <= 0 or slow_period <= 0 or fast_period >= slow_period:
        raise ValueError("periods must satisfy 0 < fast_period < slow_period")
    if close is None or len(close) < slow_period + 1:
        return False, False

    previous = close.iloc[:-1]
    previous_fast = previous.tail(fast_period).mean()
    previous_slow = previous.tail(slow_period).mean()
    current_fast = close.tail(fast_period).mean()
    current_slow = close.tail(slow_period).mean()
    crossed_up = previous_fast <= previous_slow and current_fast > current_slow
    crossed_down = previous_fast >= previous_slow and current_fast < current_slow
    return bool(crossed_up), bool(crossed_down)


def on_init(ctx):
    print(f"diePi example: MA{FAST_PERIOD}/MA{SLOW_PERIOD} strict crossover")


def on_before_market_open(ctx):
    pool = ctx.get_stock_pool()
    for symbol in pool:
        history = ctx.get_daily(symbol, days=SLOW_PERIOD + 1)
        if history is None or len(history) < SLOW_PERIOD + 1:
            continue

        crossed_up, crossed_down = moving_average_cross(
            history["close"], FAST_PERIOD, SLOW_PERIOD
        )
        position = ctx.get_position(symbol)
        has_position = position is not None and position.shares > 0

        if crossed_down and has_position:
            ctx.order_target_percent(
                symbol,
                0.0,
                when="open",
                note=f"MA{FAST_PERIOD}_DOWN_CROSS_MA{SLOW_PERIOD}",
            )
        elif crossed_up and not has_position:
            ctx.order_target_percent(
                symbol,
                TARGET_WEIGHT,
                when="open",
                note=f"MA{FAST_PERIOD}_UP_CROSS_MA{SLOW_PERIOD}",
            )
    return pool


def on_day(ctx, bars):
    pass


def on_after_market_close(ctx):
    pass


def on_finish(ctx):
    positions = ctx.get_positions()
    holding = [symbol for symbol, position in positions.items() if position.shares > 0]
    print("final holdings: " + (", ".join(holding) if holding else "none"))
