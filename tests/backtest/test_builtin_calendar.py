"""Contracts for diePi's bundled independent A-share market clock."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from diepi.backtest.data.calendar import (
    BUILTIN_CALENDAR_CONTENT_SHA256,
    BUILTIN_CALENDAR_END,
    BUILTIN_CALENDAR_ID,
    BUILTIN_CALENDAR_START,
    builtin_calendar_identity,
    load_builtin_trade_calendar,
)
from diepi.backtest.data.data_provider import DataProvider
from diepi.backtest.data.exceptions import DataNotFoundError
from diepi.backtest.data.validation_service import (
    DataValidationScope,
    _validate_calendar,
)
from diepi.backtest.outcome import (
    OutcomeTracker,
    add_trade_calendar_assumptions,
)
from diepi.backtest.result_contract import ResultStatus
from diepi.commands.doctor import run_doctor


OFFICIAL_2026_WEEKDAY_CLOSURES = {
    "20260101",
    "20260102",
    "20260216",
    "20260217",
    "20260218",
    "20260219",
    "20260220",
    "20260223",
    "20260406",
    "20260501",
    "20260504",
    "20260505",
    "20260619",
    "20260925",
    "20261001",
    "20261002",
    "20261005",
    "20261006",
    "20261007",
}


def _write_local_override(root: Path, dates, statuses) -> Path:
    target = root / "parquet/metadata/common/trade_cal.parquet"
    target.parent.mkdir(parents=True)
    pd.DataFrame(
        {"cal_date": tuple(dates), "is_open": tuple(statuses)}
    ).to_parquet(target, index=False)
    return target


def test_bundled_calendar_has_stable_identity_and_full_2026_schedule():
    frame = load_builtin_trade_calendar()
    identity = builtin_calendar_identity()

    assert frame["cal_date"].iloc[0] == BUILTIN_CALENDAR_START
    assert frame["cal_date"].iloc[-1] == BUILTIN_CALENDAR_END
    assert identity.calendar_id == BUILTIN_CALENDAR_ID
    assert identity.content_sha256 == BUILTIN_CALENDAR_CONTENT_SHA256
    assert identity.source == "bundled"
    assert identity.rows == 6209
    assert identity.open_days == 4128

    dates = pd.to_datetime(frame["cal_date"], format="%Y%m%d")
    weekday_closures = set(
        frame.loc[
            dates.dt.year.eq(2026)
            & dates.dt.dayofweek.lt(5)
            & frame["is_open"].eq(0),
            "cal_date",
        ].astype(str)
    )
    assert weekday_closures == OFFICIAL_2026_WEEKDAY_CLOSURES
    assert int(
        frame.loc[dates.dt.year.eq(2026), "is_open"].eq(1).sum()
    ) == 242


def test_missing_local_calendar_uses_bundled_fallback_and_fails_outside_it(
    tmp_path,
):
    provider = DataProvider(data_root=tmp_path)

    assert provider.is_trade_day("20260105") is True
    assert provider.is_trade_day("20260925") is False
    assert provider.trade_calendar_identity == builtin_calendar_identity()
    with pytest.raises(DataNotFoundError, match="coverage does not cover"):
        provider.get_trade_days_between("20261231", "20270101")


def test_local_calendar_is_a_complete_override_never_a_partial_merge(tmp_path):
    _write_local_override(
        tmp_path,
        ("20240101", "20240102", "20240103"),
        (0, 0, 1),
    )
    provider = DataProvider(data_root=tmp_path)

    # 2024-01-02 is open in the bundled schedule, proving local precedence.
    assert provider.is_trade_day("20240102") is False
    assert provider.is_trade_day("20240103") is True
    identity = provider.trade_calendar_identity
    assert identity.source == "local_override"
    assert identity.calendar_id.startswith("local-override-")
    assert identity.coverage_start == "20240101"
    assert identity.coverage_end == "20240103"
    with pytest.raises(DataNotFoundError, match="source=local_override"):
        provider.is_trade_day("20240104")


def test_invalid_local_override_fails_closed_instead_of_using_bundled(tmp_path):
    path = _write_local_override(
        tmp_path,
        ("20240101", "20240103"),
        (0, 1),
    )
    provider = DataProvider(data_root=tmp_path)

    with pytest.raises(
        DataNotFoundError, match="override failed strict validation"
    ):
        provider.is_trade_day("20240102")

    scope = DataValidationScope(
        symbols=("510300.SH",),
        start_date="20240101",
        end_date="20240103",
        price_mode="raw",
    )
    report, _open_days, issues = _validate_calendar(path, scope)
    assert report["source"] == "local_override"
    assert report["status"] == "fail"
    assert "TRADE_CALENDAR_SCOPE_UNPROVEN" in {
        issue.code for issue in issues
    }


def test_validation_reports_calendar_source_identity_and_scope_failure(tmp_path):
    scope = DataValidationScope(
        symbols=("510300.SH",),
        start_date="20260924",
        end_date="20261008",
        price_mode="raw",
    )
    result, open_days, issues = _validate_calendar(
        tmp_path / "parquet/metadata/common/trade_cal.parquet", scope
    )

    assert issues == []
    assert result["status"] == "pass"
    assert result["source"] == "bundled"
    assert result["calendar_id"] == BUILTIN_CALENDAR_ID
    assert result["content_sha256"] == BUILTIN_CALENDAR_CONTENT_SHA256
    assert open_days == frozenset(
        {
            "20260924",
            "20260928",
            "20260929",
            "20260930",
            "20261008",
        }
    )

    outside = DataValidationScope(
        symbols=("510300.SH",),
        start_date="20261231",
        end_date="20270104",
        price_mode="raw",
    )
    failed, _open_days, outside_issues = _validate_calendar(
        tmp_path / "parquet/metadata/common/trade_cal.parquet", outside
    )
    assert failed["status"] == "fail"
    assert failed["source"] == "bundled"
    assert "TRADE_CALENDAR_SCOPE_UNPROVEN" in {
        issue.code for issue in outside_issues
    }


def test_result_contract_assumptions_record_the_selected_calendar(tmp_path):
    provider = DataProvider(data_root=tmp_path)
    tracker = OutcomeTracker(
        expected_observation_ids=(), coverage_unit="portfolio_trade_day"
    )

    assert add_trade_calendar_assumptions(tracker, provider) is True
    contract = tracker.finalize(
        ResultStatus.INVALID,
        reason_code="NO_SCOPE",
        reason_message="test scope is intentionally empty",
    )
    assumptions = {item.key: item.value for item in contract.assumptions}
    assert assumptions["calendar.source"] == "bundled"
    assert assumptions["calendar.id"] == BUILTIN_CALENDAR_ID
    assert assumptions["calendar.content_sha256"] == (
        BUILTIN_CALENDAR_CONTENT_SHA256
    )
    assert assumptions["calendar.coverage_start"] == BUILTIN_CALENDAR_START
    assert assumptions["calendar.coverage_end"] == BUILTIN_CALENDAR_END


def test_doctor_accepts_bundled_calendar_when_local_override_is_absent(tmp_path):
    (tmp_path / "parquet/timeseries").mkdir(parents=True)
    report = run_doctor(
        data_root=tmp_path,
        results_root=tmp_path / "results",
        check_gui=False,
    )
    check = next(item for item in report.checks if item.name == "trade_calendar")

    assert check.status == "pass"
    assert "bundled" in check.message
    assert BUILTIN_CALENDAR_ID in check.value
