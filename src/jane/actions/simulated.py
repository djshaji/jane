"""Separate durable simulator: reproduces the external-effect/local-commit crash gap."""

import json
import sqlite3
from pathlib import Path

from jane.actions.registry import SPECS, DefiniteFailure
from jane.core.contracts import InspectArguments, Observation


class SimulatedCrash(BaseException):
    """Deliberately bypass normal error handling, like process loss after an effect."""


class SimulatedInspector:
    spec = SPECS["sim.inspect"]

    def __init__(self, path: Path, modes: tuple[str, ...] = ("succeed",)) -> None:
        if not modes or not set(modes) <= {
            "succeed",
            "fail",
            "timeout",
            "timeout_after_effect",
            "crash",
            "unknown",
        }:
            raise ValueError("invalid simulator modes")
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS control(id INTEGER PRIMARY KEY,body TEXT,position INTEGER)"
        )
        self.db.execute("INSERT OR IGNORE INTO control VALUES(1,?,0)", (json.dumps(modes),))
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS receipts(key TEXT PRIMARY KEY,"
            "arguments TEXT,body TEXT,status TEXT)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS effects(key TEXT PRIMARY KEY,object TEXT)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def execute(self, key: str, arguments: InspectArguments) -> Observation:
        row = self.db.execute(
            "SELECT arguments,body,status FROM receipts WHERE key=?", (key,)
        ).fetchone()
        if row:
            if row[0] != arguments.model_dump_json():
                raise ValueError("idempotency key reused with changed arguments")
            if row[2] == "succeeded":
                return Observation.model_validate_json(row[1])
            if row[2] == "unknown":
                raise TimeoutError("unresolved simulator outcome")
        with self.db:
            body, position = self.db.execute(
                "SELECT body,position FROM control WHERE id=1"
            ).fetchone()
            modes = json.loads(body)
            mode = modes[min(position, len(modes) - 1)]
            self.db.execute("UPDATE control SET position=position+1 WHERE id=1")
            result = Observation(
                object_id=arguments.object_id,
                description=f"Verified simulated object: {arguments.object_id}",
            )
            status = (
                "failed"
                if mode in {"fail", "timeout"}
                else "unknown"
                if mode == "unknown"
                else "succeeded"
            )
            self.db.execute(
                "INSERT OR REPLACE INTO receipts VALUES(?,?,?,?)",
                (key, arguments.model_dump_json(), result.model_dump_json(), status),
            )
            if status == "succeeded":
                self.db.execute(
                    "INSERT OR IGNORE INTO effects VALUES(?,?)", (key, arguments.object_id)
                )
        if mode == "crash":
            raise SimulatedCrash()
        if mode == "fail":
            raise DefiniteFailure("simulated failure before effect")
        if mode in {"timeout", "timeout_after_effect", "unknown"}:
            raise TimeoutError("simulated timeout")
        return result

    def reconcile(self, key: str, arguments: InspectArguments) -> Observation | None:
        row = self.db.execute(
            "SELECT arguments,body,status FROM receipts WHERE key=?", (key,)
        ).fetchone()
        if row is None or row[2] == "unknown":
            return None
        if row[0] != arguments.model_dump_json():
            return None
        if row[2] == "failed":
            raise DefiniteFailure("reconciliation confirms no effect")
        return Observation.model_validate_json(row[1])

    def verify(self, key: str, arguments: InspectArguments, result: Observation) -> bool:
        effect = self.db.execute("SELECT object FROM effects WHERE key=?", (key,)).fetchone()
        if not effect or effect[0] != arguments.object_id:
            return False
        actual = self.reconcile(key, arguments)
        return actual is not None and actual == result and result.object_id == arguments.object_id
