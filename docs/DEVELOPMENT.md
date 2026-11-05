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

J01 and the initial J02 slice are implemented. Full profile/affect contracts, capture/output adapters, persistent journal, real coordinator, cancellation scheduling, drives, emotion dynamics, live providers, audio, camera and GUI remain future work. The mock tool only reads immutable fixture observations. There is no SQLite database or external side effect.

Core uses Pydantic; development adds Ruff, mypy and build. Media/provider dependency groups will be added with their adapters rather than installing unused dependencies now. No license has been selected.
