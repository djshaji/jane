"""Single-process async cognitive boundary. Accepted proposals do not dispatch effects."""

import asyncio
import json
from typing import Annotated, Callable, Literal, Protocol

from pydantic import Field, ValidationError

from jane.affect.engine import Affect
from jane.cognition.context import (
    ContextBudget,
    ContextOverflow,
    Snapshot,
    build_snapshot,
    digest,
    relevant,
)
from jane.core.clock import Clock
from jane.core.contracts import (
    DECISION,
    CognitiveError,
    CognitiveErrorEvent,
    Contract,
    Decision,
    DecisionEvent,
    Finish,
    RequestObservation,
    ToolCall,
)
from jane.motivation.goals import Goals
from jane.providers.openai import OpenAIAdapter, ProviderError
from jane.storage.journal import Journal


class Limits(Contract):
    max_calls: Annotated[int, Field(ge=1, le=32)] = 8
    formatting_repairs: Annotated[int, Field(ge=0, le=1)] = 1
    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    context: ContextBudget = ContextBudget()
    provider_retries: Annotated[int, Field(ge=0, le=2)] = 1
    retry_delay_seconds: Annotated[float, Field(ge=0, le=5)] = 1.0


class Adapter(Protocol):
    async def decide(self, snapshot: Snapshot, repair: bool = False) -> str: ...


class ScriptedCognitiveAdapter:
    """JSON responses or snapshot-aware fixture callbacks; no live providers."""

    def __init__(self, script: tuple[str | Callable[[Snapshot], str], ...]) -> None:
        self.script = script
        self.position = 0

    async def decide(self, snapshot: Snapshot, repair: bool = False) -> str:
        if self.position == len(self.script):
            raise ValueError("script exhausted")
        step = self.script[self.position]
        self.position += 1
        return step(snapshot) if callable(step) else step


class Outcome(Contract):
    status: Literal["accepted", "idle", "error"]
    decision: Decision | None = None
    error: CognitiveError | None = None


class Coordinator:
    """One application owner; SQLite enforces one outstanding request across instances."""

    def __init__(
        self,
        journal: Journal,
        clock: Clock,
        adapter: Adapter,
        allowed_tools: Callable[[], tuple[str, ...]],
        limits: Limits | None = None,
    ) -> None:
        self.journal = journal
        self.clock = clock
        self.adapter = adapter
        self.allowed_tools = allowed_tools
        self.limits = Limits.model_validate_json((limits or Limits()).model_dump_json())
        self._task: asyncio.Task[str] | None = None
        self._busy = False
        self._cancelled = False
        self._cancel_signal = asyncio.Event()

    def startup(self) -> None:
        """Explicit exclusive startup: recover abandoned requests before accepting new work."""
        if self._busy or (self._task is not None and not self._task.done()):
            raise ValueError("cannot recover while running")
        rows = self.journal.db.execute(
            "SELECT id,task,interaction,calls FROM cognitive_requests WHERE status='active'"
        ).fetchall()
        for row in rows:
            self._error(
                row["id"], row["task"], row["interaction"], "cancelled", row["calls"], 0, 0.0
            )
        Affect(self.journal).recover(self.clock.now())

    def cancel(self) -> None:
        self._cancelled = True
        self._cancel_signal.set()
        if self._task is not None:
            self._task.cancel()

    def _error(
        self,
        request: int,
        task: str,
        interaction: str,
        category: str,
        calls: int,
        output_bytes: int,
        elapsed: float,
    ) -> Outcome:
        payload = CognitiveError.model_validate(
            {
                "request_id": f"request-{request}",
                "category": category,
                "calls": calls,
                "output_bytes": output_bytes,
                "elapsed_seconds": max(0.0, elapsed),
            }
        )
        now = self.clock.now()
        with self.journal.transaction():
            self.journal.accept(
                CognitiveErrorEvent(
                    event_id=f"cognitive-error-{request}",
                    source_id="coordinator",
                    task_id=task,
                    correlation_id=interaction,
                    occurred_start=now,
                    occurred_end=now,
                    received_at=now,
                    kind="cognition.error",
                    provenance_class="runtime_status",
                    payload=payload,
                )
            )
            self.journal.db.execute(
                "UPDATE cognitive_requests SET status='error' WHERE id=?", (request,)
            )
        return Outcome(status="error", error=payload)

    async def step(self, interaction_id: str) -> Outcome:
        if self._busy or (self._task is not None and not self._task.done()):
            raise ValueError("cognitive request already active")
        # Validate IDs before performing any writes.
        from pydantic import TypeAdapter

        from jane.core.contracts import Identifier

        TypeAdapter(Identifier).validate_python(interaction_id)
        self._busy = True
        self._cancelled = False
        self._cancel_signal.clear()
        request = 0
        task_id = ""
        calls = 0
        output_bytes = 0
        start = self.clock.monotonic()
        try:
            if self.journal.db.execute(
                "SELECT 1 FROM cognitive_requests WHERE status='active'"
            ).fetchone():
                raise ValueError(
                    "cognitive request already active; recover only on exclusive startup"
                )
            report = Goals(self.journal).review(self.clock.now(), self.allowed_tools())
            task_id = str(report["selected_goal"] or "")
            if not task_id:
                return Outcome(status="idle")
            goal = Goals(self.journal).get(task_id)
            with self.journal.transaction():
                cursor = self.journal.db.execute(
                    "INSERT INTO cognitive_requests(task,interaction,status,calls) "
                    "VALUES(?,?,'active',0)",
                    (task_id, interaction_id),
                )
                assert cursor.lastrowid is not None
                request = cursor.lastrowid
                try:
                    snapshot = build_snapshot(
                        self.journal,
                        goal,
                        self.clock.now(),
                        f"request-{request}",
                        interaction_id,
                        self.allowed_tools(),
                        self.limits.context,
                    )
                except ContextOverflow:
                    snapshot = None
                if snapshot:
                    self.journal.db.execute(
                        "UPDATE cognitive_requests SET snapshot=? WHERE id=?",
                        (snapshot.model_dump_json(), request),
                    )
            if snapshot is None:
                return self._error(request, task_id, interaction_id, "context_overflow", 0, 0, 0.0)
            decision = None
            category = "invalid_output"
            repairs = 0
            retries = 0
            repair = False
            for attempt in range(1 + self.limits.formatting_repairs + self.limits.provider_retries):
                if self._cancelled:
                    category = "cancelled"
                    break
                with self.journal.transaction():
                    used = self.journal.db.execute(
                        "SELECT coalesce(sum(calls),0) FROM cognitive_requests WHERE interaction=?",
                        (interaction_id,),
                    ).fetchone()[0]
                    if used >= self.limits.max_calls:
                        category = "budget_exhausted"
                        break
                    self.journal.db.execute(
                        "UPDATE cognitive_requests SET calls=calls+1 WHERE id=?", (request,)
                    )
                calls += 1
                if isinstance(self.adapter, OpenAIAdapter):
                    self.adapter.reset_usage()
                    self.journal.db.execute(
                        "INSERT INTO provider_attempts VALUES(?,?,?)",
                        (request, calls, json.dumps(self.adapter.metadata())),
                    )
                self._task = asyncio.create_task(self.adapter.decide(snapshot, repair=repair))
                try:
                    signal = asyncio.create_task(self._cancel_signal.wait())
                    try:
                        done, _ = await asyncio.wait(
                            (self._task, signal),
                            timeout=self.limits.timeout_seconds,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    finally:
                        signal.cancel()
                    if self._cancelled:
                        self._task.cancel()
                        category = "cancelled"
                        break
                    if not done:
                        self._task.cancel()
                        category = "timeout"
                        break
                    raw = self._task.result()
                except TimeoutError:
                    category = "timeout"
                    break
                except asyncio.CancelledError:
                    self._task.cancel()
                    category = "cancelled"
                    break
                except ProviderError as error:
                    category = error.category
                    if (
                        error.category == "provider_malformed"
                        and repairs < self.limits.formatting_repairs
                    ):
                        repairs += 1
                        repair = True
                        continue
                    if error.retryable and retries < self.limits.provider_retries:
                        retries += 1
                        try:
                            await asyncio.wait_for(
                                self._cancel_signal.wait(), self.limits.retry_delay_seconds
                            )
                        except TimeoutError:
                            pass
                        continue
                    break
                except Exception:
                    category = "adapter_error"
                    break
                finally:
                    if isinstance(self.adapter, OpenAIAdapter):
                        self.journal.db.execute(
                            "UPDATE provider_attempts SET metadata=? WHERE request=? AND ordinal=?",
                            (json.dumps(self.adapter.metadata()), request, calls),
                        )
                if self._cancelled:
                    category = "cancelled"
                    break
                if not isinstance(raw, str):
                    category = "invalid_output"
                    break
                output_bytes += len(raw.encode())
                if len(raw.encode()) > snapshot.output_bytes:
                    category = "output_overflow"
                    break
                try:
                    decision = DECISION.validate_json(raw)
                    break
                except ValidationError:
                    if repairs >= self.limits.formatting_repairs:
                        break
                    repairs += 1
                    repair = True
                    continue
            if decision is not None:
                with self.journal.transaction():
                    now = self.clock.now()
                    current = Goals(self.journal).get(task_id)
                    selected = self.journal.db.execute(
                        "SELECT selected FROM arbitration WHERE id=1"
                    ).fetchone()
                    stale = (
                        self._cancelled
                        or current.status != "active"
                        or not selected
                        or selected[0] != task_id
                        or digest(relevant(self.journal, current, now)) != snapshot.record_version
                    )
                    if stale:
                        category = "stale_context"
                    elif any(
                        getattr(decision, key) != getattr(snapshot, key)
                        for key in ("request_id", "snapshot_id", "task_id", "generation")
                    ):
                        category = "invalid_decision"
                    elif not set(decision.evidence_ids) <= {e.event_id for e in snapshot.events}:
                        category = "invalid_decision"
                    elif isinstance(decision, ToolCall) and (
                        decision.tool not in snapshot.available_tools
                        or decision.tool not in self.allowed_tools()
                    ):
                        category = "unauthorized"
                    elif isinstance(decision, RequestObservation):
                        category = "unsupported_observation"
                    elif isinstance(decision, Finish) and not any(
                        e.event_id in decision.evidence_ids
                        and e.kind == current.success_event_kind
                        and e.kind == "tool.observation"
                        for e in snapshot.events
                    ):
                        category = "invalid_decision"
                    elif self.journal.db.execute(
                        "SELECT 1 FROM cognitive_requests WHERE decision_id=?",
                        (decision.decision_id,),
                    ).fetchone():
                        category = "invalid_decision"
                    else:
                        self.journal.accept(
                            DecisionEvent(
                                event_id=f"cognitive-decision-{request}",
                                source_id="coordinator",
                                task_id=task_id,
                                correlation_id=interaction_id,
                                occurred_start=now,
                                occurred_end=now,
                                received_at=now,
                                kind="cognition.decision",
                                provenance_class="model_proposal",
                                payload=decision,
                            )
                        )
                        self.journal.db.execute(
                            "UPDATE cognitive_requests SET status='accepted',decision_id=?,usage=? "
                            "WHERE id=?",
                            (
                                decision.decision_id,
                                json.dumps(
                                    {
                                        "calls": calls,
                                        "output_bytes": output_bytes,
                                        "elapsed_seconds": max(0.0, self.clock.monotonic() - start),
                                    }
                                ),
                                request,
                            ),
                        )
                        return Outcome(status="accepted", decision=decision)
            return self._error(
                request,
                task_id,
                interaction_id,
                category,
                calls,
                output_bytes,
                self.clock.monotonic() - start,
            )
        finally:
            if self._task is not None:
                if self._task.done():
                    if not self._task.cancelled():
                        self._task.exception()
                    self._task = None
                else:
                    # Keep the slot occupied until a cancellation-resistant adapter actually exits.
                    self._task.add_done_callback(
                        lambda task: task.exception() if not task.cancelled() else None
                    )
            self._busy = False
