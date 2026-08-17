"""Deterministic, explicitly synthetic onboarding assets."""

from .generator import (
    DEMO_DATASET_ID,
    DEMO_END_DATE,
    DEMO_START_DATE,
    DEMO_SYMBOL,
    DemoWorkspace,
    generate_synthetic_demo,
)

__all__ = [
    "DEMO_DATASET_ID",
    "DEMO_END_DATE",
    "DEMO_START_DATE",
    "DEMO_SYMBOL",
    "DemoWorkspace",
    "generate_synthetic_demo",
]
