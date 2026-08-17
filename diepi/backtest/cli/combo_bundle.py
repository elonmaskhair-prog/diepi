"""Validated frozen-signal bundles for causal portfolio replay.

The bundle contract separates three facts that a single ``--signals`` table
cannot express safely:

* daily target weights submitted before the opening auction;
* pre-computed same-day close exits submitted after the open and before close;
* the complete set of dates on which an empty target means an empty portfolio.

Input files remain user-owned local data.  A run artifact snapshots their exact
bytes under canonical names so a later load does not depend on the original
absolute path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import csv
import hashlib
from io import BytesIO, StringIO
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

import pandas as pd


COMBO_BUNDLE_SCHEMA = "diepi.combo_replay_bundle"
COMBO_BUNDLE_SCHEMA_VERSION = 1
COMBO_MANIFEST_MAX_BYTES = 1 * 1024 * 1024
COMBO_CSV_MAX_BYTES = 128 * 1024 * 1024
_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SYMBOL_RE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
_CANONICAL_FILES = {
    "targets": "targets.csv",
    "close_sells": "close_sells.csv",
    "daily": "daily.csv",
}


def _is_link_or_reparse(path: Path) -> bool:
    info = os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _plain_directory(path: Path, label: str) -> Path:
    value = path.expanduser().absolute()
    if not value.is_dir():
        raise FileNotFoundError(f"{label}不存在或不是目录: {value}")
    if _is_link_or_reparse(value):
        raise ValueError(f"{label}不能是链接或重解析点: {value}")
    return value.resolve(strict=True)


def _plain_member(
    root: Path,
    relative: str,
    label: str,
    *,
    max_bytes: int,
) -> Path:
    if type(relative) is not str or not relative or "\\" in relative:
        raise ValueError(f"{label}必须是 bundle 内的相对 POSIX 路径")
    rel = Path(relative)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise ValueError(f"{label}不能逃逸 bundle: {relative!r}")
    candidate = root.joinpath(*rel.parts)
    info = os.lstat(candidate)
    if (
        _is_link_or_reparse(candidate)
        or not stat.S_ISREG(info.st_mode)
    ):
        raise ValueError(f"{label}必须是普通文件: {candidate}")
    if info.st_size > max_bytes:
        raise ValueError(
            f"{label} exceeds the {max_bytes}-byte safety limit"
        )
    resolved = candidate.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label}不能逃逸 bundle: {relative!r}") from exc
    return resolved


def _read_bounded_bytes(path: Path, label: str, *, max_bytes: int) -> bytes:
    """Read a plain file while enforcing both metadata and actual-byte caps."""

    info = os.lstat(path)
    if _is_link_or_reparse(path) or not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label}必须是普通文件: {path}")
    if info.st_size > max_bytes:
        raise ValueError(
            f"{label} exceeds the {max_bytes}-byte safety limit"
        )
    with path.open("rb") as handle:
        opened = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
            or opened.st_size > max_bytes
        ):
            raise ValueError(f"{label} changed or exceeds its safety limit")
        payload = handle.read(max_bytes + 1)
        opened_after = os.fstat(handle.fileno())
    if len(payload) > max_bytes:
        raise ValueError(
            f"{label} exceeds the {max_bytes}-byte safety limit"
        )
    after = os.lstat(path)
    if (
        _is_link_or_reparse(path)
        or not stat.S_ISREG(after.st_mode)
        or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)
        or after.st_size != info.st_size
        or after.st_mtime_ns != info.st_mtime_ns
        or opened_after.st_size != opened.st_size
        or opened_after.st_mtime_ns != opened.st_mtime_ns
    ):
        raise ValueError(f"{label} changed while it was being read")
    return payload


def _date_key(value: Any, label: str) -> str:
    text = str(value).strip().replace("-", "")
    if not re.fullmatch(r"\d{8}", text):
        raise ValueError(f"{label}必须是 YYYYMMDD: {value!r}")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{label}不是有效日期: {value!r}") from exc
    return text


def _symbol(value: Any, label: str) -> str:
    text = str(value).strip()
    if text != str(value) or not _SYMBOL_RE.fullmatch(text):
        raise ValueError(f"{label}必须是规范的六位证券代码，例如 000001.SZ")
    return text


def _read_csv(payload: bytes, label: str) -> pd.DataFrame:
    if type(payload) is not bytes or not payload:
        raise ValueError(f"{label}不能为空")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label}必须是有效 UTF-8 CSV") from exc
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
        raise ValueError(f"{label}不能为空") from exc
    except csv.Error as exc:
        raise ValueError(f"{label}必须是有效 CSV: {exc}") from exc
    try:
        return pd.read_csv(
            BytesIO(payload),
            encoding="utf-8-sig",
            dtype="string",
            keep_default_na=False,
        )
    except pd.errors.ParserError as exc:
        raise ValueError(f"{label}必须是有效 UTF-8 CSV: {exc}") from exc


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True)
class ComboReplayBundle:
    """Immutable, fully validated replay input."""

    root: Path
    tag: str
    targets_path: Path
    close_sells_path: Path
    daily_path: Path
    targets_bytes: bytes = field(repr=False)
    close_sells_bytes: bytes = field(repr=False)
    daily_bytes: bytes = field(repr=False)
    known_dates: frozenset[str]
    symbols: Tuple[str, ...]
    start_date: str
    end_date: str
    target_rows: int
    close_sell_rows: int
    daily_rows: int
    _targets_by_date: Mapping[str, Tuple[Tuple[str, float], ...]] = field(
        repr=False, compare=False
    )
    _close_sells_by_date: Mapping[str, Tuple[str, ...]] = field(
        repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise TypeError("root must be an absolute pathlib.Path")
        if type(self.tag) is not str or not _TAG_RE.fullmatch(self.tag):
            raise ValueError("combo tag must be a portable identifier")
        if type(self.symbols) is not tuple or not self.symbols:
            raise ValueError("combo symbols must be a non-empty tuple")
        if self.symbols != tuple(sorted(set(self.symbols))):
            raise ValueError("combo symbols must be unique and sorted")
        if type(self.known_dates) is not frozenset or not self.known_dates:
            raise ValueError("combo known_dates must be a non-empty frozenset")
        if (self.start_date, self.end_date) != (
            min(self.known_dates),
            max(self.known_dates),
        ):
            raise ValueError("combo date scope does not match daily coverage")
        for payload in (self.targets_bytes, self.close_sells_bytes, self.daily_bytes):
            if type(payload) is not bytes:
                raise TypeError("combo source payloads must be bytes")
        object.__setattr__(
            self,
            "_targets_by_date",
            MappingProxyType(dict(self._targets_by_date)),
        )
        object.__setattr__(
            self,
            "_close_sells_by_date",
            MappingProxyType(dict(self._close_sells_by_date)),
        )

    def targets_for(self, date: Any) -> Mapping[str, float]:
        key = _date_key(date, "target lookup date")
        return MappingProxyType(dict(self._targets_by_date.get(key, ())))

    def close_sells_for(self, date: Any) -> Tuple[str, ...]:
        key = _date_key(date, "close-sell lookup date")
        return self._close_sells_by_date.get(key, ())

    def validate_requested_scope(self, start_date: Any, end_date: Any) -> None:
        start = _date_key(start_date, "回测开始日")
        end = _date_key(end_date, "回测结束日")
        if start > end:
            raise ValueError("回测开始日不能晚于结束日")
        if start < self.start_date or end > self.end_date:
            raise ValueError(
                "回测区间必须落在 combo daily 覆盖内: "
                f"{self.start_date}..{self.end_date}"
            )

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "schema": COMBO_BUNDLE_SCHEMA,
            "schema_version": COMBO_BUNDLE_SCHEMA_VERSION,
            "tag": self.tag,
            "files": dict(_CANONICAL_FILES),
            "scope": {
                "start_date": self.start_date,
                "end_date": self.end_date,
                "symbols": list(self.symbols),
            },
            "rows": {
                "targets": self.target_rows,
                "close_sells": self.close_sell_rows,
                "daily": self.daily_rows,
            },
            "sha256": {
                "targets": _sha256(self.targets_bytes),
                "close_sells": _sha256(self.close_sells_bytes),
                "daily": _sha256(self.daily_bytes),
            },
            "semantics": {
                "daily_empty_target": "explicit_empty_portfolio",
                "targets": "before_open_weight_delta_v1",
                "close_sells": "on_after_open_schedule_same_day_close_v1",
                "missing_daily_date": "error_on_engine_trade_day",
            },
        }

    def manifest_bytes(self) -> bytes:
        return _canonical_json_bytes(self.manifest_dict())

    def artifact_inputs(self) -> Tuple[Tuple[str, str, bytes], ...]:
        """Return role/path/bytes using stable, path-private artifact names."""

        return (
            ("combo_manifest_source", "inputs/combo/diepi_combo.json", self.manifest_bytes()),
            ("combo_targets_source", "inputs/combo/targets.csv", self.targets_bytes),
            (
                "combo_close_sells_source",
                "inputs/combo/close_sells.csv",
                self.close_sells_bytes,
            ),
            ("combo_daily_source", "inputs/combo/daily.csv", self.daily_bytes),
        )

    def revalidated(self) -> "ComboReplayBundle":
        """Reparse frozen CSV bytes and reject a divergent execution view."""

        derived = _derive_bundle_execution(
            {
                "targets": self.targets_bytes,
                "close_sells": self.close_sells_bytes,
                "daily": self.daily_bytes,
            }
        )
        canonical = ComboReplayBundle(
            root=self.root,
            tag=self.tag,
            targets_path=self.targets_path,
            close_sells_path=self.close_sells_path,
            daily_path=self.daily_path,
            targets_bytes=self.targets_bytes,
            close_sells_bytes=self.close_sells_bytes,
            daily_bytes=self.daily_bytes,
            known_dates=derived["known_dates"],
            symbols=derived["symbols"],
            start_date=derived["start_date"],
            end_date=derived["end_date"],
            target_rows=derived["target_rows"],
            close_sell_rows=derived["close_sell_rows"],
            daily_rows=derived["daily_rows"],
            _targets_by_date=derived["targets_by_date"],
            _close_sells_by_date=derived["close_sells_by_date"],
        )
        if (
            canonical.manifest_bytes() != self.manifest_bytes()
            or _bundle_execution_bytes(canonical)
            != _bundle_execution_bytes(self)
        ):
            raise ValueError(
                "combo execution state does not match the frozen source bytes"
            )
        return canonical


def _bundle_execution_bytes(bundle: ComboReplayBundle) -> bytes:
    """Return a type-sensitive canonical view of replay instructions."""

    targets = []
    for date in sorted(bundle._targets_by_date):
        rows = bundle._targets_by_date[date]
        for symbol, weight in sorted(rows, key=lambda item: (item[0], repr(item[1]))):
            targets.append([date, symbol, weight])
    close_sells = []
    for date in sorted(bundle._close_sells_by_date):
        for symbol in sorted(bundle._close_sells_by_date[date]):
            close_sells.append([date, symbol])
    return _canonical_json_bytes(
        {"targets": targets, "close_sells": close_sells}
    )


def _derive_bundle_execution(payloads: Mapping[str, bytes]) -> dict[str, Any]:
    """Validate frozen combo CSV payloads and derive canonical replay state."""

    required_payloads = {"targets", "close_sells", "daily"}
    if set(payloads) != required_payloads:
        raise ValueError("combo payload set is incomplete")
    for name in sorted(required_payloads):
        payload = payloads[name]
        if type(payload) is not bytes:
            raise TypeError("combo source payloads must be bytes")
        if not payload:
            raise ValueError(f"combo {name} payload cannot be empty")
        if len(payload) > COMBO_CSV_MAX_BYTES:
            raise ValueError(f"combo {name} payload exceeds the safety limit")

    daily = _read_csv(payloads["daily"], "combo daily")
    if "date" not in daily.columns:
        raise ValueError("combo daily 缺少 date 列")
    daily_dates = [_date_key(value, "combo daily.date") for value in daily["date"]]
    if not daily_dates:
        raise ValueError("combo daily 不能为空")
    if len(daily_dates) != len(set(daily_dates)):
        raise ValueError("combo daily.date 不能重复")
    if daily_dates != sorted(daily_dates):
        raise ValueError("combo daily.date 必须严格递增")
    required_daily = {"invested_weight", "cash_weight"}
    if not required_daily <= set(daily.columns):
        raise ValueError(
            "combo daily 必须包含 date,invested_weight,cash_weight"
        )
    invested = pd.to_numeric(daily["invested_weight"], errors="coerce")
    cash = pd.to_numeric(daily["cash_weight"], errors="coerce")
    invalid = (~invested.between(0.0, 1.0)) | (~cash.between(0.0, 1.0))
    invalid |= (invested + cash - 1.0).abs() > 1e-9
    if invalid.any():
        raise ValueError(
            "combo daily 的 invested_weight/cash_weight 必须有限且和为 1"
        )
    known_dates = frozenset(daily_dates)

    targets = _read_csv(payloads["targets"], "combo targets")
    required_targets = {"trade_date", "symbol", "target_weight"}
    if not required_targets <= set(targets.columns):
        raise ValueError(
            "combo targets 缺列: "
            + ",".join(sorted(required_targets - set(targets.columns)))
        )
    if targets.empty:
        raise ValueError("combo targets 不能为空")
    target_rows = []
    seen_targets = set()
    for index, row in targets.iterrows():
        date = _date_key(
            row["trade_date"], f"targets row {index + 2} trade_date"
        )
        symbol = _symbol(row["symbol"], f"targets row {index + 2} symbol")
        try:
            weight = float(row["target_weight"])
        except (TypeError, ValueError):
            raise ValueError(
                f"targets row {index + 2} target_weight 必须是数值"
            ) from None
        if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
            raise ValueError(
                f"targets row {index + 2} target_weight 必须在 [0,1]"
            )
        key = (date, symbol)
        if key in seen_targets:
            raise ValueError(f"combo targets 同日同标的重复: {date} {symbol}")
        if date not in known_dates:
            raise ValueError(f"combo targets 日期不在 daily 覆盖内: {date}")
        seen_targets.add(key)
        target_rows.append((date, symbol, weight))

    day_sums: dict[str, float] = {}
    targets_by_date: dict[str, list[Tuple[str, float]]] = {}
    for date, symbol, weight in target_rows:
        day_sums[date] = day_sums.get(date, 0.0) + weight
        targets_by_date.setdefault(date, []).append((symbol, weight))
    over = [
        (date, value)
        for date, value in day_sums.items()
        if value > 1.0 + 1e-9
    ]
    if over:
        raise ValueError(f"combo targets 每日权重和不能超过 1: {over[0]}")
    daily_invested = dict(zip(daily_dates, invested.astype(float)))
    for date in daily_dates:
        if abs(daily_invested[date] - day_sums.get(date, 0.0)) > 1e-9:
            raise ValueError(
                "combo daily.invested_weight 与 targets 当日权重和不一致: "
                f"{date}"
            )
    canonical_targets = {
        date: tuple(sorted(rows)) for date, rows in targets_by_date.items()
    }

    close_sells = _read_csv(payloads["close_sells"], "combo close_sells")
    required_close = {"trade_date", "symbol"}
    if not required_close <= set(close_sells.columns):
        raise ValueError("combo close_sells 必须包含 trade_date,symbol")
    close_by_date: dict[str, list[str]] = {}
    seen_close = set()
    for index, row in close_sells.iterrows():
        date = _date_key(
            row["trade_date"], f"close_sells row {index + 2} trade_date"
        )
        symbol = _symbol(row["symbol"], f"close_sells row {index + 2} symbol")
        key = (date, symbol)
        if key in seen_close:
            raise ValueError(
                f"combo close_sells 同日同标的重复: {date} {symbol}"
            )
        if date not in known_dates:
            raise ValueError(f"combo close_sells 日期不在 daily 覆盖内: {date}")
        if "exit_price" in close_sells.columns:
            exit_price = row.get("exit_price")
            if pd.notna(exit_price) and str(exit_price).strip().lower() != "close":
                raise ValueError("combo close_sells.exit_price 只支持 close")
        seen_close.add(key)
        close_by_date.setdefault(date, []).append(symbol)
    canonical_close = {
        date: tuple(sorted(symbols)) for date, symbols in close_by_date.items()
    }
    symbols = tuple(
        sorted(
            {symbol for _, symbol, _ in target_rows}
            | {symbol for _, symbol in seen_close}
        )
    )
    return {
        "known_dates": known_dates,
        "symbols": symbols,
        "start_date": min(known_dates),
        "end_date": max(known_dates),
        "target_rows": len(target_rows),
        "close_sell_rows": len(seen_close),
        "daily_rows": len(daily_dates),
        "targets_by_date": canonical_targets,
        "close_sells_by_date": canonical_close,
    }


def _json_without_duplicate_keys(payload: str, label: str) -> Any:
    def build(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"{label}包含重复 JSON key: {key}")
            value[key] = item
        return value

    try:
        return json.loads(payload, object_pairs_hook=build)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}不是有效 UTF-8 JSON: {exc}") from exc


def _read_manifest(root: Path) -> Optional[Mapping[str, Any]]:
    manifest_path = root / "diepi_combo.json"
    if not os.path.lexists(manifest_path):
        return None
    manifest_path = _plain_member(
        root,
        "diepi_combo.json",
        "combo manifest",
        max_bytes=COMBO_MANIFEST_MAX_BYTES,
    )
    try:
        text = _read_bounded_bytes(
            manifest_path,
            "combo manifest",
            max_bytes=COMBO_MANIFEST_MAX_BYTES,
        ).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"combo manifest 不是有效 UTF-8 JSON: {exc}") from exc
    value = _json_without_duplicate_keys(text, "combo manifest")
    if type(value) is not dict:
        raise TypeError("combo manifest 必须是 JSON object")
    return value


def _manifest_layout(root: Path) -> Optional[Tuple[str, Mapping[str, str]]]:
    value = _read_manifest(root)
    if value is None:
        return None
    if value.get("schema") != COMBO_BUNDLE_SCHEMA:
        raise ValueError("不支持的 combo manifest schema")
    if value.get("schema_version") != COMBO_BUNDLE_SCHEMA_VERSION:
        raise ValueError("不支持的 combo manifest schema_version")
    tag = value.get("tag")
    files = value.get("files")
    if type(tag) is not str or not _TAG_RE.fullmatch(tag):
        raise ValueError("combo manifest tag 非法")
    if files != _CANONICAL_FILES:
        raise ValueError(
            "combo manifest files 必须使用规范 targets/close_sells/daily 文件名"
        )
    return tag, files


def _discover_layout(root: Path, tag: Optional[str]) -> Tuple[str, Mapping[str, str]]:
    manifested = _manifest_layout(root)
    if manifested is not None:
        manifest_tag, files = manifested
        if tag is not None and tag != manifest_tag:
            raise ValueError(
                f"--combo-tag={tag!r} 与 manifest tag={manifest_tag!r} 不一致"
            )
        return manifested

    canonical = {name: root / path for name, path in _CANONICAL_FILES.items()}
    if all(path.is_file() for path in canonical.values()):
        resolved_tag = tag or root.name
        if not _TAG_RE.fullmatch(resolved_tag):
            raise ValueError("规范 combo 目录名不能作为 tag；请显式提供 --combo-tag")
        return resolved_tag, _CANONICAL_FILES

    if tag is None:
        candidates = sorted(root.glob("new_combo_targets_*.csv"))
        if len(candidates) != 1:
            raise ValueError(
                "旧式 combo 目录必须用 --combo-tag 指定，或恰好只有一份 "
                "new_combo_targets_<tag>.csv"
            )
        name = candidates[0].name
        tag = name[len("new_combo_targets_") : -len(".csv")]
    if type(tag) is not str or not _TAG_RE.fullmatch(tag):
        raise ValueError("--combo-tag 必须是可移植标识符")
    return tag, {
        "targets": f"new_combo_targets_{tag}.csv",
        "close_sells": f"new_combo_close_sells_{tag}.csv",
        "daily": f"new_combo_daily_{tag}.csv",
    }


def load_combo_bundle(path: Any, *, tag: Optional[str] = None) -> ComboReplayBundle:
    """Load and strictly validate one local combo bundle directory."""

    root = _plain_directory(Path(path), "combo bundle")
    resolved_tag, layout = _discover_layout(root, tag)
    paths = {
        name: _plain_member(
            root,
            relative,
            f"combo {name}",
            max_bytes=COMBO_CSV_MAX_BYTES,
        )
        for name, relative in layout.items()
    }
    payloads = {
        name: _read_bounded_bytes(
            source,
            f"combo {name}",
            max_bytes=COMBO_CSV_MAX_BYTES,
        )
        for name, source in paths.items()
    }

    daily = _read_csv(payloads["daily"], "combo daily")
    if "date" not in daily.columns:
        raise ValueError("combo daily 缺少 date 列")
    daily_dates = [_date_key(value, "combo daily.date") for value in daily["date"]]
    if not daily_dates:
        raise ValueError("combo daily 不能为空")
    if len(daily_dates) != len(set(daily_dates)):
        raise ValueError("combo daily.date 不能重复")
    if daily_dates != sorted(daily_dates):
        raise ValueError("combo daily.date 必须严格递增")
    required_daily = {"invested_weight", "cash_weight"}
    if not required_daily <= set(daily.columns):
        raise ValueError(
            "combo daily 必须包含 date,invested_weight,cash_weight"
        )
    invested = pd.to_numeric(daily["invested_weight"], errors="coerce")
    cash = pd.to_numeric(daily["cash_weight"], errors="coerce")
    invalid = (~invested.between(0.0, 1.0)) | (~cash.between(0.0, 1.0))
    invalid |= (invested + cash - 1.0).abs() > 1e-9
    if invalid.any():
        raise ValueError("combo daily 的 invested_weight/cash_weight 必须有限且和为 1")
    known_dates = frozenset(daily_dates)

    targets = _read_csv(payloads["targets"], "combo targets")
    required_targets = {"trade_date", "symbol", "target_weight"}
    if not required_targets <= set(targets.columns):
        raise ValueError(
            "combo targets 缺列: "
            + ",".join(sorted(required_targets - set(targets.columns)))
        )
    if targets.empty:
        raise ValueError("combo targets 不能为空")
    target_rows = []
    seen_targets = set()
    for index, row in targets.iterrows():
        date = _date_key(row["trade_date"], f"targets row {index + 2} trade_date")
        symbol = _symbol(row["symbol"], f"targets row {index + 2} symbol")
        try:
            weight = float(row["target_weight"])
        except (TypeError, ValueError):
            raise ValueError(f"targets row {index + 2} target_weight 必须是数值") from None
        if not math.isfinite(weight) or not 0.0 <= weight <= 1.0:
            raise ValueError(f"targets row {index + 2} target_weight 必须在 [0,1]")
        key = (date, symbol)
        if key in seen_targets:
            raise ValueError(f"combo targets 同日同标的重复: {date} {symbol}")
        if date not in known_dates:
            raise ValueError(f"combo targets 日期不在 daily 覆盖中: {date}")
        seen_targets.add(key)
        target_rows.append((date, symbol, weight))
    day_sums: dict[str, float] = {}
    targets_by_date: dict[str, list[Tuple[str, float]]] = {}
    for date, symbol, weight in target_rows:
        day_sums[date] = day_sums.get(date, 0.0) + weight
        targets_by_date.setdefault(date, []).append((symbol, weight))
    over = [(date, value) for date, value in day_sums.items() if value > 1.0 + 1e-9]
    if over:
        raise ValueError(f"combo targets 每日权重和不能超过 1: {over[0]}")
    daily_invested = dict(zip(daily_dates, invested.astype(float)))
    for date in daily_dates:
        if abs(daily_invested[date] - day_sums.get(date, 0.0)) > 1e-9:
            raise ValueError(
                "combo daily.invested_weight 与 targets 当日权重和不一致: "
                f"{date}"
            )
    canonical_targets = {
        date: tuple(sorted(rows)) for date, rows in targets_by_date.items()
    }

    close_sells = _read_csv(payloads["close_sells"], "combo close_sells")
    required_close = {"trade_date", "symbol"}
    if not required_close <= set(close_sells.columns):
        raise ValueError("combo close_sells 必须包含 trade_date,symbol")
    close_by_date: dict[str, list[str]] = {}
    seen_close = set()
    for index, row in close_sells.iterrows():
        date = _date_key(
            row["trade_date"], f"close_sells row {index + 2} trade_date"
        )
        symbol = _symbol(row["symbol"], f"close_sells row {index + 2} symbol")
        key = (date, symbol)
        if key in seen_close:
            raise ValueError(f"combo close_sells 同日同标的重复: {date} {symbol}")
        if date not in known_dates:
            raise ValueError(f"combo close_sells 日期不在 daily 覆盖中: {date}")
        if "exit_price" in close_sells.columns:
            exit_price = row.get("exit_price")
            if pd.notna(exit_price) and str(exit_price).strip().lower() != "close":
                raise ValueError("combo close_sells.exit_price 只支持 close")
        seen_close.add(key)
        close_by_date.setdefault(date, []).append(symbol)
    canonical_close = {
        date: tuple(sorted(symbols)) for date, symbols in close_by_date.items()
    }
    symbols = tuple(
        sorted(
            {symbol for _, symbol, _ in target_rows}
            | {symbol for _, symbol in seen_close}
        )
    )
    bundle = ComboReplayBundle(
        root=root,
        tag=resolved_tag,
        targets_path=paths["targets"],
        close_sells_path=paths["close_sells"],
        daily_path=paths["daily"],
        targets_bytes=payloads["targets"],
        close_sells_bytes=payloads["close_sells"],
        daily_bytes=payloads["daily"],
        known_dates=known_dates,
        symbols=symbols,
        start_date=min(known_dates),
        end_date=max(known_dates),
        target_rows=len(target_rows),
        close_sell_rows=len(seen_close),
        daily_rows=len(daily_dates),
        _targets_by_date=canonical_targets,
        _close_sells_by_date=canonical_close,
    )
    source_manifest = _read_manifest(root)
    if (
        source_manifest is not None
        and _canonical_json_bytes(source_manifest) != bundle.manifest_bytes()
    ):
        raise ValueError(
            "combo manifest 的 scope/rows/sha256/semantics 与实际 CSV 不一致"
        )
    return bundle


__all__ = [
    "COMBO_BUNDLE_SCHEMA",
    "COMBO_BUNDLE_SCHEMA_VERSION",
    "COMBO_CSV_MAX_BYTES",
    "COMBO_MANIFEST_MAX_BYTES",
    "ComboReplayBundle",
    "load_combo_bundle",
]
