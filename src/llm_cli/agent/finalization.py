"""Durable state for a publication-aware final response.

The model may prepare an answer before the runner knows whether edits were
published, retained for review, or rejected as stale.  A mutating task therefore
keeps that text as a draft until the trusted runner supplies settlement facts.
This module defines the small JSON contract shared by the driver and storage
layer; it deliberately contains no provider-native conversation data.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from llm_cli.coordination.scopes import normalize_changed_path

_PUBLICATION_OUTCOMES = frozenset(
    {
        "not_applicable",
        "no_changes",
        "held_for_review",
        "published",
        "diverged",
        "operator_attention",
        "uncertain",
    }
)
_VERIFICATION_OUTCOMES = frozenset(
    {"not_applicable", "not_run", "passed", "failed", "stale", "interrupted"}
)
_COMPLETION_OUTCOMES = frozenset(
    {"completed", "blocked", "partial", "cancelled", "failed"}
)
_FINALIZATION_STATUSES = frozenset(
    {"prepared", "pending", "in_flight", "completed", "interrupted"}
)
_MAX_SETTLEMENT_PATHS = 5_000


@dataclass(frozen=True, slots=True)
class SettlementFacts:
    """Runner-authored facts that a final response is allowed to describe."""

    publication: str
    verification: str
    completion: str
    changed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.publication not in _PUBLICATION_OUTCOMES:
            raise ValueError("settlement publication outcome is invalid")
        if self.verification not in _VERIFICATION_OUTCOMES:
            raise ValueError("settlement verification outcome is invalid")
        if self.completion not in _COMPLETION_OUTCOMES:
            raise ValueError("settlement completion outcome is invalid")
        if len(self.changed_paths) > _MAX_SETTLEMENT_PATHS:
            raise ValueError("settlement contains too many changed paths")
        normalized = tuple(normalize_changed_path(path) for path in self.changed_paths)
        if normalized != self.changed_paths or len(set(normalized)) != len(normalized):
            raise ValueError("settlement changed paths must be unique and normalized")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "publication": self.publication,
            "verification": self.verification,
            "completion": self.completion,
            "changed_paths": list(self.changed_paths),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> SettlementFacts:
        if set(value) != {
            "version",
            "publication",
            "verification",
            "completion",
            "changed_paths",
        } or value.get("version") != 1:
            raise ValueError("settlement facts have an unsupported shape or version")
        paths = value.get("changed_paths")
        if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)) or any(
            not isinstance(path, str) for path in paths
        ):
            raise ValueError("settlement changed paths are invalid")
        normalized_paths = tuple(path for path in paths if isinstance(path, str))
        publication = value.get("publication")
        verification = value.get("verification")
        completion = value.get("completion")
        if not all(
            isinstance(item, str) for item in (publication, verification, completion)
        ):
            raise ValueError("settlement outcomes must be strings")
        assert isinstance(publication, str)
        assert isinstance(verification, str)
        assert isinstance(completion, str)
        return cls(
            publication=publication,
            verification=verification,
            completion=completion,
            changed_paths=normalized_paths,
        )


@dataclass(frozen=True, slots=True)
class FinalizationState:
    """One monotonic, at-most-once final-response state machine."""

    status: str
    attempts: int
    facts: SettlementFacts | None = None

    def __post_init__(self) -> None:
        if self.status not in _FINALIZATION_STATUSES:
            raise ValueError("finalization status is invalid")
        if type(self.attempts) is not int or self.attempts not in {0, 1}:
            raise ValueError("finalization permits at most one automatic attempt")
        if self.status == "prepared" and (
            self.attempts != 0 or self.facts is not None
        ):
            raise ValueError(
                "prepared finalization cannot have attempts or settlement facts"
            )
        if self.status == "pending" and (
            self.attempts != 0 or self.facts is None
        ):
            raise ValueError("pending finalization requires trusted settlement facts")
        if self.status == "in_flight" and (self.attempts != 1 or self.facts is None):
            raise ValueError("in-flight finalization requires one attempt and facts")
        if self.status == "completed" and self.facts is None:
            raise ValueError("completed finalization requires settlement facts")
        if self.status == "interrupted" and (
            self.attempts != 1 or self.facts is None
        ):
            raise ValueError("interrupted finalization requires a consumed attempt")

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "status": self.status,
            "attempts": self.attempts,
            "facts": self.facts.to_dict() if self.facts is not None else None,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> FinalizationState:
        if set(value) != {"version", "status", "attempts", "facts"} or value.get(
            "version"
        ) != 1:
            raise ValueError("finalization state has an unsupported shape or version")
        status = value.get("status")
        attempts = value.get("attempts")
        facts_value = value.get("facts")
        if not isinstance(status, str):
            raise ValueError("finalization status must be a string")
        if type(attempts) is not int:
            raise ValueError("finalization attempts must be an integer")
        if facts_value is not None and not isinstance(facts_value, Mapping):
            raise ValueError("finalization settlement facts are invalid")
        return cls(
            status=status,
            attempts=attempts,
            facts=(
                SettlementFacts.from_mapping(facts_value)
                if isinstance(facts_value, Mapping)
                else None
            ),
        )


def checkpoint_finalization(
    checkpoint: Mapping[str, object],
) -> FinalizationState | None:
    """Validate and return the optional finalization envelope in a checkpoint."""

    raw = checkpoint.get("finalization")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("checkpoint finalization state is invalid")
    state = FinalizationState.from_mapping(raw)
    phase = checkpoint.get("phase")
    accepted = checkpoint.get("accepted_answer")
    if state.status == "prepared":
        if phase != "awaiting_settlement" or accepted is not None:
            raise ValueError("prepared finalization requires an unaccepted draft")
    elif state.status == "pending":
        if phase != "finalization_pending" or accepted is not None:
            raise ValueError("pending finalization requires trusted settlement facts")
    elif state.status == "in_flight":
        if phase != "finalizing" or accepted is not None:
            raise ValueError("in-flight finalization cannot contain an accepted answer")
    elif phase != "finished" or accepted is None:
        raise ValueError("terminal finalization requires a finished accepted answer")
    return state


def validate_finalization_transition(
    previous: Mapping[str, object] | None,
    current: Mapping[str, object],
) -> None:
    """Reject retries, rewinds, or changed trusted facts across checkpoint saves."""

    prior = checkpoint_finalization(previous) if previous is not None else None
    next_state = checkpoint_finalization(current)
    if prior is None:
        if next_state is not None and next_state.status != "prepared":
            raise ValueError("finalization must begin in the prepared state")
        return
    if next_state is None:
        raise ValueError("a checkpoint cannot discard finalization state")
    allowed = {
        "prepared": {"prepared", "pending", "completed"},
        "pending": {"pending", "in_flight"},
        "in_flight": {"in_flight", "completed", "interrupted"},
        "completed": {"completed"},
        "interrupted": {"interrupted"},
    }
    if next_state.status not in allowed[prior.status]:
        raise ValueError("finalization state cannot move backwards")
    if next_state.attempts < prior.attempts:
        raise ValueError("finalization attempt accounting cannot be reset")
    if prior.facts is not None and next_state.facts != prior.facts:
        raise ValueError("trusted settlement facts are immutable")


__all__ = [
    "FinalizationState",
    "SettlementFacts",
    "checkpoint_finalization",
    "validate_finalization_transition",
]
