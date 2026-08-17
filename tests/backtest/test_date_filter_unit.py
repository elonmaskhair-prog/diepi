"""Pure unit tests for one-sided DataProvider date filters."""

import pandas as pd
import pytest

from diepi.backtest.data.data_provider import DataProvider
from diepi.backtest.data.exceptions import ParameterError


class _CacheStub:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def _copy(self):
        return self.frame.copy()

    def get_data(self, category, symbol):
        return self._copy()

    def get_moneyflow(self, symbol):
        return self._copy()

    def get_margin(self, symbol):
        return self._copy()

    def get_basic(self, symbol):
        return self._copy()

    def get_cyq(self, symbol):
        frame = self._copy()
        frame["price"] = frame["close"]
        frame["percent"] = [0.2, 0.3, 0.5]
        return frame


@pytest.fixture
def provider():
    frame = pd.DataFrame({
        "trade_date": ["20240102", "20240103", "20240104"],
        "open": [10.0, 11.0, 12.0],
        "close": [10.5, 11.5, 12.5],
    })
    result = DataProvider()
    result._cache = _CacheStub(frame)
    # Pure filter tests do not load a real trade calendar.  Keep their default
    # boundary after every synthetic row; dedicated tests below tighten it.
    result._get_default_end_date = lambda: "99991231"
    return result


@pytest.mark.parametrize(
    "getter",
    [
        lambda dp: dp.get_daily("000001.SZ", start="2024-01-03"),
        lambda dp: dp.get_moneyflow("000001.SZ", start="2024-01-03"),
        lambda dp: dp.get_margin("000001.SZ", start="2024-01-03"),
        lambda dp: dp.get_basic("000001.SZ", start="2024-01-03"),
    ],
)
def test_start_only_filter_applies_to_all_public_date_apis(provider, getter):
    result = getter(provider)

    assert list(result.index) == ["20240103", "20240104"]


def test_private_filter_start_only_does_not_leak_earlier_rows(provider):
    frame = provider._cache.frame

    result = provider._filter_by_date_params(
        frame, "trade_date", start="20240103", end=None, count=None
    )

    assert result["trade_date"].tolist() == ["20240103", "20240104"]


@pytest.mark.parametrize("count", [None, 10])
def test_start_branches_cannot_cross_default_causal_boundary(
    provider, monkeypatch, count
):
    provider._cache.frame = pd.DataFrame(
        {
            "trade_date": ["20240102", "20240103", "20240104"],
            "open": [10.0, 11.0, 12.0],
            "close": [10.5, 11.5, 12.5],
        }
    )
    monkeypatch.setattr(
        provider, "_get_default_end_date", lambda: "20240103"
    )

    result = provider.get_daily(
        "000001.SZ", start="20240102", count=count
    )

    assert result.index.tolist() == ["20240102", "20240103"]


def test_cyq_start_branch_cannot_cross_default_causal_boundary(
    provider, monkeypatch
):
    monkeypatch.setattr(
        provider, "_get_default_end_date", lambda: "20240103"
    )

    result = provider.get_cyq("000001.SZ", start="20240102", count=10)

    assert result["trade_date"].drop_duplicates().tolist() == [
        "20240102",
        "20240103",
    ]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"end": "20240103"}, ["20240102", "20240103"]),
        ({"start": "20240103"}, ["20240103", "20240104"]),
        ({"start": "20240102", "count": 2}, ["20240102", "20240103"]),
        ({"end": "20240104", "count": 2}, ["20240103", "20240104"]),
    ],
)
def test_cyq_uses_complete_date_parameter_matrix(provider, kwargs, expected):
    result = provider.get_cyq("000001.SZ", **kwargs)

    assert result["trade_date"].drop_duplicates().tolist() == expected


def test_cyq_rejects_trade_date_mixed_with_range(provider):
    with pytest.raises(ParameterError, match="mutually exclusive"):
        provider.get_cyq(
            "000001.SZ", trade_date="20240103", end="20240104"
        )
