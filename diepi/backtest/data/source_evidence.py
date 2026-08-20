"""Privacy-preserving identities for local market-data display sources.

Run artifacts never embed or disclose an absolute ``DATA_ROOT``.  For an
explicit symbol universe we can, however, record the content hash and logical
path of the Parquet files that supplied the configured price lanes.  The GUI
uses the execution/display lane to prove that a locally reloaded K-line is the
same file as the one identified by the artifact before overlaying trades.

The identity is intentionally file-granular.  Appending or rewriting a local
Parquet file invalidates the display proof even when the requested date slice
looks unchanged; failing closed is preferable to presenting current local
data as verified historical evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Iterable, Mapping, Optional, Sequence, Tuple

import pandas as pd

from diepi.artifacts import RunProvenance, SourceFingerprint

from .cache_manager import (
    is_supported_direct_parquet_directory,
    is_supported_direct_parquet_file,
    normalize_data_symbol,
)
from .calendar import TradeCalendarIdentity
from ..instruments import is_exchange_fund


MARKET_DATA_SOURCE_KIND = "market_data_file"
RUNTIME_METADATA_SOURCE_KIND = "market_metadata_file"
TRADE_CALENDAR_SOURCE_KIND = "trade_calendar"
PRICE_MODES = frozenset({"dual", "hfq", "raw"})
MINUTE_FREQUENCIES = frozenset(
    {"minute", "1min", "5min", "15min", "30min", "60min"}
)


def trade_calendar_fingerprint(identity: TradeCalendarIdentity) -> SourceFingerprint:
    """Represent the exact parsed market clock as artifact provenance evidence."""

    if not isinstance(identity, TradeCalendarIdentity):
        raise TypeError("identity must be a TradeCalendarIdentity")
    payload = json.dumps(
        identity.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return SourceFingerprint.from_bytes(
        kind=TRADE_CALENDAR_SOURCE_KIND,
        logical_path=f"calendar/{identity.source}.json",
        payload=payload,
    )


def collect_trade_calendar_fingerprint(data_root: object) -> SourceFingerprint:
    """Load and fingerprint the market clock a fresh provider would select."""

    # Local import avoids making the provider/calendar/cache module graph
    # depend on this provenance helper at import time.
    from .data_provider import DataProvider

    provider = DataProvider(
        data_root=Path(data_root).expanduser().resolve(strict=True),
        price_mode="hfq",
        execution_price_mode="raw",
    )
    return trade_calendar_fingerprint(provider.trade_calendar_identity)


def normalize_price_mode(value: object) -> str:
    """Return one supported public price mode, without guessing."""

    if type(value) is not str:
        raise TypeError("price_mode must be exactly str")
    mode = value.strip().lower()
    if mode not in PRICE_MODES:
        raise ValueError("price_mode must be one of dual/hfq/raw")
    return mode


def artifact_price_mode(config: Mapping) -> Optional[str]:
    """Read ``price_mode`` from either the GUI or CLI artifact config shape."""

    if not isinstance(config, Mapping):
        return None
    value = config.get("price_mode")
    parameters = config.get("parameters")
    if value is None and isinstance(parameters, Mapping):
        value = parameters.get("price_mode")
    if value is None:
        return None
    try:
        return normalize_price_mode(value)
    except (TypeError, ValueError):
        return None


def artifact_symbols(config: Mapping) -> Optional[Tuple[str, ...]]:
    """Read an explicit symbol scope; ``None`` means non-explicit/unknown."""

    if not isinstance(config, Mapping):
        return None
    value = config.get("realized_symbols")
    if value is None:
        value = config.get("symbols")
    parameters = config.get("parameters")
    if value is None and isinstance(parameters, Mapping):
        value = parameters.get("pool_symbols")
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        return None
    symbols = tuple(str(symbol).strip() for symbol in value)
    if any(not symbol for symbol in symbols):
        return None
    return symbols


def display_price_mode(price_mode: str) -> str:
    """Return the price lane appropriate for fills/trade-marker overlays."""

    mode = normalize_price_mode(price_mode)
    return "raw" if mode == "dual" else mode


def _symbol_candidates(symbol: str) -> Tuple[str, ...]:
    canonical = normalize_data_symbol(symbol)
    alternate = canonical.replace(".", "_")
    return ((canonical,) if alternate == canonical else (canonical, alternate))


def _daily_directory(symbol: str, mode: str) -> str:
    prefix = "etf_" if is_exchange_fund(symbol) else ""
    suffix = "daily_raw" if mode == "raw" else "daily"
    return prefix + suffix


def expected_daily_paths(symbol: str, mode: str) -> Tuple[str, ...]:
    """Return direct-file candidates in the same order as ``CacheManager``."""

    lane = normalize_price_mode(mode)
    if lane == "dual":
        raise ValueError("expected_daily_paths requires a concrete raw/hfq lane")
    directory = _daily_directory(symbol, lane)
    return tuple(
        PurePosixPath(
            "parquet", "timeseries", directory, f"{candidate}.parquet"
        ).as_posix()
        for candidate in _symbol_candidates(symbol)
    )


def _factor_paths(symbol: str) -> Tuple[str, ...]:
    directory = "etf_adj_factor" if is_exchange_fund(symbol) else "adj_factor"
    return tuple(
        PurePosixPath(
            "parquet", "timeseries", directory, f"{candidate}.parquet"
        ).as_posix()
        for candidate in _symbol_candidates(symbol)
    )


def _shared_runtime_paths(_symbols: Iterable[str]) -> Tuple[str, ...]:
    # These files are shared inputs rather than symbol-specific lanes.  Bind
    # all of them when present, including for ALL_MARKET (symbols=None), so a
    # universe can never be selected from one metadata generation and later
    # attributed to another.
    return (
        "parquet/metadata/common/industry/mapping.parquet",
        "parquet/metadata/stock/basic.parquet",
        "parquet/metadata/etf/basic.parquet",
    )


def _runtime_support_paths(symbol: str) -> Tuple[str, ...]:
    candidates = []
    for directory in (
        "daily_basic",
        "moneyflow",
        "cyq_chips",
        "margin",
        "holder_trade",
        "repurchase",
    ):
        candidates.extend(
            PurePosixPath(
                "parquet", "timeseries", directory, f"{candidate}.parquet"
            ).as_posix()
            for candidate in _symbol_candidates(symbol)
        )
    return tuple(candidates)


def _minute_directory(symbol: str, mode: str) -> str:
    prefix = "etf_" if is_exchange_fund(symbol) else ""
    suffix = "minute_raw" if mode == "raw" else "minute"
    return prefix + suffix


def expected_minute_directories(symbol: str, mode: str) -> Tuple[str, ...]:
    """Return direct minute-directory candidates in reader routing order."""

    lane = normalize_price_mode(mode)
    if lane == "dual":
        raise ValueError(
            "expected_minute_directories requires a concrete raw/hfq lane"
        )
    directory = _minute_directory(symbol, lane)
    return tuple(
        PurePosixPath(
            "parquet", "timeseries", directory, candidate
        ).as_posix()
        for candidate in _symbol_candidates(symbol)
    )


def _first_existing(root: Path, candidates: Sequence[str]) -> Optional[Path]:
    for relative in candidates:
        path = root.joinpath(*PurePosixPath(relative).parts)
        if is_supported_direct_parquet_file(path, root=root):
            return path
    return None


def _first_existing_plain_directory(
    root: Path, candidates: Sequence[str]
) -> Optional[Path]:
    for relative in candidates:
        path = root.joinpath(*PurePosixPath(relative).parts)
        if is_supported_direct_parquet_directory(path, root=root):
            return path
    return None


def _fingerprint_readable_nonempty_parquet(
    path: Path,
    *,
    root: Path,
    kind: str = MARKET_DATA_SOURCE_KIND,
) -> Optional[SourceFingerprint]:
    """Prove that a direct Parquet file prevents reader fallback.

    ``ParquetReader`` falls back to ETF section files when a direct daily file
    is empty or unreadable.  Merely hashing an existing path would therefore
    bind the artifact to a file that the engine did not use.  Read the direct
    file between two stable fingerprints and claim it only when it is both
    unchanged and non-empty.
    """

    before = SourceFingerprint.from_file(
        path, root=root, kind=kind
    )
    try:
        frame = pd.read_parquet(path)
    except Exception:
        return None
    after = SourceFingerprint.from_file(
        path, root=root, kind=kind
    )
    if before != after:
        raise OSError(
            "source file changed while validating direct Parquet routing"
        )
    if frame.empty:
        return None
    return before


def collect_market_data_fingerprints(
    data_root,
    *,
    symbols: Optional[Iterable[str]],
    price_mode: str,
    frequency: str = "daily",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    _include_shared_runtime_metadata: bool = True,
) -> Tuple[SourceFingerprint, ...]:
    """Hash direct sources for one explicit configured/realized universe.

    Missing/direct-file-unsupported sources are omitted instead of making
    artifact publication fail.  Their absence causes GUI display verification
    to fail closed.  This covers the raw-minimal layout and both direct-file
    lanes used by normal dual/hfq datasets; legacy ETF cross-section fallback
    is deliberately not claimed as verified by this helper.
    """

    mode = normalize_price_mode(price_mode)
    if frequency != "daily" and frequency not in MINUTE_FREQUENCIES:
        raise ValueError("frequency must be daily or a supported minute frequency")
    if data_root is None:
        return ()
    if type(_include_shared_runtime_metadata) is not bool:
        raise TypeError("_include_shared_runtime_metadata must be exactly bool")
    root = Path(data_root).expanduser().resolve()
    if not root.is_dir():
        return ()

    lanes = ("hfq", "raw") if mode == "dual" else (mode,)
    requested_years = None
    if start_date is not None or end_date is not None:
        if start_date is None or end_date is None:
            raise ValueError(
                "start_date and end_date must be supplied together"
            )
        start_text = str(start_date).replace("-", "")
        end_text = str(end_date).replace("-", "")
        if (
            len(start_text) != 8
            or len(end_text) != 8
            or not start_text.isdigit()
            or not end_text.isdigit()
            or start_text > end_text
        ):
            raise ValueError("source-evidence dates must be ordered YYYYMMDD")
        requested_years = frozenset(
            str(year)
            for year in range(
                int(start_text[:4]), int(end_text[:4]) + 1
            )
        )
    requested_symbols = () if symbols is None else symbols
    canonical_symbols = tuple(sorted({
        str(value).strip() for value in requested_symbols if str(value).strip()
    }))
    sources = []
    seen = set()
    shared_runtime_paths = (
        _shared_runtime_paths(canonical_symbols)
        if _include_shared_runtime_metadata
        else ()
    )
    for relative in shared_runtime_paths:
        common_metadata = _first_existing(root, (relative,))
        if common_metadata is None:
            continue
        source = _fingerprint_readable_nonempty_parquet(
            common_metadata,
            root=root,
            kind=RUNTIME_METADATA_SOURCE_KIND,
        )
        if source is not None:
            key = (source.kind, source.logical_path.casefold())
            if key not in seen:
                sources.append(source)
                seen.add(key)
    for symbol in canonical_symbols:
        for lane in lanes:
            path = _first_existing(root, expected_daily_paths(symbol, lane))
            if path is not None:
                source = _fingerprint_readable_nonempty_parquet(
                    path, root=root
                )
                if source is not None:
                    key = (source.kind, source.logical_path.casefold())
                    if key not in seen:
                        sources.append(source)
                        seen.add(key)
        for relative in _runtime_support_paths(symbol):
            support = _first_existing(root, (relative,))
            if support is None:
                continue
            source = _fingerprint_readable_nonempty_parquet(
                support,
                root=root,
                kind=RUNTIME_METADATA_SOURCE_KIND,
            )
            if source is not None:
                key = (source.kind, source.logical_path.casefold())
                if key not in seen:
                    sources.append(source)
                    seen.add(key)
        if mode == "dual":
            factor = _first_existing(root, _factor_paths(symbol))
            if factor is not None:
                source = _fingerprint_readable_nonempty_parquet(
                    factor, root=root
                )
                if source is not None:
                    key = (source.kind, source.logical_path.casefold())
                    if key not in seen:
                        sources.append(source)
                        seen.add(key)
        if frequency in MINUTE_FREQUENCIES:
            for lane in lanes:
                directory = _first_existing_plain_directory(
                    root, expected_minute_directories(symbol, lane)
                )
                if directory is None:
                    continue
                for path in sorted(directory.glob("*.parquet")):
                    if (
                        requested_years is not None
                        and path.stem not in requested_years
                    ):
                        continue
                    if not is_supported_direct_parquet_file(
                        path, root=root
                    ):
                        continue
                    source = _fingerprint_readable_nonempty_parquet(
                        path, root=root
                    )
                    if source is None:
                        continue
                    key = (source.kind, source.logical_path.casefold())
                    if key not in seen:
                        sources.append(source)
                        seen.add(key)
    return tuple(sorted(sources, key=lambda item: (item.kind, item.logical_path)))


def require_complete_direct_sources(
    symbol: str,
    price_mode: str,
    sources: Iterable[SourceFingerprint],
    *,
    frequency: str = "daily",
) -> Tuple[str, ...]:
    """Reject untracked fallback routes for an explicit, rankable universe."""

    mode = normalize_price_mode(price_mode)
    if frequency != "daily" and frequency not in MINUTE_FREQUENCIES:
        raise ValueError("frequency must be daily or a supported minute frequency")
    frozen = tuple(sources)
    if any(not isinstance(source, SourceFingerprint) for source in frozen):
        raise TypeError("sources must contain SourceFingerprint values")
    allowed_kinds = {MARKET_DATA_SOURCE_KIND, RUNTIME_METADATA_SOURCE_KIND}
    if any(source.kind not in allowed_kinds for source in frozen):
        raise ValueError(
            "sources must contain only market_data_file or "
            "market_metadata_file provenance evidence"
        )
    logical_paths = {
        source.logical_path
        for source in frozen
        if source.kind == MARKET_DATA_SOURCE_KIND
    }
    lanes = ("hfq", "raw") if mode == "dual" else (mode,)
    missing = []
    for lane in lanes:
        if not any(
            candidate in logical_paths for candidate in expected_daily_paths(symbol, lane)
        ):
            missing.append(f"daily:{lane}")
        if frequency in MINUTE_FREQUENCIES:
            directories = set(expected_minute_directories(symbol, lane))
            if not any(
                PurePosixPath(path).parent.as_posix() in directories
                and PurePosixPath(path).suffix == ".parquet"
                and len(PurePosixPath(path).stem) == 4
                and PurePosixPath(path).stem.isdigit()
                for path in logical_paths
            ):
                missing.append(f"minute:{lane}")
    if mode == "dual" and not any(
        candidate in logical_paths for candidate in _factor_paths(symbol)
    ):
        missing.append("adj_factor")
    if missing:
        raise ValueError(
            "DYNAMIC_MARKET_DATA_SOURCE_UNVERIFIED: observed symbol "
            f"{symbol} is not backed by complete direct v1 sources "
            f"({', '.join(missing)}); fallback routes cannot be ranked"
        )
    return ()


class DynamicMarketDataFingerprintTracker:
    """Bind sources first reached outside an explicit configured universe.

    Configured symbols keep using the existing whole-scope before/after check.
    The engine calls :meth:`observe` before a dynamic symbol is read, so this
    tracker can freeze that symbol's source generation and verify it after the
    run.  This preserves dynamic-pool strategies without misrepresenting the
    configured pool as the complete realized data scope.
    """

    def __init__(
        self,
        data_root,
        *,
        configured_symbols: Optional[Iterable[str]],
        price_mode: str,
        frequency: str = "daily",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        collector=None,
        allow_incomplete_sources: bool = False,
    ) -> None:
        self._data_root = data_root
        self._configured = frozenset(
            str(value).strip()
            for value in (configured_symbols or ())
            if str(value).strip()
        )
        self._price_mode = normalize_price_mode(price_mode)
        self._frequency = frequency
        self._start_date = start_date
        self._end_date = end_date
        self._collector = collector or collect_market_data_fingerprints
        self._uses_builtin_collector = (
            self._collector is collect_market_data_fingerprints
        )
        if type(allow_incomplete_sources) is not bool:
            raise TypeError("allow_incomplete_sources must be exactly bool")
        self._allow_incomplete_sources = allow_incomplete_sources
        self._snapshots = {}
        self._shared_snapshots = {}
        self._incomplete_symbols = set()
        self._lock = RLock()

    def _capture(
        self,
        symbol: str,
        *,
        frequency: str,
        start_date: Optional[str],
        end_date: Optional[str],
    ) -> Tuple[SourceFingerprint, ...]:
        kwargs = {
            "symbols": (symbol,),
            "price_mode": self._price_mode,
            "frequency": frequency,
            "start_date": start_date,
            "end_date": end_date,
        }
        if self._uses_builtin_collector:
            kwargs["_include_shared_runtime_metadata"] = False
        return tuple(self._collector(self._data_root, **kwargs))

    def _read_shared_source(
        self, relative: str
    ) -> Optional[SourceFingerprint]:
        if self._data_root is None:
            return None
        root = Path(self._data_root).expanduser().resolve()
        if not root.is_dir():
            return None
        path = _first_existing(root, (relative,))
        if path is None:
            return None
        return _fingerprint_readable_nonempty_parquet(
            path,
            root=root,
            kind=RUNTIME_METADATA_SOURCE_KIND,
        )

    def _shared_sources_for(
        self, symbol: str
    ) -> Tuple[SourceFingerprint, ...]:
        if not self._uses_builtin_collector:
            return ()
        sources = []
        for relative in _shared_runtime_paths((symbol,)):
            if relative not in self._shared_snapshots:
                self._shared_snapshots[relative] = self._read_shared_source(
                    relative
                )
            source = self._shared_snapshots[relative]
            if source is not None:
                sources.append(source)
        return tuple(sources)

    def _with_shared_sources(
        self,
        symbol: str,
        sources: Iterable[SourceFingerprint],
        *,
        shared_snapshots=None,
    ) -> Tuple[SourceFingerprint, ...]:
        if not self._uses_builtin_collector:
            return tuple(sources)
        frozen_shared = (
            self._shared_snapshots
            if shared_snapshots is None
            else shared_snapshots
        )
        combined = list(sources)
        for relative in _shared_runtime_paths((symbol,)):
            source = frozen_shared.get(relative)
            if source is not None:
                combined.append(source)
        unique = {
            (source.kind, source.logical_path): source for source in combined
        }
        return tuple(sorted(
            unique.values(), key=lambda item: (item.kind, item.logical_path)
        ))

    def _require_direct_v1_sources(
        self,
        symbol: str,
        sources: Tuple[SourceFingerprint, ...],
        *,
        frequency: str,
    ) -> Tuple[str, ...]:
        return require_complete_direct_sources(
            symbol,
            self._price_mode,
            sources,
            frequency=frequency,
        )

    @staticmethod
    def _years(start_date: Optional[str], end_date: Optional[str]) -> frozenset:
        if start_date is None or end_date is None:
            return frozenset()
        start = str(start_date).replace("-", "").replace("/", "")
        end = str(end_date).replace("-", "").replace("/", "")
        return frozenset(
            str(year)
            for year in range(int(start[:4]), int(end[:4]) + 1)
        )

    def observe(
        self,
        symbols: Iterable[str],
        *,
        frequency: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        require_direct_prices: bool = True,
    ) -> Tuple[str, ...]:
        if isinstance(symbols, (str, bytes)):
            raise TypeError("market-data observer requires an iterable of symbols")
        if type(require_direct_prices) is not bool:
            raise TypeError("require_direct_prices must be exactly bool")
        canonical = tuple(sorted({
            str(value).strip()
            for value in symbols
            if str(value).strip()
        }))
        evidence_frequency = frequency or self._frequency
        evidence_start = start_date or self._start_date
        evidence_end = end_date or self._end_date
        requested_years = self._years(evidence_start, evidence_end)
        base_years = self._years(self._start_date, self._end_date)
        # Serialize first-observation capture.  Strategy callbacks are normally
        # single-threaded, but this keeps the contract stable for custom hosts.
        with self._lock:
            for symbol in canonical:
                if symbol in self._configured:
                    if evidence_frequency == "daily":
                        continue
                    if (
                        self._frequency in MINUTE_FREQUENCIES
                        and requested_years.issubset(base_years)
                    ):
                        continue
                key = (
                    symbol,
                    evidence_frequency,
                    evidence_start,
                    evidence_end,
                    require_direct_prices,
                )
                if key in self._snapshots:
                    continue
                # A pool-wide observation normally freezes the whole requested
                # run window before preload.  Do not re-hash the same files on
                # every active trading day; only ranges that reach additional
                # minute-year partitions need another snapshot.
                covered = False
                for existing_key in self._snapshots:
                    (
                        existing_symbol,
                        existing_frequency,
                        existing_start,
                        existing_end,
                        existing_requires_direct_prices,
                    ) = existing_key
                    if existing_symbol != symbol:
                        continue
                    if (
                        require_direct_prices
                        and not existing_requires_direct_prices
                    ):
                        continue
                    if (
                        evidence_frequency == "daily"
                        and existing_frequency == "daily"
                    ):
                        covered = True
                        break
                    if (
                        evidence_frequency in MINUTE_FREQUENCIES
                        and existing_frequency in MINUTE_FREQUENCIES
                        and requested_years.issubset(
                            self._years(existing_start, existing_end)
                        )
                    ):
                        covered = True
                        break
                if covered:
                    continue
                self._shared_sources_for(symbol)
                sources = self._capture(
                    symbol,
                    frequency=evidence_frequency,
                    start_date=evidence_start,
                    end_date=evidence_end,
                )
                sources = self._with_shared_sources(symbol, sources)
                if require_direct_prices:
                    try:
                        self._require_direct_v1_sources(
                            symbol, sources, frequency=evidence_frequency
                        )
                    except ValueError:
                        if not self._allow_incomplete_sources:
                            raise
                        self._incomplete_symbols.add(symbol)
                self._snapshots[key] = sources
        return tuple(sorted(self._incomplete_symbols.intersection(canonical)))

    @property
    def incomplete_symbols(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._incomplete_symbols))

    @property
    def fingerprints(self) -> Tuple[SourceFingerprint, ...]:
        with self._lock:
            unique = {
                (source.kind, source.logical_path): source
                for values in self._snapshots.values()
                for source in values
            }
        return tuple(sorted(unique.values(), key=lambda item: (
            item.kind, item.logical_path
        )))

    def verify_stable(self) -> Tuple[SourceFingerprint, ...]:
        with self._lock:
            snapshots = tuple(sorted(
                self._snapshots.items(),
                key=lambda item: tuple(
                    "" if value is None else str(value)
                    for value in item[0]
                ),
            ))
            shared_snapshots = dict(self._shared_snapshots)
        for relative, before in sorted(shared_snapshots.items()):
            after = self._read_shared_source(relative)
            if after != before:
                raise OSError(
                    "MARKET_DATA_CHANGED_DURING_RUN: shared runtime metadata "
                    f"fingerprint changed for {relative}"
                )
        for (
            symbol,
            frequency,
            start_date,
            end_date,
            _requires_direct_prices,
        ), before in snapshots:
            after = self._capture(
                symbol,
                frequency=frequency,
                start_date=start_date,
                end_date=end_date,
            )
            after = self._with_shared_sources(
                symbol, after, shared_snapshots=shared_snapshots
            )
            if after != before:
                raise OSError(
                    "MARKET_DATA_CHANGED_DURING_RUN: dynamic market-data "
                    f"fingerprints changed for {symbol}"
                )
        return self.fingerprints


@dataclass(frozen=True)
class DailySourceVerification:
    """Result of matching one local K-line source to artifact provenance."""

    verified: bool
    price_mode: Optional[str]
    logical_path: Optional[str]
    message: str


def verify_display_daily_source(
    provenance,
    *,
    data_root,
    symbol: str,
    price_mode: Optional[str],
    scope_symbols: Optional[Iterable[str]] = None,
) -> DailySourceVerification:
    """Verify one current local daily file before the GUI displays it."""

    if price_mode is None:
        return DailySourceVerification(
            False, None, None, "Artifact 未记录 price_mode，无法确定 K 线价格口径"
        )
    try:
        lane = display_price_mode(price_mode)
    except (TypeError, ValueError):
        return DailySourceVerification(
            False, None, None, "Artifact 的 price_mode 无效"
        )

    if scope_symbols is not None:
        scope = {str(value).strip() for value in scope_symbols}
        if symbol not in scope:
            return DailySourceVerification(
                False,
                lane,
                None,
                f"{symbol} 不在 Artifact 记录的显式标的范围内",
            )
    if type(provenance) is not RunProvenance:
        return DailySourceVerification(
            False, lane, None, "Artifact 未提供可验证的数据来源记录"
        )
    expected = set(expected_daily_paths(symbol, lane))
    recorded = [
        source
        for source in provenance.sources
        if source.kind == MARKET_DATA_SOURCE_KIND
        and source.logical_path in expected
    ]
    if not recorded:
        return DailySourceVerification(
            False,
            lane,
            None,
            f"Artifact 未记录 {symbol} 的 {lane} 日线文件指纹",
        )
    if len(recorded) != 1:
        return DailySourceVerification(
            False,
            lane,
            None,
            f"Artifact 对 {symbol} 的 {lane} 日线记录不唯一",
        )
    if data_root is None:
        return DailySourceVerification(
            False, lane, recorded[0].logical_path, "当前 GUI 未配置 DATA_ROOT"
        )
    root = Path(data_root).expanduser().resolve()
    if not root.is_dir():
        return DailySourceVerification(
            False, lane, recorded[0].logical_path, "当前 DATA_ROOT 不存在"
        )

    source = recorded[0]
    local = root.joinpath(*PurePosixPath(source.logical_path).parts)
    if not is_supported_direct_parquet_file(local, root=root):
        return DailySourceVerification(
            False,
            lane,
            source.logical_path,
            f"当前 DATA_ROOT 缺少受支持的普通文件 {source.logical_path}",
        )
    try:
        current = SourceFingerprint.from_file(
            local, root=root, kind=MARKET_DATA_SOURCE_KIND
        )
    except (OSError, ValueError) as exc:
        return DailySourceVerification(
            False,
            lane,
            source.logical_path,
            f"当前本地行情文件无法稳定校验: {exc}",
        )
    if current.byte_length != source.byte_length or current.sha256 != source.sha256:
        return DailySourceVerification(
            False,
            lane,
            source.logical_path,
            "当前本地行情文件与 Artifact 的 SHA-256 不一致",
        )
    return DailySourceVerification(
        True,
        lane,
        source.logical_path,
        f"已校验 {source.logical_path}（SHA-256 一致）",
    )


def load_verified_display_daily_source(
    provenance,
    *,
    data_root,
    symbol: str,
    price_mode: Optional[str],
    scope_symbols: Optional[Iterable[str]] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> Tuple[pd.DataFrame, DailySourceVerification]:
    """Read the exact verified direct file, never a provider fallback.

    Verification, Parquet decoding, and a second verification form one
    fail-closed transaction.  The returned frame therefore comes from the
    content generation recorded by the artifact; a path replacement between
    verification and reading is rejected.
    """

    verification = verify_display_daily_source(
        provenance,
        data_root=data_root,
        symbol=symbol,
        price_mode=price_mode,
        scope_symbols=scope_symbols,
    )
    if not verification.verified or verification.logical_path is None:
        raise OSError(verification.message)

    root = Path(data_root).expanduser().resolve(strict=True)
    local = root.joinpath(*PurePosixPath(verification.logical_path).parts)
    recorded = next(
        source
        for source in provenance.sources
        if source.kind == MARKET_DATA_SOURCE_KIND
        and source.logical_path == verification.logical_path
    )
    try:
        payload = local.read_bytes()
    except Exception as exc:
        raise OSError(
            "verified direct daily Parquet can no longer be read"
        ) from exc
    observed = SourceFingerprint.from_bytes(
        kind=MARKET_DATA_SOURCE_KIND,
        logical_path=verification.logical_path,
        payload=payload,
    )
    if observed != recorded:
        raise OSError(
            "verified direct daily source changed before it was read"
        )
    try:
        # Parse the exact bytes whose SHA-256 was compared above.  The path is
        # never reopened for the displayed frame.
        frame = pd.read_parquet(BytesIO(payload))
    except Exception as exc:
        raise OSError(
            "verified direct daily Parquet can no longer be decoded"
        ) from exc
    confirmed = verify_display_daily_source(
        provenance,
        data_root=root,
        symbol=symbol,
        price_mode=price_mode,
        scope_symbols=scope_symbols,
    )
    if not confirmed.verified or confirmed != verification:
        raise OSError(
            "verified direct daily source changed while it was being read"
        )
    if frame.empty:
        raise OSError(
            "verified direct daily source is empty; ETF fallback is forbidden"
        )
    if "trade_date" not in frame.columns:
        raise ValueError("verified direct daily source lacks trade_date")

    result = frame.copy()
    result["trade_date"] = (
        result["trade_date"].astype(str).str.split(".").str[0]
    )
    if start is not None:
        result = result[result["trade_date"] >= str(start).replace("-", "")]
    if end is not None:
        result = result[result["trade_date"] <= str(end).replace("-", "")]
    result = result.sort_values("trade_date")
    return result.set_index("trade_date"), confirmed


__all__ = [
    "DailySourceVerification",
    "DynamicMarketDataFingerprintTracker",
    "MARKET_DATA_SOURCE_KIND",
    "MINUTE_FREQUENCIES",
    "RUNTIME_METADATA_SOURCE_KIND",
    "PRICE_MODES",
    "TRADE_CALENDAR_SOURCE_KIND",
    "artifact_price_mode",
    "artifact_symbols",
    "collect_trade_calendar_fingerprint",
    "collect_market_data_fingerprints",
    "display_price_mode",
    "expected_daily_paths",
    "expected_minute_directories",
    "load_verified_display_daily_source",
    "normalize_price_mode",
    "require_complete_direct_sources",
    "trade_calendar_fingerprint",
    "verify_display_daily_source",
]
