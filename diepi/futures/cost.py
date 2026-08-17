"""Cost model for futures trading."""
from __future__ import annotations
from dataclasses import dataclass
from math import isfinite
from numbers import Integral, Real


@dataclass(frozen=True)
class CostModel:
    """Futures trading cost: commission + slippage.

    commission_rate: per-side, as fraction of notional (CFFEX ~0.23 bps)
    slippage_points: applied to execution price in index points
    """
    commission_rate: float = 0.000023
    slippage_points: float = 0.2

    def __post_init__(self) -> None:
        for name in ("commission_rate", "slippage_points"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Real):
                raise TypeError(f"{name} must be a real number")
            normalized = float(value)
            if not isfinite(normalized) or normalized < 0:
                raise ValueError(f"{name} must be finite and non-negative")
            object.__setattr__(self, name, normalized)

    def execution_price(self, raw_price: float, is_buy: bool) -> float:
        """Apply slippage. Buy = higher price, sell = lower price."""
        if isinstance(raw_price, bool) or not isinstance(raw_price, Real):
            raise TypeError("raw_price must be a real number")
        raw_price = float(raw_price)
        if not isfinite(raw_price) or raw_price <= 0:
            raise ValueError("raw_price must be finite and positive")
        if type(is_buy) is not bool:
            raise TypeError("is_buy must be exactly bool")
        if is_buy:
            result = raw_price + self.slippage_points
        else:
            result = raw_price - self.slippage_points
        if not isfinite(result) or result <= 0:
            raise ValueError("slippage-adjusted execution price must be positive")
        return result

    def commission(self, price: float, multiplier: int, lots: int) -> float:
        """One-side commission in yuan."""
        if isinstance(price, bool) or not isinstance(price, Real):
            raise TypeError("price must be a real number")
        price = float(price)
        if not isfinite(price) or price <= 0:
            raise ValueError("price must be finite and positive")
        for name, value in (("multiplier", multiplier), ("lots", lots)):
            if isinstance(value, bool) or not isinstance(value, Integral):
                raise TypeError(f"{name} must be an integer")
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        return price * int(multiplier) * int(lots) * self.commission_rate

    def round_trip_cost(self, entry_price: float, exit_price: float,
                        multiplier: int, lots: int) -> float:
        """Total cost for open + close in yuan."""
        return (self.commission(entry_price, multiplier, lots)
                + self.commission(exit_price, multiplier, lots))
