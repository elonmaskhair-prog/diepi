"""Causal end-of-window guards for scheduled trading calendars."""

from datetime import datetime, timedelta, timezone

import pytest

from diepi.backtest.engine.window import (
    bound_cash_trade_days,
    latest_closed_cash_date,
)


SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def test_current_session_is_not_closed_before_cash_close():
    before = datetime(2026, 8, 7, 14, 59, tzinfo=SHANGHAI)
    after = datetime(2026, 8, 7, 15, 0, tzinfo=SHANGHAI)

    assert latest_closed_cash_date(before) == "20260806"
    assert latest_closed_cash_date(after) == "20260807"


def test_future_scheduled_sessions_are_truncated_with_warning_evidence():
    result = bound_cash_trade_days(
        ["20260806", "20260807", "20260810", "20260811"],
        requested_end="20260811",
        now=datetime(2026, 8, 9, 12, 0, tzinfo=SHANGHAI),
    )

    assert result.trade_days == ("20260806", "20260807")
    assert result.cutoff == "20260807"
    assert result.truncated
    assert "flat-cash" in result.warning_message


def test_stale_data_end_is_a_second_independent_cutoff():
    result = bound_cash_trade_days(
        ["20260803", "20260804", "20260805", "20260806", "20260807"],
        requested_end="20260807",
        data_dates=["20260803", "20260804", "20260805"],
        now=datetime(2026, 8, 9, 12, 0, tzinfo=SHANGHAI),
    )

    assert result.trade_days == ("20260803", "20260804", "20260805")
    assert result.cutoff == "20260805"
    assert "market-data" in result.warning_message


def test_historical_window_is_unchanged_and_dates_are_strict():
    result = bound_cash_trade_days(
        ["20240102", "20240103"],
        requested_end="20240103",
        data_dates=["20240102", "20240103"],
        now=datetime(2026, 8, 9, 12, 0, tzinfo=SHANGHAI),
    )
    assert result.trade_days == ("20240102", "20240103")
    assert not result.truncated

    with pytest.raises(ValueError, match="valid date"):
        bound_cash_trade_days([], requested_end="20260230")
