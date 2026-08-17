"""Stable result contract for delayed target-weight execution.

The broker keeps decision-time :class:`TargetIntent` objects and terminal
:class:`TargetAchievement` objects in separate append-only snapshots.  This
module joins those snapshots only at the result boundary.  It deliberately
does not perform sizing, matching, or outcome inference.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, ClassVar, Tuple

from .target import TargetAchievement, TargetIntent


TARGET_EXECUTION_BUNDLE_SCHEMA = "diepi.target_execution_bundle"
TARGET_EXECUTION_BUNDLE_SCHEMA_VERSION = 1

_TARGET_EXECUTION_BUNDLE_KEYS = frozenset(
    {
        "achievements",
        "complete",
        "intents",
        "pending_intent_ids",
        "schema",
        "schema_version",
    }
)


def _canonical_json(value: dict) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


@dataclass(frozen=True)
class TargetExecutionBundle:
    """One immutable, versioned target decision/outcome result.

    Intents are stored in canonical decision order.  Achievements are a unique
    subset stored in the same relative intent-id order; the remaining intent
    IDs are explicitly exposed as pending.  This lets a non-success result
    preserve decisions committed before an independent engine failure.  A
    SUCCESS result must separately require ``complete``.

    ``from_snapshots`` performs the only allowed ordering normalization;
    direct construction and deserialization reject noncanonical order instead
    of silently changing evidence.

    Current engines publish an explicit empty bundle when no target operation
    was requested.  ``None`` is reserved by result containers for legacy
    objects and artifacts that predate this contract.
    """

    SCHEMA: ClassVar[str] = TARGET_EXECUTION_BUNDLE_SCHEMA
    SCHEMA_VERSION: ClassVar[int] = TARGET_EXECUTION_BUNDLE_SCHEMA_VERSION

    intents: Tuple[TargetIntent, ...] = ()
    achievements: Tuple[TargetAchievement, ...] = ()
    schema_version: int = TARGET_EXECUTION_BUNDLE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.intents) is not tuple:
            raise TypeError("intents must be exactly tuple")
        if type(self.achievements) is not tuple:
            raise TypeError("achievements must be exactly tuple")
        if type(self.schema_version) is not int:
            raise TypeError("schema_version must be exactly int")
        if self.schema_version != self.SCHEMA_VERSION:
            raise ValueError("unsupported target execution bundle schema_version")

        for index, intent in enumerate(self.intents):
            if type(intent) is not TargetIntent:
                raise TypeError(
                    f"intents[{index}] must be exactly TargetIntent"
                )
        canonical_intents = tuple(
            sorted(self.intents, key=lambda intent: intent.ordering_key)
        )
        if canonical_intents != self.intents:
            raise ValueError("intents must use canonical decision order")

        intent_ids = tuple(intent.intent_id for intent in self.intents)
        if len(set(intent_ids)) != len(intent_ids):
            raise ValueError("intent_id must be unique within the bundle")

        for index, achievement in enumerate(self.achievements):
            if type(achievement) is not TargetAchievement:
                raise TypeError(
                    f"achievements[{index}] must be exactly TargetAchievement"
                )
        achievement_ids = tuple(
            achievement.intent_id for achievement in self.achievements
        )
        if len(set(achievement_ids)) != len(achievement_ids):
            raise ValueError("achievement intent_id must be unique within the bundle")
        intent_by_id = {intent.intent_id: intent for intent in self.intents}
        orphan_ids = sorted(set(achievement_ids) - set(intent_ids))
        if orphan_ids:
            raise ValueError(
                f"achievement intent_id must reference an intent: {orphan_ids}"
            )
        expected_achievement_ids = tuple(
            intent_id for intent_id in intent_ids
            if intent_id in set(achievement_ids)
        )
        if achievement_ids != expected_achievement_ids:
            raise ValueError(
                "achievements must use canonical relative intent order"
            )

        seen_order_ids = set()
        for achievement in self.achievements:
            intent = intent_by_id[achievement.intent_id]
            if achievement.batch_id != intent.batch_id:
                raise ValueError("achievement batch_id must match its intent")
            if achievement.symbol != intent.symbol:
                raise ValueError("achievement symbol must match its intent")
            if achievement.target_weight != intent.target_weight:
                raise ValueError("achievement target_weight must match its intent")
            if achievement.trade_date != intent.expire_date:
                raise ValueError("achievement trade_date must match intent expire_date")
            for order_id in achievement.order_ids:
                if order_id in seen_order_ids:
                    raise ValueError(
                        "order_id must not be shared by target achievements"
                    )
                seen_order_ids.add(order_id)

    @property
    def pending_intent_ids(self) -> Tuple[str, ...]:
        achieved = {
            achievement.intent_id for achievement in self.achievements
        }
        return tuple(
            intent.intent_id for intent in self.intents
            if intent.intent_id not in achieved
        )

    @property
    def complete(self) -> bool:
        return not self.pending_intent_ids

    @classmethod
    def empty(cls) -> "TargetExecutionBundle":
        """Return explicit evidence that a run contained no target intents."""

        return cls()

    @classmethod
    def from_snapshots(
        cls,
        intents: Tuple[TargetIntent, ...],
        achievements: Tuple[TargetAchievement, ...],
    ) -> "TargetExecutionBundle":
        """Canonicalize immutable broker snapshots and validate all links."""

        if type(intents) is not tuple:
            raise TypeError("intents snapshot must be exactly tuple")
        if type(achievements) is not tuple:
            raise TypeError("achievements snapshot must be exactly tuple")
        for index, intent in enumerate(intents):
            if type(intent) is not TargetIntent:
                raise TypeError(
                    f"intents snapshot[{index}] must be exactly TargetIntent"
                )
        for index, achievement in enumerate(achievements):
            if type(achievement) is not TargetAchievement:
                raise TypeError(
                    "achievements snapshot[{}] must be exactly "
                    "TargetAchievement".format(index)
                )

        canonical_intents = tuple(
            sorted(intents, key=lambda intent: intent.ordering_key)
        )
        snapshot_intent_ids = tuple(intent.intent_id for intent in intents)
        if len(set(snapshot_intent_ids)) != len(snapshot_intent_ids):
            raise ValueError("intent_id must be unique within the snapshot")
        achievement_by_intent = {}
        for achievement in achievements:
            if achievement.intent_id in achievement_by_intent:
                raise ValueError(
                    "achievement intent_id must be unique within the snapshot"
                )
            achievement_by_intent[achievement.intent_id] = achievement

        orphan = sorted(
            set(achievement_by_intent) - set(snapshot_intent_ids)
        )
        if orphan:
            raise ValueError(
                "target snapshots contain orphan achievements: "
                f"orphan_achievements={orphan}"
            )

        canonical_achievements = tuple(
            achievement_by_intent[intent.intent_id]
            for intent in canonical_intents
            if intent.intent_id in achievement_by_intent
        )
        return cls(
            intents=canonical_intents,
            achievements=canonical_achievements,
        )

    def to_dict(self) -> dict:
        return {
            "achievements": [
                achievement.to_dict() for achievement in self.achievements
            ],
            "complete": self.complete,
            "intents": [intent.to_dict() for intent in self.intents],
            "pending_intent_ids": list(self.pending_intent_ids),
            "schema": self.SCHEMA,
            "schema_version": self.schema_version,
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> "TargetExecutionBundle":
        if type(value) is not dict:
            raise TypeError("TargetExecutionBundle payload must be exactly dict")
        actual_keys = frozenset(value)
        if actual_keys != _TARGET_EXECUTION_BUNDLE_KEYS:
            missing = sorted(_TARGET_EXECUTION_BUNDLE_KEYS - actual_keys)
            extra = sorted(actual_keys - _TARGET_EXECUTION_BUNDLE_KEYS)
            raise ValueError(
                "TargetExecutionBundle keys mismatch: "
                f"missing={missing}, extra={extra}"
            )
        if type(value["schema"]) is not str:
            raise TypeError("target execution bundle schema must be exactly str")
        if value["schema"] != cls.SCHEMA:
            raise ValueError("unsupported target execution bundle schema")
        if type(value["schema_version"]) is not int:
            raise TypeError(
                "target execution bundle schema_version must be exactly int"
            )
        if type(value["intents"]) is not list:
            raise TypeError("target execution bundle intents must be exactly list")
        if type(value["achievements"]) is not list:
            raise TypeError(
                "target execution bundle achievements must be exactly list"
            )
        if type(value["complete"]) is not bool:
            raise TypeError("target execution bundle complete must be exactly bool")
        if type(value["pending_intent_ids"]) is not list:
            raise TypeError(
                "target execution bundle pending_intent_ids must be exactly list"
            )
        if any(type(intent_id) is not str for intent_id in value["pending_intent_ids"]):
            raise TypeError(
                "target execution bundle pending_intent_ids must contain strings"
            )

        restored = cls(
            intents=tuple(
                TargetIntent.from_dict(payload)
                for payload in value["intents"]
            ),
            achievements=tuple(
                TargetAchievement.from_dict(payload)
                for payload in value["achievements"]
            ),
            schema_version=value["schema_version"],
        )
        if restored.to_dict() != value:
            raise ValueError(
                "target execution bundle payload is not canonical"
            )
        return restored


__all__ = [
    "TARGET_EXECUTION_BUNDLE_SCHEMA",
    "TARGET_EXECUTION_BUNDLE_SCHEMA_VERSION",
    "TargetExecutionBundle",
]
