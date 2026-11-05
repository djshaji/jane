"""Pure ingest-order reducer. Queries derive freshness without mutating history."""

from datetime import datetime
from typing import Any

from jane.core.contracts import ClaimEvent, Event, ObservationEvent, PredictionEvent, SelfEvent


def project(events: tuple[Event, ...]) -> dict[str, Any]:
    claims: dict[str, Any] = {}
    entities: set[str] = set()
    self_state: dict[str, Any] = {}
    episodes: dict[str, Any] = {}
    predictions: dict[str, Any] = {}
    seen: dict[str, Event] = {}
    for event in events:
        episode_key = f"{event.task_id}/{event.correlation_id}"
        episode = episodes.setdefault(
            episode_key,
            {
                "task_id": event.task_id,
                "correlation_id": event.correlation_id,
                "event_ids": [],
                "start": event.occurred_start.isoformat(),
                "end": event.occurred_end.isoformat(),
            },
        )
        episode["event_ids"].append(event.event_id)
        if event.occurred_start < datetime.fromisoformat(episode["start"]):
            episode["start"] = event.occurred_start.isoformat()
        if event.occurred_end > datetime.fromisoformat(episode["end"]):
            episode["end"] = event.occurred_end.isoformat()
        if isinstance(event, ObservationEvent):
            entities.add(event.payload.object_id)
            claims[event.event_id] = {
                "entity_id": event.payload.object_id,
                "predicate": "description",
                "value": event.payload.description,
                "basis": "observed",
                "evidence_ids": [event.event_id],
                "valid_from": event.occurred_start.isoformat(),
                # A snapshot is not proof of an indefinitely unchanged world.
                "valid_until": None,
                "snapshot_only": True,
                "supersedes": None,
                "superseded_by": None,
            }
        elif isinstance(event, ClaimEvent):
            payload = event.payload
            if not set(payload.evidence_ids) <= seen.keys():
                raise ValueError("claim requires existing evidence")
            if payload.basis == "observed" and not all(
                isinstance(seen[e], ObservationEvent) for e in payload.evidence_ids
            ):
                raise ValueError("observed claim requires tool observations")
            if payload.basis == "user_reported" and not all(
                seen[e].provenance_class == "user_statement" for e in payload.evidence_ids
            ):
                raise ValueError("user-reported claim requires user statements")
            if payload.supersedes is not None:
                previous = claims.get(payload.supersedes)
                if previous is None or previous["superseded_by"] is not None:
                    raise ValueError("correction target missing or already superseded")
                if (previous["entity_id"], previous["predicate"]) != (
                    payload.entity_id,
                    payload.predicate,
                ):
                    raise ValueError("correction must concern the same entity and predicate")
                previous["superseded_by"] = event.event_id
            entities.add(payload.entity_id)
            claims[event.event_id] = payload.model_dump(mode="json") | {
                "snapshot_only": False,
                "superseded_by": None,
            }
        elif isinstance(event, SelfEvent):
            key = f"{event.payload.category}/{event.payload.key}"
            old = self_state.get(key)
            # A late health report must not overwrite a newer report.
            if old is None or event.occurred_end >= datetime.fromisoformat(old["occurred_at"]):
                self_state[key] = event.payload.model_dump() | {
                    "event_id": event.event_id,
                    "occurred_at": event.occurred_end.isoformat(),
                }
        elif isinstance(event, PredictionEvent):
            if not set(event.payload.evidence_ids) <= seen.keys():
                raise ValueError("prediction requires existing evidence")
            predictions[event.event_id] = event.payload.model_dump(mode="json") | {
                "task_id": event.task_id,
                "matched_event_id": None,
            }
        # Predictions are evaluated only against subsequently accepted evidence.
        for prediction in predictions.values():
            if (
                prediction["matched_event_id"] is None
                and event.kind == prediction["expected_kind"]
                and event.source_id == prediction["source_id"]
                and event.task_id == prediction["task_id"]
                and datetime.fromisoformat(prediction["not_before"])
                <= event.occurred_start
                <= datetime.fromisoformat(prediction["deadline"])
            ):
                prediction["matched_event_id"] = event.event_id
        seen[event.event_id] = event
    return {
        "claims": claims,
        "entities": sorted(entities),
        "self": self_state,
        "episodes": episodes,
        "predictions": predictions,
    }


def view(state: dict[str, Any], now: datetime) -> dict[str, Any]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("view time must be timezone-aware")
    # Query state is detached from the durable projection.
    import copy

    result = copy.deepcopy(state)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for claim in result["claims"].values():
        start = datetime.fromisoformat(claim["valid_from"])
        end = claim["valid_until"]
        status = "current"
        if claim["superseded_by"] is not None:
            status = "superseded"
        elif now < start:
            status = "not_yet_valid"
        elif (end is not None and now >= datetime.fromisoformat(end)) or (
            claim["snapshot_only"] and now > start
        ):
            status = "stale"
        claim["status"] = status
        claim["conflict"] = False
        if status == "current":
            groups.setdefault((claim["entity_id"], claim["predicate"]), []).append(claim)
    for claims in groups.values():
        if len({c["value"] for c in claims}) > 1:
            for claim in claims:
                claim["conflict"] = True
    for prediction in result["predictions"].values():
        prediction["status"] = (
            "supported"
            if prediction["matched_event_id"]
            else "expired"
            if now > datetime.fromisoformat(prediction["deadline"])
            else "unresolved"
        )
    return result
