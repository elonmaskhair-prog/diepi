"""Contract selector with explicit expiry/roll inputs and normalized dates."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
from math import isfinite
from pathlib import Path
import re

import pandas as pd

from .constants import get_spec


_COMPACT_DATE = re.compile(r"^\d{8}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _schedule_hash(schedule: Mapping[str, str] | None) -> str:
    if schedule is None:
        return "none"
    payload = json.dumps(
        sorted(schedule.items()),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(payload).hexdigest()


def normalize_trade_date(value, *, field: str = "trade_date") -> str:
    """Normalize supported date scalars to YYYYMMDD without ambiguity."""
    if value is None or value is pd.NaT:
        raise ValueError(f"{field} must be YYYYMMDD or YYYY-MM-DD")
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            raise ValueError(f"{field} must be YYYYMMDD or YYYY-MM-DD")
        return value.strftime("%Y%m%d")
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y%m%d")

    text = str(value).strip()
    if _COMPACT_DATE.fullmatch(text):
        fmt = "%Y%m%d"
    elif _ISO_DATE.fullmatch(text):
        fmt = "%Y-%m-%d"
    else:
        raise ValueError(
            f"{field} must be YYYYMMDD or YYYY-MM-DD, got {value!r}")
    try:
        return datetime.strptime(text, fmt).strftime("%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc


def _read_schedule_frame(source, *, label: str) -> pd.DataFrame:
    if isinstance(source, pd.DataFrame):
        return source.copy()
    path = Path(source)
    if not path.exists():
        raise FileNotFoundError(f"{label} not found: {path}")
    if path.suffix.lower() in (".parquet", ".pq"):
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path, dtype=str)
    raise ValueError(f"{label} must be a mapping, DataFrame, CSV, or parquet")


def _load_expiry_schedule(source, raw: pd.DataFrame) -> tuple[dict[str, str], str]:
    if source is None:
        if "expiry_date" not in raw.columns:
            raise ValueError(
                "An explicit contract expiry schedule is required; refusing "
                "to infer expiry from the last downloaded market-data row"
            )
        frame = raw[["ts_code", "expiry_date"]].copy()
        source_name = "contract_data.expiry_date"
    elif isinstance(source, Mapping):
        frame = pd.DataFrame(
            list(source.items()), columns=["ts_code", "expiry_date"])
        source_name = "explicit_mapping"
    else:
        frame = _read_schedule_frame(source, label="contract expiry schedule")
        source_name = "explicit_table"

    required = {"ts_code", "expiry_date"}
    missing_columns = required - set(frame.columns)
    if missing_columns:
        raise ValueError(
            f"contract expiry schedule missing columns: {sorted(missing_columns)}")

    expiry: dict[str, str] = {}
    for row_index, row in frame.iterrows():
        ts_code = str(row["ts_code"]).strip()
        if not ts_code:
            raise ValueError(
                f"contract expiry schedule has blank ts_code at row {row_index}")
        expiry_date = normalize_trade_date(
            row["expiry_date"], field=f"expiry_date[{ts_code}]")
        prior = expiry.get(ts_code)
        if prior is not None and prior != expiry_date:
            raise ValueError(
                f"conflicting expiry dates for {ts_code}: {prior}, {expiry_date}")
        expiry[ts_code] = expiry_date

    raw_contracts = set(raw["ts_code"])
    missing_contracts = sorted(raw_contracts - set(expiry))
    if missing_contracts:
        raise ValueError(
            "contract expiry schedule missing contracts: "
            f"{missing_contracts[:5]}"
        )
    first_observed = raw.groupby("ts_code")["trade_date"].min().to_dict()
    impossible = [
        code for code, first_date in first_observed.items()
        if expiry[code] < first_date
    ]
    if impossible:
        raise ValueError(
            "contract expiry predates first market-data row: "
            f"{sorted(impossible)[:5]}"
        )
    return expiry, source_name


def _load_roll_schedule(source, *, product: str,
                        continuous_data_dir: str | None) -> tuple[dict[str, str] | None, str]:
    if source is not None:
        if isinstance(source, Mapping):
            frame = pd.DataFrame(
                list(source.items()), columns=["trade_date", "mapping_ts_code"])
        else:
            frame = _read_schedule_frame(source, label="contract roll schedule")
        source_name = "explicit_roll_schedule"
    elif continuous_data_dir:
        path = Path(continuous_data_dir) / f"{product}_continuous.parquet"
        if not path.exists():
            return None, "none"
        frame = pd.read_parquet(path)
        source_name = "continuous_mapping_file"
    else:
        return None, "none"

    required = {"trade_date", "mapping_ts_code"}
    missing_columns = required - set(frame.columns)
    if missing_columns:
        raise ValueError(
            f"contract roll schedule missing columns: {sorted(missing_columns)}")

    mapping: dict[str, str] = {}
    for row_index, row in frame.iterrows():
        trade_date = normalize_trade_date(
            row["trade_date"], field=f"roll.trade_date[{row_index}]")
        ts_code = str(row["mapping_ts_code"]).strip()
        if not ts_code:
            raise ValueError(f"blank mapping_ts_code at roll row {row_index}")
        prior = mapping.get(trade_date)
        if prior is not None and prior != ts_code:
            raise ValueError(
                f"conflicting roll schedule on {trade_date}: {prior}, {ts_code}")
        mapping[trade_date] = ts_code
    return mapping, source_name


@dataclass
class ContractSelection:
    """Record of a contract selection decision."""
    ts_code: str
    trade_date: str
    method: str
    expiry_date: str
    days_to_expiry: int
    volume_rank: int
    reason: str


class ContractSelector:
    """Select futures contracts without lookahead.

    Data source: individual contract parquet (ts_code, trade_date, OHLCV, vol, oi).
    Expiry must come from an explicit schedule (or an explicit expiry_date
    column); the last downloaded quote is never treated as exchange expiry.
    Selection uses T-1 volume ranking or exchange mapping.
    get_price() returns None if contract has no data on that date (never falls back).
    """

    def __init__(self, product: str, contract_data_dir: str,
                 continuous_data_dir: str | None = None,
                 contract_expiry=None,
                 roll_schedule=None):
        self._product = product
        spec = get_spec(product)
        self._multiplier = spec["multiplier"]

        # Load individual contract data
        path = Path(contract_data_dir) / f"{product}_contracts.parquet"
        if not path.exists():
            raise FileNotFoundError(f"Contract data not found: {path}")

        raw = pd.read_parquet(path)
        required_columns = {"ts_code", "trade_date"}
        missing_columns = required_columns - set(raw.columns)
        if missing_columns:
            raise ValueError(
                f"Contract data missing columns: {sorted(missing_columns)}")
        if raw.empty:
            raise ValueError(f"Contract data is empty: {path}")
        raw = raw.copy()
        raw["ts_code"] = raw["ts_code"].astype(str).str.strip()
        raw["trade_date"] = raw["trade_date"].map(normalize_trade_date)
        duplicates = raw.duplicated(["ts_code", "trade_date"], keep=False)
        if duplicates.any():
            samples = raw.loc[duplicates, ["ts_code", "trade_date"]].head(5)
            raise ValueError(
                "Duplicate contract/date market-data rows: "
                f"{samples.to_dict('records')}")
        self._raw = raw

        self._expiry, self._expiry_source = _load_expiry_schedule(
            contract_expiry, raw)
        self._expiry_schedule_hash = _schedule_hash(self._expiry)

        # MultiIndex for O(1) price lookup
        self._idx = raw.set_index(["ts_code", "trade_date"]).sort_index()

        # Per-date contract data for selection (keyed by date for fast lookup)
        self._by_date: dict[str, pd.DataFrame] = {}
        for d, grp in raw.groupby("trade_date"):
            self._by_date[str(d)] = grp

        # Calendar
        self._calendar = sorted(raw["trade_date"].unique().tolist())
        self._cal_set = set(self._calendar)
        self._cal_idx = {d: i for i, d in enumerate(self._calendar)}

        # Optional explicit roll/main-contract mapping.
        self._mapping, self._roll_schedule_source = _load_roll_schedule(
            roll_schedule,
            product=product,
            continuous_data_dir=continuous_data_dir,
        )
        if self._mapping:
            known_contracts = set(self._expiry)
            unknown = sorted(set(self._mapping.values()) - known_contracts)
            if unknown:
                raise ValueError(
                    "contract roll schedule references unknown contracts: "
                    f"{unknown[:5]}"
                )
            expired_rows = sorted(
                (trade_date, ts_code)
                for trade_date, ts_code in self._mapping.items()
                if trade_date > self._expiry[ts_code]
            )
            if expired_rows:
                raise ValueError(
                    "contract roll schedule maps contracts after expiry: "
                    f"{expired_rows[:5]}"
                )
        self._mapping_dates = sorted(self._mapping) if self._mapping else []
        self._roll_schedule_hash = _schedule_hash(self._mapping)

        # Audit log
        self._audit_log: list[dict] = []

    @property
    def calendar(self) -> list[str]:
        return list(self._calendar)

    @property
    def audit_log(self) -> list[dict]:
        return list(self._audit_log)

    @property
    def expiry_source(self) -> str:
        return self._expiry_source

    @property
    def roll_schedule_source(self) -> str:
        return self._roll_schedule_source

    @property
    def expiry_schedule_hash(self) -> str:
        return self._expiry_schedule_hash

    @property
    def roll_schedule_hash(self) -> str:
        return self._roll_schedule_hash

    def get_prev_date(self, trade_date: str) -> str | None:
        """Get previous trading day. Returns None if trade_date is the first day."""
        trade_date = normalize_trade_date(trade_date)
        idx = self._cal_idx.get(trade_date)
        if idx is None or idx == 0:
            return None
        return self._calendar[idx - 1]

    def get_price(self, ts_code: str, trade_date: str, field: str) -> float | None:
        """Get price field for a specific contract on a specific date.
        Returns None if data doesn't exist. NEVER falls back silently."""
        trade_date = normalize_trade_date(trade_date)
        try:
            val = self._idx.loc[(ts_code, trade_date), field]
            return float(val)
        except (KeyError, TypeError, ValueError):
            return None

    def get_expiry(self, ts_code: str) -> str:
        """Return the explicitly supplied expiry date for this contract."""
        return self._expiry.get(ts_code, "")

    def is_expired_on(self, ts_code: str, trade_date: str) -> bool:
        """Return whether the explicit expiry is before ``trade_date``."""
        trade_date = normalize_trade_date(trade_date)
        expiry = self._expiry.get(ts_code)
        return expiry is None or trade_date > expiry

    def has_data_on(self, ts_code: str, trade_date: str) -> bool:
        """Return whether a quote exists, independently of expiry semantics."""
        trade_date = normalize_trade_date(trade_date)
        try:
            _ = self._idx.loc[(ts_code, trade_date)]
            return True
        except KeyError:
            return False

    def is_valid_on(self, ts_code: str, trade_date: str) -> bool:
        """Check explicit expiry and quote availability for this date."""
        return (
            not self.is_expired_on(ts_code, trade_date)
            and self.has_data_on(ts_code, trade_date)
        )

    def should_roll(self, ts_code: str, trade_date: str) -> bool:
        """Return whether an explicit roll schedule changes contract today."""
        trade_date = normalize_trade_date(trade_date)
        mapped = self._mapped_contract_on(trade_date)
        if not mapped:
            raise ValueError(
                f"contract roll schedule has no effective contract on {trade_date}")
        return mapped != ts_code

    def _mapped_contract_on(self, trade_date: str) -> str | None:
        """Resolve the contract effective after ``trade_date`` close."""
        if self._mapping is None:
            return None
        trade_date = normalize_trade_date(trade_date)
        index = bisect_right(self._mapping_dates, trade_date) - 1
        if index < 0:
            return None
        return self._mapping[self._mapping_dates[index]]

    def _mapped_contract_before_close(self, trade_date: str) -> str | None:
        """Resolve the contract eligible before that day's close roll.

        A change first recorded on a date takes effect at that date's close.
        The schedule's first row is necessarily also its initial contract, so
        it is available from that day's open.
        """
        if self._mapping is None:
            return None
        trade_date = normalize_trade_date(trade_date)
        insertion = bisect_left(self._mapping_dates, trade_date)
        if (
            insertion < len(self._mapping_dates)
            and self._mapping_dates[insertion] == trade_date
            and insertion > 0
        ):
            return self._mapping[self._mapping_dates[insertion - 1]]
        index = bisect_right(self._mapping_dates, trade_date) - 1
        if index < 0:
            return None
        return self._mapping[self._mapping_dates[index]]

    def select(self, trade_date: str, min_days_remaining: int = 7,
               method: str = "volume_t1") -> ContractSelection | None:
        """Select a contract for entry on trade_date.

        method="volume_t1": Use T-1 day's volume ranking (no lookahead).
        method="mapping": Use exchange-defined main contract mapping.

        Returns None if no valid contract available.
        """
        trade_date = normalize_trade_date(trade_date)
        if method == "volume_t1":
            return self._select_by_volume_t1(trade_date, min_days_remaining)
        elif method == "mapping":
            if self._mapping is None:
                raise ValueError(
                    "contract_method='mapping' requires an explicit roll schedule")
            return self._select_by_mapping(trade_date, min_days_remaining)
        else:
            raise ValueError(f"Unknown method: {method}")

    def select_for_close_roll(
        self,
        trade_date: str,
        min_days_remaining: int = 7,
        method: str = "volume_t1",
    ) -> ContractSelection | None:
        """Select the replacement eligible immediately after this close."""
        trade_date = normalize_trade_date(trade_date)
        if method == "mapping":
            if self._mapping is None:
                raise ValueError(
                    "contract_method='mapping' requires an explicit roll schedule")
            return self._select_by_mapping(
                trade_date, min_days_remaining, at_close=True)
        return self.select(trade_date, min_days_remaining, method)

    def _select_by_volume_t1(self, trade_date: str,
                              min_days_remaining: int,
                              _from_fallback: bool = False) -> ContractSelection | None:
        """Select using T-1 day's volume (zero lookahead)."""
        prev_date = self.get_prev_date(trade_date)
        if prev_date is None:
            # First day: fall back to mapping if available (but not if already a fallback)
            if self._mapping and not _from_fallback:
                sel = self._select_by_mapping(trade_date, min_days_remaining, _from_fallback=True)
                if sel is not None:
                    sel.method = "first_day_mapping"
                    sel.reason = "First day: no T-1 data, used exchange mapping"
                    # Update audit log entry
                    if self._audit_log and self._audit_log[-1]["trade_date"] == trade_date:
                        self._audit_log[-1]["method"] = "first_day_mapping"
                return sel
            return None

        # Get contracts that traded on T-1
        prev_data = self._by_date.get(prev_date)
        if prev_data is None or prev_data.empty:
            return None

        entry_dt = pd.Timestamp(trade_date)
        candidates = []
        for _, row in prev_data.iterrows():
            ts_code = str(row["ts_code"])
            expiry = self._expiry.get(ts_code, "")
            if not expiry:
                continue
            days_left = (pd.Timestamp(expiry) - entry_dt).days
            if days_left >= min_days_remaining:
                try:
                    volume = float(row["vol"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError(
                        f"Invalid T-1 volume for {ts_code} on {prev_date}"
                    ) from None
                if not isfinite(volume) or volume < 0:
                    raise ValueError(
                        f"Invalid T-1 volume for {ts_code} on {prev_date}"
                    )
                candidates.append((ts_code, expiry, days_left, volume))

        if not candidates:
            return None

        # Sort by volume (T-1) descending, pick highest
        candidates.sort(key=lambda item: (-item[3], item[0]))
        best = candidates[0]
        # Selection is committed solely from T-1 information.  Looking at
        # which candidates happen to have a complete T row and then choosing
        # the next-ranked contract would make today's data availability part
        # of the decision.  Missing execution-day data is therefore a hard
        # data-contract failure for the already-selected contract.
        if not self.has_data_on(best[0], trade_date):
            raise ValueError(
                f"Selected T-1 volume contract {best[0]} has no market data "
                f"on {trade_date}"
            )

        selection = ContractSelection(
            ts_code=best[0],
            trade_date=trade_date,
            method="volume_t1",
            expiry_date=best[1],
            days_to_expiry=best[2],
            volume_rank=1,
            reason=f"T-1 vol rank 1 of {len(candidates)} valid contracts",
        )
        self._audit_log.append({
            "trade_date": trade_date, "selected": best[0],
            "method": "volume_t1", "candidates": len(candidates),
            "expiry": best[1], "days_left": best[2],
        })
        return selection

    def _select_by_mapping(self, trade_date: str,
                            min_days_remaining: int,
                            _from_fallback: bool = False,
                            *, at_close: bool = False) -> ContractSelection | None:
        """Select using exchange main contract mapping."""
        if self._mapping is None:
            return None

        ts_code = (
            self._mapped_contract_on(trade_date)
            if at_close else self._mapped_contract_before_close(trade_date)
        )
        if not ts_code:
            if not _from_fallback:
                raise ValueError(
                    f"contract roll schedule has no effective contract on {trade_date}")
            return None

        # Check expiry
        expiry = self._expiry.get(ts_code, "")
        if not expiry:
            return None
        days_left = (pd.Timestamp(expiry) - pd.Timestamp(trade_date)).days
        if days_left < min_days_remaining:
            # A supplied mapping is the independent, pre-committed contract
            # calendar.  Substituting a volume-ranked contract here would
            # silently change both the contract and its roll date.
            if not _from_fallback:
                raise ValueError(
                    f"roll schedule maps {ts_code} on {trade_date} with "
                    f"only {days_left} days to expiry; configured minimum is "
                    f"{min_days_remaining}"
                )
            return None

        # Check data available on trade_date
        if not self.is_valid_on(ts_code, trade_date):
            if not _from_fallback:
                if self.is_expired_on(ts_code, trade_date):
                    raise ValueError(
                        f"roll schedule maps expired contract {ts_code} on {trade_date}")
                raise ValueError(
                    f"roll schedule maps {ts_code} on {trade_date}, but market data is missing")
            return None

        selection = ContractSelection(
            ts_code=ts_code,
            trade_date=trade_date,
            method="mapping_close" if at_close else "mapping",
            expiry_date=expiry,
            days_to_expiry=days_left,
            volume_rank=0,
            reason=f"Exchange main contract mapping",
        )
        self._audit_log.append({
            "trade_date": trade_date, "selected": ts_code,
            "method": "mapping_close" if at_close else "mapping",
            "expiry": expiry, "days_left": days_left,
        })
        return selection
