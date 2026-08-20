"""Public orchestration-adapter compatibility contract."""

import pytest

import diepi
import diepi.integration as integration
from diepi.artifacts import ArtifactStore
from diepi.backtest.cli.runner import run_backtest
from diepi.backtest.data.source_evidence import require_complete_direct_sources


REQUIRED_ADAPTER_CAPABILITIES = frozenset(
    {
        "diepi.artifact_binding.v1",
        "diepi.backtest.stop_check.v1",
        "diepi.calendar_identity.v1",
        "diepi.direct_source_fingerprints.v1",
        "diepi.doctor.v1",
        "diepi.local_data_validation.v1",
        "diepi.synthetic_demo.v1",
    }
)


def test_public_integration_facade_has_one_versioned_closed_contract():
    assert diepi.INTEGRATION_API_VERSION == 1
    assert integration.INTEGRATION_API_VERSION == 1
    assert diepi.INTEGRATION_CAPABILITIES == REQUIRED_ADAPTER_CAPABILITIES
    assert integration.INTEGRATION_CAPABILITIES == REQUIRED_ADAPTER_CAPABILITIES
    integration.require_integration_contract(
        api_version=1,
        capabilities=REQUIRED_ADAPTER_CAPABILITIES,
    )


def test_public_integration_facade_preserves_canonical_object_identity():
    from diepi.demo import generate_synthetic_demo

    assert integration.ArtifactStore is ArtifactStore
    assert integration.run_backtest is run_backtest
    assert integration.generate_synthetic_demo is generate_synthetic_demo
    assert (
        integration.require_complete_direct_sources
        is require_complete_direct_sources
    )


@pytest.mark.parametrize(
    ("api_version", "capabilities", "match"),
    [
        (0, REQUIRED_ADAPTER_CAPABILITIES, "API mismatch"),
        (2, REQUIRED_ADAPTER_CAPABILITIES, "API mismatch"),
        (1, {"diepi.future_contract.v99"}, "capabilities are missing"),
        (1, {""}, "non-empty strings"),
    ],
)
def test_integration_handshake_fails_closed(api_version, capabilities, match):
    with pytest.raises(integration.IntegrationCompatibilityError, match=match):
        integration.require_integration_contract(
            api_version=api_version,
            capabilities=capabilities,
        )
