"""Transactional journal. Queue delivery is at least once, not external exactly once."""

import json
import math
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from jane.affect.engine import Affect
from jane.core.contracts import EVENT, Event
from jane.state.projection import project, view

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
                if version not in (0, 1, 2, 3, 4, 5, 6, 7, 8):
                    raise ValueError(f"unsupported database version: {version}")
                if version == 0:
                    for statement in DDL:
                        self.db.execute(statement)
                    self.db.execute("PRAGMA user_version=1")
                if version < 2:
                    self.db.execute(
                        "CREATE TABLE projection (id INTEGER PRIMARY KEY CHECK(id=1), "
                        "last_seq INTEGER NOT NULL, body TEXT NOT NULL)"
                    )
                    self._project()
                    self.db.execute("PRAGMA user_version=2")
                if version < 3:
                    self.db.execute(
                        "CREATE TABLE goals(goal_id TEXT PRIMARY KEY,body TEXT NOT NULL)"
                    )
                    self.db.execute(
                        "CREATE TABLE goal_changes(seq INTEGER PRIMARY KEY,goal_id TEXT NOT NULL,"
                        "at TEXT NOT NULL,reason TEXT NOT NULL,body TEXT NOT NULL)"
                    )
                    self.db.execute(
                        "CREATE TABLE drives(goal_id TEXT PRIMARY KEY REFERENCES goals(goal_id),"
                        "drive TEXT NOT NULL,urgency REAL NOT NULL,evaluated_at TEXT NOT NULL,"
                        "next_review TEXT NOT NULL)"
                    )
                    self.db.execute(
                        "CREATE TABLE arbitration(id INTEGER PRIMARY KEY CHECK(id=1),"
                        "selected TEXT,selected_at TEXT NOT NULL,reviewed_at TEXT NOT NULL,"
                        "body TEXT NOT NULL,profile TEXT NOT NULL)"
                    )
                    self.db.execute("PRAGMA user_version=3")
                if version < 4:
                    for statement in (
                        "CREATE TABLE affect_config(id INTEGER PRIMARY KEY "
                        "CHECK(id=1),body TEXT NOT NULL)",
                        "CREATE TABLE affect_state(id INTEGER PRIMARY KEY "
                        "CHECK(id=1),body TEXT NOT NULL)",
                        "CREATE TABLE appraisals(evidence TEXT NOT NULL,"
                        "rule TEXT NOT NULL,PRIMARY KEY(evidence,rule))",
                        "CREATE TABLE affect_transitions(seq INTEGER PRIMARY KEY,"
                        "evidence TEXT NOT NULL,rule TEXT NOT NULL,body TEXT NOT NULL)",
                        "CREATE TABLE affect_repetition(key TEXT PRIMARY KEY,"
                        "start REAL NOT NULL,count INTEGER NOT NULL,used TEXT NOT NULL)",
                        "CREATE TABLE affect_seen(task TEXT NOT NULL,"
                        "object TEXT NOT NULL,PRIMARY KEY(task,object))",
                    ):
                        self.db.execute(statement)
                    self.db.execute("PRAGMA user_version=4")
                if version < 5:
                    self.db.execute(
                        "CREATE TABLE cognitive_requests(id INTEGER PRIMARY KEY,task TEXT NOT NULL,"
                        "interaction TEXT NOT NULL,status TEXT NOT NULL,calls INTEGER NOT NULL,"
                        "snapshot TEXT,decision_id TEXT UNIQUE,usage TEXT)"
                    )
                    self.db.execute(
                        "CREATE UNIQUE INDEX cognitive_single_active ON cognitive_requests(status) "
                        "WHERE status='active'"
                    )
                    self.db.execute("PRAGMA user_version=5")
                if version < 6:
                    self.db.execute(
                        "CREATE TABLE actions(request INTEGER PRIMARY KEY "
                        "REFERENCES cognitive_requests(id),"
                        "task TEXT NOT NULL,generation INTEGER NOT NULL,tool TEXT NOT NULL,"
                        "arguments TEXT NOT NULL,key TEXT UNIQUE NOT NULL,resource TEXT NOT NULL,"
                        "version TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN "
                        "('prepared','running','succeeded','failed','unknown',"
                        "'needs_review','cancelled')),"
                        "attempts INTEGER NOT NULL,result TEXT,evidence TEXT UNIQUE)"
                    )
                    self.db.execute(
                        "CREATE UNIQUE INDEX action_resource_lock ON actions(resource) "
                        "WHERE status IN ('prepared','running','unknown','needs_review')"
                    )
                    self.db.execute(
                        "CREATE TABLE action_changes(seq INTEGER PRIMARY KEY,"
                        "request INTEGER NOT NULL "
                        "REFERENCES actions(request),at TEXT NOT NULL,"
                        "status TEXT NOT NULL,reason TEXT NOT NULL)"
                    )
                    self.db.execute(
                        "CREATE TABLE executions(request INTEGER PRIMARY KEY "
                        "REFERENCES cognitive_requests(id),"
                        "status TEXT NOT NULL)"
                    )
                    self.db.execute(
                        "CREATE TABLE runtime_control(id INTEGER PRIMARY KEY CHECK(id=1),"
                        "paused INTEGER NOT NULL,stopped INTEGER NOT NULL,"
                        "generation INTEGER NOT NULL)"
                    )
                    self.db.execute("INSERT INTO runtime_control VALUES(1,0,0,0)")
                    self.db.execute("PRAGMA user_version=6")
                if version < 7:
                    self.db.execute(
                        "CREATE TABLE provider_attempts(request INTEGER NOT NULL "
                        "REFERENCES cognitive_requests(id),ordinal INTEGER NOT NULL,"
                        "metadata TEXT NOT NULL,"
                        "PRIMARY KEY(request,ordinal))"
                    )
                    self.db.execute("PRAGMA user_version=7")
                if version < 8:
                    self.db.execute(
                        "CREATE TABLE assets(asset_id TEXT PRIMARY KEY,path TEXT NOT NULL UNIQUE,"
                        "body TEXT NOT NULL,state TEXT NOT NULL "
                        "CHECK(state IN ('retained','expired')))"
                    )
                    self.db.execute("PRAGMA user_version=8")
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def transaction(self) -> Iterator[None]:
        nested = self.db.in_transaction
        self.db.execute("SAVEPOINT jane_nested" if nested else "BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("RELEASE jane_nested" if nested else "COMMIT")
        except BaseException:
            if self.db.in_transaction:
                if nested:
                    self.db.execute("ROLLBACK TO jane_nested")
                    self.db.execute("RELEASE jane_nested")
                else:
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
            if event.kind == "sensor.image":
                from jane.storage.media import read_asset

                asset, _ = read_asset(self, event.payload.asset_id)
                if asset.digest != event.payload.digest or asset.quality != event.payload.quality:
                    raise ValueError("image event does not match finalized asset")
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
            self._project()
            Affect(self).event(event)
        return seq

    def events(self) -> tuple[Event, ...]:
        return tuple(
            EVENT.validate_json(row[0])
            for row in self.db.execute("SELECT body FROM events ORDER BY seq")
        )

    def _project(self) -> None:
        state = project(self.events())
        last_seq = self.db.execute("SELECT coalesce(max(seq),0) FROM events").fetchone()[0]
        self.db.execute(
            "INSERT INTO projection VALUES(1,?,?) ON CONFLICT(id) "
            "DO UPDATE SET last_seq=excluded.last_seq,body=excluded.body",
            (last_seq, json.dumps(state, sort_keys=True)),
        )

    def rebuild(self) -> None:
        with self.transaction():
            self._project()

    def world(self, now: datetime) -> dict[str, Any]:
        row = self.db.execute("SELECT body FROM projection WHERE id=1").fetchone()
        return view(json.loads(row[0]), now)

    def timeline(self) -> tuple[Event, ...]:
        # Python's stable sort preserves ingest order for equal occurrence times.
        return tuple(sorted(self.events(), key=lambda event: event.occurred_start))

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
