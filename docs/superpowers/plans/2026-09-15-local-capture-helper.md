# Local Capture Helper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让公网网页在授权后通过访问者电脑上的本地辅助程序，识别本机硬件、执行采集，并按原 EXE 逻辑保存 CSV 与本地 MySQL；访客模拟模式保持独立。

**Architecture:** 公网服务只负责网页、授权、配对和命令中继；本地辅助程序通过出站 WSS 运行硬件发现、传感器采集、文件保存和 MySQL 写入。辅助程序复用现有 `acquisition.py`、`mysql_storage.py` 和接口协议定义，网页显示结构化状态与诊断结果。

**Tech Stack:** Python 3.11、现有 HTTP 服务、WebSocket 客户端、pyserial、USB/UVC 驱动、现有 MySQL 驱动、现有静态 HTML/JS、PyInstaller 交付。

**Spec:** `docs/superpowers/specs/2026-09-15-local-capture-helper-design.md`

## Global Constraints

- 主程序交付 EXE 保持单一目标；本地辅助程序是明确独立组件。
- 原 EXE 的接口协议、通道映射、文件夹层级、命名和 MySQL 写入逻辑是唯一基准。
- 普通浏览器不直接访问 COM、USB、UVC、PLC、ABB 或访问者 MySQL。
- 访客模拟模式不读取真实硬件；真实模式必须配对本地辅助程序。
- 每个任务必须先写失败测试，测试通过后提交 Git，才进入下一个任务。

## File Map

- Modify: `visualization_app/mysql_storage.py` — 增加连接预检和错误分类所需的结构化字段。
- Modify: `visualization_app/app.py` — 增加 MySQL 诊断接口、辅助程序配对与命令中继接口。
- Create: `visualization_app/local_capture_agent.py` — 本地辅助程序核心：配对、发现、采集、保存、MySQL 预检。
- Create: `visualization_app/test_local_capture_agent.py` — 辅助程序单元和协议测试。
- Modify: `visualization_app/static/app.js` — 网页配对、辅助程序状态、真实采集路由和 MySQL 本地目标。
- Modify: `visualization_app/static/index.html` — 辅助程序连接状态、配对控制和本地 MySQL 诊断区域。
- Modify: `visualization_app/test_public_web_security.py` — 访问权限、配对和凭据不落日志测试。
- Modify: `visualization_app/test_frontend_guest_simulation.py` — 模拟/真实隔离与辅助程序契约测试。
- Create: `visualization_app/build_local_capture_helper.ps1` — 生成唯一的辅助程序 EXE。
- Modify: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/app/...` — 每次验证通过后同步源文件，不创建新的主程序目录。

### Task 1: MySQL 连接预检和错误诊断

**Files:**
- Modify: `visualization_app/mysql_storage.py`
- Modify: `visualization_app/app.py`
- Test: `visualization_app/test_mysql_diagnostics.py`

**Interfaces:**
- Produces `MySQLCaptureStore.preflight() -> dict`，返回 `ok`, `stage`, `host`, `port`, `driver`, `database`, `schema_ready`, `write_test`, `error_detail`。
- `/api/mysql/preflight` 接收现有 MySQL 设置和 `write_test` 布尔值。

- [ ] Step 1: Write failing tests

```python
def test_preflight_reports_port_unreachable_without_calling_schema(self):
    result = MySQLCaptureStore(unreachable_settings()).preflight()
    self.assertFalse(result["ok"])
    self.assertEqual(result["stage"], "connect")
    self.assertEqual(result["error_detail"]["category"], "network")
```

- [ ] Step 2: Run `py -3.11 -m unittest visualization_app.test_mysql_diagnostics -v` and verify failure because `preflight` is absent.
- [ ] Step 3: Implement `preflight()` using the existing connector selection, `SELECT 1`, schema inspection, and an optional rollback write check; never log passwords.
- [ ] Step 4: Add `/api/mysql/preflight`, return the same structured result, and keep `/api/mysql/test` backward compatible.
- [ ] Step 5: Run the focused tests plus `visualization_app.test_acquisition_integrity`; expected all pass.
- [ ] Step 6: Commit `fix: add structured mysql preflight diagnostics`.

### Task 2: Local helper core and hardware discovery contract

**Files:**
- Create: `visualization_app/local_capture_agent.py`
- Create: `visualization_app/test_local_capture_agent.py`
- Modify: `visualization_app/acquisition.py` only if a shared discovery adapter is needed.

**Interfaces:**
- `LocalCaptureAgent.discover() -> dict` returns `interfaces`, `sensor_bindings`, `capabilities`, and `warnings`.
- `LocalCaptureAgent.mysql_preflight(settings) -> dict` delegates to `MySQLCaptureStore.preflight()`.
- `LocalCaptureAgent.start_capture(config) -> dict`, `stop_capture() -> dict`, and `status() -> dict`.

- [ ] Step 1: Add failing tests for five logical mappings, PLC/ABB shared Ethernet, unique non-Ethernet bindings, and no-sensor data warnings.
- [ ] Step 2: Run `py -3.11 -m unittest visualization_app.test_local_capture_agent -v`; verify failure because the module is absent.
- [ ] Step 3: Implement the agent as a thin adapter over existing `AcquisitionManager.discover_interfaces()`, `test_connection()`, `start()`, and `stop()`; do not duplicate protocol readers.
- [ ] Step 4: Return separate interface, driver, sensor-data, and acquisition states.
- [ ] Step 5: Run focused helper tests and existing acquisition tests; expected pass.
- [ ] Step 6: Commit `feat: add local capture agent core`.

### Task 3: Pairing and command relay

**Files:**
- Modify: `visualization_app/app.py`
- Modify: `visualization_app/web_access.py`
- Create: `visualization_app/helper_relay.py` — in-memory pairing leases and command allow-list.
- Modify: `visualization_app/test_public_web_security.py`
- Test: `visualization_app/test_local_capture_agent.py`

**Interfaces:**
- `POST /api/helper/pair/start` creates a one-time pairing challenge.
- `POST /api/helper/pair/complete` accepts a helper device id and challenge response.
- `GET /api/helper/status` returns online state and capabilities without secrets.
- `POST /api/helper/command` accepts an allow-listed command and correlation id.

- [ ] Step 1: Add failing tests proving guest users cannot pair or send hardware commands, and authorized users can send only allow-listed commands.
- [ ] Step 2: Run the security tests and verify failure.
- [ ] Step 3: Implement in-memory pairing leases, CSRF checks, command allow-listing, correlation ids, and redacted audit events.
- [ ] Step 4: Add an outbound WebSocket message contract for helper `hello`, `result`, `event`, and `heartbeat` messages.
- [ ] Step 5: Run the security and helper protocol tests; expected pass.
- [ ] Step 6: Commit `feat: add authorized helper pairing relay`.

### Task 4: Helper executable transport and packaging

**Files:**
- Modify: `visualization_app/local_capture_agent.py`
- Create: `visualization_app/build_local_capture_helper.ps1`
- Create: `visualization_app/test_helper_transport.py`

**Interfaces:**
- CLI: `local_capture_agent.exe --server wss://host/helper --pairing-token TOKEN`.
- Messages: JSON objects with `type`, `request_id`, `timestamp`, and `payload`.

- [ ] Step 1: Add failing transport tests for heartbeat, reconnect backoff, malformed command rejection, and secret redaction.
- [ ] Step 2: Run focused transport tests and verify failure.
- [ ] Step 3: Implement the outbound WSS client with bounded reconnect backoff and no inbound listener requirement.
- [ ] Step 4: Implement the PowerShell build script using the existing Python environment and output one helper EXE under the existing delivery tree.
- [ ] Step 5: Run transport tests and a local loopback relay test; expected pass.
- [ ] Step 6: Commit `feat: package local capture helper transport`.

### Task 5: Web UI integration and mode isolation

**Files:**
- Modify: `visualization_app/static/index.html`
- Modify: `visualization_app/static/app.js`
- Modify: `visualization_app/test_frontend_guest_simulation.py`

**Interfaces:**
- UI state `state.helperStatus` contains `paired`, `online`, `capabilities`, and `lastError`.
- Simulation start continues to call existing `/api/acquisition/start` locally.
- Real start sends an allow-listed helper command and renders returned interface/sensor states.

- [ ] Step 1: Add failing DOM/script contract tests for helper status, pairing controls, and simulation bypass.
- [ ] Step 2: Run the frontend tests and verify failure.
- [ ] Step 3: Add a compact helper status panel and pairing button without changing existing sections.
- [ ] Step 4: Route real-mode discover/start/stop through the helper only after authorization; keep simulation catalog independent.
- [ ] Step 5: Run frontend tests, `node --check visualization_app/static/app.js`, and browser smoke checks for both modes.
- [ ] Step 6: Commit `feat: connect web modes to local capture helper`.

### Task 6: Visitor-local CSV and MySQL saving

**Files:**
- Modify: `visualization_app/local_capture_agent.py`
- Modify: `visualization_app/static/app.js`
- Modify: `visualization_app/test_local_capture_agent.py`
- Modify: `visualization_app/test_acquisition_integrity.py`

**Interfaces:**
- Helper receives the original `AcquisitionConfig` save fields and executes the existing save path.
- Web status returns `csv_saved`, `mysql_saved`, `pending_mysql`, and `save_error`.

- [ ] Step 1: Add failing tests for empty save path (no save), authorized local folder save, MySQL-only save, and pending retry.
- [ ] Step 2: Run focused acquisition/helper tests and verify failure for helper routing.
- [ ] Step 3: Reuse original save functions; do not create a second naming or folder algorithm.
- [ ] Step 4: Add browser authorization only when a non-empty folder is confirmed; never claim green save status for an empty path.
- [ ] Step 5: Run all acquisition tests and a local fake-MySQL integration test; expected pass.
- [ ] Step 6: Commit `feat: save web captures on visitor computer`.

### Task 7: LangChain local-plus-model diagnostics

**Files:**
- Modify: `visualization_app/interface_agent.py`
- Modify: `visualization_app/app.py`
- Modify: `visualization_app/static/app.js`
- Modify: `visualization_app/test_interface_agent.py`

**Interfaces:**
- `diagnose(events, api_key, model_name) -> dict` always returns local findings; valid authorized credentials add `model_findings`.
- Tool calls are read-only: configuration, communication logs, and recent sample summary.

- [ ] Step 1: Add failing tests for local-only, valid model enhancement, timeout fallback, and preserved prior findings.
- [ ] Step 2: Run focused agent tests and verify failure for helper event payloads.
- [ ] Step 3: Implement structured event input and schema validation; preserve local findings on every model error.
- [ ] Step 4: Add helper status and MySQL status as model context without sending passwords.
- [ ] Step 5: Run agent tests and browser diagnosis smoke test; expected pass.
- [ ] Step 6: Commit `fix: diagnose helper events with local and model paths`.

### Task 8: Delivery synchronization and end-to-end acceptance

**Files:**
- Modify: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/app/...`
- Create: `docs/superpowers/plans/2026-09-15-local-capture-helper-verification.md`

- [ ] Step 1: Sync only the changed source files into the existing delivery tree and build/update the single main EXE plus the single helper EXE.
- [ ] Step 2: Run `py -3.11 -m unittest visualization_app.test_frontend_guest_simulation visualization_app.test_acquisition_integrity visualization_app.test_local_capture_agent visualization_app.test_mysql_diagnostics -v`.
- [ ] Step 3: Run syntax checks for Python and JavaScript.
- [ ] Step 4: Start the delivery server and perform browser checks: guest simulation, helper offline, helper paired, MySQL unavailable, MySQL available, real capture without sensor, and real capture with a test-double sensor.
- [ ] Step 5: Verify the delivery hashes match source, Git status is clean except ignored delivery artifacts, and one-step rollback points exist.
- [ ] Step 6: Commit `release: deliver web local capture helper` and report exact EXE paths and test evidence.
