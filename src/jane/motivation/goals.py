"""Bounded goal selection; selection does not dispatch tools or invoke a model."""

import json
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from jane.core.contracts import EVENT, Contract, Identifier, Text
from jane.storage.journal import Journal

Terminal = {"completed", "cancelled", "failed"}


class Goal(Contract):
    goal_id: Identifier
    description: Text
    scope: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    success_event_kind: Literal["tool.observation", "communication.response"]
    created_at: AwareDatetime
    deadline: AwareDatetime | None = None
    priority: Annotated[int, Field(ge=0, le=3)] = 2
    budget: Annotated[int, Field(gt=0)] = 8
    dependencies: tuple[Identifier, ...] = ()
    drive: Literal["commitment", "uncertainty", "continuity"] = "commitment"
    status: Literal[
        "pending", "active", "waiting", "blocked", "completed", "cancelled", "failed"
    ] = "pending"
    generation: Annotated[int, Field(ge=0)] = 0
    spent: Annotated[int, Field(ge=0)] = 0
    failures: Annotated[int, Field(ge=0)] = 0
    wake_at: AwareDatetime | None = None
    wake_source: Identifier | None = None
    wait_cursor: Annotated[int, Field(ge=0)] = 0
    completion_evidence: Identifier | None = None


class Profile(Contract):
    curiosity: Annotated[float, Field(ge=0, le=1)] = 0.5
    patience: Annotated[float, Field(ge=0, le=1)] = 0.5
    switch_margin: Annotated[float, Field(ge=0, le=1)] = 0.2
    minimum_commitment_seconds: Annotated[float, Field(ge=0, le=300)] = 10.0


def aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware time required")


class Goals:
    def __init__(self, journal: Journal) -> None:
        self.journal = journal

    def list(self) -> tuple[Goal, ...]:
        return tuple(
            Goal.model_validate_json(row[0])
            for row in self.journal.db.execute("SELECT body FROM goals ORDER BY goal_id")
        )

    def get(self, goal_id: str) -> Goal:
        row = self.journal.db.execute(
            "SELECT body FROM goals WHERE goal_id=?", (goal_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown goal")
        return Goal.model_validate_json(row[0])

    def _save(self, goal: Goal, now: datetime, reason: str) -> None:
        body = goal.model_dump_json()
        self.journal.db.execute(
            "INSERT INTO goals VALUES(?,?) ON CONFLICT(goal_id) DO UPDATE SET body=excluded.body",
            (goal.goal_id, body),
        )
        self.journal.db.execute(
            "INSERT INTO goal_changes(goal_id,at,reason,body) VALUES(?,?,?,?)",
            (goal.goal_id, now.isoformat(), reason, body),
        )

    def add(self, goal: Goal) -> None:
        goal = Goal.model_validate_json(goal.model_dump_json())
        if (
            goal.status != "pending"
            or goal.generation
            or goal.spent
            or goal.failures
            or goal.wake_at
            or goal.wake_source
            or goal.wait_cursor
            or goal.completion_evidence
        ):
            raise ValueError("new goals must start pending with clean runtime state")
        with self.journal.transaction():
            if self.journal.db.execute(
                "SELECT 1 FROM goals WHERE goal_id=?", (goal.goal_id,)
            ).fetchone():
                raise ValueError("goal already exists")
            for dependency in goal.dependencies:
                self.get(dependency)  # Only existing goals: dependency cycles cannot be introduced.
            self._save(goal, goal.created_at, "created")

    def change(
        self,
        goal_id: str,
        action: str,
        now: datetime,
        *,
        wake_at: datetime | None = None,
        wake_source: str | None = None,
        evidence_id: str | None = None,
    ) -> Goal:
        aware(now)
        with self.journal.transaction():
            goal = self.get(goal_id)
            if goal.status in Terminal:
                raise ValueError("terminal goal cannot be changed")
            data = goal.model_dump()
            if action == "cancel":
                data.update(status="cancelled", generation=goal.generation + 1)
            elif action == "wait":
                if wake_at is None and wake_source is None:
                    raise ValueError("wait requires a timer or source")
                if wake_at is not None:
                    aware(wake_at)
                    if wake_at <= now:
                        raise ValueError("wake time must be in the future")
                cursor = self.journal.db.execute(
                    "SELECT coalesce(max(seq),0) FROM events"
                ).fetchone()[0]
                data.update(
                    status="waiting", wake_at=wake_at, wake_source=wake_source, wait_cursor=cursor
                )
            elif action == "fail":
                data.update(status="failed")
            elif action == "block":
                data.update(status="blocked")
            elif action == "resume" and goal.status in {"blocked", "waiting"}:
                data.update(status="pending", wake_at=None, wake_source=None)
            elif action == "complete":
                event = next((e for e in self.journal.events() if e.event_id == evidence_id), None)
                if (
                    event is None
                    or event.task_id != goal_id
                    or event.kind != goal.success_event_kind
                ):
                    raise ValueError("completion requires matching task and event kind")
                data.update(status="completed", completion_evidence=evidence_id)
            else:
                raise ValueError("invalid lifecycle transition")
            # Every transition invalidates proposals made before it.
            data["generation"] = goal.generation + 1
            updated = Goal.model_validate(data)
            self._save(updated, now, action)
            return updated

    def authorize(self, goal_id: str, generation: int, tool: str, now: datetime) -> Goal:
        """Reserve one step atomically; a future executor still owns actual dispatch."""
        aware(now)
        with self.journal.transaction():
            goal = self.get(goal_id)
            selected = self.journal.db.execute(
                "SELECT selected FROM arbitration WHERE id=1"
            ).fetchone()
            if (
                goal.status != "active"
                or selected is None
                or selected[0] != goal_id
                or goal.generation != generation
                or tool not in goal.scope
                or goal.spent >= goal.budget
            ):
                raise ValueError("action is not eligible")
            if any(self.get(d).status != "completed" for d in goal.dependencies):
                raise ValueError("dependencies not completed")
            updated = Goal.model_validate(goal.model_dump() | {"spent": goal.spent + 1})
            self._save(updated, now, "step_reserved")
            return updated

    def record_failure(self, goal_id: str, now: datetime) -> None:
        aware(now)
        with self.journal.transaction():
            goal = self.get(goal_id)
            if goal.status != "active":
                raise ValueError("failure requires active goal")
            self._save(
                Goal.model_validate(goal.model_dump() | {"failures": goal.failures + 1}),
                now,
                "attempt_failed",
            )

    def review(
        self, now: datetime, allowed_tools: tuple[str, ...], profile: Profile | None = None
    ) -> dict[str, object]:
        aware(now)
        profile = Profile.model_validate_json((profile or Profile()).model_dump_json())
        with self.journal.transaction():
            old = self.journal.db.execute("SELECT * FROM arbitration WHERE id=1").fetchone()
            if old and now < datetime.fromisoformat(old["reviewed_at"]):
                raise ValueError("review clock moved backwards")
            all_goals = self.list()
            completed = {g.goal_id for g in all_goals if g.status == "completed"}
            candidates: list[tuple[int, float, str, Goal]] = []
            for goal in all_goals:
                if goal.status == "waiting":
                    wake = goal.wake_at is not None and now >= goal.wake_at
                    if goal.wake_source:
                        wake = wake or any(
                            e.source_id == goal.wake_source and e.task_id == goal.goal_id
                            for row in self.journal.db.execute(
                                "SELECT body FROM events WHERE seq>?", (goal.wait_cursor,)
                            )
                            for e in [EVENT.validate_json(row[0])]
                        )
                    if wake:
                        goal = Goal.model_validate(
                            goal.model_dump()
                            | {"status": "pending", "wake_at": None, "wake_source": None}
                        )
                        self._save(goal, now, "woke")
                eligible = (
                    goal.status in {"pending", "active"}
                    and goal.created_at <= now
                    and set(goal.dependencies) <= completed
                    and goal.spent < goal.budget
                    and set(goal.scope) <= set(allowed_tools)
                )
                age = min(0.3, max(0.0, (now - goal.created_at).total_seconds() / 3600) * 0.05)
                urgency = 0.0
                if eligible:
                    urgency = 0.4 + age
                    if goal.deadline and (goal.deadline - now).total_seconds() <= 300:
                        urgency = 1.0
                    candidates.append((goal.priority, -urgency, goal.goal_id, goal))
                self.journal.db.execute(
                    "INSERT INTO drives VALUES(?,?,?,?,?) "
                    "ON CONFLICT(goal_id) DO UPDATE SET urgency=excluded.urgency,"
                    "evaluated_at=excluded.evaluated_at,next_review=excluded.next_review",
                    (
                        goal.goal_id,
                        goal.drive,
                        urgency,
                        now.isoformat(),
                        (now + timedelta(seconds=5)).isoformat(),
                    ),
                )
            candidates.sort(key=lambda item: item[:3])
            chosen = candidates[0] if candidates else None
            if chosen and old:
                current = next((c for c in candidates if c[2] == old["selected"]), None)
                if current and current[0] == chosen[0] and current[2] != chosen[2]:
                    held = (now - datetime.fromisoformat(old["selected_at"])).total_seconds()
                    if (
                        held < profile.minimum_commitment_seconds
                        or current[1] - chosen[1] < profile.switch_margin
                    ):
                        chosen = current
            selected = chosen[2] if chosen else None
            for goal in self.list():
                if goal.status in {"pending", "active"}:
                    status = "active" if goal.goal_id == selected else "pending"
                    if status != goal.status:
                        self._save(
                            Goal.model_validate(
                                goal.model_dump()
                                | {"status": status, "generation": goal.generation + 1}
                            ),
                            now,
                            "selection_changed",
                        )
            template = "rest"
            if selected:
                goal = self.get(selected)
                template = (
                    "recover"
                    if goal.failures >= 1 + int(profile.patience * 2)
                    else "investigate"
                    if goal.drive == "uncertainty"
                    else "continue"
                )
            report: dict[str, object] = {
                "selected_goal": selected,
                "template": template,
                "reason": "eligible priority and urgency" if selected else "no eligible goals",
                "next_review": (now + timedelta(seconds=5)).isoformat(),
                "optional_observations": min(3, int(profile.curiosity * 3)) if selected else 0,
            }
            selected_at = (
                old["selected_at"] if old and old["selected"] == selected else now.isoformat()
            )
            self.journal.db.execute(
                "INSERT INTO arbitration VALUES(1,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET selected=excluded.selected,"
                "selected_at=excluded.selected_at,"
                "reviewed_at=excluded.reviewed_at,body=excluded.body,profile=excluded.profile",
                (
                    selected,
                    selected_at,
                    now.isoformat(),
                    json.dumps(report),
                    profile.model_dump_json(),
                ),
            )
            return report
