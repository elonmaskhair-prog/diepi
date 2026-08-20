"""Stable, versioned integration facade for local orchestration adapters.

Adapters should import this module instead of depending on the internal
``diepi.backtest.*`` package layout.  The capability handshake is intentionally
strict: an adapter can reject an older or incomplete diePi before accepting a
job, while the underlying APIs retain their existing Python object identity.
"""

from __future__ import annotations

from typing import Iterable

from . import INTEGRATION_API_VERSION, INTEGRATION_CAPABILITIES
from .artifacts import (
    ArtifactStore,
    EngineKind,
    RunOutcome,
    RunProvenance,
    SourceFingerprint,
)
from .backtest.cli.runner import run_backtest
from .backtest.data import DataProvider, TradeCalendarIdentity, validate_local_data
from .backtest.data.source_evidence import (
    TRADE_CALENDAR_SOURCE_KIND,
    collect_market_data_fingerprints,
    collect_trade_calendar_fingerprint,
    require_complete_direct_sources,
    trade_calendar_fingerprint,
)
from .commands.doctor import run_doctor
from .demo import (
    DEMO_DATASET_ID,
    DEMO_END_DATE,
    DEMO_GENERATOR_VERSION,
    DEMO_START_DATE,
    DEMO_SYMBOL,
    DEMO_VALIDATION_FILENAME,
    DemoWorkspace,
    generate_synthetic_demo,
)


class IntegrationCompatibilityError(RuntimeError):
    """Raised when an adapter requests an unsupported integration contract."""


def require_integration_contract(
    *,
    api_version: int,
    capabilities: Iterable[str],
) -> None:
    """Fail closed unless this installation provides an exact public contract."""

    if type(api_version) is not int or api_version != INTEGRATION_API_VERSION:
        raise IntegrationCompatibilityError(
            "diePi integration API mismatch: "
            f"required={api_version!r}, installed={INTEGRATION_API_VERSION}"
        )
    try:
        required = frozenset(capabilities)
    except TypeError as exc:
        raise IntegrationCompatibilityError(
            "diePi integration capabilities must be an iterable of strings"
        ) from exc
    if any(type(value) is not str or not value for value in required):
        raise IntegrationCompatibilityError(
            "diePi integration capabilities must be non-empty strings"
        )
    missing = sorted(required - INTEGRATION_CAPABILITIES)
    if missing:
        raise IntegrationCompatibilityError(
            "diePi integration capabilities are missing: " + ", ".join(missing)
        )


__all__ = [
    "ArtifactStore",
    "DataProvider",
    "DEMO_DATASET_ID",
    "DEMO_END_DATE",
    "DEMO_GENERATOR_VERSION",
    "DEMO_START_DATE",
    "DEMO_SYMBOL",
    "DEMO_VALIDATION_FILENAME",
    "DemoWorkspace",
    "EngineKind",
    "INTEGRATION_API_VERSION",
    "INTEGRATION_CAPABILITIES",
    "IntegrationCompatibilityError",
    "RunOutcome",
    "RunProvenance",
    "SourceFingerprint",
    "TRADE_CALENDAR_SOURCE_KIND",
    "TradeCalendarIdentity",
    "collect_market_data_fingerprints",
    "collect_trade_calendar_fingerprint",
    "generate_synthetic_demo",
    "require_complete_direct_sources",
    "require_integration_contract",
    "run_backtest",
    "run_doctor",
    "trade_calendar_fingerprint",
    "validate_local_data",
]
