# Agentic Offline Diagnosis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a no-sensor acceptance mode and a bounded tool-calling diagnosis loop that reports the five absent AFP sensor interfaces accurately, lets a configured SiliconFlow model select read-only evidence tools, and never replaces hardware facts with unsupported model claims.

**Architecture:** The existing acquisition check remains the source of hardware facts. A focused tool registry exposes only sanitized snapshots from the current check, interface discovery, and acquisition status; an offline diagnostic policy and the SiliconFlow Function Calling loop consume the same tool contract and emit the same evidence-backed result schema. The existing `/api/agent/diagnose` route and hardware-results panel remain the integration points so the original UI layout and non-diagnostic functions stay unchanged.

**Tech Stack:** Python 3.11, `langchain-core==1.6.2`, Python standard-library HTTP client, existing `AcquisitionManager`, vanilla JavaScript/CSS, `unittest`, Git.

**Spec:** `docs/superpowers/specs/2026-09-13-agentic-offline-diagnosis-design.md`

## Global Constraints

- In the no-sensor acceptance scenario, report `0/5` target interfaces confirmed by valid data and `0/17` channels normal.
- A discovered NIC, COM port, USB controller, ordinary camera, placeholder binding, or vendor DLL is not proof that the target sensor is connected.
- Keep Panasonic PLC process pressure (`压力`) separate from M3232 thin-film pressure (`薄膜压力`) through interface, channel, evidence, and output fields.
- The offline diagnostic policy must be labelled `offline_test`; it must never be presented as GPT, DeepSeek, or a field-validated hardware conclusion.
- The existing I-ModernTCN forecasting model must not be used for interface diagnosis.
- Agent tools are read-only, allowlisted by configured interface ID, limited to 5 rounds, 8 tool calls, and 90 seconds, and cannot write PLC registers, command ABB, change mappings, kill processes, restart devices, or scan arbitrary endpoints.
- When acquisition is running, tools read existing manager state and must not reopen the same hardware.
- The original hardware result remains visible if the model, offline policy, parser, or network call fails.
- Do not restore the detailed LangChain call trace; show only concise evidence-source labels.
- Continue in the current Git repository and feature branch; do not copy the project or create differently named EXE files.
- Do not build or replace the delivery EXE unless the user explicitly requests packaging after source verification.

## File Structure

- Create `visualization_app/diagnostic_tools.py`: sanitized evidence context, tool schemas, allowlist validation, and deterministic read-only tool implementations.
- Create `visualization_app/agentic_diagnosis.py`: offline tool-selection policy, SiliconFlow Function Calling loop, result validation, and common result assembly.
- Create `visualization_app/test_agentic_diagnosis.py`: no-sensor truth fixture, tool-selection tests, malicious-call rejection tests, and mocked SiliconFlow loop tests.
- Modify `visualization_app/interface_agent.py`: preserve legacy event/local-rule helpers and delegate the new diagnosis mode to `agentic_diagnosis.py`.
- Modify `visualization_app/app.py`: pass the current hardware result plus server-produced discovery/status snapshots into the new diagnosis runner.
- Modify `visualization_app/static/app.js`: make offline diagnosis available without credentials and render facts, hypotheses, evidence sources, confidence, unknowns, and recommendations in the existing hardware result panel.
- Modify `visualization_app/static/index.html`: update only the short LangChain configuration explanation and mode label; keep existing controls and layout.
- Modify `visualization_app/static/styles.css`: add styles only for the new evidence-backed result fields inside the existing panel.
- Modify `visualization_app/test_interface_agent.py`: update compatibility assertions from `local_rules`/model enhancement to `offline_test`/tool-calling result behavior.

---

### Task 0: Mark the implementation baseline

**Files:**
- No file changes.

**Interfaces:**
- Produces: Git tag `agentic-diagnosis-baseline` pointing to the approved design commit `d1a58c7`.

- [ ] **Step 1: Confirm the approved design commit and clean worktree**

Run:

```text
git show d1a58c7 --oneline --no-patch
git status --short
```

Expected: commit `d1a58c7 docs: design agentic offline interface diagnosis` is shown and `git status --short` has no output.

- [ ] **Step 2: Create and verify the baseline tag**

Run:

```text
git tag agentic-diagnosis-baseline d1a58c7
git show agentic-diagnosis-baseline --oneline --no-patch
```

Expected: the tag resolves to `d1a58c7`.

### Task 1: Lock the no-sensor truth contract

**Files:**
- Create: `visualization_app/test_agentic_diagnosis.py`
- Reference: `visualization_app/acquisition.py:211-324`
- Reference: `docs/superpowers/specs/2026-09-13-agentic-offline-diagnosis-design.md`

**Interfaces:**
- Produces: `no_sensor_hardware_result() -> dict[str, Any]`, a five-interface/17-channel fixture used by every later task.
- Produces: expected interface state map keyed by `thermocouple_8ch`, `plc_process`, `uvc_temperature`, `abb_motion`, and `m3232_pressure`.

- [ ] **Step 1: Create the failing no-sensor fixture and truth assertions**

```python
def no_sensor_hardware_result() -> dict:
    channels = {
        "thermocouple_8ch": [f"温度{i}" for i in range(1, 9)],
        "plc_process": ["温度", "压力", "张力"],
        "uvc_temperature": ["ROI平均温度"],
        "abb_motion": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
        "m3232_pressure": ["薄膜压力"],
    }
    states = {
        "thermocouple_8ch": ("identity_unconfirmed", "未识别到匹配的 SMRFCT08B USB HID 设备"),
        "plc_process": ("endpoint_unreachable", "PLC 端点 192.168.125.5:502 不可达"),
        "uvc_temperature": ("identity_unconfirmed", "未识别到可产生 BSV 温度帧的设备"),
        "abb_motion": ("endpoint_unreachable", "ABB RWS 端点 192.168.125.1 不可达"),
        "m3232_pressure": ("identity_unconfirmed", "未确认串口设备为 M3232，未收到有效帧"),
    }
    interfaces = []
    sensors = []
    for interface_id, names in channels.items():
        state, message = states[interface_id]
        interfaces.append({
            "id": interface_id,
            "enabled": True,
            "expected_channels": names,
            "detected_channels": [],
            "missing_channels": names,
            "invalid_channels": [],
            "sample_counts": {},
            "state": state,
            "message": message,
            "ok": False,
        })
        sensors.extend({
            "name": name,
            "selected": True,
            "received_samples": 0,
            "invalid_samples": 0,
            "state": "no_data",
            "message": "未检测到数据",
            "ok": False,
        } for name in names)
    return {"simulated": False, "ok": False, "interfaces": interfaces, "sensors": sensors}


def test_no_sensor_truth_is_five_failed_interfaces_and_seventeen_channels():
    result = no_sensor_hardware_result()
    assert len(result["interfaces"]) == 5
    assert len(result["sensors"]) == 17
    assert sum(item["ok"] for item in result["interfaces"]) == 0
    assert sum(item["ok"] for item in result["sensors"]) == 0
    assert all("损坏" not in item["message"] for item in result["interfaces"])
```

- [ ] **Step 2: Run the contract test and confirm the missing production normalizer test fails**

Run: `python -m unittest test_agentic_diagnosis.NoSensorTruthTests -v`

Expected: the fixture-only count test passes and the assertion importing `normalize_no_sensor_states` fails because the function does not exist yet.

- [ ] **Step 3: Add explicit assertions for false-positive physical interfaces**

```python
def test_placeholder_interfaces_never_count_as_connected():
    result = no_sensor_hardware_result()
    for item in result["interfaces"]:
        item["physical_interface_id"] = {
            "thermocouple_8ch": "usb_hid:auto",
            "plc_process": "ethernet:Ethernet",
            "uvc_temperature": "usb_uvc:auto",
            "abb_motion": "ethernet:Ethernet",
            "m3232_pressure": "serial:COM8",
        }[item["id"]]
        item["physical_verified"] = False
    normalized = normalize_no_sensor_states(result)
    assert sum(item["ok"] for item in normalized["interfaces"]) == 0
    assert normalized["summary"] == "检查未通过：0/5 个目标传感器接口已由有效数据确认，0/17 个通道正常"
```

- [ ] **Step 4: Commit the red contract tests**

Run:

```text
git add visualization_app/test_agentic_diagnosis.py
git commit -m "test: define no-sensor diagnostic truth"
```

### Task 2: Implement the read-only evidence tool registry

**Files:**
- Create: `visualization_app/diagnostic_tools.py`
- Modify: `visualization_app/test_agentic_diagnosis.py`

**Interfaces:**
- Produces: `normalize_no_sensor_states(hardware_result: dict) -> dict`.
- Produces: `DiagnosticToolContext(events: list[dict], hardware_result: dict, discovery: dict, acquisition_status: dict)`.
- Produces: `tool_definitions() -> list[dict]` using the OpenAI-compatible function schema.
- Produces: `execute_tool(context: DiagnosticToolContext, name: str, arguments: dict) -> dict` with an `evidence_id` in every successful response.
- Consumes: normalized agent events from `interface_agent.build_agent_events`.

- [ ] **Step 1: Add failing tests for allowlisted tools and evidence IDs**

```python
def test_each_tool_returns_evidence_id():
    context = diagnostic_context_for(no_sensor_hardware_result())
    checks = [
        ("inspect_interface_mapping", {"interface_id": "thermocouple_8ch"}),
        ("recheck_interface", {"interface_id": "m3232_pressure", "duration_seconds": 1}),
        ("get_recent_channel_stats", {"channel_names": ["薄膜压力"], "window_seconds": 30}),
        ("get_cross_interface_timeline", {"interface_ids": ["plc_process", "abb_motion"], "window_seconds": 60}),
        ("check_network_path", {"interface_id": "plc_process"}),
        ("inspect_protocol_frame", {"interface_id": "m3232_pressure"}),
        ("lookup_device_knowledge", {"device_type": "m3232_pressure", "symptom": "no_data"}),
        ("compare_related_signals", {"channel_names": ["压力", "薄膜压力"], "window_seconds": 30}),
    ]
    for name, arguments in checks:
        evidence = execute_tool(context, name, arguments)
        assert evidence["evidence_id"].startswith("EV-")
        assert evidence["tool"] == name
```

- [ ] **Step 2: Run the focused tests and verify import/function failures**

Run: `python -m unittest test_agentic_diagnosis.DiagnosticToolTests -v`

Expected: FAIL because `diagnostic_tools.py` does not exist.

- [ ] **Step 3: Implement normalized states and bounded evidence context**

```python
@dataclass
class DiagnosticToolContext:
    events: tuple[dict[str, Any], ...]
    hardware_result: dict[str, Any]
    discovery: dict[str, Any]
    acquisition_status: dict[str, Any]
    evidence_sequence: int = 0

    def next_evidence_id(self, _tool_name: str) -> str:
        self.evidence_sequence += 1
        return f"EV-{self.evidence_sequence:03d}"


def normalize_no_sensor_states(hardware_result: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(hardware_result)
    for interface in result.get("interfaces", []):
        valid_count = sum(int(value) for value in (interface.get("sample_counts") or {}).values())
        if valid_count > 0:
            continue
        physical_id = str(interface.get("physical_interface_id") or "")
        if bool(interface.get("physical_fallback")) or physical_id.endswith(":auto"):
            interface["state"] = "identity_unconfirmed"
            interface["ok"] = False
    interface_ok = sum(bool(item.get("ok")) for item in result.get("interfaces", []))
    channel_ok = sum(item.get("state") == "ok" for item in result.get("sensors", []))
    result["summary"] = (
        f"检查未通过：{interface_ok}/5 个目标传感器接口已由有效数据确认，"
        f"{channel_ok}/17 个通道正常"
    )
    return result
```

- [ ] **Step 4: Implement schemas and the tool dispatcher**

The dispatcher must reject unknown tools, unknown interface IDs, arbitrary network targets, more than 256 bytes of protocol evidence, `duration_seconds > 3`, and `window_seconds > 120` with `DiagnosticToolError`.

`recheck_interface` reads the latest probe belonging to the current user-triggered case; it does not silently start a second hardware scan or reopen a port. `inspect_protocol_frame` returns a bounded existing frame summary when present and an explicit `evidence_available=false` when the acquisition layer captured no frame. `compare_related_signals` performs only deterministic calculations over current acquisition-status summaries and returns `insufficient_data=true` when no valid series exists.

```python
ALLOWED_TOOLS = {
    "inspect_interface_mapping": _inspect_interface_mapping,
    "recheck_interface": _recheck_interface,
    "get_recent_channel_stats": _get_recent_channel_stats,
    "get_cross_interface_timeline": _get_cross_interface_timeline,
    "check_network_path": _check_network_path,
    "inspect_protocol_frame": _inspect_protocol_frame,
    "lookup_device_knowledge": _lookup_device_knowledge,
    "compare_related_signals": _compare_related_signals,
}


def execute_tool(context: DiagnosticToolContext, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name not in ALLOWED_TOOLS:
        raise DiagnosticToolError(f"不允许的诊断工具：{name}")
    clean_arguments = _validate_arguments(context, name, arguments)
    payload = ALLOWED_TOOLS[name](context, clean_arguments)
    return {"evidence_id": context.next_evidence_id(name), "tool": name, **payload}
```

- [ ] **Step 5: Add and pass malicious-call rejection tests**

```python
def test_tool_registry_rejects_arbitrary_endpoint_and_interface():
    context = diagnostic_context_for(no_sensor_hardware_result())
    with self.assertRaises(DiagnosticToolError):
        execute_tool(context, "check_network_path", {"interface_id": "8.8.8.8"})
    with self.assertRaises(DiagnosticToolError):
        execute_tool(context, "run_command", {"command": "whoami"})
```

Run: `python -m unittest test_agentic_diagnosis.DiagnosticToolTests -v`

Expected: PASS.

- [ ] **Step 6: Commit the evidence tool layer**

Run:

```text
git add visualization_app/diagnostic_tools.py visualization_app/test_agentic_diagnosis.py
git commit -m "feat: add bounded diagnostic evidence tools"
```

### Task 3: Implement the built-in offline diagnostic policy

**Files:**
- Create: `visualization_app/agentic_diagnosis.py`
- Modify: `visualization_app/test_agentic_diagnosis.py`

**Interfaces:**
- Consumes: `DiagnosticToolContext`, `tool_definitions`, and `execute_tool` from Task 2.
- Produces: `run_offline_diagnosis(context: DiagnosticToolContext, local_diagnoses: list[dict]) -> dict`.
- Produces: common result with `execution_mode="offline_test"`, five diagnoses, tool evidence, hypotheses, recommendations, unknowns, and local fallback.

- [ ] **Step 1: Add failing tests for phenomenon-dependent tool choice**

```python
def test_offline_policy_uses_network_correlation_for_plc_and_abb():
    result = run_offline_diagnosis(diagnostic_context_for(no_sensor_hardware_result()), [])
    network_tools = {
        evidence["tool"]
        for diagnosis in result["diagnoses"]
        for evidence in diagnosis["evidence"]
        if diagnosis["interface_id"] in {"plc_process", "abb_motion"}
    }
    assert "get_cross_interface_timeline" in network_tools
    assert "check_network_path" in network_tools


def test_offline_policy_does_not_use_network_tool_for_m3232():
    result = run_offline_diagnosis(diagnostic_context_for(no_sensor_hardware_result()), [])
    diagnosis = next(item for item in result["diagnoses"] if item["interface_id"] == "m3232_pressure")
    assert "check_network_path" not in {item["tool"] for item in diagnosis["evidence"]}
    assert "inspect_interface_mapping" in {item["tool"] for item in diagnosis["evidence"]}
```

- [ ] **Step 2: Run the offline-policy tests and verify they fail**

Run: `python -m unittest test_agentic_diagnosis.OfflineAgentTests -v`

Expected: FAIL because `run_offline_diagnosis` does not exist.

- [ ] **Step 3: Implement the bounded offline decision policy**

```python
def _offline_tool_plan(context: DiagnosticToolContext) -> dict[str, list[tuple[str, dict]]]:
    plans: dict[str, list[tuple[str, dict]]] = {}
    abnormal_ids = {event["interface_id"] for event in context.events}
    shared_network_failure = {"plc_process", "abb_motion"}.issubset(abnormal_ids)
    for event in context.events:
        interface_id = event["interface_id"]
        plan = [("inspect_interface_mapping", {"interface_id": interface_id})]
        if shared_network_failure and interface_id in {"plc_process", "abb_motion"}:
            plan.extend([
                ("get_cross_interface_timeline", {"interface_ids": ["plc_process", "abb_motion"], "window_seconds": 60}),
                ("check_network_path", {"interface_id": interface_id}),
            ])
        elif event.get("state") == "invalid_protocol":
            plan.extend([
                ("inspect_protocol_frame", {"interface_id": interface_id}),
                ("lookup_device_knowledge", {"device_type": interface_id, "symptom": "invalid_protocol"}),
            ])
        else:
            plan.append(("recheck_interface", {"interface_id": interface_id, "duration_seconds": 1}))
        plans[interface_id] = plan
    return plans
```

The final assembler must use evidence facts to distinguish observation from hypothesis, set confidence no higher than `0.85` in offline mode, include `unknowns`, and add the boundary text `离线测试诊断只验证软件流程，不等同于真实硬件故障确认`.

- [ ] **Step 4: Add exact no-sensor diagnosis assertions**

```python
def test_offline_no_sensor_result_matches_declared_truth():
    result = run_offline_diagnosis(diagnostic_context_for(no_sensor_hardware_result()), [])
    assert result["execution_mode"] == "offline_test"
    assert result["model_status"] == "offline_success"
    assert len(result["diagnoses"]) == 5
    assert sum(item["confirmed_ok"] for item in result["diagnoses"]) == 0
    serialized = json.dumps(result, ensure_ascii=False)
    assert "0/5" in serialized and "0/17" in serialized
    assert "传感器损坏" not in serialized
    assert all(item["evidence_sources"] for item in result["diagnoses"])
```

- [ ] **Step 5: Run all new tests**

Run: `python -m unittest test_agentic_diagnosis -v`

Expected: PASS.

- [ ] **Step 6: Commit the offline policy**

Run:

```text
git add visualization_app/agentic_diagnosis.py visualization_app/test_agentic_diagnosis.py
git commit -m "feat: add offline evidence-driven diagnosis"
```

### Task 4: Add SiliconFlow native tool-calling with offline fallback

**Files:**
- Modify: `visualization_app/agentic_diagnosis.py`
- Modify: `visualization_app/interface_agent.py`
- Modify: `visualization_app/test_agentic_diagnosis.py`
- Modify: `visualization_app/test_interface_agent.py`

**Interfaces:**
- Produces: `run_siliconflow_tool_agent(api_key: str, model_name: str, context: DiagnosticToolContext, local_diagnoses: list[dict], transport: Callable | None = None) -> dict`.
- Produces: `run_agentic_diagnoses(events: list[dict], hardware_result: dict, discovery: dict, acquisition_status: dict, api_key: str, model_name: str, transport: Callable | None = None) -> dict`.
- Preserves: `interface_agent.run_interface_diagnoses(...)` as the API-compatible entry point.

- [ ] **Step 1: Add a failing mocked two-turn tool-call test**

```python
def test_siliconflow_agent_executes_requested_tool_then_returns_final():
    replies = iter([
        {"choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "check_network_path", "arguments": '{"interface_id":"plc_process"}'},
        }]}}]},
        {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "diagnoses": [{
                "interface_id": "plc_process",
                "observed_facts": [{"text": "目标端点不可达", "evidence_ids": ["EV-001"]}],
                "hypotheses": [{"cause": "共用网络路径异常", "confidence": 0.8, "evidence_ids": ["EV-001"]}],
                "recommended_actions": [{"priority": 1, "action": "检查共用网卡和子网", "reason": "PLC与ABB共用网卡"}],
                "unknowns": ["未确认网线或交换机状态"]
            }]
        }, ensure_ascii=False)}}]},
    ])
    result = run_siliconflow_tool_agent("sk-test", "deepseek-ai/DeepSeek-V3.2", context, [], transport=lambda _payload: next(replies))
    assert result["execution_mode"] == "siliconflow_agent"
    assert result["tool_call_count"] == 1
```

- [ ] **Step 2: Run the mocked online-agent tests and verify failure**

Run: `python -m unittest test_agentic_diagnosis.SiliconFlowAgentTests -v`

Expected: FAIL because the native tool-call loop does not exist.

- [ ] **Step 3: Implement the bounded Function Calling loop**

```python
for round_index in range(MAX_AGENT_ROUNDS):
    response = transport(build_chat_payload(model_name, messages, tool_definitions()))
    message = extract_assistant_message(response)
    calls = message.get("tool_calls") or []
    if not calls:
        return validate_final_result(message, context, evidence_by_id, local_diagnoses)
    messages.append(message)
    for call in calls:
        if tool_call_count >= MAX_TOOL_CALLS:
            raise AgentLimitError("诊断工具调用超过 8 次限制")
        evidence = execute_tool(context, call["function"]["name"], json.loads(call["function"]["arguments"]))
        evidence_by_id[evidence["evidence_id"]] = evidence
        messages.append({
            "role": "tool",
            "tool_call_id": call["id"],
            "content": json.dumps(evidence, ensure_ascii=False),
        })
        tool_call_count += 1
raise AgentLimitError("诊断超过 5 轮限制")
```

The request must use the existing SiliconFlow URL, `tools`, `tool_choice="auto"`, `stream=False`, `temperature=0.1`, and a 60-second per-request timeout while enforcing a 90-second overall deadline.

- [ ] **Step 4: Implement evidence-reference validation and offline fallback**

Reject or downgrade any `observed_facts` or `hypotheses` evidence ID not present in `evidence_by_id`. On HTTP errors, timeouts, malformed tool arguments, unsupported tool names, partial diagnoses, or overall deadline, return `run_offline_diagnosis(...)` with `model_status="failed_offline_fallback"` and a sanitized `model_message`.

- [ ] **Step 5: Update the legacy adapter and default model**

`interface_agent.get_agent_defaults()` must return `deepseek-ai/DeepSeek-V3.2` unless `AFP_SILICONFLOW_MODEL` overrides it. `run_interface_diagnoses` must build a `DiagnosticToolContext` and delegate to `run_agentic_diagnoses`; keep legacy local-rule generation as `local_fallback`.

- [ ] **Step 6: Run agent tests**

Run:

```text
python -m unittest test_agentic_diagnosis test_interface_agent -v
```

Expected: PASS with no real SiliconFlow network request because transports are mocked.

- [ ] **Step 7: Commit the tool-calling Agent**

Run:

```text
git add visualization_app/agentic_diagnosis.py visualization_app/interface_agent.py visualization_app/test_agentic_diagnosis.py visualization_app/test_interface_agent.py
git commit -m "feat: let diagnosis agent select evidence tools"
```

### Task 5: Integrate the new modes into the existing API and panel

**Files:**
- Modify: `visualization_app/app.py`
- Modify: `visualization_app/static/app.js`
- Modify: `visualization_app/static/index.html`
- Modify: `visualization_app/static/styles.css`
- Modify: `visualization_app/test_agentic_diagnosis.py`
- Modify: `visualization_app/test_interface_agent.py`

**Interfaces:**
- Consumes: `run_interface_diagnoses(..., hardware_result, discovery, acquisition_status)` from Task 4.
- Produces: `/api/agent/diagnose` response with `execution_mode`, `model_status`, `summary`, and evidence-backed `diagnoses`.
- Preserves: the existing hardware-check result DOM node and manual check/reset behavior.

- [ ] **Step 1: Add failing API/static-contract tests**

```python
def test_frontend_sends_current_hardware_result_and_supports_offline_mode():
    script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    assert "hardware_result: state.hardwareCheck" in script
    assert 'execution_mode === "offline_test"' in script
    assert "evidence_sources" in script
    assert "observed_facts" in script
    assert "unknowns" in script
    assert "agent-trace" not in script
```

- [ ] **Step 2: Run the static-contract tests and verify failure**

Run: `python -m unittest test_interface_agent.InterfaceAgentTests.test_original_frontend_contains_agent_additions -v`

Expected: FAIL on missing offline/evidence-backed UI fields.

- [ ] **Step 3: Extend request validation and route context**

Allow only `api_key`, `model_name`, `events`, and `hardware_result` in the agent request. In `app.py`, obtain `discovery=self.dashboard.acquisition.discover_interfaces()` and `acquisition_status=self.dashboard.acquisition.status()` on the server, then pass them to `run_interface_diagnoses`. Do not accept discovery data, arbitrary endpoints, or tool definitions from the browser.

- [ ] **Step 4: Enable diagnosis without API credentials**

In `renderAgentGate`, set readiness from `eventPresent && !state.agentBusy`; use credentials only to select online mode. Update the helper text to:

```text
未填写或模型不可用时使用内置离线测试诊断；配置支持工具调用的硅基流动模型后，模型可按现象选择只读取证工具。
```

- [ ] **Step 5: Render evidence-backed fields in the existing panel**

Render mode labels as `离线测试诊断`, `硅基流动工具诊断`, or `本地规则兜底`. For each diagnosis render `observed_facts`, `hypotheses` with confidence, `cross_interface_findings`, `recommended_actions`, `unknowns`, and a concise `依据：...` line. Keep the original hardware error above these fields and do not create a second result panel.

- [ ] **Step 6: Prevent stale results from winning races**

Add a monotonically increasing `agentRequestId`. Capture it before the request and assign `state.agentResult` only when the returned request ID matches the current one. Reset/cancel increments the ID and aborts the active controller.

- [ ] **Step 7: Run frontend/API compatibility tests**

Run:

```text
python -m unittest test_agentic_diagnosis test_interface_agent test_app -v
node --check static/app.js
```

Expected: PASS.

- [ ] **Step 8: Commit the API and UI integration**

Run:

```text
git add visualization_app/app.py visualization_app/static/app.js visualization_app/static/index.html visualization_app/static/styles.css visualization_app/test_agentic_diagnosis.py visualization_app/test_interface_agent.py
git commit -m "feat: show evidence-driven diagnosis in hardware results"
```

### Task 6: Run offline acceptance against the declared no-sensor reality

**Files:**
- Modify only if tests expose a defect: files changed in Tasks 1-5.
- Do not build or replace an EXE in this task.

**Interfaces:**
- Consumes: source application and tests from Tasks 1-5.
- Produces: verified source behavior and a Git rollback point.

- [ ] **Step 1: Run the complete interface/agent test set**

Run:

```text
python -m unittest test_agentic_diagnosis test_interface_agent test_m3232_pressure test_acquisition_integrity -v
```

Expected: all tests pass.

- [ ] **Step 2: Run the full visualization application tests**

Run: `python -m unittest discover -s . -p "test_*.py" -v`

Expected: all tests pass, or pre-existing fixture/environment failures are separately identified and shown not to be caused by this change.

- [ ] **Step 3: Run a source-mode no-sensor diagnostic acceptance probe**

Start the existing source application, submit the declared no-sensor hardware fixture to the local diagnostic function without API credentials, and save only the console test output. Verify:

```text
execution_mode = offline_test
interface summary = 0/5
channel summary = 0/17
diagnoses = 5
contains "传感器损坏" = false
all diagnoses have evidence sources = true
```

- [ ] **Step 4: Inspect the current machine without claiming target-device identity**

Run `AcquisitionManager.discover_interfaces()` and compare the discovered physical interfaces with target protocol identities. A NIC, COM port, placeholder USB entry, or BSV DLL may be listed; the acceptance result remains failed unless the target protocol produced valid data.

- [ ] **Step 5: Launch the source UI and visually verify the existing panel**

Open the source application at its existing local address, run “立即检查”, and verify that the hardware result remains visible, the mode reads “离线测试诊断”, five interface diagnoses are shown, evidence-source summaries are present, and no detailed LangChain trace appears.

- [ ] **Step 6: Commit any acceptance-only fixes and verify clean Git state**

Run:

```text
git status --short
git log --oneline --decorate -8
```

Expected: clean working tree; the baseline tag and small feature commits provide one-command identifiable rollback points. No new EXE or duplicate delivery folder exists.
