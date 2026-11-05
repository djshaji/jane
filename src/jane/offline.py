"""Explicit bounded offline application loop with a deterministic policy adapter."""

import json
from pathlib import Path

from jane.actions.executor import Executor
from jane.actions.registry import Registry
from jane.actions.simulated import SimulatedInspector
from jane.cognition.context import Snapshot
from jane.cognition.coordinator import Coordinator
from jane.core.clock import Clock
from jane.core.contracts import Message, UserEvent
from jane.motivation.goals import Goal, Goals
from jane.storage.journal import Journal


class InspectionPolicy:
    """Scripted inspection strategy, not natural-language understanding or an LLM."""

    async def decide(self, snapshot: Snapshot, repair: bool = False) -> str:
        data: dict[str, object] = {
            k: getattr(snapshot, k) for k in ("request_id", "snapshot_id", "task_id", "generation")
        }
        data.update(
            decision_id="decision-" + snapshot.request_id,
            summary="Offline inspection policy",
            evidence_ids=[],
        )
        evidence = next(
            (
                e
                for e in reversed(snapshot.events)
                if e.kind == "tool.observation"
                and e.payload.object_id == snapshot.goal.target_object_id
            ),
            None,
        )
        responded = any(e.kind == "communication.response" for e in snapshot.events)
        if evidence is None:
            data.update(
                type="call_tool",
                tool="sim.inspect",
                arguments={"object_id": snapshot.goal.target_object_id},
            )
        elif not responded:
            data.update(
                type="respond", text=evidence.payload.description, evidence_ids=[evidence.event_id]
            )
        else:
            data.update(type="finish", evidence_ids=[evidence.event_id])
        return json.dumps(data)


class OfflineRuntime:
    def __init__(
        self,
        journal: Journal,
        simulator_path: Path,
        clock: Clock,
        modes: tuple[str, ...] = ("fail", "fail", "succeed"),
    ) -> None:
        self.j = journal
        self.clock = clock
        self.simulator = SimulatedInspector(simulator_path, modes)
        self.registry = Registry((self.simulator,))
        self.executor = Executor(journal, clock, self.registry, self.registry.names)
        self.coordinator = Coordinator(journal, clock, InspectionPolicy(), self.registry.names)

    def close(self) -> None:
        self.simulator.close()

    def startup(self) -> None:
        self.coordinator.startup()
        self.executor.recover()

    def submit(self, text: str, object_id: str) -> str:
        with self.j.transaction():
            if any(
                self.j.db.execute(
                    "SELECT paused,stopped FROM runtime_control WHERE id=1"
                ).fetchone()
            ):
                raise ValueError("runtime is paused or stopped; resume before submission")
            number = self.j.db.execute("SELECT count(*) FROM goals").fetchone()[0] + 1
            while self.j.db.execute(
                "SELECT 1 FROM goals WHERE goal_id=?", (f"offline-{number}",)
            ).fetchone():
                number += 1
            task = f"offline-{number}"
            now = self.clock.now()
            goal = Goal(
                goal_id=task,
                description=text,
                target_object_id=object_id,
                scope=("sim.inspect",),
                success_event_kind="tool.observation",
                created_at=now,
            )
            Goals(self.j).add(goal)
            self.j.accept(
                UserEvent(
                    event_id=f"input-{task}",
                    task_id=task,
                    correlation_id=f"turn-{task}",
                    source_id="cli",
                    occurred_start=now,
                    occurred_end=now,
                    received_at=now,
                    kind="user.message",
                    provenance_class="user_statement",
                    payload=Message(text=text),
                )
            )
            return task

    async def run(self, max_steps: int = 8) -> list[dict[str, object]]:
        if type(max_steps) is not int or not 1 <= max_steps <= 32:
            raise ValueError("max_steps must be between 1 and 32")
        trace: list[dict[str, object]] = []
        for _ in range(max_steps):
            if any(
                self.j.db.execute(
                    "SELECT paused,stopped FROM runtime_control WHERE id=1"
                ).fetchone()
            ):
                break
            # Unresolved effects block additional plans until reconciled or explicitly reviewed.
            if self.j.db.execute(
                "SELECT 1 FROM actions WHERE status IN ('running','unknown','needs_review')"
            ).fetchone():
                break
            prepared = self.j.db.execute(
                "SELECT request FROM actions WHERE status='prepared' ORDER BY request LIMIT 1"
            ).fetchone()
            if prepared:
                status = self.executor.dispatch(prepared[0])
                trace.append({"action": prepared[0], "status": status, "recovered_intent": True})
                continue
            selected = Goals(self.j).review(self.clock.now(), self.registry.names())[
                "selected_goal"
            ]
            if not selected:
                break
            # Resume a committed proposal after a crash before execution preparation.
            pending = self.j.db.execute(
                "SELECT c.id FROM cognitive_requests c LEFT JOIN actions a ON a.request=c.id "
                "LEFT JOIN executions e ON e.request=c.id WHERE c.status='accepted' AND "
                "c.task=? AND a.request IS NULL AND e.request IS NULL ORDER BY c.id LIMIT 1",
                (selected,),
            ).fetchone()
            if pending:
                request = int(pending[0])
            else:
                outcome = await self.coordinator.step(f"turn-{selected}")
                if outcome.status != "accepted":
                    trace.append(outcome.model_dump(mode="json"))
                    break
                assert outcome.decision is not None
                request = int(outcome.decision.request_id.removeprefix("request-"))
            row = self.j.db.execute(
                "SELECT snapshot FROM cognitive_requests WHERE id=?", (request,)
            ).fetchone()
            template = Snapshot.model_validate_json(row[0]).template
            try:
                status = self.executor.apply(request)
            except ValueError as error:
                # Reject stale proposals once, so restart does not retry them forever.
                with self.j.transaction():
                    self.j.db.execute(
                        "INSERT OR IGNORE INTO executions VALUES(?,'rejected')", (request,)
                    )
                trace.append({"request": request, "status": "rejected", "reason": str(error)})
                continue
            trace.append({"request": request, "status": status, "template": template})
            if status in {"unknown", "needs_review", "waiting"}:
                break
        return trace

    def status(self) -> dict[str, object]:
        return {
            "goals": [g.model_dump(mode="json") for g in Goals(self.j).list()],
            "actions": [
                dict(r)
                for r in self.j.db.execute(
                    "SELECT request,task,tool,status,attempts,"
                    "evidence FROM actions ORDER BY request"
                )
            ],
            "control": dict(
                self.j.db.execute("SELECT * FROM runtime_control WHERE id=1").fetchone()
            ),
        }
