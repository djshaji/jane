"""Durable offline intents. Unknown external effects are reconciled, never blindly retried."""

import uuid
from datetime import timedelta
from typing import Callable

from jane.actions.registry import DefiniteFailure, Registry
from jane.cognition.context import Snapshot, digest, relevant
from jane.core.clock import Clock
from jane.core.contracts import (
    DECISION,
    Decision,
    Fail,
    Finish,
    InspectArguments,
    Message,
    Observation,
    ObservationEvent,
    Respond,
    ResponseEvent,
    ToolCall,
    Wait,
)
from jane.motivation.goals import Goals, Terminal
from jane.storage.journal import Journal


class Executor:
    def __init__(
        self,
        journal: Journal,
        clock: Clock,
        registry: Registry,
        permissions: Callable[[], tuple[str, ...]],
    ) -> None:
        self.j = journal
        self.clock = clock
        self.registry = registry
        self.permissions = permissions

    def _proposal(self, request: int) -> tuple[Decision, Snapshot]:
        row = self.j.db.execute(
            "SELECT * FROM cognitive_requests WHERE id=?", (request,)
        ).fetchone()
        if row is None or row["status"] != "accepted":
            raise ValueError("execution requires an accepted cognitive request")
        event = next(
            (
                e
                for e in self.j.events()
                if e.kind == "cognition.decision" and e.payload.decision_id == row["decision_id"]
            ),
            None,
        )
        if event is None or event.kind != "cognition.decision":
            raise ValueError("missing accepted decision")
        return DECISION.validate_json(
            event.payload.model_dump_json()
        ), Snapshot.model_validate_json(row["snapshot"])

    def _valid(self, task: str, generation: int, version: str) -> bool:
        control = self.j.db.execute(
            "SELECT paused,stopped FROM runtime_control WHERE id=1"
        ).fetchone()
        goal = Goals(self.j).get(task)
        selected = self.j.db.execute("SELECT selected FROM arbitration WHERE id=1").fetchone()
        return bool(
            not any(control)
            and goal.status == "active"
            and goal.generation == generation
            and selected
            and selected[0] == task
            and digest(relevant(self.j, goal, self.clock.now())) == version
        )

    def _transition(self, request: int, status: str, reason: str) -> None:
        self.j.db.execute("UPDATE actions SET status=? WHERE request=?", (status, request))
        self.j.db.execute(
            "INSERT INTO action_changes(request,at,status,reason) VALUES(?,?,?,?)",
            (request, self.clock.now().isoformat(), status, reason),
        )

    def prepare(self, request: int) -> str:
        with self.j.transaction():
            existing = self.j.db.execute(
                "SELECT status FROM actions WHERE request=?", (request,)
            ).fetchone()
            if existing:
                return str(existing[0])
            decision, snapshot = self._proposal(request)
            if not isinstance(decision, ToolCall):
                raise ValueError("only tool calls create action intents")
            tool = self.registry.get(decision.tool)
            if not self._valid(decision.task_id, decision.generation, snapshot.record_version):
                raise ValueError("stale or stopped decision")
            if tool.spec.permission not in self.permissions():
                raise ValueError("permission denied")
            goal = Goals(self.j).authorize(
                decision.task_id, decision.generation, decision.tool, self.clock.now()
            )
            version = digest(relevant(self.j, goal, self.clock.now()))
            self.j.db.execute(
                "INSERT INTO actions(request,task,generation,tool,arguments,key,"
                "resource,version,status,attempts) "
                "VALUES(?,?,?,?,?,?,?,?,'prepared',0)",
                (
                    request,
                    goal.goal_id,
                    goal.generation,
                    decision.tool,
                    decision.arguments.model_dump_json(),
                    uuid.uuid4().hex,
                    tool.spec.resource(decision.arguments),
                    version,
                ),
            )
            self._transition(request, "prepared", "intent and one budget step committed")
            return "prepared"

    def dispatch(self, request: int, *, retry: bool = False) -> str:
        with self.j.transaction():
            row = self.j.db.execute("SELECT * FROM actions WHERE request=?", (request,)).fetchone()
            if row is None:
                raise ValueError("missing prepared action")
            if row["status"] not in ({"prepared", "failed"} if retry else {"prepared"}):
                return str(row["status"])
            tool = self.registry.get(row["tool"])
            if (
                not self._valid(row["task"], row["generation"], row["version"])
                or tool.spec.permission not in self.permissions()
            ):
                self._transition(request, "cancelled", "dispatch preconditions changed")
                return "cancelled"
            if row["attempts"] >= tool.spec.max_attempts or (retry and not tool.spec.idempotent):
                return str(row["status"])
            if row["attempts"]:
                # Each definite-failure retry consumes a new hard-budget step, retaining its key.
                goal = Goals(self.j).authorize(
                    row["task"], row["generation"], row["tool"], self.clock.now()
                )
                self.j.db.execute(
                    "UPDATE actions SET version=? WHERE request=?",
                    (digest(relevant(self.j, goal, self.clock.now())), request),
                )
            self.j.db.execute("UPDATE actions SET attempts=attempts+1 WHERE request=?", (request,))
            self._transition(request, "running", "dispatch begins")
        arguments = InspectArguments.model_validate_json(row["arguments"])
        # No journal transaction is held across the external operation.
        try:
            result = tool.execute(row["key"], arguments)
        except DefiniteFailure:
            return self._failed(request)
        except Exception:
            with self.j.transaction():
                self._transition(request, "unknown", "tool raised without confirmed outcome")
            return "unknown"
        return self._succeeded(request, result)

    def _failed(self, request: int) -> str:
        with self.j.transaction():
            row = self.j.db.execute("SELECT * FROM actions WHERE request=?", (request,)).fetchone()
            if row["status"] == "failed":
                return "failed"
            self._transition(request, "failed", "tool confirms no successful effect")
            goal = Goals(self.j).get(row["task"])
            if goal.status == "active" and goal.generation == row["generation"]:
                still_current = digest(relevant(self.j, goal, self.clock.now())) == row["version"]
                Goals(self.j).record_failure(goal.goal_id, self.clock.now())
                goal = Goals(self.j).get(row["task"])
                if not still_current:
                    return "failed"
                self.j.db.execute(
                    "UPDATE actions SET version=? WHERE request=?",
                    (digest(relevant(self.j, goal, self.clock.now())), request),
                )
        return "failed"

    def _succeeded(self, request: int, result: Observation) -> str:
        row = self.j.db.execute("SELECT * FROM actions WHERE request=?", (request,)).fetchone()
        tool = self.registry.get(row["tool"])
        arguments = InspectArguments.model_validate_json(row["arguments"])
        try:
            result = Observation.model_validate_json(result.model_dump_json())
            verified = tool.verify(row["key"], arguments, result)
        except Exception:
            verified = False
        with self.j.transaction():
            if not verified:
                self._transition(request, "needs_review", "result postcondition not verified")
                return "needs_review"
            decision, snapshot = self._proposal(request)
            now = self.clock.now()
            event_id = f"action-observation-{request}"
            self.j.accept(
                ObservationEvent(
                    event_id=event_id,
                    source_id=row["tool"],
                    source_event_id=row["key"],
                    occurred_start=now,
                    occurred_end=now,
                    received_at=now,
                    task_id=row["task"],
                    correlation_id=snapshot.interaction_id,
                    kind="tool.observation",
                    provenance_class="tool_result",
                    payload=result,
                )
            )
            self.j.db.execute(
                "UPDATE actions SET result=?,evidence=? WHERE request=?",
                (result.model_dump_json(), event_id, request),
            )
            self._transition(request, "succeeded", "direct postcondition verified")
        return "succeeded"

    def reconcile(self, request: int) -> str:
        row = self.j.db.execute("SELECT * FROM actions WHERE request=?", (request,)).fetchone()
        if row is None:
            raise ValueError("unknown action")
        if row["status"] not in {"running", "unknown", "needs_review"}:
            return str(row["status"])
        tool = self.registry.get(row["tool"])
        try:
            result = tool.reconcile(
                row["key"], InspectArguments.model_validate_json(row["arguments"])
            )
        except DefiniteFailure:
            return self._failed(request)
        except Exception:
            result = None
        if result is not None:
            return self._succeeded(request, result)
        with self.j.transaction():
            self._transition(request, "needs_review", "external outcome cannot be established")
        return "needs_review"

    def recover(self) -> None:
        # Exclusive application startup only; a running live worker must not be stolen.
        for row in self.j.db.execute(
            "SELECT request FROM actions WHERE status IN ('running','unknown')"
        ).fetchall():
            self.reconcile(row[0])

    def apply(self, request: int) -> str:
        decision, snapshot = self._proposal(request)
        if isinstance(decision, ToolCall):
            self.prepare(request)
            return self.dispatch(request)
        with self.j.transaction():
            done = self.j.db.execute(
                "SELECT status FROM executions WHERE request=?", (request,)
            ).fetchone()
            if done:
                return str(done[0])
            if not self._valid(decision.task_id, decision.generation, snapshot.record_version):
                raise ValueError("stale or stopped decision")
            goal = Goals(self.j).get(decision.task_id)
            now = self.clock.now()
            if isinstance(decision, Respond):
                self.j.accept(
                    ResponseEvent(
                        event_id=f"action-response-{request}",
                        source_id="offline.runtime",
                        occurred_start=now,
                        occurred_end=now,
                        received_at=now,
                        task_id=goal.goal_id,
                        correlation_id=snapshot.interaction_id,
                        kind="communication.response",
                        provenance_class="model_response",
                        payload=Message(text=decision.text),
                    )
                )
                status = "responded"
            elif isinstance(decision, Wait):
                Goals(self.j).change(
                    goal.goal_id,
                    "wait",
                    now,
                    wake_at=now + timedelta(milliseconds=decision.review_after_ms),
                )
                status = "waiting"
            elif isinstance(decision, Fail):
                Goals(self.j).change(goal.goal_id, "fail", now)
                status = "failed"
            elif isinstance(decision, Finish):
                evidence = None
                for event_id in decision.evidence_ids:
                    row = self.j.db.execute(
                        "SELECT result FROM actions WHERE evidence=? AND task=? AND "
                        "generation=? AND status='succeeded'",
                        (event_id, goal.goal_id, goal.generation),
                    ).fetchone()
                    if row and goal.target_object_id is not None:
                        result = Observation.model_validate_json(row[0])
                        if result.object_id == goal.target_object_id:
                            evidence = event_id
                if evidence is None:
                    raise ValueError("finish lacks verified evidence for the goal's target object")
                Goals(self.j).change(goal.goal_id, "complete", now, evidence_id=evidence)
                status = "completed"
            else:
                raise ValueError("observation capture not available")
            self.j.db.execute("INSERT INTO executions VALUES(?,?)", (request, status))
            return status

    def control(self, operation: str) -> None:
        if operation not in {"pause", "resume", "stop"}:
            raise ValueError("unknown control")
        with self.j.transaction():
            self.j.db.execute(
                "UPDATE runtime_control SET paused=?,stopped=?,generation=generation+1 WHERE id=1",
                (int(operation == "pause"), int(operation == "stop")),
            )
            if operation == "stop":
                for goal in Goals(self.j).list():
                    if goal.status not in Terminal:
                        Goals(self.j).change(goal.goal_id, "cancel", self.clock.now())
                for row in self.j.db.execute(
                    "SELECT request FROM actions WHERE status='prepared'"
                ).fetchall():
                    self._transition(row[0], "cancelled", "operator stop")
