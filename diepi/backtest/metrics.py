"""Single, deterministic metric implementation for all result surfaces.

The first observed close is compared with an explicit pre-run NAV baseline.
This prevents a first-day loss from disappearing from daily returns, Sharpe,
and maximum drawdown.  Close-to-close drawdown, causal prior-peak-to-low
drawdown, and the deliberately pessimistic same-day high-to-low upper bound
are kept as distinct metrics; callers must not relabel one as another.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from math import isclose, isfinite, sqrt
from numbers import Integral, Real
from statistics import stdev, pstdev
from typing import Iterable, Optional, Sequence, Tuple


def _finite_real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"{name} must be a real number")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _positive_real(value: object, name: str) -> float:
    result = _finite_real(value, name)
    if result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def _iso_date(value: object, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be exactly str")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must use valid YYYY-MM-DD") from None
    canonical = parsed.isoformat()
    if canonical != value:
        raise ValueError(f"{name} must use valid YYYY-MM-DD")
    return canonical


@dataclass(frozen=True)
class MetricConfig:
    """Versioned numerical assumptions shared by every engine/UI."""

    trading_days_per_year: int = 252
    risk_free_rate: float = 0.03
    return_std_ddof: int = 1
    schema_version: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.trading_days_per_year, bool) or not isinstance(
            self.trading_days_per_year, Integral
        ):
            raise TypeError("trading_days_per_year must be an integer")
        if self.trading_days_per_year <= 0:
            raise ValueError("trading_days_per_year must be positive")
        object.__setattr__(
            self,
            "trading_days_per_year",
            int(self.trading_days_per_year),
        )
        object.__setattr__(
            self,
            "risk_free_rate",
            _finite_real(self.risk_free_rate, "risk_free_rate"),
        )
        if type(self.return_std_ddof) is not int:
            raise TypeError("return_std_ddof must be exactly int")
        if self.return_std_ddof not in (0, 1):
            raise ValueError("return_std_ddof must be 0 or 1")
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be exactly int")
        if self.schema_version != 1:
            raise ValueError("unsupported MetricConfig schema_version")

    def assumptions(self) -> Tuple[Tuple[str, str], ...]:
        return (
            ("metrics.schema_version", str(self.schema_version)),
            ("metrics.trading_days_per_year", str(self.trading_days_per_year)),
            ("metrics.risk_free_rate", format(self.risk_free_rate, ".17g")),
            ("metrics.return_std_ddof", str(self.return_std_ddof)),
        )


def _annualized_return(
    terminal_growth: float,
    observation_count: int,
    trading_days_per_year: int,
) -> float:
    """Annualize a positive finite growth factor with a stable error type."""

    try:
        result = (
            terminal_growth
            ** (trading_days_per_year / observation_count)
            - 1.0
        )
    except (OverflowError, ValueError):
        raise ValueError("annual_return overflowed") from None
    if not isfinite(result):
        raise ValueError("annual_return overflowed")
    return result


def _expected_sharpe(
    daily_returns: Tuple[float, ...], config: MetricConfig
) -> Optional[float]:
    required = config.return_std_ddof + 1
    if len(daily_returns) < required:
        return None
    try:
        dispersion = (
            stdev(daily_returns)
            if config.return_std_ddof == 1
            else pstdev(daily_returns)
        )
    except OverflowError:
        raise ValueError("daily return dispersion overflowed") from None
    if not isfinite(dispersion):
        raise ValueError("daily return dispersion overflowed")
    if dispersion == 0:
        return None
    average = sum(daily_returns) / len(daily_returns)
    if not isfinite(average):
        raise ValueError("average daily return overflowed")
    result = (
        (average - config.risk_free_rate / config.trading_days_per_year)
        / dispersion
        * sqrt(config.trading_days_per_year)
    )
    if not isfinite(result):
        raise ValueError("sharpe_ratio overflowed")
    return result


def _same_metric(actual: float, expected: float) -> bool:
    return isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-12)


@dataclass(frozen=True)
class MetricResult:
    """Immutable metrics plus sufficient vectors for deterministic auditing.

    ``dates`` and ``config`` are optional only for compatibility with callers
    that manually constructed the original result shape.  ``MetricEngine``
    always supplies both, making its total return, annual return, Sharpe, and
    drawdown maxima independently recomputable from this value.
    """

    total_return: float
    annual_return: float
    sharpe_ratio: Optional[float]
    max_drawdown_close_nav: float
    max_drawdown_intraday_low_nav: Optional[float]
    max_drawdown_intraday_high_to_low: Optional[float]
    daily_returns: Tuple[float, ...]
    close_drawdowns: Tuple[float, ...]
    causal_intraday_low_drawdowns: Optional[Tuple[float, ...]]
    intraday_high_to_low_drawdowns: Optional[Tuple[float, ...]]
    dates: Optional[Tuple[str, ...]] = None
    config: Optional[MetricConfig] = None

    def __post_init__(self) -> None:
        for name in (
            "total_return",
            "annual_return",
            "max_drawdown_close_nav",
        ):
            object.__setattr__(
                self, name, _finite_real(getattr(self, name), name)
            )
        for name in (
            "sharpe_ratio",
            "max_drawdown_intraday_low_nav",
            "max_drawdown_intraday_high_to_low",
        ):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _finite_real(value, name))

        for name in ("daily_returns", "close_drawdowns"):
            values = getattr(self, name)
            if type(values) is not tuple:
                raise TypeError(f"{name} must be exactly tuple")
            object.__setattr__(
                self,
                name,
                tuple(
                    _finite_real(value, f"{name}[{index}]")
                    for index, value in enumerate(values)
                ),
            )
        for name in (
            "causal_intraday_low_drawdowns",
            "intraday_high_to_low_drawdowns",
        ):
            values = getattr(self, name)
            if values is not None:
                if type(values) is not tuple:
                    raise TypeError(f"{name} must be exactly tuple or None")
                object.__setattr__(
                    self,
                    name,
                    tuple(
                        _finite_real(value, f"{name}[{index}]")
                        for index, value in enumerate(values)
                    ),
                )

        count = len(self.daily_returns)
        if count == 0:
            raise ValueError("metric audit vectors must not be empty")
        if len(self.close_drawdowns) != count:
            raise ValueError(
                "close_drawdowns length must match daily_returns"
            )
        for name in (
            "causal_intraday_low_drawdowns",
            "intraday_high_to_low_drawdowns",
        ):
            values = getattr(self, name)
            if values is not None and len(values) != count:
                raise ValueError(f"{name} length must match daily_returns")

        if self.dates is not None:
            if type(self.dates) is not tuple:
                raise TypeError("dates must be exactly tuple or None")
            canonical_dates = tuple(
                _iso_date(value, f"dates[{index}]")
                for index, value in enumerate(self.dates)
            )
            if len(canonical_dates) != count:
                raise ValueError("dates length must match daily_returns")
            if tuple(sorted(canonical_dates)) != canonical_dates:
                raise ValueError("dates must be strictly increasing")
            if len(set(canonical_dates)) != len(canonical_dates):
                raise ValueError("dates must be unique")
            object.__setattr__(self, "dates", canonical_dates)

        if self.config is not None and type(self.config) is not MetricConfig:
            raise TypeError("config must be exactly MetricConfig or None")

        for index, value in enumerate(self.daily_returns):
            if value <= -1.0:
                raise ValueError(
                    f"daily_returns[{index}] must be greater than -1"
                )
        for name in (
            "close_drawdowns",
            "causal_intraday_low_drawdowns",
            "intraday_high_to_low_drawdowns",
        ):
            values = getattr(self, name)
            if values is not None:
                for index, value in enumerate(values):
                    if not 0.0 <= value <= 1.0:
                        raise ValueError(
                            f"{name}[{index}] must be within [0, 1]"
                        )
        for name in (
            "max_drawdown_close_nav",
            "max_drawdown_intraday_low_nav",
            "max_drawdown_intraday_high_to_low",
        ):
            value = getattr(self, name)
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1]")

        if self.total_return <= -1.0:
            raise ValueError("total_return must be greater than -1")
        if self.annual_return < -1.0:
            raise ValueError("annual_return must not be below -1")

        growth = 1.0
        for value in self.daily_returns:
            growth *= 1.0 + value
            if not isfinite(growth) or growth <= 0.0:
                raise ValueError("daily return growth overflowed or underflowed")
        expected_total = growth - 1.0
        if not _same_metric(self.total_return, expected_total):
            raise ValueError("total_return must match daily_returns")

        vector_pairs = (
            (
                "max_drawdown_close_nav",
                "close_drawdowns",
            ),
            (
                "max_drawdown_intraday_low_nav",
                "causal_intraday_low_drawdowns",
            ),
            (
                "max_drawdown_intraday_high_to_low",
                "intraday_high_to_low_drawdowns",
            ),
        )
        for maximum_name, vector_name in vector_pairs:
            maximum = getattr(self, maximum_name)
            vector = getattr(self, vector_name)
            if (maximum is None) != (vector is None):
                raise ValueError(
                    f"{maximum_name} and {vector_name} must both be present or None"
                )
            if vector is not None and not _same_metric(maximum, max(vector)):
                raise ValueError(f"{maximum_name} must equal max({vector_name})")
        if (
            self.intraday_high_to_low_drawdowns is not None
            and self.causal_intraday_low_drawdowns is None
        ):
            raise ValueError(
                "intraday high-to-low audit requires causal low audit"
            )

        if self.config is not None:
            expected_annual = _annualized_return(
                growth,
                count,
                self.config.trading_days_per_year,
            )
            if not _same_metric(self.annual_return, expected_annual):
                raise ValueError(
                    "annual_return must match daily_returns and config"
                )
            expected_sharpe = _expected_sharpe(
                self.daily_returns, self.config
            )
            if (self.sharpe_ratio is None) != (expected_sharpe is None):
                raise ValueError(
                    "sharpe_ratio nullability must match daily_returns and config"
                )
            if (
                expected_sharpe is not None
                and not _same_metric(self.sharpe_ratio, expected_sharpe)
            ):
                raise ValueError(
                    "sharpe_ratio must match daily_returns and config"
                )


@dataclass(frozen=True)
class TradeRoundMetrics:
    """Closed-round win statistics; no closed rounds means ``win_rate=None``."""

    open_round_count: int
    closed_round_count: int
    winning_round_count: int
    win_rate: Optional[float]

    def __post_init__(self) -> None:
        for name in (
            "open_round_count",
            "closed_round_count",
            "winning_round_count",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"{name} must be exactly int")
            if value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.winning_round_count > self.closed_round_count:
            raise ValueError("winning rounds cannot exceed closed rounds")
        expected = (
            None
            if self.closed_round_count == 0
            else self.winning_round_count / self.closed_round_count
        )
        if self.win_rate != expected:
            raise ValueError("win_rate must match closed-round counts")


def calculate_trade_round_metrics(
    realized_pnls: Iterable[float], *, open_round_count: int = 0
) -> TradeRoundMetrics:
    if type(open_round_count) is not int:
        raise TypeError("open_round_count must be exactly int")
    if open_round_count < 0:
        raise ValueError("open_round_count must be non-negative")
    pnls = tuple(
        _finite_real(value, f"realized_pnls[{index}]")
        for index, value in enumerate(realized_pnls)
    )
    wins = sum(value > 0 for value in pnls)
    return TradeRoundMetrics(
        open_round_count=open_round_count,
        closed_round_count=len(pnls),
        winning_round_count=wins,
        win_rate=(wins / len(pnls)) if pnls else None,
    )


def _validated_navs(values: Sequence[float], name: str) -> Tuple[float, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be a sequence")
    return tuple(
        _positive_real(value, f"{name}[{index}]")
        for index, value in enumerate(values)
    )


class MetricEngine:
    """Pure calculator for NAV-based metrics."""

    def __init__(self, config: MetricConfig = MetricConfig()) -> None:
        if type(config) is not MetricConfig:
            raise TypeError("config must be exactly MetricConfig")
        self.config = config

    def calculate(
        self,
        *,
        initial_nav: float,
        dates: Sequence[str],
        close_nav: Sequence[float],
        intraday_low_nav: Optional[Sequence[float]] = None,
        intraday_high_nav: Optional[Sequence[float]] = None,
    ) -> MetricResult:
        initial = _positive_real(initial_nav, "initial_nav")
        if isinstance(dates, (str, bytes)) or not isinstance(dates, Sequence):
            raise TypeError("dates must be a sequence")
        canonical_dates = tuple(
            _iso_date(value, f"dates[{index}]")
            for index, value in enumerate(dates)
        )
        if tuple(sorted(canonical_dates)) != canonical_dates:
            raise ValueError("dates must be strictly increasing")
        if len(set(canonical_dates)) != len(canonical_dates):
            raise ValueError("dates must be unique")

        closes = _validated_navs(close_nav, "close_nav")
        if len(closes) != len(canonical_dates):
            raise ValueError("close_nav length must match dates")
        if not closes:
            raise ValueError("at least one close NAV observation is required")

        lows = None
        if intraday_low_nav is not None:
            lows = _validated_navs(intraday_low_nav, "intraday_low_nav")
            if len(lows) != len(closes):
                raise ValueError("intraday_low_nav length must match dates")
            if any(low > close for low, close in zip(lows, closes)):
                raise ValueError("intraday_low_nav cannot exceed close_nav")

        highs = None
        if intraday_high_nav is not None:
            highs = _validated_navs(intraday_high_nav, "intraday_high_nav")
            if len(highs) != len(closes):
                raise ValueError("intraday_high_nav length must match dates")
            if any(high < close for high, close in zip(highs, closes)):
                raise ValueError("intraday_high_nav cannot be below close_nav")
        if highs is not None and lows is None:
            raise ValueError(
                "intraday_high_nav requires intraday_low_nav for high-to-low drawdown"
            )
        if highs is not None and any(low > high for low, high in zip(lows, highs)):
            raise ValueError("intraday_low_nav cannot exceed intraday_high_nav")

        daily_returns = []
        previous = initial
        for close in closes:
            daily_return = close / previous - 1.0
            if not isfinite(daily_return) or daily_return <= -1.0:
                raise ValueError("daily return overflowed or underflowed")
            daily_returns.append(daily_return)
            previous = close

        close_drawdowns = []
        causal_low_drawdowns = [] if lows is not None else None
        peak = initial
        for index, close in enumerate(closes):
            prior_peak = peak
            peak = max(peak, close)
            close_drawdowns.append(max(0.0, (peak - close) / peak))
            if lows is not None:
                causal_low_drawdowns.append(
                    max(0.0, (prior_peak - lows[index]) / prior_peak)
                )

        high_low_drawdowns = None
        if highs is not None:
            high_low_drawdowns = [
                max(0.0, (high - low) / high)
                for high, low in zip(highs, lows)
            ]

        terminal_growth = closes[-1] / initial
        if not isfinite(terminal_growth) or terminal_growth <= 0.0:
            raise ValueError("total_return overflowed or underflowed")
        total_return = terminal_growth - 1.0
        annual_return = _annualized_return(
            terminal_growth,
            len(closes),
            self.config.trading_days_per_year,
        )
        daily_returns_tuple = tuple(daily_returns)
        sharpe = _expected_sharpe(daily_returns_tuple, self.config)

        low_tuple = (
            tuple(causal_low_drawdowns)
            if causal_low_drawdowns is not None else None
        )
        high_low_tuple = (
            tuple(high_low_drawdowns)
            if high_low_drawdowns is not None else None
        )
        return MetricResult(
            total_return=total_return,
            annual_return=annual_return,
            sharpe_ratio=sharpe,
            max_drawdown_close_nav=max(close_drawdowns),
            max_drawdown_intraday_low_nav=(
                max(causal_low_drawdowns)
                if causal_low_drawdowns is not None else None
            ),
            max_drawdown_intraday_high_to_low=(
                max(high_low_drawdowns)
                if high_low_drawdowns is not None else None
            ),
            daily_returns=daily_returns_tuple,
            close_drawdowns=tuple(close_drawdowns),
            causal_intraday_low_drawdowns=low_tuple,
            intraday_high_to_low_drawdowns=high_low_tuple,
            dates=canonical_dates,
            config=self.config,
        )


__all__ = [
    "MetricConfig",
    "MetricEngine",
    "MetricResult",
    "TradeRoundMetrics",
    "calculate_trade_round_metrics",
]
