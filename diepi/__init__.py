"""dieΠ public package metadata."""

__version__ = "0.1.1"
__brand__ = "dieΠ"

INTEGRATION_API_VERSION = 1
INTEGRATION_CAPABILITIES = frozenset(
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

__all__ = [
    "INTEGRATION_API_VERSION",
    "INTEGRATION_CAPABILITIES",
    "__brand__",
    "__version__",
]
