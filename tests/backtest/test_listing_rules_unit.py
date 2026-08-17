"""Synthetic tests for IPO limit-exemption windows."""

import pandas as pd
import pytest

from diepi.backtest.engine.listing_rules import compute_limit_exempt_dates


class _ProviderStub:
    def __init__(self, list_date="20240102"):
        self.list_date = list_date
        self.daily_calls = []

    def get_stock_info(self, symbol):
        return {"list_date": self.list_date}

    def get_daily(self, symbol, start, end, price_mode):
        self.daily_calls.append((symbol, start, end, price_mode))
        dates = ["20240102", "20240103", "20240104", "20240105", "20240108"]
        return pd.DataFrame({"close": [10.0] * len(dates)}, index=dates)


@pytest.mark.parametrize(
    "backtest_start, expected",
    [
        ("2024-01-03", {"20240103", "20240104", "20240105", "20240108"}),
        ("20240104", {"20240104", "20240105", "20240108"}),
        (20240105, {"20240105", "20240108"}),
        (pd.Timestamp("2024-01-08"), {"20240108"}),
    ],
)
def test_backtest_starting_during_listing_window_keeps_remaining_exempt_days(
        backtest_start, expected):
    provider = _ProviderStub()

    result = compute_limit_exempt_dates(
        provider, ["600001.SH"], backtest_start=backtest_start
    )

    assert result["600001.SH"] == expected


@pytest.mark.parametrize(
    "list_date",
    ["2024-01-02", 20240102, 20240102.0, pd.Timestamp("2024-01-02")],
)
def test_listing_date_metadata_is_normalized(list_date):
    provider = _ProviderStub(list_date=list_date)

    result = compute_limit_exempt_dates(
        provider, ["600001.SH"], backtest_start="20240103"
    )

    assert result["600001.SH"] == {
        "20240103", "20240104", "20240105", "20240108"
    }


def test_start_after_actual_exemption_window_returns_no_symbol():
    provider = _ProviderStub()

    result = compute_limit_exempt_dates(
        provider, ["600001.SH"], backtest_start="20240109"
    )

    assert result == {}


def test_symbol_avoids_data_read_when_conservative_search_window_is_before_start():
    provider = _ProviderStub()

    result = compute_limit_exempt_dates(
        provider, ["600001.SH"], backtest_start="20240301"
    )

    assert result == {}
    assert provider.daily_calls == []


def test_exchange_fund_does_not_enter_stock_ipo_exemption_path():
    provider = _ProviderStub()

    result = compute_limit_exempt_dates(
        provider, ["510300.SH"], backtest_start="20240103"
    )

    assert result == {}
    assert provider.daily_calls == []
