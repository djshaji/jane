"""Bounded, offline fixture replay, not the future persistent coordinator."""

import json
from importlib.resources import files
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from jane.actions.mock import MockInspector, ReadOnlyTool
from jane.cognition.scripted import CognitiveAdapter, Context, ScriptedAdapter
from jane.core.clock import FakeClock
from jane.core.contracts import (
    DECISION,
    Contract,
    Decision,
    DecisionEvent,
    Event,
    Fail,
    Finish,
    Identifier,
    Message,
    Observation,
    ObservationEvent,
    Respond,
    ResponseEvent,
    ToolCall,
    UserEvent,
    Wait,
)


class Step(Contract):
    advance_seconds: Annotated[float, Field(ge=0, le=86400)]
    decision: Decision


class Expected(Contract):
    terminal: Literal["finished", "waiting", "failed"]
    response: str
    inspected_objects: tuple[Identifier, ...]


class Scenario(Contract):
    schema_version: Literal[1]
    start: AwareDatetime
    request_id: Identifier
    generation: Annotated[int, Field(ge=0)]
    input: UserEvent
    observations: tuple[Observation, ...]
    steps: Annotated[tuple[Step, ...], Field(min_length=1, max_length=32)]
    expected: Expected

    @model_validator(mode="after")
    def initial_time(self) -> Self:
        if self.input.received_at > self.start:
            raise ValueError("initial event is received after scenario start")
        if len({o.object_id for o in self.observations}) != len(self.observations):
            raise ValueError("duplicate object IDs")
        return self


class ReplayError(ValueError):
    """A valid contract violates replay policy or expected outcomes."""


class Result(Contract):
    terminal: Literal["finished", "waiting", "failed"]
    response: str
    inspected_objects: tuple[Identifier, ...]
    events: tuple[Event, ...]
    elapsed_seconds: float

    def render(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def demo_scenario() -> Scenario:
    return Scenario.model_validate_json(files("jane.scenarios").joinpath("demo.json").read_text())


def run(
    scenario: Scenario,
    adapter: CognitiveAdapter | None = None,
    tool: ReadOnlyTool | None = None,
) -> Result:
    clock = FakeClock(scenario.start)
    engine = (
        adapter
        if adapter is not None
        else ScriptedAdapter(tuple(step.decision for step in scenario.steps))
    )
    inspector = tool if tool is not None else MockInspector(scenario.observations)
    events: list[Event] = [scenario.input]
    inspected: list[str] = []
    response = ""
    terminal: Literal["finished", "waiting", "failed"] | None = None
    decision_ids: set[str] = set()

    for index, step in enumerate(scenario.steps, start=1):
        clock.advance(step.advance_seconds)
        context = Context(
            request_id=scenario.request_id,
            task_id=scenario.input.task_id,
            generation=scenario.generation,
            snapshot_id=f"snapshot-{index:03}",
            events=tuple(events),
        )
        decision = DECISION.validate_json(engine.decide(context).model_dump_json())
        if any(
            getattr(decision, field) != getattr(context, field)
            for field in ("request_id", "task_id", "generation", "snapshot_id")
        ):
            raise ReplayError("decision does not match the active context")
        known = {event.event_id for event in events}
        if not set(decision.evidence_ids) <= known:
            raise ReplayError("decision references unknown or future evidence")
        if decision.decision_id in decision_ids:
            raise ReplayError("duplicate decision ID")
        decision_ids.add(decision.decision_id)
        now = clock.now()
        decision_event = DecisionEvent(
            event_id=f"replay-decision-{index:03}",
            source_id="scripted",
            occurred_start=now,
            occurred_end=now,
            received_at=now,
            task_id=context.task_id,
            correlation_id=scenario.input.correlation_id,
            causation_event_id=events[-1].event_id,
            kind="cognition.decision",
            provenance_class="model_proposal",
            payload=decision,
        )
        if decision_event.event_id in known:
            raise ReplayError("generated event ID collision")
        events.append(decision_event)
        if isinstance(decision, ToolCall):
            observation = inspector.inspect(decision.arguments)
            if observation.object_id != decision.arguments.object_id:
                raise ReplayError("tool returned an observation for a different object")
            events.append(
                ObservationEvent(
                    event_id=f"replay-observation-{index:03}",
                    source_id="mock.inspect",
                    occurred_start=now,
                    occurred_end=now,
                    received_at=now,
                    task_id=context.task_id,
                    correlation_id=scenario.input.correlation_id,
                    causation_event_id=decision_event.event_id,
                    kind="tool.observation",
                    provenance_class="tool_result",
                    payload=observation,
                )
            )
            inspected.append(observation.object_id)
        elif isinstance(decision, Respond):
            response = decision.text
            events.append(
                ResponseEvent(
                    event_id=f"replay-response-{index:03}",
                    source_id="scripted",
                    occurred_start=now,
                    occurred_end=now,
                    received_at=now,
                    task_id=context.task_id,
                    correlation_id=scenario.input.correlation_id,
                    causation_event_id=decision_event.event_id,
                    kind="communication.response",
                    provenance_class="model_response",
                    payload=Message(text=response),
                )
            )
        elif isinstance(decision, Finish):
            evidence = [e for e in events if e.event_id in decision.evidence_ids]
            if not any(isinstance(e, ObservationEvent) for e in evidence) or not response:
                raise ReplayError("demo completion requires an observation and a response")
            terminal = "finished"
        elif isinstance(decision, Wait):
            terminal = "waiting"
        elif isinstance(decision, Fail):
            terminal = "failed"
        else:
            raise ReplayError("observation capture is not implemented in the offline runner")
        if terminal is not None:
            if index != len(scenario.steps):
                raise ReplayError("script contains steps after a terminal decision")
            break
    if terminal is None:
        raise ReplayError("decision budget exhausted without terminal decision")
    result = Result(
        terminal=terminal,
        response=response,
        inspected_objects=tuple(inspected),
        events=tuple(events),
        elapsed_seconds=clock.monotonic(),
    )
    actual = Expected(terminal=terminal, response=response, inspected_objects=tuple(inspected))
    if actual != scenario.expected:
        raise ReplayError("replay did not match the fixture's expected outcomes")
    if len({e.event_id for e in events}) != len(events):
        raise ReplayError("duplicate event IDs in trace")
    return result
