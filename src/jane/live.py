"""Explicit opt-in model inference using the existing verified simulation loop."""

import json
from pathlib import Path

from jane.cognition.coordinator import Limits
from jane.core.clock import SystemClock
from jane.offline import OfflineRuntime
from jane.providers.openai import OpenAIAdapter, ProviderConfig
from jane.storage.journal import Journal


async def run_live(
    path: Path, max_steps: int, max_calls: int, max_output_tokens: int, reasoning: str
) -> dict[str, object]:
    # Validate and load credentials before mutating a journal or recovering tasks.
    config = ProviderConfig.model_validate(
        {"max_output_tokens": max_output_tokens, "reasoning_effort": reasoning}
    )
    limits = Limits(max_calls=max_calls, timeout_seconds=config.timeout_seconds)
    if type(max_steps) is not int or not 1 <= max_steps <= 32:
        raise ValueError("max_steps must be between 1 and 32")
    adapter = OpenAIAdapter(config)
    journal = None
    runtime = None
    try:
        journal = Journal(path)
        runtime = OfflineRuntime(
            journal, Path(str(path) + ".sim.db"), SystemClock(), adapter=adapter, limits=limits
        )
        runtime.startup()
        trace = await runtime.run(max_steps)
        usage = [
            {"request": row[0], "attempt": row[1], "metadata": json.loads(row[2])}
            for row in journal.db.execute(
                "SELECT * FROM provider_attempts ORDER BY request,ordinal"
            )
        ]
        return {"trace": trace, "state": runtime.status(), "provider_attempts": usage}
    finally:
        if runtime is not None:
            runtime.close()
        if journal is not None:
            journal.close()
        await adapter.close()
