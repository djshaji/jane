# Developer setup

The initial foundation targets Python 3.12. Install dependencies once; replay and tests then require no network, credentials or hardware.

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install --no-build-isolation --no-deps -e .
jane --help
jane demo
jane replay src/jane/scenarios/demo.json
```

The demo prints a deterministic JSON trace: user request, scripted inspection decision, mock observation, scripted response and finish decision. Time advances by three simulated seconds. It does not call an LLM or inspect a physical object.

```bash
python -m unittest discover -s tests -v
ruff check .
ruff format --check .
mypy src
python -m build --no-isolation
```

CI also installs the wheel outside the source directory and compares two demo outputs. Tests block socket creation during replay. Dependency installation requires package-index access unless wheels are already available locally.

## Contracts and scope

`core/contracts.py` uses strict Pydantic models with forbidden extra fields. JSON dates are timezone-aware strings; numeric strings are not accepted for numeric fields. The canonical decision discriminator is `type`, with task generation and snapshot references. The roadmap now uses that format.

All six architecture decision variants validate. This runner executes only `mock.inspect`, text responses and terminal finish/wait/fail decisions. Capture requests fail explicitly; waiting ends replay rather than scheduling background work. Finish requires observed tool evidence and a response; this is a fixture-level check, not general semantic verification.

The event union covers the four event kinds used by the demonstration. New event/tool variants require explicit typed schemas. Scenario length is capped at 32 decisions. Script IDs, start time and fake-clock advances make output reproducible. The runner validates context identity and evidence existence before tool dispatch.

J01 and the initial J02 slice are implemented. Full profile/affect contracts, capture/output adapters, real coordinator, cancellation scheduling, drives, emotion dynamics, live providers, audio, camera and GUI remain future work. The mock tool only reads immutable fixture observations. J03 adds SQLite event storage; there are no external tool effects.

Core uses Pydantic; development adds Ruff, mypy and build. Media/provider dependency groups will be added with their adapters rather than installing unused dependencies now. No license has been selected.

## Durable journal (J03)

```bash
jane journal seed-demo jane.db
jane journal status jane.db
jane journal drain jane.db
jane journal seed-demo jane.db
```

Each command runs in a separate process. Seeding accepts six demo trace events and queues six internal receipts. Status shows schema version, event count, task last-event projections, checkpoint and queue counts. Drain acknowledges internal receipts only; it does not replay tool actions. Reseeding is idempotent, so completed receipts remain completed.

The journal commits events, task projections, the acceptance checkpoint and outbox work atomically, acknowledges only after commit and rejects conflicting deduplication keys. Event occurrence time remains in the validated JSON while ingest sequence determines replay order. SQLite transactions serialize writers; each Journal connection belongs to its creating thread.

Workers claim with a lease token and bounded attempt count (default three); expired leases recover on the next claim. Stale workers cannot settle reassigned work. Retry delays postpone eligibility; exhausted work becomes failed. These semantics are at-least-once delivery: future effectful workers require idempotency/reconciliation, not blind replay. Caller-supplied queue times are epoch seconds; production callers must use a consistent clock.

Migration 1 creates events, outbox, task projection, checkpoint and reserved profile tables. Future schema versions are rejected without downgrade. Profiles have no activation API yet; full task lifecycle, world projections and the persistent cognitive coordinator remain J04 onward. Use the same attempt policy for all workers. Errors propagate and the CLI exits nonzero rather than acknowledging failed storage.

## World and self projections (J04)

Migration 2 derives a durable projection from retained events and leaves existing work-queue state intact. New `state.claim`, `state.self` and `state.prediction` variants use strict payloads. Claim evidence must already exist; observed/user-reported labels require the matching evidence class. Explicit corrections identify a prior claim for the same entity/predicate. Corrections retain original evidence and mark the earlier claim superseded. Invalid references roll back event acceptance, task/checkpoint changes and queued work together.

```bash
jane journal world jane.db
jane journal timeline jane.db
jane journal rebuild jane.db
```

World inspection includes entities, all claims with freshness/conflict flags, runtime self-status, episodes and predictions. Timeline orders by occurrence time with ingest-order ties; journal replay always uses ingest order. Episodes are grouped by task and correlation ID, with min/max occurrence bounds. Entity IDs are supplied explicitly; there is no visual identity matching.

A mock observation creates a snapshot-only description. It is stale after its observation instant; explicit claims can supply a validity interval. Stale and conflicting evidence remains inspectable and does not manufacture a replacement fact. Self-status supports capability, source health, activity, pending action and commitment categories; values are reported runtime facts, not an implemented action/goal lifecycle. Newer occurrence time wins, with ingest-order tie breaking.

Predictions initially match event kind, source, task and occurrence window against subsequently accepted events. They become supported when matching evidence is received, expired when the window passes without a match, or unresolved otherwise. Late matching evidence can update an expired interpretation to supported. Semantic contradiction, prediction cancellation and object-specific prediction predicates remain future extensions. Expiry is a derived query result, not a fabricated observation.

`world(now)` is the latest retained interpretation with validity evaluated at `now`, not an as-of-history reconstruction. Rebuild deterministically replays the journal without enqueueing work. The initial implementation rebuilds projections on each acceptance inside the same transaction: simple and inspectable, but O(history length) per event. Incremental reducers/pagination are needed before large continuous sensor workloads. No model calls, automatic physical actions, affect updates or GUI are added here.

## Persistent goals and drive review (J05)

Migration 3 adds goals, an append-only lifecycle audit, drive snapshots and an arbitration checkpoint. Existing journal/outbox/projections are preserved. Goal ownership is the local operator: this API is not exposed as an LLM tool.

Create `goal.json`:

```json
{
  "goal_id": "task-001",
  "description": "Inspect the mock object",
  "scope": ["mock.inspect"],
  "success_event_kind": "tool.observation",
  "created_at": "2026-11-05T00:00:00Z",
  "budget": 3,
  "drive": "uncertainty"
}
```

```bash
jane goals add jane.db --file goal.json
jane goals review jane.db --allow mock.inspect
jane goals list jane.db
jane goals cancel jane.db --id task-001
```

Review is explicit and makes no model/tool call. A later coordinator will call it on scheduled ticks/events. It restores persisted pending work without requiring another user prompt; it does not install a daemon. An empty eligible set returns `rest` and no selected goal. Permission filtering uses the supplied allowed-tool set on every review; dependencies must be complete and a goal must have budget remaining. Priority 0 is highest. Aging is bounded within a priority class, and a near/overdue deadline increases urgency. Equal rankings use goal ID for deterministic ties.

The Python `Goals` service exposes wait (future time and/or source), block, resume, cancel, fail and complete operations. Event wakeups require new journal entries for the same task after the wait cursor. Completion requires a matching task/event-kind reference, an initial structural success predicate rather than full semantic verification. All transitions invalidate prior proposal generations. `authorize` atomically reserves a step only for the selected active generation and a scoped tool; the future executor must still recheck current external permissions before dispatch. There are no external actions in this change.

Commitment, uncertainty and continuity goals share the eligibility/urgency evaluator. Resource discipline is enforced as a hard step-budget constraint rather than an independently competing goal. Drive evaluations, urgency, review times, selected goal and profile parameters persist. Profile curiosity bounds optional observations; patience sets the recovery threshold; commitment intervals and switch margins reduce same-class oscillation. Higher priority work can preempt immediately. All observation attempts must also reserve a step against the hard budget.

Lifecycle changes are audited in `goal_changes`, separate from sensor events; J04 event projection rebuilding does not erase them. Existing-goal-only dependencies prevent cycles in this initial immutable dependency graph. Scope and dependency editing, semantic success predicates, a continuously running scheduler, emotional modulation and real dispatch remain later work. Backward review-clock changes fail explicitly.

## Appraisal and simulated affect (J06)

Migration 4 adds affect state/configuration, event/rule deduplication, repetition counters,
seen task/object pairs and evidence-linked transition history. Migration does not appraise
old events retroactively. New event appraisal shares the acceptance transaction; progress
and failure appraisal share the goal lifecycle transaction. SQLite failure rolls back both.
These are functional state variables, not measured feelings or consciousness.

```bash
jane affect status jane.db
jane affect history jane.db
jane affect recover jane.db
jane affect configure jane.db --file affect.json
jane affect reset jane.db
```

Example `affect.json`: `{"computation": true, "behaviour": false, "expressive_tone": false}`.
Omitted settings take defaults; configuration replaces the whole profile, rather than
patching it. Bounds, baselines, positive recovery constants and finite numeric values are
validated. Configuration and reset are audited operator APIs, never model tools. Reset
keeps deduplication and repetition history, so resetting cannot replay old rewards.

Rules (`affect-rules-v1`):

- Verified progress requires an explicit goal completion referencing an accepted tool
  observation for that task. A model response or finish proposal alone receives no reward.
  Completion still uses J05's structural predicate; semantic success verification is future work.
- Novelty is the first fresh observation of an object ID within a live task, identified by
  its goal ID. New event IDs/descriptions for that same object do not earn more novelty.
  Different objects share a source/task novelty contribution cap. This is exact identity
  matching, not semantic or visual novelty detection.
- The second and subsequent runtime-recorded failed attempts increase frustration.
  `Goals.record_failure` is a trusted runtime/operator API: one call means one failed
  attempt; a future executor must deduplicate action-result delivery before calling it.
- Irrelevant observation/message bursts increase activation starting with the third
  input in the source/task window. High activation suppresses optional observations.
  Other event kinds and stale/future-dated observations do not contribute.

Default repetition windows are 60 seconds, with a per-component absolute contribution
cap of 0.3 and a `1/(1+count)` diminishing factor. Counters saturate; source, rule and task
form the grouping key. The first two irrelevant inputs count but contribute zero.
The stale threshold is 30 seconds against receipt time. All transitions retain evidence
IDs, versioned rules, configuration, decayed before/after state and applied contributions.
`goal_change:<seq>` references the lifecycle audit, whose completion row in turn cites
its observation. There is no live reappraisal when a rule/configuration changes.

Recovery uses UTC elapsed time and component-specific exponential constants. `state(now)`
is a read-only decayed view. Call `recover(now)` **once at runtime startup**, before
reviewing goals, to persist recovery capped at the configured interval (default one hour).
The explicit CLI recovery command demonstrates this until J07 owns application startup.
Negative elapsed time is clamped to zero and the persisted clock watermark never moves
backwards; the recovery/transition record flags the anomaly. Recovery fabricates no events.
Normal in-process decay has no recovery cap. Computation disabled freezes the vector and
consumes incoming appraisals without contributions; re-enabling never backfills rewards.

Goal review applies affect after eligibility and priority selection. Interest >= 0.28 adds
one optional observation, capped at three and the remaining hard step budget. Frustration
>= 0.2 selects `recover`; activation >= 0.8 suppresses optional sampling. Behaviour can be
turned off independently from computation. Tone is a separate persisted flag reported to
future consumers; no response wording is changed in this milestone. Global frustration
influences strategy only; it is never evidence of failure in the selected task. Permissions,
world facts, success predicates and the authorize/reservation path are unchanged.

The test suite compares the same goal/world with behavioural influence on/off: allowance
changes from one to two after novel evidence, while denied tools and exhausted budgets
remain ineligible. This proves a bounded parameter effect, not improved task performance.
No continuous scheduler, model-based appraisal, live devices, GUI or experiential learning
is introduced. J07 will assemble snapshots and coordinate these components. Repetition
keys/seen objects and audit retention need cleanup policies before continuous workloads.

## Context and cognitive coordinator (J07)

```bash
jane coordinator-demo
```

This deterministic, offline demonstration selects a persistent goal, builds a snapshot,
accepts a scripted scoped tool proposal, receives a **fixture-supplied** observation, then
accepts response and finish proposals citing that observation. It dispatches no tool,
emits no user response and does not mark the goal complete. The existing `jane demo`
continues to exercise the original fixture executor. J08 will connect accepted proposals
to durable intents, execution, verification and lifecycle effects. `wait` and `fail` are
validated proposals at this stage, not executed lifecycle transitions.

`Coordinator.step(interaction_id)` performs one async cognitive request (plus at most one
formatting repair) and returns a strict `Outcome`. Call `startup()` once under exclusive
application ownership before accepting work: it marks abandoned requests cancelled and
runs J06 capped affect recovery. No startup method may be used to steal a live request.
The runtime is one process, event loop and Journal owner; multi-process leader election,
provider subprocess termination and a continuous scheduler remain later work.

Migration 5 stores request snapshots, statuses, decision IDs, runtime usage and provider
call counts. A partial unique index permits only one active request in the database.
Calls are reserved before invocation and count against the configured interaction limit
(default eight), including formatting repairs and failed calls. Recreating the coordinator
or restarting cannot reset an interaction's consumed calls. A new interaction ID identifies
new operator/runtime work; the LLM does not choose it. Limits are trusted runtime settings.

Snapshots contain goal/success criterion/scope, generation, dependencies via their record
version, relevant world claims/predictions, global self-state, drives, profile, affect,
template, mock tool argument schemas and hard/optional budgets. Arbitrary nested
projections are canonical JSON strings to make the snapshot deeply immutable. Task events
are included in ingest order with provenance. The latest user instruction and observation
are essential; optional history is selected newest-first, preserving ingest order in the
final request. Relevant world claims and task constraints are never silently truncated.

Budgets are UTF-8 **bytes**, not estimated tokens: input defaults to 32 KiB and separately
reserved output to 8 KiB. Essential overflow records `context_overflow` without invoking
the adapter. A future provider must map its tokenizer and total context window onto these
limits. Oversized output is rejected without parsing or repair. Only schema/formatting
errors permit one repair; stale, unauthorized or semantically invalid decisions do not.

A content hash versions the full goal, dependencies, task evidence, relevant entity claims,
global self-state, profile and affect configuration. Completion checks recompute this view,
including time-dependent freshness. Superseding task input, cancellation/generation changes,
relevant corrections, changed profile or revoked tool access reject old results. Unrelated
task events do not invalidate the request unless they change a shared relevant record.
The affect vector is informational snapshot state; unrelated affect transitions alone do
not invalidate it. J08 must recheck execution preconditions and current budgets at dispatch.

All decision variants pass the canonical strict schema. References must be present in the
snapshot; duplicate decision IDs and context mismatches fail. Tools must be both scoped and
currently permitted. `finish` requires matching observation evidence under the initial
structural success predicate. Observation capture requests validate structurally but return
`unsupported_observation` until a capture adapter exists. An empty eligible goal set is idle
and performs no model call. Decisions do not reserve tool steps; J08 owns that boundary.

Cancellation immediately fences results and requests adapter cancellation. The caller
returns without waiting for a cancellation-resistant provider; that provider's in-process
slot stays occupied until it exits. Async adapters must yield to the loop: blocking native
work needs process isolation in a future provider. A configured real async timeout bounds
waiting, while the injected monotonic clock measures request elapsed time (deterministic
under FakeClock). Provider output cannot claim usage: calls, response bytes and elapsed
seconds are measured by the runtime; token/cost usage is not estimated. Errors are typed
`cognition.error` events, never raw provider exceptions or partial actions.

Accepted decisions and request status/usage commit atomically, as do typed errors and their
request status. Journal transactions now support nested SQLite savepoints for this purpose.
A storage failure propagates and leaves unfinished work for explicit startup recovery;
it does not acknowledge an accepted proposal. Formatting-repair prompts currently expose a
boolean repair flag to the offline adapter; real provider instructions are J09 work.
