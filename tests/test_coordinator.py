import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from jane.cognition.context import ContextBudget, build_snapshot
from jane.cognition.coordinator import Coordinator, Limits, ScriptedCognitiveAdapter
from jane.core.clock import FakeClock
from jane.core.contracts import Message
from jane.motivation.goals import Goal, Goals
from jane.runner import demo_scenario, run
from jane.storage.journal import Journal


def decision(snapshot, kind="wait", **extra):
    data = {
        key: getattr(snapshot, key)
        for key in ("request_id", "snapshot_id", "task_id", "generation")
    }
    data.update(
        decision_id="decision-" + snapshot.request_id,
        summary="Fixture decision",
        evidence_ids=[],
        type=kind,
    )
    if kind == "wait":
        data["review_after_ms"] = 1000
    return json.dumps(data | extra)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.j = Journal(Path(self.tmp.name) / "coordinator.db")
        self.clock = FakeClock(demo_scenario().start)
        self.goals = Goals(self.j)
        self.goal_id = demo_scenario().input.task_id
        self.goals.add(
            Goal(
                goal_id=self.goal_id,
                description="Inspect object",
                scope=("mock.inspect",),
                success_event_kind="tool.observation",
                created_at=self.clock.now(),
            )
        )
        self.j.accept(demo_scenario().input)
        self.permissions = ("mock.inspect",)

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def coordinator(self, *script, limits=None):
        return Coordinator(
            self.j, self.clock, ScriptedCognitiveAdapter(script), lambda: self.permissions, limits
        )

    async def test_scoped_tool_response_finish(self):
        coordinator = self.coordinator(
            lambda s: decision(s, "call_tool", tool="mock.inspect", arguments={"object_id": "cup"}),
            lambda s: decision(
                s, "respond", text="Observed fixture", evidence_ids=[s.events[-1].event_id]
            ),
            lambda s: decision(s, "finish", evidence_ids=[s.events[-1].event_id]),
        )
        coordinator.startup()
        first = await coordinator.step("turn")
        self.assertEqual(first.decision.type, "call_tool")
        self.assertFalse(any(e.kind == "tool.observation" for e in self.j.events()))
        # Caller supplies the known fixture result; J07 itself dispatches nothing.
        self.j.accept(run(demo_scenario()).events[2])
        self.assertEqual((await coordinator.step("turn")).decision.type, "respond")
        self.assertEqual((await coordinator.step("turn")).decision.type, "finish")
        self.assertEqual(self.goals.get(self.goal_id).spent, 0)
        self.assertEqual(self.goals.get(self.goal_id).status, "active")
        rows = self.j.db.execute("SELECT snapshot,usage FROM cognitive_requests").fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual(json.loads(rows[0][1])["calls"], 1)

    async def test_one_repair_and_persistent_call_budget(self):
        c = self.coordinator("{", decision, decision, limits=Limits(max_calls=2))
        self.assertEqual((await c.step("turn")).status, "accepted")
        self.assertEqual((await c.step("turn")).error.category, "budget_exhausted")
        other = self.coordinator(decision, limits=Limits(max_calls=2))
        self.assertEqual((await other.step("turn")).error.category, "budget_exhausted")
        self.assertEqual((await other.step("another-turn")).status, "accepted")

    async def test_malformed_and_semantic_errors_never_dispatch(self):
        for script, category in [
            (("{", "{"), "invalid_output"),
            ((lambda s: decision(s, "finish", evidence_ids=["missing"]),), "invalid_decision"),
            (
                (
                    lambda s: decision(
                        s,
                        "request_observation",
                        observation={
                            "source_id": "camera",
                            "modality": "image",
                            "max_age_ms": 0,
                            "purpose": "look",
                        },
                    ),
                ),
                "unsupported_observation",
            ),
        ]:
            outcome = await self.coordinator(*script).step("errors")
            self.assertEqual(outcome.error.category, category)
        self.assertFalse(any(e.kind == "cognition.decision" for e in self.j.events()))
        self.assertEqual(self.goals.get(self.goal_id).spent, 0)

    async def test_wait_and_fail_validated_without_lifecycle_effects(self):
        self.assertEqual((await self.coordinator(decision).step("wait")).decision.type, "wait")

        def fail(s):
            return decision(s, "fail", category="blocked")

        self.assertEqual((await self.coordinator(fail).step("fail")).decision.type, "fail")
        self.assertEqual(self.goals.get(self.goal_id).status, "active")

    async def test_superseding_instruction_and_unrelated_event(self):
        def update(snapshot, same):
            event = demo_scenario().input.model_copy(
                update={
                    "event_id": "change-" + str(same),
                    "task_id": self.goal_id if same else "unrelated",
                    "payload": Message(text="new"),
                }
            )
            self.j.accept(event)
            return decision(snapshot)

        c = self.coordinator(lambda s: update(s, False), lambda s: update(s, True))
        self.assertEqual((await c.step("a")).status, "accepted")
        self.assertEqual((await c.step("a")).error.category, "stale_context")

    async def test_permission_revocation_and_generation(self):
        def revoked(s):
            self.permissions = ()
            return decision(s, "call_tool", tool="mock.inspect", arguments={"object_id": "cup"})

        self.assertEqual((await self.coordinator(revoked).step("a")).error.category, "unauthorized")
        self.permissions = ("mock.inspect",)

        def cancelled(s):
            self.goals.change(self.goal_id, "cancel", self.clock.now())
            return decision(s)

        self.assertEqual(
            (await self.coordinator(cancelled).step("a")).error.category, "stale_context"
        )
        self.assertEqual((await self.coordinator(decision).step("a")).status, "idle")

    async def test_active_request_and_cancellation(self):
        entered = asyncio.Event()

        class Slow:
            async def decide(self, snapshot, repair=False):
                entered.set()
                await asyncio.Event().wait()

        c = Coordinator(self.j, self.clock, Slow(), lambda: self.permissions)
        pending = asyncio.create_task(c.step("a"))
        await entered.wait()
        with self.assertRaises(ValueError):
            await c.step("b")
        with self.assertRaises(ValueError):
            await self.coordinator(decision).step("b")
        c.cancel()
        self.assertEqual((await pending).error.category, "cancelled")

    async def test_timeout_and_adapter_exception(self):
        class Slow:
            async def decide(self, snapshot, repair=False):
                await asyncio.Event().wait()

        c = Coordinator(
            self.j, self.clock, Slow(), lambda: self.permissions, Limits(timeout_seconds=0.01)
        )
        self.assertEqual((await c.step("a")).error.category, "timeout")
        await asyncio.sleep(0)
        self.assertEqual((await self.coordinator().step("b")).error.category, "adapter_error")

    async def test_essential_overflow_and_output_cap(self):
        c = self.coordinator(decision, limits=Limits(context=ContextBudget(input_bytes=256)))
        self.assertEqual((await c.step("a")).error.category, "context_overflow")
        self.assertEqual(c.adapter.position, 0)
        c = self.coordinator("x" * 300, limits=Limits(context=ContextBudget(output_bytes=256)))
        self.assertEqual((await c.step("b")).error.category, "output_overflow")

    async def test_cancellation_resistant_adapter_cannot_return_accepted_result(self):
        entered = asyncio.Event()
        release = asyncio.Event()

        class Resistant:
            async def decide(self, snapshot, repair=False):
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                return decision(snapshot)

        c = Coordinator(self.j, self.clock, Resistant(), lambda: self.permissions)
        pending = asyncio.create_task(c.step("resistant"))
        await entered.wait()
        c.cancel()
        self.assertEqual((await asyncio.wait_for(pending, 0.2)).error.category, "cancelled")
        # A late provider result is discarded even if it suppresses cancellation.
        release.set()
        await asyncio.sleep(0)
        self.assertFalse(any(e.kind == "cognition.decision" for e in self.j.events()))

    async def test_demo_is_deterministic(self):
        from jane.cognition.demo import coordinator_demo

        first = await coordinator_demo()
        self.assertEqual(first, await coordinator_demo())
        self.assertEqual([o["status"] for o in json.loads(first)["outcomes"]], ["accepted"] * 3)

    async def test_snapshot_immutability_and_history_budget(self):
        for n in range(10):
            self.j.accept(
                demo_scenario().input.model_copy(
                    update={"event_id": f"history-{n}", "payload": Message(text="x" * 1000)}
                )
            )
        self.goals.review(self.clock.now(), self.permissions)
        snapshot = build_snapshot(
            self.j,
            self.goals.get(self.goal_id),
            self.clock.now(),
            "r",
            "turn",
            self.permissions,
            ContextBudget(input_bytes=6000),
        )
        self.assertLessEqual(len(snapshot.model_dump_json().encode()), 6000)
        self.assertGreater(snapshot.omitted_events, 0)
        self.assertIn("history-9", [e.event_id for e in snapshot.events])
        with self.assertRaises(ValidationError):
            snapshot.goal.budget = 100
        parsed = json.loads(snapshot.world_json)
        parsed["self"]["x"] = "changed"
        self.assertNotIn("x", json.loads(snapshot.world_json)["self"])

    async def test_usage_spoofing_rejected(self):
        c = self.coordinator(lambda s: decision(s, usage={"calls": 0}), "{}")
        self.assertEqual((await c.step("a")).error.category, "invalid_output")

    async def test_restart_recovers_abandoned_request(self):
        self.j.db.execute(
            "INSERT INTO cognitive_requests(task,interaction,status,calls) "
            "VALUES(?,'old','active',1)",
            (self.goal_id,),
        )
        c = self.coordinator(decision)
        c.startup()
        c.startup()
        errors = [e for e in self.j.events() if e.kind == "cognition.error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].payload.category, "cancelled")
        self.assertEqual((await c.step("a")).status, "accepted")

    async def test_atomic_decision_journal_and_request_update(self):
        self.j.db.execute(
            "CREATE TRIGGER reject_accept BEFORE UPDATE ON cognitive_requests "
            "WHEN NEW.status='accepted' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with self.assertRaises(sqlite3.Error):
            await self.coordinator(decision).step("a")
        self.assertFalse(any(e.kind == "cognition.decision" for e in self.j.events()))
        self.assertEqual(
            self.j.db.execute("SELECT status FROM cognitive_requests").fetchone()[0], "active"
        )
