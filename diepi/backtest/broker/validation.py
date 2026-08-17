"""Numeric validation helpers for broker/account boundaries."""

from math import isfinite
from numbers import Integral


def finite_float(value, name: str) -> float:
    """Return *value* as a finite float or raise a stable ``ValueError``."""
    if isinstance(value, (bool, str, bytes)):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number") from None
    if not isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def nonnegative_float(value, name: str) -> float:
    number = finite_float(value, name)
    if number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def positive_float(value, name: str) -> float:
    number = finite_float(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def unit_fraction(value, name: str = 'percent') -> float:
    number = finite_float(value, name)
    if number <= 0 or number > 1:
        raise ValueError(f"{name} must be in (0, 1]")
    return number


def unit_interval(value, name: str) -> float:
    """Return a finite ratio in ``[0, 1]``."""
    number = finite_float(value, name)
    if number < 0 or number > 1:
        raise ValueError(f"{name} must be in [0, 1]")
    return number


def fraction_below_one(value, name: str) -> float:
    """Return a finite ratio in ``[0, 1)``."""
    number = finite_float(value, name)
    if number < 0 or number >= 1:
        raise ValueError(f"{name} must be in [0, 1)")
    return number


def nonnegative_int(value, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative integer")
    if isinstance(value, Integral):
        number = int(value)
    else:
        numeric = finite_float(value, name)
        if not numeric.is_integer():
            raise ValueError(f"{name} must be a non-negative integer")
        number = int(numeric)
    if number < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return number


def positive_int(value, name: str = 'shares') -> int:
    number = nonnegative_int(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return number


def positive_product(left, right, name: str) -> float:
    """Multiply two values and require a finite, strictly positive result."""
    try:
        product = left * right
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be finite and positive") from None
    try:
        return positive_float(product, name)
    except ValueError:
        raise ValueError(f"{name} must be finite and positive") from None
