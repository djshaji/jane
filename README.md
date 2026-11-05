# JANE: Just Another Neural Engine

JANE is a proposed persistent cognitive system built around an LLM decision engine. It combines multimodal observations, temporal memory, configurable drives, behavioural inclinations, and simulated emotional states to select actions and verify their outcomes.

**Status:** Architecture and planning. The runtime has not yet been implemented.

## Project documents

- [Roadmap](Roadmap-v2.md): scope, design principles, drives, emotional dynamics and staged implementation.
- [Detailed architecture](docs/JANE_Architecture_Design.md): components, contracts, data models, state machines, persistence, recovery and evaluation.

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

Begin with a single-user Python application, an asynchronous coordinator, SQLite persistence, a fake clock and simulated tools. Establish a text decision loop before adding audio, vision, real tools and proactive operation. See the architecture document for implementation phases and acceptance criteria.

Simulated emotional states are functional control variables; the design makes no claim of subjective experience.
