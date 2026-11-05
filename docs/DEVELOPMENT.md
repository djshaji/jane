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
