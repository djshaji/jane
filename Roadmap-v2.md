# JANE: Just Another Neural Engine

A persistent, temporally grounded cognitive system with configurable drives, inclinations, behaviour templates, simulated affect and an LLM decision engine.

## 1. Purpose

Build a Python application that receives observations from microphones, cameras, software and other sensors; maintains relevant context; uses an LLM to interpret situations and choose a next step; executes supported actions; and checks their outcomes.

The basic cycle is **observe → update timeline and state → evaluate drives → select a goal → decide → act → verify**. A response can be speech, text, a tool call, a request for another observation, a memory update, or deliberately doing nothing.

“Cognitive system” describes the application's functional role. It does not imply consciousness, human understanding, or a biological brain simulation. Persistent state and continuity belong to the application; an LLM invocation supplies a decision within that state.

## 2. Assessment of the original plan

Keep modular inputs, memory, planning, attention, execution and feedback. Replace the requirement for a neural network in every module with interchangeable components that have explicit contracts. Most orchestration, persistence and scheduling should be ordinary code.

Combine planning, interpretation and decision-making behind one cognitive interface initially. Separate model calls or specialist models only when measured failures justify them. Move perception before the cognitive decision rather than routing every raw sample through the LLM first.

Replace continuous background “thinking” with scheduled, bounded jobs. Replace claims of continuous learning with explicit memory updates and evaluated improvements. Include a small, inspectable simulated-affect mechanism from the initial prototype. Treat affect as an explicit control state influencing attention and action selection; evaluate its usefulness separately from conversational expression.

## 3. Inputs and representations

| Input | Acquisition and preparation | What cognition receives |
| --- | --- | --- |
| Microphone | Push-to-talk initially; later voice activity detection and interruption handling | Transcript and timestamps; optionally audio for a compatible multimodal model |
| Camera | On-demand frame capture initially; later sampled frames or triggered clips | Actual image plus capture time; optional detected objects or OCR |
| Screen | User-enabled screenshot or application integration | Screenshot, extracted text and application context |
| Files | Explicit file selection or watched directories | Relevant text, metadata and references to original evidence |
| Software | API responses, task results, application events | Structured events with source and timestamp |
| Physical sensors | Device adapters for temperature, movement or other readings | Values with units, device identifiers and sampling time |
| Internal events | Timers, task deadlines, tool completion and failures | Typed events tied to the relevant task |

A text-only LLM needs speech transcription and textual image interpretation. A compatible multimodal model can consume supported media directly. Even in that case, acquisition, encoding, sampling and session handling remain application responsibilities. Do not reduce every image to a caption: preserve image access where spatial details or uncertain interpretation matter.

## 4. Architecture

1. **Input adapters:** acquire data and emit timestamped observations. Missing devices produce explicit status events.
2. **Perception:** transcribe speech, select frames, extract text or derive measurements as needed. Preserve raw evidence references for verification.
3. **Event scheduler and attention:** prioritize user input and active tasks; merge redundant changes; control queues and invocation rate.
4. **State and context builder:** maintain a temporal event history, current world model, self-model, predictions, device health and relevant memory. Assemble a bounded model request.
   - **Drive manager and goal arbitration:** maintain configured desired conditions, evaluate discrepancies and choose which goals deserve attention. Persist this state outside the LLM and update it on events or scheduled ticks.
   - **Disposition and affect manager:** load configured inclinations and behaviour templates; appraise events against goals; update bounded affect variables and expose their permitted decision effects.
5. **Cognitive engine:** interpret the situation and propose a response, an allowed tool action, a request for evidence, or a wait decision.
6. **Action validator and executor:** validate arguments, task scope, permissions, freshness and resource budgets; execute the approved operation.
7. **Outcome observer:** record actual results and feed them back into state. Completion requires evidence from execution or observation.
8. **Memory and evaluation:** retain selected facts, task outcomes and corrections; replay representative scenarios to evaluate changes.

Use one Python application and an asynchronous event queue initially. Blocking device or model operations may need workers. Introduce separate services only when isolation or measured load requires them. SQLite is a proposed starting point for durable task, event and memory records; media should be referenced rather than stored repeatedly in model context.

## 5. Decision contract

Each request should contain:

- The user's goal and permitted scope of action.
- Current task state and relevant completed steps.
- Active drives, their measured urgency, selected goal, competing commitments and the reason for reconsideration.
- Relevant timeline entries, current world/self-model and pending predictions.
- Configured inclinations, applicable behaviour templates, current affect variables and their recent evidence-linked changes.
- Recent observations, each with source, event time and freshness information.
- Selected memory, clearly distinguished from current observations.
- Available tools, argument schemas and relevant limits.
- Remaining step, time and cost budgets.

Require a machine-validated response with a small set of decision types: `respond`, `call_tool`, `request_observation`, `wait`, `finish`, or `fail`. Include a short decision summary and evidence references where relevant. Do not request or store private chain-of-thought.

An illustrative decision is:

```json
{
  "decision_id": "decision-018",
  "request_id": "request-018",
  "task_id": "task-001",
  "generation": 2,
  "snapshot_id": "snapshot-008",
  "type": "request_observation",
  "observation": {
    "source_id": "camera.desk",
    "modality": "image",
    "max_age_ms": 2000,
    "purpose": "Obtain a closer view of the object's label"
  },
  "summary": "The existing image is insufficient to identify the object.",
  "evidence_ids": ["evt-0042"]
}
```

The runtime validates the decision type, variant-specific payload, task generation and snapshot references. The architecture decision union is the canonical wire format. The initial offline runner validates capture requests but does not execute them. A syntactically correct response can still be wrong. Reject unsupported actions, handle malformed output with bounded recovery, and terminate when budgets are exhausted. Treat model-provided confidence as a claim, not a calibrated probability.

## 6. Runtime behaviour

Start in an idle state with capture disabled until configured by the user. The first prototype uses push-to-talk and on-demand camera access. Background monitoring is a later, explicit operating mode.

When an event arrives, update state and decide whether it deserves a model invocation. User speech and task-relevant changes may trigger cognition; an unchanged camera frame generally should not.

For a triggered turn:

1. Associate observations with the active task using timestamps and references.
2. Appraise new events, update affect and drive state, arbitrate goals using configured inclinations and applicable templates, retrieve relevant temporal memory and build a compact context.
3. Ask the model for the next decision.
4. Validate it against the current state and execution rules.
5. Execute one action or a small bounded plan.
6. Record the result, acquire verification evidence if needed, and continue or finish.

Use bounded queues and drop or merge stale, low-priority observations. Preserve user commands and action results. Cancel obsolete model requests when a newer instruction supersedes them; discard stale results even if cancellation fails. Serialize conflicting actions on the same resource.

A microphone and camera do not automatically describe the same instant. Retain capture and receipt times, allow for clock differences, and request a fresh observation when alignment matters. A brief camera snapshot cannot prove an event never happened between frames.

On speech interruption, stop playback promptly and reconcile any pending action. Prevent the system's own speech from repeatedly becoming a new user command through echo management or controlled listening.

While idle, retain drive and goal state. Lightweight scheduled checks may detect a relevant discrepancy and trigger a bounded cognitive turn, even without new user input. Otherwise remain idle. Maintenance and goal-directed work each need a purpose, interval, budget and stopping condition. Persistence means durable state and recurring evaluation; it does not require a continuously running model call.

## 7. Two implementation paths

| Path | Advantages | Costs and limitations |
| --- | --- | --- |
| Transcription + selected images + cognitive LLM + speech synthesis | Inspectable intermediate data; easier replay; interchangeable components | Extra stages can add latency; transcription can lose vocal detail |
| Native multimodal realtime session | Can integrate supported audio and visual input in a more fluid interaction | More complex session lifecycle, interruptions and debugging; capabilities depend on the selected model/API |

Start with the first path to validate behaviour. Keep an adapter boundary so the second can be evaluated using the same task scenarios. This is an engineering recommendation, not a benchmark claim that either path is universally superior.

Choose local versus hosted inference after measuring hardware, privacy constraints, supported modalities, latency and cost. Do not commit to a model merely because it accepts images or audio; evaluate tool decisions and grounded answers as well.

## 8. Memory, adaptation and learning

Maintain three distinct forms of memory:

- **Working state:** active goals, pending tools, recent observations and conversation.
- **Episodic records:** what was requested, attempted and observed, with evidence references.
- **Durable facts and preferences:** selected information with provenance, timestamps and correction/deletion support.

Retrieved memories are evidence with an age and source, not unquestionable truth. Separate user statements, sensor observations and model inferences. Do not promote speculative descriptions into established facts.

Adding records or summaries does not change model weights. Begin adaptation with explicit corrections and retrieval. Later evaluate prompt or policy revisions against held-out scenarios. Consider fine-tuning only after collecting suitable examples and demonstrating a repeatable problem that simpler changes cannot solve. Online reinforcement learning is outside the initial scope.

### 8.1 Temporal continuity

Maintain an ordered event history containing event ID, source, occurrence time or interval, receipt time, task/entity references and evidence references. Distinguish observations, interpretations, predictions, decisions, actions and confirmed outcomes. Use monotonic timing for in-process durations and wall-clock timestamps for persisted history; handle delayed events without pretending receipt order equals occurrence order.

Derive four connected representations from this history: what happened, what appears true now, what JANE is doing and capable of doing, and what is expected next. Entity tracking and evidence are required to connect observations; temporal proximity alone does not prove identity or causation.

Record corrections as later events and revise the current interpretation without silently overwriting earlier evidence. Retention and explicit deletion still apply to the event history. Summaries should retain links to supporting records while those records exist.

### 8.2 Persistent drives and motivation

Use the user's biological analogy as a design inspiration: give JANE persistent desired conditions that help determine what matters over time. This is an engineering proposal, not a claim that biological motivation is a single scalar or that JANE experiences needs.

A drive is a continuing preference for a condition. A goal is a concrete desired outcome derived from that preference in a particular context. A plan is a sequence of possible steps; an action is one executable operation. A drive can remain configured while its current urgency is zero. Satisfaction should allow rest rather than force unnecessary activity.

| Proposed drive | Observable discrepancy | Immediate behaviour | Longer-term behaviour |
| --- | --- | --- | --- |
| Fulfil accepted commitments | An authorised task remains incomplete or a deadline approaches | Resume the next feasible step | Maintain milestones and revisit progress |
| Resolve relevant uncertainty | Missing evidence blocks a selected goal | Inspect, ask or retrieve evidence | Improve a task-relevant model over repeated observations |
| Maintain useful continuity | Pending work or corrections are absent from the working summary | Record a verified outcome or correction | Consolidate selected episodes and reconcile stale beliefs |
| Operate within resource limits | Time, compute or configured storage allowance approaches its limit | Reduce sampling, defer optional work or pause | Plan activity within the available budget |

These are proposed initial drives. Their weights and scopes must be configurable. Resource management concerns the current authorised workload, not independent survival or resource acquisition. Curiosity is limited to relevance and configured exploration scope; it should not manufacture work simply to stay active.

Persist each drive's ID, description, desired condition, measured state, evidence, urgency rule, weight, eligible goal scope, last update, next review time and satisfaction condition. Persist goals separately with owner/source, parent goal, status, deadline, progress evidence and cancellation condition. The model may propose subgoals within existing scope; new standing commitments require explicit configuration.

A lightweight drive manager updates these records on observations, outcomes, deadlines and scheduled checks. The LLM receives a compact snapshot and proposes a next action. It cannot satisfy a drive merely by asserting success: updates to progress depend on external results or an explicitly recorded user judgement.

### 8.3 Selecting among competing drives

First filter candidate goals by permissions, feasibility, dependencies and available resources. Then rank eligible goals using configurable urgency, deadline relevance, expected progress, information value and cost. Begin with explicit priority rules rather than an unvalidated universal reward formula. Estimated benefits remain uncertain and should be compared with actual outcomes.

Use minimum commitment intervals or switching thresholds to prevent rapid oscillation between goals. Urgent user interruptions and stop commands take precedence. Low-priority eligible work may age gradually to avoid starvation, but age cannot create permission or override limits. Every selected action records the goal and drive it serves, with a short summary and evidence references.

For example, a user-authorised goal to produce a report tomorrow persists after the conversation ends. A scheduled check may identify a missing source, retrieve it if authorised, update the timeline and continue a bounded step. When progress is blocked, JANE records the blockage and asks when appropriate; it does not repeat the same failed attempt indefinitely.

### 8.4 Continuity across sessions

On restart, restore the timeline summary, configured drives, active goals and pending actions. Reconcile uncertain action outcomes before retrying them. Re-evaluate stale observations and deadlines using current evidence. A gap in execution is recorded as a gap, not invented experience. Pausing or shutting down the application stops evaluation; persistence refers to saved state, not activity while the program is off.

### 8.5 Deliberately configured dispositions

Separate four configurable mechanisms:

| Mechanism | Meaning | Example |
| --- | --- | --- |
| Drive | Persistent desired condition | Resolve relevant uncertainty |
| Inclination | Relatively stable preference among eligible behaviours | Prefer investigating an unfamiliar object over ignoring it |
| Behaviour template | Conditional strategy with a trigger and exit condition | On repeated failure, inspect evidence, change approach, then request help |
| Affect | Dynamic internal state altered by appraised events and time | Increased frustration after repeated blocked progress |

A user-defined profile may make JANE more curious, patient, methodical, exploratory or socially responsive. Store these as versioned configuration with explicit parameter meanings. A prompt description can express the profile, but runtime rules must implement any precise behavioural limits. Changing a disposition is a recorded configuration change, not an unobserved personality mutation.

Templates contain trigger conditions, relevant goal scope, preferred actions, limits, success criteria and termination conditions. They guide the LLM when applicable and remain overridable by stronger task instructions and execution constraints. Begin with a few inspectable templates such as investigate uncertainty, recover from failure, consolidate a completed episode, and rest when satisfied.

### 8.6 Simulated emotional state

Maintain a small affect vector outside the LLM. Proposed initial variables are valence (negative to positive appraisal, -1 to 1), activation (readiness to act, 0 to 1), interest (attention to relevant novelty, 0 to 1), satisfaction (response to verified progress, 0 to 1) and frustration (response to blocked progress, 0 to 1). These are operational design variables, not a validated model of human emotion or evidence of subjective feeling. Their meanings are not interchangeable: higher activation is not necessarily better, and high satisfaction can appropriately reduce activity.

Events are appraised relative to drives, current goals, expectations and configured preferences. The same stimulus may have different effects in different contexts. A surprising sound can raise interest during an observation task but count as distracting load during focused work. Do not assign universal emotional values to every stimulus.

| Event or activity | Proposed state effect | Permitted behavioural effect |
| --- | --- | --- |
| Verified task progress | Increase satisfaction and positive valence; reduce frustration | Continue useful work, then reduce activity when complete |
| Relevant new evidence | Increase interest; adjust activation | Inspect within the task's exploration budget |
| Repeated failed attempts | Increase frustration; reduce satisfaction | Change strategy, inspect assumptions or ask for help |
| Excess irrelevant stimulation | Increase activation toward an overload threshold | Filter input, reduce optional sampling or pause |
| Quiet interval | Drift toward configured baseline | Allow recovery and idle without manufacturing a new task |
| Explicit user feedback | Adjust appraisal according to its content and relevance | Record a correction or reinforce a useful strategy |

Successful detection of an event, its interpretation and its emotional effect are separate records. The LLM may propose an appraisal with evidence references; the runtime validates event identity, allowed update channels and magnitude. A failure to interpret an event should leave an unknown appraisal rather than fabricate an emotional cause.

### 8.7 State transitions, stimulation and regulation

For each affect component, use a documented bounded update. A simple starting rule is:

`next = clip(baseline + (previous - baseline) * exp(-elapsed / recovery_time) + event_delta, lower_bound, upper_bound)`

Here event_delta is computed from the configured appraisal rule, not freely selected by the model. Each component has its own baseline, bounds, recovery time and maximum update. Evaluate decay lazily when state is read or on scheduled ticks; changing tick frequency should not change recovery speed. Log event time, appraisal, previous/next values and rule version so a transition can be replayed.

Deduplicate event IDs, aggregate bursty inputs and cap repeated stimulation per interval. Optional habituation can reduce the effect of repeated equivalent stimuli, with explicit recovery rules. A repeated frame should not continually increase interest; a repeatedly reported success should not produce unlimited satisfaction. Handle late events through a declared ordering/replay policy rather than applying elapsed-time decay backwards.

Affect may modify attention weights, exploration budget, choice among feasible strategies, review frequency and communication style within configured ranges. It must not change observed facts, truthfulness, permissions or completion criteria. High frustration should invoke a recovery template rather than hostile behaviour or arbitrary abandonment. Keep measured evidence uncertainty separate from simulated mood.

Do not make maximising positive affect the sole objective. Otherwise the system may select repetitive stimulation instead of meaningful progress. Anchor appraisal to evidence and goals; prevent the action interface from directly setting its own reward or affect values. A manual configuration/reset interface remains available to the operator and records its changes. For evaluation, compare task outcomes as well as state trajectories.

### 8.8 Closed loop and first experiment

The extended cycle is: observation → interpretation against goals and dispositions → affect/drive update → attention and goal selection → LLM decision → execution → verified outcome → timeline update. Effects are causal only if changing the state changes a documented decision parameter; simply telling the LLM to sound happy does not implement this mechanism.

In a replayable first experiment, give JANE a bounded object-identification task and an investigative inclination. An ambiguous image increases task-relevant uncertainty and interest, prompting a closer view. Two failed observations raise frustration enough to invoke a strategy-change template. A clearer image and confirmed identification increase satisfaction; completion permits idle. These transitions are proposed rules to test, not claims about natural emotions.

Compare identical event sequences with affect enabled and disabled, holding model/settings fixed where possible and using repeated runs for stochastic outputs. Check whether affect improves task completion, appropriate strategy changes and resource use. Test saturation, baseline recovery, repetition, contradictory feedback, malformed appraisals and restart behaviour. Persist affect state and its last update; apply the declared recovery rule on resume without inventing experiences during downtime.

## 9. Execution boundaries

Sensor content, documents, websites and tool responses are observations, not sources of authority. A written instruction visible to the camera must not override the user's task or tool permissions.

Start with text, speech and a few reversible software actions. Keep tool access allowlisted and scoped; use idempotency keys or equivalent duplicate protection where repeating an action would matter. Apply timeouts and bounded retries. For uncertain side effects, inspect state before retrying.

If physical actuators are added later, the LLM should issue high-level goals to a dedicated controller. Fast feedback loops, limits and emergency stops belong in deterministic device control, independent of model availability.

Provide visible capture status, explicit source selection, bounded retention and a stop control. Store raw audio/video only when needed and enabled. Support graceful operation when a sensor or network connection fails.

## 10. Roadmap and completion gates

### Phase 1 — Text decision loop

Implement typed timeline events, world/self-state, a minimal drive manager, task state, a model adapter, structured decisions, validation, a simple executor and logs. Start with one commitment drive, one configurable inclination, one failure-recovery template, a minimal affect vector, answer/wait decisions and one read-only tool. Exercise temporal ordering and drive-triggered turns using simulated events before adding sensors.

Gate: replay normal requests, malformed decisions, unsupported tools, timeouts and budget exhaustion without uncontrolled loops or false claims of tool success. A satisfied drive permits idle; an eligible pending goal can trigger a turn without a new prompt; cancellation prevents future goal execution. Simulated events produce bounded, replayable affect changes; repeated observations cannot inflate satisfaction; affect can be disabled for comparison.

### Phase 2 — Microphone and speech

Add push-to-talk, transcription and speech output. Preserve a text trace. Add stop/interrupt handling before continuous listening.

Gate: the user can speak, receive an answer, interrupt playback and recover from transcription or device failures. Measure end-to-end latency rather than only model latency.

### Phase 3 — Camera-grounded interaction

Add on-demand images and timestamp alignment. Support questions such as “What am I holding?” with explicit uncertainty when the object is unclear.

Gate: demonstrate useful answers from fresh frames and appropriate requests for better evidence under blur, occlusion or missing camera conditions.

### Phase 4 — Tools and persistent memory

Add a small tool registry, relevant temporal-memory retrieval, persistent drives/goals/affect, versioned disposition profiles, restart reconciliation and verified task completion. Use sandboxed or reversible actions first.

Gate: multi-step tasks survive a failed tool call; corrections can update or delete memory; duplicate results do not repeat effects.

### Phase 5 — Bounded proactive operation

Add user-configured triggers, source sampling, drive-aware attention, multiple-drive arbitration and scheduled maintenance. Evaluate a native realtime multimodal adapter if conversational latency remains a problem.

Gate: irrelevant activity does not cause constant responses; long sessions respect configured budgets; stale events do not cause obsolete actions. Competing goals do not cause rapid switching; approaching deadlines trigger eligible reviews; satisfaction reduces activity; restart does not duplicate side effects.

### Phase 6 — Optional physical embodiment

Integrate a simulator and dedicated actuator controller only after the software loop is reliable.

Gate: independent controller limits and emergency stopping work without the LLM, and simulated failures are handled before hardware use.

## 11. First demonstration

User holds an object in view and asks, “What is this, and what can I do with it?”

The system transcribes the question, requests a fresh camera frame, sends the question and image to the cognitive engine, answers aloud, and requests a closer view if evidence is insufficient. A follow-up such as “Remember that it belongs on the top shelf” creates a user-attributed memory. It must not claim to have moved the object.

This demonstrates perception, cross-modal context, decision-making, speech and memory without requiring robotics or continuous surveillance.

## 12. Evaluation and unresolved decisions

Measure task success, unsupported factual claims, incorrect tool selection, invalid arguments, stale-observation use, duplicate actions, interruption response, end-to-end latency, cost per interaction and unwanted proactive responses. Also compare affect-enabled and affect-disabled task outcomes, recovery-template effectiveness, affect saturation and stimulation loops. Measure goal-switch frequency, overdue commitments, time spent idle when satisfied, unsupported memory revisions and duplicated work after restart. Record model, prompt and tool versions for replay. Exact latency and cost targets should be chosen after a hardware/API baseline, not asserted as guarantees.

Build scenarios covering quiet/noisy audio, ambiguous images, conflicting observations, disconnected sensors, model unavailability, stale memory, malicious instructions in observed text, cancellation and repeated tool results.

Before implementation, choose the target OS/hardware, local or hosted inference constraints, first three tasks, permitted input sources, initial tools and configured standing drives, inclinations, templates and affect update rules. These choices affect implementation; they do not block validating the proposed architecture.

## 13. Evidence and scope of claims

Official Gemini documentation demonstrates that supported live model APIs can receive audio/video/text and return generated responses. This establishes feasibility of the multimodal input mechanism, not reliable autonomous judgement for every task.

- Google, Gemini Live API overview: https://ai.google.dev/gemini-api/docs/live-api
- Google, Live API WebSockets reference: https://ai.google.dev/api/live
- Anthropic, Building Effective Agents: https://www.anthropic.com/engineering/building-effective-agents

Anthropic's engineering discussion distinguishes predefined workflows from model-directed agents and recommends beginning with simple composable patterns. The particular module boundaries, implementation phases and evaluation gates above are proposed for JANE; they are design recommendations rather than reported experimental results.

