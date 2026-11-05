"""Deterministic coordinator demonstration with an explicitly supplied fixture observation."""

import json
import tempfile
from pathlib import Path

from jane.cognition.context import Snapshot
from jane.cognition.coordinator import Coordinator, ScriptedCognitiveAdapter
from jane.core.clock import FakeClock
from jane.motivation.goals import Goal, Goals
from jane.runner import demo_scenario, run
from jane.storage.journal import Journal


async def coordinator_demo() -> str:
    scenario = demo_scenario()
    clock = FakeClock(scenario.start)

    def proposal(snapshot: Snapshot, kind: str) -> str:
        data: dict[str, object] = {
            key: getattr(snapshot, key)
            for key in ("request_id", "snapshot_id", "task_id", "generation")
        }
        data.update(
            decision_id="decision-" + snapshot.request_id,
            type=kind,
            summary="Offline fixture proposal",
            evidence_ids=[],
        )
        if kind == "call_tool":
            data.update(
                tool="mock.inspect", arguments={"object_id": scenario.observations[0].object_id}
            )
        else:
            data["evidence_ids"] = [
                e.event_id for e in snapshot.events if e.kind == "tool.observation"
            ]
            if kind == "respond":
                data["text"] = scenario.expected.response
        return json.dumps(data)

    with tempfile.TemporaryDirectory() as directory:
        journal = Journal(Path(directory) / "demo.db")
        try:
            Goals(journal).add(
                Goal(
                    goal_id=scenario.input.task_id,
                    description="Inspect the fixture object",
                    scope=("mock.inspect",),
                    success_event_kind="tool.observation",
                    created_at=clock.now(),
                )
            )
            journal.accept(scenario.input)
            adapter = ScriptedCognitiveAdapter(
                (
                    lambda s: proposal(s, "call_tool"),
                    lambda s: proposal(s, "respond"),
                    lambda s: proposal(s, "finish"),
                )
            )
            coordinator = Coordinator(journal, clock, adapter, lambda: ("mock.inspect",))
            coordinator.startup()
            outcomes = [await coordinator.step("demo-turn")]
            # Explicit fixture injection, not production action execution or verification.
            journal.accept(run(scenario).events[2])
            outcomes.append(await coordinator.step("demo-turn"))
            outcomes.append(await coordinator.step("demo-turn"))
            return (
                json.dumps(
                    {
                        "mode": "proposal-only; observation supplied by fixture",
                        "outcomes": [o.model_dump(mode="json") for o in outcomes],
                        "events": [e.model_dump(mode="json") for e in journal.events()],
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
        finally:
            journal.close()
