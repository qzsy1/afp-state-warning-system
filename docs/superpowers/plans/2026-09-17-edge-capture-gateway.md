# AFP Edge Capture Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route LAN/public real acquisition through the visitor computer's local helper and feed its ordered samples into the existing server prediction, warning and browser WebSocket pipeline.

**Architecture:** Introduce a distinct `lan_operator` identity, centralize helper-backed role checks, and add a session-scoped remote acquisition mirror. The existing helper owns hardware and persistence, while acknowledged sample batches populate the server mirror used by `DashboardData.live()`.

**Tech Stack:** Python 3, stdlib HTTP/WebSocket server, websocket-client, vanilla JavaScript, unittest, existing AcquisitionManager.

**Spec:** `docs/superpowers/specs/2026-09-17-edge-capture-gateway-design.md`

## Global Constraints

- Work on the existing `feature/original-ui-langchain-agent` branch.
- Preserve the stable MySQL implementation; only change execution routing.
- Do not create duplicate delivery folders or versioned EXE copies.
- Write a failing regression test before each production behavior change.
- Never fall back from visitor-helper acquisition to server-local hardware.
- Keep simulation state independent from real-helper state.

---

### Task 1: LAN identity and helper execution policy

**Files:**
- Modify: `visualization_app/web_access.py`
- Modify: `visualization_app/app.py`
- Modify: `visualization_app/static/app.js`
- Test: `visualization_app/test_public_web_security.py`
- Test: `visualization_app/test_frontend_guest_simulation.py`

**Interfaces:**
- Produces: `Role = Literal["guest", "authorized", "lan_operator", "local_admin"]`
- Produces: unique `lan-<guest_id>` session identifiers.
- Produces: frontend `usesLocalCaptureHelper()` policy.

- [x] Write tests proving direct LAN clients are `lan_operator`, two guest cookies do not share a session, LAN cannot call admin routes, and LAN can call pairing/real-control routes.
- [x] Run the focused tests and confirm they fail because `lan_operator` does not exist.
- [x] Implement the role and replace helper routing checks that currently only accept `authorized`.
- [x] Run focused security/frontend tests and commit `feat: isolate lan helper sessions`.

### Task 2: Ordered remote acquisition mirror

**Files:**
- Create: `visualization_app/edge_capture.py`
- Modify: `visualization_app/helper_relay.py`
- Test: `visualization_app/test_edge_capture.py`

**Interfaces:**
- Produces: `RemoteAcquisitionMirror.ingest(batch) -> dict`.
- Produces: `RemoteAcquisitionMirror.status() -> dict`.
- Produces: `RemoteAcquisitionMirror.numeric_matrix() -> tuple[list[dict], list[float]]`.
- Produces: `RemoteAcquisitionRegistry.for_session(session_id)`.

- [x] Write tests for first batch, duplicate acknowledgement, out-of-order rejection, capture reset and independent sessions.
- [x] Run tests and confirm import/behavior failures.
- [x] Implement bounded, locked mirrors with `(capture_uuid, sequence)` deduplication.
- [x] Run focused tests and commit `feat: add session remote acquisition mirror`.

### Task 3: Helper sample batching and acknowledgement

**Files:**
- Modify: `visualization_app/local_capture_agent.py`
- Modify: `visualization_app/local_capture_helper_entry.py`
- Modify: `visualization_app/app.py`
- Test: `visualization_app/test_local_capture_agent.py`
- Test: `visualization_app/test_helper_websocket.py`

**Interfaces:**
- Produces: `LocalCaptureAgent.next_sample_batch(limit=20)`.
- Produces: `LocalCaptureAgent.ack_sample_batch(capture_uuid, sequence)`.
- Consumes: `RemoteAcquisitionRegistry.ingest(session_id, batch)`.

- [x] Write failing tests for pending-batch replay, acknowledgement advancement, private HTTP/WS acceptance, public plaintext rejection and shared agent state across WSS-to-HTTP fallback.
- [x] Run focused tests and confirm expected failures.
- [x] Implement sample messages for WSS and `/api/helper/samples` for HTTPS polling fallback.
- [x] Run focused tests and commit `feat: stream helper sample batches`.

### Task 4: Feed remote rows into prediction and warning

**Files:**
- Modify: `visualization_app/app.py`
- Test: `visualization_app/test_app.py`
- Test: `visualization_app/test_helper_websocket.py`

**Interfaces:**
- Produces: handler acquisition selection by identity/session.
- Consumes: `RemoteAcquisitionMirror.status()` and `numeric_matrix()`.

- [x] Write a failing integration test where helper rows appear in `/api/live` for their session and never in another session or server-local acquisition.
- [x] Run the test and confirm it reads the wrong acquisition source.
- [x] Select the remote mirror for helper-backed live HTTP/WebSocket and acquisition status.
- [x] Run integration tests and commit `feat: use edge samples for live prediction`.

### Task 5: Frontend pairing, start-state and no-fallback behavior

**Files:**
- Modify: `visualization_app/static/app.js`
- Modify: `visualization_app/static/index.html`
- Test: `visualization_app/test_frontend_guest_simulation.py`
- Test: `visualization_app/test_process_parameter_frontend.py`

**Interfaces:**
- Consumes: `lan_operator`, helper status and remote acquisition status.
- Produces: explicit execution-source and first-sample states.

- [x] Write source-contract tests for LAN pairing visibility, helper routing of discovery/start/stop/process parameters/local MySQL, and no server fallback.
- [x] Run tests and confirm the old authorized-only conditions fail.
- [x] Implement the shared helper policy and wait-for-first-sample status.
- [x] Run frontend contract tests and commit `feat: expose lan edge capture state`.

### Task 6: Regression, packaging and two-process verification

**Files:**
- Modify: `visualization_app/build_local_capture_helper.ps1` only if dependency packaging requires it.
- Modify: existing delivery EXEs in place through the established build scripts.

**Interfaces:**
- Verifies the full edge gateway contract; introduces no new runtime API.

- [x] Run helper, security, acquisition, MySQL, diagnosis and frontend test suites.
- [x] Run Python compile checks and JavaScript syntax checks.
- [x] Start a server plus helper test process, pair, ingest known rows, verify live output, disconnect/reconnect and verify no duplicates.
- [x] Build the helper and integrated EXE into their existing delivery paths, then run `--verify-files`, `--self-test` and functional smoke checks.
- [x] Inspect `git diff`, confirm no duplicate delivery directory, and commit `build: refresh edge capture gateway binaries`.
