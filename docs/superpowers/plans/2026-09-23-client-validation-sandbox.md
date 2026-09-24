# AFP 客户端验证沙盒 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变五类传感器协议、产品采集逻辑和预测功能的前提下，建立一个以 `F:\AFP_Client_Validation_Lab` 为宿主工作区的 Windows Sandbox 客户端验证环境，并用真实 helper、浏览器、便携 MySQL、虚拟 TCP JSON 传感器和独立虚拟 helper 完成公网客户端回归。

**Architecture:** 源码仓库只新增验证资产和测试；执行时将经过哈希核验的 helper、测试数据、无数据目录的 MySQL 运行文件和脚本暂存到 F 盘的独立实验室目录。Windows Sandbox 将输入目录只读映射、将唯一结果目录读写映射，沙盒内部运行真实 helper 与环回 TCP JSON 数据源；另一个协议级虚拟 helper 负责可重复的 50 Hz、ACK、断线重传和 30,000 行完整性测试。Windows Sandbox 的系统运行盘仍由 Windows 管理，不能指定到 F 盘；F 盘保存的是可重建输入、配置和持久验收证据。

**Tech Stack:** Windows 11 Pro、Windows Sandbox `.wsb`、Windows PowerShell 5.1、Python 3.11、现有 `AFP_Local_Capture_Helper.exe`、MySQL 8.4 portable、HTTP/WSS、CSV/JSON、`unittest`。

**Spec:** `openspec/changes/fix-remote-acquisition-workflows/design.md`、`openspec/changes/fix-remote-acquisition-workflows/specs/remote-acquisition-transport/spec.md`、`openspec/changes/fix-remote-acquisition-workflows/specs/mysql-scope-portability/spec.md`、`delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/SECOND_PC_ACCEPTANCE.md`

## Global Constraints

- 产品代码、五类接口驱动、协议解析、预测模型和业务阈值不得为测试环境增加旁路或测试开关。
- Windows Sandbox 功能启用、管理员权限操作和主机重启必须在执行前单独取得用户确认；本计划本身不执行这些操作。
- 不关闭测试完成后的 Sandbox，不卸载 Sandbox；但每分钟把不含秘密的证据写入 F 盘，防止沙盒崩溃、误关或主机重启导致证据丢失。
- Sandbox 配置使用 `MemoryInMB=4096`；网络启用，vGPU、音频输入、视频输入、打印机和剪贴板重定向禁用。
- `F:\AFP_Client_Validation_Lab\input` 只读映射；只有 `F:\AFP_Client_Validation_Lab\results` 可由 Sandbox 写入。
- 不把主机现有 MySQL `data`、`my.ini`、密码、DPAPI 密文、配对令牌、网页授权密码或 API Key 复制进 Sandbox。
- 浏览器授权密码由用户在 Sandbox 浏览器中手工输入；脚本不得读取、记录或导出该密码。
- Sandbox MySQL 只绑定 `127.0.0.1`，使用沙盒内生成的临时密码；结果仅记录配置状态和行数，不记录密码。
- 模拟或虚拟 TCP 数据只能证明软件链路，不得描述为五类真实传感器或真实缺陷证据。
- 实体第二台电脑的 USB/HID、串口、UVC、PLC、ABB 和不同网络现场验收继续保持“待现场确认”。
- 工作区存在用户既有改动；任何提交只能逐文件加入本计划新增/修改的验证资产，禁止包含无关文件。

---

## File Structure

### Source-controlled assets

- Create: `validation/client_lab/README.md` — 实验室使用说明、风险边界和人工步骤。
- Create: `validation/client_lab/AFP-Client-Validation.wsb.template` — F 盘路径占位的 Sandbox 配置模板。
- Create: `validation/client_lab/New-ClientLabStage.ps1` — 生成 F 盘实验室目录、复制白名单文件、渲染 `.wsb`、输出哈希清单。
- Create: `validation/client_lab/Initialize-ClientLab.ps1` — Sandbox 内初始化工作目录和便携 MySQL。
- Create: `validation/client_lab/Start-VirtualTcpSensor.ps1` — 在 `127.0.0.1:19001` 以 50 Hz 输出换行 JSON 样本。
- Create: `validation/client_lab/Invoke-HelperNetworkInterruption.ps1` — 仅阻断 Sandbox 内 helper 出站网络 10 秒并恢复。
- Create: `validation/client_lab/Export-ClientLabEvidence.ps1` — 每分钟导出脱敏状态、计数和进程信息。
- Create: `validation/client_lab/lab-mysql.ini.template` — 只绑定回环地址的 MySQL 配置模板。
- Create: `validation/client_lab/virtual_helper_client.py` — 协议级 WSS/ACK/重连客户端，不使用产品硬件驱动。
- Create: `validation/client_lab/test_client_lab_assets.py` — XML、路径、哈希、秘密扫描和脚本合同测试。
- Create: `validation/client_lab/test_virtual_helper_client.py` — 30,000 行、单在途 ACK、断线重传与幂等测试。

### Planning and acceptance updates

- Modify: `openspec/changes/fix-remote-acquisition-workflows/design.md` — 增加“隔离客户端实验室”设计决策与证据边界。
- Modify: `openspec/changes/fix-remote-acquisition-workflows/specs/remote-acquisition-transport/spec.md` — 增加真实 helper 环回源与协议级虚拟 helper 场景。
- Modify: `openspec/changes/fix-remote-acquisition-workflows/specs/mysql-scope-portability/spec.md` — 增加新用户 DPAPI、Sandbox 本机 MySQL 和跨环境密文拒绝场景。
- Modify: `openspec/changes/fix-remote-acquisition-workflows/tasks.md` — 增加 12.x 客户端实验室任务。
- Modify: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/SECOND_PC_ACCEPTANCE.md` — 将“Sandbox 已验证”和“实体电脑仍待确认”分栏。

### Runtime layout on F drive

```text
F:\AFP_Client_Validation_Lab\
  config\AFP-Client-Validation.wsb
  input\helper\AFP_Local_Capture_Helper.exe
  input\fixtures\SIM_PRESSURE_M3232_new_collection.csv
  input\mysql\bin|lib|share|LICENSE
  input\scripts\*.ps1
  results\<run-id>\
    stage-manifest.json
    environment.json
    minute-metrics.jsonl
    public-browser-checks.json
    helper-pairing.json
    helper-transport.json
    mysql-local.json
    mysql-target.json
    capture-counts.json
    diagnostic-checks.json
    final-summary.json
```

---

### Task 1: Capture the client-lab contract in OpenSpec

**Files:**
- Modify: `openspec/changes/fix-remote-acquisition-workflows/design.md`
- Modify: `openspec/changes/fix-remote-acquisition-workflows/specs/remote-acquisition-transport/spec.md`
- Modify: `openspec/changes/fix-remote-acquisition-workflows/specs/mysql-scope-portability/spec.md`
- Modify: `openspec/changes/fix-remote-acquisition-workflows/tasks.md`

**Interfaces:**
- Consumes: Existing change `fix-remote-acquisition-workflows` and tasks 8.2, 9.10, 10.8.
- Produces: Requirements and scenarios that Tasks 2–10 implement and verify.

- [ ] **Step 1: Add the design boundary**

Add a design section stating that Sandbox verifies a distinct Windows user, DPAPI scope, public browser, actual helper executable, helper-local MySQL and loopback TCP driver, while a protocol emulator verifies deterministic 50 Hz transport. State explicitly that neither substitutes for vendor-specific physical hardware.

- [ ] **Step 2: Add transport scenarios**

Add scenarios requiring exactly 30,000 generated rows for 50 Hz × 600 seconds, one in-flight batch, idempotent retransmission after a 10-second disconnect, progressing ACK sequence and no session crossover.

- [ ] **Step 3: Add MySQL scenarios**

Add scenarios requiring an initially missing Sandbox profile, rejection of copied host DPAPI ciphertext, successful same-Sandbox helper restart, loopback-only MySQL preflight, local write count equality and password-free evidence.

- [ ] **Step 4: Add 12.x tasks**

Create separate task checkboxes for asset generation, Sandbox enablement, real helper verification, protocol-emulator transport, MySQL, evidence export and final review. Keep physical-device validation unchecked.

- [ ] **Step 5: Validate OpenSpec**

Run:

```powershell
openspec validate fix-remote-acquisition-workflows --strict
```

Expected: `Change 'fix-remote-acquisition-workflows' is valid`.

- [ ] **Step 6: Commit only planning artifacts**

```powershell
git add -- openspec/changes/fix-remote-acquisition-workflows/design.md openspec/changes/fix-remote-acquisition-workflows/specs/remote-acquisition-transport/spec.md openspec/changes/fix-remote-acquisition-workflows/specs/mysql-scope-portability/spec.md openspec/changes/fix-remote-acquisition-workflows/tasks.md
git commit -m "test: specify isolated client validation lab"
```

---

### Task 2: Build and test the source-controlled Sandbox asset pack

**Files:**
- Create: `validation/client_lab/README.md`
- Create: `validation/client_lab/AFP-Client-Validation.wsb.template`
- Create: `validation/client_lab/New-ClientLabStage.ps1`
- Create: `validation/client_lab/test_client_lab_assets.py`

**Interfaces:**
- Consumes: Delivery helper and fixture hashes.
- Produces: `New-ClientLabStage.ps1 -RepoRoot <path> -LabRoot F:\AFP_Client_Validation_Lab` and a rendered `.wsb`.

- [ ] **Step 1: Write failing asset-contract tests**

Tests must parse the WSB XML and assert:

```python
assert config.findtext("MemoryInMB") == "4096"
assert config.findtext("Networking") == "Enable"
assert config.findtext("VGpu") == "Disable"
assert config.findtext("AudioInput") == "Disable"
assert config.findtext("VideoInput") == "Disable"
assert config.findtext("PrinterRedirection") == "Disable"
assert config.findtext("ClipboardRedirection") == "Disable"
assert input_mapping.findtext("ReadOnly").lower() == "true"
assert results_mapping.findtext("ReadOnly").lower() == "false"
```

Also assert the staging script excludes `data`, `my.ini`, `runtime`, `logs`, `*.sqlite3`, `config.json` and all secret patterns.

- [ ] **Step 2: Run tests to verify failure**

```powershell
py -3.11 -m unittest -v validation.client_lab.test_client_lab_assets
```

Expected: FAIL because assets do not exist.

- [ ] **Step 3: Implement the template and staging script**

The rendered WSB must map:

```xml
<MappedFolder>
  <HostFolder>F:\AFP_Client_Validation_Lab\input</HostFolder>
  <SandboxFolder>C:\AFP-Lab\Input</SandboxFolder>
  <ReadOnly>true</ReadOnly>
</MappedFolder>
<MappedFolder>
  <HostFolder>F:\AFP_Client_Validation_Lab\results</HostFolder>
  <SandboxFolder>C:\AFP-Lab\Results</SandboxFolder>
  <ReadOnly>false</ReadOnly>
</MappedFolder>
```

The logon command must call `powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\AFP-Lab\Input\scripts\Initialize-ClientLab.ps1`.

- [ ] **Step 4: Pin input hashes**

The staging script must reject any mismatch from:

```text
helper=A743CF60E2AD768E1A599F17613C8BA439BD074E4A22D9157BEBFDA3BDF46564
fixture=58C9290EFD79B8967DD4806FE9CA13B15B4E5508835D3F019A5E943BADD78267
```

- [ ] **Step 5: Run asset tests**

```powershell
py -3.11 -m unittest -v validation.client_lab.test_client_lab_assets
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the asset contract**

```powershell
git add -- validation/client_lab/README.md validation/client_lab/AFP-Client-Validation.wsb.template validation/client_lab/New-ClientLabStage.ps1 validation/client_lab/test_client_lab_assets.py
git commit -m "test: add Windows Sandbox client lab assets"
```

---

### Task 3: Implement Sandbox-local MySQL and virtual TCP sensor assets

**Files:**
- Create: `validation/client_lab/Initialize-ClientLab.ps1`
- Create: `validation/client_lab/Start-VirtualTcpSensor.ps1`
- Create: `validation/client_lab/Export-ClientLabEvidence.ps1`
- Create: `validation/client_lab/lab-mysql.ini.template`
- Modify: `validation/client_lab/test_client_lab_assets.py`

**Interfaces:**
- Consumes: Read-only `C:\AFP-Lab\Input` and writable `C:\AFP-Lab\Results`.
- Produces: MySQL on `127.0.0.1:3306`, TCP JSON sensor on `127.0.0.1:19001`, and one evidence JSONL record per minute.

- [ ] **Step 1: Add failing static and secret-scan tests**

Assert MySQL configuration contains:

```ini
bind-address=127.0.0.1
port=3306
character-set-server=utf8mb4
```

Assert scripts never contain the server target password, browser password, pairing token, OpenAI key or a copied host data path.

- [ ] **Step 2: Implement clean MySQL initialization**

`Initialize-ClientLab.ps1` must copy only `bin`, `lib`, `share` and `LICENSE` from read-only input to `C:\AFP-Lab\Work\mysql`, create a new `data` directory inside Sandbox, call `mysqld --initialize-insecure`, immediately set a generated root password, create `afp_state_warning`, create a loopback-only `afp_app`, and never write the password into F-drive results.

- [ ] **Step 3: Implement deterministic TCP JSON generation**

`Start-VirtualTcpSensor.ps1` must listen only on `127.0.0.1:19001`, schedule using `Stopwatch`, emit exactly 30,000 newline-delimited JSON rows over 600 seconds, include the selected schema channels, and write only sequence/time counters to evidence. Generated values must be marked `synthetic_client_lab=true` and must not be used as defect labels.

- [ ] **Step 4: Implement periodic evidence export**

Every 60 seconds append one JSON object with timestamp, helper process state, feeder produced count, MySQL row count, CSV row count and non-secret network status to `minute-metrics.jsonl`. Write using a temporary file plus atomic rename where a single JSON document is produced.

- [ ] **Step 5: Run tests**

```powershell
py -3.11 -m unittest -v validation.client_lab.test_client_lab_assets
```

Expected: all tests PASS and secret scan returns zero findings.

- [ ] **Step 6: Commit Sandbox-local runtime assets**

```powershell
git add -- validation/client_lab/Initialize-ClientLab.ps1 validation/client_lab/Start-VirtualTcpSensor.ps1 validation/client_lab/Export-ClientLabEvidence.ps1 validation/client_lab/lab-mysql.ini.template validation/client_lab/test_client_lab_assets.py
git commit -m "test: add isolated MySQL and TCP sensor lab"
```

---

### Task 4: Implement the protocol-level virtual helper

**Files:**
- Create: `validation/client_lab/virtual_helper_client.py`
- Create: `validation/client_lab/test_virtual_helper_client.py`

**Interfaces:**
- Consumes: `--server`, one-time `--pairing-challenge`, `--rate-hz`, `--duration-seconds`, `--disconnect-at-seconds`, `--disconnect-seconds`, `--out`.
- Produces: `helper-transport.json` containing counts, ACK sequence, duplicate/replay counts, P50/P95/P99 delay and final state; never outputs token or samples.

- [ ] **Step 1: Write failing pump tests**

Cover these exact assertions:

```python
assert report["produced_rows"] == 30_000
assert report["accepted_rows"] == 30_000
assert report["missing_rows"] == 0
assert report["duplicate_rows"] == 0
assert report["max_in_flight_batches"] == 1
assert report["disconnect_seconds"] == 10
assert report["p95_seconds"] <= 1.0
assert "pairing_token" not in json.dumps(report)
```

- [ ] **Step 2: Run tests to verify failure**

```powershell
py -3.11 -m unittest -v validation.client_lab.test_virtual_helper_client
```

Expected: FAIL because `virtual_helper_client` does not exist.

- [ ] **Step 3: Implement WSS pairing and one-in-flight transport**

Reuse the existing helper WebSocket frame and pairing contracts; do not import or change physical drivers. Hold one unacknowledged batch, replay the same `(capture_uuid, sequence)` after reconnect, and advance only after matching `sample_ack`.

- [ ] **Step 4: Implement the 10-second interruption**

At 300 seconds close only the virtual helper socket, keep the unacknowledged batch, wait 10 seconds, reconnect with the same session token in memory, and replay once. Never persist the token.

- [ ] **Step 5: Run virtual-helper tests**

```powershell
py -3.11 -m unittest -v validation.client_lab.test_virtual_helper_client
```

Expected: all tests PASS with deterministic virtual time.

- [ ] **Step 6: Commit the protocol emulator**

```powershell
git add -- validation/client_lab/virtual_helper_client.py validation/client_lab/test_virtual_helper_client.py
git commit -m "test: add deterministic public helper emulator"
```

---

### Task 5: Stage the F-drive lab without enabling Sandbox

**Files:**
- Create at execution time: `F:\AFP_Client_Validation_Lab\...`
- Read: `F:\AFP_Integrated_Modular_v2\delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic\local_helper\AFP_Local_Capture_Helper.exe`
- Read: `F:\AFP_Capture\simulation_m3232_new_collection\SIM_PRESSURE_M3232_new_collection.csv`
- Read: `F:\softwawre\mysql\bin|lib|share|LICENSE`

**Interfaces:**
- Consumes: Task 2 and Task 3 scripts.
- Produces: `F:\AFP_Client_Validation_Lab\config\AFP-Client-Validation.wsb` and `stage-manifest.json`.

- [ ] **Step 1: Resolve and validate the F-drive target**

Resolve the absolute path and assert it equals or is below `F:\AFP_Client_Validation_Lab`; do not delete or recursively move any broader path. Explicitly reject the repository root `F:\AFP_Integrated_Modular_v2` and every parent of the lab root as a cleanup target.

- [ ] **Step 2: Create only the planned directories**

Run `New-ClientLabStage.ps1` with explicit literal paths. Existing results directories must never be overwritten; allocate a timestamped run id.

- [ ] **Step 3: Verify the staged manifest**

Recalculate every staged file hash. Expected: zero missing, zero mismatched, zero malformed, and no `data`, `my.ini`, runtime database or secret-bearing configuration.

- [ ] **Step 4: Verify no product files changed**

Compare delivery main EXE, helper EXE and `app/legacy` hashes before/after staging. Expected: exact equality.

- [ ] **Step 5: Save a pre-enable environment report**

Record Windows edition/build, RAM, free disk, virtualization firmware status, `HypervisorPresent`, Sandbox executable presence and public health status to `environment.json` without usernames or tokens.

---

### Task 6: Enable Windows Sandbox under a separate approval checkpoint

**Files:**
- No repository file changes.
- System feature change: `Containers-DisposableClientVM`.

**Interfaces:**
- Consumes: Explicit user confirmation for administrator operation and restart.
- Produces: A working `WindowsSandbox.exe` and active Microsoft hypervisor.

- [ ] **Step 1: Reconfirm with the user**

Show the exact command, explain that it may enable Hyper-V dependencies and require one restart, and obtain explicit confirmation immediately before execution.

- [ ] **Step 2: Query current state from an administrator shell**

```powershell
Get-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM
```

Expected before enablement: `Disabled` or `DisabledWithPayloadRemoved`.

- [ ] **Step 3: Enable only the Sandbox feature**

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All -NoRestart
```

Do not disable Memory Integrity, Credential Guard, antivirus, firewall or any security feature.

- [ ] **Step 4: Ask the user to restart at a convenient time**

Do not restart automatically. After the user reports the restart complete, continue.

- [ ] **Step 5: Verify enablement**

Confirm `C:\Windows\System32\WindowsSandbox.exe` exists, feature state is enabled, `HypervisorPresent=true`, and the existing AFP server/public health still returns 200.

---

### Task 7: Launch Sandbox and verify clean-client identity boundaries

**Files:**
- Run: `F:\AFP_Client_Validation_Lab\config\AFP-Client-Validation.wsb`
- Write: `F:\AFP_Client_Validation_Lab\results\<run-id>\environment.json`
- Write: `...\helper-pairing.json`

**Interfaces:**
- Consumes: Enabled Sandbox, staged input and current public URL.
- Produces: Evidence that the browser and helper operate as a separate Windows user/client.

- [ ] **Step 1: Launch the WSB config**

Expected: `C:\AFP-Lab\Input` is read-only, `C:\AFP-Lab\Results` is writable, working files are copied to `C:\AFP-Lab\Work`, MySQL initializes on loopback, and Edge opens the public URL.

- [ ] **Step 2: Verify initial helper-local profile is missing**

Before pairing or configuration, `local_mysql_profile` must report `configured=false`; it must not inherit the host/server password.

- [ ] **Step 3: Verify copied DPAPI ciphertext is rejected**

Use a dedicated test copy of the host encrypted config in Sandbox work storage. Loading must yield `missing` or `decrypt_failed`, never an empty-password fallback. Do not overwrite the actual Sandbox config.

- [ ] **Step 4: Perform manual browser login and pairing**

The user manually enters the authorization password, generates a five-minute pairing code and pastes it into the real helper GUI. Expected: webpage reports helper online and the five static sensor/interface types remain distinct.

- [ ] **Step 5: Restart only the helper inside Sandbox**

Expected: the same Sandbox user decrypts its saved token/profile and reconnects without asking for the host's credentials.

- [ ] **Step 6: Export pairing evidence**

Record only helper hash, device label, online timestamps, capabilities, DPAPI state and interface-type count. Exclude pairing token and password fields recursively.

---

### Task 8: Execute browser simulation and real-helper loopback acquisition

**Files:**
- Run inside Sandbox: `Start-VirtualTcpSensor.ps1`
- Write: `public-browser-checks.json`, `helper-transport.json`, `mysql-local.json`, `capture-counts.json`
- Modify after evidence review: `SECOND_PC_ACCEPTANCE.md`

**Interfaces:**
- Consumes: Real helper pairing, loopback MySQL and virtual TCP sensor.
- Produces: Public browser evidence and actual helper executable evidence.

- [ ] **Step 1: Run the authorized browser simulation workflow**

Upload the staged CSV using the Sandbox browser. Verify the browser uses uploaded content rather than a server `F:\` path, simulation starts without real-control lease/hardware checks, status count grows, and stop produces one saved CSV.

- [ ] **Step 2: Verify five-interface UI preservation**

Switch simulation/real modes and CSV/file-folder source types. Assert PLC, ABB, thermocouple, pressure and UVC retain distinct roles/protocols; no card collapses to custom JSON or a common simulator driver.

- [ ] **Step 3: Configure helper-local MySQL**

In the web form use Sandbox-local `127.0.0.1:3306`, user `afp_app` and database `afp_state_warning`; enter the generated password only inside Sandbox. Preflight must return `scope=helper_local`, `execution_host=visitor_local_computer` and no password.

- [ ] **Step 4: Add one session-local custom TCP JSON interface**

Do not edit the five built-in cards. Add a sixth custom interface bound to `127.0.0.1:19001`, choose only the synthetic test channels, and label it `CLIENT-LAB-TCP`. This tests the real helper pipeline without claiming vendor-driver validation.

- [ ] **Step 5: Run the actual helper for 10 minutes at 50 Hz**

Start real capture with helper-local MySQL and server-target MySQL enabled. Expected generated count is 30,000; helper ACK sequence progresses; page remains responsive; local CSV, local MySQL and server target each report the same complete row count or an explicit independent failure state.

- [ ] **Step 6: Verify database separation**

Confirm helper never receives the target MySQL password, server target and helper-local results display separately, and a failure in either database does not remove the CSV.

- [ ] **Step 7: Export counts before any cleanup**

Write capture UUID, sample rate, duration, generated/accepted/CSV/local-MySQL/target-MySQL row counts, first/last sequence and save states. Do not export raw passwords or the full sample set.

---

### Task 9: Execute disconnect recovery, protocol emulator and diagnosis tests

**Files:**
- Run inside Sandbox: `Invoke-HelperNetworkInterruption.ps1`
- Run on host: `validation/client_lab/virtual_helper_client.py`
- Write: `helper-transport.json`, `diagnostic-checks.json`

**Interfaces:**
- Consumes: Active real helper session and a separate authorized test session for the protocol emulator.
- Produces: Reconnection, idempotency, latency and diagnostic evidence.

- [ ] **Step 1: Block only helper outbound traffic for 10 seconds**

Create a Sandbox-local outbound firewall rule scoped to the copied helper executable, wait exactly 10 seconds, then remove that rule in a `finally` block. Browser and host network must remain online.

- [ ] **Step 2: Verify real helper recovery**

Expected: UI may temporarily show reconnecting/offline; after recovery there is one active helper connection, ACK sequence continues, old connection cleanup does not mark the new connection offline, and no accepted sequence is duplicated.

- [ ] **Step 3: Run the protocol emulator in an independent session**

Use a new one-time pairing code. Run 50 Hz × 600 seconds with a 10-second disconnect at 300 seconds. Expected: 30,000 produced and accepted rows, zero missing/duplicate rows, max one in-flight batch and P95 ≤ 1 second.

- [ ] **Step 4: Run local-first diagnosis**

Use the no-hardware or custom-interface result. Record time to first local diagnosis, final model status, timeout/cache status and evidence wording. Assert it does not claim confirmed hardware damage from synthetic/no-data evidence.

- [ ] **Step 5: Export and redact evidence**

Recursively reject keys or values matching password, pairing token, API key, authorization cookie or private key patterns before writing to `F:\AFP_Client_Validation_Lab\results`.

---

### Task 10: Run regressions, review evidence and leave Sandbox running

**Files:**
- Modify: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/SECOND_PC_ACCEPTANCE.md`
- Modify: `openspec/changes/fix-remote-acquisition-workflows/evidence/verification.md`
- Modify: `openspec/changes/fix-remote-acquisition-workflows/tasks.md`
- Write: `F:\AFP_Client_Validation_Lab\results\<run-id>\final-summary.json`

**Interfaces:**
- Consumes: All Task 7–9 evidence.
- Produces: Auditable client-lab conclusion without overclaiming physical-device validation.

- [ ] **Step 1: Run validation-asset tests**

```powershell
py -3.11 -m unittest -v validation.client_lab.test_client_lab_assets validation.client_lab.test_virtual_helper_client
```

Expected: all tests PASS.

- [ ] **Step 2: Run product regressions**

Run the existing helper transport/WebSocket/relay/local agent, public web security, frontend simulation, MySQL and interface regression groups used by change 11.4. Record exact totals and failures; do not replace full results with a subset claim.

- [ ] **Step 3: Verify delivery integrity**

Run `AFP_Integrated_System_Modular.exe --verify-files`, then independently verify `SHA256SUMS.txt`. Expected: zero missing, mismatched and malformed entries; main/helper EXE hashes unchanged.

- [ ] **Step 4: Produce the final client-lab scorecard**

Use three statuses only: `passed`, `failed`, `not_covered`. Mark vendor-specific USB/HID/serial/UVC/PLC/ABB physical acquisition and a genuinely separate network/computer as `not_covered`, regardless of loopback success.

- [ ] **Step 5: Update OpenSpec conservatively**

Mark 8.2 or 9.10 complete only if their exact authorized-public scenarios have evidence. Keep 10.8 and physical-device portions incomplete until a real second computer and real target database row-count check are returned.

- [ ] **Step 6: Leave Sandbox open**

Do not close Sandbox and do not uninstall the feature. Keep `Export-ClientLabEvidence.ps1` running at one-minute intervals. Report current approximate memory usage and warn that host restart, Sandbox crash or accidental close can still destroy Sandbox-local state.

- [ ] **Step 7: Commit only reviewed verification artifacts**

```powershell
git add -- validation/client_lab delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/SECOND_PC_ACCEPTANCE.md openspec/changes/fix-remote-acquisition-workflows/evidence/verification.md openspec/changes/fix-remote-acquisition-workflows/tasks.md
git commit -m "test: add isolated public client acceptance evidence"
```

---

## Acceptance Matrix

| Area | Required result | Evidence |
|---|---|---|
| F-drive staging | Only planned lab files; hashes match | `stage-manifest.json` |
| Clean client | No inherited helper/MySQL configuration | `environment.json` |
| DPAPI | Host ciphertext rejected; same-Sandbox restart succeeds | `helper-pairing.json` |
| Public browser | HTTPS, upload, simulation start/status/stop work | `public-browser-checks.json` |
| Five interface UI | Five built-in roles remain distinct | browser evidence + summary |
| Real helper EXE | Pairs, stays online, reconnects after 10 seconds | `helper-transport.json` |
| Actual helper pipeline | Custom TCP JSON → helper → WSS → server | `capture-counts.json` |
| 50 Hz | 30,000 generated/accepted, no growth backlog | `helper-transport.json` |
| Helper-local MySQL | Loopback preflight and row count match | `mysql-local.json` |
| Server-target MySQL | Server executes, password not sent, complete rows | `mysql-target.json` |
| Diagnosis | Local result first; model failure does not erase it | `diagnostic-checks.json` |
| Secrets | Zero exported passwords/tokens/keys | secret-scan result |
| Physical five devices | Not covered by Sandbox | `not_covered` |

## Rollback and Recovery

- Before Sandbox enablement: remove only `F:\AFP_Client_Validation_Lab` if the user explicitly requests it; no system rollback is needed.
- After Sandbox enablement: normal test completion leaves Sandbox open and feature installed as requested.
- If the lab fails: export current evidence first, stop only lab processes inside Sandbox, keep product/server processes untouched, and report the exact failed checkpoint.
- If the user later requests full removal: close Sandbox only after evidence export, disable `Containers-DisposableClientVM` through Windows Optional Features, and restart. This is outside the current requested completion behavior and requires separate confirmation.
- Never use `git reset --hard`, broad recursive deletion, or cleanup outside the exact F-drive lab root.
