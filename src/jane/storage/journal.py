"""Transactional journal. Queue delivery is at least once, not external exactly once."""

import math
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from jane.core.contracts import EVENT, Event

DDL = (
    "CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL, "
    "source_id TEXT NOT NULL, source_event_id TEXT, body TEXT NOT NULL, "
    "UNIQUE(source_id,source_event_id))",
    "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, "
    "last_seq INTEGER NOT NULL REFERENCES events(seq))",
    "CREATE TABLE checkpoints (name TEXT PRIMARY KEY, seq INTEGER NOT NULL REFERENCES events(seq))",
    "CREATE TABLE profiles (profile_id TEXT NOT NULL, version INTEGER NOT NULL CHECK(version>0), "
    "body TEXT NOT NULL, PRIMARY KEY(profile_id,version))",
    "CREATE TABLE outbox (work_id TEXT PRIMARY KEY REFERENCES events(event_id), "
    "status TEXT NOT NULL CHECK(status IN ('pending','leased','done','failed')), "
    "attempts INTEGER NOT NULL DEFAULT 0, eligible REAL NOT NULL, lease_until REAL, token TEXT)",
    "CREATE INDEX outbox_ready ON outbox(status,eligible)",
)


@dataclass(frozen=True)
class Lease:
    work_id: str
    token: str
    event: Event
    attempts: int


def timestamp(value: float) -> None:
    if isinstance(value, bool) or not math.isfinite(value):
        raise ValueError("time must be finite")


class Journal:
    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path, isolation_level=None, timeout=1)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.transaction():
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise ValueError(f"unsupported database version: {version}")
                if version == 0:
                    for statement in DDL:
                        self.db.execute(statement)
                    self.db.execute("PRAGMA user_version=1")
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self.db.close()

    def accept(self, event: Event) -> int:
        event = EVENT.validate_json(event.model_dump_json())
        body = event.model_dump_json()
        with self.transaction():
            rows = self.db.execute(
                "SELECT seq,body FROM events WHERE event_id=? OR "
                "(source_id=? AND source_event_id=?)",
                (event.event_id, event.source_id, event.source_event_id),
            ).fetchall()
            if rows:
                if len(rows) != 1:
                    raise ValueError("conflicting event identities")
                old = EVENT.validate_json(rows[0]["body"])
                # Transport receipt time and newly assigned IDs may change on redelivery.
                excluded = {"received_at", "event_id"}
                if old.model_dump(exclude=excluded) != event.model_dump(exclude=excluded):
                    raise ValueError("deduplication key reused for different content")
                return int(rows[0]["seq"])
            cursor = self.db.execute(
                "INSERT INTO events(event_id,source_id,source_event_id,body) VALUES(?,?,?,?)",
                (event.event_id, event.source_id, event.source_event_id, body),
            )
            assert cursor.lastrowid is not None
            seq = cursor.lastrowid
            self.db.execute(
                "INSERT INTO tasks VALUES(?,?) ON CONFLICT(task_id) "
                "DO UPDATE SET last_seq=excluded.last_seq",
                (event.task_id, seq),
            )
            self.db.execute(
                "INSERT INTO checkpoints VALUES('accepted',?) "
                "ON CONFLICT(name) DO UPDATE SET seq=excluded.seq",
                (seq,),
            )
            self.db.execute(
                "INSERT INTO outbox(work_id,status,eligible) VALUES(?,'pending',?)",
                (event.event_id, event.received_at.timestamp()),
            )
        return seq

    def events(self) -> tuple[Event, ...]:
        return tuple(
            EVENT.validate_json(row[0])
            for row in self.db.execute("SELECT body FROM events ORDER BY seq")
        )

    def status(self) -> dict[str, object]:
        return {
            "schema_version": self.db.execute("PRAGMA user_version").fetchone()[0],
            "events": self.db.execute("SELECT count(*) FROM events").fetchone()[0],
            "checkpoints": dict(self.db.execute("SELECT name,seq FROM checkpoints")),
            "tasks": dict(self.db.execute("SELECT task_id,last_seq FROM tasks")),
            "outbox": dict(self.db.execute("SELECT status,count(*) FROM outbox GROUP BY status")),
        }

    def claim(self, now: float, lease_seconds: float = 30, max_attempts: int = 3) -> Lease | None:
        timestamp(now)
        timestamp(lease_seconds)
        if lease_seconds <= 0 or type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("positive lease and attempt limit required")
        timestamp(now + lease_seconds)
        with self.transaction():
            self.db.execute(
                "UPDATE outbox SET status='pending',token=NULL,lease_until=NULL "
                "WHERE status='leased' AND lease_until<=?",
                (now,),
            )
            self.db.execute(
                "UPDATE outbox SET status='failed' WHERE status='pending' AND attempts>=?",
                (max_attempts,),
            )
            row = self.db.execute(
                "SELECT o.work_id,o.attempts,e.body FROM outbox o JOIN events e "
                "ON e.event_id=o.work_id WHERE o.status='pending' AND o.eligible<=? "
                "ORDER BY e.seq LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            self.db.execute(
                "UPDATE outbox SET status='leased',attempts=attempts+1,token=?,lease_until=? "
                "WHERE work_id=?",
                (token, now + lease_seconds, row["work_id"]),
            )
            lease = Lease(
                row["work_id"], token, EVENT.validate_json(row["body"]), row["attempts"] + 1
            )
        return lease

    def settle(self, lease: Lease, now: float, retry_after: float | None = None) -> None:
        timestamp(now)
        if retry_after is not None:
            timestamp(retry_after)
            if retry_after < 0:
                raise ValueError("retry delay must be nonnegative")
        eligible = now + (retry_after or 0)
        timestamp(eligible)
        with self.transaction():
            cursor = self.db.execute(
                "UPDATE outbox SET status=?,eligible=?,token=NULL,lease_until=NULL "
                "WHERE work_id=? AND token=? AND status='leased' AND lease_until>?",
                (
                    "done" if retry_after is None else "pending",
                    eligible,
                    lease.work_id,
                    lease.token,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("expired or superseded lease")
