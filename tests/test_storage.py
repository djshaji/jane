import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jane.runner import demo_scenario, run
from jane.storage.journal import Journal


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.db"
        self.store = Journal(self.path)
        self.event = demo_scenario().input
        self.now = self.event.received_at.timestamp()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_reopen_and_deduplicate(self):
        seq = self.store.accept(self.event)
        self.store.close()
        self.store = Journal(self.path)
        self.assertEqual(self.store.accept(self.event), seq)
        self.assertEqual(self.store.status()["events"], 1)
        self.assertEqual(self.store.status()["outbox"], {"pending": 1})
        self.assertEqual(self.store.status()["checkpoints"], {"accepted": seq})
        self.assertEqual(self.store.status()["tasks"], {self.event.task_id: seq})

    def test_source_dedupe_and_conflict(self):
        event = self.event.model_copy(update={"source_event_id": "source-1"})
        seq = self.store.accept(event)
        retry = event.model_copy(update={"event_id": "another-id"})
        self.assertEqual(self.store.accept(retry), seq)
        with self.assertRaises(ValueError):
            self.store.accept(event.model_copy(update={"task_id": "different"}))

    def test_atomic_rollback_on_queue_failure(self):
        self.store.db.execute(
            "CREATE TRIGGER reject_work BEFORE INSERT ON outbox "
            "BEGIN SELECT RAISE(ABORT,'injected storage error'); END"
        )
        with self.assertRaises(sqlite3.Error):
            self.store.accept(self.event)
        self.assertEqual(self.store.status()["events"], 0)
        self.assertEqual(self.store.status()["tasks"], {})
        self.assertEqual(self.store.status()["checkpoints"], {})

    def test_lease_recovery_fences_old_worker(self):
        self.store.accept(self.event)
        old = self.store.claim(self.now, 5)
        self.assertIsNotNone(old)
        other = Journal(self.path)
        try:
            self.assertIsNone(other.claim(self.now + 1))
            recovered = other.claim(self.now + 5)
            self.assertEqual(recovered.attempts, 2)
            with self.assertRaises(ValueError):
                self.store.settle(old, self.now + 6)
            other.settle(recovered, self.now + 6)
            self.assertIsNone(self.store.claim(self.now + 7))
        finally:
            other.close()

    def test_retry_delay_and_limit(self):
        self.store.accept(self.event)
        lease = self.store.claim(self.now, max_attempts=2)
        self.store.settle(lease, self.now, retry_after=10)
        self.assertIsNone(self.store.claim(self.now + 9, max_attempts=2))
        lease = self.store.claim(self.now + 10, lease_seconds=1, max_attempts=2)
        self.assertEqual(lease.attempts, 2)
        self.assertIsNone(self.store.claim(self.now + 11, max_attempts=2))
        self.assertEqual(self.store.status()["outbox"], {"failed": 1})

    def test_late_occurrence_preserves_ingest_order(self):
        events = run(demo_scenario()).events
        self.store.accept(events[-1])
        self.store.accept(events[0])
        self.assertEqual(self.store.events(), (events[-1], events[0]))

    def test_readonly_storage_never_acknowledges(self):
        self.store.db.execute("PRAGMA query_only=ON")
        with self.assertRaises(sqlite3.Error):
            self.store.accept(self.event)
        self.assertEqual(self.store.events(), ())

    def test_future_schema_rejected(self):
        self.store.db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(ValueError, "unsupported database"):
            Journal(self.path)

    def test_crash_before_and_after_commit(self):
        script = """
import os,sys
from pathlib import Path
from jane.storage.journal import Journal
from jane.runner import demo_scenario
s=Journal(Path(sys.argv[1]))
if sys.argv[2]=='before':
    with s.transaction():
        s.db.execute("INSERT INTO profiles VALUES('test',1,'{}')")
        os._exit(17)
s.accept(demo_scenario().input)
os._exit(17)
"""
        for phase in ["before", "after"]:
            result = subprocess.run(
                [sys.executable, "-c", script, str(self.path), phase], env=os.environ.copy()
            )
            self.assertEqual(result.returncode, 17)
            self.assertEqual(
                self.store.db.execute("SELECT count(*) FROM profiles").fetchone()[0], 0
            )
        self.assertEqual(self.store.accept(self.event), 1)
        self.assertEqual(self.store.status()["events"], 1)

    def test_invalid_time_rejected(self):
        for value in [float("nan"), float("inf"), True]:
            with self.assertRaises(ValueError):
                self.store.claim(value)
