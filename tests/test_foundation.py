import contextlib
import io
import json
import socket
import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from pydantic import ValidationError

from jane.actions.mock import MockInspector
from jane.cli import main
from jane.cognition.scripted import ScriptedAdapter
from jane.core.clock import FakeClock
from jane.core.contracts import DECISION, EVENT, InspectArguments
from jane.runner import ReplayError, Scenario, demo_scenario, run


class FoundationTests(unittest.TestCase):
    def fixture(self):
        return json.loads(demo_scenario().model_dump_json())

    def scenario(self, data):
        return Scenario.model_validate_json(json.dumps(data))

    def test_demo_repeats_exactly_without_network(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
            first = run(demo_scenario())
            second = run(demo_scenario())
        self.assertEqual(first.render(), second.render())
        self.assertEqual(first.terminal, "finished")
        self.assertEqual(first.elapsed_seconds, 3)
        self.assertEqual(first.inspected_objects, ("object-001",))
        self.assertEqual(
            [e.kind for e in first.events],
            [
                "user.message",
                "cognition.decision",
                "tool.observation",
                "cognition.decision",
                "communication.response",
                "cognition.decision",
            ],
        )
        self.assertEqual(first.events[2].occurred_start, datetime(2026, 11, 5, 0, 0, 1, tzinfo=UTC))

    def test_contract_roundtrip_and_schema(self):
        fixture = demo_scenario()
        self.assertEqual(Scenario.model_validate_json(fixture.model_dump_json()), fixture)
        for event in run(fixture).events:
            self.assertEqual(EVENT.validate_json(event.model_dump_json()), event)
        self.assertEqual(DECISION.json_schema()["discriminator"]["propertyName"], "type")

    def test_decision_variants_validate(self):
        common = self.fixture()["steps"][0]["decision"]
        common.pop("arguments")
        common.pop("tool")
        variants = [
            dict(type="respond", text="Hello"),
            dict(
                type="request_observation",
                observation=dict(
                    source_id="desk", modality="image", max_age_ms=2000, purpose="Inspect"
                ),
            ),
            dict(type="wait", review_after_ms=5000),
            dict(type="finish"),
            dict(type="fail", category="blocked"),
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                decision = DECISION.validate_json(json.dumps(common | variant))
                self.assertEqual(decision.type, variant["type"])

    def test_rejects_malformed_decisions(self):
        good = self.fixture()["steps"][0]["decision"]
        mutations = [
            dict(type="execute_shell"),
            dict(generation="0"),
            dict(generation=True),
            dict(generation=-1),
            dict(tool="shell"),
            dict(unexpected=1),
            dict(arguments={"object_id": "object-001", "path": "/etc/passwd"}),
            dict(arguments={"object_id": 3}),
            dict(evidence_ids=[""]),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                DECISION.validate_json(json.dumps(good | mutation))

    def test_event_provenance_and_timestamps(self):
        event = self.fixture()["input"]
        for mutation in [
            dict(provenance_class="tool_result"),
            dict(kind="unknown"),
            dict(occurred_end="2026-11-04T23:59:59Z"),
            dict(received_at="2026-11-05T00:00:00"),
        ]:
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                EVENT.validate_json(json.dumps(event | mutation))

    def test_nonfinite_and_coerced_time_rejected(self):
        for value in [float("nan"), float("inf"), -1, "1", True]:
            data = self.fixture()
            data["steps"][0]["advance_seconds"] = value
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.scenario(data)

    def test_clock_atomic_invalid_advance(self):
        clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
        clock.advance(2.5)
        for value in [-1, float("nan"), float("inf"), True]:
            with self.assertRaises(ValueError):
                clock.advance(value)
            self.assertEqual(clock.monotonic(), 2.5)
        self.assertEqual(clock.now(), datetime(2026, 1, 1, 0, 0, 2, 500000, tzinfo=UTC))
        with self.assertRaises(ValueError):
            FakeClock(datetime(2026, 1, 1))

    def test_stale_decision_rejected_before_tool(self):
        class NeverTool:
            def inspect(self, arguments):
                raise AssertionError("tool should not be called")

        for field, value in [
            ("generation", 1),
            ("task_id", "other"),
            ("snapshot_id", "stale"),
            ("request_id", "other"),
        ]:
            data = self.fixture()
            data["steps"][0]["decision"][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ReplayError, "active context"):
                run(self.scenario(data), tool=NeverTool())

    def test_unknown_evidence_is_rejected(self):
        data = self.fixture()
        data["steps"][0]["decision"]["evidence_ids"] = ["replay-observation-001"]
        with self.assertRaisesRegex(ReplayError, "unknown or future"):
            run(self.scenario(data))

    def test_terminal_and_budget_rules(self):
        data = self.fixture()
        data["steps"] = data["steps"][:2]
        with self.assertRaisesRegex(ReplayError, "budget exhausted"):
            run(self.scenario(data))
        data = self.fixture()
        data["steps"][0]["decision"] = data["steps"][0]["decision"] | dict(
            type="wait", review_after_ms=1
        )
        del data["steps"][0]["decision"]["tool"]
        del data["steps"][0]["decision"]["arguments"]
        with self.assertRaisesRegex(ReplayError, "after a terminal"):
            run(self.scenario(data))

    def test_no_false_completion_from_user_evidence(self):
        data = self.fixture()
        data["steps"][2]["decision"]["evidence_ids"] = ["user-001"]
        with self.assertRaisesRegex(ReplayError, "completion requires"):
            run(self.scenario(data))

    def test_tool_is_readonly_and_unknown_object_fails(self):
        tool = MockInspector(demo_scenario().observations)
        args = InspectArguments(object_id="object-001")
        before = tool.inspect(args)
        self.assertEqual(tool.inspect(args), before)
        with self.assertRaises(ValidationError):
            before.description = "changed"
        with self.assertRaisesRegex(ValueError, "unknown mock object"):
            tool.inspect(InspectArguments(object_id="missing"))

    def test_expected_results_are_asserted(self):
        data = self.fixture()
        data["expected"]["response"] = "wrong"
        with self.assertRaisesRegex(ReplayError, "expected outcomes"):
            run(self.scenario(data))

    def test_adapter_exhaustion(self):
        with self.assertRaisesRegex(ValueError, "script exhausted"):
            run(demo_scenario(), adapter=ScriptedAdapter(()))

    def test_cli_success_and_failure(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(main(["demo"]), 0)
        self.assertEqual(json.loads(out.getvalue())["terminal"], "finished")
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["replay", "/does-not-exist-jane.json"]), 2)
        self.assertIn("jane:", err.getvalue())
