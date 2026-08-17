"""Explicit, causal liquidity assumptions for daily auction windows.

Daily OHLCV data contains full-session turnover, not opening- or closing-
auction turnover.  Using the current day's ``amount`` to size either auction
therefore both leaks future information at the open and overstates the
available close-auction pool.  This module deliberately has no implicit
fallback: every auction window that is used must have an explicit cap rule.

Two causal rules are supported:

``fixed_yuan``
    A fixed executable-notional cap in yuan.

``previous_day_ratio``
    A fraction of the previous trading day's full-session turnover.  The
    previous-day amount is observable before both auctions, but remains a
    modelling assumption rather than measured auction liquidity.

The resolved value is the *final executable notional cap*.  A broker-level
bar participation ratio must not be applied to it a second time.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from numbers import Real
from typing import Optional, Tuple


class AuctionWindow(str, Enum):
    """Daily auction windows that require independent liquidity pools."""

    OPEN = "open"
    CLOSE = "close"


class AuctionCapMode(str, Enum):
    """Supported causal cap inputs."""

    FIXED_YUAN = "fixed_yuan"
    PREVIOUS_DAY_RATIO = "previous_day_ratio"


class AuctionLiquidityUnavailable(ValueError):
    """Raised when an auction is used without a resolvable explicit cap."""


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a real number")
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return result


@dataclass(frozen=True)
class AuctionCapSpec:
    """One immutable cap rule for one daily auction window."""

    mode: AuctionCapMode
    value: float

    def __post_init__(self) -> None:
        if type(self.mode) is not AuctionCapMode:
            raise TypeError("mode must be exactly AuctionCapMode")
        value = _positive_number(self.value, "auction cap value")
        if self.mode is AuctionCapMode.PREVIOUS_DAY_RATIO and value > 1.0:
            raise ValueError("previous_day_ratio must be within (0, 1]")
        object.__setattr__(self, "value", value)

    @classmethod
    def fixed_yuan(cls, amount_yuan: float) -> "AuctionCapSpec":
        return cls(AuctionCapMode.FIXED_YUAN, amount_yuan)

    @classmethod
    def previous_day_ratio(cls, ratio: float) -> "AuctionCapSpec":
        return cls(AuctionCapMode.PREVIOUS_DAY_RATIO, ratio)

    def resolve(self, *, previous_day_amount_yuan: Optional[float] = None) -> float:
        """Resolve the final executable-notional cap in yuan."""

        if self.mode is AuctionCapMode.FIXED_YUAN:
            return self.value
        if previous_day_amount_yuan is None:
            raise AuctionLiquidityUnavailable(
                "previous-day turnover is required by the auction cap rule"
            )
        previous = _positive_number(
            previous_day_amount_yuan, "previous_day_amount_yuan"
        )
        resolved = previous * self.value
        if not isfinite(resolved) or resolved <= 0:
            raise AuctionLiquidityUnavailable(
                "resolved auction cap must be finite and positive"
            )
        return resolved

    def audit_value(self) -> str:
        return f"{self.mode.value}:{format(self.value, '.17g')}"


@dataclass(frozen=True)
class DailyAuctionLiquidityPolicy:
    """Explicit cap rules for daily opening and closing auctions.

    Either side may be omitted so a strategy that only uses the other auction
    need not invent an irrelevant assumption.  Attempting to resolve an
    omitted side fails loudly at the execution boundary.
    """

    open_cap: Optional[AuctionCapSpec] = None
    close_cap: Optional[AuctionCapSpec] = None

    def __post_init__(self) -> None:
        for name in ("open_cap", "close_cap"):
            value = getattr(self, name)
            if value is not None and type(value) is not AuctionCapSpec:
                raise TypeError(f"{name} must be exactly AuctionCapSpec or None")

    def _spec_for(self, window: AuctionWindow) -> Optional[AuctionCapSpec]:
        if type(window) is not AuctionWindow:
            raise TypeError("window must be exactly AuctionWindow")
        return self.open_cap if window is AuctionWindow.OPEN else self.close_cap

    def resolve(
        self,
        window: AuctionWindow,
        *,
        previous_day_amount_yuan: Optional[float] = None,
    ) -> float:
        spec = self._spec_for(window)
        if spec is None:
            cli_fixed = f"--daily-{window.value}-cap-yuan"
            cli_ratio = f"--daily-{window.value}-previous-day-ratio"
            raise AuctionLiquidityUnavailable(
                f"daily {window.value} auction requires an explicit liquidity cap; "
                f"CLI users must set {cli_fixed} or {cli_ratio}"
            )
        return spec.resolve(
            previous_day_amount_yuan=previous_day_amount_yuan
        )

    def assumptions(self) -> Tuple[Tuple[str, str], ...]:
        """Return stable key/value pairs suitable for ``ResultAssumption``."""

        values = []
        for window in (AuctionWindow.OPEN, AuctionWindow.CLOSE):
            spec = self._spec_for(window)
            values.append((
                f"liquidity.daily_{window.value}_cap",
                spec.audit_value() if spec is not None else "unconfigured",
            ))
        return tuple(values)


def build_daily_auction_liquidity_policy(
    *,
    open_fixed_yuan: Optional[float] = None,
    close_fixed_yuan: Optional[float] = None,
    open_previous_day_ratio: Optional[float] = None,
    close_previous_day_ratio: Optional[float] = None,
) -> Optional[DailyAuctionLiquidityPolicy]:
    """Build one explicit policy from public API/CLI scalar inputs.

    Fixed-yuan and previous-day-ratio rules are mutually exclusive within
    each auction window.  Omitting all four inputs returns ``None`` so the
    engine retains its fail-fast-on-use default rather than manufacturing a
    hidden assumption.
    """
    if open_fixed_yuan is not None and open_previous_day_ratio is not None:
        raise ValueError(
            "daily open auction accepts either fixed_yuan or "
            "previous_day_ratio, not both"
        )
    if close_fixed_yuan is not None and close_previous_day_ratio is not None:
        raise ValueError(
            "daily close auction accepts either fixed_yuan or "
            "previous_day_ratio, not both"
        )
    if all(value is None for value in (
            open_fixed_yuan,
            close_fixed_yuan,
            open_previous_day_ratio,
            close_previous_day_ratio)):
        return None

    open_cap = None
    if open_fixed_yuan is not None:
        open_cap = AuctionCapSpec.fixed_yuan(open_fixed_yuan)
    elif open_previous_day_ratio is not None:
        open_cap = AuctionCapSpec.previous_day_ratio(
            open_previous_day_ratio)

    close_cap = None
    if close_fixed_yuan is not None:
        close_cap = AuctionCapSpec.fixed_yuan(close_fixed_yuan)
    elif close_previous_day_ratio is not None:
        close_cap = AuctionCapSpec.previous_day_ratio(
            close_previous_day_ratio)

    return DailyAuctionLiquidityPolicy(
        open_cap=open_cap,
        close_cap=close_cap,
    )


__all__ = [
    "AuctionCapMode",
    "AuctionCapSpec",
    "AuctionLiquidityUnavailable",
    "AuctionWindow",
    "DailyAuctionLiquidityPolicy",
    "build_daily_auction_liquidity_policy",
]
