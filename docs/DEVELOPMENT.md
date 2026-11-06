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

## Durable actions and full offline loop (J08)

```bash
jane offline submit jane.db --text "Inspect the simulated cup" --object cup
jane offline run jane.db
jane offline status jane.db
jane offline timeline jane.db
jane offline pause jane.db
jane offline resume jane.db
jane offline stop jane.db
```

These commands now run an offline application loop, not just proposal validation. `submit`
records the text and creates a scoped inspection goal with an explicit target object.
The adapter is a deterministic inspection policy: it does **not** understand arbitrary
natural-language requests or call an LLM. `--object` declares the simulated object to inspect.
`run` executes at most eight loop steps by default (`--max-steps` accepts 1–32); the persistent
J07 call limit also applies. Submission does not automatically run work. `status` shows
persistent goals/actions/control state. `timeline` includes accepted events and the separate
action transition audit. Responses are durable `communication.response` records visible
there; no network, microphone, speaker, camera or GUI is used.

The first new simulator starts with fail, fail, succeed. The third inspection uses the
recovery template. Verified evidence then supports a response and explicit goal completion.
A second process can inspect the same completed goal without re-executing it. Simulator
attempt position persists: subsequent inspections succeed by default. `jane demo` and
`jane coordinator-demo` retain their original deterministic fixture/proposal meanings.

Migration 6 adds action intents, lifecycle audit, applied-decision receipts and persisted
pause/stop controls. Intents reference accepted cognitive requests and store a unique
idempotency key, argument payload, task generation, resource key and relevant-state hash.
Preparation validates scope and permissions and atomically reserves one tool-budget step.
Redispatch of an already prepared/completed request does not reserve again. A reservation
cancelled before dispatch remains consumed conservatively. Definite-failure retries reserve
another step and reuse the key; they are limited to three dispatch attempts and the goal's
remaining budget. Verified goals may respond/finish after their tool budget is exhausted,
with an empty available-tool list and no extra tool authorization.

Registered contracts cover `mock.inspect` (immutable read-only fixtures) and `sim.inspect`
(a local stateful simulator). Both declare argument/result schemas, permission name,
resource key, effect class, idempotency and reconciliation strategy. The registry rejects
unknown/changed contracts; it does not import arbitrary code from model output. A unique
partial index holds a resource while its action is prepared, running, unknown or needs review.
Dispatch rechecks generation, selected goal, permissions, runtime controls and the read set.
Intent/running status commits **before** calling the tool, outside any journal transaction.
A successful call is not sufficient: the tool's direct postcondition must verify its result
before the observation and succeeded status are committed together.

The simulator has a separate durable database, `jane.db.sim.db`, representing external
state that cannot commit atomically with JANE's journal. Keep both files for restart tests.
Its Python API accepts modes `succeed`, `fail`, `timeout`, `timeout_after_effect`, `crash`
and `unknown`. It records receipts and unique-key effects; postcondition checks compare
the receipt, effect entry, requested object and returned observation. `crash` raises a
special BaseException after committing the external effect, deliberately bypassing normal
runtime error handling. No real external system is modified.

Exclusive startup reconciles running/unknown actions. Confirmed success records the real
result once; confirmed no-effect becomes failed; ambiguity becomes needs_review and retains
the resource lock. **Unknown effects are never blindly retried**, even for these idempotent
tools. `Executor.reconcile(request)` can recheck needs_review after external evidence changes;
there is no unsafe force-success or unlock command. Human resolution UI is later work.
`Executor.dispatch(request, retry=True)` only retries prepared/definitely failed work within
its bounds. The CLI normally makes fresh proposals after definite failures, which lets the
next snapshot show changed failures and the recovery template. It stops on unknown/review
outcomes. Prepared intents and accepted-but-unapplied proposals can resume after restart;
stale proposals are rejected rather than repeatedly attempted.

Goal completion is separate from tool success: `finish` must cite a succeeded action's
observation for the same task, generation and explicit target object. A fixture-injected
observation alone cannot complete an executable goal. `respond`, `wait` and `fail` now have
idempotent durable lifecycle applications. Waiting stores a timer; a later explicit `run`
reviews it. There is no continuously running scheduler.

Pause prevents further dispatch/submission; resume permits work again. Every control change
advances a version, invalidating older prepared snapshots. Stop cancels nonterminal goals
and prepared actions; resume does not revive cancelled goals. Effects already committed
cannot be undone: if a tool completes after cancellation, JANE records its actual outcome
without completing the cancelled goal or executing more of its plan.

The runtime requires one application owner and short synchronous offline tools. Do not run
multiple `run` processes simultaneously or call startup recovery against an active worker.
Cross-process leadership, interruptible real tools and hard process-isolated timeouts remain
future work. Unknown outcomes deliberately block the offline loop pending reconciliation.
Simulator timeout modes model ambiguous outcomes; they are not a production deadline runner.
There is no universal exactly-once claim across arbitrary external systems.

## First live text provider (J09)

J09 adds an optional OpenAI Responses adapter for `gpt-6.1-sol`, default reasoning `medium`.
The supported model/capability profile is explicit: text input, structured decision output,
no media, hosted tools or arbitrary provider endpoints. Unsupported models/modalities fail
configuration validation. The provider is interchangeable with the scripted adapter; the
same J07 decision checks and J08 execution/postcondition checks remain authoritative.
Only the local simulated inspection tool is exposed by this first live CLI.

Official documentation checked on 6 November 2026:

- [Model capabilities and reasoning settings](https://developers.openai.com/api/docs/models/gpt-6.1-sol)
- [Structured output schema rules](https://developers.openai.com/api/docs/guides/structured-outputs)
- [Responses API](https://developers.openai.com/api/docs/guides/responses)

The model page publishes an alias rather than a dated snapshot. Requests record the exact
requested alias, settings and returned model identifier; this does not guarantee unchanged
behaviour if the alias changes. Structured output uses an object envelope containing the
canonical decision union as nested `anyOf`. Provider schema conversion removes unsupported
annotation/discriminator forms; strict internal Pydantic validation still runs afterward.
The model only proposes decisions. No hosted tool is configured or executed by OpenAI.

```bash
python -m pip install -e '.[openai]'
# Supply OPENAI_API_KEY through your local environment or secret manager.
# Do not put the key in a goal file, command argument, committed file or database.
jane live submit jane.db --text "Inspect the simulated cup and report what you observe" --object cup
jane live run jane.db --enable-live --max-calls 8 --max-output-tokens 4096 --reasoning medium
jane live status jane.db
jane live timeline jane.db
```

Only `live run --enable-live` invokes inference. `live submit/status/timeline/pause/resume/stop`
use the same local commands as offline mode. The explicit run flag acknowledges that the
snapshot's task text and evidence will be sent to OpenAI and may incur API charges.
`OPENAI_API_KEY` is required; missing credentials fail before opening/mutating a database.
There is no fallback to a different model or offline policy after a provider failure.
Install HTTPX SOCKS support separately if your configured proxy requires it; normal proxy
and CA environment configuration is preserved. Redirects are disabled and the endpoint
is fixed to `https://api.openai.com/v1/responses`.

The adapter performs one HTTP attempt per `decide`. The coordinator permits at most one
retry for 429 or selected 5xx errors by default (configurable 0–2); each counts against the
same durable interaction call limit, including across restarts. It waits a bounded second
between attempts. A malformed response permits at most one formatting repair. Authentication,
refusal, incomplete output, timeout and ambiguous network failures are not automatically
retried. All failures become typed error events and leave the goal recoverable. Timeout or
cancellation aborts the local HTTP request and fences any late result; it cannot guarantee
that remote processing or billing has stopped. Stop/pause controls still prevent stale
results from dispatching actions, including when inference has already begun.

Each call sets `max_output_tokens` (default 4096, configurable 256–8192), including reasoning
usage. The coordinator defaults to eight total calls per interaction. Input snapshot limits
remain byte-based; the provider adds a full request-body byte limit including instructions
and schema. HTTP responses are streamed into a capped 1 MiB buffer and decision text retains
the snapshot's byte limit. These are bounds, not an exact monetary budget or token estimate.
An incomplete response can require an explicit configuration change before retrying.

Migration 7 adds `provider_attempts`. Before each provider attempt, the runtime persists the
model/settings; after it returns, it records HTTP status and API-reported input/output/total
tokens when available. Generated decision fields cannot supply usage. Cancelled requests,
transport failures and crashes may have unknown usage (`null`); do not count that as zero
cost. Runtime call reservations survive even if the process dies before HTTP dispatch.
The core request table continues to measure calls, output bytes and injected-clock elapsed
time. No price estimate or hidden SDK retry is used.

Credentials exist only in the transport Authorization header, never in snapshots or audit
rows. Provider error bodies, refusal wording and raw exceptions are discarded; audit rows
contain only whitelisted metadata. The normal journal still stores user evidence and
accepted decisions under the existing J07 policy. Requests use `store=false`; this is an API
storage setting, not a claim about the provider's broader retention policies.

Tests use fake transports and HTTPX MockTransport: schema conversion, rate limits, retry
budgets, malformed output/repair, timeout, cancellation, refusal, incomplete response,
unsupported modality, usage spoofing, request size and sanitized HTTP errors. A fake
provider also completes the full existing inspection loop without network access.

The real contract test is **opt-in and paid**, with a maximum of one model request and no
action execution:

```bash
JANE_LIVE_TEST=1 python -m unittest discover -s tests -p test_provider.py -v
```

It additionally requires `OPENAI_API_KEY` and the optional dependency. Default CI never
sets this flag or needs a key. This implementation session had no configured API key, so
live model availability, schema acceptance and real task quality remain unverified; run the
opt-in test before enabling the adapter for regular use. Audio/camera input, semantic goal
creation, general-purpose real tools and GUI remain outside J09's text inspection scope.

## Push-to-talk voice (J10)

This first voice slice targets Linux terminals. It adds microphone → transcription →
reviewed text → the existing bounded cognitive/execution loop → local synthesized speech.
It does not add general-purpose goal interpretation, real tools, camera input or a GUI.
Use one process and a dedicated database; unfinished existing goals must be resolved before
another voice task can be sent. `--object` selects the simulated object, not a visual target.

Install the distribution's PortAudio runtime and `espeak-ng` system packages (on Debian/
Ubuntu these are normally `libportaudio2` and `espeak-ng`), then:

```bash
python -m pip install --no-build-isolation -e '.[voice]'
# Configure OPENAI_API_KEY in your environment, without committing it.
jane voice devices
jane voice session --database jane-voice.db --device 0 --object cup \
  --enable-microphone --enable-live
```

Omit `--device` for the default input. Discovery never starts capture. Neither import nor
installation opens a microphone. Both enable flags are required before a session starts;
each recording additionally requires the `record` command. Speech is AI-generated and
uses the system's default output device through eSpeak NG. No hosted synthesis API is used.

Terminal commands (type each and press Enter):

1. `record`: begin a new utterance. Speak for less than 15 seconds.
2. `release`: close the input stream and start transcription asynchronously.
3. `status`: inspect progress; when state is `preview`, review the final transcript.
4. `send`: explicitly accept that transcript and run the inspection task.
5. `stop`: cancel the turn, clear the preview, close capture and terminate speech output.
6. `quit` or EOF: perform the same cleanup and exit.

This is a two-command push-to-talk boundary, not raw keyboard key-down/key-up capture.
There is no always-listening wake word or acoustic echo cancellation. `record` is rejected
while transcribing, previewing, thinking or speaking; use `stop` to discard the old turn.
Partial transcripts are presentation-only and generation-fenced. The current HTTP adapter
returns finals only. A final is never actionable until `send`. Noise can fool any ASR model;
the preview confirmation is required even when quality checks pass.

Capture is mono PCM16 at 16 kHz, with a 480,000-byte (15 second) hard buffer cap. Overflow,
PortAudio callback errors, inactive/disconnected devices, very short recordings, low RMS
silence and heavily clipped input reject the entire utterance. Nothing silently truncates a
command. Unsupported device sample rates fail visibly. Reconnect, select a working device
(or restart with another `--device`) and record again. Raw PCM and WAV exist only in memory;
the buffer is cleared on release/abort and the WAV is dropped after transcription or
cancellation. No audio file is written. An in-flight adapter can retain bytes until its
request returns/cancels; this is reference release, not guaranteed secure memory erasure.

The hosted transcriber uses [OpenAI's speech-to-text API](https://developers.openai.com/api/docs/guides/speech-to-text),
`gpt-transcribe`, WAV multipart uploads, a 30-second HTTP timeout and a 32 KiB response cap.
There are no redirects, hidden retries or model fallbacks. Empty text or an unidentified
language fails closed. Each release makes at most one transcription request; repeated
recordings are new paid requests. Audio and transcripts leave the machine, and cancellation
does not guarantee remote processing/billing stops. The existing J09 per-interaction
cognitive budgets still apply. Transcription usage/cost is not yet durably metered.

Accepted speech is a canonical `user.message` with source `microphone`, a unique capture
association and capture start/end timestamps. It enters the existing journal/goal transaction;
partial or rejected transcripts do not. The transcript, goal, evidence and decisions remain
in SQLite under the normal retention policy. Quality (duration, RMS, clipped fraction),
device/transcription failures and stop timings are kept in a bounded 64-record session audit
and shown by `status`; these diagnostics are not yet persisted across process exits.

Stop advances the session generation immediately and cancels the coordinator/current goal.
It then terminates the eSpeak NG child independently of inference, escalating to kill after
0.5 seconds if needed. Process creation is cancellation-safe; an old task cannot terminate a
new utterance. Metrics distinguish `ack_seconds` from `playback_process_stop_seconds`.
**Process exit is not acoustic silence**: output-device/server buffers can still drain. Do
not report these numbers as measured physical playback-stop latency.

Offline tests use synthetic PCM, fake devices, a fake speaker, fake time, real SQLite and
HTTPX MockTransport. They cover overflow, clipping, silence, unplug/retry, confirmation,
partial/final fencing, echo gating, late inference suppression, subprocess spawn races and
kill escalation. CI does not install or activate audio hardware and never calls a live API.

Hardware/provider release gate still pending (no microphone, eSpeak NG or API key available
in the implementation environment):

- Record a clear request, review it, send it and hear the verified simulated response.
- Try silence and unintelligible noise; discard inaccurate previews rather than sending them.
- Exceed 15 seconds and confirm rejection; unplug during capture and verify reconnect/retry.
- Interrupt during transcription, inference and speech; verify no late task or speech starts.
- Measure acknowledgment, process termination and actual audible-stop latency separately
  using an external recorder/loopback. Confirm no playback is captured after a stop/restart.

Live acoustic validation, durable audio diagnostics/usage, streamed partial transcription,
and desktop push-to-talk controls remain follow-up work. This slice does not claim the full
J10/M3 hardware acceptance gate has passed.

## On-demand camera evidence (J11)

This initial J11 slice captures and prepares actual image evidence. It does **not** yet
identify objects through a live vision model or complete the M3 voice/camera demonstration.
Camera capture is a separate CLI operation, and it never sends pixels over the network.

```bash
python -m pip install --no-build-isolation -e '.[camera]'
# Use the task ID returned by offline/live submit, for example offline-1:
jane camera capture jane.db --task offline-1 --device 0 --enable-camera
# Explicitly retain pixels for later inspection:
jane camera capture jane.db --task offline-1 --device 0 --enable-camera --retain-image
jane camera status jane.db
jane camera expire jane.db --asset image-REPLACE_WITH_RETURNED_ID
# Exclusive recovery after a crash; stop all other JANE processes first:
jane camera recover jane.db
```

`--enable-camera` permits a single capture, never monitoring. Without `--retain-image`, the
CLI finalizes the image, commits its event, then expires the processing file before returning.
The event and metadata remain; they cannot be used as available pixels after expiry. With
explicit retention, the file stays in `jane.db.media` until `expire`. `recover` removes
interrupted temporary/orphan files, expires leftover processing-only assets and completes
interrupted deletion. Run recovery after a crash. No automatic timed retention policy or
background collector is introduced. Use the existing single-writer/exclusive-startup model.

The optional OpenCV backend runs in a separate one-shot Python worker. The parent enforces
an eight-second timeout; a hung/disconnected driver is killed without blocking the engine
indefinitely. The worker releases its device, requests 1280×720 and downscales larger frames
before PNG encoding. Linux/OpenCV device indexes are used; permissions and driver support
are system dependencies. No camera opens at import, install, status or help time.

Pillow validates a single JPEG/PNG, bounds compressed input/output to 4 MiB and decoded
images to four million pixels with each dimension at most 4096, converts to RGB PNG and
removes supplied EXIF/other metadata. Standard deviation of grayscale intensity is a
conservative low-contrast quality hint: flat/covered and heavily blurred fixture images
request a clearer view. It is **not** a general blur, occlusion or object detector. A usable
quality flag does not establish semantic confidence or object identity.

Migration 8 adds `assets`: opaque ID, absolute managed path, validated metadata, SHA-256,
byte size, MIME type, dimensions, creation time, quality, retention preference and lifecycle
state. Media writes use a private temporary file, fsync, atomic rename and directory fsync
before the asset row and `sensor.image` event commit in one SQLite transaction. The journal
rejects references to missing, expired or mismatched assets. A crash between rename and DB
commit can leave an orphan, which exclusive recovery removes. Never copy just the database
when backing up retained media; moved paths require restoration to the original location
in this first implementation. Digest checks detect corruption; they do not authenticate
what the camera depicts.

Image events retain task/turn/source IDs and host capture intervals. They enter the timeline
without creating world-model identity or disappearance claims. Camera failures append source
health events; a subsequent failure invalidates reuse of an older successful frame. A fresh
successful capture restores source health. Missing, expired or corrupted files request a new
capture rather than a fabricated caption.

`CameraService.current` refreshes stale/invalid evidence even for an unchanged scene; a failed
refresh never returns cached pixels. Freshness uses the capture start plus a conservative
1000 ms uncertainty allowance. This is a configured allowance, not measured sensor exposure
latency. Tight freshness requirements can reject even a new frame. Cross-input alignment
requires the same task and turn and checks interval separation plus both clock uncertainties;
nearby timestamps never prove that speech refers to a particular object.

`ImageContext` hands an image-capable consumer the verified bytes and metadata. Freshness
and integrity are checked again after its result returns; stale results are rejected.
Processing-only assets expire after this handoff (including cancellation/failure); explicitly
retained assets stay available. Consumer output is advisory and does not execute actions or
bypass canonical decision validation. No production vision consumer is shipped in this slice.
The scripted test consumer proves direct pixel access without claiming visual recognition.

Latest image evidence is essential context: it cannot silently disappear because of the
snapshot budget. The current J09 OpenAI adapter rejects image-bearing snapshots with
`unsupported_observation` **before HTTP dispatch**. It never sends an image ID as a substitute
for seeing pixels, and it does not silently fall back to a caption. Consequently an existing
task with image evidence needs a future compatible adapter; text-only tasks continue to work.

Offline tests cover explicit enablement, strict references, atomic failure/orphan recovery,
restart, processing/retained lifecycles, stale stationary scenes, source failure/recovery,
missing/corrupted media, covered/blurred fixtures, temporal alignment, late consumer results,
modality rejection and bounded camera-worker timeout. Tests need Pillow but no OpenCV,
camera device, model credentials or network. CI also exercises camera help from the wheel.

Remaining acceptance: real device capture/unplug testing, calibrated quality and clock
uncertainty, a live image-capable provider integrated with canonical decisions, wiring model
observation requests to capture, and the combined spoken object-identification/confirmation
flow. GUI integration remains in the desktop work packages. Do not mark J11/M3 complete
until those gates pass.

Backend references: [OpenCV Python installation](https://docs.opencv.org/4.13.0/db/dd1/tutorial_py_pip_install.html),
[OpenCV package](https://pypi.org/project/opencv-python-headless/4.12.0.88/),
[Pillow package](https://pypi.org/project/pillow/12.0.0/).
