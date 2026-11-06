import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jane.actions.executor import Executor
from jane.actions.registry import Registry
from jane.actions.simulated import SimulatedCrash, SimulatedInspector
from jane.cognition.coordinator import Coordinator, ScriptedCognitiveAdapter
from jane.core.clock import FakeClock
from jane.core.contracts import Message, Observation
from jane.motivation.goals import Goal, Goals
from jane.offline import OfflineRuntime
from jane.runner import demo_scenario
from jane.storage.journal import Journal


def reply(snapshot, kind="call_tool", **kwargs):
    data = {
        key: getattr(snapshot, key)
        for key in ("request_id", "snapshot_id", "task_id", "generation")
    }
    data.update(
        type=kind, decision_id="d-" + snapshot.request_id, summary="Script", evidence_ids=[]
    )
    if kind == "call_tool":
        data.update(tool="sim.inspect", arguments={"object_id": "cup"})
    return json.dumps(data | kwargs)


class ActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "jane.db"
        self.external = Path(self.tmp.name) / "sim.db"
        self.j = Journal(self.path)
        self.clock = FakeClock(demo_scenario().start)
        self.permissions = ("sim.inspect",)
        self.tool = None
        self.goals = Goals(self.j)
        self.goals.add(
            Goal(
                goal_id="task",
                description="Inspect cup",
                target_object_id="cup",
                scope=("sim.inspect",),
                success_event_kind="tool.observation",
                created_at=self.clock.now(),
            )
        )

    def tearDown(self):
        if self.tool:
            self.tool.close()
        self.j.close()
        self.tmp.cleanup()

    def executor(self, modes=("succeed",)):
        if self.tool is None:
            self.tool = SimulatedInspector(self.external, modes)
        return Executor(self.j, self.clock, Registry((self.tool,)), lambda: self.permissions)

    async def propose(self, function=reply):
        c = Coordinator(
            self.j, self.clock, ScriptedCognitiveAdapter((function,)), lambda: self.permissions
        )
        outcome = await c.step("turn")
        self.assertEqual(outcome.status, "accepted")
        return int(outcome.decision.request_id.removeprefix("request-"))

    def row(self, request):
        return self.j.db.execute("SELECT * FROM actions WHERE request=?", (request,)).fetchone()

    def effects(self):
        return self.tool.db.execute("SELECT count(*) FROM effects").fetchone()[0]

    async def test_intent_and_result_are_idempotent(self):
        ex = self.executor()
        request = await self.propose()
        self.assertEqual(ex.prepare(request), "prepared")
        key = self.row(request)["key"]
        self.assertEqual(self.effects(), 0)
        self.assertEqual(ex.prepare(request), "prepared")
        self.assertEqual(self.goals.get("task").spent, 1)
        self.assertEqual(ex.dispatch(request), "succeeded")
        self.assertEqual(ex.apply(request), "succeeded")
        self.assertEqual(self.row(request)["key"], key)
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.goals.get("task").status, "active")
        self.assertEqual(len([e for e in self.j.events() if e.kind == "tool.observation"]), 1)

    async def test_crash_after_external_effect_reconciles_after_restart(self):
        ex = self.executor(("crash",))
        request = await self.propose()
        ex.prepare(request)
        with self.assertRaises(SimulatedCrash):
            ex.dispatch(request)
        self.assertEqual(self.row(request)["status"], "running")
        self.assertEqual(self.effects(), 1)
        self.j.close()
        self.j = Journal(self.path)
        ex = Executor(self.j, self.clock, Registry((self.tool,)), lambda: self.permissions)
        ex.recover()
        ex.recover()
        self.assertEqual(self.row(request)["status"], "succeeded")
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.row(request)["attempts"], 1)

    async def test_unknown_is_not_retried_and_holds_resource(self):
        ex = self.executor(("unknown",))
        request = await self.propose()
        ex.prepare(request)
        self.assertEqual(ex.dispatch(request), "unknown")
        self.assertEqual(ex.dispatch(request, retry=True), "unknown")
        self.assertEqual(ex.reconcile(request), "needs_review")
        other = await self.propose()
        spent = self.goals.get("task").spent
        with self.assertRaises(sqlite3.IntegrityError):
            ex.prepare(other)
        self.assertEqual(self.goals.get("task").spent, spent)
        self.assertEqual(self.effects(), 0)
        self.assertEqual(self.row(request)["attempts"], 1)

    async def test_timeout_after_effect_reconciles_success(self):
        ex = self.executor(("timeout_after_effect",))
        request = await self.propose()
        ex.prepare(request)
        self.assertEqual(ex.dispatch(request), "unknown")
        self.assertEqual(ex.reconcile(request), "succeeded")
        self.assertEqual(self.effects(), 1)

    async def test_timeout_before_effect_confirms_failure_and_allows_same_key_retry(self):
        ex = self.executor(("timeout", "succeed"))
        request = await self.propose()
        ex.prepare(request)
        key = self.row(request)["key"]
        self.assertEqual(ex.dispatch(request), "unknown")
        self.assertEqual(ex.reconcile(request), "failed")
        self.assertEqual(ex.dispatch(request, retry=True), "succeeded")
        self.assertEqual(self.row(request)["key"], key)
        self.assertEqual(self.goals.get("task").spent, 2)
        self.assertEqual(self.effects(), 1)

    async def test_retry_cap_and_failure_appraisal_deduplication(self):
        ex = self.executor(("fail",))
        request = await self.propose()
        ex.prepare(request)
        for _ in range(5):
            ex.dispatch(request, retry=True)
        self.assertEqual(self.row(request)["attempts"], 3)
        self.assertEqual(self.goals.get("task").failures, 3)
        self.assertEqual(self.goals.get("task").spent, 3)
        ex.reconcile(request)
        self.assertEqual(self.goals.get("task").failures, 3)

    async def test_revoked_and_stopped_prepared_actions_do_not_dispatch(self):
        ex = self.executor()
        request = await self.propose()
        ex.prepare(request)
        self.permissions = ()
        self.assertEqual(ex.dispatch(request), "cancelled")
        self.assertEqual(self.effects(), 0)
        self.permissions = ("sim.inspect",)
        other = await self.propose()
        ex.prepare(other)
        ex.control("stop")
        self.assertEqual(ex.dispatch(other), "cancelled")
        self.assertEqual(self.goals.get("task").status, "cancelled")
        self.assertEqual(self.effects(), 0)

    async def test_pause_resume_invalidates_prepared_snapshot(self):
        ex = self.executor()
        request = await self.propose()
        ex.prepare(request)
        ex.control("pause")
        ex.control("resume")
        self.assertEqual(ex.dispatch(request), "cancelled")
        self.assertEqual(self.effects(), 0)

    async def test_superseding_input_invalidates_intent(self):
        ex = self.executor()
        request = await self.propose()
        ex.prepare(request)
        self.j.accept(
            demo_scenario().input.model_copy(
                update={
                    "task_id": "task",
                    "event_id": "supersede",
                    "payload": Message(text="Stop inspecting"),
                }
            )
        )
        self.assertEqual(ex.dispatch(request), "cancelled")
        self.assertEqual(self.effects(), 0)

    async def test_success_after_cancellation_is_recorded_without_goal_completion(self):
        ex = self.executor()
        request = await self.propose()
        ex.prepare(request)
        original = self.tool.execute

        def stopped(key, arguments):
            result = original(key, arguments)
            ex.control("stop")
            return result

        with patch.object(self.tool, "execute", stopped):
            self.assertEqual(ex.dispatch(request), "succeeded")
        self.assertEqual(self.goals.get("task").status, "cancelled")
        self.assertEqual(self.effects(), 1)

    async def test_bad_result_is_needs_review_not_observation(self):
        ex = self.executor()
        request = await self.propose()
        ex.prepare(request)
        with patch.object(
            self.tool, "execute", return_value=Observation(object_id="wrong", description="wrong")
        ):
            self.assertEqual(ex.dispatch(request), "needs_review")
        self.assertFalse(any(e.kind == "tool.observation" for e in self.j.events()))

    async def test_result_storage_failure_recovers_existing_effect(self):
        ex = self.executor()
        request = await self.propose()
        ex.prepare(request)
        self.j.db.execute(
            "CREATE TRIGGER reject_result BEFORE UPDATE ON actions WHEN NEW.status='succeeded' "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with self.assertRaises(sqlite3.Error):
            ex.dispatch(request)
        self.assertEqual(self.effects(), 1)
        self.assertEqual(self.row(request)["status"], "running")
        self.assertFalse(any(e.kind == "tool.observation" for e in self.j.events()))
        self.j.db.execute("DROP TRIGGER reject_result")
        self.assertEqual(ex.reconcile(request), "succeeded")
        self.assertEqual(self.effects(), 1)

    async def test_finish_requires_correct_verified_target(self):
        ex = self.executor()
        request = await self.propose(lambda s: reply(s, arguments={"object_id": "wrong"}))
        ex.apply(request)
        event_id = self.row(request)["evidence"]
        finish = await self.propose(lambda s: reply(s, "finish", evidence_ids=[event_id]))
        with self.assertRaises(ValueError):
            ex.apply(finish)
        self.assertEqual(self.goals.get("task").status, "active")

    async def test_last_tool_step_still_allows_response_and_verified_finish(self):
        ex = self.executor()
        goal = self.goals.get("task")
        with self.j.transaction():
            self.goals._save(
                Goal.model_validate(goal.model_dump() | {"budget": 1}),
                self.clock.now(),
                "test-budget",
            )
        request = await self.propose()
        ex.apply(request)
        evidence = self.row(request)["evidence"]
        request = await self.propose(
            lambda s: reply(s, "respond", text="Cup inspected", evidence_ids=[evidence])
        )
        self.assertEqual(ex.apply(request), "responded")
        request = await self.propose(lambda s: reply(s, "finish", evidence_ids=[evidence]))
        self.assertEqual(ex.apply(request), "completed")
        self.assertEqual(self.goals.get("task").spent, 1)

    async def test_retry_budget_cannot_be_exceeded(self):
        ex = self.executor(("fail",))
        goal = self.goals.get("task")
        with self.j.transaction():
            self.goals._save(
                Goal.model_validate(goal.model_dump() | {"budget": 1}),
                self.clock.now(),
                "test-budget",
            )
        request = await self.propose()
        ex.apply(request)
        with self.assertRaises(ValueError):
            ex.dispatch(request, retry=True)
        self.assertEqual(self.goals.get("task").spent, 1)
        self.assertEqual(self.row(request)["attempts"], 1)

    async def test_migrate_v5_preserves_accepted_proposals(self):
        request = await self.propose()
        for table in ("action_changes", "actions", "executions", "runtime_control"):
            self.j.db.execute(f"DROP TABLE {table}")
        self.j.db.execute("DROP TABLE provider_attempts")
        self.j.db.execute("PRAGMA user_version=5")
        self.j.close()
        self.j = Journal(self.path)
        self.goals = Goals(self.j)
        self.assertEqual(self.j.status()["schema_version"], 7)
        # Runtime-control defaults are preserved when migrating a v5-shaped database.
        self.assertEqual(
            self.j.db.execute(
                "SELECT status FROM cognitive_requests WHERE id=?", (request,)
            ).fetchone()[0],
            "accepted",
        )
        self.assertEqual(self.executor().apply(request), "succeeded")

    async def test_wait_and_fail_lifecycle_is_applied_once(self):
        ex = self.executor()
        request = await self.propose(lambda s: reply(s, "wait", review_after_ms=1000))
        self.assertEqual(ex.apply(request), "waiting")
        generation = self.goals.get("task").generation
        self.assertEqual(ex.apply(request), "waiting")
        self.assertEqual(self.goals.get("task").generation, generation)
        self.clock.advance(1)
        request = await self.propose(lambda s: reply(s, "fail", category="blocked"))
        self.assertEqual(ex.apply(request), "failed")
        self.assertEqual(self.goals.get("task").status, "failed")


class OfflineTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_offline_m1_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jane.db"
            sim = Path(directory) / "sim.db"
            clock = FakeClock(demo_scenario().start)
            j = Journal(path)
            r = OfflineRuntime(j, sim, clock)
            try:
                with patch("socket.socket", side_effect=AssertionError("network forbidden")):
                    r.startup()
                    task = r.submit("Inspect simulated cup", "cup")
                    trace = await r.run()
                self.assertEqual(
                    [x["status"] for x in trace],
                    ["failed", "failed", "succeeded", "responded", "completed"],
                )
                self.assertEqual(trace[2]["template"], "recover")
                self.assertEqual(Goals(j).get(task).status, "completed")
            finally:
                r.close()
                j.close()
            j = Journal(path)
            r = OfflineRuntime(j, sim, clock)
            try:
                r.startup()
                self.assertEqual(await r.run(), [])
                self.assertEqual(Goals(j).get(task).status, "completed")
                self.assertEqual(
                    r.simulator.db.execute("SELECT count(*) FROM effects").fetchone()[0], 1
                )
            finally:
                r.close()
                j.close()

    async def test_pause_resume_stop_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            j = Journal(Path(directory) / "jane.db")
            r = OfflineRuntime(j, Path(directory) / "sim.db", FakeClock(demo_scenario().start))
            try:
                task = r.submit("Inspect cup", "cup")
                r.executor.control("pause")
                self.assertEqual(await r.run(), [])
                with self.assertRaises(ValueError):
                    r.submit("Inspect other", "other")
                r.executor.control("resume")
                await r.run(1)
                r.executor.control("stop")
                self.assertEqual(await r.run(), [])
                self.assertEqual(Goals(j).get(task).status, "cancelled")
            finally:
                r.close()
                j.close()
