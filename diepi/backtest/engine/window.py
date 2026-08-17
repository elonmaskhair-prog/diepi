"""Causal run-window bounds shared by cash backtest engines."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Iterable, Optional, Tuple


_SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
_CASH_CLOSE = time(15, 0)


def _compact_date(value: object, name: str) -> str:
    text = str(value).strip().replace("-", "").replace("/", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"{name} must be YYYYMMDD")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        raise ValueError(f"{name} must be a valid date") from None
    return text


def latest_closed_cash_date(now: Optional[datetime] = None) -> str:
    """Return the latest wall-clock date whose 15:00 cash close has passed."""
    if now is None:
        local = datetime.now(_SHANGHAI)
    elif now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    else:
        local = now.astimezone(_SHANGHAI)
    cutoff = local.date()
    if local.time() < _CASH_CLOSE:
        cutoff -= timedelta(days=1)
    return cutoff.strftime("%Y%m%d")


@dataclass(frozen=True)
class WindowBound:
    trade_days: Tuple[str, ...]
    requested_end: str
    cutoff: str
    wall_clock_cutoff: str
    data_cutoff: Optional[str]
    truncated: bool

    @property
    def warning_message(self) -> str:
        source = "latest completed market close"
        if self.data_cutoff is not None and self.data_cutoff == self.cutoff:
            source = "latest available market-data date"
        return (
            f"Requested end {self.requested_end} was truncated to "
            f"{self.cutoff} ({source}); future/unavailable sessions were not "
            "treated as flat-cash observations"
        )


def bound_cash_trade_days(
    trade_days: Iterable[object],
    *,
    requested_end: object,
    data_dates: Iterable[object] = (),
    now: Optional[datetime] = None,
) -> WindowBound:
    """Clamp a scheduled calendar to completed sessions and observed data."""
    requested = _compact_date(requested_end, "requested_end")
    canonical_days = tuple(
        sorted({_compact_date(value, "trade_day") for value in trade_days})
    )
    wall_cutoff = latest_closed_cash_date(now)
    canonical_data = tuple(
        sorted({_compact_date(value, "data_date") for value in data_dates})
    )
    data_cutoff = canonical_data[-1] if canonical_data else None
    raw_cutoff = min(
        value for value in (requested, wall_cutoff, data_cutoff)
        if value is not None
    )
    bounded = tuple(day for day in canonical_days if day <= raw_cutoff)
    truncated = requested > raw_cutoff or len(bounded) != len(canonical_days)
    cutoff = bounded[-1] if bounded else raw_cutoff
    return WindowBound(
        trade_days=bounded,
        requested_end=requested,
        cutoff=cutoff,
        wall_clock_cutoff=wall_cutoff,
        data_cutoff=data_cutoff,
        truncated=truncated,
    )


__all__ = ["WindowBound", "bound_cash_trade_days", "latest_closed_cash_date"]
