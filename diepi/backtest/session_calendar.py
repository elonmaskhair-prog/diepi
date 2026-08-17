"""Effective-dated cash-market trading sessions.

The calendar is intentionally independent from data providers and engines.  It
answers only which intraday session owns a timestamp for a classified security.
It does not attempt to be a holiday calendar.

Built-in historical coverage is conservative: SSE and SZSE rules start on
2006-07-01, while BSE rules start on its first covered trading regime,
2021-11-15.  Requests before those dates fail instead of inheriting a modern
session layout.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
import hashlib
import json
import re
from typing import Any, Dict, Optional, Sequence, Tuple

from .rulebook import DEFAULT_RULEBOOK, InstrumentKind, RuleBook, Venue


SESSION_SOURCE_VERSION = "cn-cash-sessions-v3-20260809"

_SNAPSHOT_SPEC = {
    "coverage_floor": {"SSE": "2006-07-01", "SZSE": "2006-07-01"},
    "SZSE": {"closing_auction_from": "2006-07-01"},
    "SSE_A_SHARE": {"closing_auction_from": "2018-08-20"},
    "SSE_ETF_LOF": {"closing_auction_from": "2026-07-06"},
    "BSE_A_SHARE": {"closing_auction_from": "2021-11-15"},
    # Vendor minute bars in diepi use the exact 09:30 stamp as the
    # opening-auction observation.  It is therefore an independent execution
    # window and must never be grouped with 09:31+ continuous observations.
    "opening_auction_bar": "09:30",
    # Source bars use minute-end timestamps: the first afternoon continuous
    # bar is stamped 13:01, not 13:00.
    "continuous": ["09:31-11:30", "13:01-15:00"],
    "closing_auction_layout": [
        "09:30",
        "09:31-11:30",
        "13:01-14:57",
        "14:58-15:00",
    ],
}
SESSION_SNAPSHOT_HASH = hashlib.sha256(
    json.dumps(
        _SNAPSHOT_SPEC,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
).hexdigest()

_DATE_COMPACT_RE = re.compile(r"^\d{8}$")
_DATE_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class SessionKind(str, Enum):
    """Semantic phase of an exchange session."""

    OPENING_AUCTION = "opening_auction"
    CONTINUOUS = "continuous"
    CLOSING_AUCTION = "closing_auction"


class SessionName(str, Enum):
    """Stable names used in grouping and audit output."""

    OPENING_AUCTION = "opening_auction"
    MORNING_CONTINUOUS = "morning_continuous"
    AFTERNOON_CONTINUOUS = "afternoon_continuous"
    CLOSING_AUCTION = "closing_auction"


class SessionCalendarError(ValueError):
    """Base error for an unsupported or invalid session lookup."""


class SessionRuleNotFoundError(SessionCalendarError):
    """Raised when no effective rule covers an instrument and date."""


class TimestampOutsideSessionError(SessionCalendarError):
    """Raised when a timestamp is outside all effective trading sessions."""


def normalize_session_date(value: Any, field: str = "trade_date") -> str:
    """Normalize an exact string/date/datetime to ISO ``YYYY-MM-DD``."""

    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    elif type(value) is str:
        if _DATE_COMPACT_RE.fullmatch(value):
            candidate = "%s-%s-%s" % (value[:4], value[4:6], value[6:8])
        elif _DATE_ISO_RE.fullmatch(value):
            candidate = value
        else:
            raise ValueError(f"{field} must use YYYYMMDD or YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"{field} is not a valid calendar date") from exc
    else:
        raise TypeError(f"{field} must be exactly str, date, or datetime")
    return parsed.isoformat()


def _parse_clock(value: Any, field: str) -> time:
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise ValueError(f"{field} must be timezone-naive")
        return value
    if type(value) is not str or not re.fullmatch(r"\d{2}:\d{2}", value):
        raise ValueError(f"{field} must use HH:MM")
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid time") from exc


@dataclass(frozen=True)
class SessionWindow:
    """One non-overlapping intraday session window.

    Continuous windows may include their final timestamp.  When they do, the
    resampler assigns that exact endpoint to the preceding minute bucket.  A
    closing-auction window is always aggregated as a single independent bar.
    """

    name: SessionName
    kind: SessionKind
    start: str
    end: str
    sequence: int
    include_end: bool
    aggregate_as_single_bar: bool = False

    def __post_init__(self) -> None:
        if type(self.name) is not SessionName:
            raise TypeError("session name must be exactly SessionName")
        if type(self.kind) is not SessionKind:
            raise TypeError("session kind must be exactly SessionKind")
        start_time = _parse_clock(self.start, "session start")
        end_time = _parse_clock(self.end, "session end")
        if start_time >= end_time:
            raise ValueError("session start must be before end")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("session sequence must be a non-negative integer")
        if type(self.include_end) is not bool:
            raise TypeError("include_end must be exactly bool")
        if type(self.aggregate_as_single_bar) is not bool:
            raise TypeError("aggregate_as_single_bar must be exactly bool")
        if self.kind in (
            SessionKind.OPENING_AUCTION,
            SessionKind.CLOSING_AUCTION,
        ):
            if not self.aggregate_as_single_bar:
                raise ValueError("auction session must aggregate as one bar")
        elif self.aggregate_as_single_bar:
            raise ValueError("continuous session cannot aggregate as one bar")

    @property
    def start_time(self) -> time:
        return _parse_clock(self.start, "session start")

    @property
    def end_time(self) -> time:
        return _parse_clock(self.end, "session end")

    def contains(self, value: time) -> bool:
        if self.include_end:
            return self.start_time <= value <= self.end_time
        return self.start_time <= value < self.end_time

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name.value,
            "kind": self.kind.value,
            "start": self.start,
            "end": self.end,
            "sequence": self.sequence,
            "include_end": self.include_end,
            "aggregate_as_single_bar": self.aggregate_as_single_bar,
        }


@dataclass(frozen=True)
class SessionRule:
    """Effective-dated session layout for one venue/instrument family."""

    venue: Venue
    instrument_kind: InstrumentKind
    sessions: Tuple[SessionWindow, ...]
    effective_from: str
    effective_to: Optional[str]
    source_version: str
    snapshot_hash: str

    def __post_init__(self) -> None:
        if type(self.venue) is not Venue:
            raise TypeError("venue must be exactly Venue")
        if type(self.instrument_kind) is not InstrumentKind:
            raise TypeError("instrument_kind must be exactly InstrumentKind")
        if type(self.sessions) is not tuple or not self.sessions:
            raise TypeError("sessions must be a non-empty tuple")
        if any(type(item) is not SessionWindow for item in self.sessions):
            raise TypeError("each session must be exactly SessionWindow")
        sequences = tuple(item.sequence for item in self.sessions)
        if sequences != tuple(range(len(self.sessions))):
            raise ValueError("session sequences must be contiguous from zero")

        effective_from = normalize_session_date(
            self.effective_from, "effective_from"
        )
        if effective_from != self.effective_from:
            raise ValueError("effective_from must use canonical YYYY-MM-DD")
        if self.effective_to is not None:
            effective_to = normalize_session_date(self.effective_to, "effective_to")
            if effective_to != self.effective_to:
                raise ValueError("effective_to must use canonical YYYY-MM-DD")
            if effective_from > effective_to:
                raise ValueError("effective_from must not be after effective_to")
        if type(self.source_version) is not str or not self.source_version.strip():
            raise ValueError("source_version must be a non-empty string")
        if type(self.snapshot_hash) is not str or not _HASH_RE.fullmatch(
            self.snapshot_hash
        ):
            raise ValueError("snapshot_hash must be a lowercase SHA-256 hex digest")

        previous = None
        for current in self.sessions:
            if previous is not None:
                previous_end = previous.end_time
                current_start = current.start_time
                if previous_end > current_start:
                    raise ValueError("session windows must not overlap")
                if previous_end == current_start and previous.include_end:
                    raise ValueError(
                        "touching session windows cannot both own their boundary"
                    )
            previous = current

    def applies_on(self, trade_date: Any) -> bool:
        value = normalize_session_date(trade_date)
        return self.effective_from <= value and (
            self.effective_to is None or value <= self.effective_to
        )

    def session_for_time(self, value: time) -> Optional[SessionWindow]:
        matches = tuple(session for session in self.sessions if session.contains(value))
        if len(matches) > 1:
            raise AssertionError("validated session rule produced overlapping matches")
        return matches[0] if matches else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "venue": self.venue.value,
            "instrument_kind": self.instrument_kind.value,
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "source_version": self.source_version,
            "snapshot_hash": self.snapshot_hash,
            "sessions": [item.to_dict() for item in self.sessions],
        }


def _continuous_layout() -> Tuple[SessionWindow, ...]:
    return (
        SessionWindow(
            SessionName.OPENING_AUCTION,
            SessionKind.OPENING_AUCTION,
            "09:30",
            "09:31",
            0,
            False,
            True,
        ),
        SessionWindow(
            SessionName.MORNING_CONTINUOUS,
            SessionKind.CONTINUOUS,
            "09:31",
            "11:30",
            1,
            True,
        ),
        SessionWindow(
            SessionName.AFTERNOON_CONTINUOUS,
            SessionKind.CONTINUOUS,
            "13:01",
            "15:00",
            2,
            True,
        ),
    )


def _closing_auction_layout() -> Tuple[SessionWindow, ...]:
    return (
        SessionWindow(
            SessionName.OPENING_AUCTION,
            SessionKind.OPENING_AUCTION,
            "09:30",
            "09:31",
            0,
            False,
            True,
        ),
        SessionWindow(
            SessionName.MORNING_CONTINUOUS,
            SessionKind.CONTINUOUS,
            "09:31",
            "11:30",
            1,
            True,
        ),
        SessionWindow(
            SessionName.AFTERNOON_CONTINUOUS,
            SessionKind.CONTINUOUS,
            "13:01",
            "14:57",
            2,
            True,
        ),
        SessionWindow(
            SessionName.CLOSING_AUCTION,
            SessionKind.CLOSING_AUCTION,
            "14:58",
            "15:00",
            3,
            True,
            True,
        ),
    )


def _make_rule(
    venue: Venue,
    kind: InstrumentKind,
    sessions: Tuple[SessionWindow, ...],
    effective_from: str,
    effective_to: Optional[str] = None,
) -> SessionRule:
    return SessionRule(
        venue=venue,
        instrument_kind=kind,
        sessions=sessions,
        effective_from=effective_from,
        effective_to=effective_to,
        source_version=SESSION_SOURCE_VERSION,
        snapshot_hash=SESSION_SNAPSHOT_HASH,
    )


_CASH_KINDS = (
    InstrumentKind.A_SHARE,
    InstrumentKind.ETF,
    InstrumentKind.LOF,
)

_DEFAULT_RULES = []
for _kind in _CASH_KINDS:
    _DEFAULT_RULES.append(
        _make_rule(
            Venue.SZSE,
            _kind,
            _closing_auction_layout(),
            "2006-07-01",
        )
    )

_DEFAULT_RULES.extend(
    (
        _make_rule(
            Venue.SSE,
            InstrumentKind.A_SHARE,
            _continuous_layout(),
            "2006-07-01",
            "2018-08-19",
        ),
        _make_rule(
            Venue.SSE,
            InstrumentKind.A_SHARE,
            _closing_auction_layout(),
            "2018-08-20",
        ),
    )
)
for _kind in (InstrumentKind.ETF, InstrumentKind.LOF):
    _DEFAULT_RULES.extend(
        (
            _make_rule(
                Venue.SSE,
                _kind,
                _continuous_layout(),
                "2006-07-01",
                "2026-07-05",
            ),
            _make_rule(
                Venue.SSE,
                _kind,
                _closing_auction_layout(),
                "2026-07-06",
            ),
        )
    )
_DEFAULT_RULES.append(
    _make_rule(
        Venue.BSE,
        InstrumentKind.A_SHARE,
        _closing_auction_layout(),
        "2021-11-15",
    )
)

DEFAULT_SESSION_RULES: Tuple[SessionRule, ...] = tuple(_DEFAULT_RULES)
del _DEFAULT_RULES, _kind


class SessionCalendar:
    """Resolve immutable session rules by symbol and effective date."""

    def __init__(
        self,
        rules: Sequence[SessionRule] = DEFAULT_SESSION_RULES,
        rulebook: RuleBook = DEFAULT_RULEBOOK,
    ) -> None:
        if not isinstance(rulebook, RuleBook):
            raise TypeError("rulebook must be a RuleBook")
        if isinstance(rules, (str, bytes)) or not isinstance(rules, Sequence):
            raise TypeError("rules must be a sequence of SessionRule")
        canonical_rules = tuple(rules)
        if not canonical_rules:
            raise ValueError("rules must not be empty")
        if any(type(item) is not SessionRule for item in canonical_rules):
            raise TypeError("each rule must be exactly SessionRule")
        self._rules = tuple(
            sorted(
                canonical_rules,
                key=lambda item: (
                    item.venue.value,
                    item.instrument_kind.value,
                    item.effective_from,
                ),
            )
        )
        self._rulebook = rulebook
        self._validate_effective_ranges()

    @property
    def rules(self) -> Tuple[SessionRule, ...]:
        return self._rules

    def _validate_effective_ranges(self) -> None:
        previous_by_key = {}
        for rule in self._rules:
            key = (rule.venue, rule.instrument_kind)
            previous = previous_by_key.get(key)
            if previous is not None:
                if previous.effective_to is None:
                    raise ValueError(
                        "open-ended session rule cannot have a successor"
                    )
                previous_end = date.fromisoformat(previous.effective_to)
                current_start = date.fromisoformat(rule.effective_from)
                if (current_start - previous_end).days != 1:
                    raise ValueError(
                        "effective session rules must be contiguous and non-overlapping"
                    )
            previous_by_key[key] = rule

    def _instrument_identity(
        self, symbol: str, metadata: Optional[Any] = None
    ) -> Tuple[Venue, InstrumentKind]:
        instrument_rule = self._rulebook.get_rule(symbol, metadata=metadata)
        key = (instrument_rule.venue, instrument_rule.kind)
        supported_keys = {
            (rule.venue, rule.instrument_kind) for rule in self._rules
        }
        if key not in supported_keys:
            raise SessionRuleNotFoundError(
                "no cash-session rules for symbol=%r venue=%s kind=%s"
                % (
                    symbol,
                    instrument_rule.venue.value,
                    instrument_rule.kind.value,
                )
            )
        return key

    def validate_symbol(self, symbol: str, metadata: Optional[Any] = None) -> None:
        """Fail fast when an instrument family has no session-rule coverage."""

        self._instrument_identity(symbol, metadata=metadata)

    def get_rule(
        self,
        symbol: str,
        trade_date: Any,
        metadata: Optional[Any] = None,
    ) -> SessionRule:
        venue, kind = self._instrument_identity(symbol, metadata=metadata)
        canonical_date = normalize_session_date(trade_date)
        matches = tuple(
            rule
            for rule in self._rules
            if rule.venue is venue
            and rule.instrument_kind is kind
            and rule.applies_on(canonical_date)
        )
        if len(matches) != 1:
            raise SessionRuleNotFoundError(
                "no effective session rule for symbol=%r venue=%s kind=%s date=%s"
                % (symbol, venue.value, kind.value, canonical_date)
            )
        return matches[0]

    def session_for_timestamp(
        self,
        symbol: str,
        timestamp: datetime,
        metadata: Optional[Any] = None,
    ) -> Tuple[SessionRule, SessionWindow]:
        if not isinstance(timestamp, datetime):
            raise TypeError("timestamp must be a datetime")
        if timestamp.tzinfo is not None:
            raise ValueError("timestamp must be timezone-naive")
        if timestamp.second or timestamp.microsecond:
            raise ValueError("minute timestamp must be aligned to whole minutes")
        rule = self.get_rule(symbol, timestamp.date(), metadata=metadata)
        session = rule.session_for_time(timestamp.time())
        if session is None:
            raise TimestampOutsideSessionError(
                "timestamp %s is outside sessions for %s"
                % (timestamp.isoformat(sep=" "), symbol)
            )
        return rule, session


DEFAULT_SESSION_CALENDAR = SessionCalendar()


__all__ = [
    "DEFAULT_SESSION_CALENDAR",
    "DEFAULT_SESSION_RULES",
    "SESSION_SNAPSHOT_HASH",
    "SESSION_SOURCE_VERSION",
    "SessionCalendar",
    "SessionCalendarError",
    "SessionKind",
    "SessionName",
    "SessionRule",
    "SessionRuleNotFoundError",
    "SessionWindow",
    "TimestampOutsideSessionError",
    "normalize_session_date",
]
