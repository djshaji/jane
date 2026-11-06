import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from jane.cognition.context import ContextBudget, build_snapshot
from jane.cognition.coordinator import Coordinator, Limits
from jane.core.clock import FakeClock
from jane.motivation.goals import Goal, Goals
from jane.providers.openai import OpenAIAdapter, ProviderConfig, ProviderError, output_schema
from jane.runner import demo_scenario
from jane.storage.journal import Journal


def envelope(snapshot):
    decision = {
        k: getattr(snapshot, k) for k in ("request_id", "snapshot_id", "task_id", "generation")
    }
    decision.update(
        type="wait",
        review_after_ms=1000,
        decision_id="d-" + snapshot.request_id,
        evidence_ids=[],
        summary="Wait for input",
    )
    return {
        "model": "gpt-6.1-sol",
        "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": json.dumps({"decision": decision})}],
            }
        ],
    }


class FakeTransport:
    def __init__(self, *steps):
        self.steps = list(steps)
        self.bodies = []
        self.closed = False

    async def post(self, body):
        self.bodies.append(body)
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step(body) if callable(step) else step

    async def close(self):
        self.closed = True


def success(body):
    from jane.cognition.context import Snapshot

    return 200, envelope(Snapshot.model_validate_json(body["input"][0]["content"]))


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.j = Journal(Path(self.tmp.name) / "provider.db")
        self.clock = FakeClock(demo_scenario().start)
        Goals(self.j).add(
            Goal(
                goal_id="task",
                description="Inspect fixture",
                scope=("sim.inspect",),
                success_event_kind="tool.observation",
                created_at=self.clock.now(),
            )
        )
        Goals(self.j).review(self.clock.now(), ("sim.inspect",))
        self.snapshot = build_snapshot(
            self.j,
            Goals(self.j).get("task"),
            self.clock.now(),
            "r",
            "turn",
            ("sim.inspect",),
            ContextBudget(),
        )

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def coordinator(self, transport, **limits):
        return Coordinator(
            self.j,
            self.clock,
            OpenAIAdapter(transport=transport),
            lambda: ("sim.inspect",),
            Limits(retry_delay_seconds=0.0, **limits),
        )

    async def test_conversion_metadata_and_no_provider_storage(self):
        transport = FakeTransport(success)
        c = self.coordinator(transport)
        result = await c.step("turn")
        self.assertEqual(result.status, "accepted")
        body = transport.bodies[0]
        self.assertFalse(body["store"])
        self.assertNotIn("tools", body)
        self.assertEqual(body["reasoning"], {"effort": "medium"})
        self.assertEqual(body["max_output_tokens"], 4096)
        metadata = json.loads(
            self.j.db.execute("SELECT metadata FROM provider_attempts").fetchone()[0]
        )
        self.assertEqual(metadata["usage"]["total_tokens"], 150)
        self.assertEqual(metadata["returned_model"], "gpt-6.1-sol")

    async def test_rate_limit_retry_counted_and_bounded(self):
        transport = FakeTransport((429, {}), success)
        c = self.coordinator(transport)
        self.assertEqual((await c.step("turn")).status, "accepted")
        self.assertEqual(len(transport.bodies), 2)
        self.assertEqual(self.j.db.execute("SELECT calls FROM cognitive_requests").fetchone()[0], 2)
        self.assertEqual(
            self.j.db.execute("SELECT count(*) FROM provider_attempts").fetchone()[0], 2
        )

    async def test_retry_cannot_exceed_total_budget(self):
        transport = FakeTransport((503, {}), success)
        c = self.coordinator(transport, max_calls=1)
        self.assertEqual((await c.step("turn")).error.category, "budget_exhausted")
        self.assertEqual(len(transport.bodies), 1)
        self.assertEqual(Goals(self.j).get("task").status, "active")

    async def test_malformed_one_repair_then_recoverable(self):
        transport = FakeTransport((200, {}), (200, {}), success)
        c = self.coordinator(transport)
        self.assertEqual((await c.step("turn")).error.category, "provider_malformed")
        self.assertEqual(len(transport.bodies), 2)
        self.assertIn("previous attempt", transport.bodies[1]["instructions"])
        self.assertEqual((await c.step("turn")).status, "accepted")

    async def test_timeout_and_auth_not_retried(self):
        for step, category in [
            (ProviderError("provider_timeout"), "provider_timeout"),
            ((401, {"error": "secret-key"}), "provider_auth"),
        ]:
            c = self.coordinator(FakeTransport(step))
            outcome = await c.step(category)
            self.assertEqual(outcome.error.category, category)
            self.assertEqual(outcome.error.calls, 1)
        self.assertNotIn("secret-key", "".join(self.j.db.iterdump()))

    async def test_refusal_incomplete_and_usage_preserved(self):
        for status, content, expected in [
            ("incomplete", [], "provider_incomplete"),
            ("completed", [{"type": "refusal", "refusal": "private refusal"}], "provider_refusal"),
        ]:
            response = envelope(self.snapshot)
            response["status"] = status
            response["output"][0]["content"] = content
            c = self.coordinator(FakeTransport((200, response)))
            self.assertEqual((await c.step(expected)).error.category, expected)
        metadata = [
            json.loads(r[0]) for r in self.j.db.execute("SELECT metadata FROM provider_attempts")
        ]
        self.assertTrue(all(m["usage"]["total_tokens"] == 150 for m in metadata))
        self.assertNotIn("private refusal", "".join(self.j.db.iterdump()))

    async def test_invalid_modality_model_and_missing_key(self):
        for config in ({"modality": "image"}, {"model": "unverified-model"}):
            with self.assertRaises(ValidationError):
                ProviderConfig.model_validate(config)
        with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
            with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
                OpenAIAdapter()

    async def test_usage_cannot_be_injected_through_decision(self):
        response = envelope(self.snapshot)
        decision = json.loads(response["output"][0]["content"][0]["text"])
        decision["decision"]["usage"] = {"total_tokens": 0}
        response["output"][0]["content"][0]["text"] = json.dumps(decision)
        adapter = OpenAIAdapter(transport=FakeTransport((200, response)))
        with self.assertRaisesRegex(ProviderError, "provider_malformed"):
            await adapter.decide(self.snapshot)
        self.assertEqual(adapter.last_usage.total_tokens, 150)

    async def test_request_overflow_before_transport(self):
        transport = FakeTransport(success)
        adapter = OpenAIAdapter(ProviderConfig(max_request_bytes=4096), transport)
        with self.assertRaisesRegex(ProviderError, "context_overflow"):
            await adapter.decide(self.snapshot)
        self.assertEqual(transport.bodies, [])

    async def test_cancellation_closes_out_request_without_effect(self):
        entered = asyncio.Event()
        cancelled = asyncio.Event()

        class Slow(FakeTransport):
            async def post(self, body):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        c = self.coordinator(Slow())
        task = asyncio.create_task(c.step("turn"))
        await entered.wait()
        c.cancel()
        self.assertEqual((await task).error.category, "cancelled")
        await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual(self.j.db.execute("SELECT count(*) FROM actions").fetchone()[0], 0)

    async def test_http_boundary_sanitizes_errors_and_does_not_follow_redirects(self):
        try:
            import httpx
        except ImportError:
            self.skipTest("optional httpx not installed")
        from pydantic import SecretStr

        from jane.providers.openai import HTTPTransport

        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(
                302, headers={"Location": "https://untrusted.invalid"}, json={"error": "SECRET-KEY"}
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        with patch("httpx.AsyncClient", return_value=client):
            transport = HTTPTransport(SecretStr("SECRET-KEY"), 1.0)
        adapter = OpenAIAdapter(transport=transport)
        try:
            with self.assertRaises(ProviderError) as caught:
                await adapter.decide(self.snapshot)
            self.assertEqual(str(caught.exception), "provider_rejected")
            self.assertEqual(len(calls), 1)
            self.assertEqual(str(calls[0].url), "https://api.openai.com/v1/responses")
            self.assertNotIn("SECRET-KEY", json.dumps(adapter.metadata()))
        finally:
            await adapter.close()

    async def test_http_timeout_maps_to_safe_error(self):
        try:
            import httpx
        except ImportError:
            self.skipTest("optional httpx not installed")
        from pydantic import SecretStr

        from jane.providers.openai import HTTPTransport

        def handler(request):
            raise httpx.ReadTimeout("SECRET-KEY")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("httpx.AsyncClient", return_value=client):
            transport = HTTPTransport(SecretStr("SECRET-KEY"), 1.0)
        try:
            with self.assertRaisesRegex(ProviderError, "^provider_timeout$"):
                await transport.post({})
        finally:
            await transport.close()

    async def test_fake_provider_exercises_verified_application_loop(self):
        from jane.cognition.context import Snapshot
        from jane.offline import InspectionPolicy, OfflineRuntime

        class PolicyTransport(FakeTransport):
            async def post(self, body):
                self.bodies.append(body)
                snapshot = Snapshot.model_validate_json(body["input"][0]["content"])
                response = envelope(snapshot)
                output = await InspectionPolicy().decide(snapshot)
                response["output"][0]["content"][0]["text"] = json.dumps(
                    {"decision": json.loads(output)}
                )
                return 200, response

        transport = PolicyTransport()
        adapter = OpenAIAdapter(transport=transport)
        runtime = OfflineRuntime(
            self.j, Path(self.tmp.name) / "sim.db", self.clock, adapter=adapter
        )
        try:
            # Use a fresh explicitly targeted inspection goal, ahead of the setup fixture goal.
            Goals(self.j).change("task", "cancel", self.clock.now())
            task = runtime.submit("Inspect the simulated cup", "cup")
            runtime.startup()
            with patch("socket.socket", side_effect=AssertionError("network forbidden")):
                trace = await runtime.run()
            self.assertEqual(trace[-1]["status"], "completed")
            self.assertEqual(Goals(self.j).get(task).status, "completed")
            self.assertEqual(len(transport.bodies), 5)
            self.assertEqual(
                self.j.db.execute("SELECT count(*) FROM provider_attempts").fetchone()[0], 5
            )
        finally:
            runtime.close()
            await adapter.close()

    async def test_schema_root_object_nested_anyof_all_fields_required(self):
        schema = output_schema()
        self.assertEqual(schema["type"], "object")
        self.assertIn("anyOf", schema["properties"]["decision"])
        self.assertNotIn("discriminator", json.dumps(schema))
        for definition in schema["$defs"].values():
            if definition.get("type") == "object":
                self.assertFalse(definition["additionalProperties"])
                self.assertEqual(set(definition["required"]), set(definition["properties"]))


@unittest.skipUnless(os.getenv("JANE_LIVE_TEST") == "1", "live inference requires JANE_LIVE_TEST=1")
class LiveContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_response_validates_and_records_usage(self):
        # An explicitly enabled paid contract test: one request, no executable tool effects.
        with tempfile.TemporaryDirectory() as directory:
            j = Journal(Path(directory) / "live.db")
            adapter = OpenAIAdapter()
            clock = FakeClock(demo_scenario().start)
            try:
                Goals(j).add(
                    Goal(
                        goal_id="live-test",
                        description="Return wait with a short review interval.",
                        scope=("sim.inspect",),
                        success_event_kind="tool.observation",
                        created_at=clock.now(),
                    )
                )
                coordinator = Coordinator(
                    j,
                    clock,
                    adapter,
                    lambda: ("sim.inspect",),
                    Limits(max_calls=1, formatting_repairs=0, provider_retries=0),
                )
                result = await coordinator.step("live-test")
                self.assertEqual(result.status, "accepted", result.model_dump_json())
                self.assertIsNotNone(adapter.last_usage)
                self.assertEqual(j.db.execute("SELECT count(*) FROM actions").fetchone()[0], 0)
            finally:
                await adapter.close()
                j.close()
