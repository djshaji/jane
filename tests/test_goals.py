import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from jane.motivation.goals import Goal, Goals, Profile
from jane.runner import demo_scenario, run
from jane.storage.journal import Journal


class GoalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "goals.db"
        self.j = Journal(self.path)
        self.g = Goals(self.j)
        self.now = demo_scenario().start

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def add(self, id="task-001", **kw):
        goal = Goal(
            goal_id=id,
            description="inspect",
            scope=("mock.inspect",),
            success_event_kind="tool.observation",
            created_at=self.now,
            **kw,
        )
        self.g.add(goal)
        return goal

    def review(self, seconds=0):
        return self.g.review(self.now + timedelta(seconds=seconds), ("mock.inspect",))

    def test_idle_scope_and_budget(self):
        self.assertIsNone(self.review()["selected_goal"])
        self.add(budget=1)
        self.assertIsNone(self.g.review(self.now, ())["selected_goal"])
        self.review()
        goal = self.g.get("task-001")
        self.g.authorize(goal.goal_id, goal.generation, "mock.inspect", self.now)
        self.assertIsNone(self.review(1)["selected_goal"])
        with self.assertRaises(ValueError):
            self.g.authorize(goal.goal_id, goal.generation, "mock.inspect", self.now)

    def test_cancel_fences_previous_generation(self):
        self.add()
        self.review()
        before = self.g.get("task-001")
        after = self.g.change(before.goal_id, "cancel", self.now)
        self.assertGreater(after.generation, before.generation)
        with self.assertRaises(ValueError):
            self.g.authorize(before.goal_id, before.generation, "mock.inspect", self.now)
        self.assertIsNone(self.review()["selected_goal"])

    def test_wait_timer_and_event(self):
        self.add()
        self.g.change("task-001", "wait", self.now, wake_at=self.now + timedelta(seconds=5))
        self.assertIsNone(self.review(4)["selected_goal"])
        self.assertEqual(self.review(5)["selected_goal"], "task-001")
        observation = run(demo_scenario()).events[2]
        self.j.accept(observation)
        self.g.change(
            "task-001", "wait", self.now + timedelta(seconds=5), wake_source="mock.inspect"
        )
        self.assertIsNone(self.review(6)["selected_goal"])
        self.j.accept(observation.model_copy(update={"event_id": "new-observation"}))
        self.assertEqual(self.review(7)["selected_goal"], "task-001")

    def test_dependencies_completion_and_restart(self):
        self.add()
        self.add("second", dependencies=("task-001",))
        self.assertEqual(self.review()["selected_goal"], "task-001")
        with self.assertRaises(ValueError):
            self.g.change("task-001", "complete", self.now, evidence_id="missing")
        event = run(demo_scenario()).events[2]
        self.j.accept(event)
        self.g.change("task-001", "complete", self.now, evidence_id=event.event_id)
        self.assertEqual(self.review()["selected_goal"], "second")
        self.j.close()
        self.j = Journal(self.path)
        self.g = Goals(self.j)
        self.assertEqual(self.g.get("task-001").status, "completed")
        self.assertEqual(self.review()["selected_goal"], "second")

    def test_priority_preempts_and_hysteresis(self):
        self.add("old")
        self.review()
        self.add("new", deadline=self.now + timedelta(seconds=100))
        self.assertEqual(self.review(1)["selected_goal"], "old")
        self.assertEqual(self.review(11)["selected_goal"], "new")
        self.add("urgent", priority=0)
        self.assertEqual(self.review(12)["selected_goal"], "urgent")

    def test_recovery_and_inclination(self):
        self.add(drive="uncertainty")
        self.assertEqual(self.review()["template"], "investigate")
        self.g.record_failure("task-001", self.now)
        result = self.g.review(self.now, ("mock.inspect",), Profile(patience=0.0, curiosity=1.0))
        self.assertEqual(result["template"], "recover")
        self.assertEqual(result["optional_observations"], 3)

    def test_invalid_dependencies_and_runtime_state(self):
        with self.assertRaises(ValueError):
            self.add(dependencies=("missing",))
        with self.assertRaises(ValueError):
            self.add(status="completed")
        self.assertEqual(self.g.list(), ())
