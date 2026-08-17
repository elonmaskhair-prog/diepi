"""Built-in, independent A-share trading calendar.

The bundled schedule covers every natural day from 2010-01-01 through
2026-12-31.  Open sessions are reconstructed as Monday-Friday minus the
exchange-announced weekday closures below.  It is deliberately independent
from instrument bars: a suspension or missing quote must not redefine the
market clock.

Sources and verification
------------------------
The closure facts are reconstructed from the Shanghai and Shenzhen stock
exchanges' public annual closure notices.  The official source indexes and
the two full-year boundary notices used in the review are:

* https://www.sse.com.cn/disclosure/dealinstruc/closed/
* https://www.sse.com.cn/disclosure/dealinstruc/closed/list/
* https://www.sse.com.cn/aboutus/mediacenter/hotandd/c/c_20150912_3988350.shtml
* https://www.sse.com.cn/disclosure/dealinstruc/closed/c/c_20251222_10802510.shtml
* https://www.szse.cn/disclosure/notice/general/t20251222_618087.html

The 2010-2025 reconstruction was cross-checked against the project's local
SSE natural-day calendar.  The complete 2026 schedule was derived from the
official full-year SSE/SZSE notices and matched the local calendar through its
2026-09-04 endpoint.  Weekends are closed under the exchange trading rules.

This module contains calendar facts only; it is not an upstream code snapshot
and introduces no runtime dependency or network access.
"""

from __future__ import annotations

from datetime import date, timedelta
from dataclasses import dataclass
from functools import lru_cache
import hashlib
from typing import Any, Dict, Iterator, Tuple

import pandas as pd


BUILTIN_CALENDAR_ID = "cn-a-share-2010-2026-v1"
BUILTIN_CALENDAR_VERSION = "1"
BUILTIN_CALENDAR_START = "20100101"
BUILTIN_CALENDAR_END = "20261231"
BUILTIN_CALENDAR_NATURAL_DAYS = 6209
BUILTIN_CALENDAR_OPEN_DAYS = 4128
BUILTIN_CALENDAR_OPEN_DAYS_SHA256 = (
    "d2e966b343b1d04767bccc14d898095169972e20b42b8e5cf078ba14c5b47e2c"
)
BUILTIN_CALENDAR_CONTENT_SHA256 = (
    "882bb722fd15cbe4ceddb2370422c30fead8c6e81f842300221996ea3cb44a59"
)


class TradeCalendarError(ValueError):
    """Raised when a local calendar cannot prove an independent clock."""


@dataclass(frozen=True)
class TradeCalendarIdentity:
    """Stable evidence for the exact schedule selected by a run."""

    source: str
    calendar_id: str
    version: str
    content_sha256: str
    coverage_start: str
    coverage_end: str
    rows: int
    open_days: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "calendar_id": self.calendar_id,
            "version": self.version,
            "content_sha256": self.content_sha256,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "rows": self.rows,
            "open_days": self.open_days,
        }

    def assumptions(self) -> Tuple[Tuple[str, str], ...]:
        return (
            ("calendar.source", self.source),
            ("calendar.id", self.calendar_id),
            ("calendar.version", self.version),
            ("calendar.content_sha256", self.content_sha256),
            ("calendar.coverage_start", self.coverage_start),
            ("calendar.coverage_end", self.coverage_end),
            ("calendar.rows", str(self.rows)),
            ("calendar.open_days", str(self.open_days)),
        )


# Weekday closures only.  Saturdays and Sundays are closed by the general
# exchange rule and therefore do not need to be repeated here.  Keeping the
# exceptions explicit makes the schedule reviewable without a data vendor or
# an opaque third-party calendar implementation.
_WEEKDAY_CLOSURES = frozenset(
    """
    20100101 20100215 20100216 20100217 20100218 20100219 20100405
    20100503 20100614 20100615 20100616 20100922 20100923 20100924
    20101001 20101004 20101005 20101006 20101007
    20110103 20110202 20110203 20110204 20110207 20110208 20110404
    20110405 20110502 20110606 20110912 20111003 20111004 20111005
    20111006 20111007
    20120102 20120103 20120123 20120124 20120125 20120126 20120127
    20120402 20120403 20120404 20120430 20120501 20120622 20121001
    20121002 20121003 20121004 20121005
    20130101 20130102 20130103 20130211 20130212 20130213 20130214
    20130215 20130404 20130405 20130429 20130430 20130501 20130610
    20130611 20130612 20130919 20130920 20131001 20131002 20131003
    20131004 20131007
    20140101 20140131 20140203 20140204 20140205 20140206 20140407
    20140501 20140502 20140602 20140908 20141001 20141002 20141003
    20141006 20141007
    20150101 20150102 20150218 20150219 20150220 20150223 20150224
    20150406 20150501 20150622 20150903 20150904 20151001 20151002
    20151005 20151006 20151007
    20160101 20160208 20160209 20160210 20160211 20160212 20160404
    20160502 20160609 20160610 20160915 20160916 20161003 20161004
    20161005 20161006 20161007
    20170102 20170127 20170130 20170131 20170201 20170202 20170403
    20170404 20170501 20170529 20170530 20171002 20171003 20171004
    20171005 20171006
    20180101 20180215 20180216 20180219 20180220 20180221 20180405
    20180406 20180430 20180501 20180618 20180924 20181001 20181002
    20181003 20181004 20181005 20181231
    20190101 20190204 20190205 20190206 20190207 20190208 20190405
    20190501 20190502 20190503 20190607 20190913 20191001 20191002
    20191003 20191004 20191007
    20200101 20200124 20200127 20200128 20200129 20200130 20200131
    20200406 20200501 20200504 20200505 20200625 20200626 20201001
    20201002 20201005 20201006 20201007 20201008
    20210101 20210211 20210212 20210215 20210216 20210217 20210405
    20210503 20210504 20210505 20210614 20210920 20210921 20211001
    20211004 20211005 20211006 20211007
    20220103 20220131 20220201 20220202 20220203 20220204 20220404
    20220405 20220502 20220503 20220504 20220603 20220912 20221003
    20221004 20221005 20221006 20221007
    20230102 20230123 20230124 20230125 20230126 20230127 20230405
    20230501 20230502 20230503 20230622 20230623 20230929 20231002
    20231003 20231004 20231005 20231006
    20240101 20240209 20240212 20240213 20240214 20240215 20240216
    20240404 20240405 20240501 20240502 20240503 20240610 20240916
    20240917 20241001 20241002 20241003 20241004 20241007
    20250101 20250128 20250129 20250130 20250131 20250203 20250204
    20250404 20250501 20250502 20250505 20250602 20251001 20251002
    20251003 20251006 20251007 20251008
    20260101 20260102 20260216 20260217 20260218 20260219 20260220
    20260223 20260406 20260501 20260504 20260505 20260619 20260925
    20261001 20261002 20261005 20261006 20261007
    """.split()
)


def _compact(value: date) -> str:
    return value.strftime("%Y%m%d")


def _natural_dates() -> Iterator[date]:
    current = date(2010, 1, 1)
    end = date(2026, 12, 31)
    while current <= end:
        yield current
        current += timedelta(days=1)


def _build_rows() -> Tuple[Tuple[str, int, str | None], ...]:
    rows = []
    open_dates = []
    previous_open = None
    for natural_date in _natural_dates():
        compact = _compact(natural_date)
        is_open = int(
            natural_date.weekday() < 5 and compact not in _WEEKDAY_CLOSURES
        )
        rows.append((compact, is_open, previous_open))
        if is_open:
            open_dates.append(compact)
            previous_open = compact

    closure_errors = sorted(
        value
        for value in _WEEKDAY_CLOSURES
        if value < BUILTIN_CALENDAR_START
        or value > BUILTIN_CALENDAR_END
        or date.fromisoformat(
            f"{value[:4]}-{value[4:6]}-{value[6:]}"
        ).weekday() >= 5
    )
    if closure_errors:
        raise RuntimeError(
            "built-in A-share calendar has invalid weekday closures: "
            + ",".join(closure_errors[:5])
        )
    if len(rows) != BUILTIN_CALENDAR_NATURAL_DAYS:
        raise RuntimeError("built-in A-share calendar natural-day count changed")
    if len(open_dates) != BUILTIN_CALENDAR_OPEN_DAYS:
        raise RuntimeError("built-in A-share calendar open-day count changed")
    digest = hashlib.sha256(
        ("\n".join(open_dates) + "\n").encode("ascii")
    ).hexdigest()
    if digest != BUILTIN_CALENDAR_OPEN_DAYS_SHA256:
        raise RuntimeError("built-in A-share calendar identity check failed")
    content = "cal_date,is_open\n" + "".join(
        f"{cal_date},{is_open}\n" for cal_date, is_open, _ in rows
    )
    if hashlib.sha256(content.encode("ascii")).hexdigest() != (
        BUILTIN_CALENDAR_CONTENT_SHA256
    ):
        raise RuntimeError("built-in A-share calendar content check failed")
    return tuple(rows)


@lru_cache(maxsize=1)
def _cached_builtin_trade_calendar() -> pd.DataFrame:
    return pd.DataFrame.from_records(
        _build_rows(),
        columns=("cal_date", "is_open", "pretrade_date"),
    ).astype({"cal_date": "string", "is_open": "int8"})


def load_builtin_trade_calendar() -> pd.DataFrame:
    """Return an isolated copy of the reviewed built-in market clock."""

    return _cached_builtin_trade_calendar().copy(deep=True)


def builtin_open_days() -> Tuple[str, ...]:
    """Return the immutable ordered open-session identity."""

    frame = _cached_builtin_trade_calendar()
    return tuple(frame.loc[frame["is_open"].eq(1), "cal_date"].astype(str))


def _canonical_schedule(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("trade calendar must be a pandas DataFrame")
    missing = sorted({"cal_date", "is_open"} - set(frame.columns))
    if missing:
        raise TradeCalendarError(
            "trade calendar is missing required columns: " + ",".join(missing)
        )
    if frame.empty:
        raise TradeCalendarError("trade calendar has no rows")

    dates = frame["cal_date"].astype("string").str.strip()
    # Numeric Parquet sources occasionally expose YYYYMMDD as float-like text.
    dates = dates.str.replace(r"\.0$", "", regex=True)
    valid_shape = dates.str.fullmatch(r"\d{8}", na=False)
    parsed = pd.to_datetime(
        dates.where(valid_shape), format="%Y%m%d", errors="coerce"
    )
    invalid_date = ~valid_shape | parsed.isna()
    if invalid_date.any():
        samples = ",".join(dates[invalid_date].astype(str).head(5))
        raise TradeCalendarError(
            "trade calendar cal_date must contain valid YYYYMMDD values: "
            + samples
        )

    statuses = pd.to_numeric(frame["is_open"], errors="coerce")
    invalid_status = statuses.isna() | ~statuses.isin((0, 1))
    if invalid_status.any():
        samples = ",".join(
            frame.loc[invalid_status, "is_open"].astype(str).head(5)
        )
        raise TradeCalendarError(
            "trade calendar is_open must contain only 0 or 1: " + samples
        )

    canonical = pd.DataFrame(
        {"cal_date": dates.astype(str), "is_open": statuses.astype("int8")}
    )
    conflicts = canonical.groupby("cal_date", sort=False)["is_open"].nunique()
    conflicting_dates = sorted(conflicts[conflicts.gt(1)].index.astype(str))
    if conflicting_dates:
        raise TradeCalendarError(
            "trade calendar has conflicting is_open values: "
            + ",".join(conflicting_dates[:5])
        )
    canonical = (
        canonical.drop_duplicates("cal_date")
        .sort_values("cal_date", kind="mergesort")
        .reset_index(drop=True)
    )
    first = canonical["cal_date"].iloc[0]
    last = canonical["cal_date"].iloc[-1]
    expected = pd.date_range(
        pd.to_datetime(first, format="%Y%m%d"),
        pd.to_datetime(last, format="%Y%m%d"),
        freq="D",
    ).strftime("%Y%m%d")
    if len(expected) != len(canonical) or tuple(expected) != tuple(
        canonical["cal_date"]
    ):
        missing_dates = sorted(set(expected) - set(canonical["cal_date"]))
        raise TradeCalendarError(
            "trade calendar must contain every natural date in its coverage: "
            + ",".join(missing_dates[:5])
        )
    return canonical


def identify_trade_calendar(
    frame: pd.DataFrame, *, source: str
) -> TradeCalendarIdentity:
    """Validate and identify a bundled or full local-override schedule."""

    if source not in {"bundled", "local_override"}:
        raise ValueError("calendar source must be bundled or local_override")
    canonical = _canonical_schedule(frame)
    content = "cal_date,is_open\n" + "".join(
        f"{row.cal_date},{int(row.is_open)}\n"
        for row in canonical.itertuples(index=False)
    )
    digest = hashlib.sha256(content.encode("ascii")).hexdigest()
    first = str(canonical["cal_date"].iloc[0])
    last = str(canonical["cal_date"].iloc[-1])
    open_days = int(canonical["is_open"].eq(1).sum())
    if source == "bundled":
        if (
            digest != BUILTIN_CALENDAR_CONTENT_SHA256
            or first != BUILTIN_CALENDAR_START
            or last != BUILTIN_CALENDAR_END
        ):
            raise TradeCalendarError(
                "bundled trade calendar does not match its reviewed identity"
            )
        calendar_id = BUILTIN_CALENDAR_ID
        version = BUILTIN_CALENDAR_VERSION
    else:
        calendar_id = f"local-override-{digest[:16]}"
        version = "unversioned"
    return TradeCalendarIdentity(
        source=source,
        calendar_id=calendar_id,
        version=version,
        content_sha256=digest,
        coverage_start=first,
        coverage_end=last,
        rows=len(canonical),
        open_days=open_days,
    )


def builtin_calendar_identity() -> TradeCalendarIdentity:
    """Return the stable identity exposed in validation and run evidence."""

    return identify_trade_calendar(
        _cached_builtin_trade_calendar(), source="bundled"
    )


__all__ = [
    "BUILTIN_CALENDAR_END",
    "BUILTIN_CALENDAR_ID",
    "BUILTIN_CALENDAR_CONTENT_SHA256",
    "BUILTIN_CALENDAR_NATURAL_DAYS",
    "BUILTIN_CALENDAR_OPEN_DAYS",
    "BUILTIN_CALENDAR_OPEN_DAYS_SHA256",
    "BUILTIN_CALENDAR_START",
    "BUILTIN_CALENDAR_VERSION",
    "TradeCalendarError",
    "TradeCalendarIdentity",
    "builtin_calendar_identity",
    "builtin_open_days",
    "identify_trade_calendar",
    "load_builtin_trade_calendar",
]
