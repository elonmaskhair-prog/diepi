"""Immutable signal inputs for causal replay.

The execution boundary is :class:`SignalReplayInput`, not a filesystem path.
CSV is the built-in adapter today; future adapters can construct the same
normalized object without teaching the replay strategy about file formats.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import csv
import hashlib
from io import BytesIO, StringIO
import math
import os
from pathlib import Path
import re
import stat
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from ..data.cache_manager import normalize_data_symbol


SIGNAL_INPUT_SCHEMA = "diepi.signal_replay_input"
SIGNAL_INPUT_SCHEMA_VERSION = 1
SIGNAL_CSV_MAX_BYTES = 128 * 1024 * 1024
# CSV numbers pass through pandas and the execution API.  Staying inside the
# exact-integer range shared by IEEE-754 avoids silently folding two distinct
# share counts into the same floating-point value in downstream consumers.
SIGNAL_MAX_SAFE_SHARES = 9_007_199_254_740_991
_PORTABLE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FORMATS = frozenset({"auto", "target", "action"})


def _portable_source_name(value: Any) -> str:
    name = Path(str(value)).name
    return name if _PORTABLE_NAME_RE.fullmatch(name) else "signals.csv"


def _is_link_or_reparse(info) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )


def _snapshot(info) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
    )


def _same_object(left, right) -> bool:
    if left.st_ino and right.st_ino:
        return left.st_dev == right.st_dev and left.st_ino == right.st_ino
    return True


def _read_stable_plain_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Read one plain file once, with bounded and stable handle semantics."""

    requested = path.expanduser().absolute()
    try:
        path_before = requested.lstat()
    except OSError as exc:
        raise FileNotFoundError(f"信号文件不存在: {requested}") from exc
    if _is_link_or_reparse(path_before) or not stat.S_ISREG(
        path_before.st_mode
    ):
        raise ValueError("信号文件必须是无链接/重解析点的普通文件")
    if path_before.st_size > max_bytes:
        raise ValueError(
            f"信号文件超过 {max_bytes} 字节安全上限"
        )

    with requested.open("rb") as handle:
        handle_before = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(handle_before.st_mode)
            or not _same_object(path_before, handle_before)
            or path_before.st_size != handle_before.st_size
        ):
            raise OSError("信号文件在读取前发生身份变化")
        payload = handle.read(max_bytes + 1)
        handle_after = os.fstat(handle.fileno())

    path_after = requested.lstat()
    if len(payload) > max_bytes:
        raise ValueError(
            f"信号文件超过 {max_bytes} 字节安全上限"
        )
    if (
        _is_link_or_reparse(path_after)
        or not stat.S_ISREG(path_after.st_mode)
        or _snapshot(path_before) != _snapshot(path_after)
        or _snapshot(handle_before) != _snapshot(handle_after)
        or not _same_object(handle_after, path_after)
        or len(payload) != handle_after.st_size
    ):
        raise OSError("信号文件在读取期间发生变化")
    return payload


def _validate_csv_shape(payload: bytes, label: str) -> None:
    """Reject CSV forms for which pandas would silently change semantics."""

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 CSV") from exc
    if "\x00" in text:
        raise ValueError(f"{label} must not contain NUL bytes")
    try:
        reader = csv.reader(StringIO(text), strict=True)
        header = next(reader)
        if not header or any(
            not name or name != name.strip() for name in header
        ):
            raise ValueError(
                f"{label} header must be the first non-empty record and use "
                "non-empty unpadded names"
            )
        if len(header) != len(set(header)):
            raise ValueError(f"{label} contains duplicate header names")
        width = len(header)
        for row_number, row in enumerate(reader, start=2):
            if not row or all(not value for value in row):
                continue
            if len(row) != width:
                raise ValueError(
                    f"{label} row {row_number} has {len(row)} fields; "
                    f"expected exactly {width}"
                )
    except StopIteration as exc:
        raise ValueError(f"{label} cannot be empty") from exc
    except csv.Error as exc:
        raise ValueError(f"{label} is not valid CSV: {exc}") from exc


def _date_key(value: Any, label: str) -> str:
    text = str(value).strip().replace("-", "")
    if text.endswith(".0"):
        text = text[:-2]
    if not re.fullmatch(r"\d{8}", text):
        raise ValueError(f"{label} 必须是 YYYYMMDD: {value!r}")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{label} 不是有效日期: {value!r}") from exc
    return text


def _symbol(value: Any, label: str) -> str:
    if pd.isna(value):
        raise ValueError(f"{label} 不能为空")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{label} 不能为空")
    try:
        return normalize_data_symbol(text)
    except ValueError as exc:
        raise ValueError(f"{label} 不是安全的证券标识") from exc


def _optional_number(row, key: str, *, row_number: int) -> Optional[float]:
    value = row.get(key)
    if value is None or pd.isna(value) or str(value).strip() == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"signals row {row_number} {key} 必须是数值"
        ) from exc
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(
            f"signals row {row_number} {key} 必须是有限正数"
        )
    return number


def _optional_shares(row, *, row_number: int) -> Optional[int]:
    value = row.get("shares")
    if value is None or pd.isna(value) or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            f"signals row {row_number} shares 必须是整数"
        ) from exc
    if not number.is_finite() or number != number.to_integral_value():
        raise ValueError(f"signals row {row_number} shares 必须是整数")
    if number < 1 or number > SIGNAL_MAX_SAFE_SHARES:
        raise ValueError(
            f"signals row {row_number} shares 必须在 "
            f"[1,{SIGNAL_MAX_SAFE_SHARES}]"
        )
    shares = int(number)
    return shares


@dataclass(frozen=True)
class SignalInstruction:
    """One normalized, path-independent replay instruction."""

    date: str
    symbol: str
    target_weight: Optional[float] = None
    action: Optional[str] = None
    percent: Optional[float] = None
    shares: Optional[int] = None
    amount: Optional[float] = None

    def __post_init__(self) -> None:
        if type(self.date) is not str or not re.fullmatch(r"\d{8}", self.date):
            raise ValueError("instruction date must be canonical YYYYMMDD")
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("instruction symbol must be non-empty")
        if normalize_data_symbol(self.symbol) != self.symbol:
            raise ValueError(
                "instruction symbol must use canonical path-safe uppercase syntax"
            )
        if self.target_weight is not None:
            if self.action is not None or any(
                value is not None
                for value in (self.percent, self.shares, self.amount)
            ):
                raise ValueError("target instruction cannot contain action quantity")
            if (
                type(self.target_weight) is not float
                or not math.isfinite(self.target_weight)
                or not 0.0 <= self.target_weight <= 1.0
            ):
                raise ValueError("target_weight must be a finite float in [0,1]")
            return
        if self.action not in {"buy", "sell"}:
            raise ValueError("action instruction must use buy/sell")
        if self.percent is not None and (
            type(self.percent) is not float
            or not math.isfinite(self.percent)
            or not 0.0 < self.percent <= 1.0
        ):
            raise ValueError("percent must be a finite float in (0,1]")
        if self.shares is not None and (
            type(self.shares) is not int
            or not 1 <= self.shares <= SIGNAL_MAX_SAFE_SHARES
        ):
            raise ValueError(
                f"shares must be an integer in [1,{SIGNAL_MAX_SAFE_SHARES}]"
            )
        if self.amount is not None and (
            type(self.amount) is not float
            or not math.isfinite(self.amount)
            or self.amount <= 0.0
        ):
            raise ValueError("amount must be a finite positive float")
        present = sum(
            value is not None for value in (self.percent, self.shares, self.amount)
        )
        if present > 1:
            raise ValueError("action instruction accepts at most one quantity")
        if self.action == "buy" and present != 1:
            raise ValueError("buy instruction requires one quantity")
        if self.action == "sell" and self.amount is not None:
            raise ValueError("sell instruction does not support amount")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "amount": self.amount,
            "date": self.date,
            "percent": self.percent,
            "shares": self.shares,
            "symbol": self.symbol,
            "target_weight": self.target_weight,
        }


@dataclass(frozen=True)
class SignalReplayInput:
    """Frozen bytes plus normalized instructions used by replay and Artifact."""

    source_name: str
    source_bytes: bytes = field(repr=False)
    signal_format: str
    instructions: Tuple[SignalInstruction, ...]
    warnings: Tuple[str, ...] = ()
    symbols: Tuple[str, ...] = field(init=False)
    dates: Tuple[str, ...] = field(init=False)
    _by_date: Tuple[Tuple[str, Tuple[SignalInstruction, ...]], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if (
            type(self.source_name) is not str
            or not _PORTABLE_NAME_RE.fullmatch(self.source_name)
        ):
            raise ValueError("source_name must be a portable basename")
        if type(self.source_bytes) is not bytes or not self.source_bytes:
            raise TypeError("source_bytes must be non-empty bytes")
        if len(self.source_bytes) > SIGNAL_CSV_MAX_BYTES:
            raise ValueError("source_bytes exceeds the signal CSV safety limit")
        if self.signal_format not in {"target", "action"}:
            raise ValueError("signal_format must be resolved target/action")
        if type(self.instructions) is not tuple or not self.instructions:
            raise ValueError("instructions must be a non-empty tuple")
        if any(type(item) is not SignalInstruction for item in self.instructions):
            raise TypeError("instructions must contain SignalInstruction values")
        if type(self.warnings) is not tuple or any(
            type(item) is not str for item in self.warnings
        ):
            raise TypeError("warnings must be a tuple of strings")
        if self.signal_format == "target" and any(
            item.target_weight is None for item in self.instructions
        ):
            raise ValueError("target input contains an action instruction")
        if self.signal_format == "action" and any(
            item.target_weight is not None for item in self.instructions
        ):
            raise ValueError("action input contains a target instruction")

        symbols = tuple(sorted({item.symbol for item in self.instructions}))
        dates = tuple(sorted({item.date for item in self.instructions}))
        grouped = []
        for date in dates:
            grouped.append((date, tuple(
                item for item in self.instructions if item.date == date
            )))
        object.__setattr__(self, "symbols", symbols)
        object.__setattr__(self, "dates", dates)
        object.__setattr__(self, "_by_date", tuple(grouped))

    @classmethod
    def from_bytes(
        cls,
        payload: bytes,
        *,
        signal_format: str = "auto",
        source_name: str = "signals.csv",
    ) -> "SignalReplayInput":
        """Parse and validate the built-in target/action CSV adapter once."""

        if type(payload) is not bytes:
            raise TypeError("signal payload must be exactly bytes")
        if not payload:
            raise ValueError("信号 CSV 不能为空")
        if len(payload) > SIGNAL_CSV_MAX_BYTES:
            raise ValueError(
                f"信号 CSV 超过 {SIGNAL_CSV_MAX_BYTES} 字节安全上限"
            )
        if signal_format not in _FORMATS:
            raise ValueError("signal_format 必须是 auto/target/action")
        _validate_csv_shape(payload, "signal CSV")
        try:
            frame = pd.read_csv(
                BytesIO(payload),
                encoding="utf-8-sig",
                dtype="string",
                keep_default_na=False,
            )
        except (UnicodeDecodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as exc:
            raise ValueError(f"信号输入必须是有效 UTF-8 CSV: {exc}") from exc
        columns = set(frame.columns)
        if not {"date", "symbol"} <= columns:
            raise ValueError(
                "信号清单缺必需列 date/symbol，实际列: "
                f"{sorted(columns)}"
            )
        if frame.empty:
            raise ValueError("信号清单不能为空")

        resolved = signal_format
        if resolved == "auto":
            has_target = "target_weight" in columns
            has_action = "action" in columns
            if has_target and has_action:
                raise ValueError(
                    "signals-format auto is ambiguous when both "
                    "target_weight and action columns are present; choose "
                    "target or action explicitly"
                )
            if has_target:
                resolved = "target"
            elif has_action:
                resolved = "action"
            else:
                raise ValueError(
                    "无法识别清单格式：目标权重型需 target_weight 列，"
                    "动作型需 action 列"
                )
        required = "target_weight" if resolved == "target" else "action"
        if required not in columns:
            raise ValueError(f"{resolved} 信号清单缺少 {required} 列")

        def column_has_values(name: str) -> bool:
            if name not in frame.columns:
                return False
            values = frame[name]
            return bool(
                values.notna().any()
                and values.fillna("").astype(str).str.strip().ne("").any()
            )

        if resolved == "target":
            conflicting = [
                name
                for name in ("action", "percent", "shares", "amount")
                if column_has_values(name)
            ]
        else:
            conflicting = (
                ["target_weight"] if column_has_values("target_weight") else []
            )
        if conflicting:
            raise ValueError(
                f"{resolved} signals contain conflicting semantic columns: "
                + ",".join(conflicting)
            )

        normalized = []
        warnings = []
        if resolved == "target":
            last_by_key = {}
            duplicate_count = 0
            for offset, row in enumerate(frame.to_dict("records"), start=2):
                date = _date_key(row.get("date"), f"signals row {offset} date")
                symbol = _symbol(
                    row.get("symbol"), f"signals row {offset} symbol"
                )
                value = row.get("target_weight")
                if value is None or pd.isna(value) or str(value).strip() == "":
                    raise ValueError(
                        f"signals row {offset} target_weight 是空值"
                    )
                try:
                    weight = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"signals row {offset} target_weight 非数值"
                        f"（{date} {symbol}）"
                    ) from exc
                if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
                    raise ValueError(
                        f"signals row {offset} target_weight 必须在 [0,1]"
                    )
                key = (date, symbol)
                if key in last_by_key:
                    duplicate_count += 1
                last_by_key[key] = (offset, SignalInstruction(
                    date=date, symbol=symbol, target_weight=weight
                ))
            normalized = [
                item for _, item in sorted(
                    last_by_key.values(), key=lambda value: value[0]
                )
            ]
            if duplicate_count:
                warnings.append(
                    "清单同日同标的重复 "
                    f"{duplicate_count} 行，按声明式语义取末行生效"
                )
            day_sums = {}
            for item in normalized:
                day_sums[item.date] = (
                    day_sums.get(item.date, 0.0) + item.target_weight
                )
            over = [
                (date, value)
                for date, value in sorted(day_sums.items())
                if value > 1.0 + 1e-9
            ]
            if over:
                raise ValueError(
                    "清单每日目标权重之和 > 1"
                    f"（首例 {over[0][0]}: {over[0][1]:.4f}）"
                )
        else:
            for offset, row in enumerate(frame.to_dict("records"), start=2):
                date = _date_key(row.get("date"), f"signals row {offset} date")
                symbol = _symbol(
                    row.get("symbol"), f"signals row {offset} symbol"
                )
                action = str(row.get("action", "")).strip().lower()
                if action not in {"buy", "sell"}:
                    raise ValueError(
                        f"signals row {offset} 未知 action {action!r}，"
                        "必须是 buy/sell"
                    )
                percent = _optional_number(row, "percent", row_number=offset)
                shares = _optional_shares(row, row_number=offset)
                amount = _optional_number(row, "amount", row_number=offset)
                normalized.append(SignalInstruction(
                    date=date,
                    symbol=symbol,
                    action=action,
                    percent=percent,
                    shares=shares,
                    amount=amount,
                ))

        return cls(
            source_name=_portable_source_name(source_name),
            source_bytes=payload,
            signal_format=resolved,
            instructions=tuple(normalized),
            warnings=tuple(warnings),
        )

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.source_bytes).hexdigest()

    def rows_for(self, date: Any) -> Tuple[SignalInstruction, ...]:
        key = _date_key(date, "signal lookup date")
        for candidate, rows in self._by_date:
            if candidate == key:
                return rows
        return ()

    def artifact_inputs(self) -> Tuple[Tuple[str, str, bytes], ...]:
        return (("signals_source", "inputs/signals.csv", self.source_bytes),)

    def manifest_dict(self) -> Dict[str, Any]:
        return {
            "adapter": "csv_target_action_v1",
            "format": self.signal_format,
            "rows": len(self.instructions),
            "schema": SIGNAL_INPUT_SCHEMA,
            "schema_version": SIGNAL_INPUT_SCHEMA_VERSION,
            "scope": {
                "dates": list(self.dates),
                "symbols": list(self.symbols),
            },
            "sha256": self.sha256,
            "source_name": self.source_name,
        }

    def revalidated(self) -> "SignalReplayInput":
        """Reparse the frozen bytes and reject a mismatched execution view.

        ``SignalReplayInput`` is public so future adapters can target the same
        normalized boundary.  Public construction, however, must not let a
        caller attach instructions that differ from the bytes persisted in a
        RunArtifact.  The runner calls this method immediately before it
        accepts the input and then executes the returned canonical instance.
        """

        canonical = SignalReplayInput.from_bytes(
            self.source_bytes,
            signal_format=self.signal_format,
            source_name=self.source_name,
        )
        if canonical != self:
            raise ValueError(
                "signal instructions do not match the frozen source bytes"
            )
        return canonical


def load_signal_replay_input(
    path: Any, *, signal_format: str = "auto"
) -> SignalReplayInput:
    """Load one plain CSV path into the immutable execution boundary."""

    source = Path(path).expanduser().absolute()
    payload = _read_stable_plain_bytes(
        source, max_bytes=SIGNAL_CSV_MAX_BYTES
    )
    return SignalReplayInput.from_bytes(
        payload,
        signal_format=signal_format,
        source_name=source.name,
    )


__all__ = [
    "SIGNAL_CSV_MAX_BYTES",
    "SIGNAL_INPUT_SCHEMA",
    "SIGNAL_INPUT_SCHEMA_VERSION",
    "SIGNAL_MAX_SAFE_SHARES",
    "SignalInstruction",
    "SignalReplayInput",
    "load_signal_replay_input",
]
