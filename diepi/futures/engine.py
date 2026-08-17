"""FuturesEngine: standardized futures backtest with built-in validation."""
from __future__ import annotations

from collections.abc import Iterable
import hashlib
import json
import os
from math import isfinite
from numbers import Integral, Real
from pathlib import Path

import numpy as np
import pandas as pd

from ..backtest.metrics import MetricEngine, calculate_trade_round_metrics
from ..backtest.outcome import OutcomeTracker
from ..backtest.result_contract import ResultContract, ResultStatus

from .constants import get_spec
from .contract import ContractSelector, normalize_trade_date
from .cost import CostModel
from .journal import (
    EMPTY_FUTURES_EVENT_JOURNAL_JSON,
    FUTURES_EVENT_JOURNAL_SCOPE,
    FuturesDiagnosticJournal,
)
from .result import DEFAULT_ASSUMPTIONS, FuturesResult, FuturesValidationError


def _load_trading_calendar(
    source,
    coverage: tuple[str, str] | None = None,
) -> tuple[tuple[str, ...], str, str, str, str]:
    """Load independent open sessions and the source's coverage interval.

    Open rows alone cannot prove that dates after the final row were holidays
    rather than a truncated download.  Tables retaining closed rows prove
    their full interval.  Open-day-only inputs may extend that interval via
    ``coverage=(start_date, end_date)``.
    """

    if source is None:
        raise ValueError(
            "An explicit trading_calendar is required; refusing to derive "
            "expected coverage from downloaded contract market data"
        )
    if isinstance(source, pd.DataFrame):
        frame = source.copy()
        source_name = "explicit_dataframe"
    elif isinstance(source, (str, os.PathLike)):
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"trading calendar not found: {path}")
        if path.suffix.lower() in (".parquet", ".pq"):
            frame = pd.read_parquet(path)
        elif path.suffix.lower() == ".csv":
            frame = pd.read_csv(path, dtype=str)
        else:
            raise ValueError("trading_calendar path must be CSV or parquet")
        source_name = "explicit_table"
    else:
        if isinstance(source, (bytes, bytearray)) or not isinstance(
                source, Iterable):
            raise TypeError(
                "trading_calendar must be an iterable, DataFrame, CSV, or parquet"
            )
        frame = pd.DataFrame({"trade_date": list(source)})
        source_name = "explicit_iterable"

    date_column = next(
        (name for name in ("trade_date", "cal_date", "date")
         if name in frame.columns),
        None,
    )
    if date_column is None:
        raise ValueError(
            "trading_calendar must contain trade_date, cal_date, or date"
        )
    all_dates = [
        normalize_trade_date(value, field=f"trading_calendar[{index}]")
        for index, value in frame[date_column].items()
    ]
    if not all_dates:
        raise ValueError("trading_calendar contains no calendar dates")
    all_date_series = pd.Series(all_dates, dtype=str)
    duplicate_mask = all_date_series.duplicated(keep=False)
    if duplicate_mask.any():
        duplicate_dates = sorted(
            set(all_date_series[duplicate_mask].tolist())
        )
        if "is_open" in frame.columns:
            status_by_date: dict[str, set[str]] = {}
            raw_status = frame["is_open"].astype(str).str.strip().str.lower()
            for date, status in zip(all_dates, raw_status):
                status_by_date.setdefault(date, set()).add(status)
            conflicts = [
                date for date in duplicate_dates
                if len(status_by_date.get(date, set())) > 1
            ]
            if conflicts:
                raise ValueError(
                    "trading_calendar contains conflicting is_open rows: "
                    f"{conflicts[:5]}"
                )
        raise ValueError(
            "trading_calendar contains duplicate dates: "
            f"{duplicate_dates[:5]}"
        )

    has_open_status = "is_open" in frame.columns
    if has_open_status:
        open_values = frame["is_open"].astype(str).str.strip().str.lower()
        invalid = ~open_values.isin({"0", "1", "false", "true"})
        if invalid.any():
            raise ValueError("trading_calendar.is_open must be boolean or 0/1")
        if coverage is None:
            expected_dates = set(
                pd.date_range(
                    min(all_dates), max(all_dates), freq="D"
                ).strftime("%Y%m%d")
            )
            missing_dates = sorted(expected_dates - set(all_dates))
            if missing_dates:
                raise ValueError(
                    "trading_calendar with is_open must contain one row for "
                    "every calendar date in its claimed interval; missing "
                    f"dates: {missing_dates[:5]}"
                )
        frame = frame[open_values.isin({"1", "true"})]

    dates = [
        normalize_trade_date(value, field=f"trading_calendar[{index}]")
        for index, value in frame[date_column].items()
    ]
    if not dates:
        raise ValueError("trading_calendar contains no open trading days")
    calendar = tuple(sorted(dates))
    if coverage is None:
        coverage_start, coverage_end = min(all_dates), max(all_dates)
        # Weekends are structurally closed, so an open-day-only source ending
        # on Friday can safely prove a requested Saturday/Sunday tail.  Never
        # infer across a weekday, where a holiday and a truncated download are
        # indistinguishable.
        coverage_start_ts = pd.Timestamp(coverage_start)
        while (coverage_start_ts - pd.Timedelta(days=1)).weekday() >= 5:
            coverage_start_ts -= pd.Timedelta(days=1)
        coverage_end_ts = pd.Timestamp(coverage_end)
        while (coverage_end_ts + pd.Timedelta(days=1)).weekday() >= 5:
            coverage_end_ts += pd.Timedelta(days=1)
        coverage_start = coverage_start_ts.strftime("%Y%m%d")
        coverage_end = coverage_end_ts.strftime("%Y%m%d")
    else:
        if (
            isinstance(coverage, (str, bytes, bytearray))
            or not isinstance(coverage, Iterable)
        ):
            raise TypeError(
                "trading_calendar_coverage must be a (start_date, end_date) pair"
            )
        coverage_values = tuple(coverage)
        if len(coverage_values) != 2:
            raise ValueError(
                "trading_calendar_coverage must contain exactly two dates"
            )
        coverage_start = normalize_trade_date(
            coverage_values[0], field="trading_calendar_coverage[0]"
        )
        coverage_end = normalize_trade_date(
            coverage_values[1], field="trading_calendar_coverage[1]"
        )
        if coverage_start > coverage_end:
            raise ValueError(
                "trading_calendar_coverage start must not be after end"
            )
    if coverage_start > calendar[0] or coverage_end < calendar[-1]:
        raise ValueError(
            "trading_calendar_coverage must enclose every supplied open day"
        )
    payload = json.dumps(
        {
            "open_days": calendar,
            "coverage_start": coverage_start,
            "coverage_end": coverage_end,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return (
        calendar,
        source_name,
        hashlib.sha256(payload).hexdigest(),
        coverage_start,
        coverage_end,
    )


def _default_dir(sub: str) -> str:
    """Resolve a default market-data directory from the shared config.

    ``diepi.futures`` shares result and metric contracts with
    ``diepi.backtest``, so callers relying on ``DATA_ROOT`` must set it before
    importing either package. Explicit constructor directories bypass this
    helper but do not create a second configuration namespace.
    """
    from ..backtest.config import PARQUET_ROOT
    return os.path.join(PARQUET_ROOT, sub)


class FuturesEngine:
    """Approximate index-futures research backtester.

    PnL = price_change x multiplier x lots (absolute yuan).
    Contract selection uses T-1 volume (no lookahead).
    MaxDD uses intraday LOW (long) / HIGH (short).
    Maintenance margin is checked once at the daily close using close price;
    exchange settlement prices and intraday liquidation are not modeled.
    Daily records: strictly one per day.
    Auto-validates results; raises FuturesValidationError if inconsistent.
    """

    def __init__(
        self,
        product: str,
        start_date: str,
        end_date: str,
        account_size: float = 300_000,
        lots: int = 1,
        commission_rate: float = 0.000023,
        slippage_points: float = 0.2,
        contract_data_dir: str = None,
        continuous_data_dir: str = None,
        contract_method: str = "volume_t1",
        min_days_to_expiry: int = 7,
        contract_expiry=None,
        roll_schedule=None,
        trading_calendar=None,
        trading_calendar_coverage: tuple[str, str] | None = None,
    ):
        self.last_result_contract: ResultContract | None = None
        if contract_data_dir is None:
            contract_data_dir = _default_dir("futures_daily")
        if continuous_data_dir is None:
            continuous_data_dir = _default_dir("futures_continuous")
        self._product = product
        self._start = normalize_trade_date(start_date, field="start_date")
        self._end = normalize_trade_date(end_date, field="end_date")
        if self._start > self._end:
            raise ValueError("start_date must be on or before end_date")
        if isinstance(account_size, bool) or not isinstance(account_size, Real):
            raise TypeError("account_size must be a real number")
        self._account_size = float(account_size)
        if not isfinite(self._account_size) or self._account_size <= 0:
            raise ValueError("account_size must be finite and positive")
        if isinstance(lots, bool) or not isinstance(lots, Integral):
            raise TypeError("lots must be an integer")
        self._lots = int(lots)
        if self._lots <= 0:
            raise ValueError("lots must be positive")
        if contract_method not in ("volume_t1", "mapping"):
            raise ValueError(
                "contract_method must be 'volume_t1' or 'mapping'")
        if roll_schedule is not None and contract_method != "mapping":
            raise ValueError(
                "an explicit roll_schedule requires contract_method='mapping'; "
                "volume_t1 rolls only on explicit contract expiry dates"
            )
        self._contract_method = contract_method
        if (
            isinstance(min_days_to_expiry, bool)
            or not isinstance(min_days_to_expiry, Integral)
        ):
            raise TypeError("min_days_to_expiry must be an integer")
        self._min_expiry = int(min_days_to_expiry)
        if self._min_expiry < 0:
            raise ValueError("min_days_to_expiry must be non-negative")

        spec = get_spec(product)
        self._multiplier = spec["multiplier"]
        self._margin_rate = spec["margin_rate"]

        self._cost = CostModel(commission_rate=commission_rate, slippage_points=slippage_points)
        self._metric_engine = MetricEngine()
        self._selector = ContractSelector(
            product,
            contract_data_dir,
            continuous_data_dir,
            contract_expiry=contract_expiry,
            roll_schedule=roll_schedule,
        )
        (
            self._trading_calendar,
            self._trading_calendar_source,
            self._trading_calendar_hash,
            self._trading_calendar_coverage_start,
            self._trading_calendar_coverage_end,
        ) = _load_trading_calendar(
            trading_calendar,
            coverage=trading_calendar_coverage,
        )

    # ─── Public API ───

    def run(self, signals: pd.DataFrame,
            policy: str = "strict") -> FuturesResult:
        """Run backtest from signals DataFrame.
        Required columns: trade_date (str), direction (LONG/SHORT/FLAT).

        policy='strict' requires one signal per trading day.  Sparse inputs are
        accepted only with explicit policy='event' (missing day = no new event)
        or policy='ffill' (carry the last target direction forward).
        """
        self.last_result_contract = None
        self._active_expected_observation_ids: tuple[str, ...] = ()
        self._active_observed_dates: list[str] = []
        self._active_signal_policy = "strict"
        try:
            policy = str(policy).strip().lower()
            if policy not in ("strict", "event", "ffill"):
                raise ValueError(
                    "signal policy must be 'strict', 'event', or 'ffill'")
            self._active_signal_policy = policy
            normalized = self._validate_signals(signals)
            signal_map = dict(zip(
                normalized["trade_date"],
                normalized["direction"],
            ))
            return self._execute(signal_map, policy=policy)
        except Exception:
            # Precondition failures have no declared expected scope and remain
            # plain exceptions.  Once execution starts, preserve its exact
            # observed progress without replacing the original exception.
            if self._active_expected_observation_ids:
                try:
                    self.last_result_contract = self._failed_result_contract()
                except Exception:
                    self.last_result_contract = None
            raise
        finally:
            self._active_expected_observation_ids = ()
            self._active_observed_dates = []
            self._active_signal_policy = "strict"

    def _failed_result_contract(self) -> ResultContract:
        tracker = OutcomeTracker(
            expected_observation_ids=self._active_expected_observation_ids,
            coverage_unit="futures_trading_day",
        )
        for trade_date in self._active_observed_dates:
            tracker.observe(f"{self._product}|{trade_date}", trade_date)
        for key, value in sorted(self._result_assumptions(
                self._active_signal_policy).items()):
            tracker.add_assumption(key, value)
        return tracker.finalize(
            ResultStatus.FAILED,
            reason_code="FUTURES_ENGINE_RUNTIME_ERROR",
            reason_message="Futures engine failed after execution began",
        )

    def _result_assumptions(self, signal_policy: str) -> dict[str, str]:
        metric_engine = getattr(self, "_metric_engine", MetricEngine())
        assumptions = dict(DEFAULT_ASSUMPTIONS)
        assumptions.update({
            "signal_policy": signal_policy,
            "contract_expiry_source": getattr(
                self._selector, "expiry_source", "explicit_test_selector"),
            "roll_schedule_source": getattr(
                self._selector, "roll_schedule_source", "none"),
            "contract_expiry_schedule_sha256": getattr(
                self._selector, "expiry_schedule_hash", "synthetic"),
            "roll_schedule_sha256": getattr(
                self._selector, "roll_schedule_hash", "none"),
            "trading_calendar_source": getattr(
                self, "_trading_calendar_source", "explicit_test_calendar"),
            "trading_calendar_sha256": getattr(
                self, "_trading_calendar_hash", "synthetic"),
            "trading_calendar_coverage_start": str(getattr(
                self, "_trading_calendar_coverage_start", "unspecified")),
            "trading_calendar_coverage_end": str(getattr(
                self, "_trading_calendar_coverage_end", "unspecified")),
            "contract_selection_method": str(self._contract_method),
            "roll_trigger_policy": (
                "mapping_change_or_contract_expiry"
                if self._contract_method == "mapping"
                else "contract_expiry_only"
            ),
            "minimum_days_to_expiry": str(self._min_expiry),
            "sparse_signal_window_seed": (
                "last_pre_window_signal_or_flat"
                if signal_policy in ("event", "ffill")
                else "not_applicable"
            ),
            "commission_rate_per_side": format(
                self._cost.commission_rate, ".17g"),
            "slippage_points_per_side": format(
                self._cost.slippage_points, ".17g"),
            "product_margin_rate": format(self._margin_rate, ".17g"),
            "event_journal_scope": FUTURES_EVENT_JOURNAL_SCOPE,
            "event_journal_completeness": (
                "diagnostic_only_not_order_fill_or_nav_replay"
            ),
            "event_journal_simulated_close_time": "15:00:00",
            "event_journal_same_close_order": "roll_then_margin_check",
        })
        assumptions.update(dict(metric_engine.config.assumptions()))
        for key, value in assumptions.items():
            if type(key) is not str or type(value) is not str:
                raise TypeError("futures assumptions must be dict[str, str]")
        return assumptions

    def run_from_csv(self, path: str,
                     policy: str = "strict") -> FuturesResult:
        self.last_result_contract = None
        df = pd.read_csv(path, dtype=str)
        df.columns = [c.strip().lower() for c in df.columns]
        return self.run(df, policy=policy)

    def run_from_combo_daily(self, combo_daily_path: str,
                             direction_when_invested: str = "LONG",
                             direction_when_idle: str = "SHORT",
                             policy: str = "strict") -> FuturesResult:
        self.last_result_contract = None
        daily = pd.read_csv(combo_daily_path, dtype=str)
        col = "date" if "date" in daily.columns else daily.columns[0]
        daily["trade_date"] = daily[col].astype(str).str.strip()
        daily["invested"] = daily["invested_weight"].astype(float) > 0
        rows = []
        for _, r in daily.iterrows():
            direction = direction_when_invested if r["invested"] else direction_when_idle
            rows.append({"trade_date": r["trade_date"], "direction": direction})
        return self.run(pd.DataFrame(rows), policy=policy)

    # ─── Validation ───

    def _validate_signals(self, signals: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(signals, pd.DataFrame):
            raise ValueError("signals must be a pandas DataFrame")
        if "trade_date" not in signals.columns:
            raise ValueError("signals must have 'trade_date' column")
        if "direction" not in signals.columns:
            raise ValueError("signals must have 'direction' column")
        normalized = signals[["trade_date", "direction"]].copy()
        normalized["trade_date"] = [
            normalize_trade_date(value, field=f"signals.trade_date[{index}]")
            for index, value in normalized["trade_date"].items()
        ]
        normalized["direction"] = (
            normalized["direction"].astype(str).str.strip().str.upper()
        )
        # Check duplicates only after ISO/compact dates share one namespace.
        dups = normalized["trade_date"].duplicated()
        if dups.any():
            dup_dates = normalized.loc[dups, "trade_date"].unique().tolist()
            raise ValueError(f"Duplicate trade_date entries: {dup_dates[:5]}")
        bad = normalized[
            ~normalized["direction"].isin(["LONG", "SHORT", "FLAT"])
        ]
        if not bad.empty:
            raise ValueError(f"Invalid direction values: {bad['direction'].unique().tolist()}")
        return normalized

    # ─── Margin check (Fix #5) ───

    def _required_margin(self, price: float) -> float:
        return (
            price * self._multiplier * self._lots * self._margin_rate
        )

    def _check_margin(self, price: float, nav: float) -> bool:
        """Return whether NAV covers static margin at the supplied checkpoint."""
        return nav >= self._required_margin(price)

    # ─── Core execution ───

    def _execute(self, signal_map: dict[str, str],
                 policy: str = "strict") -> FuturesResult:
        expected_calendar = getattr(self, "_trading_calendar", None)
        if expected_calendar is None:
            raise ValueError(
                "FuturesEngine requires an independent trading_calendar"
            )
        if not expected_calendar:
            raise ValueError(f"No trading days in {self._start}~{self._end}")
        coverage_start = getattr(
            self,
            "_trading_calendar_coverage_start",
            min(expected_calendar) if expected_calendar else None,
        )
        coverage_end = getattr(
            self,
            "_trading_calendar_coverage_end",
            max(expected_calendar) if expected_calendar else None,
        )
        if (
            coverage_start is None
            or coverage_end is None
            or self._start < coverage_start
            or self._end > coverage_end
        ):
            raise ValueError(
                "Independent trading_calendar coverage does not cover the "
                f"requested window {self._start}~{self._end}; "
                f"coverage={coverage_start}~{coverage_end}"
            )
        cal = [d for d in expected_calendar if self._start <= d <= self._end]
        if not cal:
            raise ValueError(f"No trading days in {self._start}~{self._end}")

        calendar_set = set(cal)
        complete_calendar_set = set(expected_calendar)
        window_signal_dates = {
            d for d in signal_map if self._start <= d <= self._end
        }
        overlap = calendar_set & window_signal_dates
        prior_signal_dates = sorted(
            d for d in signal_map
            if d < cal[0] and d in complete_calendar_set
        )
        seed_signal = (
            signal_map[prior_signal_dates[-1]] if prior_signal_dates else None
        )
        if not overlap and not (
                policy in ("event", "ffill") and seed_signal is not None):
            raise ValueError(
                "Signal/trading calendar has zero overlap in requested window "
                f"{self._start}~{self._end}"
            )
        non_trading = sorted(window_signal_dates - calendar_set)
        if non_trading:
            raise ValueError(
                "Signals contain non-trading dates in requested window: "
                f"{non_trading[:5]}"
            )
        missing_signals = sorted(calendar_set - window_signal_dates)
        if policy == "strict" and missing_signals:
            raise ValueError(
                f"Strict signal policy missing signals for "
                f"{len(missing_signals)} trading days: {missing_signals[:5]}"
            )

        expected_observation_ids = tuple(
            f"{self._product}|{trade_date}" for trade_date in cal
        )
        outcome = OutcomeTracker(
            expected_observation_ids=expected_observation_ids,
            coverage_unit="futures_trading_day",
        )
        self._active_expected_observation_ids = expected_observation_ids
        self._active_observed_dates = []

        market_calendar = {
            d for d in self._selector.calendar
            if self._start <= d <= self._end
        }
        missing_market_dates = sorted(calendar_set - market_calendar)
        unexpected_market_dates = sorted(market_calendar - calendar_set)
        if missing_market_dates or unexpected_market_dates:
            self._active_observed_dates = sorted(
                calendar_set & market_calendar
            )
            raise ValueError(
                "Independent trading calendar/market-data coverage mismatch: "
                f"missing_market_dates={missing_market_dates[:5]}, "
                f"unexpected_market_dates={unexpected_market_dates[:5]}"
            )
        event_journal = FuturesDiagnosticJournal(
            product=self._product, lots=self._lots)

        mult = self._multiplier
        lots = self._lots
        selector = self._selector

        def required_price(ts_code: str, trade_date: str, field: str,
                           checkpoint: str) -> float:
            value = selector.get_price(ts_code, trade_date, field)
            if value is None or not np.isfinite(value) or value <= 0:
                raise ValueError(
                    f"Missing valid {field} price for {ts_code} on "
                    f"{trade_date} at {checkpoint}"
                )
            return float(value)

        nav = float(self._account_size)
        daily_records: dict[str, dict] = {}  # keyed by date, guarantees one per day
        trade_records: list[dict] = []

        # State
        state = "FLAT"
        contract = ""
        entry_price = 0.0
        entry_date = ""
        entry_commission = 0.0
        hold_days = 0       # Fix #3: 0 on entry day, incremented at start of next day
        nav_at_entry = 0.0
        direction_sign = 0

        last_ffill_signal = seed_signal or "FLAT"
        for ii, d in enumerate(cal):
            dp = cal[ii - 1] if ii > 0 else None
            if d in signal_map:
                sig = signal_map[d]
                last_ffill_signal = sig
            elif policy == "ffill":
                sig = last_ffill_signal
            elif policy == "event":
                # No event means preserve the state present at today's open.
                # A prior margin call therefore stays flat, while a valid
                # carried position (including an expiry roll) remains targeted.
                sig = (
                    seed_signal
                    if ii == 0 and seed_signal is not None
                    else state
                )
            else:  # strict coverage was checked above
                raise AssertionError(f"missing strict signal for {d}")
            day_nav = nav
            day_worst = nav
            day_position = "FLAT"
            day_contract = ""
            allow_entry_today = True
            close_ref = None

            # ─── Open checkpoint: expiry and today's signal execute first ───
            if state != "FLAT":
                hold_days += 1  # Fix #3: increment at day start (entry day was 0, so first full day = 1)

                # Expiry comes only from the explicit contract schedule.  A
                # missing quote before that date is a data failure, not a
                # synthetic expiry/roll event.
                is_expired = (
                    selector.is_expired_on(contract, d)
                    if hasattr(selector, "is_expired_on")
                    else not selector.is_valid_on(contract, d)
                )
                has_data = (
                    selector.has_data_on(contract, d)
                    if hasattr(selector, "has_data_on")
                    else selector.is_valid_on(contract, d)
                )
                if is_expired:
                    raise ValueError(
                        f"Live contract {contract} reached {d} after its "
                        "pre-scheduled roll/expiry close"
                    )
                elif not has_data:
                    raise ValueError(
                        f"Missing market data for live contract {contract} on {d}; "
                        "refusing to treat a data gap as contract expiry"
                    )
                else:
                    # A FLAT/reversal signal is an open event.  It must execute
                    # before a maintenance check based on this day's close.
                    ref = (
                        required_price(
                            contract, dp, "close", "overnight reference")
                        if dp else entry_price
                    )
                    if sig != state:
                        exit_raw = required_price(
                            contract, d, "open", "signal change")
                        exit_price = self._cost.execution_price(
                            exit_raw, is_buy=(state == "SHORT"))
                        exit_comm = self._cost.commission(exit_price, mult, lots)
                        gap_pnl = direction_sign * (exit_price - ref) * mult * lots
                        nav += gap_pnl - exit_comm
                        trade_pnl = nav - nav_at_entry
                        trade_records.append(self._make_trade(
                            entry_date, d, state, contract, entry_price, exit_price,
                            hold_days, trade_pnl, nav_at_entry, "signal_change",
                            commission_yuan=entry_commission + exit_comm))

                        day_worst = min(day_worst, nav)
                        state = "FLAT"; contract = ""; hold_days = 0
                        direction_sign = 0
                        entry_commission = 0.0
                        # Fall through to entry logic if sig wants new direction
                    else:
                        close_ref = ref

            # ─── Entry logic (state is FLAT here, either from start or just exited) ───
            if allow_entry_today and state == "FLAT" and sig in ("LONG", "SHORT"):
                sel = selector.select(d, self._min_expiry, self._contract_method)
                if sel is not None:
                    op = required_price(sel.ts_code, d, "open", "entry")
                    # Fix #5: margin check
                    if not self._check_margin(op, nav):
                        pass  # insufficient margin, stay flat
                    else:
                        direction_sign = 1 if sig == "LONG" else -1
                        is_buy = (sig == "LONG")
                        exec_price = self._cost.execution_price(op, is_buy=is_buy)
                        entry_commission = self._cost.commission(
                            exec_price, mult, lots)

                        nav_at_entry = nav
                        nav -= entry_commission

                        state = sig
                        contract = sel.ts_code
                        entry_price = exec_price
                        entry_date = d
                        hold_days = 0  # Fix #3: entry day = 0, first full day will be 1
                        close_ref = exec_price
                        day_worst = min(day_worst, nav)

            # ─── Close checkpoint: mark and maintain the post-open position ───
            # This is intentionally close-only.  The approximation has no
            # intraday liquidation and currently uses close rather than settle.
            if state != "FLAT" and close_ref is not None:
                cl = required_price(contract, d, "close", "daily close mark")
                nav_before_close = nav
                close_pnl = direction_sign * (cl - close_ref) * mult * lots
                nav += close_pnl

                if state == "LONG":
                    worst_p = required_price(
                        contract, d, "low", "daily stress mark")
                    worst_pnl = (worst_p - close_ref) * mult * lots
                else:
                    worst_p = required_price(
                        contract, d, "high", "daily stress mark")
                    worst_pnl = -(worst_p - close_ref) * mult * lots
                day_worst = min(
                    day_worst, nav_before_close + worst_pnl, nav)

                expiry_date = (
                    selector.get_expiry(contract)
                    if hasattr(selector, "get_expiry") else ""
                )
                expiry_roll = expiry_date == d
                mapping_roll = (
                    self._contract_method == "mapping"
                    and hasattr(selector, "should_roll")
                    and selector.should_roll(contract, d)
                )
                # The final requested observation is liquidated by the normal
                # end-of-backtest close below.  Opening a replacement here
                # would create and immediately close exposure solely to pay a
                # spurious round trip of costs.
                if (expiry_roll or mapping_roll) and ii + 1 < len(cal):
                    old_state = state
                    old_contract = contract
                    old_direction_sign = direction_sign
                    roll_reason = (
                        "contract_expiry" if expiry_roll else "contract_roll"
                    )

                    exit_price = self._cost.execution_price(
                        cl, is_buy=(state == "SHORT"))
                    exit_comm = self._cost.commission(
                        exit_price, mult, lots)
                    # The old leg was already marked to raw close.  Only its
                    # exit slippage and commission remain at this checkpoint.
                    nav += (
                        direction_sign * (exit_price - cl) * mult * lots
                        - exit_comm
                    )
                    trade_pnl = nav - nav_at_entry
                    trade_records.append(self._make_trade(
                        entry_date, d, state, contract, entry_price, exit_price,
                        hold_days, trade_pnl, nav_at_entry, roll_reason,
                        commission_yuan=entry_commission + exit_comm))
                    day_worst = min(day_worst, nav)

                    state = "FLAT"
                    contract = ""
                    hold_days = 0
                    direction_sign = 0
                    entry_commission = 0.0

                    replacement_min_days = (
                        max(self._min_expiry, 1) if expiry_roll
                        else self._min_expiry
                    )
                    if hasattr(selector, "select_for_close_roll"):
                        replacement = selector.select_for_close_roll(
                            d,
                            replacement_min_days,
                            self._contract_method,
                        )
                    else:
                        replacement = selector.select(
                            d,
                            replacement_min_days,
                            self._contract_method,
                        )
                    if (
                        replacement is None
                        or replacement.ts_code == old_contract
                    ):
                        raise ValueError(
                            f"No valid replacement contract for {old_contract} "
                            f"on scheduled roll date {d}")

                    replacement_close = required_price(
                        replacement.ts_code, d, "close",
                        "roll replacement entry")
                    direction_sign = old_direction_sign
                    entry_price = self._cost.execution_price(
                        replacement_close, is_buy=(old_state == "LONG"))
                    entry_commission = self._cost.commission(
                        entry_price, mult, lots)
                    nav_at_entry = nav
                    nav -= entry_commission
                    # Entry occurs at this close, so entry slippage must be
                    # marked immediately instead of disappearing at T+1.
                    nav += (
                        direction_sign * (replacement_close - entry_price)
                        * mult * lots
                    )
                    state = old_state
                    contract = replacement.ts_code
                    entry_date = d
                    hold_days = 0
                    cl = replacement_close
                    event_journal.record_roll(
                        trade_date=d,
                        from_contract=old_contract,
                        to_contract=contract,
                        reason=roll_reason,
                        old_exit_price=exit_price,
                        new_entry_price=entry_price,
                        new_close_price=replacement_close,
                    )
                    day_worst = min(day_worst, nav)

                # Every position present at the agreed close checkpoint is
                # checked, including one opened or reversed at today's open.
                required_margin = self._required_margin(cl)
                margin_passed = self._check_margin(cl, nav)
                event_journal.record_margin(
                    trade_date=d,
                    contract=contract,
                    mark_price=cl,
                    nav=nav,
                    required_margin=required_margin,
                    passed=margin_passed,
                )
                if not margin_passed:
                    exit_price = self._cost.execution_price(
                        cl, is_buy=(state == "SHORT"))
                    exit_comm = self._cost.commission(exit_price, mult, lots)
                    exit_pnl = (
                        direction_sign * (exit_price - cl) * mult * lots
                    )
                    nav += exit_pnl - exit_comm
                    trade_pnl = nav - nav_at_entry
                    trade_records.append(self._make_trade(
                        entry_date, d, state, contract, entry_price, exit_price,
                        hold_days, trade_pnl, nav_at_entry, "margin_call",
                        commission_yuan=entry_commission + exit_comm))
                    day_worst = min(day_worst, nav)
                    state = "FLAT"; contract = ""; hold_days = 0
                    direction_sign = 0
                    entry_commission = 0.0
                    allow_entry_today = False
                    # Margin call is a close event; entry logic has already run,
                    # so same-day re-entry is impossible.
                else:
                    day_position = state
                    day_contract = contract

            # ─── Record exactly one row per day ───
            day_nav = nav
            daily_records[d] = {
                "date": d, "nav": day_nav, "nav_worst": min(day_worst, day_nav),
                "position": day_position, "contract": day_contract,
            }
            outcome.observe(f"{self._product}|{d}", d)
            self._active_observed_dates.append(d)

        # ─── Force close any open position at end ───
        if state != "FLAT" and cal:
            last_day = cal[-1]
            last_close = required_price(
                contract, last_day, "close", "end of backtest")
            exit_price = self._cost.execution_price(last_close, is_buy=(state == "SHORT"))
            exit_comm = self._cost.commission(exit_price, mult, lots)
            nav += (
                direction_sign * (exit_price - last_close) * mult * lots
                - exit_comm
            )
            trade_pnl = nav - nav_at_entry
            trade_records.append(self._make_trade(
                entry_date, last_day, state, contract, entry_price, exit_price,
                hold_days, trade_pnl, nav_at_entry, "end_of_backtest",
                commission_yuan=entry_commission + exit_comm))
            daily_records[last_day]["nav"] = nav
            daily_records[last_day]["nav_worst"] = min(
                daily_records[last_day]["nav_worst"], nav)
            daily_records[last_day]["position"] = "FLAT"
            daily_records[last_day]["contract"] = ""

        # ─── Build result ───
        ordered_records = [daily_records[d] for d in cal if d in daily_records]
        result = self._build_result(
            ordered_records,
            trade_records,
            len(cal),
            signal_policy=policy,
            event_journal_json=event_journal.to_json(),
        )
        for key in sorted(result.assumptions):
            outcome.add_assumption(key, result.assumptions[key])
        result_contract = outcome.finalize_completed()
        if result_contract.status is not ResultStatus.SUCCESS:
            raise FuturesValidationError(
                "Complete futures execution did not produce SUCCESS")
        result.result_contract = result_contract
        if not result.validate():
            errors = "\n".join(result.validation_errors)
            raise FuturesValidationError(
                f"Backtest validation failed after outcome finalization:\n{errors}")
        self.last_result_contract = result_contract
        return result

    def _make_trade(self, entry_date: str, exit_date: str, direction: str,
                    contract: str, entry_price: float, exit_price: float,
                    hold_days: int, pnl_yuan: float, nav_at_entry: float,
                    reason: str, *, commission_yuan: float = 0.0) -> dict:
        return {
            "entry_date": entry_date,
            "exit_date": exit_date,
            "direction": direction,
            "product": self._product,
            "contract": contract,
            "entry_price": round(entry_price, 2),
            "exit_price": round(exit_price, 2),
            "hold_days": hold_days,
            "commission_yuan": round(commission_yuan, 2),
            "pnl_yuan": round(pnl_yuan, 2),
            "pnl_pct": round(pnl_yuan / nav_at_entry, 4) if nav_at_entry > 0 else 0.0,
            "exit_reason": reason,
        }

    def _build_result(self, daily_records: list[dict], trade_records: list[dict],
                      calendar_len: int,
                      signal_policy: str = "strict",
                      event_journal_json: str | None = None) -> FuturesResult:
        daily_df = pd.DataFrame(daily_records)
        trades_df = pd.DataFrame(trade_records) if trade_records else pd.DataFrame()

        na = daily_df["nav"].values.astype(float)
        wa = daily_df["nav_worst"].values.astype(float)
        metric_engine = getattr(self, "_metric_engine", MetricEngine())
        metric_dates = [
            f"{value[:4]}-{value[4:6]}-{value[6:]}"
            for value in daily_df["date"].tolist()
        ]
        metrics = metric_engine.calculate(
            initial_nav=self._account_size,
            dates=metric_dates,
            close_nav=na.tolist(),
            intraday_low_nav=wa.tolist(),
        )
        mdd_w = metrics.max_drawdown_intraday_low_nav
        if mdd_w is None:
            raise RuntimeError("futures stress NAV did not produce drawdown")
        cal_c = (
            metrics.annual_return / metrics.max_drawdown_close_nav
            if metrics.max_drawdown_close_nav > 0 else 0.0
        )
        cal_w = (
            metrics.annual_return / mdd_w if mdd_w > 0 else 0.0
        )

        trade_metrics = calculate_trade_round_metrics(
            trades_df["pnl_yuan"].tolist() if not trades_df.empty else ()
        )
        n_trades = trade_metrics.closed_round_count
        avg_pnl = (
            float(trades_df["pnl_yuan"].mean()) if n_trades > 0 else 0.0
        )

        assumptions = self._result_assumptions(signal_policy)
        if event_journal_json is None:
            event_journal_json = EMPTY_FUTURES_EVENT_JOURNAL_JSON

        result = FuturesResult(
            product=self._product,
            start_date=self._start, end_date=self._end,
            account_size=self._account_size, lots=self._lots, multiplier=self._multiplier,
            final_nav=round(float(na[-1]), 2),
            total_return=round(metrics.total_return, 4),
            cagr=round(metrics.annual_return, 4),
            sharpe=(
                None if metrics.sharpe_ratio is None
                else round(metrics.sharpe_ratio, 3)
            ),
            max_drawdown_close=round(metrics.max_drawdown_close_nav, 4),
            max_drawdown_worst=round(mdd_w, 4),
            calmar_close=round(cal_c, 3), calmar_worst=round(cal_w, 3),
            trade_count=n_trades,
            win_rate=(
                None if trade_metrics.win_rate is None
                else round(trade_metrics.win_rate, 3)
            ),
            avg_trade_pnl=round(avg_pnl, 2),
            daily_nav=daily_df, trades=trades_df,
            expected_calendar_len=calendar_len,
            contract_selector=self._selector,  # pass for validation
            assumptions=assumptions,
            event_journal_json=event_journal_json,
        )

        if not result.validate():
            errors = "\n".join(result.validation_errors)
            raise FuturesValidationError(f"Backtest validation failed:\n{errors}")

        return result
