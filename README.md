# JANE: Just Another Neural Engine

JANE is a proposed persistent cognitive system built around an LLM decision engine. It combines multimodal observations, temporal memory, configurable drives, behavioural inclinations, and simulated emotional states to select actions and verify their outcomes.

**End product:** A Python GTK desktop application for Linux, with a persistent engine and a CLI for development, diagnostics and automation. Local or hosted model inference is configurable.

**Status:** Initial offline foundation (J01 and initial J02). J03 adds a SQLite journal and recovery queue; J04 adds evidence-linked world/self projections, temporal views and prediction tracking. No live model, devices or GUI yet.

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


## Durable event demonstration

```bash
jane journal seed-demo jane.db
jane journal status jane.db
jane journal drain jane.db
```

Restart between commands; events and pending receipts survive. Reseeding does not duplicate them. See [storage semantics](docs/DEVELOPMENT.md#durable-journal-j03).

Inspect temporal state with `jane journal world jane.db` and `jane journal timeline jane.db`. Rebuild derived state with `jane journal rebuild jane.db`.

## Goals and drives

J05 adds persistent goals, budgets, dependency checks, waiting/wakeup rules and deterministic drive review. Use `jane goals --help`; see [goal setup](docs/DEVELOPMENT.md#persistent-goals-and-drive-review-j05). Review selects work without invoking a model or dispatching tools.

J06 adds deterministic simulated affect: evidence-linked progress/failure/novelty rules,
elapsed-time recovery, repetition caps and bounded modulation of goal review. Inspect it
with `jane affect status jane.db` and `jane affect history jane.db`. Configuration, recovery
and operator reset are documented in [developer setup](docs/DEVELOPMENT.md#appraisal-and-simulated-affect-j06).
The offline demo remains deterministic; the persistent cognitive coordinator is next (J07).

J07 adds immutable bounded contexts and an async cognitive coordinator with cancellation,
stale-result rejection, persistent call budgets and typed errors. Run `jane coordinator-demo`
for a deterministic proposal pipeline. It uses fixture evidence; action dispatch and
lifecycle execution remain J08.

J08 connects proposals to durable offline execution. Try:

```bash
jane offline submit jane.db --text "Inspect the simulated cup" --object cup
jane offline run jane.db
jane offline status jane.db
```

A new simulator fails twice, selects recovery, verifies the inspection and completes the
goal. Restart preserves completion. Action intents, bounded retries, crash reconciliation,
resource locks and pause/resume/stop controls are documented in
[developer setup](docs/DEVELOPMENT.md#durable-actions-and-full-offline-loop-j08).
This uses a scripted policy and local simulator; J09 adds a real model provider.

J09 adds optional live text decisions through OpenAI's Responses API (`gpt-6.1-sol`).
Install `.[openai]`, configure `OPENAI_API_KEY`, submit with `jane live submit`, then opt in
with `jane live run jane.db --enable-live`. Calls remain budgeted and actions stay within
the local simulator. See [provider setup and test limits](docs/DEVELOPMENT.md#first-live-text-provider-j09).

J10's initial voice path adds explicit push-to-talk capture, a final transcript preview,
confirmed task submission, and interruptible local speech. Install `.[voice]` plus PortAudio
and eSpeak NG, then run `jane voice devices` and
`jane voice session --enable-microphone --enable-live`. The task still inspects a simulated
object. See [voice setup and hardware acceptance](docs/DEVELOPMENT.md#push-to-talk-voice-j10).
