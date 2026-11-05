# JANE: Just Another Neural Engine

JANE is a proposed persistent cognitive system built around an LLM decision engine. It combines multimodal observations, temporal memory, configurable drives, behavioural inclinations, and simulated emotional states to select actions and verify their outcomes.

**End product:** A Python GTK desktop application for Linux, with a persistent engine and a CLI for development, diagnostics and automation. Local or hosted model inference is configurable.

**Status:** Initial offline foundation (J01 and initial J02). No live model, devices, GUI or persistence yet.

## Run the offline demo

Requires Python 3.12. See [developer setup](docs/DEVELOPMENT.md) for a reproducible environment.

```bash
python -m pip install .
jane demo
```

The demo emits a deterministic JSON trace using a scripted adapter and read-only mock observation.

## Project documents

- [Roadmap](Roadmap-v2.md): scope, design principles, drives, emotional dynamics and staged implementation.
- [Detailed architecture](docs/JANE_Architecture_Design.md): components, contracts, data models, state machines, persistence, recovery and evaluation.

- [Implementation plan](docs/IMPLEMENTATION_PLAN.md): ordered work packages, dependencies, deliverables and acceptance tests.

## Core cycle

Observe → interpret → update timeline and state → evaluate drives → select goal → decide → execute → verify.

The timeline, world model, self-model, goals and emotional state persist outside the LLM. The model proposes decisions; the runtime validates actions and checks actual outcomes.

## Planned capabilities

- Text, microphone, camera and extensible sensor/application inputs.
- Evidence-linked temporal memory and persistent goals.
- Configurable drives, inclinations and behaviour templates.
- Bounded simulated affect that influences attention and strategy.
- Structured model decisions, scoped tools and outcome verification.
- Cancellation, restart recovery and reproducible scenario evaluation.

## Implementation direction

Begin with a single-user Python application, an asynchronous coordinator, SQLite persistence, a fake clock and simulated tools. Establish a text decision loop, then add a GTK frontend using local IPC, audio, vision, real tools and proactive operation. Desktop release includes conversation, camera/microphone controls, timeline and internal-state inspectors, profile editing, explicit background/quit behaviour and installation packaging. See the architecture document for implementation phases and acceptance criteria.

Simulated emotional states are functional control variables; the design makes no claim of subjective experience.

