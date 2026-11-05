"""Canonical strict wire contracts; persistence and runtime policies come later."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator

Identifier = Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z0-9_.:-]+$")]
Text = Annotated[str, Field(min_length=1)]
NonNegative = Annotated[int, Field(ge=0)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class Message(Contract):
    text: Text


class Observation(Contract):
    object_id: Identifier
    description: Text


class DecisionBase(Contract):
    decision_id: Identifier
    request_id: Identifier
    task_id: Identifier
    generation: NonNegative
    snapshot_id: Identifier
    summary: Text
    evidence_ids: tuple[Identifier, ...]


class Respond(DecisionBase):
    type: Literal["respond"]
    text: Text


class ToolCall(DecisionBase):
    type: Literal["call_tool"]
    # Initial registry deliberately contains only this read-only tool.
    tool: Literal["mock.inspect"]
    arguments: "InspectArguments"


class InspectArguments(Contract):
    object_id: Identifier


class ObservationRequest(Contract):
    source_id: Identifier
    modality: Literal["text", "image", "audio"]
    max_age_ms: NonNegative
    purpose: Text


class RequestObservation(DecisionBase):
    type: Literal["request_observation"]
    observation: ObservationRequest


class Wait(DecisionBase):
    type: Literal["wait"]
    review_after_ms: Annotated[int, Field(gt=0)]


class Finish(DecisionBase):
    type: Literal["finish"]
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]


class Fail(DecisionBase):
    type: Literal["fail"]
    category: Literal["blocked", "budget_exhausted", "invalid_evidence", "unavailable"]


Decision = Annotated[
    Respond | ToolCall | RequestObservation | Wait | Finish | Fail, Field(discriminator="type")
]
DECISION: TypeAdapter[Decision] = TypeAdapter(Decision)


class EventBase(Contract):
    event_id: Identifier
    schema_version: Literal[1] = 1
    source_id: Identifier
    source_event_id: Identifier | None = None
    occurred_start: AwareDatetime
    occurred_end: AwareDatetime
    received_at: AwareDatetime
    clock_uncertainty_ms: NonNegative = 0
    task_id: Identifier
    correlation_id: Identifier
    causation_event_id: Identifier | None = None

    @model_validator(mode="after")
    def ordered_interval(self) -> Self:
        if self.occurred_end < self.occurred_start:
            raise ValueError("occurrence interval is reversed")
        return self


class UserEvent(EventBase):
    kind: Literal["user.message"]
    provenance_class: Literal["user_statement"]
    payload: Message


class DecisionEvent(EventBase):
    kind: Literal["cognition.decision"]
    provenance_class: Literal["model_proposal"]
    payload: Decision


class ObservationEvent(EventBase):
    kind: Literal["tool.observation"]
    provenance_class: Literal["tool_result"]
    payload: Observation


class ResponseEvent(EventBase):
    kind: Literal["communication.response"]
    provenance_class: Literal["model_response"]
    payload: Message


class Claim(Contract):
    entity_id: Identifier
    predicate: Identifier
    value: Text
    basis: Literal["observed", "user_reported", "inferred", "predicted"]
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    valid_from: AwareDatetime
    valid_until: AwareDatetime | None = None
    supersedes: Identifier | None = None

    @model_validator(mode="after")
    def interval(self) -> Self:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("claim validity must be a positive interval")
        return self


class ClaimEvent(EventBase):
    kind: Literal["state.claim"]
    provenance_class: Literal["interpretation"]
    payload: Claim


class SelfState(Contract):
    category: Literal["capability", "source_health", "activity", "pending_action", "commitment"]
    key: Identifier
    value: Text


class SelfEvent(EventBase):
    kind: Literal["state.self"]
    provenance_class: Literal["runtime_status"]
    payload: SelfState


class Prediction(Contract):
    expected_kind: Literal["tool.observation", "communication.response"]
    source_id: Identifier
    not_before: AwareDatetime
    deadline: AwareDatetime
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def window(self) -> Self:
        if self.deadline < self.not_before:
            raise ValueError("prediction window reversed")
        return self


class PredictionEvent(EventBase):
    kind: Literal["state.prediction"]
    provenance_class: Literal["prediction"]
    payload: Prediction


Event = Annotated[
    UserEvent
    | DecisionEvent
    | ObservationEvent
    | ResponseEvent
    | ClaimEvent
    | SelfEvent
    | PredictionEvent,
    Field(discriminator="kind"),
]
EVENT: TypeAdapter[Event] = TypeAdapter(Event)
