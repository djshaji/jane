import asyncio
import io
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from jane.core.clock import FakeClock
from jane.core.contracts import ImageEvent, Message, ObservationRequest, UserEvent
from jane.storage.journal import Journal
from jane.storage.media import MAX_IMAGE_BYTES, MediaStore, normalize, read_asset
from jane.vision.service import CameraService


def picture(flat=False):
    image = Image.new("RGB", (64, 64), "gray")
    if not flat:
        for x in range(64):
            for y in range(64):
                image.putpixel((x, y), (255, 255, 255) if (x + y) % 2 else (0, 0, 0))
    result = io.BytesIO()
    image.save(result, format="PNG")
    return result.getvalue()


class Camera:
    def __init__(self):
        self.data = picture()
        self.calls = 0
        self.failed = False

    def capture(self):
        self.calls += 1
        if self.failed:
            raise OSError("unplugged")
        return self.data


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name)
        self.j = Journal(self.path / "jane.db")
        self.media = MediaStore(self.j, self.path / "media")
        self.camera = Camera()
        self.clock = FakeClock(datetime(2026, 11, 6, tzinfo=UTC))
        self.service = CameraService(self.j, self.media, self.camera, self.clock)
        self.request = ObservationRequest(
            source_id="camera.0",
            modality="image",
            max_age_ms=5000,
            purpose="Inspect current object",
        )

    def tearDown(self):
        self.j.close()
        self.directory.cleanup()

    def capture(self):
        return self.service.capture(self.request, "task", "turn", enabled=True)

    def test_explicit_enablement_before_device_or_event(self):
        with self.assertRaises(ValueError):
            self.service.capture(self.request, "task", "turn", enabled=False)
        self.assertEqual(self.camera.calls, 0)
        self.assertFalse(self.j.events())

    def test_finalized_pixels_survive_restart_without_world_identity_claims(self):
        event = self.capture()
        context = self.service.context(event, self.request)
        self.assertEqual(context.data, picture())
        self.assertEqual(context.asset.width, 64)
        self.j.close()
        self.j = Journal(self.path / "jane.db")
        self.assertEqual(read_asset(self.j, event.payload.asset_id)[1], context.data)
        self.assertEqual(len([e for e in self.j.events() if isinstance(e, ImageEvent)]), 1)
        self.assertFalse(self.j.world(self.clock.now())["claims"])

    def test_expired_frames_are_replaced_even_for_stationary_scene(self):
        first = self.service.current(self.request, "task", "turn", enabled=True)
        cached = self.service.current(self.request, "task", "turn", enabled=True)
        self.assertEqual(first.event.event_id, cached.event.event_id)
        self.clock.advance(6)
        fresh = self.service.current(self.request, "task", "turn", enabled=True)
        self.assertNotEqual(first.event.event_id, fresh.event.event_id)
        self.assertEqual(first.asset.digest, fresh.asset.digest)
        self.assertEqual(self.camera.calls, 2)

    def test_failure_never_reuses_old_frame_and_recovers(self):
        first = self.capture()
        self.camera.failed = True
        with self.assertRaises(ValueError):
            self.capture()
        with self.assertRaises(ValueError):
            self.service.context(first, self.request)
        with self.assertRaises(ValueError):
            self.service.current(self.request, "task", "turn", enabled=True)
        self.camera.failed = False
        self.assertNotEqual(
            self.service.current(self.request, "task", "turn", enabled=True).event, first
        )

    def test_uncertain_occluded_image_requests_closer_view(self):
        self.camera.data = picture(flat=True)
        event = self.capture()
        self.assertEqual(event.payload.quality, "uncertain")
        with self.assertRaisesRegex(ValueError, "clearer or closer"):
            self.service.context(event, self.request)
        self.assertFalse(self.j.world(self.clock.now())["claims"])

    def test_missing_corrupt_and_expired_media_are_not_usable(self):
        for mutation in ("missing", "corrupt", "expired"):
            with self.subTest(mutation=mutation):
                event = self.capture()
                path = self.media.root / (event.payload.asset_id + ".png")
                if mutation == "missing":
                    path.unlink()
                elif mutation == "corrupt":
                    path.write_bytes(b"x" * path.stat().st_size)
                else:
                    self.media.expire(event.payload.asset_id)
                with self.assertRaises(ValueError):
                    self.service.context(event, self.request)
                self.assertIn(event, self.j.events())  # Retention never rewrites history.

    def test_write_failure_publishes_only_health_and_orphans_are_cleaned(self):
        with patch("os.replace", side_effect=OSError("disk failure")):
            with self.assertRaises(ValueError):
                self.capture()
        self.assertFalse(any(isinstance(e, ImageEvent) for e in self.j.events()))
        self.assertFalse(list(self.media.root.iterdir()))
        with self.assertRaises(RuntimeError):
            with self.j.transaction():
                self.media.stage(picture(), self.clock.now())
                raise RuntimeError("crash before commit")
        (self.media.root / "image-interrupted.tmp").write_bytes(b"partial")
        self.assertEqual(self.media.recover(), 2)
        self.assertFalse(list(self.media.root.iterdir()))

    def test_missing_asset_reference_rejected_atomically(self):
        event = self.capture()
        with self.assertRaises(ValueError):
            self.j.accept(
                ImageEvent.model_validate(
                    event.model_dump()
                    | {
                        "event_id": "forged",
                        "source_event_id": "missing",
                        "payload": event.payload.model_dump() | {"asset_id": "missing"},
                    }
                )
            )
        self.assertFalse(any(e.event_id == "forged" for e in self.j.events()))

    def test_alignment_requires_same_task_turn_and_reports_uncertainty(self):
        event = self.capture()
        anchor = UserEvent(
            event_id="speech",
            source_id="microphone",
            task_id="task",
            correlation_id="turn",
            occurred_start=self.clock.now(),
            occurred_end=self.clock.now(),
            received_at=self.clock.now(),
            kind="user.message",
            provenance_class="user_statement",
            payload=Message(text="What is this?"),
        )
        self.j.accept(anchor)
        self.assertIn(
            "not_identity_proof",
            self.service.context(event, self.request, anchor="speech").alignment,
        )
        self.j.accept(
            UserEvent.model_validate(
                anchor.model_dump()
                | {
                    "event_id": "other",
                    "task_id": "other-task",
                }
            )
        )
        with self.assertRaises(ValueError):
            self.service.context(event, self.request, anchor="other")

    def test_direct_pixels_only_for_image_capable_adapter(self):
        event = self.capture()

        class Scripted:
            supports_images = True

            async def inspect(inner, context):
                self.assertEqual(context.data, picture())
                return "Scripted image received; no object identity inferred"

        self.assertIn(
            "Scripted", asyncio.run(self.service.handoff(event, self.request, Scripted()))
        )
        consumer = Scripted()
        consumer.supports_images = False
        with self.assertRaises(ValueError):
            asyncio.run(self.service.handoff(event, self.request, consumer))

    def test_migration_from_v7_preserves_existing_events(self):
        self.j.db.execute("DROP TABLE assets")
        self.j.db.execute("PRAGMA user_version=7")
        self.j.close()
        self.j = Journal(self.path / "jane.db")
        self.assertEqual(self.j.status()["schema_version"], 8)

    def test_decode_bounds_and_invalid_image(self):
        for data in (b"", b"not an image", b"x" * (MAX_IMAGE_BYTES + 1)):
            with self.assertRaises(ValueError):
                normalize(data)

    def test_recovery_expires_processing_media_but_preserves_explicit_retention(self):
        processing = self.capture()
        kept = self.service.capture(self.request, "task", "turn", enabled=True, retain=True)
        self.media.recover()
        with self.assertRaises(ValueError):
            read_asset(self.j, processing.payload.asset_id)
        self.assertTrue(read_asset(self.j, kept.payload.asset_id)[1])

    def test_blurred_frame_requests_more_evidence(self):
        from PIL import ImageFilter

        with Image.open(io.BytesIO(picture())) as image:
            blurred = image.filter(ImageFilter.GaussianBlur(8))
            output = io.BytesIO()
            blurred.save(output, format="PNG")
        self.camera.data = output.getvalue()
        with self.assertRaisesRegex(ValueError, "clearer or closer"):
            self.service.current(self.request, "task", "turn", enabled=True)

    def test_stale_inference_result_is_rejected_and_processing_image_expires(self):
        event = self.capture()

        class Slow:
            supports_images = True

            async def inspect(inner, context):
                self.clock.advance(6)
                return "obsolete"

        with self.assertRaisesRegex(ValueError, "stale"):
            asyncio.run(self.service.handoff(event, self.request, Slow()))
        with self.assertRaises(ValueError):
            read_asset(self.j, event.payload.asset_id)

    def test_text_provider_rejects_image_before_network_dispatch(self):
        from jane.cognition.context import ContextBudget, build_snapshot
        from jane.motivation.goals import Goal, Goals
        from jane.providers.openai import OpenAIAdapter, ProviderError

        self.capture()
        goals = Goals(self.j)
        goals.add(
            Goal(
                goal_id="task",
                description="Inspect image",
                scope=("sim.inspect",),
                success_event_kind="tool.observation",
                created_at=self.clock.now(),
            )
        )
        goals.review(self.clock.now(), ("sim.inspect",))
        snapshot = build_snapshot(
            self.j,
            goals.get("task"),
            self.clock.now(),
            "request",
            "turn",
            ("sim.inspect",),
            ContextBudget(),
        )
        self.assertTrue(any(isinstance(e, ImageEvent) for e in snapshot.events))

        class Transport:
            async def post(inner, body):
                self.fail("text-only provider must not dispatch image context")

        with self.assertRaises(ProviderError) as error:
            asyncio.run(OpenAIAdapter(transport=Transport()).decide(snapshot))
        self.assertEqual(error.exception.category, "unsupported_observation")

    def test_cli_no_camera_or_database_without_opt_in(self):
        import contextlib

        from jane.cli import main

        path = self.path / "disabled.db"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["camera", "capture", str(path), "--task", "task"]), 2)
        self.assertFalse(path.exists())

    def test_capture_worker_timeout_is_sanitized(self):
        import subprocess

        from jane.vision.camera import OpenCVCamera

        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("camera", 8)):
            with self.assertRaisesRegex(ValueError, "timed out"):
                OpenCVCamera().capture()
