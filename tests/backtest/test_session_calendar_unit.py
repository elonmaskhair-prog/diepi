"""Pure unit tests for effective-dated cash-market sessions."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone

import pytest

from diepi.backtest.rulebook import InstrumentKind, Venue
from diepi.backtest.session_calendar import (
    DEFAULT_SESSION_CALENDAR,
    DEFAULT_SESSION_RULES,
    SESSION_SNAPSHOT_HASH,
    SESSION_SOURCE_VERSION,
    SessionCalendar,
    SessionKind,
    SessionName,
    SessionRule,
    SessionRuleNotFoundError,
    SessionWindow,
    TimestampOutsideSessionError,
    normalize_session_date,
)


def _names(rule):
    return tuple(session.name for session in rule.sessions)


CONTINUOUS_NAMES = (
    SessionName.OPENING_AUCTION,
    SessionName.MORNING_CONTINUOUS,
    SessionName.AFTERNOON_CONTINUOUS,
)
AUCTION_NAMES = CONTINUOUS_NAMES + (SessionName.CLOSING_AUCTION,)


@pytest.mark.parametrize(
    ("symbol", "before", "effective", "before_names", "effective_names"),
    [
        (
            "600000.SH",
            "2018-08-19",
            "2018-08-20",
            CONTINUOUS_NAMES,
            AUCTION_NAMES,
        ),
        (
            "510300.SH",
            "2026-07-05",
            "2026-07-06",
            CONTINUOUS_NAMES,
            AUCTION_NAMES,
        ),
        (
            "501018.SH",
            "2026-07-05",
            "2026-07-06",
            CONTINUOUS_NAMES,
            AUCTION_NAMES,
        ),
    ],
)
def test_sse_session_changes_apply_on_exact_effective_date(
    symbol, before, effective, before_names, effective_names
):
    assert _names(DEFAULT_SESSION_CALENDAR.get_rule(symbol, before)) == before_names
    assert _names(DEFAULT_SESSION_CALENDAR.get_rule(symbol, effective)) == effective_names


@pytest.mark.parametrize(
    ("symbol", "date_value", "expected_kind", "expected_venue"),
    [
        ("000001.SZ", "2006-07-01", InstrumentKind.A_SHARE, Venue.SZSE),
        ("159915.SZ", "2006-07-01", InstrumentKind.ETF, Venue.SZSE),
        ("161725.SZ", "2006-07-01", InstrumentKind.LOF, Venue.SZSE),
        ("600000.SH", "2018-08-20", InstrumentKind.A_SHARE, Venue.SSE),
        ("510300.SH", "2026-07-06", InstrumentKind.ETF, Venue.SSE),
        ("501018.SH", "2026-07-06", InstrumentKind.LOF, Venue.SSE),
        ("830001.BJ", "2021-11-15", InstrumentKind.A_SHARE, Venue.BSE),
    ],
)
def test_supported_cash_families_resolve_to_explicit_rules(
    symbol, date_value, expected_kind, expected_venue
):
    rule = DEFAULT_SESSION_CALENDAR.get_rule(symbol, date_value)
    assert rule.instrument_kind is expected_kind
    assert rule.venue is expected_venue
    assert rule.source_version == SESSION_SOURCE_VERSION
    assert rule.snapshot_hash == SESSION_SNAPSHOT_HASH


@pytest.mark.parametrize(
    ("symbol", "date_value"),
    [
        ("000001.SZ", "2006-06-30"),
        ("159915.SZ", "2006-06-30"),
        ("600000.SH", "2006-06-30"),
        ("510300.SH", "2006-06-30"),
        ("830001.BJ", "2021-11-14"),
    ],
)
def test_dates_before_conservative_rule_coverage_fail_fast(symbol, date_value):
    with pytest.raises(SessionRuleNotFoundError, match="no effective session rule"):
        DEFAULT_SESSION_CALENDAR.get_rule(symbol, date_value)


@pytest.mark.parametrize(
    ("symbol", "date_value"),
    [
        ("000300.SH", "2024-01-02"),
        ("508000.SH", "2024-01-02"),
        ("IF2406.CFFEX", "2024-01-02"),
        ("580001.SH", "2024-01-02"),
        ("not-a-symbol", "2024-01-02"),
        ("000300", "2024-01-02"),
    ],
)
def test_unknown_or_unsupported_instrument_families_fail_fast(symbol, date_value):
    with pytest.raises(SessionRuleNotFoundError, match="no cash-session rules"):
        DEFAULT_SESSION_CALENDAR.get_rule(symbol, date_value)


@pytest.mark.parametrize(
    ("symbol", "stamp", "expected_name", "expected_kind"),
    [
        (
            "000001.SZ",
            "2024-01-02 09:30:00",
            SessionName.OPENING_AUCTION,
            SessionKind.OPENING_AUCTION,
        ),
        (
            "000001.SZ",
            "2024-01-02 09:31:00",
            SessionName.MORNING_CONTINUOUS,
            SessionKind.CONTINUOUS,
        ),
        (
            "600000.SH",
            "2018-08-19 14:57:00",
            SessionName.AFTERNOON_CONTINUOUS,
            SessionKind.CONTINUOUS,
        ),
        (
            "600000.SH",
            "2018-08-19 15:00:00",
            SessionName.AFTERNOON_CONTINUOUS,
            SessionKind.CONTINUOUS,
        ),
        (
            "600000.SH",
            "2018-08-20 14:56:00",
            SessionName.AFTERNOON_CONTINUOUS,
            SessionKind.CONTINUOUS,
        ),
        (
            "600000.SH",
            "2018-08-20 14:57:00",
            SessionName.AFTERNOON_CONTINUOUS,
            SessionKind.CONTINUOUS,
        ),
        (
            "600000.SH",
            "2018-08-20 14:58:00",
            SessionName.CLOSING_AUCTION,
            SessionKind.CLOSING_AUCTION,
        ),
        (
            "510300.SH",
            "2026-07-05 15:00:00",
            SessionName.AFTERNOON_CONTINUOUS,
            SessionKind.CONTINUOUS,
        ),
        (
            "510300.SH",
            "2026-07-06 14:57:00",
            SessionName.AFTERNOON_CONTINUOUS,
            SessionKind.CONTINUOUS,
        ),
        (
            "510300.SH",
            "2026-07-06 14:58:00",
            SessionName.CLOSING_AUCTION,
            SessionKind.CLOSING_AUCTION,
        ),
        (
            "000001.SZ",
            "2006-07-01 15:00:00",
            SessionName.CLOSING_AUCTION,
            SessionKind.CLOSING_AUCTION,
        ),
        (
            "830001.BJ",
            "2021-11-15 14:57:00",
            SessionName.AFTERNOON_CONTINUOUS,
            SessionKind.CONTINUOUS,
        ),
        (
            "830001.BJ",
            "2021-11-15 14:58:00",
            SessionName.CLOSING_AUCTION,
            SessionKind.CLOSING_AUCTION,
        ),
    ],
)
def test_boundary_timestamp_ownership_is_effective_dated(
    symbol, stamp, expected_name, expected_kind
):
    _, session = DEFAULT_SESSION_CALENDAR.session_for_timestamp(
        symbol, datetime.fromisoformat(stamp)
    )
    assert session.name is expected_name
    assert session.kind is expected_kind


@pytest.mark.parametrize(
    "stamp",
    [
        "2024-01-02 09:29:00",
        "2024-01-02 11:31:00",
        "2024-01-02 12:00:00",
        "2024-01-02 12:59:00",
        "2024-01-02 13:00:00",
        "2024-01-02 15:01:00",
        "2024-01-02 23:59:00",
    ],
)
def test_off_session_timestamp_fails_fast(stamp):
    with pytest.raises(TimestampOutsideSessionError, match="outside sessions"):
        DEFAULT_SESSION_CALENDAR.session_for_timestamp(
            "000001.SZ", datetime.fromisoformat(stamp)
        )


def test_timestamp_must_be_naive_and_minute_aligned():
    with pytest.raises(ValueError, match="timezone-naive"):
        DEFAULT_SESSION_CALENDAR.session_for_timestamp(
            "000001.SZ",
            datetime(2024, 1, 2, 9, 30, tzinfo=timezone.utc),
        )
    with pytest.raises(ValueError, match="whole minutes"):
        DEFAULT_SESSION_CALENDAR.session_for_timestamp(
            "000001.SZ", datetime(2024, 1, 2, 9, 30, 1)
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("20240102", "2024-01-02"),
        ("2024-01-02", "2024-01-02"),
        (datetime(2024, 1, 2, 13, 0), "2024-01-02"),
    ],
)
def test_session_dates_have_one_canonical_form(value, expected):
    assert normalize_session_date(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "2024/01/02", "2024-1-2", "20240230", 20240102, True, None],
)
def test_invalid_session_dates_are_rejected(value):
    with pytest.raises((TypeError, ValueError)):
        normalize_session_date(value)


def test_rule_metadata_is_complete_stable_and_serializable():
    assert len(SESSION_SNAPSHOT_HASH) == 64
    assert all(character in "0123456789abcdef" for character in SESSION_SNAPSHOT_HASH)
    payloads = [rule.to_dict() for rule in DEFAULT_SESSION_RULES]
    assert payloads
    for rule, payload in zip(DEFAULT_SESSION_RULES, payloads):
        assert payload["effective_from"] == rule.effective_from
        assert "effective_to" in payload
        assert payload["source_version"] == SESSION_SOURCE_VERSION
        assert payload["snapshot_hash"] == SESSION_SNAPSHOT_HASH
        assert payload["sessions"]


def test_rules_and_windows_are_frozen():
    rule = DEFAULT_SESSION_CALENDAR.get_rule("600000.SH", "2018-08-20")
    with pytest.raises(FrozenInstanceError):
        rule.effective_from = "2000-01-01"
    with pytest.raises(FrozenInstanceError):
        rule.sessions[0].start = "00:00"


def test_custom_calendar_rejects_gap_overlap_and_open_ended_successor():
    first = DEFAULT_SESSION_CALENDAR.get_rule("600000.SH", "2018-08-19")
    later = DEFAULT_SESSION_CALENDAR.get_rule("600000.SH", "2018-08-20")
    with pytest.raises(ValueError, match="contiguous"):
        SessionCalendar(
            rules=(first, replace(later, effective_from="2018-08-21"))
        )
    with pytest.raises(ValueError, match="open-ended"):
        SessionCalendar(
            rules=(
                replace(first, effective_to=None),
                later,
            )
        )


def test_touching_windows_require_unambiguous_boundary_ownership():
    morning = SessionWindow(
        SessionName.MORNING_CONTINUOUS,
        SessionKind.CONTINUOUS,
        "09:30",
        "11:30",
        0,
        True,
    )
    touching = SessionWindow(
        SessionName.AFTERNOON_CONTINUOUS,
        SessionKind.CONTINUOUS,
        "11:30",
        "15:00",
        1,
        True,
    )
    with pytest.raises(ValueError, match="cannot both own"):
        SessionRule(
            Venue.SSE,
            InstrumentKind.A_SHARE,
            (morning, touching),
            "2024-01-01",
            None,
            SESSION_SOURCE_VERSION,
            SESSION_SNAPSHOT_HASH,
        )
