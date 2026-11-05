"""A scripted adapter implementing the same decision boundary as future providers."""

from typing import Protocol

from jane.core.contracts import DECISION, Contract, Decision, Event, Identifier, NonNegative


class Context(Contract):
    request_id: Identifier
    task_id: Identifier
    generation: NonNegative
    snapshot_id: Identifier
    events: tuple[Event, ...]


class CognitiveAdapter(Protocol):
    def decide(self, context: Context) -> Decision: ...


class ScriptedAdapter:
    def __init__(self, decisions: tuple[Decision, ...]) -> None:
        # Copy through validation; do not trust unchecked model_construct objects.
        self._script = tuple(DECISION.validate_json(d.model_dump_json()) for d in decisions)
        self._position = 0

    def decide(self, context: Context) -> Decision:
        if self._position >= len(self._script):
            raise ValueError("script exhausted before a terminal decision")
        decision = self._script[self._position]
        self._position += 1
        return decision

    @property
    def remaining(self) -> int:
        return len(self._script) - self._position
