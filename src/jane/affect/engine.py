"""Internal reducers run inside the caller's journal transaction; no model tools."""

import json
import math
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, Self

from pydantic import Field, model_validator

from jane.core.contracts import Contract, Event

if TYPE_CHECKING:
    from jane.storage.journal import Journal

RULESET = "affect-rules-v1"


class Component(Contract):
    lower: float = 0.0
    upper: float = 1.0
    baseline: float = 0.2
    recovery_seconds: Annotated[float, Field(gt=0)] = 60.0

    @model_validator(mode="after")
    def bounds(self) -> Self:
        if not self.lower <= self.baseline <= self.upper or self.lower >= self.upper:
            raise ValueError("invalid affect bounds/baseline")
        return self


class AffectConfig(Contract):
    computation: bool = True
    behaviour: bool = True
    expressive_tone: bool = False
    valence: Component = Component(lower=-1.0, baseline=0.0)
    activation: Component = Component(recovery_seconds=30.0)
    interest: Component = Component(recovery_seconds=120.0)
    satisfaction: Component = Component(recovery_seconds=180.0)
    frustration: Component = Component(baseline=0.0, recovery_seconds=120.0)
    window_seconds: Annotated[float, Field(gt=0)] = 60.0
    contribution_cap: Annotated[float, Field(gt=0, le=1)] = 0.3
    max_recovery_seconds: Annotated[float, Field(gt=0)] = 3600.0
    stale_seconds: Annotated[float, Field(ge=0)] = 30.0
    interest_threshold: Annotated[float, Field(ge=0, le=1)] = 0.28
    frustration_threshold: Annotated[float, Field(ge=0, le=1)] = 0.2

    @model_validator(mode="after")
    def canonical_ranges(self) -> Self:
        for name, component in self.components().items():
            if component.lower < (-1 if name == "valence" else 0) or component.upper > 1:
                raise ValueError("component bounds exceed canonical range")
        return self

    def components(self) -> dict[str, Component]:
        return {
            name: getattr(self, name)
            for name in ("valence", "activation", "interest", "satisfaction", "frustration")
        }


class Affect:
    def __init__(self, journal: "Journal") -> None:
        self.journal = journal
        self.db = journal.db

    def config(self) -> AffectConfig:
        row = self.db.execute("SELECT body FROM affect_config WHERE id=1").fetchone()
        return AffectConfig.model_validate_json(row[0]) if row else AffectConfig()

    def state(self, now: datetime, *, recovery: bool = False) -> dict[str, Any]:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("timezone-aware time required")
        config = self.config()
        row = self.db.execute("SELECT body FROM affect_state WHERE id=1").fetchone()
        old = json.loads(row[0]) if row else None
        elapsed = (now - datetime.fromisoformat(old["at"])).total_seconds() if old else 0.0
        dt = max(0.0, elapsed)
        if recovery:
            dt = min(dt, config.max_recovery_seconds)
        values = {}
        for name, component in config.components().items():
            prior = old["values"][name] if old else component.baseline
            values[name] = (
                component.baseline
                + (prior - component.baseline) * math.exp(-dt / component.recovery_seconds)
                if config.computation
                else prior
            )
        return {
            "at": old["at"] if old and elapsed < 0 else now.isoformat(),
            "values": values,
            "clock_anomaly": elapsed < 0,
        }

    def _save(
        self,
        state: dict[str, Any],
        evidence: str,
        rule: str,
        before: dict[str, Any],
        delta: dict[str, float],
    ) -> None:
        self.db.execute(
            "INSERT INTO affect_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
            (json.dumps(state, sort_keys=True),),
        )
        self.db.execute(
            "INSERT INTO affect_transitions(evidence,rule,body) VALUES(?,?,?)",
            (
                evidence,
                rule,
                json.dumps(
                    {
                        "before": before,
                        "after": state,
                        "delta": delta,
                        "config": self.config().model_dump(),
                    },
                    sort_keys=True,
                ),
            ),
        )

    def configure(self, config: AffectConfig, now: datetime) -> None:
        config = AffectConfig.model_validate_json(config.model_dump_json())
        with self.journal.transaction():
            before = self.state(now)
            self.db.execute(
                "INSERT INTO affect_config VALUES(1,"
                "?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (config.model_dump_json(),),
            )
            after = dict(
                before,
                values={
                    name: min(c.upper, max(c.lower, before["values"][name]))
                    for name, c in config.components().items()
                },
            )
            self._save(after, "operator", "configure", before, {})

    def reset(self, now: datetime) -> None:
        """Operator-only reset; preserve deduplication and habituation history."""
        with self.journal.transaction():
            before = self.state(now)
            after = dict(
                before, values={name: c.baseline for name, c in self.config().components().items()}
            )
            self._save(after, "operator", "reset", before, {})

    def recover(self, now: datetime) -> dict[str, Any]:
        """Call once at startup; capped UTC recovery creates no experience or reward."""
        with self.journal.transaction():
            state = self.state(now, recovery=True)
            self._save(state, "runtime", "recover", state, {})
            return state

    def _apply(
        self,
        evidence: str,
        rule: str,
        source: str,
        task: str,
        now: datetime,
        delta: dict[str, float],
    ) -> None:
        version = f"{RULESET}:{rule}"
        if self.db.execute(
            "SELECT 1 FROM appraisals WHERE evidence=? AND rule=?", (evidence, version)
        ).fetchone():
            return
        config = self.config()
        before = self.state(now)
        # Watermark prevents backward clock movement from opening fresh repetition windows.
        at = datetime.fromisoformat(before["at"])
        key = json.dumps([source, rule, task])
        row = self.db.execute("SELECT * FROM affect_repetition WHERE key=?", (key,)).fetchone()
        fresh = row is None or at.timestamp() - row["start"] >= config.window_seconds
        count = 0 if fresh else row["count"]
        used = {} if fresh else json.loads(row["used"])
        applied = {}
        for name, value in delta.items():
            magnitude = min(
                (0.0 if rule == "stimulation" and count < 2 else abs(value)) / (1 + count),
                max(0.0, config.contribution_cap - used.get(name, 0.0)),
            )
            applied[name] = math.copysign(magnitude, value) if config.computation else 0.0
            used[name] = used.get(name, 0.0) + abs(applied[name])
        values = dict(before["values"])
        for name, value in applied.items():
            c = config.components()[name]
            values[name] = min(c.upper, max(c.lower, values[name] + value))
        self.db.execute("INSERT INTO appraisals VALUES(?,?)", (evidence, version))
        self.db.execute(
            "INSERT INTO affect_repetition VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "start=excluded.start,count=excluded.count,used=excluded.used",
            (
                key,
                at.timestamp() if fresh else row["start"],
                min(count + 1, 1000000),
                json.dumps(used),
            ),
        )
        self._save(dict(before, values=values), evidence, version, before, applied)

    def event(self, event: Event) -> None:
        """Appraise accepted fresh observations; proposals and praise cannot prove progress."""
        config = self.config()
        age = (event.received_at - event.occurred_end).total_seconds()
        if age < 0 or age > config.stale_seconds:
            return
        row = self.db.execute("SELECT body FROM goals WHERE goal_id=?", (event.task_id,)).fetchone()
        goal = json.loads(row[0]) if row else None
        relevant = goal and goal["status"] in {"active", "pending", "waiting"}
        if event.kind == "tool.observation" and relevant:
            # Novelty is exact new object evidence within a task, not arbitrary new event IDs.
            prior = self.db.execute(
                "SELECT 1 FROM affect_seen WHERE task=? AND object=?",
                (event.task_id, event.payload.object_id),
            ).fetchone()
            if prior is None:
                self.db.execute(
                    "INSERT INTO affect_seen VALUES(?,?)", (event.task_id, event.payload.object_id)
                )
                self._apply(
                    "event:" + event.event_id,
                    "novelty",
                    event.source_id,
                    event.task_id,
                    event.received_at,
                    {"interest": 0.1, "activation": 0.05},
                )
        elif event.kind in {"tool.observation", "user.message"} and not relevant:
            # Equivalent irrelevant events share source/task counters even with distinct payloads.
            self._apply(
                "event:" + event.event_id,
                "stimulation",
                event.source_id,
                event.task_id,
                event.received_at,
                {"activation": 0.1},
            )

    def goal_change(self, seq: int, goal: dict[str, Any], now: datetime, reason: str) -> None:
        delta: dict[str, float] = {}
        if reason == "complete":
            row = self.db.execute(
                "SELECT body FROM events WHERE event_id=?", (goal["completion_evidence"],)
            ).fetchone()
            # Only actual observations qualify, never a generated completion statement.
            if row and json.loads(row[0])["kind"] == "tool.observation":
                delta = {"satisfaction": 0.15, "valence": 0.1, "frustration": -0.1}
        elif reason == "attempt_failed" and goal["failures"] >= 2:
            delta = {"frustration": 0.15, "satisfaction": -0.05}
        if delta:
            self._apply(f"goal_change:{seq}", reason, "runtime", goal["goal_id"], now, delta)

    def influence(
        self, now: datetime, optional: int, remaining: int, template: str, selected: bool
    ) -> dict[str, object]:
        config = self.config()
        values = self.state(now)["values"]
        if selected and config.behaviour:
            optional += int(values["interest"] >= config.interest_threshold)
            if values["frustration"] >= config.frustration_threshold:
                template = "recover"
            if values["activation"] >= 0.8:
                optional = 0
        return {
            "optional_observations": min(3, remaining, optional) if selected else 0,
            "template": template,
            "affect_behaviour": config.behaviour,
            "expressive_tone": config.expressive_tone,
        }

    def history(self) -> list[dict[str, Any]]:
        return [
            dict(row) | {"body": json.loads(row["body"])}
            for row in self.db.execute("SELECT * FROM affect_transitions ORDER BY seq")
        ]
