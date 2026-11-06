"""Task-scoped camera evidence, conservative freshness and direct image handoff."""

from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol
from uuid import uuid4

from jane.core.clock import Clock
from jane.core.contracts import (
    ImageEvent,
    ImageEvidence,
    ObservationRequest,
    SelfEvent,
    SelfState,
)
from jane.storage.journal import Journal
from jane.storage.media import ImageAsset, MediaStore, read_asset
from jane.vision.camera import Camera


@dataclass(frozen=True)
class ImageContext:
    event: ImageEvent
    asset: ImageAsset
    data: bytes
    alignment: str = "unassociated"


class ImageConsumer(Protocol):
    """Future multimodal adapters receive actual pixels, not an implied image caption."""

    supports_images: bool

    async def inspect(self, context: ImageContext) -> str: ...


class CameraService:
    def __init__(self, journal: Journal, media: MediaStore, camera: Camera, clock: Clock) -> None:
        self.j, self.media, self.camera, self.clock = journal, media, camera, clock

    def capture(
        self,
        request: ObservationRequest,
        task: str,
        turn: str,
        *,
        enabled: bool,
        retain: bool = False,
    ) -> ImageEvent:
        if not enabled:
            raise ValueError("camera capture requires --enable-camera")
        if request.modality != "image":
            raise ValueError("camera supports image observations only")
        start = self.clock.now()
        identifier = uuid4().hex
        # Validate event identifiers before touching the device or writing files.
        health = SelfEvent(
            event_id="camera-health-" + identifier,
            source_id=request.source_id,
            task_id=task,
            correlation_id=turn,
            occurred_start=start,
            occurred_end=start,
            received_at=start,
            kind="state.self",
            provenance_class="runtime_status",
            payload=SelfState(category="source_health", key=request.source_id, value="unavailable"),
        )
        try:
            data = self.camera.capture()
            end = self.clock.now()
            with self.j.transaction():
                asset = self.media.stage(data, end, retain=retain)
                event = ImageEvent(
                    event_id="frame-" + identifier,
                    source_id=request.source_id,
                    source_event_id=asset.asset_id,
                    task_id=task,
                    correlation_id=turn,
                    occurred_start=start,
                    occurred_end=end,
                    received_at=end,
                    clock_uncertainty_ms=1000,
                    kind="sensor.image",
                    provenance_class="sensor_observation",
                    payload=ImageEvidence(
                        asset_id=asset.asset_id, digest=asset.digest, quality=asset.quality
                    ),
                )
                self.j.accept(event)
                self.j.accept(
                    SelfEvent.model_validate(
                        health.model_dump()
                        | {
                            "occurred_end": end,
                            "received_at": end,
                            "payload": SelfState(
                                category="source_health", key=request.source_id, value="available"
                            ),
                        }
                    )
                )
            return event
        except Exception:
            end = self.clock.now()
            self.j.accept(
                SelfEvent.model_validate(
                    health.model_dump()
                    | {
                        "occurred_end": end,
                        "received_at": end,
                    }
                )
            )
            raise ValueError("camera capture failed; no replacement frame is available") from None

    def context(
        self, event: ImageEvent, request: ObservationRequest, *, anchor: str | None = None
    ) -> ImageContext:
        if request.modality != "image" or event.source_id != request.source_id:
            raise ValueError("observation source/modality mismatch")
        committed = next((e for e in self.j.events() if e.event_id == event.event_id), None)
        if committed != event:
            raise ValueError("image must be a committed event")
        now = self.clock.now()
        uncertainty = timedelta(milliseconds=event.clock_uncertainty_ms)
        if event.occurred_end > now or now - event.occurred_start + uncertainty > timedelta(
            milliseconds=request.max_age_ms
        ):
            raise ValueError("image is stale or time alignment is uncertain; capture again")
        # A subsequent failed capture prevents silent reuse of an earlier successful frame.
        history = self.j.events()
        index = next(i for i, e in enumerate(history) if e.event_id == event.event_id)
        if any(
            e.source_id == event.source_id
            and e.kind == "state.self"
            and e.payload.category == "source_health"
            and e.payload.value == "unavailable"
            for e in history[index + 1 :]
        ):
            raise ValueError("camera failed after this frame; capture again")
        asset, data = read_asset(self.j, event.payload.asset_id)
        if asset.quality != "usable":
            raise ValueError("image quality uncertain; request a clearer or closer view")
        alignment = "unassociated"
        if anchor is not None:
            other = next((e for e in history if e.event_id == anchor), None)
            if (
                other is None
                or other.task_id != event.task_id
                or other.correlation_id != event.correlation_id
            ):
                raise ValueError("alignment requires evidence from the same task and turn")
            gap = max(
                other.occurred_start - event.occurred_end,
                event.occurred_start - other.occurred_end,
                timedelta(),
            )
            error = timedelta(milliseconds=other.clock_uncertainty_ms + event.clock_uncertainty_ms)
            if gap + error > timedelta(milliseconds=request.max_age_ms):
                raise ValueError("ambiguous temporal alignment; request a fresh frame")
            alignment = "nearby_with_clock_uncertainty_not_identity_proof"
        return ImageContext(event, asset, data, alignment)

    def current(
        self, request: ObservationRequest, task: str, turn: str, *, enabled: bool
    ) -> ImageContext:
        candidates = [
            e
            for e in self.j.events()
            if isinstance(e, ImageEvent)
            and e.task_id == task
            and e.correlation_id == turn
            and e.source_id == request.source_id
        ]
        if candidates:
            try:
                return self.context(candidates[-1], request)
            except ValueError:
                pass
        # No old-frame fallback if this attempt fails or its quality is insufficient.
        return self.context(self.capture(request, task, turn, enabled=enabled), request)

    async def handoff(
        self, event: ImageEvent, request: ObservationRequest, consumer: ImageConsumer
    ) -> str:
        if not consumer.supports_images:
            raise ValueError("adapter does not support images; use an image-capable adapter")
        context = self.context(event, request)
        try:
            result = await consumer.inspect(context)
            self.context(event, request)  # Revalidate freshness/integrity after inference.
            return result
        finally:
            if not context.asset.retain:
                self.media.expire(context.asset.asset_id)
