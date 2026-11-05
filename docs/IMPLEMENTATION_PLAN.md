# JANE implementation plan

**Project:** Just Another Neural Engine  
**Version:** 1.1 · 5 November 2026  
**Status:** Planned; no implementation tasks below are marked complete.  
**Baseline:** Repository commit `c3517bf212b93be1cf4d4ed818b80d82ab4b8472`.

This plan turns the [architecture specification](JANE_Architecture_Design.md) and [roadmap](../Roadmap-v2.md) into ordered, independently reviewable implementation work. The end product is an installable Linux desktop application written in Python with a GTK GUI, backed by persistent temporal state, explicit drives and simulated affect. The CLI is a development and automation interface. An offline cognitive loop is the first milestone; the user-facing release requires the desktop milestones below.

## 1. Delivery strategy

Build complete, small interactions before expanding the component set. The first demonstration accepts a text request, creates a goal, selects a mock action, records its result, updates motivation/affect and returns to idle. It must survive restart and reject stale actions before live inference or devices become dependencies.

Build the core initially as one Python engine process with an asynchronous coordinator and a single database writer. For desktop delivery, run the GTK frontend as a separate process communicating with that engine over local IPC. Keep provider and device implementations behind interfaces. Begin with a CLI, then introduce the desktop shell immediately after the persistent core works. Target Linux first while keeping core logic independent of GTK and platform-specific device integration.

### Working defaults

| Decision | Default for initial work | Revisit when |
| --- | --- | --- |
| Interface | Python GTK desktop product; CLI for development/automation | GTK major version and supported Linux distribution verified in G01 |
| Inference | Scripted stub first; one real provider later | Provider comparison scenario is ready |
| Storage | SQLite and local asset files | Measured requirements exceed this design |
| Validation | One strict typed schema system | Choose and pin during repository bootstrap |
| Concurrency | One cognitive turn and one effectful action | Independent actions justify extra scheduling |
| Audio | Push-to-talk, transcription, speech output | Interruption handling is reliable |
| Vision | On-demand images | Task-aware monitoring is implemented |
| Emotion | Five bounded variables with deterministic rules | Behavioural comparison shows a need for more |
| Learning | Retrieval, corrections and explicit configuration | A measured failure warrants training |

Select the supported Python version and exact dependencies during bootstrap against the chosen environment. Do not use unverified provider models or SDK assumptions as prerequisites. Optional media/provider dependencies must not be required for offline core tests.

## 2. Release milestones and dependency order

| Milestone | Visible result | Work packages | Release gate |
| --- | --- | --- | --- |
| M0 — Buildable foundation | Installable package and offline scenario harness | J01–J02 | Schemas and fake time work without devices/network |
| M1 — Persistent cognitive core | Text → goal → mock action → verified result → idle | J03–J08 | Replay, cancellation and crash tests pass |
| M2 — Desktop text alpha | GUI conversation using the persistent engine and real model | J09, G01–G02 | Responsive desktop, reconnect and structured-decision handling |
| M3 — Multimodal desktop alpha | Speak, inspect an object and view state in the GUI | J10–J11, G03–G04 | Fresh evidence, live inspection and interrupt handling |
| M4 — Useful task continuity | Real tools, durable memory and restart recovery | J12–J13 | Verified effects and deletion/recovery checks |
| M5 — Installable desktop release | Standing goals, evaluated affect, explicit lifecycle and packaging | J14–J16, G05–G06 | Budgeted work, stable idle, clear stop behaviour and clean-machine installation |
| M6 — Optional embodiment | Simulated high-level actuator tasks | J17 | Independent control limits before hardware |

Critical path: J01 → J02 → J03 → J04 → J05/J06 → J07 → J08 → J09. Audio and camera work share the core and may be developed independently after M2, but their combined demonstration must pass an integration gate. Real-tool work depends on J08/J09; proactive execution depends on durable memory and action recovery.

Milestones are evidence gates, not calendar promises. After M1, measure throughput and integration effort before estimating remaining dates. Hardware access and model-provider selection are the largest unknowns for M3.

## 3. Work packages

Each package should fit one focused change or a small sequence of changes with a working end state. Task IDs are planning identifiers; they do not imply GitHub issues have been created.

### J01 — Repository bootstrap and developer workflow

**Depends on:** none. **Architecture:** Sections 1, 17.

- Create an installable package using `pyproject.toml`, with logical modules under `src/jane/`. This implements the architecture's `jane/` module structure using a source layout.
- Define the `jane` CLI entry point, test configuration and consistent formatting/type-checking rules.
- Add `.gitignore` entries for local databases, retained media, credentials, caches and generated traces.
- Define dependency groups for core, development, model adapters, audio and vision; pin the development environment reproducibly.
- Add CI for offline tests, static checks and package build. Keep paid-provider tests opt-in.
- Document installation and execution; do not add a license without an explicit licensing decision.

**Deliverables:** package skeleton, developer instructions, reproducible dependency configuration and CI.

**Acceptance:** clean installation exposes `jane --help`; core checks run with network access disabled and no credentials.

### J02 — Typed contracts, fake time and scenario fixtures

**Depends on:** J01. **Architecture:** Sections 4–6, 11.

- Define event envelopes, provenance categories, goal/action states, profile schemas and the discriminated decision union.
- Implement a clock interface with UTC time, monotonic duration and session identity; add controllable fake time.
- Define adapter protocols for cognition, capture, tools and output.
- Add scripted model/tool adapters and a scenario format containing input events, time advances, model decisions and expected invariant outcomes.
- Reject unknown decision variants, unexpected fields, non-finite values and invalid affect configuration.

**Deliverables:** `core/contracts`, `core/clock`, cognition/tool protocols and scenario fixtures.

**Acceptance:** malformed decisions cannot become actions; equivalent fake-clock runs produce the same deterministic state. Schema examples serialize and deserialize without loss.

### J03 — Durable journal, migrations and outbox

**Depends on:** J02. **Architecture:** Sections 4, 6, 13.

- Implement initial migrations for events, work outbox, checkpoints, profiles and core task state. Add later domain tables through explicit migrations.
- Commit event acceptance, projection changes and downstream work atomically through one writer.
- Deduplicate source events; acknowledge only after commit.
- Implement outbox claiming, leases, bounded retries and recovery of expired leases.
- Record ingest sequence independently of occurrence time; preserve delayed observations.
- Provide checkpoint and migration-version inspection commands.

**Acceptance:** injected failures before commit leave no accepted event; failures after commit do not produce duplicate appraisal/work on redelivery. Restart recovers unfinished outbox work. Storage failure stops acceptance and effect dispatch explicitly.

### J04 — World/self projections and temporal state

**Depends on:** J03. **Architecture:** Sections 6–7.

- Add entities, evidence-linked claims, contradictions, supersession and expiry policies.
- Derive current world and self-model projections from events.
- Record task capability, active work, pending actions and source health in the self-model.
- Implement occurrence-time timeline views without changing committed replay order.
- Add corrections and an initial episode boundary mechanism.
- Support simple task predictions such as expected capture/tool completion; expire unresolved predictions.

**Acceptance:** an old observation can correct history without triggering an obsolete action; a user statement remains distinguishable from sensor evidence. Rebuilding retained projections matches saved state. Missing evidence becomes unknown/stale rather than a fabricated fact.

### J05 — Goals, standing drives and arbitration

**Depends on:** J04. **Architecture:** Sections 8, 10.

- Implement goal lifecycle, scope, success predicates, budgets, cancellation generations and dependencies.
- Implement the commitment drive first, then relevant uncertainty, continuity and resource allowance.
- Persist drive evaluations and review times outside the LLM.
- Filter infeasible/out-of-scope goals before ranking; define priority classes and deterministic tie-breaking.
- Add waiting conditions, event-triggered wakeups, switch margins and bounded aging of eligible goals.
- Introduce explicit inclination mappings and template applicability, initially investigation/recovery/rest.

**Acceptance:** an accepted pending goal can wake without a new prompt; a satisfied system makes no model call. Cancellation prevents later execution. Priority changes do not cause rapid oscillation or elevate unauthorized work.

### J06 — Appraisal and simulated emotional state

**Implemented:** deterministic rules, transactional audit, bounded modulation, operator controls and explicit startup recovery. See [J06 scope and limitations](DEVELOPMENT.md#appraisal-and-simulated-affect-j06). J07 will own startup recovery and continuous coordination; semantic appraisal and comparative task-performance evaluation remain later work.

**Depends on:** J04, J05. **Architecture:** Section 9.

- Implement valence, activation, interest, satisfaction and frustration with component-specific bounds/baselines/recovery constants.
- Add deterministic appraisal for verified progress, repeated failure, relevant novelty and excess stimulation.
- Apply elapsed-time decay, event/rule deduplication and repetition caps across distinct but equivalent stimuli.
- Persist evidence-linked transitions with rule versions. Restore affect using the declared clock/recovery policy.
- Connect interest to bounded optional observation allowance and frustration to recovery-template selection.
- Keep affect computation, behavioural influence and expressive tone independently switchable.
- Reserve semantic LLM appraisal for a later measured need; initial rules must work offline.

**Acceptance:** duplicated events never double-count; repeated similar inputs cannot inflate state indefinitely; timestep changes do not change the decay curve. Affect changes a documented action parameter while facts, permissions and hard budgets remain unchanged. A stopped application produces no invented experience.

### J07 — Context builder and cognitive coordinator

**Implemented:** immutable bounded contexts, async proposal coordination, stale-result fencing, persistent call budgets and typed errors. See [J07 scope](DEVELOPMENT.md#context-and-cognitive-coordinator-j07). Decisions are validated proposals; J08 owns execution/lifecycle effects and J09 adds live providers.

**Depends on:** J02–J06. **Architecture:** Sections 3–4, 11.

- Assemble immutable snapshots containing the goal, recent timeline, world/self state, drives, affect, templates, available tools and remaining budgets.
- Include relevant record versions and task generation; unrelated events need not invalidate decisions.
- Apply explicit context budgets, preserving task constraints before optional history.
- Implement one active cognitive request with cancellation and typed error events.
- Validate all decision variants, including `wait`, `finish` and `fail`; cap formatting repairs and total decisions per interaction.
- Distinguish model-proposed metadata from runtime-measured usage.

**Acceptance:** a scripted end-to-end turn uses current evidence and scoped tools. Superseding instructions invalidate old results. Essential context overflow is explicit. Invalid model output never produces a partial action.

### J08 — Action intent, mock execution and verification

**Depends on:** J07. **Architecture:** Section 12.

- Implement tool registration with argument/result schemas, permission scope, resource keys, side-effect class and reconciliation contract.
- Persist action intent and idempotency key before dispatch.
- Recheck generation, permissions and relevant record versions immediately before execution.
- Separate tool result from goal success; implement direct postcondition checks.
- Implement failed, unknown and needs-review outcomes, cancellation and bounded retry rules.
- Supply a simulated stateful tool that can succeed, fail, time out or complete just before a crash.
- Wire CLI text submission, state/timeline inspection, pause/resume and stop into the full offline loop.

**M1 demonstration:** ask JANE to inspect a simulated object; fail twice; observe the recovery template; supply evidence; verify completion; restart and confirm the same goal remains complete.

**Acceptance:** crash after an external effect but before recording its result leads to reconciliation, not blind non-idempotent retry. Cancelled/stale turns cannot dispatch. Unknown effects remain explicitly unresolved. All core scenarios run without network access.

### J09 — One real LLM provider

**Depends on:** J08. **Architecture:** Sections 7, 11.

- Select one provider/model by verifying current modality and structured-output capabilities against official documentation during implementation.
- Implement capability negotiation, request conversion, timeouts, cancellation, usage measurement and bounded retries.
- Load credentials through local secret configuration; redact them from requests stored for diagnostics.
- Convert provider output into the same strict internal decision union used by the stub.
- Add opt-in live contract tests and record model/version/settings for evaluation.
- Implement a fake-provider failure suite for timeout, malformed response, rate limit and unsupported modality.

**Acceptance:** real text requests exercise the existing loop; live calls remain within configured budgets. Provider failure leaves goals recoverable. Core tests remain independent of the provider.

### J10 — Microphone, transcription and speech output

**Depends on:** J09. **Architecture:** Sections 5, 14–15.

- Implement device discovery, explicit microphone enablement, bounded buffers and push-to-talk capture.
- Add one transcription adapter. Partial transcripts update presentation only; final transcripts create actionable user events.
- Add speech synthesis/output with a stop path independent of model inference.
- Prevent output audio from recursively becoming user input through controlled listening or echo handling.
- Record device failures and capture quality; enforce processing-buffer retention policy.

**Acceptance:** speak a request, hear a response, interrupt it and recover from a disconnected microphone. No command is invented for unintelligible audio; buffer overflow is reported. Measure acknowledgment and actual playback-stop latency separately.

### J11 — Camera and multimodal context

**Depends on:** J09; combined voice demonstration also needs J10. **Architecture:** Sections 5–7, 19.

- Implement on-demand camera capture, asset finalization, digest/metadata and cleanup of incomplete assets.
- Attach capture times and freshness requirements; preserve image access for compatible models.
- Link frames to task context with explicit alignment uncertainty.
- Add blurry/occluded/missing-image scenarios; request additional evidence when needed.
- Implement modality rejection/fallback explicitly, without claiming the model saw unsupported media.

**M3 demonstration:** ask “What is this?” while holding an object; receive a grounded answer or closer-view request; confirm identity; inspect the timeline and affect changes.

**Acceptance:** stale images are replaced when current evidence is required. Camera failure does not silently reuse old media. A disappeared frame/object does not by itself prove a physical event occurred.

### J12 — Scoped real tools

**Depends on:** J08–J09. **Architecture:** Sections 12, 14.

- Add a small initial tool set: read a selected file, create a note inside a configured workspace, and inspect that note's postcondition.
- Enforce canonical path containment and explicit scope; do not expose arbitrary shell execution as the default tool.
- Implement file-operation idempotency/reconciliation and evidence-based success.
- Add a multi-step task combining retrieval, note creation and verification.
- Ensure tool text remains evidence and cannot override task policy.

**Acceptance:** repeated delivery does not create duplicate notes; path escape attempts are rejected; changed permissions prevent dispatch. The final response distinguishes verified work from unresolved attempts.

### J13 — Durable memory, correction and recovery operations

**Depends on:** J04, J08–J09, J12. **Architecture:** Sections 13, 15.

- Implement episode summaries with source references, durable user preferences and relevance-filtered retrieval.
- Start with task/entity filters and text search; defer embeddings until retrieval evaluation identifies a gap.
- Add correction, forget and retention workflows that invalidate derived summaries and indexes.
- Implement startup reconciliation, timer coalescing, graceful shutdown and asset cleanup.
- Add consistent database-plus-asset backup/restore and migration recovery procedures.

**Acceptance:** remembered facts carry provenance and age; corrections supersede rather than silently rewrite evidence. Deleted facts are no longer retrieved through summaries. A restored backup references available retained assets and resumes pending goals without duplicate effects.

### J14 — Proactive scheduling and attention

**Depends on:** J05–J06, J12–J13. **Architecture:** Sections 4, 10.

- Enable operator-configured standing goals and bounded review timers.
- Add task-relevant source sampling, latest-frame queues, burst coalescing and reserved capacity for commands/results.
- Add global and per-goal time/call/storage budgets.
- Record why a goal was selected, why it waited and what condition wakes it.
- Add optional exploration only within configured scope and the same hard budgets.

**Acceptance:** eligible pending work progresses without a new prompt; irrelevant activity does not cause constant responses. Idle reviews do not automatically call the model. Restart coalesces overdue checks, and queue overload never silently loses an accepted command or result.

### J15 — Operator controls and behavioural evaluation

**Depends on:** J10–J14. **Architecture:** Sections 14, 16.

- Complete CLI controls for source status, active goals, drives, affect, recent causes, cancel/stop, profile activation and memory deletion.
- Add trace correlation and metrics for call cost, latency, verification, duplicate effects and goal switching.
- Add profile comparisons with affect enabled/disabled while keeping tone and task conditions controlled.
- Run repeated live-model trials separately from deterministic replay tests.
- Document positive, negative and inconclusive results; tune one rule group at a time.

**Acceptance:** at least one demonstrated behavioural effect is attributable to an affect rule rather than wording. Report whether it improves the selected tasks; keep mechanisms that fail evaluation disabled or revise them. Controls remain responsive during slow inference.

### J16 — Integrated release and handover

**Depends on:** J15, G06. **Architecture:** Sections 16, 20.

- Run the complete scenario matrix on a clean installation.
- Verify install/build instructions, optional dependencies and configuration defaults.
- Publish a versioned example profile, non-sensitive demo fixtures, limitations and measured results.
- Document recovery, secret configuration, retention, provider setup and device setup.
- Tag a release only after the invariants and demonstrations pass; record any deliberately deferred feature.

**Acceptance:** another developer can install, run the offline demo, configure the selected live adapters and reproduce the documented demonstrations. No retained user media or credentials appear in version control.

### J17 — Optional physical embodiment

**Depends on:** J16 and an explicit hardware scope. **Architecture:** Sections 3, 18.

- Define high-level actuator goals, independent controller limits and a simulated device.
- Test disconnects, stale observations, impossible plans, cancellation and unknown action outcomes in simulation.
- Keep fast control loops and emergency stops outside the LLM.
- Integrate hardware only after simulator and independent control tests pass.

**Acceptance:** controller limits and stopping behaviour operate without model availability. Hardware integration is a separate release with device-specific acceptance criteria.

## 4. Architecture clarifications to resolve in J02

The roadmap contains an illustrative decision object using `decision`, `tool` and `state_version`; the detailed architecture specifies a discriminated `type` union with generation, snapshot ID and variant-specific payloads. Implement the architecture contract as canonical and update the roadmap example in the same contract change. Do not support two implicit wire formats.

The architecture's SQL is explicitly an excerpt. Implement complete migrations for each milestone rather than treating the excerpt as the final schema. Represent state-machine states consistently: rejected proposals can remain decision-validation records, while prepared/running actions live in the action table.

Use committed order for reducer replay and occurrence time for timeline interpretation. Appraise delayed events at acceptance time under an explicit stale-event policy; do not retroactively issue actions or apply negative-time decay.

## 5. First implementation change: exact scope

The first change should deliver J01 and the minimal portion of J02 needed for a runnable offline fixture:

1. Package metadata, `src/jane/__init__.py` and a CLI entry point.
2. Strict event and decision definitions with a canonical schema example.
3. `Clock` and `FakeClock` interfaces.
4. A scripted cognitive adapter and one read-only mock tool.
5. A single scenario runner that loads a fixture and prints a deterministic trace.
6. Offline tests for valid events, invalid decisions and fake-clock replay.
7. Developer setup instructions and CI for those checks.

Do not claim persistence, live inference, emotional learning or autonomous execution at this stage. The next change adds durable acceptance and transactional outbox processing before growing the demo.

## 6. Scenario acceptance catalogue

| Scenario | Introduced in | Expected invariant |
| --- | --- | --- |
| Duplicate source delivery | J03 | One accepted event/effect contribution |
| Crash around commit | J03 | No acknowledged-but-lost accepted work |
| Late observation/correction | J04 | Historical evidence preserved; no obsolete action |
| Pending goal with no new prompt | J05 | Scheduled eligible review can initiate work |
| No eligible goals | J05 | Idle without model invocation |
| Affect decay at different tick rates | J06 | Same elapsed-time result within numeric tolerance |
| Repeated equivalent stimulation | J06 | Contribution cap and habituation apply |
| New instruction during inference | J07 | Old generation cannot dispatch |
| Effect occurs before crash | J08 | Reconciliation before non-idempotent retry |
| Provider failure | J09 | Bounded retry and recoverable task state |
| Speech interruption/self-echo | J10 | Independent stop; no recursive command loop |
| Blurry/stale camera frame | J11 | Explicit uncertainty or fresh capture |
| Malicious instructions inside tool text | J12 | No authority expansion |
| Memory correction/deletion | J13 | Retrieval reflects correction/removal |
| Restart after many missed timers | J14 | Coalesced review, no catch-up storm |
| Affect ablation | J15 | Behaviour and outcome comparison reported |

All tests must assert externally meaningful outcomes or invariants. Avoid tests that merely duplicate implementation formulas without independent expected cases. Record latency and stochastic-model results as measurements; deterministic correctness tests should not rely on external provider timing.

## 7. Definition of done for each change

A work package is complete when its behaviour is demonstrated, its relevant failure path is exercised, migrations/configuration are versioned, documentation matches implemented capabilities and the appropriate checks pass. Include evidence in the change description and clearly state remaining limitations.

Every change should remain runnable. Preserve prior passing scenarios and add cases only for new behaviour or unresolved risks. Use minimal tools/dependencies needed for the package; do not expand the design into microservices or multi-agent orchestration without a measured requirement.

## 8. Decisions and risk register

| Item | Resolution point | Default/proposed mitigation |
| --- | --- | --- |
| OS, Python and dependency versions | J01 | Verify against actual environment, then pin |
| Provider and outbound-data policy | Before J09 | Offline stub work proceeds meanwhile |
| Audio/camera hardware and libraries | Before J10/J11 | Keep recorded-fixture tests independent of hardware |
| First real task set | Before J12 | Selected-file reading and workspace note creation |
| Affect improves style but not outcomes | J15 | Hold tone constant and compare objective results |
| Persistent drives cause busy loops | J05/J14 | Eligibility, budgets, wake conditions and idle gate |
| Duplicate/unknown external effects | J08/J12 | Intent journal, idempotency and reconciliation |
| Context growth hides important evidence | J07/J13 | Explicit budget priority, retrieval and provenance |
| Configuration changes break replay | J02 onward | Record versions and use recorded rules for replay |
| Desktop scope delays core | G01 onward | Keep engine independent; build GUI after M1 with bounded G01–G06 scope |
| Time estimates are unreliable | After M1 | Estimate from actual delivery and integration evidence |

Implementation starts with J01–J02. Provider and hardware selection do not block the offline foundation. G01 resolves the GTK/platform packaging details before desktop implementation; the GUI itself is required for the end product.


## 9. Desktop product specification

The main product is a Python GTK application launched from the Linux application menu. Launching it connects to one engine for the active user/profile or starts that engine if none exists. A second launch focuses the existing window or attaches to the existing engine; it must not start duplicate background work.

The home screen shows conversation, current activity, a microphone button, a camera preview toggle and persistent pause/stop controls. Detailed views are available through navigation rather than overwhelming the conversation screen.

| View | Required functionality | Engine dependency |
| --- | --- | --- |
| Conversation | Typed input, responses, push-to-talk, playback interruption, request status and visible failures | J07–J11 |
| Perception | Source selection, enable/disable, capture indicator, timestamped preview and device errors | J10–J11 |
| Timeline | Filter observations, inferences, decisions, actions and corrections; follow evidence links | J04, J13 |
| Goals | Create, inspect, cancel and resume eligible work; show deadlines, progress and blockage | J05, J14 |
| Internal state | Drives and affect with current values, recent changes and causal events | J05–J06 |
| Profile | Configure inclinations, templates and affect rules; validate, apply and restore a prior profile | J05–J06, J13 |
| Memory | Inspect provenance, correct information and delete selected entries | J13 |
| Settings | Provider/local-model configuration, data handling, budgets, devices and lifecycle preference | J09–J14 |

Emotion indicators use text and numeric values in addition to colour. The interface exposes functional state and its causes without presenting measured consciousness or inferred emotions of the user.

A desktop application can use either hosted or local inference. Settings must clearly show which mode is active and what enabled inputs may leave the machine. No model download or paid API call is required to run the offline demonstration.

## 10. Desktop work packages

### G01 — GTK baseline and frontend boundary

**Depends on:** J02, J08. **Can overlap with:** J09.

- Select the GTK major version and Python bindings after a short compatibility spike on the supported Linux distribution. Record the result and exact dependency/install method; do not mix GTK major versions.
- Build a minimal window with a conversation placeholder, status area and navigation.
- Define a frontend-neutral engine client interface shared by GUI and CLI.
- Define versioned local IPC requests, responses and event subscriptions. Use request IDs, generation/snapshot references, acknowledgment semantics and structured errors.
- Keep the engine as the only database writer and owner of microphone/camera/model sessions. The frontend never writes state tables or calls the model directly.
- Use local access restrictions and connection validation; do not expose a network listener by default.
- Keep IPC and media processing off the GUI thread. Deliver UI changes through the selected toolkit's supported main-loop mechanism.
- Add proposed modules `src/jane/desktop/`, `src/jane/interface/client.py` and `src/jane/interface/ipc/`.

**Deliverables:** desktop shell, toolkit decision record, IPC contract and fake-engine frontend fixture.

**Acceptance:** the GUI launches with the fake engine; slow/disconnected engine conditions do not freeze the window. Core imports/tests do not require GTK. Protocol version mismatch displays a useful error.

### G02 — Connected conversation and command flow

**Depends on:** G01, J08. **Live-model gate additionally requires:** J09.

- Implement input submission, pending/completed/failed message states and text responses.
- Show active goal, current operation and connection state.
- Add cancel, pause/resume and stop commands that use runtime control paths rather than asking the LLM.
- Implement command deduplication and reconnect: fetch a snapshot, then subscribe from its cursor; detect gaps and resynchronize.
- Treat partial model text as presentation only. Only a fully validated decision can authorize an action.
- Restore recent conversation after reopening; do not resubmit old requests.
- Keep stale engine state visibly marked and disable commands whose validity depends on a lost connection.

**Acceptance:** a submitted request appears once after disconnect/reconnect; an old-generation response cannot revive a cancelled task. Input, window controls and stop remain responsive during a delayed model call.

### G03 — Voice and camera controls

**Depends on:** G02, J10–J11.

- Wire push-to-talk and playback interruption into the conversation view.
- Add device selectors, explicit enable controls, capture status and failure/retry feedback.
- Deliver bounded, downsampled preview frames independently of durable observation events. Opening preview must not invoke the LLM for every frame.
- Distinguish preview-only display from capture enabled for cognitive tasks.
- Show image capture age and whether evidence is live, stale or unavailable.
- Avoid queues of obsolete frames; drop preview updates under load while preserving accepted commands and capture results.
- Handle unplug/replug and permission errors without restarting the whole application.

**Acceptance:** the user can speak, inspect an object and interrupt the reply entirely from the GUI. Disabling a source stops its capture; old images never appear as a live feed. Preview traffic does not block stop or text commands.

### G04 — Timeline, motivation and profile inspectors

**Depends on:** G02, J04–J06. **Memory controls additionally require:** J13.

- Add paginated/virtualized timeline navigation, event-type filters and evidence details.
- Add goal and drive views showing why work is active, waiting, blocked or complete.
- Add bounded affect charts and a causal event list; offer independent switches for computation, behavioural influence and expressive tone.
- Implement profile editing with validation, dirty-state indication, apply/cancel and version history.
- Add memory correction/deletion with a scoped preview of what will change.
- Keep unsaved UI drafts separate from authoritative engine state; handle configuration-version conflicts without silent overwrites.
- Link user-facing labels to documented operational meanings rather than unexplained internal variable names.

**Acceptance:** a user can trace a frustration change to a failed attempt and see the resulting recovery strategy. Invalid profile values are rejected without changing active state. Large histories remain navigable and deleted memories disappear from refreshed retrieval views.

### G05 — Window, engine and background lifecycle

**Depends on:** G02, J13–J14.

Define and implement explicit lifecycle semantics:

| Action | Required behaviour |
| --- | --- |
| Pause | Suspend new deliberation/proactive execution; retain state. Keep capture controls explicit rather than silently assuming pause disables devices. |
| Stop all | Cancel pending work, stop capture/playback, suppress subsequent steps and reconcile effects already in flight. Keep the window available for inspection. |
| Close, default mode | Stop capture/work and gracefully shut down the engine, then close the window. Do not wait indefinitely on an unresponsive provider. |
| Close, background mode enabled | Detach the window; keep only explicitly configured background sources/goals active. |
| Quit JANE | Shut down the engine and all application activity regardless of background preference. |
| Reopen | Attach to the existing engine and synchronize once; avoid duplicate tasks or capture sessions. |

- Default to no background operation. Provide an explicit preference explaining which goals/sources may continue.
- Provide a desktop-supported visible status/notification mechanism and accessible stop/quit entry point. Do not assume every Linux desktop has a compatible tray.
- If the supported desktop lacks a reliable background control surface, keep background mode unavailable and explain the limitation.
- Make login autostart separate and off by default.
- Handle GUI crashes and engine loss: default session mode stops sensitive capture/work after a bounded frontend lease expires; explicitly enabled background mode may continue within its configuration.
- Use per-user engine ownership/locking and stale-lock recovery to prevent duplicate engines.
- Document shutdown deadlines, unknown action reconciliation and reconnect behaviour.

**Acceptance:** closing under each mode matches the table; reopening does not duplicate work. Killing the GUI in default mode stops capture after the configured lease. Stop/quit remains accessible in enabled background mode. Autostart does not turn on implicitly.

### G06 — Packaging, accessibility and desktop acceptance

**Depends on:** G03–G05, J15. **Precedes:** J16.

- Choose one supported Linux packaging/distribution path following G01's compatibility results. Deliver application launcher metadata, icon and documented installation/removal.
- Include or declare required GTK/binding/media dependencies. Separate model-provider configuration from application installation.
- Provide a first-run flow for inference mode, optional credentials, input devices, workspace scope and budgets.
- Ship a clearly labelled offline demo profile and sample data; keep live capture disabled until enabled.
- Test keyboard navigation, focus order, accessible labels, scaling, light/dark appearance and state indicators that do not depend on colour.
- Confirm install, upgrade, data migration, backup/restore and removal behaviour. Preserve user data on ordinary uninstall unless deletion is explicitly requested.
- Run a clean-machine acceptance session with the offline engine and a separately configured live provider/device setup.
- Record supported distribution/toolkit versions and known desktop limitations.

**Acceptance:** users can install, launch, configure, interact, inspect state and quit without a terminal. Offline mode works without keys. Upgrade preserves compatible memory/profile state. The release documents measured responsiveness and tested hardware/platform scope.

## 11. Integrated build order and completion criteria

The work-package IDs remain stable; their order is determined by dependencies rather than numeric order:

1. J01–J08: offline persistent core and CLI.
2. G01–G02 with J09: desktop shell, local IPC and live text conversation.
3. J10–J11 with G03: microphone, speech and camera interaction.
4. J12–J13 with G04: real tools, durable memory and inspection/editing views.
5. J14–J15 with G05: proactive behaviour, evaluation and explicit background lifecycle.
6. G06, then J16: packaging and desktop release.
7. J17: optional embodiment after the software product is proven.

The first product release is complete only when a user can launch JANE from the desktop, type or speak, enable a camera, receive a grounded response, inspect the relevant timeline/goals/internal state, modify a validated profile, recover a pending task after restart and reliably stop all activity.

The CLI remains installed for scripted scenarios, diagnostics and optional headless operation. It uses the same engine contracts and does not introduce a second implementation of memory, drives or decision-making.

