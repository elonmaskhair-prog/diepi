"""Construction-time frequency contract for the public cash engines."""

import pytest

from diepi.backtest.engine.backtest_engine import BacktestEngine
from diepi.backtest.engine.portfolio_engine import MINUTE_FREQS, PortfolioEngine


def _single(freq):
    return BacktestEngine(
        symbol="600000.SH",
        start_date="20240102",
        end_date="20240103",
        freq=freq,
    )


def _portfolio(freq):
    return PortfolioEngine(
        start_date="20240102",
        end_date="20240103",
        freq=freq,
    )


@pytest.mark.parametrize("freq", ("daily", "minute"))
def test_single_engine_accepts_only_its_declared_frequencies(freq):
    assert _single(freq).freq == freq


@pytest.mark.parametrize("freq", ("daily", *sorted(MINUTE_FREQS)))
def test_portfolio_engine_accepts_declared_frequencies(freq):
    assert _portfolio(freq).freq == freq


@pytest.mark.parametrize("factory", (_single, _portfolio))
@pytest.mark.parametrize("freq", (None, True, 1, ["daily"]))
def test_cash_engines_reject_non_string_frequency(factory, freq):
    with pytest.raises(TypeError, match="freq must be exactly str"):
        factory(freq)


@pytest.mark.parametrize("factory", (_single, _portfolio))
@pytest.mark.parametrize("freq", ("", "Daily", " daily", "weekly", "1h"))
def test_cash_engines_reject_unknown_frequency_without_fallback(factory, freq):
    with pytest.raises(ValueError, match="freq must"):
        factory(freq)
