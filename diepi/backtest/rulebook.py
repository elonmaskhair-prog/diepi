"""Pure instrument classification and execution-routing rules.

This module deliberately has no dependency on the cash backtest engines, broker,
or data providers.  It describes *what* an instrument is and whether the current
framework supports it; adapters can later translate ``data_route`` into a
frequency-specific storage category.

Classification precedence is deterministic:

1. security-master metadata;
2. an explicit ``kind_overrides`` entry;
3. conservative exchange/code-range fallbacks.

The fallback cannot resolve every exchange-traded fund subtype.  Callers with a
security master should therefore provide ``kind`` metadata for ambiguous codes.
"""
from __future__ import annotations

import re
from hashlib import sha256
from dataclasses import dataclass, fields, replace
from datetime import date as date_type, datetime
from enum import Enum
from math import isfinite
from numbers import Integral, Real
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional, Tuple, Type, TypeVar, Union


class InstrumentKind(str, Enum):
    """Instrument families understood by the rule book."""

    A_SHARE = "a_share"
    ETF = "etf"
    LOF = "lof"
    REIT = "reit"
    INDEX = "index"
    FUTURE = "future"
    UNKNOWN = "unknown"


class Venue(str, Enum):
    """Trading/listing venue, independent from vendor suffix conventions."""

    SSE = "SSE"
    SZSE = "SZSE"
    BSE = "BSE"
    CFFEX = "CFFEX"
    UNKNOWN = "UNKNOWN"


class ExecutionEngine(str, Enum):
    """Engine family capable of executing an instrument."""

    CASH = "cash"
    FUTURES = "futures"
    NONE = "none"


class LimitBandRoundingPolicy(str, Enum):
    """Tick rounding used to derive legal daily price-band boundaries."""

    NEAREST_HALF_UP = "nearest_half_up"
    TOWARD_REFERENCE = "toward_reference"


_ENGINE_BY_KIND = {
    InstrumentKind.A_SHARE: ExecutionEngine.CASH,
    InstrumentKind.ETF: ExecutionEngine.CASH,
    InstrumentKind.LOF: ExecutionEngine.CASH,
    InstrumentKind.REIT: ExecutionEngine.CASH,
    InstrumentKind.INDEX: ExecutionEngine.NONE,
    InstrumentKind.FUTURE: ExecutionEngine.FUTURES,
    InstrumentKind.UNKNOWN: ExecutionEngine.NONE,
}
_ROUTE_BY_KIND = {
    InstrumentKind.A_SHARE: "equity",
    InstrumentKind.ETF: "exchange_fund",
    InstrumentKind.LOF: "exchange_fund",
    InstrumentKind.REIT: "exchange_fund",
    InstrumentKind.INDEX: "index",
    InstrumentKind.FUTURE: "cffex_futures",
    InstrumentKind.UNKNOWN: "unknown",
}
_SETTLEMENTS_BY_KIND = {
    InstrumentKind.A_SHARE: frozenset({"T+1"}),
    InstrumentKind.ETF: frozenset({"T+0", "T+1"}),
    InstrumentKind.LOF: frozenset({"T+0", "T+1"}),
    InstrumentKind.REIT: frozenset({"T+1"}),
    InstrumentKind.INDEX: frozenset({"N/A"}),
    InstrumentKind.FUTURE: frozenset({"T+0"}),
    InstrumentKind.UNKNOWN: frozenset({"N/A"}),
}
_SUPPORTED_VENUES_BY_KIND = {
    InstrumentKind.A_SHARE: frozenset({Venue.SSE, Venue.SZSE, Venue.BSE}),
    InstrumentKind.ETF: frozenset({Venue.SSE, Venue.SZSE}),
    InstrumentKind.LOF: frozenset({Venue.SSE, Venue.SZSE}),
    InstrumentKind.FUTURE: frozenset({Venue.CFFEX}),
}
_UNSUPPORTED_KINDS = frozenset(
    {InstrumentKind.REIT, InstrumentKind.INDEX, InstrumentKind.UNKNOWN}
)


@dataclass(frozen=True)
class InstrumentRule:
    """Immutable, reproducible rules for one classified instrument.

    ``data_route`` is a logical data family, not a concrete daily/minute cache
    key.  ``effective_from`` and ``effective_to`` use ``YYYYMMDD`` when set.
    """

    kind: InstrumentKind
    venue: Venue
    engine: ExecutionEngine
    supported: bool
    data_route: str
    price_decimals: int
    buy_min_qty: int
    buy_step: int
    settlement: str
    stamp_exempt: bool
    multiplier: Optional[int] = None
    margin_rate: Optional[float] = None
    effective_from: Optional[str] = None
    effective_to: Optional[str] = None
    source_version: str = "cn-mainland-rulebook-v1"

    def __post_init__(self) -> None:
        for name, enum_type in (
            ("kind", InstrumentKind),
            ("venue", Venue),
            ("engine", ExecutionEngine),
        ):
            if not isinstance(getattr(self, name), enum_type):
                raise TypeError(f"{name} must be a {enum_type.__name__}")
        for name in ("supported", "stamp_exempt"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")
        for name in ("price_decimals", "buy_min_qty", "buy_step"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, Integral):
                raise TypeError(f"{name} must be an integer")
        if self.price_decimals < 0:
            raise ValueError("price_decimals must be non-negative")
        if self.buy_min_qty < 0 or self.buy_step < 0:
            raise ValueError("buy_min_qty and buy_step must be non-negative")
        if self.multiplier is not None:
            if isinstance(self.multiplier, bool) or not isinstance(
                self.multiplier, Integral
            ):
                raise TypeError("multiplier must be an integer when provided")
            if self.multiplier <= 0:
                raise ValueError("multiplier must be positive when provided")
        if self.margin_rate is not None:
            if isinstance(self.margin_rate, bool) or not isinstance(
                self.margin_rate, Real
            ):
                raise TypeError("margin_rate must be a real number when provided")
            if not isfinite(float(self.margin_rate)):
                raise ValueError("margin_rate must be finite when provided")
            if not 0 < self.margin_rate <= 1:
                raise ValueError("margin_rate must be in (0, 1]")
        for name in ("effective_from", "effective_to"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
                    raise ValueError(f"{name} must use YYYYMMDD")
                try:
                    datetime.strptime(value, "%Y%m%d")
                except ValueError as exc:
                    raise ValueError(f"{name} must be a valid YYYYMMDD date") from exc
        if (
            self.effective_from is not None
            and self.effective_to is not None
            and self.effective_from > self.effective_to
        ):
            raise ValueError("effective_from must not be later than effective_to")
        for name in ("data_route", "settlement", "source_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

        expected_engine = _ENGINE_BY_KIND[self.kind]
        if self.engine is not expected_engine:
            raise ValueError(
                f"kind={self.kind.value} requires engine={expected_engine.value}"
            )
        expected_route = _ROUTE_BY_KIND[self.kind]
        if self.data_route != expected_route:
            raise ValueError(
                f"kind={self.kind.value} requires data_route={expected_route!r}"
            )
        allowed_settlements = _SETTLEMENTS_BY_KIND[self.kind]
        if self.settlement not in allowed_settlements:
            allowed = ", ".join(sorted(allowed_settlements))
            raise ValueError(
                f"kind={self.kind.value} settlement must be one of: {allowed}"
            )
        if self.supported and self.engine is ExecutionEngine.NONE:
            raise ValueError("engine=none cannot be supported")
        if self.supported and self.kind in _UNSUPPORTED_KINDS:
            raise ValueError(f"kind={self.kind.value} is not supported in v0.1")
        if self.supported:
            allowed_venues = _SUPPORTED_VENUES_BY_KIND.get(self.kind)
            if allowed_venues is None or self.venue not in allowed_venues:
                allowed = ", ".join(
                    sorted(venue.value for venue in (allowed_venues or ()))
                )
                raise ValueError(
                    f"kind={self.kind.value} cannot be supported on "
                    f"venue={self.venue.value}; allowed venues: {allowed or 'none'}"
                )
            if self.buy_min_qty <= 0 or self.buy_step <= 0:
                raise ValueError(
                    "supported instruments require positive buy_min_qty and buy_step"
                )

        if self.kind is InstrumentKind.FUTURE:
            has_multiplier = self.multiplier is not None
            has_margin = self.margin_rate is not None
            if has_multiplier != has_margin:
                raise ValueError(
                    "futures multiplier and margin_rate must be provided together"
                )
            if self.supported and not (has_multiplier and has_margin):
                raise ValueError(
                    "supported futures require multiplier and margin_rate"
                )
        elif self.multiplier is not None or self.margin_rate is not None:
            raise ValueError(
                "non-futures instruments cannot define multiplier or margin_rate"
            )


class UnsupportedInstrumentError(ValueError):
    """Raised when a rule is unsupported or routed to a different engine."""

    def __init__(
        self,
        symbol: str,
        rule: InstrumentRule,
        requested_engine: Optional[ExecutionEngine] = None,
    ) -> None:
        self.symbol = symbol
        self.rule = rule
        self.requested_engine = requested_engine
        if not rule.supported:
            reason = f"kind={rule.kind.value} is not supported"
        else:
            reason = (
                f"requires engine={rule.engine.value}, "
                f"not engine={requested_engine.value if requested_engine else 'none'}"
            )
        super().__init__(f"Unsupported instrument {symbol!r}: {reason}")


MetadataEntry = Union[InstrumentRule, InstrumentKind, str, Mapping[str, Any]]
_EnumT = TypeVar("_EnumT", bound=Enum)

_RULE_FIELD_NAMES = {item.name for item in fields(InstrumentRule)}
_METADATA_KIND_KEYS = ("kind", "instrument_kind", "asset_type")
_DIRECT_METADATA_KEYS = _RULE_FIELD_NAMES.union(_METADATA_KIND_KEYS)
_FUTURE_PATTERN = re.compile(r"^(IF|IH|IC|IM)(?:\d{4})?(?:\.(?:CFX|CFFEX|CFE))?$")
_CORE_INDEX_CODES = frozenset({"000016", "000300", "000852", "000905"})

# Effective-dated cash price-band snapshot.  This code list is deliberately
# private to the rule-book snapshot: execution code must never carry its own
# security list or silently classify an unknown code as an ordinary stock.
# The observations were refreshed on 2026-08-09 and are applied only to
# instruments which RuleBook has independently classified as ETF/LOF.
_FUND_20PCT_SNAPSHOT_CODES = frozenset(
    {
        "159022", "159107", "159256", "159287", "159363", "159367",
        "159370", "159371", "159373", "159375", "159383", "159541",
        "159563", "159571", "159572", "159573", "159575", "159597",
        "159603", "159675", "159676", "159681", "159682", "159773",
        "159777", "159780", "159781", "159782", "159783", "159804",
        "159808", "159810", "159814", "159821", "159836", "159908",
        "159915", "159948", "159949", "159952", "159956", "159957",
        "159958", "159964", "159966", "159967", "159971", "159977",
        "159991", "160143", "160223", "160325", "160529", "160637",
        "160926", "161040", "161914", "162720", "166027", "168207",
        "506000", "506001", "506002", "506003",
        "506005", "506006", "506008",
    }
)

# Metadata rule (index_name/csname contains 创业板/科创板/科创创业/双创,
# excluding 科技创新公司债) unioned with price-band observations.  Dates are
# max(2020-08-24, list_date), frozen from metadata/etf/basic.parquet on
# 2026-08-07.  This closes the known 22-symbol false-10% hole in the former
# close-return-only generator (for example 159381/159388).
_METADATA_FUND_20PCT_EFFECTIVE_FROM = {
    "159009": "20260414", "159012": "20260520", "159022": "20260424",
    "159069": "20260721", "159076": "20260708", "159107": "20250922",
    "159122": "20251103", "159139": "20251219", "159140": "20251218",
    "159141": "20251211", "159142": "20251212", "159149": "20260120",
    "159187": "20260401", "159190": "20260330", "159205": "20250612",
    "159242": "20250724", "159243": "20260105", "159246": "20250710",
    "159247": "20260202", "159256": "20250804", "159261": "20250707",
    "159270": "20250724", "159279": "20250929", "159287": "20250827",
    "159288": "20250926", "159289": "20250915", "159290": "20250917",
    "159291": "20250901", "159292": "20250902", "159293": "20260113",
    "159295": "20251104", "159298": "20250908", "159363": "20241216",
    "159367": "20250214", "159368": "20250324", "159369": "20250821",
    "159370": "20250120", "159371": "20250212", "159372": "20250609",
    "159373": "20250210", "159375": "20250110", "159377": "20250415",
    "159381": "20250324", "159382": "20250509", "159383": "20250214",
    "159387": "20250610", "159388": "20250407", "159541": "20231031",
    "159563": "20240223", "159571": "20240102", "159572": "20231225",
    "159573": "20231225", "159575": "20240102", "159597": "20240527",
    "159603": "20220608", "159675": "20221202", "159676": "20230412",
    "159681": "20230103", "159682": "20230103", "159773": "20211008",
    "159777": "20211018", "159780": "20210705", "159781": "20210705",
    "159782": "20210707", "159783": "20210705", "159804": "20200824",
    "159808": "20200904", "159810": "20200824", "159814": "20200824",
    "159821": "20201029", "159836": "20210525", "159908": "20200824",
    "159915": "20200824", "159948": "20200824", "159949": "20200824",
    "159952": "20200824", "159955": "20200824", "159956": "20200824",
    "159957": "20200824", "159958": "20200824", "159964": "20200824",
    "159966": "20200824", "159967": "20200824", "159971": "20200824",
    "159977": "20200824", "159991": "20200824",
}
_FUND_20PCT_EFFECTIVE_FROM = MappingProxyType(
    {
        **{code: "20200824" for code in _FUND_20PCT_SNAPSHOT_CODES},
        **_METADATA_FUND_20PCT_EFFECTIVE_FROM,
    }
)

LIMIT_BAND_SOURCE_VERSION = "cn-cash-limit-band-v3-20260809"
LIMIT_BAND_FUND_SNAPSHOT_FREEZE_DATE = "20260807"
LIMIT_BAND_SNAPSHOT_COVERAGE = (
    "sse-szse-rules-from-19901219;"
    "bse-rules-from-20211115;"
    "explicit-20pct-fund-codes-observed-through-20260807;"
    "post-freeze-fund-issuance-not-detectable-without-authoritative-list-date;"
    "unknown-funds-use-ordinary-10pct-with-residual-misclassification-risk"
)
_LIMIT_BASE_EFFECTIVE_FROM = "19901219"
_LIMIT_REFORM_DATE = "20200824"
_LIMIT_PRE_REFORM_END = "20200823"
_LIMIT_BSE_DATE = "20211115"
_LIMIT_STAR_BOARD_FROM = "20190722"
_LIMIT_STAR_ETF_FROM = "20200824"
_LIMIT_BAND_SNAPSHOT_PAYLOAD = "|".join(
    (
        LIMIT_BAND_SOURCE_VERSION,
        f"coverage:sse-szse-from:{_LIMIT_BASE_EFFECTIVE_FROM}",
        f"coverage:bse-from:{_LIMIT_BSE_DATE}",
        (
            "fund-20pct-snapshot-freeze:"
            f"{LIMIT_BAND_FUND_SNAPSHOT_FREEZE_DATE}"
        ),
        f"coverage-statement:{LIMIT_BAND_SNAPSHOT_COVERAGE}",
        f"gem-and-observed-funds:{_LIMIT_REFORM_DATE}:10pct-to-20pct",
        f"bse:{_LIMIT_BSE_DATE}:30pct",
        (
            "rounding:default:"
            f"{LimitBandRoundingPolicy.NEAREST_HALF_UP.value}"
        ),
        (
            f"rounding:bse-from:{_LIMIT_BSE_DATE}:"
            f"{LimitBandRoundingPolicy.TOWARD_REFERENCE.value}"
        ),
        f"star-board:{_LIMIT_STAR_BOARD_FROM}:20pct:tick-0.01",
        f"star-etf:{_LIMIT_STAR_ETF_FROM}:20pct:tick-0.001",
        "ordinary-a-share:10pct:tick-0.01",
        "ordinary-etf-lof:10pct:tick-0.001",
        *(
            f"{code}:{effective_from}"
            for code, effective_from in sorted(
                _FUND_20PCT_EFFECTIVE_FROM.items()
            )
        ),
    )
)
LIMIT_BAND_SNAPSHOT_HASH = sha256(
    _LIMIT_BAND_SNAPSHOT_PAYLOAD.encode("ascii")
).hexdigest()

_KIND_ALIASES = {
    "A_SHARE": InstrumentKind.A_SHARE,
    "ASHARE": InstrumentKind.A_SHARE,
    "A_STOCK": InstrumentKind.A_SHARE,
    "STOCK": InstrumentKind.A_SHARE,
    "EQUITY": InstrumentKind.A_SHARE,
    "ETF": InstrumentKind.ETF,
    "LOF": InstrumentKind.LOF,
    "REIT": InstrumentKind.REIT,
    "REITS": InstrumentKind.REIT,
    "INDEX": InstrumentKind.INDEX,
    "FUTURE": InstrumentKind.FUTURE,
    "FUTURES": InstrumentKind.FUTURE,
    "UNKNOWN": InstrumentKind.UNKNOWN,
}

_VENUE_ALIASES = {
    "SH": Venue.SSE,
    "SSE": Venue.SSE,
    "XSHG": Venue.SSE,
    "SZ": Venue.SZSE,
    "SZSE": Venue.SZSE,
    "XSHE": Venue.SZSE,
    "BJ": Venue.BSE,
    "BSE": Venue.BSE,
    "XBEI": Venue.BSE,
    "CFX": Venue.CFFEX,
    "CFE": Venue.CFFEX,
    "CFFEX": Venue.CFFEX,
    "UNKNOWN": Venue.UNKNOWN,
}

_ENGINE_ALIASES = {
    "CASH": ExecutionEngine.CASH,
    "FUTURE": ExecutionEngine.FUTURES,
    "FUTURES": ExecutionEngine.FUTURES,
    "NONE": ExecutionEngine.NONE,
}


def _normalise_symbol(symbol: str) -> str:
    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")
    normalised = symbol.strip().upper()
    if not normalised:
        raise ValueError("symbol must be a non-empty string")
    return normalised


def _symbol_parts(symbol: str) -> Tuple[str, str]:
    normalised = _normalise_symbol(symbol)
    if "." not in normalised:
        return normalised, ""
    code, suffix = normalised.split(".", 1)
    return code, suffix


def _future_product(symbol: str) -> Optional[str]:
    normalised = _normalise_symbol(symbol)
    match = _FUTURE_PATTERN.fullmatch(normalised)
    return match.group(1) if match else None


def _candidate_keys(symbol: str) -> Tuple[str, ...]:
    normalised = _normalise_symbol(symbol)
    code, _ = _symbol_parts(normalised)
    product = _future_product(normalised)
    candidates = []
    for candidate in (normalised, code, product):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def _coerce_kind(value: Union[InstrumentKind, str]) -> InstrumentKind:
    if isinstance(value, InstrumentKind):
        return value
    if not isinstance(value, str):
        raise TypeError(f"kind must be InstrumentKind or str, got {type(value).__name__}")
    key = value.strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return _KIND_ALIASES[key]
    except KeyError as exc:
        raise ValueError(f"unknown instrument kind: {value!r}") from exc


def _coerce_enum(
    enum_type: Type[_EnumT], value: Union[_EnumT, str], aliases: Mapping[str, _EnumT]
) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        raise TypeError(f"{enum_type.__name__} must be an enum member or str")
    key = value.strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return aliases[key]
    except KeyError as exc:
        raise ValueError(f"unknown {enum_type.__name__}: {value!r}") from exc


def _normalise_entry(entry: MetadataEntry) -> MetadataEntry:
    if isinstance(entry, Mapping):
        unknown = [key for key in entry if key not in _DIRECT_METADATA_KEYS]
        if unknown:
            labels = ", ".join(sorted(repr(key) for key in unknown))
            raise ValueError(f"metadata contains unknown rule field(s): {labels}")
        normalised = dict(entry)
        declared_kinds = []
        for key in _METADATA_KIND_KEYS:
            if key in normalised and normalised[key] is not None:
                declared_kinds.append((key, _coerce_kind(normalised[key])))
        if declared_kinds:
            first_kind = declared_kinds[0][1]
            if any(kind is not first_kind for _, kind in declared_kinds[1:]):
                fields_used = ", ".join(key for key, _ in declared_kinds)
                raise ValueError(
                    f"metadata contains conflicting kind fields: {fields_used}"
                )
        return normalised
    if isinstance(entry, (InstrumentRule, InstrumentKind, str)):
        return entry
    raise TypeError(
        "metadata entry must be an InstrumentRule, InstrumentKind, str, or mapping"
    )


def _normalise_keyed_entries(
    entries: Optional[Mapping[str, MetadataEntry]], name: str
) -> Dict[str, MetadataEntry]:
    result: Dict[str, MetadataEntry] = {}
    if entries is None:
        return result
    if not isinstance(entries, Mapping):
        raise TypeError(f"{name} must be a mapping keyed by symbol")
    for raw_key, raw_entry in entries.items():
        key = _normalise_symbol(raw_key)
        if not key:
            raise ValueError(f"{name} contains an empty symbol")
        if key in result:
            raise ValueError(f"{name} contains duplicate normalised symbol {key!r}")
        result[key] = _normalise_entry(raw_entry)
    return result


def _lookup(entries: Mapping[str, MetadataEntry], symbol: str) -> Optional[MetadataEntry]:
    for key in _candidate_keys(symbol):
        if key in entries:
            return entries[key]
    return None


def _looks_like_direct_metadata(value: Mapping[str, Any]) -> bool:
    return any(key in _DIRECT_METADATA_KEYS for key in value)


def _metadata_for_symbol(
    symbol: str, metadata: Optional[MetadataEntry]
) -> Optional[MetadataEntry]:
    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        return _normalise_entry(metadata)
    if _looks_like_direct_metadata(metadata):
        return _normalise_entry(metadata)
    normalised = _normalise_keyed_entries(metadata, "metadata")
    return _lookup(normalised, symbol)


def _kind_from_metadata(entry: Optional[MetadataEntry]) -> Optional[InstrumentKind]:
    if entry is None:
        return None
    if isinstance(entry, InstrumentRule):
        return entry.kind
    if isinstance(entry, (InstrumentKind, str)):
        return _coerce_kind(entry)
    for key in _METADATA_KIND_KEYS:
        if key in entry and entry[key] is not None:
            return _coerce_kind(entry[key])
    return None


def _venue_from_metadata(entry: Optional[MetadataEntry]) -> Optional[Venue]:
    if isinstance(entry, InstrumentRule):
        return entry.venue
    if isinstance(entry, Mapping) and entry.get("venue") is not None:
        return _coerce_enum(Venue, entry["venue"], _VENUE_ALIASES)
    return None


def _infer_venue(symbol: str) -> Venue:
    code, suffix = _symbol_parts(symbol)
    if _future_product(symbol) is not None:
        return Venue.CFFEX
    if suffix:
        return _VENUE_ALIASES.get(suffix, Venue.UNKNOWN)
    # These bare codes collide with valid Shenzhen securities.  A suffix or
    # explicit metadata is required; silently routing them to either exchange
    # can select the wrong instrument.
    if code in _CORE_INDEX_CODES:
        return Venue.UNKNOWN
    if code.startswith(("5", "6")):
        return Venue.SSE
    if code.startswith(("0", "3", "15", "16", "18")):
        return Venue.SZSE
    if code.startswith(("43", "83", "87", "92")):
        return Venue.BSE
    return Venue.UNKNOWN


def _fallback_kind(symbol: str, venue: Venue) -> InstrumentKind:
    code, suffix = _symbol_parts(symbol)
    if _future_product(symbol) is not None:
        return InstrumentKind.FUTURE
    if not re.fullmatch(r"\d{6}", code):
        return InstrumentKind.UNKNOWN

    # Exchange suffixes eliminate otherwise dangerous 000xxx ambiguities.
    if (venue is Venue.SSE and code.startswith("000")) or (
        venue is Venue.SZSE and code.startswith("399")
    ):
        return InstrumentKind.INDEX
    if not suffix and code in _CORE_INDEX_CODES:
        return InstrumentKind.UNKNOWN

    # REIT and LOF ranges must be checked before the broader fund ranges.
    if (venue is Venue.SSE and code.startswith("508")) or (
        venue is Venue.SZSE and code.startswith("180")
    ):
        return InstrumentKind.REIT
    if (venue is Venue.SSE and code.startswith(("501", "502"))) or (
        venue is Venue.SZSE and code.startswith("16")
    ):
        return InstrumentKind.LOF
    # 580xxx were exchange warrants, while Shenzhen 150xxx-158xxx include
    # historical graded/closed-end products.  Treat only the unambiguous
    # current Shenzhen 159xxx ETF range as an ETF without security metadata.
    if venue is Venue.SSE and code.startswith("580"):
        return InstrumentKind.UNKNOWN
    if venue is Venue.SZSE and code.startswith("15") and not code.startswith("159"):
        return InstrumentKind.UNKNOWN
    if (venue is Venue.SSE and code.startswith("5")) or (
        venue is Venue.SZSE and code.startswith("159")
    ):
        return InstrumentKind.ETF

    if venue is Venue.SSE and code.startswith("6"):
        return InstrumentKind.A_SHARE
    if venue is Venue.SZSE and code.startswith(("0", "3")):
        return InstrumentKind.A_SHARE
    if venue is Venue.BSE and code.startswith(("43", "83", "87", "92")):
        return InstrumentKind.A_SHARE
    return InstrumentKind.UNKNOWN


def _load_future_spec(product: str) -> Mapping[str, Any]:
    """Load futures specifications only when a futures rule is requested."""

    from ..futures.constants import get_spec

    return get_spec(product)


def _base_rule(
    symbol: str,
    kind: InstrumentKind,
    venue: Venue,
    source_version: str,
) -> InstrumentRule:
    code, _ = _symbol_parts(symbol)
    if kind is InstrumentKind.A_SHARE:
        if venue is Venue.SSE and code.startswith(("688", "689")):
            buy_min_qty, buy_step = 200, 1
        elif venue is Venue.BSE:
            buy_min_qty, buy_step = 100, 1
        else:
            buy_min_qty, buy_step = 100, 100
        return InstrumentRule(
            kind=kind,
            venue=venue,
            engine=ExecutionEngine.CASH,
            supported=True,
            data_route="equity",
            price_decimals=2,
            buy_min_qty=buy_min_qty,
            buy_step=buy_step,
            settlement="T+1",
            stamp_exempt=False,
            source_version=source_version,
        )
    if kind in (InstrumentKind.ETF, InstrumentKind.LOF):
        # 511 (bond/money-market), 513 (cross-border), and 518 (gold)
        # Shanghai ETFs are unambiguously T+0 from their code ranges.  Other
        # ranges, especially Shenzhen 159xxx, mix products with different
        # settlement rules and therefore stay conservatively T+1 unless
        # security-master metadata overrides them.
        settlement = "T+1"
        if (
            kind is InstrumentKind.ETF
            and venue is Venue.SSE
            and code.startswith(("511", "513", "518"))
        ):
            settlement = "T+0"
        return InstrumentRule(
            kind=kind,
            venue=venue,
            engine=ExecutionEngine.CASH,
            supported=True,
            data_route="exchange_fund",
            price_decimals=3,
            buy_min_qty=100,
            buy_step=100,
            settlement=settlement,
            stamp_exempt=True,
            source_version=source_version,
        )
    if kind is InstrumentKind.REIT:
        return InstrumentRule(
            kind=kind,
            venue=venue,
            engine=ExecutionEngine.CASH,
            supported=False,
            data_route="exchange_fund",
            price_decimals=3,
            buy_min_qty=100,
            buy_step=1,
            settlement="T+1",
            stamp_exempt=True,
            source_version=source_version,
        )
    if kind is InstrumentKind.INDEX:
        return InstrumentRule(
            kind=kind,
            venue=venue,
            engine=ExecutionEngine.NONE,
            supported=False,
            data_route="index",
            price_decimals=2,
            buy_min_qty=0,
            buy_step=0,
            settlement="N/A",
            stamp_exempt=True,
            source_version=source_version,
        )
    if kind is InstrumentKind.FUTURE:
        product = _future_product(symbol)
        if product is not None:
            spec = _load_future_spec(product)
            return InstrumentRule(
                kind=kind,
                venue=Venue.CFFEX,
                engine=ExecutionEngine.FUTURES,
                supported=True,
                data_route="cffex_futures",
                price_decimals=1,
                buy_min_qty=1,
                buy_step=1,
                settlement="T+0",
                stamp_exempt=True,
                multiplier=int(spec["multiplier"]),
                margin_rate=float(spec["margin_rate"]),
                source_version=source_version,
            )
        return InstrumentRule(
            kind=kind,
            venue=venue,
            engine=ExecutionEngine.FUTURES,
            supported=False,
            data_route="cffex_futures",
            price_decimals=1,
            buy_min_qty=1,
            buy_step=1,
            settlement="T+0",
            stamp_exempt=True,
            source_version=source_version,
        )
    return InstrumentRule(
        kind=InstrumentKind.UNKNOWN,
        venue=venue,
        engine=ExecutionEngine.NONE,
        supported=False,
        data_route="unknown",
        price_decimals=2,
        buy_min_qty=0,
        buy_step=0,
        settlement="N/A",
        stamp_exempt=True,
        source_version=source_version,
    )


def _apply_metadata(rule: InstrumentRule, entry: Optional[MetadataEntry]) -> InstrumentRule:
    if entry is None or isinstance(entry, (InstrumentKind, str)):
        return rule
    if isinstance(entry, InstrumentRule):
        return entry
    updates = {key: value for key, value in entry.items() if key in _RULE_FIELD_NAMES}
    # Alias kind keys participate in classification but should also update the rule.
    metadata_kind = _kind_from_metadata(entry)
    if metadata_kind is not None:
        updates["kind"] = metadata_kind
    if "venue" in updates:
        updates["venue"] = _coerce_enum(Venue, updates["venue"], _VENUE_ALIASES)
    if "engine" in updates:
        updates["engine"] = _coerce_enum(
            ExecutionEngine, updates["engine"], _ENGINE_ALIASES
        )
    return replace(rule, **updates)


class RuleBook:
    """Classify instruments and produce immutable execution rules."""

    DEFAULT_SOURCE_VERSION = "cn-mainland-rulebook-v1"

    def __init__(
        self,
        metadata: Optional[Mapping[str, MetadataEntry]] = None,
        kind_overrides: Optional[Mapping[str, Union[InstrumentKind, str]]] = None,
        source_version: str = DEFAULT_SOURCE_VERSION,
    ) -> None:
        if not isinstance(source_version, str) or not source_version.strip():
            raise ValueError("source_version must be a non-empty string")
        self._metadata = _normalise_keyed_entries(metadata, "metadata")
        raw_overrides = _normalise_keyed_entries(kind_overrides, "kind_overrides")
        self._kind_overrides: Dict[str, InstrumentKind] = {
            key: _coerce_kind(value) for key, value in raw_overrides.items()
        }
        self.source_version = source_version

    def _metadata_entry(
        self, symbol: str, metadata: Optional[MetadataEntry]
    ) -> Optional[MetadataEntry]:
        direct = _metadata_for_symbol(symbol, metadata)
        if direct is not None:
            return direct
        return _lookup(self._metadata, symbol)

    def classify(
        self, symbol: str, metadata: Optional[MetadataEntry] = None
    ) -> InstrumentKind:
        normalised = _normalise_symbol(symbol)
        entry = self._metadata_entry(normalised, metadata)
        metadata_kind = _kind_from_metadata(entry)
        if metadata_kind is not None:
            return metadata_kind
        override = _lookup(self._kind_overrides, normalised)
        if override is not None:
            return _coerce_kind(override)
        venue = _venue_from_metadata(entry) or _infer_venue(normalised)
        return _fallback_kind(normalised, venue)

    def get_rule(
        self, symbol: str, metadata: Optional[MetadataEntry] = None
    ) -> InstrumentRule:
        normalised = _normalise_symbol(symbol)
        entry = self._metadata_entry(normalised, metadata)
        if isinstance(entry, InstrumentRule):
            return entry
        kind = self.classify(normalised, metadata=entry)
        venue = _venue_from_metadata(entry) or _infer_venue(normalised)
        base = _base_rule(normalised, kind, venue, self.source_version)
        return _apply_metadata(base, entry)

    def require_supported(
        self,
        symbol: str,
        engine: Optional[Union[ExecutionEngine, str]] = None,
        metadata: Optional[MetadataEntry] = None,
    ) -> InstrumentRule:
        rule = self.get_rule(symbol, metadata=metadata)
        requested_engine = None
        if engine is not None:
            requested_engine = _coerce_enum(
                ExecutionEngine, engine, _ENGINE_ALIASES
            )
        if not rule.supported or (
            requested_engine is not None and rule.engine is not requested_engine
        ):
            raise UnsupportedInstrumentError(symbol, rule, requested_engine)
        return rule


class LimitBandRuleNotFoundError(ValueError):
    """Raised when no supported cash price-band rule covers a trade date."""


@dataclass(frozen=True)
class LimitBandRule:
    """One immutable, effective-dated daily price-band rule.

    ``tick_size`` and ``price_decimals`` intentionally travel with the band so
    matching code cannot independently guess a security's price grid.
    """

    instrument_kind: InstrumentKind
    venue: Venue
    limit_pct: float
    tick_size: float
    price_decimals: int
    effective_from: str
    effective_to: Optional[str]
    rule_id: str
    rounding_policy: LimitBandRoundingPolicy
    source_version: str = LIMIT_BAND_SOURCE_VERSION
    snapshot_hash: str = LIMIT_BAND_SNAPSHOT_HASH

    def __post_init__(self) -> None:
        if self.instrument_kind not in (
            InstrumentKind.A_SHARE,
            InstrumentKind.ETF,
            InstrumentKind.LOF,
        ):
            raise ValueError(
                "LimitBandRule supports only A-share, ETF, and LOF instruments"
            )
        if not isinstance(self.venue, Venue):
            raise TypeError("venue must be a Venue")
        if type(self.rounding_policy) is not LimitBandRoundingPolicy:
            raise TypeError(
                "rounding_policy must be exactly LimitBandRoundingPolicy"
            )
        if isinstance(self.limit_pct, bool) or not isinstance(self.limit_pct, Real):
            raise TypeError("limit_pct must be a real number")
        if not isfinite(float(self.limit_pct)) or not 0 < float(self.limit_pct) < 1:
            raise ValueError("limit_pct must be finite and in (0, 1)")
        if isinstance(self.tick_size, bool) or not isinstance(self.tick_size, Real):
            raise TypeError("tick_size must be a real number")
        if not isfinite(float(self.tick_size)) or float(self.tick_size) <= 0:
            raise ValueError("tick_size must be finite and positive")
        if isinstance(self.price_decimals, bool) or not isinstance(
            self.price_decimals, Integral
        ):
            raise TypeError("price_decimals must be an integer")
        expected_tick = 10.0 ** -int(self.price_decimals)
        if abs(float(self.tick_size) - expected_tick) > 1e-12:
            raise ValueError("tick_size must match price_decimals")
        for name in ("effective_from", "effective_to"):
            value = getattr(self, name)
            if value is None and name == "effective_to":
                continue
            _normalise_rule_date(value, name)
        if self.effective_to is not None and self.effective_from > self.effective_to:
            raise ValueError("effective_from must not be later than effective_to")
        for name in ("rule_id", "source_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.snapshot_hash, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.snapshot_hash
        ):
            raise ValueError("snapshot_hash must be a lowercase SHA-256 digest")

    def is_effective_on(self, trade_date: Union[str, date_type, datetime]) -> bool:
        value = _normalise_rule_date(trade_date, "trade_date")
        return self.effective_from <= value and (
            self.effective_to is None or value <= self.effective_to
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a stable JSON-compatible audit representation."""

        return {
            "instrument_kind": self.instrument_kind.value,
            "venue": self.venue.value,
            "limit_pct": float(self.limit_pct),
            "tick_size": float(self.tick_size),
            "price_decimals": int(self.price_decimals),
            "effective_from": self.effective_from,
            "effective_to": self.effective_to,
            "rule_id": self.rule_id,
            "rounding_policy": self.rounding_policy.value,
            "source_version": self.source_version,
            "snapshot_hash": self.snapshot_hash,
            "fund_snapshot_freeze_date": (
                LIMIT_BAND_FUND_SNAPSHOT_FREEZE_DATE
            ),
            "snapshot_coverage": LIMIT_BAND_SNAPSHOT_COVERAGE,
        }


def _normalise_rule_date(
    value: Union[str, date_type, datetime], name: str = "trade_date"
) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date_type):
        return value.strftime("%Y%m%d")
    if not isinstance(value, str):
        raise TypeError(f"{name} must be YYYYMMDD, date, or datetime")
    compact = value.strip().replace("-", "")
    if not re.fullmatch(r"\d{8}", compact):
        raise ValueError(f"{name} must use YYYYMMDD")
    try:
        datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid date") from exc
    return compact


class LimitBandService:
    """Resolve price limits and ticks from one effective-dated snapshot.

    BSE coverage starts when the exchange opened on 2021-11-15.  Earlier BJ
    symbols belonged to markets whose historical bands cannot be inferred
    from the current venue alone, so those dates fail closed rather than
    inventing a 10% rule.

    The 20% fund classification is an explicit snapshot, not a generative
    prefix rule.  ``RuleBook`` metadata has a rule ``effective_from`` field
    but no authoritative ``list_date``.  Consequently post-freeze issuance
    cannot be detected without risking false failures for existing ordinary
    10% funds; the residual fallback risk is disclosed in every serialized
    :class:`LimitBandRule`.
    """

    _BASE_EFFECTIVE_FROM = _LIMIT_BASE_EFFECTIVE_FROM
    _REFORM_DATE = _LIMIT_REFORM_DATE
    _PRE_REFORM_END = _LIMIT_PRE_REFORM_END
    _BSE_DATE = _LIMIT_BSE_DATE
    _STAR_BOARD_FROM = _LIMIT_STAR_BOARD_FROM
    _STAR_ETF_FROM = _LIMIT_STAR_ETF_FROM

    def __init__(self, rulebook: Optional[RuleBook] = None) -> None:
        resolved = DEFAULT_RULEBOOK if rulebook is None else rulebook
        if not isinstance(resolved, RuleBook):
            raise TypeError("rulebook must be a RuleBook")
        self.rulebook = resolved

    @staticmethod
    def _intersect_effective_dates(
        instrument: InstrumentRule,
        effective_from: str,
        effective_to: Optional[str],
    ) -> Tuple[str, Optional[str]]:
        start = max(
            value
            for value in (effective_from, instrument.effective_from)
            if value is not None
        )
        ends = [
            value
            for value in (effective_to, instrument.effective_to)
            if value is not None
        ]
        return start, min(ends) if ends else None

    def _build_rule(
        self,
        instrument: InstrumentRule,
        *,
        limit_pct: float,
        effective_from: str,
        effective_to: Optional[str],
        rule_id: str,
        rounding_policy: LimitBandRoundingPolicy,
    ) -> LimitBandRule:
        start, end = self._intersect_effective_dates(
            instrument, effective_from, effective_to
        )
        if end is not None and start > end:
            raise LimitBandRuleNotFoundError(
                f"instrument metadata and limit rule do not overlap for {rule_id}"
            )
        return LimitBandRule(
            instrument_kind=instrument.kind,
            venue=instrument.venue,
            limit_pct=limit_pct,
            tick_size=10.0 ** -instrument.price_decimals,
            price_decimals=instrument.price_decimals,
            effective_from=start,
            effective_to=end,
            rule_id=rule_id,
            rounding_policy=rounding_policy,
        )

    def get_rule(
        self, symbol: str, trade_date: Union[str, date_type, datetime]
    ) -> LimitBandRule:
        date_value = _normalise_rule_date(trade_date)
        instrument = self.rulebook.require_supported(
            symbol, engine=ExecutionEngine.CASH
        )
        code, _ = _symbol_parts(symbol)

        if instrument.kind is InstrumentKind.A_SHARE:
            if instrument.venue is Venue.SSE and code.startswith(("688", "689")):
                spec = (0.20, self._STAR_BOARD_FROM, None, "sse-star-a-share-20pct")
            elif (
                instrument.venue is Venue.SZSE
                and code.startswith(("300", "301", "302"))
            ):
                spec = (
                    (0.20, self._REFORM_DATE, None, "szse-gem-a-share-20pct")
                    if date_value >= self._REFORM_DATE
                    else (
                        0.10,
                        self._BASE_EFFECTIVE_FROM,
                        self._PRE_REFORM_END,
                        "szse-gem-a-share-10pct-pre-reform",
                    )
                )
            elif instrument.venue is Venue.BSE:
                if date_value < self._BSE_DATE:
                    raise LimitBandRuleNotFoundError(
                        f"no effective limit-band rule for {symbol!r} on "
                        f"{date_value}; BSE snapshot coverage begins "
                        f"{self._BSE_DATE}"
                    )
                spec = (0.30, self._BSE_DATE, None, "bse-a-share-30pct")
            else:
                spec = (
                    0.10,
                    self._BASE_EFFECTIVE_FROM,
                    None,
                    "ordinary-a-share-10pct",
                )
        else:
            if instrument.venue is Venue.SSE and code.startswith(("588", "589")):
                spec = (0.20, self._STAR_ETF_FROM, None, "sse-star-etf-20pct")
            elif code in _FUND_20PCT_EFFECTIVE_FROM:
                band_from = _FUND_20PCT_EFFECTIVE_FROM[code]
                if date_value >= band_from:
                    spec = (
                        0.20,
                        band_from,
                        None,
                        "metadata-or-observed-fund-20pct",
                    )
                elif band_from == self._REFORM_DATE:
                    spec = (
                        0.10,
                        self._BASE_EFFECTIVE_FROM,
                        self._PRE_REFORM_END,
                        "fund-10pct-pre-reform",
                    )
                else:
                    raise LimitBandRuleNotFoundError(
                        f"no effective limit-band rule for {symbol!r} on "
                        f"{date_value}; snapshot coverage begins {band_from}"
                    )
            else:
                spec = (
                    0.10,
                    self._BASE_EFFECTIVE_FROM,
                    None,
                    "ordinary-exchange-fund-10pct",
                )

        rule = self._build_rule(
            instrument,
            limit_pct=spec[0],
            effective_from=spec[1],
            effective_to=spec[2],
            rule_id=spec[3],
            rounding_policy=(
                LimitBandRoundingPolicy.TOWARD_REFERENCE
                if (
                    instrument.venue is Venue.BSE
                    and date_value >= self._BSE_DATE
                )
                else LimitBandRoundingPolicy.NEAREST_HALF_UP
            ),
        )
        if not rule.is_effective_on(date_value):
            raise LimitBandRuleNotFoundError(
                f"no effective limit-band rule for {symbol!r} on {date_value}"
            )
        return rule


DEFAULT_RULEBOOK = RuleBook()
DEFAULT_LIMIT_BAND_SERVICE = LimitBandService(DEFAULT_RULEBOOK)


def classify(
    symbol: str,
    metadata: Optional[MetadataEntry] = None,
    kind_overrides: Optional[Mapping[str, Union[InstrumentKind, str]]] = None,
) -> InstrumentKind:
    """Classify with the built-in rules (and optional one-shot overrides)."""

    book = DEFAULT_RULEBOOK if kind_overrides is None else RuleBook(kind_overrides=kind_overrides)
    return book.classify(symbol, metadata=metadata)


def get_rule(
    symbol: str,
    metadata: Optional[MetadataEntry] = None,
    kind_overrides: Optional[Mapping[str, Union[InstrumentKind, str]]] = None,
) -> InstrumentRule:
    """Return the immutable rule for ``symbol``."""

    book = DEFAULT_RULEBOOK if kind_overrides is None else RuleBook(kind_overrides=kind_overrides)
    return book.get_rule(symbol, metadata=metadata)


def require_supported(
    symbol: str,
    engine: Optional[Union[ExecutionEngine, str]] = None,
    metadata: Optional[MetadataEntry] = None,
    kind_overrides: Optional[Mapping[str, Union[InstrumentKind, str]]] = None,
) -> InstrumentRule:
    """Return a supported rule or raise :class:`UnsupportedInstrumentError`."""

    book = DEFAULT_RULEBOOK if kind_overrides is None else RuleBook(kind_overrides=kind_overrides)
    return book.require_supported(symbol, engine=engine, metadata=metadata)


__all__ = [
    "DEFAULT_LIMIT_BAND_SERVICE",
    "DEFAULT_RULEBOOK",
    "ExecutionEngine",
    "InstrumentKind",
    "InstrumentRule",
    "LIMIT_BAND_FUND_SNAPSHOT_FREEZE_DATE",
    "LIMIT_BAND_SNAPSHOT_HASH",
    "LIMIT_BAND_SNAPSHOT_COVERAGE",
    "LIMIT_BAND_SOURCE_VERSION",
    "LimitBandRoundingPolicy",
    "LimitBandRule",
    "LimitBandRuleNotFoundError",
    "LimitBandService",
    "RuleBook",
    "UnsupportedInstrumentError",
    "Venue",
    "classify",
    "get_rule",
    "require_supported",
]
