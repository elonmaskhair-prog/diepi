"""Built-in causal replay strategy for :mod:`diepi` combo bundles."""

from __future__ import annotations

import math

from diepi.backtest.cli.combo_bundle import ComboReplayBundle


# Injected by the CLI/GUI after this module body is evaluated.
COMBO_BUNDLE = None

# These thresholds are part of the replay strategy, not hidden engine knobs.
WEIGHT_TOL = 0.002
MIN_REDUCE_RATIO = 0.02
MIN_TRADE_WEIGHT = 0.003
MIN_TRADE_AMOUNT = 5000.0

# Frozen overlap contract (v1): a same-day close sell has priority over the
# target row at the opening auction.  The target expresses the position that
# should already exist before the pre-known close exit; it is not an instruction
# to churn out and back in at the open.  A subwindow that starts on such an exit
# cannot reconstruct the omitted pre-window position and is marked PARTIAL by
# the engine instead of reporting a cash-only SUCCESS.
CLOSE_TARGET_OVERLAP_SEMANTICS = "close_priority_existing_position_v1"

_STATE = {
    "executed_dates": set(),
    "first_engine_date": None,
}


def _bundle() -> ComboReplayBundle:
    if type(COMBO_BUNDLE) is not ComboReplayBundle:
        raise ValueError("combo replay requires a validated COMBO_BUNDLE")
    return COMBO_BUNDLE


def on_init(ctx):
    bundle = _bundle()
    _STATE["executed_dates"] = set()
    _STATE["first_engine_date"] = None
    print(
        "combo replay: "
        f"tag={bundle.tag} scope={bundle.start_date}..{bundle.end_date} "
        f"symbols={len(bundle.symbols)} targets={bundle.target_rows} "
        f"close_sells={bundle.close_sell_rows}"
    )


def on_before_market_open(ctx):
    """Submit target reductions/additions before the opening auction."""

    bundle = _bundle()
    trade_date = str(ctx.current_date).replace("-", "")
    positions = ctx.get_positions() or {}
    held = {
        symbol
        for symbol, position in positions.items()
        if int(getattr(position, "shares", 0)) > 0
    }
    if trade_date not in bundle.known_dates:
        raise ValueError(
            "combo daily is missing an engine trade day: " + trade_date
        )

    target = bundle.targets_for(trade_date)
    close_sell_today = set(bundle.close_sells_for(trade_date))
    _STATE["executed_dates"].add(trade_date)
    if _STATE["first_engine_date"] is None:
        _STATE["first_engine_date"] = trade_date
        missing_warm_positions = sorted(
            (set(target) & close_sell_today) - held
        )
        if missing_warm_positions:
            shown = ", ".join(missing_warm_positions[:10])
            suffix = "..." if len(missing_warm_positions) > 10 else ""
            ctx.add_result_warning(
                "COMBO_WARM_START_UNAVAILABLE",
                "combo subwindow starts on a target+close overlap but the "
                "required pre-window position is absent for "
                f"{len(missing_warm_positions)} symbol(s): {shown}{suffix}",
            )
    total_asset = float(ctx.get_total_asset())
    if not math.isfinite(total_asset) or total_asset <= 0.0:
        raise ValueError("combo replay requires positive finite total asset")
    current_weights = {}
    for symbol, position in positions.items():
        shares = int(getattr(position, "shares", 0))
        if shares <= 0:
            continue
        market_value = float(getattr(position, "market_value", 0.0))
        if market_value <= 0.0:
            daily = ctx.get_daily(symbol, days=1, fields=["close"])
            if daily is not None and not daily.empty:
                market_value = shares * float(daily["close"].iloc[-1])
        if market_value > 0.0:
            current_weights[symbol] = market_value / total_asset

    universe = sorted(
        set(current_weights) | set(target) | close_sell_today
    )
    for symbol in universe:
        if symbol in close_sell_today:
            continue
        current = float(current_weights.get(symbol, 0.0))
        wanted = float(target.get(symbol, 0.0))
        if current <= wanted + WEIGHT_TOL:
            continue
        position = ctx.get_position(symbol)
        if position is None or int(getattr(position, "available_shares", 0)) <= 0:
            continue
        reduction = max(0.0, min(1.0, (current - wanted) / max(current, 1e-12)))
        if reduction >= MIN_REDUCE_RATIO:
            ctx.sell_at_open(symbol, percent=reduction)

    for symbol in universe:
        if symbol in close_sell_today:
            continue
        current = float(current_weights.get(symbol, 0.0))
        wanted = float(target.get(symbol, 0.0))
        if wanted <= current + WEIGHT_TOL:
            continue
        delta_weight = wanted - current
        amount = delta_weight * total_asset
        if delta_weight < MIN_TRADE_WEIGHT and amount < MIN_TRADE_AMOUNT:
            continue
        ctx.buy_at_open(symbol, amount=amount)
    return universe


def on_after_open(ctx, bars):
    """Causally submit pre-known exits for today's closing auction."""

    bundle = _bundle()
    trade_date = str(ctx.current_date).replace("-", "")
    for symbol in bundle.close_sells_for(trade_date):
        position = ctx.get_position(symbol)
        if position is None or int(getattr(position, "available_shares", 0)) <= 0:
            continue
        ctx.schedule_at_close("sell", symbol, percent=1.0)


def on_finish(ctx):
    bundle = _bundle()
    missed = sorted(set(bundle.known_dates) - _STATE["executed_dates"])
    if missed:
        shown = ", ".join(missed[:10])
        suffix = "..." if len(missed) > 10 else ""
        ctx.add_result_warning(
            "UNCONSUMED_COMBO_DATES",
            f"{len(missed)} combo date(s) were outside the executed trading "
            f"window or were not exchange sessions: {shown}{suffix}",
        )
