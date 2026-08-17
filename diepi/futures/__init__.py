"""Futures backtest framework."""
from .engine import FuturesEngine
from .result import FuturesResult, FuturesValidationError
from .contract import ContractSelector, ContractSelection
from .cost import CostModel
from .combiner import FuturesCombiner
from .constants import PRODUCT_SPECS, get_spec, get_multiplier, get_margin_rate

__all__ = [
    "FuturesEngine", "FuturesResult", "FuturesValidationError",
    "ContractSelector", "ContractSelection", "CostModel",
    "FuturesCombiner", "PRODUCT_SPECS", "get_spec", "get_multiplier", "get_margin_rate",
]
