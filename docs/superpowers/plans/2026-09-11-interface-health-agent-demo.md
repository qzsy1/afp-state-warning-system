# Interface Health Agent Demo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an independently runnable visual interface-health Demo that shows five AFP interfaces, separate PLC and thin-film pressure channels, and an on-page local LangChain diagnostic flow.

**Architecture:** Add a self-contained `interface_monitor_demo` package beside the existing application. A deterministic in-memory monitor creates fault events; a `langchain-core` Runnable sequence invokes four local tools and returns a trace; a standard-library HTTP server exposes the state to a single-page frontend. The v2.0.6 delivery directory and existing runtime remain untouched.

**Tech Stack:** Python 3.11, `langchain-core==1.6.2`, standard-library `http.server`, HTML5, CSS, vanilla JavaScript, `unittest`, Node syntax check, Microsoft Edge headless screenshot.

**Spec:** `docs/superpowers/specs/2026-09-11-interface-health-agent-demo-design.md`

## Global Constraints

- Treat `delivery/AFP_Integrated_System_M3232_v2.0.6/AFP_Integrated_System_Modular.exe` as the immutable functional baseline.
- Keep `压力` for the Panasonic PLC and `薄膜压力` for the independent M3232 sensor; never merge or alias these channels.
- Do not connect to physical hardware or an external model provider.
- Do not transmit, persist, log, or echo the API Key; the browser may send only `api_key_present: true`.
- Mark every event and diagnosis as simulated and do not present confidence as a real fault probability.
- Bind the Demo to `127.0.0.1` and default port `8770`.
- Add only new files under `interface_monitor_demo` plus this plan/spec; do not modify existing acquisition, prediction, training, packaging, or delivery files.

## File Structure

- `interface_monitor_demo/interface_catalog.py`: authoritative five-interface catalog and channel ownership.
- `interface_monitor_demo/monitor.py`: deterministic scenario state, event normalization, reset, and snapshots.
- `interface_monitor_demo/agent_flow.py`: LangChain tools, Runnable sequence, gate validation, trace, and report.
- `interface_monitor_demo/server.py`: local static server and JSON API.
- `interface_monitor_demo/static/index.html`: semantic page structure and visible LangChain thought-flow diagram.
- `interface_monitor_demo/static/app.js`: state rendering, scenario actions, gate logic, animated trace, and safe request payloads.
- `interface_monitor_demo/static/styles.css`: responsive industrial dashboard presentation and flow-node states.
- `interface_monitor_demo/tests/test_monitor.py`: catalog and deterministic monitoring tests.
- `interface_monitor_demo/tests/test_agent_flow.py`: gate, tool order, output, and secret-boundary tests.
- `interface_monitor_demo/tests/test_server.py`: live local API contract tests.
- `interface_monitor_demo/tests/test_frontend_contract.py`: DOM, JavaScript, flow-visualization, and secret-payload checks.
- `interface_monitor_demo/requirements.txt`: exact Demo dependency pin.
- `interface_monitor_demo/start_demo.ps1`: repeatable isolated environment setup and launcher.
- `interface_monitor_demo/.gitignore`: excludes `.venv`, caches, and screenshots generated during local checking.
- `interface_monitor_demo/README.md`: startup order, walkthrough, data contract, and evidence boundary.

---

### Task 1: Five-interface catalog and deterministic monitor

**Files:**
- Create: `interface_monitor_demo/tests/test_monitor.py`
- Create: `interface_monitor_demo/interface_catalog.py`
- Create: `interface_monitor_demo/monitor.py`

**Interfaces:**
- Produces: `build_interface_catalog() -> list[dict[str, object]]`
- Produces: `InterfaceMonitor.snapshot() -> dict[str, object]`
- Produces: `InterfaceMonitor.apply_scenario(interface_id: str, scenario: str) -> dict[str, object]`
- Produces: `InterfaceMonitor.reset() -> dict[str, object]`
- Produces event fields: `event_id`, `occurred_at`, `interface_id`, `sensor_name`, `channels`, `state`, `severity`, `summary`, `evidence`, `simulated`

- [ ] **Step 1: Write the failing catalog and monitor tests**

```python
class InterfaceCatalogTests(unittest.TestCase):
    def test_catalog_has_five_interfaces_and_separate_pressure_channels(self):
        catalog = build_interface_catalog()
        self.assertEqual(len(catalog), 5)
        by_id = {item["id"]: item for item in catalog}
        self.assertIn("压力", by_id["plc_process"]["channels"])
        self.assertNotIn("薄膜压力", by_id["plc_process"]["channels"])
        self.assertEqual(by_id["m3232_pressure"]["channels"], ["薄膜压力"])
        self.assertEqual(by_id["m3232_pressure"]["endpoint"], "COM8")
        self.assertEqual(by_id["m3232_pressure"]["baudrate"], 115200)

class InterfaceMonitorTests(unittest.TestCase):
    def test_m3232_parse_error_creates_simulated_film_pressure_event(self):
        event = InterfaceMonitor().apply_scenario("m3232_pressure", "parse_error")
        self.assertEqual(event["interface_id"], "m3232_pressure")
        self.assertEqual(event["channels"], ["薄膜压力"])
        self.assertEqual(event["state"], "invalid_data")
        self.assertNotIn("压力", event["channels"])
        self.assertTrue(event["simulated"])
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `py -3.11 -m unittest interface_monitor_demo.tests.test_monitor -v`

Expected: import failure for the missing `interface_catalog`/`monitor` modules.

- [ ] **Step 3: Implement the fixed catalog and scenario-to-event mapping**

Create catalog entries for `thermocouple_8ch`, `plc_process`, `uvc_temperature`, `abb_motion`, and `m3232_pressure`. Implement scenario definitions for `healthy`, `not_found`, `open_failed`, `timeout`, `stale`, `invalid_data`, `partial_channels`, plus M3232 `parse_error`. Healthy state clears the interface event; every non-healthy scenario produces a whitelisted event and updates snapshot counters.

```python
class InterfaceMonitor:
    def __init__(self, catalog: list[dict[str, object]] | None = None) -> None:
        self.catalog = catalog or build_interface_catalog()
        self._by_id = {item["id"]: item for item in self.catalog}
        self._states = {
            item["id"]: healthy_state(item) for item in self.catalog
        }
        self._events: dict[str, dict[str, object]] = {}

    def snapshot(self) -> dict[str, object]:
        last_event_id = next(reversed(self._events), None)
        return {
            "interfaces": list(self._states.values()),
            "events": list(self._events.values()),
            "active_event": self._events.get(last_event_id),
            "simulated": True,
        }

    def apply_scenario(self, interface_id: str, scenario: str) -> dict[str, object]:
        interface = self._by_id[interface_id]
        state, event = build_scenario_state(interface, scenario)
        self._states[interface_id] = state
        if event is None:
            self._events = {
                key: value for key, value in self._events.items()
                if value["interface_id"] != interface_id
            }
            return state
        self._events[event["event_id"]] = event
        return event

    def reset(self) -> dict[str, object]:
        self._states = {
            item["id"]: healthy_state(item) for item in self.catalog
        }
        self._events.clear()
        return self.snapshot()
```

- [ ] **Step 4: Run monitor tests and verify GREEN**

Run: `py -3.11 -m unittest interface_monitor_demo.tests.test_monitor -v`

Expected: all monitor tests pass.

- [ ] **Step 5: Commit the monitor slice**

```powershell
git add interface_monitor_demo/interface_catalog.py interface_monitor_demo/monitor.py interface_monitor_demo/tests/test_monitor.py
git commit -m "feat: add deterministic interface health monitor"
```

### Task 2: Local LangChain tool flow and security gate

**Files:**
- Create: `interface_monitor_demo/tests/test_agent_flow.py`
- Create: `interface_monitor_demo/agent_flow.py`
- Create: `interface_monitor_demo/requirements.txt`
- Create: `interface_monitor_demo/.gitignore`

**Interfaces:**
- Consumes: normalized event and catalog from Task 1.
- Produces: `AgentGateError`
- Produces: `run_diagnosis(event: dict[str, object] | None, catalog: list[dict[str, object]], *, api_key_present: bool, model_name: str) -> dict[str, object]`
- Produces trace step IDs: `event_received`, `gate_checked`, `get_interface_config`, `inspect_latest_observation`, `lookup_fault_rule`, `compose_diagnostic_report`, `complete`

- [ ] **Step 1: Create the isolated Python 3.11 environment**

Run:

```powershell
py -3.11 -m venv interface_monitor_demo\.venv
& interface_monitor_demo\.venv\Scripts\python.exe -m pip install --upgrade pip
& interface_monitor_demo\.venv\Scripts\python.exe -m pip install langchain-core==1.6.2
```

Expected: import `langchain_core` succeeds inside the Demo environment.

- [ ] **Step 2: Write failing gate and flow tests**

```python
def test_gate_rejects_missing_inputs(self):
    with self.assertRaises(AgentGateError):
        run_diagnosis(self.event, self.catalog, api_key_present=False, model_name="demo-model")
    with self.assertRaises(AgentGateError):
        run_diagnosis(self.event, self.catalog, api_key_present=True, model_name="")

def test_flow_uses_langchain_tools_in_visible_order(self):
    result = run_diagnosis(
        self.event, self.catalog, api_key_present=True, model_name="local-demo-model"
    )
    self.assertEqual(
        [step["id"] for step in result["trace"]],
        ["event_received", "gate_checked", "get_interface_config",
         "inspect_latest_observation", "lookup_fault_rule",
         "compose_diagnostic_report", "complete"],
    )
    self.assertEqual(result["diagnosis"]["channels"], ["薄膜压力"])
    self.assertTrue(result["simulated"])
```

- [ ] **Step 3: Run the tests and verify RED**

Run: `& interface_monitor_demo\.venv\Scripts\python.exe -m unittest interface_monitor_demo.tests.test_agent_flow -v`

Expected: import failure for missing `agent_flow`.

- [ ] **Step 4: Implement four LangChain tools and Runnable sequence**

Use `from langchain_core.runnables import RunnableLambda` and `from langchain_core.tools import tool`. Invoke each tool locally from named Runnable steps, append a sanitized trace record, and return structured Chinese diagnosis text. No provider SDK, network client, environment API key, or LangSmith tracing is allowed.

```python
@tool
def get_interface_config(interface_id: str) -> dict[str, object]:
    """Return the sanitized configuration for one simulated AFP interface."""

def run_diagnosis(event, catalog, *, api_key_present, model_name):
    if not api_key_present or not model_name.strip():
        raise AgentGateError("API Key 和模型名称均填写后才能运行本地模拟")
    chain = (
        RunnableLambda(receive_event)
        | RunnableLambda(check_gate)
        | RunnableLambda(call_config_tool)
        | RunnableLambda(call_observation_tool)
        | RunnableLambda(call_rule_tool)
        | RunnableLambda(call_report_tool)
        | RunnableLambda(complete_trace)
    )
    return chain.invoke(context)
```

- [ ] **Step 5: Run LangChain tests and verify GREEN**

Run: `& interface_monitor_demo\.venv\Scripts\python.exe -m unittest interface_monitor_demo.tests.test_agent_flow -v`

Expected: all gate and flow tests pass with no network access.

- [ ] **Step 6: Commit the LangChain slice**

```powershell
git add interface_monitor_demo/agent_flow.py interface_monitor_demo/requirements.txt interface_monitor_demo/.gitignore interface_monitor_demo/tests/test_agent_flow.py
git commit -m "feat: add local LangChain diagnosis flow"
```

### Task 3: Local JSON API and static server

**Files:**
- Create: `interface_monitor_demo/tests/test_server.py`
- Create: `interface_monitor_demo/server.py`

**Interfaces:**
- Consumes: `InterfaceMonitor` and `run_diagnosis`.
- Produces: `create_server(host: str = "127.0.0.1", port: int = 8770) -> ThreadingHTTPServer`
- Produces endpoints: `GET /api/bootstrap`, `GET /api/status`, `POST /api/scenario`, `POST /api/diagnose`, `POST /api/reset`

- [ ] **Step 1: Write failing live-server contract tests**

Start `create_server(port=0)` in a test thread and use `urllib.request` to assert:

```python
self.assertEqual(bootstrap["interfaces"][4]["id"], "m3232_pressure")
self.assertEqual(bootstrap["interfaces"][4]["channels"], ["薄膜压力"])
self.assertEqual(post("/api/diagnose", {"api_key_present": False, "model_name": "x", "event_id": event_id}).status, 400)
self.assertEqual(post("/api/diagnose", {"api_key": "secret", "api_key_present": True, "model_name": "x", "event_id": event_id}).status, 400)
```

- [ ] **Step 2: Run server tests and verify RED**

Run: `& interface_monitor_demo\.venv\Scripts\python.exe -m unittest interface_monitor_demo.tests.test_server -v`

Expected: import failure for missing `server`.

- [ ] **Step 3: Implement the server and explicit request validation**

Serve only files under `static/`; JSON-encode UTF-8 responses; cap request bodies at 64 KiB. Reject keys matching `api_key`, `token`, `secret`, `authorization`, or `password` before dispatch. Resolve diagnosis events only by `event_id`; never accept a caller-supplied full event object.

- [ ] **Step 4: Run server tests and verify GREEN**

Run: `& interface_monitor_demo\.venv\Scripts\python.exe -m unittest interface_monitor_demo.tests.test_server -v`

Expected: all API tests pass and the server shuts down cleanly.

- [ ] **Step 5: Commit the server slice**

```powershell
git add interface_monitor_demo/server.py interface_monitor_demo/tests/test_server.py
git commit -m "feat: expose interface monitor demo API"
```

### Task 4: Visual frontend and animated LangChain thought flow

**Files:**
- Create: `interface_monitor_demo/tests/test_frontend_contract.py`
- Create: `interface_monitor_demo/static/index.html`
- Create: `interface_monitor_demo/static/app.js`
- Create: `interface_monitor_demo/static/styles.css`

**Interfaces:**
- Consumes: Task 3 JSON endpoints.
- Produces DOM IDs: `interfaceGrid`, `apiKeyInput`, `modelNameInput`, `agentEnabledState`, `diagnoseButton`, `scenarioInterface`, `scenarioType`, `injectScenarioButton`, `resetButton`, `thoughtFlow`, `toolTrace`, `diagnosisPanel`, `simulationBanner`.
- Produces flow node IDs: `flow-monitor`, `flow-event`, `flow-gate`, `flow-langchain`, `flow-report`.

- [ ] **Step 1: Write failing frontend contract tests**

Read the static files as text and assert all required DOM IDs exist, the page includes both `压力` and `薄膜压力` labels, the JavaScript diagnosis payload contains `api_key_present` but never reads the Key into the request object, and five flow nodes are present.

```python
self.assertIn('id="thoughtFlow"', html)
self.assertIn('id="flow-langchain"', html)
self.assertIn('type="password"', html)
self.assertIn('api_key_present: Boolean(apiKeyInput.value.trim())', js)
self.assertNotIn('api_key: apiKeyInput.value', js)
```

- [ ] **Step 2: Run frontend contract tests and verify RED**

Run: `& interface_monitor_demo\.venv\Scripts\python.exe -m unittest interface_monitor_demo.tests.test_frontend_contract -v`

Expected: missing static files or required DOM IDs.

- [ ] **Step 3: Build the semantic HTML and responsive industrial styling**

Create a one-page dashboard with a persistent simulation banner, header summary, five interface cards, adjacent Agent configuration panel, scenario controls, visible five-node LangChain flow, detailed four-tool trace, and structured diagnosis panel. Use text plus color for state; preserve readable layout at 1365×900 and narrow widths.

- [ ] **Step 4: Implement frontend state and flow animation**

Fetch bootstrap/status, render all interface channels, gate the diagnosis button on API Key + model + active fault, and post only:

```javascript
{
  api_key_present: Boolean(apiKeyInput.value.trim()),
  model_name: modelNameInput.value.trim(),
  event_id: state.activeEvent.event_id
}
```

Animate returned trace IDs and update the permanent thought-flow nodes. Clear the API Key on page load and do not use local/session storage.

- [ ] **Step 5: Run frontend checks and verify GREEN**

Run:

```powershell
& interface_monitor_demo\.venv\Scripts\python.exe -m unittest interface_monitor_demo.tests.test_frontend_contract -v
node --check interface_monitor_demo\static\app.js
```

Expected: contract tests pass and Node exits 0.

- [ ] **Step 6: Commit the frontend slice**

```powershell
git add interface_monitor_demo/static interface_monitor_demo/tests/test_frontend_contract.py
git commit -m "feat: add visual interface diagnosis demo"
```

### Task 5: Startup workflow, visual QA, and regression verification

**Files:**
- Create: `interface_monitor_demo/start_demo.ps1`
- Create: `interface_monitor_demo/README.md`
- Modify: `docs/superpowers/specs/2026-09-11-interface-health-agent-demo-design.md` only to mark implementation status if all acceptance checks pass.

**Interfaces:**
- Produces: one-command startup using `start_demo.ps1`.
- Produces: visual QA screenshot under `interface_monitor_demo/verification/` for local evidence; the directory remains excluded from Git unless the screenshot is intentionally retained.

- [ ] **Step 1: Write failing startup/documentation contract tests**

Extend `test_frontend_contract.py` to require the launcher to create/use `.venv`, install `requirements.txt`, call `server.py`, and require README sections for startup, demo steps, security boundary, and real-hardware boundary.

- [ ] **Step 2: Run the contract test and verify RED**

Run: `& interface_monitor_demo\.venv\Scripts\python.exe -m unittest interface_monitor_demo.tests.test_frontend_contract -v`

Expected: launcher and README requirements fail because files do not exist.

- [ ] **Step 3: Implement the launcher and README**

`start_demo.ps1` must use `py -3.11`, create `.venv` only when absent, install the pinned requirements, start `server.py --host 127.0.0.1 --port 8770`, and open the local URL. README must list the five interfaces and clearly state that the API Key remains browser-local and the M3232 protocol is not real-hardware validated by this Demo.

- [ ] **Step 4: Run the full Demo test suite**

Run: `& interface_monitor_demo\.venv\Scripts\python.exe -m unittest discover -s interface_monitor_demo\tests -v`

Expected: zero failures and zero errors.

- [ ] **Step 5: Start the Demo and exercise the live API**

Run the server on a free local port, request `/api/bootstrap`, inject M3232 `parse_error`, run `/api/diagnose` with `api_key_present=true`, and verify the response identifies `薄膜压力` and returns all seven trace steps. Shut down the server after the check.

- [ ] **Step 6: Render and inspect the actual frontend**

Use installed Microsoft Edge in headless mode at 1365×900 to save a screenshot. Inspect it for clipped text, overlapping cards, missing flow nodes, unreadable contrast, and whether all key regions are visible. Fix any visual issue through a failing contract test where practical, then repeat the screenshot.

- [ ] **Step 7: Verify the immutable baseline and existing focused tests**

Run:

```powershell
Get-FileHash delivery\AFP_Integrated_System_M3232_v2.0.6\AFP_Integrated_System_Modular.exe -Algorithm SHA256
py -3.11 -m unittest visualization_app.test_m3232_pressure -v
git diff --check
git status --short
```

Expected EXE hash: `FE42C67E5B38710BE182B8E5898341500EE7CAF2A668CFB619429C7E491DEC0C`. Existing M3232 focused tests must pass; only planned Demo/spec/plan changes may appear.

- [ ] **Step 8: Commit documentation and final verified state**

```powershell
git add interface_monitor_demo/start_demo.ps1 interface_monitor_demo/README.md docs/superpowers/specs/2026-09-11-interface-health-agent-demo-design.md
git commit -m "docs: add interface monitor demo walkthrough"
```

- [ ] **Step 9: Run final verification after the last commit**

Repeat the full Demo tests, Node syntax check, live API smoke, screenshot inspection, EXE hash check, focused existing tests, `git diff --check`, and `git status --short`. Report exact counts and any pre-existing test limitations without overstating real-hardware validation.
