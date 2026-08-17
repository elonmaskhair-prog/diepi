"""Engine-specific payload adapters for RunArtifact v1.

Cash single, cash portfolio, parallel aggregate, and index-futures result
containers have explicit v1 adapters.  Orchestration-only contracts are built
at this boundary rather than being injected into the engine return types.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import math
from numbers import Integral, Real
import re
from typing import Any, Dict, Mapping, Optional, Tuple, Type

import numpy as np
import pandas as pd

from diepi.backtest.broker.events import ExecutionEventJournal
from diepi.backtest.broker.replay import (
    CashAuditBundle,
    CashReplaySeed,
    cash_replay_trade_records,
)
from diepi.backtest.broker.target_execution import TargetExecutionBundle
from diepi.backtest.comparison import ComparisonBundle
from diepi.backtest.engine.backtest_engine import BacktestResult
from diepi.backtest.engine.parallel_runner import ParallelResult
from diepi.backtest.engine.portfolio_engine import PortfolioResult
from diepi.backtest.result_contract import (
    ActualInterval,
    DataCoverage,
    ResultAssumption,
    ResultContract,
    ResultReason,
    ResultStatus,
    ResultWarning,
)
from diepi.futures.result import (
    ENGINE_SCOPE as FUTURES_ENGINE_SCOPE,
    FuturesResult,
    ValidationState,
)

from .models import (
    EngineKind,
    RunOutcome,
    canonical_json_bytes,
    load_json_bytes,
    normalize_json_value,
    validate_artifact_relative_path,
)


DATAFRAME_SCHEMA = "diepi.dataframe"
DATAFRAME_SCHEMA_VERSION = 1
RECORD_LIST_SCHEMA = "diepi.record_list"
RECORD_LIST_SCHEMA_VERSION = 1
CASH_SINGLE_RESULT_SCHEMA = "diepi.cash_single_result_snapshot"
CASH_PORTFOLIO_RESULT_SCHEMA = "diepi.cash_portfolio_result_snapshot"
CASH_RESULT_SCHEMA_VERSION = 1
PARALLEL_RESULT_SCHEMA = "diepi.cash_parallel_result_snapshot"
PARALLEL_CHILDREN_SCHEMA = "diepi.cash_parallel_children"
PARALLEL_RESULT_SCHEMA_VERSION = 1
FUTURES_RESULT_SCHEMA = "diepi.index_futures_result_snapshot"
FUTURES_RESULT_SCHEMA_VERSION = 1

_ROLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_JSON_MEDIA_TYPE = "application/json"
_TEXT_MEDIA_TYPE = "text/plain; charset=utf-8"


@dataclass(frozen=True)
class ArtifactPayload:
    role: str
    path: str
    media_type: str
    data: bytes

    def __post_init__(self) -> None:
        if type(self.role) is not str or not _ROLE_RE.fullmatch(self.role):
            raise ValueError("artifact payload role must be lower snake case")
        validate_artifact_relative_path(self.path)
        if type(self.media_type) is not str or not self.media_type:
            raise ValueError("artifact payload media_type must be non-empty")
        if type(self.data) is not bytes:
            raise TypeError("artifact payload data must be exactly bytes")


def _exact_keys(value: Any, expected, label: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise TypeError("{} must be exactly object".format(label))
    expected_keys = set(expected)
    actual_keys = set(value)
    if actual_keys != expected_keys:
        raise ValueError(
            "{} keys mismatch: missing={}, unknown={}".format(
                label,
                sorted(expected_keys - actual_keys),
                sorted(actual_keys - expected_keys),
            )
        )
    return value


def _finite_number(value: Any, field: str, *, optional: bool = False):
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError("{} must be a real number".format(field))
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("{} must be finite".format(field))
    return number


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError("{} must be an integer".format(field))
    normalized = int(value)
    if normalized < 0:
        raise ValueError("{} must be non-negative".format(field))
    return normalized


def _text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise TypeError("{} must be exactly str".format(field))
    if value != value.strip() or (not allow_empty and not value):
        raise ValueError("{} must be canonical text".format(field))
    if "\x00" in value:
        raise ValueError("{} must not contain NUL".format(field))
    return value


def _encode_scalar(value: Any, path: str) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return {"$diepi": "missing"}
    if isinstance(value, np.bool_):
        value = bool(value)
    elif isinstance(value, np.integer):
        value = int(value)
    elif isinstance(value, np.floating):
        value = float(value)
    elif isinstance(value, np.datetime64):
        value = pd.Timestamp(value)

    if type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if math.isnan(value):
            return {"$diepi": "missing"}
        if not math.isfinite(value):
            raise ValueError("{} must be finite".format(path))
        return value
    if isinstance(value, pd.Timestamp):
        if value.tz is not None:
            raise ValueError("{} must be timezone-naive".format(path))
        return {"$diepi": "timestamp_ns", "value": int(value.value)}
    if isinstance(value, datetime):
        timestamp = pd.Timestamp(value)
        if timestamp.tz is not None:
            raise ValueError("{} must be timezone-naive".format(path))
        return {"$diepi": "timestamp_ns", "value": int(timestamp.value)}
    if isinstance(value, date):
        return {"$diepi": "date", "value": value.isoformat()}
    raise TypeError(
        "{} contains unsupported scalar type {}".format(
            path, type(value).__name__
        )
    )


def _decode_scalar(value: Any, path: str) -> Any:
    if type(value) is dict and "$diepi" in value:
        tag = value.get("$diepi")
        if tag == "missing" and set(value) == {"$diepi"}:
            return None
        if tag == "timestamp_ns" and set(value) == {"$diepi", "value"}:
            raw = value["value"]
            if type(raw) is not int:
                raise TypeError("{} timestamp value must be int".format(path))
            return pd.Timestamp(raw, unit="ns")
        if tag == "date" and set(value) == {"$diepi", "value"}:
            raw = value["value"]
            if type(raw) is not str:
                raise TypeError("{} date value must be str".format(path))
            try:
                return date.fromisoformat(raw)
            except ValueError as exc:
                raise ValueError("{} contains invalid date".format(path)) from exc
        raise ValueError("{} contains unsupported tagged scalar".format(path))
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("{} must be finite".format(path))
        return value
    raise TypeError("{} contains non-scalar JSON value".format(path))


def _restore_series(values, dtype: str, name: str) -> pd.Series:
    if type(dtype) is not str or not dtype:
        raise ValueError("{} dtype must be non-empty text".format(name))
    if dtype.startswith("datetime64["):
        return pd.Series(pd.to_datetime(values), dtype=dtype)
    try:
        return pd.Series(values, dtype=dtype)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "{} values do not match declared dtype {!r}".format(name, dtype)
        ) from exc


def dataframe_to_bytes(frame: pd.DataFrame) -> bytes:
    """Serialize the canonical daily result table without float rounding."""

    if type(frame) is not pd.DataFrame:
        raise TypeError("daily_values must be exactly pandas.DataFrame")
    if isinstance(frame.index, pd.MultiIndex):
        raise TypeError("RunArtifact v1 does not support MultiIndex daily tables")
    if frame.index.name is not None and type(frame.index.name) is not str:
        raise TypeError("daily table index name must be str or None")
    if any(type(column) is not str for column in frame.columns):
        raise TypeError("daily table column names must be strings")
    if not frame.columns.is_unique:
        raise ValueError("daily table columns must be unique")

    columns = []
    for column in frame.columns:
        series = frame[column]
        columns.append(
            {
                "dtype": str(series.dtype),
                "name": column,
                "values": [
                    _encode_scalar(value, "daily_values.{}".format(column))
                    for value in series.tolist()
                ],
            }
        )
    payload = {
        "columns": columns,
        "index": {
            "dtype": str(frame.index.dtype),
            "name": frame.index.name,
            "values": [
                _encode_scalar(value, "daily_values.index")
                for value in frame.index.tolist()
            ],
        },
        "schema": DATAFRAME_SCHEMA,
        "schema_version": DATAFRAME_SCHEMA_VERSION,
    }
    return canonical_json_bytes(payload)


def dataframe_from_bytes(payload: bytes) -> pd.DataFrame:
    value = _exact_keys(
        load_json_bytes(payload, "daily values"),
        ("columns", "index", "schema", "schema_version"),
        "daily table",
    )
    if value["schema"] != DATAFRAME_SCHEMA:
        raise ValueError("unsupported daily table schema")
    if value["schema_version"] != DATAFRAME_SCHEMA_VERSION:
        raise ValueError("unsupported daily table schema_version")
    index_payload = _exact_keys(
        value["index"], ("dtype", "name", "values"), "daily table index"
    )
    if index_payload["name"] is not None and type(index_payload["name"]) is not str:
        raise TypeError("daily table index name must be str or None")
    if type(index_payload["values"]) is not list:
        raise TypeError("daily table index values must be list")
    index_values = [
        _decode_scalar(item, "daily_values.index")
        for item in index_payload["values"]
    ]
    index_series = _restore_series(
        index_values, index_payload["dtype"], "daily table index"
    )
    index = pd.Index(index_series.array, name=index_payload["name"])

    if type(value["columns"]) is not list:
        raise TypeError("daily table columns must be list")
    data = {}
    for position, item in enumerate(value["columns"]):
        column = _exact_keys(
            item, ("dtype", "name", "values"),
            "daily table column {}".format(position),
        )
        name = column["name"]
        if type(name) is not str or not name:
            raise ValueError("daily table column names must be non-empty strings")
        if name in data:
            raise ValueError("daily table column names must be unique")
        if type(column["values"]) is not list:
            raise TypeError("daily table column values must be list")
        if len(column["values"]) != len(index):
            raise ValueError("daily table columns must match index length")
        decoded = [
            _decode_scalar(value, "daily_values.{}".format(name))
            for value in column["values"]
        ]
        data[name] = _restore_series(decoded, column["dtype"], name).array
    restored = pd.DataFrame(data, index=index)
    if dataframe_to_bytes(restored) != payload:
        raise ValueError("daily table payload is not canonical")
    return restored


def record_list_to_bytes(records: Any) -> bytes:
    if type(records) not in (list, tuple):
        raise TypeError("record list must be exactly list or tuple")
    normalized = []
    for index, record in enumerate(records):
        if type(record) is not dict:
            raise TypeError("records[{}] must be exactly object".format(index))
        normalized.append(normalize_json_value(record, "records[{}]".format(index)))
    return canonical_json_bytes(
        {
            "records": normalized,
            "schema": RECORD_LIST_SCHEMA,
            "schema_version": RECORD_LIST_SCHEMA_VERSION,
        }
    )


def record_list_from_bytes(payload: bytes) -> list:
    value = _exact_keys(
        load_json_bytes(payload, "record list"),
        ("records", "schema", "schema_version"),
        "record list",
    )
    if value["schema"] != RECORD_LIST_SCHEMA:
        raise ValueError("unsupported record-list schema")
    if value["schema_version"] != RECORD_LIST_SCHEMA_VERSION:
        raise ValueError("unsupported record-list schema_version")
    if type(value["records"]) is not list:
        raise TypeError("records must be exactly list")
    for index, record in enumerate(value["records"]):
        if type(record) is not dict:
            raise TypeError("records[{}] must be exactly object".format(index))
        normalize_json_value(record, "records[{}]".format(index))
    if record_list_to_bytes(value["records"]) != payload:
        raise ValueError("record-list payload is not canonical")
    return value["records"]


_COMMON_SNAPSHOT_KEYS = frozenset(
    {
        "annual_return",
        "end_date",
        "final_value",
        "initial_cash",
        "max_drawdown",
        "max_drawdown_close_nav",
        "max_drawdown_intraday_high_to_low",
        "max_drawdown_intraday_low_nav",
        "schema",
        "schema_version",
        "sharpe_ratio",
        "start_date",
        "total_return",
        "trade_count",
        "win_rate",
    }
)
_SINGLE_SNAPSHOT_KEYS = _COMMON_SNAPSHOT_KEYS | {"symbol"}
_PORTFOLIO_SNAPSHOT_KEYS = _COMMON_SNAPSHOT_KEYS | {
    "benchmark_code",
    "benchmark_return",
    "error",
    "excess_return",
    "execution_stats",
}


def _common_snapshot(result: Any, schema: str) -> Dict[str, Any]:
    return {
        "annual_return": _finite_number(result.annual_return, "annual_return"),
        "end_date": _text(result.end_date, "end_date"),
        "final_value": _finite_number(result.final_value, "final_value"),
        "initial_cash": _finite_number(result.initial_cash, "initial_cash"),
        "max_drawdown": _finite_number(result.max_drawdown, "max_drawdown"),
        "max_drawdown_close_nav": _finite_number(
            result.max_drawdown_close_nav, "max_drawdown_close_nav"
        ),
        "max_drawdown_intraday_high_to_low": _finite_number(
            result.max_drawdown_intraday_high_to_low,
            "max_drawdown_intraday_high_to_low",
            optional=True,
        ),
        "max_drawdown_intraday_low_nav": _finite_number(
            result.max_drawdown_intraday_low_nav,
            "max_drawdown_intraday_low_nav",
        ),
        "schema": schema,
        "schema_version": CASH_RESULT_SCHEMA_VERSION,
        "sharpe_ratio": _finite_number(
            result.sharpe_ratio, "sharpe_ratio", optional=True
        ),
        "start_date": _text(result.start_date, "start_date"),
        "total_return": _finite_number(result.total_return, "total_return"),
        "trade_count": _nonnegative_int(result.trade_count, "trade_count"),
        "win_rate": _finite_number(result.win_rate, "win_rate", optional=True),
    }


def _single_snapshot(result: BacktestResult) -> Dict[str, Any]:
    value = _common_snapshot(result, CASH_SINGLE_RESULT_SCHEMA)
    value["symbol"] = _text(result.symbol, "symbol")
    return value


def _portfolio_snapshot(result: PortfolioResult) -> Dict[str, Any]:
    value = _common_snapshot(result, CASH_PORTFOLIO_RESULT_SCHEMA)
    value.update(
        {
            "benchmark_code": _text(
                result.benchmark_code, "benchmark_code", allow_empty=True
            ),
            "benchmark_return": _finite_number(
                result.benchmark_return, "benchmark_return"
            ),
            "error": _text(result.error, "error", allow_empty=True),
            "excess_return": _finite_number(
                result.excess_return, "excess_return"
            ),
            "execution_stats": normalize_json_value(
                result.execution_stats, "execution_stats"
            ),
        }
    )
    return value


def _parse_common_snapshot(value: Dict[str, Any], schema: str) -> Dict[str, Any]:
    if value["schema"] != schema:
        raise ValueError("unexpected cash result snapshot schema")
    if value["schema_version"] != CASH_RESULT_SCHEMA_VERSION:
        raise ValueError("unsupported cash result snapshot schema_version")
    return {
        "start_date": _text(value["start_date"], "start_date"),
        "end_date": _text(value["end_date"], "end_date"),
        "initial_cash": _finite_number(value["initial_cash"], "initial_cash"),
        "final_value": _finite_number(value["final_value"], "final_value"),
        "total_return": _finite_number(value["total_return"], "total_return"),
        "annual_return": _finite_number(value["annual_return"], "annual_return"),
        "max_drawdown": _finite_number(value["max_drawdown"], "max_drawdown"),
        "trade_count": _nonnegative_int(value["trade_count"], "trade_count"),
        "win_rate": _finite_number(value["win_rate"], "win_rate", optional=True),
        "sharpe_ratio": _finite_number(
            value["sharpe_ratio"], "sharpe_ratio", optional=True
        ),
        "max_drawdown_close_nav": _finite_number(
            value["max_drawdown_close_nav"], "max_drawdown_close_nav"
        ),
        "max_drawdown_intraday_low_nav": _finite_number(
            value["max_drawdown_intraday_low_nav"],
            "max_drawdown_intraday_low_nav",
        ),
        "max_drawdown_intraday_high_to_low": _finite_number(
            value["max_drawdown_intraday_high_to_low"],
            "max_drawdown_intraday_high_to_low",
            optional=True,
        ),
    }


def _validate_daily_scope(frame: pd.DataFrame, contract: ResultContract) -> None:
    coverage = contract.data_coverage
    interval = contract.actual_interval
    if coverage is None or coverage.actual_observations == 0:
        if not frame.empty:
            raise ValueError("zero-observation result must have empty daily_values")
        return
    if len(frame) != coverage.actual_observations:
        raise ValueError("daily_values length must equal actual observations")
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise TypeError("observed cash result requires a DatetimeIndex")
    if frame.index.tz is not None:
        raise ValueError("daily_values index must be timezone-naive")
    if not frame.index.is_unique or not frame.index.is_monotonic_increasing:
        raise ValueError("daily_values index must be unique and increasing")
    observed_start = frame.index[0].date().isoformat()
    observed_end = frame.index[-1].date().isoformat()
    if interval is None or (
        observed_start != interval.start_date or observed_end != interval.end_date
    ):
        raise ValueError("daily_values endpoints must equal actual_interval")


def _validate_cash_result(result: Any, outcome: RunOutcome) -> None:
    if getattr(result, "result_contract", None) != outcome.result_contract:
        raise ValueError("cash result contract must equal run outcome contract")
    result._validate_target_execution()
    result._validate_cash_audit()
    _validate_daily_scope(result.daily_values, outcome.result_contract)
    if outcome.result_contract.status is ResultStatus.SUCCESS:
        if result.target_execution is None:
            raise ValueError("SUCCESS cash artifact requires target_execution")
        if result.cash_audit is None:
            raise ValueError("SUCCESS cash artifact requires cash_audit")


def _json_payload(role: str, path: str, value: Any) -> ArtifactPayload:
    return ArtifactPayload(
        role=role,
        path=path,
        media_type=_JSON_MEDIA_TYPE,
        data=canonical_json_bytes(value),
    )


class CashResultAdapter:
    engine_kind: EngineKind
    result_type: Type[Any]
    snapshot_schema: str

    def build_payloads(self, outcome: RunOutcome) -> Tuple[ArtifactPayload, ...]:
        if outcome.engine_kind is not self.engine_kind:
            raise TypeError("outcome engine_kind does not match adapter")
        result = outcome.result
        if result is None:
            if outcome.result_role is not None:
                raise ValueError("result_role requires an in-memory result to save")
            return ()
        if type(result) is not self.result_type:
            raise TypeError(
                "{} adapter requires exact {}".format(
                    self.engine_kind.value, self.result_type.__name__
                )
            )
        _validate_cash_result(result, outcome)

        snapshot = self._snapshot(result)
        payloads = [
            _json_payload("result", "result.json", snapshot),
            ArtifactPayload(
                role="daily_values",
                path="tables/daily_values.json",
                media_type=_JSON_MEDIA_TYPE,
                data=dataframe_to_bytes(result.daily_values),
            ),
            ArtifactPayload(
                role="trades",
                path="tables/trades.json",
                media_type=_JSON_MEDIA_TYPE,
                data=record_list_to_bytes(result.trades),
            ),
        ]
        if result.target_execution is not None:
            payloads.append(
                ArtifactPayload(
                    role="target_execution",
                    path="evidence/target_execution.json",
                    media_type=_JSON_MEDIA_TYPE,
                    data=result.target_execution.to_json().encode("utf-8"),
                )
            )
        if result.cash_audit is not None:
            payloads.extend(
                (
                    ArtifactPayload(
                        role="cash_replay_seed",
                        path="evidence/cash_replay_seed.json",
                        media_type=_JSON_MEDIA_TYPE,
                        data=result.cash_audit.seed.to_json().encode("utf-8"),
                    ),
                    ArtifactPayload(
                        role="execution_event_journal",
                        path="evidence/execution_event_journal.json",
                        media_type=_JSON_MEDIA_TYPE,
                        data=result.cash_audit.journal_json.encode("utf-8"),
                    ),
                )
            )
        payloads.extend(self._extra_payloads(result))
        return tuple(payloads)

    def load_result(
        self, outcome: RunOutcome, payloads: Mapping[str, bytes]
    ) -> Any:
        if outcome.engine_kind is not self.engine_kind:
            raise TypeError("outcome engine_kind does not match adapter")
        if outcome.result_role is None:
            return None
        required = {"result", "daily_values", "trades"}
        if outcome.result_contract.status is ResultStatus.SUCCESS:
            required.update(
                {
                    "target_execution",
                    "cash_replay_seed",
                    "execution_event_journal",
                }
            )
        missing = sorted(required - set(payloads))
        if missing:
            raise ValueError("cash artifact is missing roles: {}".format(missing))

        snapshot = load_json_bytes(payloads["result"], "result.json")
        daily_values = dataframe_from_bytes(payloads["daily_values"])
        stored_trades = record_list_from_bytes(payloads["trades"])

        target_execution = None
        if "target_execution" in payloads:
            target_execution = TargetExecutionBundle.from_dict(
                load_json_bytes(
                    payloads["target_execution"], "target_execution.json"
                )
            )

        cash_audit = None
        has_seed = "cash_replay_seed" in payloads
        has_journal = "execution_event_journal" in payloads
        if has_seed != has_journal:
            raise ValueError("cash replay seed and journal must be recorded together")
        if has_seed:
            seed_text = payloads["cash_replay_seed"].decode("utf-8")
            journal_text = payloads["execution_event_journal"].decode("utf-8")
            seed = CashReplaySeed.from_json(seed_text)
            journal = ExecutionEventJournal.from_json(journal_text)
            cash_audit = CashAuditBundle.from_parts(seed, journal)
            replay_trades = cash_replay_trade_records(cash_audit)
            if stored_trades != replay_trades:
                raise ValueError("persisted trades must equal cash replay fill view")

        result = self._restore(
            snapshot=snapshot,
            outcome=outcome,
            daily_values=daily_values,
            trades=stored_trades,
            target_execution=target_execution,
            cash_audit=cash_audit,
            payloads=payloads,
        )
        _validate_cash_result(result, outcome)
        if self._snapshot(result) != snapshot:
            raise ValueError("cash result snapshot contains inconsistent fields")
        return result

    def _snapshot(self, result: Any) -> Dict[str, Any]:
        raise NotImplementedError

    def _extra_payloads(self, result: Any) -> Tuple[ArtifactPayload, ...]:
        return ()

    def _restore(self, **kwargs):
        raise NotImplementedError


class CashSingleAdapter(CashResultAdapter):
    engine_kind = EngineKind.CASH_SINGLE
    result_type = BacktestResult
    snapshot_schema = CASH_SINGLE_RESULT_SCHEMA

    def _snapshot(self, result: BacktestResult) -> Dict[str, Any]:
        return _single_snapshot(result)

    def _restore(
        self,
        *,
        snapshot,
        outcome,
        daily_values,
        trades,
        target_execution,
        cash_audit,
        payloads,
    ) -> BacktestResult:
        value = _exact_keys(snapshot, _SINGLE_SNAPSHOT_KEYS, "single result")
        common = _parse_common_snapshot(value, CASH_SINGLE_RESULT_SCHEMA)
        return BacktestResult(
            symbol=_text(value["symbol"], "symbol"),
            daily_values=daily_values,
            trades=trades,
            result_contract=outcome.result_contract,
            target_execution=target_execution,
            cash_audit=cash_audit,
            **common
        )


class CashPortfolioAdapter(CashResultAdapter):
    engine_kind = EngineKind.CASH_PORTFOLIO
    result_type = PortfolioResult
    snapshot_schema = CASH_PORTFOLIO_RESULT_SCHEMA

    def _snapshot(self, result: PortfolioResult) -> Dict[str, Any]:
        return _portfolio_snapshot(result)

    def _extra_payloads(
        self, result: PortfolioResult
    ) -> Tuple[ArtifactPayload, ...]:
        payloads = [
            ArtifactPayload(
                role="positions",
                path="tables/positions.json",
                media_type=_JSON_MEDIA_TYPE,
                data=record_list_to_bytes(result.position_history),
            )
        ]
        if result.comparisons is not None:
            payloads.append(
                ArtifactPayload(
                    role="comparisons",
                    path="evidence/comparisons.json",
                    media_type=_JSON_MEDIA_TYPE,
                    data=result.comparisons.to_json().encode("utf-8"),
                )
            )
        return tuple(payloads)

    def _restore(
        self,
        *,
        snapshot,
        outcome,
        daily_values,
        trades,
        target_execution,
        cash_audit,
        payloads,
    ) -> PortfolioResult:
        value = _exact_keys(
            snapshot, _PORTFOLIO_SNAPSHOT_KEYS, "portfolio result"
        )
        common = _parse_common_snapshot(value, CASH_PORTFOLIO_RESULT_SCHEMA)
        if "positions" not in payloads:
            raise ValueError("portfolio artifact is missing positions")
        positions = record_list_from_bytes(payloads["positions"])
        comparisons = None
        if "comparisons" in payloads:
            comparisons = ComparisonBundle.from_dict(
                load_json_bytes(payloads["comparisons"], "comparisons.json")
            )
        execution_stats = value["execution_stats"]
        if type(execution_stats) is not dict:
            raise TypeError("execution_stats must be exactly object")
        normalize_json_value(execution_stats, "execution_stats")
        return PortfolioResult(
            daily_values=daily_values,
            trades=trades,
            position_history=positions,
            execution_stats=execution_stats,
            benchmark_code=_text(
                value["benchmark_code"], "benchmark_code", allow_empty=True
            ),
            benchmark_return=_finite_number(
                value["benchmark_return"], "benchmark_return"
            ),
            excess_return=_finite_number(value["excess_return"], "excess_return"),
            error=_text(value["error"], "error", allow_empty=True),
            comparisons=comparisons,
            result_contract=outcome.result_contract,
            target_execution=target_execution,
            cash_audit=cash_audit,
            **common
        )


def build_parallel_outcome(result: ParallelResult) -> RunOutcome:
    """Build an aggregate contract from ParallelResult's explicit evidence.

    The aggregate does not inherit one child's contract.  Its observations are
    requested symbols, while the ordered daily scope remains separately
    recorded in the result snapshot and each child keeps its own contract.
    """

    if type(result) is not ParallelResult:
        raise TypeError("result must be exactly ParallelResult")
    total = _nonnegative_int(result.total_symbols, "total_symbols")
    success = _nonnegative_int(result.success_count, "success_count")
    failed = _nonnegative_int(result.failed_count, "failed_count")
    if success + failed != total:
        raise ValueError("parallel success_count + failed_count must equal total")
    requested = tuple(result.requested_symbols)
    if total == 0 or len(requested) != total or len(set(requested)) != total:
        raise ValueError(
            "parallel requested_symbols must uniquely enumerate total_symbols"
        )
    if set(result.results) | set(result.errors) != set(requested):
        raise ValueError(
            "parallel results and errors must partition requested_symbols"
        )
    if len(result.results) != success or len(result.errors) != failed:
        raise ValueError("parallel child counts do not match result mappings")
    for symbol, child in result.results.items():
        if type(child) is not BacktestResult:
            raise TypeError("parallel accepted children must be BacktestResult")
        contract = child.result_contract
        if type(contract) is not ResultContract or not contract.is_rankable:
            raise ValueError("parallel accepted children require SUCCESS contracts")
        if child.symbol != symbol:
            raise ValueError("parallel child symbol does not match mapping key")
        _validate_cash_result(
            child,
            RunOutcome.from_result(
                child, engine_kind=EngineKind.CASH_SINGLE
            ),
        )
    for symbol, detail in result.errors.items():
        _text(symbol, "parallel error symbol")
        _text(detail, "parallel error detail")

    assumptions = (
        ResultAssumption(
            "aggregation.parallel_scope", "requested_symbol_outcomes"
        ),
        ResultAssumption(
            "aggregation.parallel_child_contracts",
            "persisted_per_symbol_without_synthesis",
        ),
        ResultAssumption(
            "aggregation.parallel_daily_scope",
            "ordered_dates_recorded_separately",
        ),
    )
    if result.is_rankable:
        status = ResultStatus.SUCCESS
        reason = None
        warnings = ()
        coverage = DataCoverage(total, total, 1.0)
    else:
        status = ResultStatus.PARTIAL if success else ResultStatus.INVALID
        reason = ResultReason(
            "PARALLEL_INCOMPLETE",
            result.ranking_error or "Parallel result is not rankable",
        )
        warnings = (
            ResultWarning(
                "PARALLEL_CHILD_FAILURES",
                "One or more requested child outcomes are not rankable",
            ),
        ) if failed else ()
        coverage = DataCoverage(
            total, success, 0.0 if total == 0 else success / total
        )
    interval = None
    if success:
        intervals = tuple(
            child.result_contract.actual_interval
            for child in result.results.values()
        )
        interval = ActualInterval(
            min(item.start_date for item in intervals),
            max(item.end_date for item in intervals),
        )
    if result.is_rankable:
        expected_scope = tuple(result.ranking_observation_ids)
        for symbol, child in result.results.items():
            child_scope = tuple(
                value.date().isoformat()
                for value in child.daily_values.index
            )
            if child_scope != expected_scope:
                raise ValueError(
                    "rankable parallel child {!r} does not match aggregate "
                    "ordered daily scope".format(symbol)
                )
    contract = ResultContract(
        status=status,
        reason=reason,
        warnings=warnings,
        assumptions=assumptions,
        actual_interval=interval,
        data_coverage=coverage,
    )
    return RunOutcome(
        engine_kind=EngineKind.CASH_PARALLEL,
        result_contract=contract,
        result_role="result",
        result=result,
    )


class ParallelAdapter:
    engine_kind = EngineKind.CASH_PARALLEL
    result_type = ParallelResult

    _SNAPSHOT_KEYS = frozenset(
        {
            "avg_annual_return",
            "avg_max_drawdown",
            "avg_return",
            "avg_sharpe",
            "avg_win_rate",
            "end_date",
            "failed_count",
            "initial_cash",
            "ranking_error",
            "ranking_observation_ids",
            "ranking_scope",
            "requested_symbols",
            "schema",
            "schema_version",
            "start_date",
            "success_count",
            "top_performers",
            "total_symbols",
            "universe_assumptions",
            "universe_warnings",
            "worst_performers",
        }
    )

    def _validate(self, outcome: RunOutcome) -> ParallelResult:
        if outcome.engine_kind is not self.engine_kind:
            raise TypeError("outcome engine_kind does not match adapter")
        result = outcome.result
        if type(result) is not ParallelResult:
            raise TypeError("parallel adapter requires exact ParallelResult")
        rebuilt = build_parallel_outcome(result)
        if rebuilt.result_contract != outcome.result_contract:
            raise ValueError(
                "parallel outcome contract must equal the evidence-built contract"
            )
        return result

    @staticmethod
    def _snapshot(result: ParallelResult) -> Dict[str, Any]:
        ranking_scope = result.ranking_scope
        if ranking_scope is not None:
            if type(ranking_scope) is not tuple or len(ranking_scope) != 4:
                raise TypeError("parallel ranking_scope must be a four-item tuple")
            scope_value = [
                _text(ranking_scope[0], "ranking_scope.start"),
                _text(ranking_scope[1], "ranking_scope.end"),
                _nonnegative_int(ranking_scope[2], "ranking_scope.expected"),
                _nonnegative_int(ranking_scope[3], "ranking_scope.actual"),
            ]
        else:
            scope_value = None
        return {
            "avg_annual_return": _finite_number(
                result.avg_annual_return, "avg_annual_return"
            ),
            "avg_max_drawdown": _finite_number(
                result.avg_max_drawdown, "avg_max_drawdown"
            ),
            "avg_return": _finite_number(result.avg_return, "avg_return"),
            "avg_sharpe": _finite_number(
                result.avg_sharpe, "avg_sharpe", optional=True
            ),
            "avg_win_rate": _finite_number(
                result.avg_win_rate, "avg_win_rate", optional=True
            ),
            "end_date": _text(result.end_date, "end_date"),
            "failed_count": _nonnegative_int(
                result.failed_count, "failed_count"
            ),
            "initial_cash": _finite_number(result.initial_cash, "initial_cash"),
            "ranking_error": (
                None
                if result.ranking_error is None
                else _text(result.ranking_error, "ranking_error")
            ),
            "ranking_observation_ids": [
                _text(value, "ranking_observation_id")
                for value in result.ranking_observation_ids
            ],
            "ranking_scope": scope_value,
            "requested_symbols": [
                _text(value, "requested_symbol")
                for value in result.requested_symbols
            ],
            "schema": PARALLEL_RESULT_SCHEMA,
            "schema_version": PARALLEL_RESULT_SCHEMA_VERSION,
            "start_date": _text(result.start_date, "start_date"),
            "success_count": _nonnegative_int(
                result.success_count, "success_count"
            ),
            "top_performers": normalize_json_value(
                result.top_performers, "top_performers"
            ),
            "total_symbols": _nonnegative_int(
                result.total_symbols, "total_symbols"
            ),
            "universe_assumptions": normalize_json_value(
                result.universe_assumptions, "universe_assumptions"
            ),
            "universe_warnings": normalize_json_value(
                result.universe_warnings, "universe_warnings"
            ),
            "worst_performers": normalize_json_value(
                result.worst_performers, "worst_performers"
            ),
        }

    def build_payloads(self, outcome: RunOutcome) -> Tuple[ArtifactPayload, ...]:
        result = self._validate(outcome)
        child_payloads = []
        payloads = [
            _json_payload("result", "result.json", self._snapshot(result))
        ]
        for symbol in result.requested_symbols:
            if symbol in result.results:
                child = result.results[symbol]
                index = len(child_payloads)
                child_outcome = RunOutcome.from_result(
                    child, engine_kind=EngineKind.CASH_SINGLE
                )
                child_bundle = CashSingleAdapter().build_payloads(child_outcome)
                role_by_source = {}
                for member in child_bundle:
                    role = "parallel_child_{:04d}_{}".format(index, member.role)
                    path = "children/{:04d}/{}".format(index, member.path)
                    payloads.append(
                        ArtifactPayload(
                            role=role,
                            path=path,
                            media_type=member.media_type,
                            data=member.data,
                        )
                    )
                    role_by_source[member.role] = role
                child_payloads.append(
                    {
                        "error": None,
                        "payload_roles": role_by_source,
                        "result_contract": child.result_contract.to_dict(),
                        "symbol": symbol,
                    }
                )
            else:
                child_payloads.append(
                    {
                        "error": result.errors[symbol],
                        "payload_roles": None,
                        "result_contract": None,
                        "symbol": symbol,
                    }
                )
        children = {
            "children": child_payloads,
            "schema": PARALLEL_CHILDREN_SCHEMA,
            "schema_version": PARALLEL_RESULT_SCHEMA_VERSION,
        }
        payloads.append(
            _json_payload("parallel_children", "evidence/children.json", children)
        )
        return tuple(payloads)

    def load_result(
        self, outcome: RunOutcome, payloads: Mapping[str, bytes]
    ) -> ParallelResult:
        missing = {"result", "parallel_children"} - set(payloads)
        if missing:
            raise ValueError("parallel artifact is missing roles: {}".format(
                sorted(missing)
            ))
        snapshot = _exact_keys(
            load_json_bytes(payloads["result"], "parallel result"),
            self._SNAPSHOT_KEYS,
            "parallel result",
        )
        if (
            snapshot["schema"] != PARALLEL_RESULT_SCHEMA
            or snapshot["schema_version"] != PARALLEL_RESULT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported parallel result snapshot schema")
        children_root = _exact_keys(
            load_json_bytes(payloads["parallel_children"], "parallel children"),
            ("children", "schema", "schema_version"),
            "parallel children",
        )
        if (
            children_root["schema"] != PARALLEL_CHILDREN_SCHEMA
            or children_root["schema_version"] != PARALLEL_RESULT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported parallel children schema")
        if type(children_root["children"]) is not list:
            raise TypeError("parallel children must be exactly list")
        requested = tuple(snapshot["requested_symbols"])
        if any(type(value) is not str for value in requested):
            raise TypeError("requested_symbols must contain strings")
        results = {}
        errors = {}
        seen = []
        for index, raw in enumerate(children_root["children"]):
            child = _exact_keys(
                raw,
                (
                    "error", "payload_roles", "result_contract", "symbol"
                ),
                "parallel child {}".format(index),
            )
            symbol = _text(child["symbol"], "parallel child symbol")
            seen.append(symbol)
            if child["error"] is not None:
                if (
                    child["payload_roles"] is not None
                    or child["result_contract"] is not None
                ):
                    raise ValueError("failed parallel child must not carry a result")
                errors[symbol] = _text(child["error"], "parallel child error")
                continue
            if type(child["payload_roles"]) is not dict:
                raise TypeError("successful child payload_roles must be object")
            child_payload_map = {}
            for source_role, artifact_role in child["payload_roles"].items():
                if type(source_role) is not str or type(artifact_role) is not str:
                    raise TypeError("parallel child payload role map must be text")
                if artifact_role not in payloads:
                    raise ValueError(
                        "parallel child references a missing artifact role"
                    )
                child_payload_map[source_role] = payloads[artifact_role]
            child_contract = ResultContract.from_dict(
                child["result_contract"]
            )
            child_outcome = RunOutcome(
                engine_kind=EngineKind.CASH_SINGLE,
                result_contract=child_contract,
                result_role="result",
            )
            restored = CashSingleAdapter().load_result(
                child_outcome, child_payload_map
            )
            if restored.symbol != symbol:
                raise ValueError("parallel child symbol mismatch")
            results[symbol] = restored
        if tuple(seen) != requested:
            raise ValueError(
                "parallel children must exactly follow requested_symbols order"
            )
        scope = snapshot["ranking_scope"]
        ranking_scope = None if scope is None else tuple(scope)
        result = ParallelResult(
            start_date=_text(snapshot["start_date"], "start_date"),
            end_date=_text(snapshot["end_date"], "end_date"),
            initial_cash=_finite_number(snapshot["initial_cash"], "initial_cash"),
            total_symbols=_nonnegative_int(snapshot["total_symbols"], "total_symbols"),
            success_count=_nonnegative_int(snapshot["success_count"], "success_count"),
            failed_count=_nonnegative_int(snapshot["failed_count"], "failed_count"),
            avg_return=_finite_number(snapshot["avg_return"], "avg_return"),
            avg_annual_return=_finite_number(snapshot["avg_annual_return"], "avg_annual_return"),
            avg_max_drawdown=_finite_number(snapshot["avg_max_drawdown"], "avg_max_drawdown"),
            avg_sharpe=_finite_number(snapshot["avg_sharpe"], "avg_sharpe", optional=True),
            avg_win_rate=_finite_number(snapshot["avg_win_rate"], "avg_win_rate", optional=True),
            top_performers=snapshot["top_performers"],
            worst_performers=snapshot["worst_performers"],
            results=results,
            errors=errors,
            universe_assumptions=snapshot["universe_assumptions"],
            universe_warnings=snapshot["universe_warnings"],
            ranking_scope=ranking_scope,
            ranking_observation_ids=tuple(snapshot["ranking_observation_ids"]),
            ranking_error=snapshot["ranking_error"],
            requested_symbols=requested,
        )
        rebuilt = build_parallel_outcome(result)
        if rebuilt.result_contract != outcome.result_contract:
            raise ValueError("parallel aggregate contract does not match evidence")
        if self._snapshot(result) != snapshot:
            raise ValueError("parallel snapshot contains inconsistent fields")
        return result


def _canonical_futures_dates(result: FuturesResult) -> Tuple[str, ...]:
    if type(result.daily_nav) is not pd.DataFrame or result.daily_nav.empty:
        raise ValueError("futures daily_nav must be a non-empty DataFrame")
    if "date" not in result.daily_nav.columns:
        raise ValueError("futures daily_nav must contain date")
    values = []
    for raw in result.daily_nav["date"].tolist():
        try:
            values.append(pd.to_datetime(str(raw)).date().isoformat())
        except (TypeError, ValueError):
            raise ValueError("futures daily_nav contains invalid date") from None
    result_dates = tuple(values)
    if result_dates != tuple(sorted(set(result_dates))):
        raise ValueError("futures daily_nav dates must be unique and increasing")
    return result_dates


def build_futures_outcome(result: FuturesResult) -> RunOutcome:
    """Build an explicit orchestration outcome, never an implicit SUCCESS."""

    if type(result) is not FuturesResult:
        raise TypeError("result must be exactly FuturesResult")
    if not result.validate():
        raise ValueError(
            "futures result must validate before artifacting: {}".format(
                result.validation_errors
            )
        )
    dates = _canonical_futures_dates(result)
    embedded = result.result_contract
    if embedded is not None:
        if type(embedded) is not ResultContract:
            raise TypeError("futures result_contract must be exactly ResultContract")
        contract = embedded
    else:
        expected = _nonnegative_int(
            result.expected_calendar_len, "expected_calendar_len"
        )
        if expected <= 0:
            raise ValueError(
                "legacy futures outcome requires positive expected_calendar_len"
            )
        if expected < len(dates):
            raise ValueError("expected_calendar_len cannot be below daily scope")
        status = ResultStatus.SUCCESS if expected == len(dates) else ResultStatus.PARTIAL
        reason = None if status is ResultStatus.SUCCESS else ResultReason(
            "FUTURES_WINDOW_PARTIAL",
            "Validated daily futures scope is shorter than expected calendar",
        )
        contract = ResultContract(
            status=status,
            reason=reason,
            warnings=(
                ResultWarning(
                    "ORCHESTRATION_CONTRACT_SYNTHESIZED",
                    "Legacy FuturesResult had no embedded ResultContract",
                ),
            ),
            assumptions=(
                ResultAssumption(
                    "orchestration.contract_source",
                    "validated_futures_daily_scope_v1",
                ),
                ResultAssumption(
                    "orchestration.expected_scope_source",
                    "futures_result.expected_calendar_len",
                ),
                ResultAssumption(
                    "orchestration.futures_validation",
                    "validation_state_valid",
                ),
            ),
            actual_interval=ActualInterval(dates[0], dates[-1]),
            data_coverage=DataCoverage(
                expected, len(dates), len(dates) / expected
            ),
        )
    coverage = contract.data_coverage
    interval = contract.actual_interval
    if (
        coverage is None
        or coverage.actual_observations != len(dates)
        or interval is None
        or interval.start_date != dates[0]
        or interval.end_date != dates[-1]
    ):
        raise ValueError("futures ResultContract must match exact daily scope")
    return RunOutcome(
        engine_kind=EngineKind.INDEX_FUTURES,
        result_contract=contract,
        result_role="result",
        result=result,
    )


class FuturesAdapter:
    engine_kind = EngineKind.INDEX_FUTURES
    result_type = FuturesResult
    _SNAPSHOT_KEYS = frozenset(
        {
            "account_size",
            "assumptions",
            "avg_trade_pnl",
            "cagr",
            "calmar_close",
            "calmar_worst",
            "end_date",
            "engine_scope",
            "expected_calendar_len",
            "final_nav",
            "lots",
            "max_drawdown_close",
            "max_drawdown_worst",
            "multiplier",
            "product",
            "schema",
            "schema_version",
            "sharpe",
            "start_date",
            "total_return",
            "trade_count",
            "win_rate",
        }
    )

    @staticmethod
    def _snapshot(result: FuturesResult) -> Dict[str, Any]:
        return {
            "account_size": _finite_number(result.account_size, "account_size"),
            "assumptions": normalize_json_value(result.assumptions, "assumptions"),
            "avg_trade_pnl": _finite_number(result.avg_trade_pnl, "avg_trade_pnl"),
            "cagr": _finite_number(result.cagr, "cagr"),
            "calmar_close": _finite_number(result.calmar_close, "calmar_close"),
            "calmar_worst": _finite_number(result.calmar_worst, "calmar_worst"),
            "end_date": _text(result.end_date, "end_date"),
            "engine_scope": _text(result.engine_scope, "engine_scope"),
            "expected_calendar_len": _nonnegative_int(result.expected_calendar_len, "expected_calendar_len"),
            "final_nav": _finite_number(result.final_nav, "final_nav"),
            "lots": _nonnegative_int(result.lots, "lots"),
            "max_drawdown_close": _finite_number(result.max_drawdown_close, "max_drawdown_close"),
            "max_drawdown_worst": _finite_number(result.max_drawdown_worst, "max_drawdown_worst"),
            "multiplier": _nonnegative_int(result.multiplier, "multiplier"),
            "product": _text(result.product, "product"),
            "schema": FUTURES_RESULT_SCHEMA,
            "schema_version": FUTURES_RESULT_SCHEMA_VERSION,
            "sharpe": _finite_number(result.sharpe, "sharpe", optional=True),
            "start_date": _text(result.start_date, "start_date"),
            "total_return": _finite_number(result.total_return, "total_return"),
            "trade_count": _nonnegative_int(result.trade_count, "trade_count"),
            "win_rate": _finite_number(result.win_rate, "win_rate", optional=True),
        }

    def build_payloads(self, outcome: RunOutcome) -> Tuple[ArtifactPayload, ...]:
        if outcome.engine_kind is not self.engine_kind:
            raise TypeError("outcome engine_kind does not match adapter")
        result = outcome.result
        if type(result) is not FuturesResult:
            raise TypeError("futures adapter requires exact FuturesResult")
        rebuilt = build_futures_outcome(result)
        if rebuilt.result_contract != outcome.result_contract:
            raise ValueError("futures outcome contract does not match evidence")
        return (
            _json_payload("result", "result.json", self._snapshot(result)),
            ArtifactPayload(
                role="futures_daily_nav",
                path="tables/futures_daily_nav.json",
                media_type=_JSON_MEDIA_TYPE,
                data=dataframe_to_bytes(result.daily_nav),
            ),
            ArtifactPayload(
                role="futures_trades",
                path="tables/futures_trades.json",
                media_type=_JSON_MEDIA_TYPE,
                data=dataframe_to_bytes(result.trades),
            ),
            ArtifactPayload(
                role="futures_event_journal",
                path="evidence/futures_event_journal.json",
                media_type=_JSON_MEDIA_TYPE,
                data=result.event_journal_json.encode("utf-8"),
            ),
        )

    def load_result(
        self, outcome: RunOutcome, payloads: Mapping[str, bytes]
    ) -> FuturesResult:
        required = {
            "result", "futures_daily_nav", "futures_trades",
            "futures_event_journal",
        }
        missing = required - set(payloads)
        if missing:
            raise ValueError("futures artifact is missing roles: {}".format(
                sorted(missing)
            ))
        value = _exact_keys(
            load_json_bytes(payloads["result"], "futures result"),
            self._SNAPSHOT_KEYS,
            "futures result",
        )
        if (
            value["schema"] != FUTURES_RESULT_SCHEMA
            or value["schema_version"] != FUTURES_RESULT_SCHEMA_VERSION
        ):
            raise ValueError("unsupported futures result snapshot schema")
        embedded_contract = outcome.result_contract
        source_by_key = {
            item.key: item.value for item in embedded_contract.assumptions
        }
        if source_by_key.get("orchestration.contract_source") == (
            "validated_futures_daily_scope_v1"
        ):
            embedded_contract = None
        result = FuturesResult(
            product=_text(value["product"], "product"),
            start_date=_text(value["start_date"], "start_date"),
            end_date=_text(value["end_date"], "end_date"),
            account_size=_finite_number(value["account_size"], "account_size"),
            lots=_nonnegative_int(value["lots"], "lots"),
            multiplier=_nonnegative_int(value["multiplier"], "multiplier"),
            engine_scope=_text(value["engine_scope"], "engine_scope"),
            assumptions=value["assumptions"],
            final_nav=_finite_number(value["final_nav"], "final_nav"),
            total_return=_finite_number(value["total_return"], "total_return"),
            cagr=_finite_number(value["cagr"], "cagr"),
            sharpe=_finite_number(value["sharpe"], "sharpe", optional=True),
            max_drawdown_close=_finite_number(value["max_drawdown_close"], "max_drawdown_close"),
            max_drawdown_worst=_finite_number(value["max_drawdown_worst"], "max_drawdown_worst"),
            calmar_close=_finite_number(value["calmar_close"], "calmar_close"),
            calmar_worst=_finite_number(value["calmar_worst"], "calmar_worst"),
            trade_count=_nonnegative_int(value["trade_count"], "trade_count"),
            win_rate=_finite_number(value["win_rate"], "win_rate", optional=True),
            avg_trade_pnl=_finite_number(value["avg_trade_pnl"], "avg_trade_pnl"),
            daily_nav=dataframe_from_bytes(payloads["futures_daily_nav"]),
            trades=dataframe_from_bytes(payloads["futures_trades"]),
            event_journal_json=payloads["futures_event_journal"].decode("utf-8"),
            expected_calendar_len=_nonnegative_int(value["expected_calendar_len"], "expected_calendar_len"),
            result_contract=embedded_contract,
        )
        rebuilt = build_futures_outcome(result)
        if rebuilt.result_contract != outcome.result_contract:
            raise ValueError("futures outcome contract does not match evidence")
        if result.validation_state is not ValidationState.VALID:
            raise ValueError("restored futures result did not validate")
        if self._snapshot(result) != value:
            raise ValueError("futures snapshot contains inconsistent fields")
        return result


_ADAPTERS = {
    EngineKind.CASH_SINGLE: CashSingleAdapter(),
    EngineKind.CASH_PORTFOLIO: CashPortfolioAdapter(),
    EngineKind.CASH_PARALLEL: ParallelAdapter(),
    EngineKind.INDEX_FUTURES: FuturesAdapter(),
}
RESERVED_ADAPTERS = {}


def adapter_for_kind(engine_kind: EngineKind) -> Any:
    if type(engine_kind) is not EngineKind:
        raise TypeError("engine_kind must be exactly EngineKind")
    adapter = _ADAPTERS.get(engine_kind)
    if adapter is not None:
        return adapter
    detail = RESERVED_ADAPTERS.get(engine_kind, "unsupported engine kind")
    raise NotImplementedError("{}: {}".format(engine_kind.value, detail))


__all__ = [
    "ArtifactPayload",
    "CASH_PORTFOLIO_RESULT_SCHEMA",
    "CASH_SINGLE_RESULT_SCHEMA",
    "FUTURES_RESULT_SCHEMA",
    "PARALLEL_RESULT_SCHEMA",
    "CashPortfolioAdapter",
    "CashSingleAdapter",
    "FuturesAdapter",
    "ParallelAdapter",
    "RESERVED_ADAPTERS",
    "adapter_for_kind",
    "build_futures_outcome",
    "build_parallel_outcome",
    "dataframe_from_bytes",
    "dataframe_to_bytes",
    "record_list_from_bytes",
    "record_list_to_bytes",
]
