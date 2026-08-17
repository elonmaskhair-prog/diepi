"""Generate the small deterministic dataset used by the local demo path.

The generated prices are invented.  They are designed to exercise diePi's
strict daily raw/HFQ pair contract; they are not samples of licensed market
data and must never be presented as investment or research evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Any, Dict, Tuple
import uuid

import pandas as pd

from ..backtest.data.dataset_manifest import (
    DATASET_MANIFEST_FILENAME,
    DatasetManifest,
    build_dataset_manifest,
)
from ..backtest.data.validation_service import (
    DataValidationReport,
    validate_local_data,
    write_validation_report,
)


DEMO_GENERATOR_VERSION = "1"
DEMO_DATASET_ID = "diepi-synthetic-daily-v1"
DEMO_SYMBOL = "000001.SZ"
DEMO_START_DATE = "20240102"
DEMO_END_DATE = "20240216"
DEMO_VALIDATION_FILENAME = "validation.json"

_DATA_RELATIVE_FILES = (
    "parquet/metadata/common/trade_cal.parquet",
    "parquet/metadata/stock/basic.parquet",
    f"parquet/timeseries/daily/{DEMO_SYMBOL}.parquet",
    f"parquet/timeseries/daily_raw/{DEMO_SYMBOL}.parquet",
    f"parquet/timeseries/adj_factor/{DEMO_SYMBOL}.parquet",
)


@dataclass(frozen=True)
class DemoWorkspace:
    """Paths and immutable identities produced by one successful generation."""

    workspace: Path
    data_root: Path
    strategy_file: Path
    manifest_file: Path
    validation_file: Path
    manifest: DatasetManifest
    validation_report: DataValidationReport

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "data_root": str(self.data_root),
            "strategy_file": str(self.strategy_file),
            "manifest_file": str(self.manifest_file),
            "validation_file": str(self.validation_file),
            "dataset_id": self.manifest.dataset_id,
            "dataset_kind": self.manifest.dataset_kind,
            "manifest_sha256": self.manifest.manifest_sha256,
            "validation_status": self.validation_report.status,
            "validation_report_sha256": self.validation_report.report_sha256,
            "symbol": DEMO_SYMBOL,
            "start_date": DEMO_START_DATE,
            "end_date": DEMO_END_DATE,
            "synthetic": True,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2
        ) + "\n"


def _calendar_frame() -> pd.DataFrame:
    dates = pd.date_range(DEMO_START_DATE, DEMO_END_DATE, freq="D")
    return pd.DataFrame(
        {
            "exchange": ["SSE"] * len(dates),
            "cal_date": dates.strftime("%Y%m%d"),
            "is_open": (dates.dayofweek < 5).astype("int64"),
        }
    )


def _daily_frames() -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = pd.date_range(DEMO_START_DATE, DEMO_END_DATE, freq="B")
    close_pattern = (0, 4, 7, 5, 11, 14, 12, 18, 21, 19)
    open_pattern = (-2, 1, -1, 2, 0)
    close_cents = [
        1000 + index * 3 + close_pattern[index % len(close_pattern)]
        for index in range(len(dates))
    ]
    previous_close = [998] + close_cents[:-1]
    open_cents = [
        previous_close[index] + open_pattern[index % len(open_pattern)]
        for index in range(len(dates))
    ]
    high_cents = [
        max(open_value, close_value) + 5
        for open_value, close_value in zip(open_cents, close_cents)
    ]
    low_cents = [
        min(open_value, close_value) - 5
        for open_value, close_value in zip(open_cents, close_cents)
    ]
    volumes = [100_000 + index * 2_000 for index in range(len(dates))]

    raw = pd.DataFrame(
        {
            "ts_code": [DEMO_SYMBOL] * len(dates),
            "trade_date": dates.strftime("%Y%m%d"),
            "open": [value / 100.0 for value in open_cents],
            "high": [value / 100.0 for value in high_cents],
            "low": [value / 100.0 for value in low_cents],
            "close": [value / 100.0 for value in close_cents],
            "pre_close": [value / 100.0 for value in previous_close],
            "vol": volumes,
            # The repository's daily source contract defines amount as
            # thousand yuan.  Values are deterministic and non-negative.
            "amount": [
                (close_cents[index] / 100.0) * volumes[index] / 1000.0
                for index in range(len(dates))
            ],
        }
    )
    # A constant adjustment factor makes raw and HFQ lanes intentionally
    # identical while still exercising the strict AFI-1 identity proof.
    hfq = raw.copy(deep=True)
    factors = pd.DataFrame(
        {
            "ts_code": [DEMO_SYMBOL] * (len(dates) + 1),
            "trade_date": ["20240101", *dates.strftime("%Y%m%d").tolist()],
            "adj_factor": [1.0] * (len(dates) + 1),
        }
    )
    return hfq, raw, factors


def _stock_basic_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_code": [DEMO_SYMBOL],
            "symbol": ["000001"],
            "name": ["SYNTHETIC_DEMO_NOT_REAL"],
            "area": ["SYNTHETIC"],
            "industry": ["SYNTHETIC"],
            "market": ["SYNTHETIC"],
            "list_date": ["20000101"],
            "delist_date": [""],
            "list_status": ["L"],
        }
    )


def _strategy_source() -> str:
    return '''"""Synthetic demo strategy; generated prices are not market data."""

SYMBOL = "000001.SZ"


def on_init(ctx):
    print("diePi synthetic demo: invented data only")


def on_before_market_open(ctx):
    if ctx.current_date == "20240108":
        ctx.buy_at_open(SYMBOL, percent=0.50)
    elif ctx.current_date == "20240205":
        ctx.sell_at_open(SYMBOL, percent=1.00)
    return [SYMBOL]


def on_day(ctx, bars):
    pass


def on_after_market_close(ctx):
    pass


def on_finish(ctx):
    print("Synthetic demo complete; do not interpret its returns as evidence.")
'''


def _readme_source() -> str:
    return """diePi synthetic demo workspace
================================

Every price, volume, calendar flag, and instrument label in market-data/ was
generated by diePi.  No row is real market data.  The workspace exists only
to verify installation, path wiring, strict data validation, and the example
strategy lifecycle.

Contents:
- market-data/: deterministic daily raw/HFQ data and its manifest
- strategy.py: a deterministic buy-then-sell example
- validation.json: strict validation evidence for the generated scope

For research, supply your own lawfully obtained data and validate that scope
before running it.  Passing validation proves structural/contract readiness;
it does not prove source rights, vendor authenticity, or economic correctness.
"""


def _write_text(path: Path, value: str) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)


def _write_dataset(data_root: Path) -> DatasetManifest:
    calendar = _calendar_frame()
    hfq, raw, factors = _daily_frames()
    stock_basic = _stock_basic_frame()

    frames = {
        _DATA_RELATIVE_FILES[0]: calendar,
        _DATA_RELATIVE_FILES[1]: stock_basic,
        _DATA_RELATIVE_FILES[2]: hfq,
        _DATA_RELATIVE_FILES[3]: raw,
        _DATA_RELATIVE_FILES[4]: factors,
    }
    for relative_path, frame in frames.items():
        target = data_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(target, index=False)

    manifest = build_dataset_manifest(
        data_root=data_root,
        dataset_kind="synthetic_demo",
        dataset_id=DEMO_DATASET_ID,
        symbols=(DEMO_SYMBOL,),
        start_date=DEMO_START_DATE,
        end_date=DEMO_END_DATE,
        generator="diepi.demo.generator",
        generator_version=DEMO_GENERATOR_VERSION,
        relative_files=_DATA_RELATIVE_FILES,
    )
    manifest.write(data_root / DATASET_MANIFEST_FILENAME)
    return manifest


def _safe_cleanup_staging(staging: Path, parent: Path) -> None:
    """Remove only the private, resolved staging directory created here."""

    resolved = staging.resolve()
    if (
        resolved.parent == parent.resolve()
        and resolved.name.startswith(".diepi-demo-")
        and resolved.is_dir()
    ):
        shutil.rmtree(resolved)


def generate_synthetic_demo(workspace) -> DemoWorkspace:
    """Atomically create and strictly validate a new synthetic workspace.

    Existing paths are never overwritten.  The caller owns the destination;
    failed generation removes only this function's private staging directory.
    """

    target = Path(workspace).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"demo workspace already exists: {target}")
    parent = target.parent
    if not parent.is_dir():
        raise FileNotFoundError(
            f"demo workspace parent does not exist: {parent}"
        )

    staging = parent / f".diepi-demo-{uuid.uuid4().hex}"
    staging.mkdir()
    try:
        data_root = staging / "market-data"
        data_root.mkdir()
        manifest = _write_dataset(data_root)
        _write_text(staging / "strategy.py", _strategy_source())
        _write_text(staging / "README.txt", _readme_source())

        report = validate_local_data(
            data_root=data_root,
            symbols=(DEMO_SYMBOL,),
            start_date=DEMO_START_DATE,
            end_date=DEMO_END_DATE,
            frequency="daily",
            price_mode="dual",
            verify_manifest=True,
        )
        write_validation_report(report, staging / DEMO_VALIDATION_FILENAME)
        if not report.contract_ready:
            codes = ", ".join(issue.code for issue in report.errors)
            raise RuntimeError(
                "generated synthetic data failed its own validation: " + codes
            )

        staging.rename(target)
    except Exception:
        _safe_cleanup_staging(staging, parent)
        raise

    return DemoWorkspace(
        workspace=target,
        data_root=target / "market-data",
        strategy_file=target / "strategy.py",
        manifest_file=target / "market-data" / DATASET_MANIFEST_FILENAME,
        validation_file=target / DEMO_VALIDATION_FILENAME,
        manifest=manifest,
        validation_report=report,
    )


__all__ = [
    "DEMO_DATASET_ID",
    "DEMO_END_DATE",
    "DEMO_GENERATOR_VERSION",
    "DEMO_START_DATE",
    "DEMO_SYMBOL",
    "DEMO_VALIDATION_FILENAME",
    "DemoWorkspace",
    "generate_synthetic_demo",
]
