"""
回测引擎模块
"""

from .context import Context
from .backtest_engine import BacktestEngine, BacktestResult
from .parallel_runner import ParallelRunner, ParallelResult
from .portfolio_context import PortfolioContext
from .portfolio_engine import PortfolioEngine, PortfolioResult
from .attribution import AttributionAnalyzer, calculate_attribution

__all__ = [
    'Context',
    'BacktestEngine',
    'BacktestResult',
    'ParallelRunner',
    'ParallelResult',
    'PortfolioContext',
    'PortfolioEngine',
    'PortfolioResult',
    'AttributionAnalyzer',
    'calculate_attribution',
]
