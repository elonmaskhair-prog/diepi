"""Pin public and durable identifiers to the diepi brand."""

import json
import pandas as pd
import pytest

from diepi.backtest.broker.events import (
    EXECUTION_EVENT_JOURNAL_SCHEMA,
    EXECUTION_EVENT_SCHEMA,
)
from diepi.backtest.broker.replay import (
    CASH_AUDIT_BUNDLE_SCHEMA,
    CASH_REPLAY_SEED_SCHEMA,
)
from diepi.backtest.broker.target import (
    TARGET_ACHIEVEMENT_SCHEMA,
    TARGET_INTENT_SCHEMA,
)
from diepi.backtest.broker.target_execution import TARGET_EXECUTION_BUNDLE_SCHEMA
from diepi.backtest.comparison.models import (
    COMPARISON_BUNDLE_SCHEMA,
    COMPARISON_SCOPE_SCHEMA,
)
from diepi.backtest.data.contract import (
    ADJUSTMENT_FACTOR_IDENTITY_SCHEMA,
    AdjustmentFactorIdentity,
    AdjustmentFactorRunIdentity,
    Frequency,
    PriceSpace,
    adjustment_factor_identity_artifact,
)
from diepi.backtest.data.result_storage import ResultStorage
from diepi.backtest.engine import minute_resampler, parallel_runner


def test_all_durable_schema_and_wire_names_are_pinned_to_diepi():
    identity = AdjustmentFactorIdentity(
        symbol="000001.SZ",
        frequency=Frequency.DAILY,
        strategy_price_space=PriceSpace.HFQ,
        execution_price_space=PriceSpace.RAW,
        scope_start="20240102",
        scope_end="20240102",
        scope_trade_days=1,
        scope_trade_dates_sha256="0" * 64,
        scope_observation_keys_sha256="1" * 64,
        validated_close_rows=1,
        source="brand-contract",
        base_date="20240102",
        base_factor=1.0,
        factor_values_sha256="2" * 64,
    )
    identifiers = {
        "adjustment_factor_identity": ADJUSTMENT_FACTOR_IDENTITY_SCHEMA,
        "adjustment_factor_identity_artifact": json.loads(
            adjustment_factor_identity_artifact((identity,))
        )["schema"],
        "adjustment_factor_run_identity": (
            AdjustmentFactorRunIdentity.__dataclass_fields__["schema"].default
        ),
        "cash_audit_artifacts": ResultStorage.CASH_AUDIT_ARTIFACT_SCHEMA,
        "cash_audit_bundle": CASH_AUDIT_BUNDLE_SCHEMA,
        "cash_replay_seed": CASH_REPLAY_SEED_SCHEMA,
        "comparison_bundle": COMPARISON_BUNDLE_SCHEMA,
        "comparison_scope": COMPARISON_SCOPE_SCHEMA,
        "execution_event": EXECUTION_EVENT_SCHEMA,
        "execution_event_journal": EXECUTION_EVENT_JOURNAL_SCHEMA,
        "parallel_backtest_result": (
            parallel_runner._PARALLEL_RESULT_WIRE_SCHEMA
        ),
        "target_achievement": TARGET_ACHIEVEMENT_SCHEMA,
        "target_execution_bundle": TARGET_EXECUTION_BUNDLE_SCHEMA,
        "target_intent": TARGET_INTENT_SCHEMA,
    }

    assert identifiers == {
        name: f"diepi.{name}" for name in identifiers
    }


def test_internal_names_and_staging_prefix_are_pinned_to_diepi():
    assert minute_resampler._INTERNAL_TIME == "__diepi_trade_time__"
    assert minute_resampler._INTERNAL_GROUP == "__diepi_session_group__"
    assert ResultStorage.STAGING_PREFIX == ".diepi-staging-"


@pytest.mark.parametrize(
    "reserved_name",
    [
        "__diepi_trade_time__",
        "__diepi_session_group__",
    ],
)
def test_minute_resampler_rejects_reserved_brand_columns(reserved_name):
    frame = pd.DataFrame(
        {
            "trade_time": ["2024-01-02 09:31:00"],
            "open": [10.0],
            "high": [10.1],
            "low": [9.9],
            "close": [10.0],
            reserved_name: ["caller-owned"],
        }
    )

    with pytest.raises(ValueError, match="reserved resampler column"):
        minute_resampler.resample_minute_data(
            frame,
            1,
            symbol="000001.SZ",
        )
