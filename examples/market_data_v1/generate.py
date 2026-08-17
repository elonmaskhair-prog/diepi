"""Build the published diePi market-data v1 slice from a local data root.

The builder is deliberately local and read-only: it reads only the declared
symbols, lanes, dates, and columns, never changes the source, and never uses
the network.  The source location is not recorded in any generated artifact.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import shutil
import stat
import sys
from typing import Iterable, Mapping
import uuid

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from diepi.backtest.data.calendar import load_builtin_trade_calendar
from diepi.backtest.data.cache_manager import is_supported_direct_parquet_file
from diepi.backtest.data.dataset_manifest import (
    DATASET_MANIFEST_FILENAME,
    DatasetManifest,
    build_dataset_manifest,
    logical_frame_sha256,
)


GENERATOR_VERSION = "2"
DATASET_ID = "diepi-local-market-data-v1-public-real-2026h1"
START_DATE = "20260101"
END_DATE = "20260630"
MINUTE_OBSERVATIONS_PER_SESSION = 241
_STAGING_PREFIX = ".diepi-market-data-v1-"

DAILY_COLUMNS = (
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "amount",
)
MINUTE_SOURCE_COLUMNS = (
    "ts_code",
    "trade_time",
    "open",
    "high",
    "low",
    "close",
    "amount",
)
MINUTE_COLUMNS = (
    "ts_code",
    "trade_time",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "amount",
)
FACTOR_COLUMNS = ("ts_code", "trade_date", "adj_factor")
METADATA_COLUMNS = (
    "ts_code",
    "symbol",
    "name",
    "list_date",
    "delist_date",
    "list_status",
)
NUMERIC_MARKET_COLUMNS = (
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "amount",
)


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    family: str

    @property
    def source_names(self) -> tuple[str, ...]:
        return self.symbol, self.symbol.replace(".", "_")

    @property
    def directories(self) -> tuple[str, str, str, str, str]:
        if self.family == "stock":
            return "daily", "daily_raw", "minute", "minute_raw", "adj_factor"
        return (
            "etf_daily",
            "etf_daily_raw",
            "etf_minute",
            "etf_minute_raw",
            "etf_adj_factor",
        )


INSTRUMENTS = (
    InstrumentSpec("600000.SH", "stock"),
    InstrumentSpec("000001.SZ", "stock"),
    InstrumentSpec("510300.SH", "etf"),
    InstrumentSpec("159915.SZ", "etf"),
)


def _is_link_or_reparse(path: Path) -> bool:
    info = path.lstat()
    return path.is_symlink() or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    )


def _plain_directory(value, *, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_dir() or _is_link_or_reparse(path):
        raise ValueError(f"{label} must be a plain existing directory")
    return path


def _source_data_root(value) -> Path:
    root = _plain_directory(value, label="source data root")
    parquet_root = root / "parquet"
    if not parquet_root.is_dir() or _is_link_or_reparse(parquet_root):
        raise ValueError("source data root must contain a plain parquet directory")
    return root


def _trading_dates() -> tuple[str, ...]:
    calendar = load_builtin_trade_calendar()
    selected = calendar[
        calendar["cal_date"].between(START_DATE, END_DATE)
        & calendar["is_open"].eq(1)
    ]
    dates = tuple(selected["cal_date"].astype(str))
    if len(dates) != 116 or dates[0] != "20260105" or dates[-1] != END_DATE:
        raise RuntimeError("the reviewed built-in 2026 H1 calendar identity changed")
    return dates


def _minute_labels(trading_dates: Iterable[str]) -> pd.DatetimeIndex:
    parts = []
    for trade_date in trading_dates:
        day = pd.to_datetime(trade_date, format="%Y%m%d")
        parts.append(
            pd.DatetimeIndex([day + pd.Timedelta(hours=9, minutes=30)])
            .append(
                pd.date_range(
                    day + pd.Timedelta(hours=9, minutes=31),
                    periods=120,
                    freq="min",
                )
            )
            .append(
                pd.date_range(
                    day + pd.Timedelta(hours=13, minutes=1),
                    periods=120,
                    freq="min",
                )
            )
        )
    result = parts[0]
    for part in parts[1:]:
        result = result.append(part)
    return result.astype("datetime64[ns]")


def _source_file(
    source_root: Path,
    spec: InstrumentSpec,
    directory: str,
    *,
    minute: bool = False,
) -> Path:
    category_root = source_root / "parquet" / "timeseries" / directory
    for source_name in spec.source_names:
        path = (
            category_root / source_name / "2026.parquet"
            if minute
            else category_root / f"{source_name}.parquet"
        )
        member_root = path.parent if minute else category_root
        if is_supported_direct_parquet_file(path, root=member_root):
            return path
    raise FileNotFoundError(
        f"required {directory} source is missing for {spec.symbol}"
    )


def _metadata_source_file(source_root: Path, family: str) -> Path:
    directory = source_root / "parquet" / "metadata" / family
    path = directory / "basic.parquet"
    if not is_supported_direct_parquet_file(path, root=directory):
        raise FileNotFoundError(f"required {family} basic metadata is missing")
    return path


def _normalize_dates(values: pd.Series, *, label: str) -> pd.Series:
    dates = values.astype("string").str.strip().str.replace(r"\.0$", "", regex=True)
    valid = dates.str.fullmatch(r"\d{8}", na=False)
    parsed = pd.to_datetime(dates.where(valid), format="%Y%m%d", errors="coerce")
    if not bool(valid.all()) or bool(parsed.isna().any()):
        raise ValueError(f"{label} contains an invalid YYYYMMDD value")
    return dates.astype(str)


def _normalize_symbol(frame: pd.DataFrame, symbol: str, *, label: str) -> None:
    values = frame["ts_code"].astype("string").str.strip().str.upper()
    if not bool(values.eq(symbol).all()):
        raise ValueError(f"{label} contains rows outside {symbol}")
    frame["ts_code"] = values.astype(str)


def _normalize_numeric(frame: pd.DataFrame, fields: Iterable[str], *, label: str):
    for field in fields:
        values = pd.to_numeric(frame[field], errors="raise").astype("float64")
        if not bool(np.isfinite(values.to_numpy()).all()):
            raise ValueError(f"{label} contains non-finite {field}")
        frame[field] = values


def _require_keys(actual: Iterable[str], expected: tuple[str, ...], *, label: str):
    observed = tuple(actual)
    if observed != expected:
        raise ValueError(f"{label} does not exactly cover the reviewed open dates")


def _read_daily(
    source_root: Path,
    spec: InstrumentSpec,
    directory: str,
    dates: tuple[str, ...],
) -> pd.DataFrame:
    path = _source_file(source_root, spec, directory)
    frame = pd.read_parquet(path, columns=list(DAILY_COLUMNS))
    frame["trade_date"] = _normalize_dates(
        frame["trade_date"], label=f"{spec.symbol} {directory}"
    )
    frame = frame[frame["trade_date"].between(START_DATE, END_DATE)].copy()
    _normalize_symbol(frame, spec.symbol, label=f"{spec.symbol} {directory}")
    _normalize_numeric(
        frame, NUMERIC_MARKET_COLUMNS, label=f"{spec.symbol} {directory}"
    )
    _require_keys(
        frame["trade_date"], dates, label=f"{spec.symbol} {directory}"
    )
    return frame.loc[:, DAILY_COLUMNS].reset_index(drop=True)


def _read_minute(
    source_root: Path,
    spec: InstrumentSpec,
    directory: str,
    dates: tuple[str, ...],
    daily: pd.DataFrame,
) -> pd.DataFrame:
    path = _source_file(source_root, spec, directory, minute=True)
    frame = pd.read_parquet(path, columns=list(MINUTE_SOURCE_COLUMNS))
    timestamps = pd.to_datetime(frame["trade_time"], errors="raise")
    if getattr(timestamps.dt, "tz", None) is not None:
        raise ValueError(f"{spec.symbol} {directory} trade_time must be timezone-naive")
    frame["trade_time"] = timestamps.astype("datetime64[ns]")
    end_exclusive = pd.Timestamp("2026-07-01")
    frame = frame[
        frame["trade_time"].ge(pd.Timestamp("2026-01-01"))
        & frame["trade_time"].lt(end_exclusive)
    ].copy()
    _normalize_symbol(frame, spec.symbol, label=f"{spec.symbol} {directory}")
    _normalize_numeric(
        frame,
        ("open", "high", "low", "close", "amount"),
        label=f"{spec.symbol} {directory}",
    )
    expected_timestamps = _minute_labels(dates)
    observed_timestamps = pd.DatetimeIndex(frame["trade_time"])
    if not observed_timestamps.equals(expected_timestamps):
        raise ValueError(
            f"{spec.symbol} {directory} does not exactly match the v1 minute session"
        )
    pre_close = daily.set_index("trade_date")["pre_close"]
    minute_dates = frame["trade_time"].dt.strftime("%Y%m%d")
    mapped = minute_dates.map(pre_close)
    if bool(mapped.isna().any()):
        raise ValueError(
            f"{spec.symbol} {directory} cannot derive same-lane daily pre_close"
        )
    frame.insert(6, "pre_close", mapped.astype("float64"))
    if len(frame) != len(dates) * MINUTE_OBSERVATIONS_PER_SESSION:
        raise RuntimeError("reviewed minute observation count changed")
    return frame.loc[:, MINUTE_COLUMNS].reset_index(drop=True)


def _read_factor(
    source_root: Path,
    spec: InstrumentSpec,
    directory: str,
    dates: tuple[str, ...],
) -> pd.DataFrame:
    path = _source_file(source_root, spec, directory)
    frame = pd.read_parquet(path, columns=list(FACTOR_COLUMNS))
    frame["trade_date"] = _normalize_dates(
        frame["trade_date"], label=f"{spec.symbol} {directory}"
    )
    _normalize_symbol(frame, spec.symbol, label=f"{spec.symbol} {directory}")
    _normalize_numeric(
        frame, ("adj_factor",), label=f"{spec.symbol} {directory}"
    )
    if frame.empty or frame["trade_date"].iloc[0] >= START_DATE:
        raise ValueError(f"{spec.symbol} {directory} lacks its original base anchor")
    if not bool(frame["adj_factor"].gt(0.0).all()):
        raise ValueError(f"{spec.symbol} {directory} has a non-positive factor")
    scoped = frame[frame["trade_date"].between(START_DATE, END_DATE)].copy()
    _require_keys(scoped["trade_date"], dates, label=f"{spec.symbol} {directory}")
    selected = pd.concat([frame.iloc[[0]], scoped], ignore_index=True)
    return selected.loc[:, FACTOR_COLUMNS]


def _metadata_schema(path: Path) -> frozenset[str]:
    return frozenset(pq.ParquetFile(path).schema_arrow.names)


def _read_metadata(source_root: Path, family: str) -> pd.DataFrame:
    path = _metadata_source_file(source_root, family)
    available = _metadata_schema(path)
    name_source = "name" if "name" in available else "csname"
    required = {"ts_code", name_source, "list_date", "list_status"}
    missing = sorted(required - available)
    if missing:
        raise ValueError(
            f"{family} basic metadata is missing required columns: {','.join(missing)}"
        )
    read_columns = ["ts_code", name_source, "list_date", "list_status"]
    if "delist_date" in available:
        read_columns.append("delist_date")
    source = pd.read_parquet(path, columns=read_columns)
    family_symbols = tuple(
        spec.symbol for spec in INSTRUMENTS if spec.family == family
    )
    source["ts_code"] = (
        source["ts_code"].astype("string").str.strip().str.upper()
    )
    source = source[source["ts_code"].isin(family_symbols)].copy()
    if source["ts_code"].duplicated().any() or set(source["ts_code"]) != set(
        family_symbols
    ):
        raise ValueError(f"{family} basic metadata does not uniquely cover the slice")
    source = source.set_index("ts_code").loc[list(family_symbols)].reset_index()
    result = pd.DataFrame(
        {
            "ts_code": source["ts_code"].astype(str),
            "symbol": source["ts_code"].str.split(".").str[0].astype(str),
            "name": source[name_source].fillna("").astype(str),
            "list_date": _normalize_dates(
                source["list_date"], label=f"{family} basic metadata"
            ),
            "delist_date": (
                source["delist_date"].fillna("").astype(str)
                if "delist_date" in source
                else [""] * len(source)
            ),
            "list_status": source["list_status"].fillna("").astype(str),
        }
    )
    return result.loc[:, METADATA_COLUMNS]


def _relative_paths() -> tuple[str, ...]:
    paths = [
        "parquet/metadata/stock/basic.parquet",
        "parquet/metadata/etf/basic.parquet",
    ]
    for spec in INSTRUMENTS:
        daily, raw_daily, minute, raw_minute, factor = spec.directories
        paths.extend(
            [
                f"parquet/timeseries/{daily}/{spec.symbol}.parquet",
                f"parquet/timeseries/{raw_daily}/{spec.symbol}.parquet",
                f"parquet/timeseries/{minute}/{spec.symbol}/2026.parquet",
                f"parquet/timeseries/{raw_minute}/{spec.symbol}/2026.parquet",
                f"parquet/timeseries/{factor}/{spec.symbol}.parquet",
            ]
        )
    return tuple(paths)


def _read_source_frames(source_root: Path) -> Mapping[str, pd.DataFrame]:
    dates = _trading_dates()
    frames = {
        "parquet/metadata/stock/basic.parquet": _read_metadata(
            source_root, "stock"
        ),
        "parquet/metadata/etf/basic.parquet": _read_metadata(source_root, "etf"),
    }
    for spec in INSTRUMENTS:
        daily_dir, raw_daily_dir, minute_dir, raw_minute_dir, factor_dir = (
            spec.directories
        )
        daily = _read_daily(source_root, spec, daily_dir, dates)
        raw_daily = _read_daily(source_root, spec, raw_daily_dir, dates)
        if not daily["trade_date"].equals(raw_daily["trade_date"]):
            raise ValueError(f"{spec.symbol} daily raw/HFQ keys differ")
        frames[f"parquet/timeseries/{daily_dir}/{spec.symbol}.parquet"] = daily
        frames[f"parquet/timeseries/{raw_daily_dir}/{spec.symbol}.parquet"] = (
            raw_daily
        )
        frames[
            f"parquet/timeseries/{minute_dir}/{spec.symbol}/2026.parquet"
        ] = _read_minute(source_root, spec, minute_dir, dates, daily)
        frames[
            f"parquet/timeseries/{raw_minute_dir}/{spec.symbol}/2026.parquet"
        ] = _read_minute(source_root, spec, raw_minute_dir, dates, raw_daily)
        frames[f"parquet/timeseries/{factor_dir}/{spec.symbol}.parquet"] = (
            _read_factor(source_root, spec, factor_dir, dates)
        )
    return frames


def _write_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(path, index=False, engine="pyarrow", compression="zstd")


def _write_dataset(
    target: Path, frames: Mapping[str, pd.DataFrame]
) -> DatasetManifest:
    for relative_path in _relative_paths():
        _write_parquet(target / relative_path, frames[relative_path])
    manifest = build_dataset_manifest(
        data_root=target,
        dataset_kind="user_supplied",
        dataset_id=DATASET_ID,
        symbols=tuple(spec.symbol for spec in INSTRUMENTS),
        start_date=START_DATE,
        end_date=END_DATE,
        generator="examples.market_data_v1.generate",
        generator_version=GENERATOR_VERSION,
        relative_files=_relative_paths(),
    )
    written = {identity.path: identity for identity in manifest.files}
    for relative_path, source_frame in frames.items():
        identity = written[relative_path]
        if (
            identity.rows != len(source_frame)
            or identity.logical_sha256 != logical_frame_sha256(source_frame)
        ):
            raise RuntimeError(
                f"written Parquet differs from normalized source: {relative_path}"
            )
    manifest.write(target / DATASET_MANIFEST_FILENAME)
    return manifest


def _safe_cleanup_staging(staging: Path, parent: Path) -> None:
    resolved = staging.resolve()
    if (
        resolved.parent == parent.resolve()
        and resolved.name.startswith(_STAGING_PREFIX)
        and resolved.is_dir()
        and not _is_link_or_reparse(resolved)
    ):
        shutil.rmtree(resolved)


def generate_dataset(source_data_root, output_root) -> DatasetManifest:
    """Create one normalized slice without modifying or identifying its source."""

    source_root = _source_data_root(source_data_root)
    target = Path(output_root).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"destination already exists: {target}")
    parent = _plain_directory(target.parent, label="destination parent")
    source_parquet = (source_root / "parquet").resolve()
    if target == source_root or target == source_parquet:
        raise ValueError("destination must not replace the source data root")
    try:
        target.relative_to(source_parquet)
    except ValueError:
        pass
    else:
        raise ValueError("destination must not be inside the source parquet tree")

    frames = _read_source_frames(source_root)
    staging = parent / f"{_STAGING_PREFIX}{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        manifest = _write_dataset(staging, frames)
        staging.rename(target)
    except Exception:
        _safe_cleanup_staging(staging, parent)
        raise
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-data-root",
        type=Path,
        required=True,
        help="local data root containing parquet/; read-only and never recorded",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent / "data",
        help="new data root to create; existing destinations are refused",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    manifest = generate_dataset(args.source_data_root, args.output)
    print(f"created public real-data v1 slice: {Path(args.output).resolve()}")
    print(f"manifest_sha256={manifest.manifest_sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
