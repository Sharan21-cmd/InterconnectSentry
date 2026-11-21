# InterconnectSentry backend core (pass 1)

## What works now
- Simulation engine that mirrors the browser Demo Lab model: the six built-in scenarios with the frontend's seeds and fault windows, topology presets (full, ring, star, mesh), per-link metrics, and a structured event log.
- Diagnosis: a link is healthy with no anomalies, faulted when anomalies exceed 10 percent of its transfers, and suspected otherwise.
- Adaptive test ranking: a heuristic information-split score over candidate tests, updated by test outcomes the caller supplies.
- SQLite history: completed runs only, written in one transaction, retained across restarts.

## Not done yet
- HTTP API. The frontend's Live Data panel expects http://127.0.0.1:8000/api/v1 and a GET /health handshake. FastAPI and uvicorn are not installed in the build sandbox, which has no network access, so no HTTP layer is included.
- WebSocket progress, comparison and export endpoints, and browser integration. The UI still runs its own in-browser model.
- Persisting planner state and test outcomes.

## Run
    cd backend
    python3 -m unittest discover -s tests -v

Parity with the browser model (needs node on PATH), from the project root:

    python3 backend/tools/parity_check.py interconnectsentry-preview.html

## Conventions
- Time is counted in intervals (20 per run). Latency and event `cyc` values are model cycles, not measured hardware clock cycles.
- Randomness is mulberry32, seeded per scenario. The same configuration reproduces the same output.
- Planner scores are heuristics, not probabilities. Test outcomes come from the caller; the backend does not invent them.
