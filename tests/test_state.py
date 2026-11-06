import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from jane.core.contracts import EVENT
from jane.runner import demo_scenario, run
from jane.storage.journal import DDL, Journal


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.db"
        self.store = Journal(self.path)
        self.input = demo_scenario().input
        self.now = self.input.received_at
        self.store.accept(self.input)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def event(self, id, kind, provenance, payload, offset=0):
        body = self.input.model_dump(mode="json")
        stamp = (self.now + timedelta(seconds=offset)).isoformat()
        body.update(
            event_id=id,
            kind=kind,
            provenance_class=provenance,
            payload=payload,
            occurred_start=stamp,
            occurred_end=stamp,
        )
        return EVENT.validate_json(json.dumps(body))

    def claim(self, id, value, **updates):
        payload = dict(
            entity_id="cup",
            predicate="colour",
            value=value,
            basis="user_reported",
            evidence_ids=[self.input.event_id],
            valid_from=self.now.isoformat(),
        )
        payload.update(updates)
        return self.event(id, "state.claim", "interpretation", payload)

    def test_conflict_correction_and_rebuild(self):
        self.store.accept(self.claim("red", "red"))
        self.store.accept(self.claim("blue", "blue"))
        self.assertTrue(self.store.world(self.now)["claims"]["red"]["conflict"])
        self.store.accept(self.claim("correction", "blue", supersedes="red"))
        state = self.store.world(self.now)
        self.assertEqual(state["claims"]["red"]["status"], "superseded")
        self.assertFalse(state["claims"]["blue"]["conflict"])
        before = self.store.status()
        self.store.rebuild()
        self.assertEqual(self.store.world(self.now), state)
        self.assertEqual(self.store.status(), before)
        self.store.close()
        self.store = Journal(self.path)
        self.assertEqual(self.store.world(self.now), state)

    def test_bad_evidence_rolls_back_everything(self):
        before = self.store.status()
        with self.assertRaises(ValueError):
            self.store.accept(self.claim("bad", "red", evidence_ids=["missing"]))
        with self.assertRaises(ValueError):
            self.store.accept(self.claim("bad", "red", basis="observed"))
        self.assertEqual(self.store.status(), before)

    def test_stale_claim_and_snapshot(self):
        self.store.accept(
            self.claim("red", "red", valid_until=(self.now + timedelta(seconds=5)).isoformat())
        )
        observation = run(demo_scenario()).events[2]
        self.store.accept(observation)
        state = self.store.world(self.now + timedelta(seconds=10))
        self.assertEqual(state["claims"]["red"]["status"], "stale")
        self.assertEqual(state["claims"][observation.event_id]["status"], "stale")

    def test_late_health_does_not_replace_current(self):
        for id, value, offset in [("new", "ready", 10), ("old", "offline", 2)]:
            self.store.accept(
                self.event(
                    id,
                    "state.self",
                    "runtime_status",
                    dict(category="source_health", key="camera", value=value),
                    offset,
                )
            )
        state = self.store.world(self.now)
        self.assertEqual(state["self"]["source_health/camera"]["value"], "ready")
        self.assertEqual([e.event_id for e in self.store.timeline()], ["user-001", "old", "new"])
        self.assertEqual([e.event_id for e in self.store.events()], ["user-001", "new", "old"])
        self.assertEqual(len(state["episodes"]), 1)

    def test_prediction_expiry_and_late_evidence(self):
        prediction = self.event(
            "prediction",
            "state.prediction",
            "prediction",
            dict(
                expected_kind="tool.observation",
                source_id="mock.inspect",
                not_before=self.now.isoformat(),
                deadline=(self.now + timedelta(seconds=5)).isoformat(),
                evidence_ids=["user-001"],
            ),
        )
        self.store.accept(prediction)
        later = self.now + timedelta(seconds=10)
        self.assertEqual(self.store.world(later)["predictions"]["prediction"]["status"], "expired")
        self.store.accept(run(demo_scenario()).events[2])
        self.assertEqual(
            self.store.world(later)["predictions"]["prediction"]["status"], "supported"
        )

    def test_migrate_v1_retains_queue(self):
        path = Path(self.temp.name) / "old.db"
        db = sqlite3.connect(path)
        for statement in DDL:
            db.execute(statement)
        db.execute("PRAGMA user_version=1")
        db.execute(
            "INSERT INTO events(event_id,source_id,body) VALUES(?,?,?)",
            (self.input.event_id, self.input.source_id, self.input.model_dump_json()),
        )
        db.execute(
            "INSERT INTO outbox(work_id,status,eligible) VALUES(?,'pending',0)",
            (self.input.event_id,),
        )
        db.commit()
        db.close()
        migrated = Journal(path)
        try:
            self.assertEqual(migrated.status()["schema_version"], 7)
            self.assertEqual(migrated.status()["outbox"], {"pending": 1})
            self.assertEqual(len(migrated.world(self.now)["episodes"]), 1)
        finally:
            migrated.close()
