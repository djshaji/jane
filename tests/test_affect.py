import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from pydantic import ValidationError

from jane.affect.engine import Affect, AffectConfig, Component
from jane.core.contracts import Observation
from jane.motivation.goals import Goal, Goals
from jane.runner import demo_scenario, run
from jane.storage.journal import Journal


class AffectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "affect.db"
        self.j = Journal(self.path)
        self.a = Affect(self.j)
        self.g = Goals(self.j)
        self.now = demo_scenario().start
        self.event = (
            run(demo_scenario())
            .events[2]
            .model_copy(
                update={
                    "received_at": self.now,
                    "occurred_start": self.now,
                    "occurred_end": self.now,
                }
            )
        )

    def tearDown(self):
        self.j.close()
        self.tmp.cleanup()

    def goal(self, **kw):
        self.g.add(
            Goal(
                goal_id=self.event.task_id,
                description="Inspect",
                scope=("mock.inspect",),
                success_event_kind="tool.observation",
                created_at=self.now,
                **kw,
            )
        )
        self.g.review(self.now, ("mock.inspect",))

    def test_duplicate_and_equivalent_novelty(self):
        self.goal()
        self.j.accept(self.event)
        before = self.a.state(self.now)
        self.j.accept(self.event)
        self.j.accept(self.event.model_copy(update={"event_id": "equivalent"}))
        self.assertEqual(self.a.state(self.now), before)
        self.assertAlmostEqual(before["values"]["interest"], 0.3)
        self.assertEqual(len(self.a.history()), 1)

    def test_caps_across_new_objects_and_bounds(self):
        self.goal()
        for n in range(100):
            self.j.accept(
                self.event.model_copy(
                    update={
                        "event_id": f"new-{n}",
                        "payload": Observation(object_id=f"object-{n}", description="new evidence"),
                    }
                )
            )
        self.assertAlmostEqual(self.a.state(self.now)["values"]["interest"], 0.5)
        total = sum(t["body"]["delta"]["interest"] for t in self.a.history())
        self.assertAlmostEqual(total, 0.3)
        for name, value in self.a.state(self.now)["values"].items():
            self.assertGreaterEqual(value, -1 if name == "valence" else 0)
            self.assertLessEqual(value, 1)

    def test_decay_tick_invariance_and_restart_cap(self):
        self.goal()
        self.j.accept(self.event)
        expected = self.a.state(self.now + timedelta(seconds=60))["values"]
        for seconds in range(10, 61, 10):
            self.a.recover(self.now + timedelta(seconds=seconds))
        for name, value in expected.items():
            self.assertAlmostEqual(
                self.a.state(self.now + timedelta(seconds=60))["values"][name], value
            )
        self.a.configure(AffectConfig(max_recovery_seconds=10.0), self.now + timedelta(seconds=60))
        expected = self.a.state(self.now + timedelta(seconds=70))["values"]
        events = self.j.events()
        self.j.close()
        self.j = Journal(self.path)
        self.a = Affect(self.j)
        actual = self.a.recover(self.now + timedelta(days=1))
        for name, value in expected.items():
            self.assertAlmostEqual(actual["values"][name], value)
        self.assertEqual(self.j.events(), events)
        backward = self.a.recover(self.now)
        self.assertTrue(backward["clock_anomaly"])
        self.assertEqual(actual["values"], backward["values"])
        self.assertEqual(actual["at"], backward["at"])

    def test_progress_requires_observed_completion(self):
        self.goal()
        self.j.accept(self.event)
        self.assertEqual(self.a.state(self.now)["values"]["satisfaction"], 0.2)
        self.g.change(self.event.task_id, "complete", self.now, evidence_id=self.event.event_id)
        self.assertAlmostEqual(self.a.state(self.now)["values"]["satisfaction"], 0.35)
        transition = self.a.history()[-1]
        self.assertTrue(transition["evidence"].startswith("goal_change:"))
        seq = int(transition["evidence"].split(":")[1])
        body = self.j.db.execute("SELECT body FROM goal_changes WHERE seq=?", (seq,)).fetchone()[0]
        self.assertEqual(json.loads(body)["completion_evidence"], self.event.event_id)
        with self.j.transaction():
            self.a.goal_change(seq, json.loads(body), self.now, "complete")
        self.assertEqual(len(self.a.history()), 2)

    def test_repeated_failures_modulate_recovery(self):
        self.goal()
        for n in range(3):
            self.g.record_failure(self.event.task_id, self.now)
        # Patience would require 3 failures already; inspect direct affect influence separately.
        result = self.a.influence(self.now, 1, 8, "continue", True)
        self.assertEqual(result["template"], "recover")
        self.assertAlmostEqual(self.a.state(self.now)["values"]["frustration"], 0.225)

    def test_influence_switches_permissions_and_budget(self):
        self.goal(budget=2)
        self.j.accept(self.event)
        world = self.j.world(self.now)
        enabled = self.g.review(self.now, ("mock.inspect",))
        self.assertEqual(enabled["optional_observations"], 2)
        self.a.configure(AffectConfig(behaviour=False, expressive_tone=True), self.now)
        disabled = self.g.review(self.now, ("mock.inspect",))
        self.assertEqual(disabled["optional_observations"], 1)
        self.assertTrue(disabled["expressive_tone"])
        self.assertEqual(self.j.world(self.now), world)
        self.assertIsNone(self.g.review(self.now, ())["selected_goal"])
        self.g.review(self.now, ("mock.inspect",))
        for n in range(2):
            goal = self.g.get(self.event.task_id)
            self.g.authorize(goal.goal_id, goal.generation, "mock.inspect", self.now)
        self.assertIsNone(self.g.review(self.now, ("mock.inspect",))["selected_goal"])

    def test_computation_disabled_does_not_backfill(self):
        self.goal()
        self.a.configure(AffectConfig(computation=False), self.now)
        self.j.accept(self.event)
        self.a.configure(AffectConfig(), self.now)
        self.j.accept(self.event)
        self.assertEqual(self.a.state(self.now)["values"]["interest"], 0.2)

    def test_late_event_and_proposal_do_not_reward(self):
        self.goal()
        self.j.accept(
            self.event.model_copy(update={"received_at": self.now + timedelta(minutes=2)})
        )
        self.j.accept(run(demo_scenario()).events[1])
        self.assertEqual(self.a.history(), [])

    def test_stimulation_burst_and_window_reset(self):
        for n in range(3):
            self.j.accept(self.event.model_copy(update={"event_id": f"burst-{n}"}))
            if n < 2:
                self.assertEqual(self.a.state(self.now)["values"]["activation"], 0.2)
        self.assertGreater(self.a.state(self.now)["values"]["activation"], 0.2)
        later = self.now + timedelta(seconds=61)
        self.j.accept(
            self.event.model_copy(
                update={
                    "event_id": "later",
                    "received_at": later,
                    "occurred_start": later,
                    "occurred_end": later,
                }
            )
        )
        self.assertEqual(self.a.history()[-1]["body"]["delta"]["activation"], 0.0)

    def test_appraisal_failure_rolls_back_event_and_goal(self):
        self.goal()
        self.j.db.execute(
            "CREATE TRIGGER reject_affect BEFORE INSERT ON affect_transitions "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with self.assertRaises(sqlite3.Error):
            self.j.accept(self.event)
        self.assertEqual(self.j.events(), ())
        self.assertEqual(self.j.status()["outbox"], {})
        self.assertEqual(self.j.db.execute("SELECT count(*) FROM appraisals").fetchone()[0], 0)
        self.g.record_failure(self.event.task_id, self.now)
        with self.assertRaises(sqlite3.Error):
            self.g.record_failure(self.event.task_id, self.now)
        self.assertEqual(self.g.get(self.event.task_id).failures, 1)

    def test_reset_preserves_deduplication(self):
        self.goal()
        self.j.accept(self.event)
        self.a.reset(self.now)
        self.j.accept(self.event)
        self.assertEqual(self.a.state(self.now)["values"]["interest"], 0.2)
        self.assertEqual(self.a.history()[-1]["rule"], "reset")

    def test_migrate_v3_without_retroactive_appraisal(self):
        self.j.accept(self.event)
        for table in (
            "affect_config",
            "affect_state",
            "appraisals",
            "affect_transitions",
            "affect_repetition",
            "affect_seen",
        ):
            self.j.db.execute(f"DROP TABLE {table}")
        self.j.db.execute("DROP TABLE cognitive_requests")
        self.j.db.execute("PRAGMA user_version=3")
        self.j.close()
        self.j = Journal(self.path)
        self.a = Affect(self.j)
        self.assertEqual(self.j.status()["schema_version"], 5)
        self.assertEqual(len(self.j.events()), 1)
        self.assertEqual(self.a.history(), [])
        self.j.accept(self.event)
        self.assertEqual(self.a.history(), [])

    def test_config_validation(self):
        for kwargs in (
            {"recovery_seconds": 0.0},
            {"baseline": float("nan")},
            {"lower": 2.0},
            {"baseline": 2.0},
        ):
            with self.assertRaises(ValidationError):
                Component(**kwargs)
        with self.assertRaises(ValidationError):
            AffectConfig(interest=Component(lower=-1.0))
        with self.assertRaises(ValidationError):
            AffectConfig.model_validate({"unknown": True})


if __name__ == "__main__":
    unittest.main()
