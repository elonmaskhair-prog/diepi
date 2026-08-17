"""Futures result payload with an explicit, auditable validation lifecycle."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..backtest.result_contract import ResultContract

from .journal import (
    EMPTY_FUTURES_EVENT_JOURNAL_JSON,
    FUTURES_EVENT_JOURNAL_SCOPE,
    validate_futures_journal_json,
)


ENGINE_SCOPE = "approximate_index_futures_research"
DEFAULT_ASSUMPTIONS = {
    "engine_scope": ENGINE_SCOPE,
    "commission_model": "per_side_execution_notional_rate",
    "slippage_model": "symmetric_points_embedded_in_execution_price",
    "margin_maintenance_checkpoint": "daily_close_only",
    "margin_mark_price": "close_price_not_exchange_settlement",
    "intraday_liquidation": "not_modeled",
    "margin_rate": "static_product_spec_not_historical",
    "roll_execution_checkpoint": "same_bar_close_exit_then_entry",
    "settlement_model": "cumulative_nav_close_mark_not_daily_settlement",
    "signal_policy": "strict",
    "contract_expiry_source": "explicit_schedule_required",
    "roll_schedule_source": "none",
    "trading_calendar_source": "explicit_schedule_required",
    "trading_calendar_sha256": "none",
    "metrics.schema_version": "1",
    "metrics.trading_days_per_year": "252",
    "metrics.risk_free_rate": "0.029999999999999999",
    "metrics.return_std_ddof": "1",
    "event_journal_scope": FUTURES_EVENT_JOURNAL_SCOPE,
    "event_journal_completeness": (
        "diagnostic_only_not_order_fill_or_nav_replay"
    ),
    "event_journal_simulated_close_time": "15:00:00",
    "event_journal_same_close_order": "roll_then_margin_check",
}


class FuturesValidationError(Exception):
    pass


class ValidationState(str, Enum):
    """Lifecycle state for payload consistency validation."""

    UNVALIDATED = "UNVALIDATED"
    VALID = "VALID"
    INVALID = "INVALID"


def _canonical_date(value: object) -> str:
    if type(value) is not str:
        raise ValueError("date must be a string")
    candidate = value
    if len(candidate) == 8 and candidate.isdigit():
        candidate = f"{candidate[:4]}-{candidate[4:6]}-{candidate[6:]}"
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        raise ValueError(f"invalid date {value!r}") from None


@dataclass
class FuturesResult:
    """Futures backtest result requiring an explicit ``validate`` call.

    Payload validation and run outcome are deliberately independent:
    ``validation_state`` says whether the tables are internally consistent,
    while ``result_contract`` says whether the producing run completed.
    """

    # Config echo
    product: str
    start_date: str
    end_date: str
    account_size: float
    lots: int
    multiplier: int
    engine_scope: str = ENGINE_SCOPE
    assumptions: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_ASSUMPTIONS))

    # Metrics
    final_nav: float = 0.0
    total_return: float = 0.0
    cagr: float = 0.0
    sharpe: float | None = None
    max_drawdown_close: float = 0.0
    max_drawdown_worst: float = 0.0
    calmar_close: float = 0.0
    calmar_worst: float = 0.0
    trade_count: int = 0
    win_rate: float | None = None
    avg_trade_pnl: float = 0.0

    # Data
    daily_nav: pd.DataFrame = field(default_factory=pd.DataFrame)
    trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    event_journal_json: str = EMPTY_FUTURES_EVENT_JOURNAL_JSON

    # Validation
    validation_errors: list[str] = field(default_factory=list, init=False)
    validation_state: ValidationState = field(
        default=ValidationState.UNVALIDATED, init=False)
    is_valid: bool = field(default=False, init=False)
    expected_calendar_len: int = 0
    contract_selector: object | None = None  # ContractSelector, avoids circular import
    result_contract: ResultContract | None = None

    def validate(self) -> bool:
        """Validate payload consistency and record a terminal validation state."""
        self.validation_errors = []
        self.validation_state = ValidationState.UNVALIDATED
        self.is_valid = False

        if self.result_contract is not None and not isinstance(
                self.result_contract, ResultContract):
            self.validation_errors.append(
                "result_contract must be a ResultContract or None")

        if self.engine_scope != ENGINE_SCOPE:
            self.validation_errors.append(
                f"engine_scope must be {ENGINE_SCOPE!r}")
        if type(self.assumptions) is not dict:
            self.validation_errors.append(
                "assumptions must be exactly dict[str, str]")
        else:
            non_string_items = [
                key for key, value in self.assumptions.items()
                if type(key) is not str or type(value) is not str
            ]
            if non_string_items:
                self.validation_errors.append(
                    "assumptions keys and values must be strings")
            required_assumptions = {
                "engine_scope",
                "commission_model",
                "slippage_model",
                "margin_maintenance_checkpoint",
                "margin_mark_price",
                "intraday_liquidation",
                "roll_execution_checkpoint",
                "settlement_model",
                "contract_expiry_source",
                "roll_schedule_source",
                "trading_calendar_source",
                "trading_calendar_sha256",
                "event_journal_scope",
                "event_journal_completeness",
            }
            missing_assumptions = sorted(
                required_assumptions - set(self.assumptions)
            )
            if missing_assumptions:
                self.validation_errors.append(
                    "assumptions is missing required keys: "
                    f"{missing_assumptions}")
            if self.assumptions.get("engine_scope") != self.engine_scope:
                self.validation_errors.append(
                    "assumptions.engine_scope must match engine_scope")
            if self.assumptions.get(
                    "event_journal_scope") != FUTURES_EVENT_JOURNAL_SCOPE:
                self.validation_errors.append(
                    "assumptions.event_journal_scope is invalid")
        try:
            json.dumps(self.assumptions, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            self.validation_errors.append(
                "assumptions must be JSON serializable without non-finite values")

        journal_events: tuple[dict, ...] = ()
        try:
            journal_events = validate_futures_journal_json(
                self.event_journal_json)
        except (TypeError, ValueError) as exc:
            self.validation_errors.append(
                f"event_journal_json is invalid: {exc}")

        for name in ("account_size", "final_nav"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float, np.number))
                or not np.isfinite(float(value))
                or float(value) <= 0
            ):
                self.validation_errors.append(
                    f"{name} must be finite and positive")
        if isinstance(self.trade_count, bool) or not isinstance(
                self.trade_count, (int, np.integer)):
            self.validation_errors.append(
                "trade_count must be a non-negative integer")
        elif self.trade_count < 0:
            self.validation_errors.append(
                "trade_count must be a non-negative integer")
        for name in (
            "total_return", "cagr", "max_drawdown_close",
            "max_drawdown_worst", "calmar_close", "calmar_worst",
            "avg_trade_pnl",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float, np.number))
                or not np.isfinite(float(value))
            ):
                self.validation_errors.append(f"{name} must be finite")
        if self.sharpe is not None and (
            isinstance(self.sharpe, bool)
            or not isinstance(self.sharpe, (int, float, np.number))
            or not np.isfinite(float(self.sharpe))
        ):
            self.validation_errors.append("sharpe must be finite or None")
        if self.win_rate is not None and (
            isinstance(self.win_rate, bool)
            or not isinstance(self.win_rate, (int, float, np.number))
            or not np.isfinite(float(self.win_rate))
            or not 0 <= float(self.win_rate) <= 1
        ):
            self.validation_errors.append("win_rate must be within [0, 1] or None")
        for name in ("max_drawdown_close", "max_drawdown_worst"):
            value = getattr(self, name)
            if isinstance(value, (int, float, np.number)) and np.isfinite(
                    float(value)) and not 0 <= float(value) <= 1:
                self.validation_errors.append(
                    f"{name} must be within [0, 1]")

        daily_columns: set[str] = set()
        if not isinstance(self.daily_nav, pd.DataFrame):
            self.validation_errors.append("daily_nav must be a DataFrame")
            daily_available = False
        elif self.daily_nav.empty:
            self.validation_errors.append("daily_nav must be non-empty")
            daily_available = False
        else:
            daily_columns = set(self.daily_nav.columns)
            missing = sorted({"date", "nav", "nav_worst"} - daily_columns)
            if missing:
                self.validation_errors.append(
                    f"daily_nav is missing required columns: {missing}")
            daily_available = not missing

        if not isinstance(self.trades, pd.DataFrame):
            self.validation_errors.append("trades must be a DataFrame")
            trades_available = False
            actual_trade_count = 0
        else:
            trades_available = True
            actual_trade_count = len(self.trades)

        # 1. NAV-trade consistency
        if (
            daily_available
            and trades_available
            and not self.trades.empty
            and "pnl_yuan" in self.trades.columns
        ):
            pnl_values = pd.to_numeric(
                self.trades["pnl_yuan"], errors="coerce").to_numpy()
            nav_values = pd.to_numeric(
                self.daily_nav["nav"], errors="coerce").to_numpy()
            if np.isfinite(pnl_values).all() and np.isfinite(nav_values).all():
                trade_sum = float(pnl_values.sum())
                try:
                    account_value = float(self.account_size)
                except (TypeError, ValueError):
                    account_value = float("nan")
                if np.isfinite(account_value):
                    nav_change = float(nav_values[-1]) - account_value
                    diff = abs(trade_sum - nav_change)
                    rounding_tolerance = 0.01 + 0.0051 * len(pnl_values)
                    if diff > rounding_tolerance:
                        self.validation_errors.append(
                            f"NAV inconsistency: trades_sum={trade_sum:+,.0f} "
                            f"nav_change={nav_change:+,.0f} diff={diff:,.4f} "
                            f"tolerance={rounding_tolerance:.4f}")

        # 2. Daily dates are valid, unique, and strictly increasing.
        if daily_available:
            raw_dates = self.daily_nav["date"].tolist()
            try:
                canonical_dates = [_canonical_date(value) for value in raw_dates]
            except ValueError as exc:
                self.validation_errors.append(f"Invalid daily calendar: {exc}")
            else:
                if len(canonical_dates) != len(set(canonical_dates)):
                    self.validation_errors.append(
                        "Duplicate daily records in daily_nav calendar")
                if canonical_dates != sorted(canonical_dates):
                    self.validation_errors.append(
                        "daily_nav calendar must be strictly increasing")

            nav = pd.to_numeric(self.daily_nav["nav"], errors="coerce").to_numpy()
            nav_worst = pd.to_numeric(
                self.daily_nav["nav_worst"], errors="coerce").to_numpy()
            if not np.isfinite(nav).all():
                self.validation_errors.append(
                    "daily_nav.nav must contain only finite numbers")
            if not np.isfinite(nav_worst).all():
                self.validation_errors.append(
                    "daily_nav.nav_worst must contain only finite numbers")
            if np.isfinite(nav).all() and np.isfinite(nav_worst).all():
                if (nav_worst > nav).any():
                    self.validation_errors.append(
                        "daily_nav.nav_worst cannot exceed daily_nav.nav")
                if (nav_worst <= 0).any():
                    self.validation_errors.append(
                        "daily_nav.nav_worst must stay positive")
                try:
                    final_value = float(self.final_nav)
                    account_value = float(self.account_size)
                    return_value = float(self.total_return)
                except (TypeError, ValueError):
                    final_value = account_value = return_value = float("nan")
                if np.isfinite(final_value) and not np.isclose(
                    float(nav[-1]), final_value, rtol=0.0, atol=0.011
                ):
                    self.validation_errors.append(
                        "final_nav must match the last daily_nav.nav")
                if (
                    np.isfinite(account_value)
                    and account_value > 0
                    and np.isfinite(return_value)
                    and np.isfinite(final_value)
                ):
                    expected_return = (
                        final_value / account_value - 1.0
                    )
                    if not np.isclose(
                        return_value, expected_return,
                        rtol=0.0, atol=0.000051,
                    ):
                        self.validation_errors.append(
                            "total_return must match final_nav/account_size")

        # 3. No position past expiry
        if daily_available and self.contract_selector is not None:
            missing_position_columns = {"position", "contract"} - daily_columns
            if missing_position_columns:
                self.validation_errors.append(
                    "daily_nav needs position and contract columns for expiry validation")
            else:
                pos_days = self.daily_nav[
                    (self.daily_nav["position"] != "FLAT")
                    & (self.daily_nav["contract"] != "")
                ]
                for _, row in pos_days.iterrows():
                    if not self.contract_selector.is_valid_on(
                            row["contract"], row["date"]):
                        self.validation_errors.append(
                            f"Position in {row['contract']} on {row['date']} "
                            "but contract has no data (expired?)")
                        break  # one is enough

        # 4. NAV always positive
        if daily_available:
            nav = pd.to_numeric(self.daily_nav["nav"], errors="coerce")
            finite_nav = nav[np.isfinite(nav.to_numpy())]
            if not finite_nav.empty and float(finite_nav.min()) <= 0:
                min_nav = float(finite_nav.min())
                self.validation_errors.append(
                    f"NAV went to {min_nav:,.0f} (bankruptcy)")

        # 5. MDD invariant: worst >= close
        if (
            isinstance(self.max_drawdown_worst, (int, float, np.number))
            and isinstance(self.max_drawdown_close, (int, float, np.number))
            and np.isfinite(float(self.max_drawdown_worst))
            and np.isfinite(float(self.max_drawdown_close))
            and self.max_drawdown_worst < self.max_drawdown_close - 1e-6
        ):
            self.validation_errors.append(
                f"MDD invariant violated: worst={self.max_drawdown_worst:.4f} < close={self.max_drawdown_close:.4f}")

        # 6. Day count
        if self.expected_calendar_len > 0:
            actual = len(self.daily_nav) if isinstance(
                self.daily_nav, pd.DataFrame) else 0
            if actual != self.expected_calendar_len:
                self.validation_errors.append(
                    f"Day count mismatch: expected={self.expected_calendar_len} actual={actual}")

        # 7. Trade date ordering (same-day trades allowed for single-day / expiry-roll)
        if trades_available and not self.trades.empty:
            missing_trade_columns = {
                "entry_date", "exit_date", "pnl_yuan"
            } - set(self.trades.columns)
            if missing_trade_columns:
                self.validation_errors.append(
                    "trades is missing required columns: "
                    f"{sorted(missing_trade_columns)}")
            else:
                pnl_values = pd.to_numeric(
                    self.trades["pnl_yuan"], errors="coerce").to_numpy()
                if not np.isfinite(pnl_values).all():
                    self.validation_errors.append(
                        "trades.pnl_yuan must contain only finite numbers")
                else:
                    expected_win_rate = float((pnl_values > 0).mean())
                    win_rate_is_numeric = (
                        isinstance(self.win_rate, (int, float, np.number))
                        and not isinstance(self.win_rate, bool)
                        and np.isfinite(float(self.win_rate))
                    )
                    if (
                        not win_rate_is_numeric
                        or not np.isclose(
                            float(self.win_rate), expected_win_rate,
                            rtol=0.0, atol=0.00051,
                        )
                    ):
                        self.validation_errors.append(
                            "win_rate must match trades.pnl_yuan")
                    expected_avg = float(pnl_values.mean())
                    if (
                        isinstance(self.avg_trade_pnl,
                                   (int, float, np.number))
                        and np.isfinite(float(self.avg_trade_pnl))
                        and not np.isclose(
                            float(self.avg_trade_pnl), expected_avg,
                            rtol=0.0, atol=0.011,
                        )
                    ):
                        self.validation_errors.append(
                            "avg_trade_pnl must match trades.pnl_yuan")
                if "commission_yuan" in self.trades.columns:
                    commissions = pd.to_numeric(
                        self.trades["commission_yuan"],
                        errors="coerce",
                    ).to_numpy()
                    if (
                        not np.isfinite(commissions).all()
                        or (commissions < 0).any()
                    ):
                        self.validation_errors.append(
                            "trades.commission_yuan must be finite and non-negative")
                bad = self.trades[
                    self.trades["entry_date"] > self.trades["exit_date"]
                ]
                if not bad.empty:
                    self.validation_errors.append(
                        f"{len(bad)} trades have entry_date > exit_date (time travel)")

        # The futures journal is deliberately diagnostic-only, but for engine
        # results it must still be complete for the two facts it claims:
        # every real close margin check and every real close roll.
        if self.product == "COMBINED":
            if journal_events:
                self.validation_errors.append(
                    "COMBINED results must not synthesize margin/roll events")
        else:
            expected_margin: dict[str, str] = {}
            if (
                daily_available
                and {"position", "contract"}.issubset(daily_columns)
            ):
                for _, row in self.daily_nav.iterrows():
                    if row["position"] not in ("FLAT", "COMBINED"):
                        try:
                            compact_date = _canonical_date(
                                row["date"]).replace("-", "")
                        except ValueError:
                            continue
                        expected_margin[compact_date] = str(row["contract"])

            trade_rows = (
                self.trades.reset_index(drop=True).to_dict("records")
                if isinstance(self.trades, pd.DataFrame) else []
            )
            expected_rolls: list[tuple[str, str, str, str]] = []
            for index, trade in enumerate(trade_rows):
                reason = trade.get("exit_reason")
                exit_date = str(trade.get("exit_date", ""))
                if reason in ("margin_call", "end_of_backtest"):
                    expected_margin[exit_date] = str(
                        trade.get("contract", ""))
                if reason in ("contract_roll", "contract_expiry"):
                    replacement = next((
                        candidate
                        for candidate in trade_rows[index + 1:]
                        if str(candidate.get("entry_date", "")) == exit_date
                    ), None)
                    if replacement is None:
                        self.validation_errors.append(
                            "roll trade has no same-close replacement trade")
                    else:
                        expected_rolls.append((
                            exit_date,
                            str(trade.get("contract", "")),
                            str(replacement.get("contract", "")),
                            str(reason),
                        ))

            actual_margin = []
            actual_rolls = []
            expected_id_prefix = f"futures-event:{self.product}:"
            expected_account_id = f"futures:{self.product}:research_account"
            for event in journal_events:
                if not event["event_id"].startswith(expected_id_prefix):
                    self.validation_errors.append(
                        "event_id must be scoped to the result product")
                event_date = event["simulated_time"][:10].replace("-", "")
                payload = event["payload"]
                if event["event_type"] == "margin":
                    if payload["account_id"] != expected_account_id:
                        self.validation_errors.append(
                            "margin account_id must be scoped to the result product")
                    attributes = payload["attributes"]
                    if attributes["check_passed"] != (
                            attributes["nav"] >= payload["amount"]):
                        self.validation_errors.append(
                            "margin check_passed does not match nav/required margin")
                    try:
                        margin_rate = float(
                            self.assumptions["product_margin_rate"])
                        expected_amount = (
                            float(attributes["mark_price"])
                            * int(self.multiplier)
                            * int(self.lots)
                            * margin_rate
                        )
                    except (KeyError, TypeError, ValueError, OverflowError):
                        expected_amount = float("nan")
                    if (
                        not np.isfinite(expected_amount)
                        or not np.isclose(
                            float(payload["amount"]), expected_amount,
                            rtol=1e-12, atol=1e-9,
                        )
                    ):
                        self.validation_errors.append(
                            "margin event amount does not match configured model")
                    actual_margin.append((event_date, payload["contract"]))
                else:
                    actual_rolls.append((
                        event_date,
                        payload["from_contract"],
                        payload["to_contract"],
                        payload["attributes"]["reason"],
                    ))
            if sorted(actual_margin) != sorted(expected_margin.items()):
                self.validation_errors.append(
                    "margin journal events do not match real close checks")
            if sorted(actual_rolls) != sorted(expected_rolls):
                self.validation_errors.append(
                    "roll journal events do not match real close rolls")

        # An empty closed-trade population has no mathematically defined win
        # rate.  Older producers emitted 0.0; normalize it at the validation
        # boundary so all consumers see the explicit null semantics.
        if self.trade_count != actual_trade_count:
            self.validation_errors.append(
                f"Trade count mismatch: metric={self.trade_count} actual={actual_trade_count}")
        if actual_trade_count == 0:
            self.win_rate = None

        self.is_valid = len(self.validation_errors) == 0
        self.validation_state = (
            ValidationState.VALID if self.is_valid else ValidationState.INVALID
        )
        return self.is_valid

    def summary(self) -> str:
        """Formatted summary string."""
        sharpe = "N/A" if self.sharpe is None else f"{self.sharpe:.2f}"
        win_rate = "N/A" if self.win_rate is None else f"{self.win_rate:.0%}"
        if self.validation_state is ValidationState.UNVALIDATED:
            validation = ValidationState.UNVALIDATED.value
        elif (
            self.validation_state is ValidationState.VALID
            and self.is_valid
        ):
            validation = "OK"
        else:
            validation = "FAILED"
        lines = [
            f"Engine scope:     {self.engine_scope}",
            f"Signal policy:   {self.assumptions.get('signal_policy', 'unknown')}",
            f"Expiry source:   {self.assumptions.get('contract_expiry_source', 'unknown')}",
            f"Product:         {self.product}",
            f"Period:          {self.start_date} ~ {self.end_date}",
            f"Account:         {self.account_size:,.0f}",
            f"Final NAV:       {self.final_nav:,.0f} ({self.final_nav/self.account_size:.1f}x)",
            f"CAGR:            {self.cagr:.1%}",
            f"MaxDD (close):   {self.max_drawdown_close:.1%}",
            f"MaxDD (worst):   {self.max_drawdown_worst:.1%}",
            f"Calmar (close):  {self.calmar_close:.2f}",
            f"Calmar (worst):  {self.calmar_worst:.2f}",
            f"Sharpe:          {sharpe}",
            f"Trades:          {self.trade_count}",
            f"Win rate:        {win_rate}",
            f"Valid:           {validation}",
            "Event journal:   margin/roll diagnostics only; not an "
            "order/fill/NAV replay ledger",
            "Margin boundary: daily close only; close price (not exchange "
            "settlement); no intraday liquidation",
        ]
        if self.validation_errors:
            lines.append("Errors:")
            for e in self.validation_errors:
                lines.append(f"  - {e}")
        return "\n".join(lines)

    def yearly_breakdown(self) -> dict[str, dict]:
        """Per-year return, max drawdown, trade count."""
        if self.daily_nav.empty:
            return {}
        df = self.daily_nav.copy()
        df["year"] = df["date"].str[:4]
        result = {}
        for yr, grp in df.groupby("year"):
            nav = grp["nav"].values
            ret = nav[-1] / nav[0] - 1
            peak = np.maximum.accumulate(nav)
            dd = ((peak - nav) / np.where(peak > 0, peak, 1.0)).max()
            n_trades = len(self.trades[self.trades["entry_date"].str[:4] == yr]) if not self.trades.empty else 0
            result[str(yr)] = {"return": round(float(ret), 4), "max_dd": round(float(dd), 4), "trades": n_trades}
        return result

    def to_csv(self, output_dir: str) -> None:
        """Save daily_nav, trades, summary to directory."""
        p = Path(output_dir)
        p.mkdir(parents=True, exist_ok=True)
        if not self.daily_nav.empty:
            self.daily_nav.to_csv(p / "daily_nav.csv", index=False)
        if not self.trades.empty:
            self.trades.to_csv(p / "trades.csv", index=False)
        (p / "event_journal.json").write_text(
            self.event_journal_json, encoding="utf-8")
        (p / "summary.txt").write_text(self.summary(), encoding="utf-8")

    def to_dict(self) -> dict:
        return {
            "product": self.product, "start_date": self.start_date, "end_date": self.end_date,
            "engine_scope": self.engine_scope,
            "assumptions": dict(self.assumptions),
            "event_journal_json": self.event_journal_json,
            "account_size": self.account_size,
            "lots": self.lots,
            "multiplier": self.multiplier,
            "final_nav": round(self.final_nav, 2),
            "total_return": round(self.total_return, 4),
            "cagr": round(self.cagr, 4),
            "sharpe": None if self.sharpe is None else round(self.sharpe, 3),
            "max_drawdown_close": round(self.max_drawdown_close, 4),
            "max_drawdown_worst": round(self.max_drawdown_worst, 4),
            "calmar_close": round(self.calmar_close, 3),
            "calmar_worst": round(self.calmar_worst, 3),
            "trade_count": self.trade_count,
            "avg_trade_pnl": round(self.avg_trade_pnl, 2),
            "win_rate": (
                None if self.win_rate is None else round(self.win_rate, 3)
            ),
            "is_valid": self.is_valid,
            "validation_state": self.validation_state.value,
            "validation_errors": list(self.validation_errors),
            "expected_calendar_len": self.expected_calendar_len,
            "result_contract": (
                self.result_contract.to_dict()
                if self.result_contract is not None else None
            ),
        }


__all__ = [
    "DEFAULT_ASSUMPTIONS",
    "ENGINE_SCOPE",
    "FuturesResult",
    "FuturesValidationError",
    "ValidationState",
]
