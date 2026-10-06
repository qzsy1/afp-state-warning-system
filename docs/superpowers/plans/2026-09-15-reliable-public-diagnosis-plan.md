# 公网采集与诊断可靠任务层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with a verification checkpoint after every task. Steps use checkbox syntax for tracking.

**Goal:** 让公网模式稳定完成访问者电脑辅助采集、本地/目标 MySQL 刷新以及本地规则和硅基流动 LangChain 诊断，并保留局域网原有行为。

**Architecture:** 在现有 Flask/HTTP 结构上增加辅助程序心跳过期判定和诊断后台任务存储。硬件检查生成不可变异常快照；本地诊断立即执行；模型诊断提交后台任务并通过短轮询读取结果。只读 MySQL 关系刷新不再申请真实采集控制权，公网授权真实模式统一使用访问者电脑辅助程序识别接口。

**Tech Stack:** Python 3.11、Flask 内置 HTTP 服务、SQLite 安全存储、现有 HelperRegistry HTTP 轮询、LangChain Core、SiliconFlow API、原生 JavaScript Fetch、Git。

**Spec:** docs/superpowers/specs/2026-09-15-reliable-public-diagnosis-design.md

## Global Constraints

- 使用现有分支 feature/original-ui-langchain-agent 和 Git 根目录 F:\AFP_Integrated_Modular_v2\.git。
- 不创建新的版本目录，不复制新的主 EXE；交付目录中的同一 EXE 和辅助 EXE 只在需要时覆盖更新。
- 不改变传感器协议、17 通道定义、CSV 命名层级或 MySQL 表结构。
- 模型只能调用现有只读接口证据工具，不能执行系统命令、任意网络扫描、设备控制或文件写入。
- 访客模拟模式不读取物理硬件；公网授权真实模式通过访问者电脑辅助程序；局域网管理员模式读取服务器本机。
- 每个任务严格执行：先写失败测试，运行确认失败，写最小实现，运行相关回归，提交一个小 Git commit。

---

### Task 1: 辅助程序心跳与命令生命周期

Files:
- Modify: visualization_app/helper_relay.py
- Modify: visualization_app/local_capture_helper_entry.py
- Modify: visualization_app/local_capture_agent.py
- Test: visualization_app/test_helper_relay.py
- Test: visualization_app/test_helper_transport.py

Interfaces:
- Consumes: HelperRegistry.start_pairing, complete_pairing, poll, command, accept_result.
- Produces: heartbeat_ttl_seconds=15, stale-online handling, immediate helper_offline response, visible authentication failure.

- [ ] Step 1: Write failing tests.

Add a registry test that completes pairing, advances the registry clock past the configured TTL, and asserts status online=false plus command queued=false/error=helper_offline. Add a transport test that feeds an authentication failure and asserts a re-pair message instead of silent retry.

- [ ] Step 2: Verify RED.

    cd /d F:\AFP_Integrated_Modular_v2\visualization_app
    C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe -m unittest -v test_helper_relay test_helper_transport

Expected: the stale-heartbeat and authentication-failure assertions fail against the current implementation.

- [ ] Step 3: Implement minimally.

Add a time-based TTL and one liveness predicate used by status and command. Do not enqueue commands for stale helpers. In run_http_forever, classify helper authentication failure as terminal re-pair guidance and keep bounded backoff only for transient network errors. Preserve request_id for every result.

- [ ] Step 4: Verify GREEN.

Run the two focused modules plus test_local_capture_agent. All existing command-type, pairing-label, and known-error tests must pass.

- [ ] Step 5: Commit.

    F:\software\Git\cmd\git.exe add visualization_app/helper_relay.py visualization_app/local_capture_helper_entry.py visualization_app/local_capture_agent.py visualization_app/test_helper_relay.py visualization_app/test_helper_transport.py
    F:\software\Git\cmd\git.exe commit -m "fix: make helper liveness and command errors explicit"

---

### Task 2: 公网接口识别路由与只读 MySQL 刷新

Files:
- Modify: visualization_app/app.py
- Modify: visualization_app/static/app.js
- Test: visualization_app/test_frontend_guest_simulation.py
- Test: visualization_app/test_public_web_security.py
- Test: visualization_app/test_mysql_diagnostics.py

Interfaces:
- Consumes: _require_real_control, /api/mysql/relation-map, requestLocalHelper, discoverInterfaces.
- Produces: one authoritative discovery function; public authorized discovery through helper; read-only relation refresh without capture lease.

- [ ] Step 1: Write failing tests.

Add an authorized route fixture that calls target relation-map without an acquired real-control lease and asserts it reaches relation_map rather than returning real_access_required. Add a characterization test that fails when two discoverInterfaces definitions exist or when the authorized branch does not call requestLocalHelper("discover", ...).

- [ ] Step 2: Verify RED.

    C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe -m unittest -v test_mysql_diagnostics test_public_web_security test_frontend_guest_simulation

Expected: the route fails because relation-map is in controlled_paths and the discovery test fails because a later function overrides the helper-aware branch. If the plain interpreter lacks PyTorch, record that limitation and use the packaged EXE integration check.

- [ ] Step 3: Implement minimally.

Remove relation-map from the exclusive capture-control set while keeping acquisition start/stop/test protected. Merge the two discovery functions. Simulation uses source channels, public authorized real mode uses the local helper, and LAN administrator mode uses server discovery.

- [ ] Step 4: Verify GREEN.

Run focused modules and LAN regression tests. Confirm simulation never requests physical discovery and authorized mode preserves helper discovery.

- [ ] Step 5: Commit.

    F:\software\Git\cmd\git.exe add visualization_app/app.py visualization_app/static/app.js visualization_app/test_frontend_guest_simulation.py visualization_app/test_public_web_security.py visualization_app/test_mysql_diagnostics.py
    F:\software\Git\cmd\git.exe commit -m "fix: separate public read-only mysql refresh from capture control"

---

### Task 3: 后台 LangChain 诊断任务 API

Files:
- Modify: visualization_app/app.py
- Modify: visualization_app/interface_agent.py
- Create: visualization_app/diagnosis_jobs.py
- Test: visualization_app/test_agentic_diagnosis.py
- Test: visualization_app/test_public_web_security.py

Interfaces:
- Consumes: _validate_agent_payload, run_interface_diagnoses, existing model limiter/lock, session identity and CSRF policy.
- Produces: DiagnosisJobStore.create/get, POST /api/agent/diagnose/start, GET /api/agent/diagnose/result?job_id=..., with the old synchronous endpoint retained.

- [ ] Step 1: Write failing lifecycle tests.

Use an injected deterministic runner. Assert create returns a job_id and pending/running state; success stores all diagnoses; exceptions store failed and preserve the event fingerprint; a different session cannot read the job.

- [ ] Step 2: Verify RED.

    C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe -m unittest -v test_agentic_diagnosis test_public_web_security

Expected: job store or endpoint failures because the task API does not exist.

- [ ] Step 3: Implement minimally.

Create a bounded in-memory job store with owner session, event fingerprint, timestamps, state, result and error. Start a daemon worker using the existing runner, limiter and lock. Keep model keys out of job state and logs. Enforce permission, CSRF, duplicate active fingerprint, and owner checks.

- [ ] Step 4: Verify GREEN.

Run diagnosis and available security tests. Assert local diagnosis is retained on model failure and successful provider execution is marked model_used=true.

- [ ] Step 5: Commit.

    F:\software\Git\cmd\git.exe add visualization_app/app.py visualization_app/interface_agent.py visualization_app/diagnosis_jobs.py visualization_app/test_agentic_diagnosis.py visualization_app/test_public_web_security.py
    F:\software\Git\cmd\git.exe commit -m "feat: add resumable background diagnosis jobs"

---

### Task 4: 前端异常快照与任务轮询

Files:
- Modify: visualization_app/static/app.js
- Modify: visualization_app/static/index.html
- Test: visualization_app/test_frontend_guest_simulation.py
- Test: visualization_app/test_agentic_diagnosis.py

Interfaces:
- Consumes: helper liveness, discovery routing, diagnosis job endpoints.
- Produces: immutable agentSnapshot, immediate local diagnosis, model job polling that survives page refresh, separate hardware/local/model states.

- [ ] Step 1: Write failing frontend contract tests.

Assert that five abnormal interfaces create one immutable snapshot, model submission uses diagnose/start, polling uses diagnose/result, and a new hardware check does not abort the submitted server job. Assert a model success arriving after more than 30 seconds is rendered.

- [ ] Step 2: Verify RED.

    C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe -m unittest -v test_frontend_guest_simulation test_agentic_diagnosis

Expected: the current synchronous endpoint and AbortController invalidation violate these assertions.

- [ ] Step 3: Implement minimally.

Deep-copy hardware result and events on fingerprint change. Run local diagnosis from that snapshot, submit one model job per fingerprint, poll once per second, and store completed results separately from current hardware state. Do not cancel server jobs when a new check starts.

- [ ] Step 4: Verify GREEN.

Run focused tests and a local page smoke check. Confirm status sequence hardware result, local result, model pending, model success/failure, and preserve guest Start-button behavior.

- [ ] Step 5: Commit.

    F:\software\Git\cmd\git.exe add visualization_app/static/app.js visualization_app/static/index.html visualization_app/test_frontend_guest_simulation.py visualization_app/test_agentic_diagnosis.py
    F:\software\Git\cmd\git.exe commit -m "fix: preserve diagnosis snapshots and poll model jobs"

---

### Task 5: 运行版同步、构建与完整公网闭环

Files:
- Modify: existing delivery legacy files corresponding to Tasks 1–4
- Overwrite only: existing delivery AFP_Integrated_System_Modular.exe when required
- Overwrite only: existing delivery local_helper AFP_Local_Capture_Helper.exe when required

Interfaces:
- Consumes: all source commits from Tasks 1–4.
- Produces: one synchronized delivery tree, verified startup, LAN URL, public URL, and a rollback point in Git.

- [ ] Step 1: Check delivery parity before building.

Compare SHA-256 for every copied source file. Fail if a delivery file is missing or older. Confirm no second EXE or version directory exists.

- [ ] Step 2: Synchronize in place.

Use the existing build scripts and delivery directory. Stop only exact processes locking existing binaries, copy rebuilt binaries back to the same paths, and record SHA-256.

- [ ] Step 3: Run automated verification.

    C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe -m unittest -q test_helper_relay test_helper_transport test_local_capture_agent test_mysql_diagnostics test_remote_mysql_setup test_interface_agent test_agentic_diagnosis test_frontend_guest_simulation
    C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe -m unittest -q modular_runtime.tests.test_public_web modular_runtime.tests.test_lan_web modular_runtime.tests.test_modular_runtime

Expected: all runnable tests pass; any PyTorch-only source limitation is recorded separately and covered by packaged-EXE integration checks.

- [ ] Step 4: Run LAN and public integration checks.

Start the existing EXE and Tunnel. Verify health locally, by LAN URL and by HTTPS public URL. With a fresh authorized session and paired helper, verify target and local MySQL preflight with write_test=true, target relation refresh without capture lease, helper discover/check_capture, local diagnosis coverage, model job pending-to-success, and page refresh during model execution.

Stop the test helper and remove only the temporary test session row. Keep configured owner password and model credentials unchanged.

- [ ] Step 5: Stability check and final commit.

Check the public root and health endpoint every 20 seconds for at least four checks. Confirm the desktop listener and cloudflared remain alive. Run:

    F:\software\Git\cmd\git.exe diff --check
    F:\software\Git\cmd\git.exe status --porcelain

Expected: no whitespace errors, no duplicate delivery tree, and only intentional committed changes. Commit the final synchronization:

    F:\software\Git\cmd\git.exe add visualization_app delivery
    F:\software\Git\cmd\git.exe commit -m "chore: verify public diagnosis and mysql closed loop"

