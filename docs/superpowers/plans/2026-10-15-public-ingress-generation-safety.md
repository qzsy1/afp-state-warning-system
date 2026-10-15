# Public Ingress Generation Safety Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让公网入口、部署事务、服务器 Helper 会话和验证电脑 Helper 共享同一持久化入口代次，并修复受管模型路径误判，使当前分支能够通过生产行为门禁后安全合并到 `main`。

**Architecture:** 在 `visualization_app/PublicIngress.psm1` 集中实现 Windows 公共边缘探测、入口状态原子发布和恢复所有权判断；Python 服务器通过 `public_ingress_state.py` 读取同一状态，并由 `HelperRegistry` 在每次配对、认证、轮询、结果提交和命令投递前同步代次。两个 watchdog 和单运行目录部署只调用生产入口，Helper 保存配置携带代次且只允许同代次自动重连；模型来源由后端依据 manifest 受管根目录和真实路径判定，前端只消费明确来源字段。

**Tech Stack:** Python 3 标准库、Windows PowerShell 5.1、`unittest`、现有 AFP HTTP/WebSocket 协议、OpenSpec、AFP Harness。

**Spec:** `openspec/changes/complete-pre-sensor-readiness/design.md`

## Global Constraints

- 不部署或替换 `F:\new_AFP_exam\AFP_Integrated_System_Modular`，不停止、重启或切换当前运行进程。
- 不执行双机步骤 3，不发送采集启动请求，不用超时触发自动重试。
- 不修改防火墙、Tailscale、Windows 服务、电源、时间或生产网络配置。
- 不读取或输出配对码、Token、Cookie、密码、代理凭据或 MySQL 密钥；响应正文、WebSocket 帧和敏感请求头不得写入证据或日志，静态摘要校验使用的临时响应文件必须立即清除。
- 公共边缘只有在公共 DNS 返回全局可路由 A/AAAA 地址、请求绕过代理、保持原域名 TLS SNI/Host、禁止重定向并得到精确 HTTP 200 时通过。
- URL、提供方或恢复所有者变化必须原子发布单调递增代次；旧配对、readiness、会话和未完成采集命令必须得到明确失效或终态。
- 入口切换在存在活动采集时失败关闭；普通安装、自检和自动恢复不得夺取所有权或退出旧入口。
- 所有文档和代码注释只描述当前有效状态，不保留开发流水账、阶段字段或作废方案。
- 保留当前工作树中不属于本任务的已有修改；每次提交只暂存计划明确列出的文件。
- 不增加第三方运行时依赖。

## Review Focus

- IPv4-mapped IPv6、带区域标识 IPv6、保留地址及 DNS 混合答案必须只选择真正全局可路由地址；Task 1 的地址向量测试覆盖。
- 状态文件原子替换成功、进程在内存失效前退出时，服务器重启必须依据新代次拒绝旧 token 和旧命令；Task 2 的重启恢复测试覆盖。
- Helper 轮询与入口代次发布并发时，旧命令最多得到一个失败终态且绝不执行；Task 2 和 Task 3 的并发屏障测试覆盖。
- 活动采集或已确认运行的远端采集存在时，watchdog 与部署事务都不得切换入口；Task 4 和 Task 5 的活动采集门禁测试覆盖。
- Windows junction、符号链接、大小写差异和相似目录名不得把外部模型归类为受管模型；Task 6 的真实路径测试覆盖。

## File Map

- Create `visualization_app/PublicIngress.psm1`: 公共地址分类、公共 DNS、强制边缘探测、入口状态 schema、原子发布和恢复所有权。
- Create `visualization_app/public_ingress_state.py`: Python 侧入口状态严格读取与候选身份校验。
- Create `visualization_app/test_public_ingress_module.py`: 直接调用生产 PowerShell 模块的行为测试。
- Create `visualization_app/test_public_ingress_state.py`: Python 状态读取、损坏状态和重启同步测试。
- Modify `visualization_app/helper_relay.py`: 代次绑定、旧会话失效、命令生命周期和切换就绪状态。
- Modify `visualization_app/app.py`: 共享状态路径、Helper 代次 API 和仅回环可读的切换就绪端点。
- Modify `visualization_app/local_capture_agent.py`: Helper 传输携带入口代次并拒绝不匹配命令。
- Modify `visualization_app/local_capture_helper_entry.py`: 当前入口优先、保存代次、同代次重连和重新配对。
- Modify `visualization_app/tailscale_funnel_watchdog.ps1`: Tailscale 当前所有者恢复、分层状态和原子发布。
- Modify `visualization_app/public_tunnel_watchdog.ps1`: Cloudflare 非所有者只读与显式所有者切换。
- Modify `pre_sensor/SingleRuntimeDeployment.psm1`: 使用共享公共边缘探测和可注入的异常安全提升事务。
- Modify `pre_sensor/manage-single-runtime-delivery.ps1`: 只通过生产事务入口完成提升或回滚。
- Modify `visualization_app/release_contract.py` and `pre_sensor/release-contract.json`: 声明并解析受管模型根目录。
- Modify `visualization_app/online_inference.py` and `visualization_app/static/app.js`: 后端模型来源分类与前端来源消费。
- Modify `modular_runtime/assemble_modular_delivery.ps1`: 装配共享模块并写入 manifest 受管模型根目录。
- Modify focused tests under `visualization_app/`, `harness/tests/`, and `modular_runtime/tests/`: 覆盖生产行为和发行装配。
- Modify `harness/config/regression-matrix.json`, `harness/config/profiles.json`, `harness/engine/public_ingress.py`, `harness/README.md`, and `modular_runtime/README.md`: 强制路由与当前操作合同。

---

### Task 1: Shared public-edge probe and atomic ingress state

**Files:**
- Create: `visualization_app/PublicIngress.psm1`
- Create: `visualization_app/test_public_ingress_module.py`
- Modify: `modular_runtime/assemble_modular_delivery.ps1`
- Modify: `modular_runtime/tests/test_modular_runtime.py`

**Interfaces:**
- Produces: `Test-AfpGlobalIPAddress -Address <string> -> bool`
- Produces: `Resolve-AfpPublicEdgeAddresses -HostName <string> [-Resolver <scriptblock>] -> string[]`
- Produces: `Invoke-AfpForcedPublicProbe -Uri <Uri> -Address <string> [-ExpectedSha256 <string>] [-CurlRunner <scriptblock>] -> psobject`
- Produces: `Read-AfpPublicIngressState -Path <string> [-ExpectedCandidateId <string>] -> psobject`
- Produces: `Publish-AfpPublicIngressState -Path <string> -PublicUrl <string> -Provider <string> -RecoveryOwner <string> -CandidateId <string> -Layers <hashtable> [-ExpectedGeneration <long>] -> psobject`
- Produces: `Test-AfpIngressRecoveryOwner -State <psobject> -Provider <string> -> bool`

- [ ] **Step 1: Write the failing production-module tests**

Add tests named:

- `test_global_address_classifier_rejects_every_special_range_and_accepts_public_v4_v6`
- `test_forced_probe_uses_resolve_noproxy_no_redirect_and_requires_exact_200`
- `test_dns_mixed_answers_keep_only_global_addresses`
- `test_atomic_publish_increments_only_on_url_provider_or_owner_change`
- `test_atomic_publish_preserves_previous_state_on_replace_failure`
- `test_state_reader_rejects_corruption_generation_rollback_and_candidate_mismatch`
- `test_recovery_owner_is_exact_and_case_normalized`

The address vectors include `0.0.0.0/8`, RFC1918, `100.64.0.0/10`, loopback, link-local, documentation, benchmark, multicast, reserved, IPv4-mapped IPv6, `fc00::/7`, `fe80::/10`, `2001:db8::/32`, multicast, a zoned IPv6 literal, `1.1.1.1`, and `2606:4700:4700::1111`. The probe runner records arguments and returns status `200`, `204`, `301`, and transport failure so assertions can require only `200`.

- [ ] **Step 2: Run Task 1 tests and verify the missing module fails**

Run: `python -m unittest visualization_app.test_public_ingress_module -v`

Expected: FAIL because `visualization_app/PublicIngress.psm1` or the exported functions do not exist.

- [ ] **Step 3: Implement the PowerShell module**

Use a complete special-use prefix table for IPv4 and IPv6. `Invoke-AfpForcedPublicProbe` invokes `curl.exe` with `--noproxy "*"`, `--max-redirs 0`, `--resolve`, a bounded timeout and `--write-out`; it stores any body only in a temporary file for optional SHA-256 comparison and always deletes that file. `Publish-AfpPublicIngressState` uses a named mutex derived from the normalized state path, checks `ExpectedGeneration`, writes UTF-8 JSON to a same-directory unique temporary file, flushes it, atomically replaces the destination, rereads it, and returns `changed`, `generation`, and the validated state.

- [ ] **Step 4: Package the module with every candidate**

Add `PublicIngress.psm1` to `$operationalFiles` in `assemble_modular_delivery.ps1`. Extend `test_modular_runtime.py` to assert the module is copied and included in candidate integrity output.

- [ ] **Step 5: Run Task 1 verification**

Run: `python -m unittest visualization_app.test_public_ingress_module modular_runtime.tests.test_modular_runtime -v`

Expected: PASS with all new address, HTTP, atomicity and packaging cases green.

- [ ] **Step 6: Commit Task 1**

Run:

```powershell
git add visualization_app/PublicIngress.psm1 visualization_app/test_public_ingress_module.py modular_runtime/assemble_modular_delivery.ps1 modular_runtime/tests/test_modular_runtime.py
git commit -m "feat: centralize public ingress state"
```

### Task 2: Server generation synchronization and command terminal states

**Files:**
- Create: `visualization_app/public_ingress_state.py`
- Create: `visualization_app/test_public_ingress_state.py`
- Modify: `visualization_app/helper_relay.py`
- Modify: `visualization_app/app.py`
- Modify: `visualization_app/test_helper_relay.py`
- Modify: `visualization_app/test_helper_websocket.py`
- Modify: `visualization_app/test_public_web_security.py`

**Interfaces:**
- Consumes: Task 1 state schema fields `schema_version`, `generation`, `public_url`, `provider`, `recovery_owner`, `candidate_id`, `status`, `published_at`, and `layers`.
- Produces: immutable `PublicIngressState` and `load_public_ingress_state(path: Path, expected_candidate_id: str = "") -> PublicIngressState`.
- Produces: `public_ingress_state_path(contract: ReleaseContract) -> Path`, resolved as `<manifest-root>/runtime/public-ingress-state.json`.
- Produces: `HelperRegistry(..., ingress_state_path: str | Path | None = None, expected_candidate_id: str = "", ingress_state_loader: Callable | None = None)`.
- Produces: `HelperRegistry.cutover_readiness() -> dict[str, Any]` with `allowed`, `generation`, `active_capture_uuids`, and `blocking_request_ids`.
- Produces: loopback-only `GET /api/ingress/cutover-readiness`.

- [ ] **Step 1: Write failing Python state and registry tests**

Add tests for valid load, missing state, malformed JSON, missing fields, negative generation, candidate mismatch and manifest-root path resolution. Extend `test_helper_relay.py` with:

- `test_generation_change_revokes_pairing_and_persisted_helper_token`
- `test_generation_change_terminalizes_queued_and_unconfirmed_commands_once`
- `test_generation_change_during_poll_never_delivers_old_command`
- `test_restart_after_state_publish_refuses_old_persisted_helper`
- `test_command_and_result_generation_mismatch_are_rejected`
- `test_cutover_readiness_blocks_confirmed_running_capture`

Use a `threading.Barrier` around poll/state-load for the concurrency case. Assert terminal payload `{"ok": False, "error": "ingress_generation_changed", "terminal": True}` is returned once through `pop_result`.

- [ ] **Step 2: Run focused tests and verify generation behavior is absent**

Run: `python -m unittest visualization_app.test_public_ingress_state visualization_app.test_helper_relay -v`

Expected: FAIL because the state module, constructor parameters, generation fields and terminalization do not exist.

- [ ] **Step 3: Implement strict state loading and registry synchronization**

Add `generation` to `_Pairing` and `_Helper`. Track command records by request ID with command name, generation, delivery state and terminal result; persist only hashes, generation, non-sensitive command metadata and terminal status. At the start of pairing, completion, attach, authenticate, poll, result acceptance, status and command, synchronize the state under the registry lock. A newer generation clears pairings and helper credentials, detaches senders, terminalizes old queued/delivered-unconfirmed commands, and never rewrites them to the new generation. State errors keep the registry not ready.

- [ ] **Step 4: Add app wiring and the loopback cutover-readiness endpoint**

Pass `public_ingress_state_path(RELEASE_CONTRACT)` and the manifest candidate ID into `HelperRegistry`. The endpoint combines `HelperRegistry.cutover_readiness()`, local acquisition running state, remote acquisition state and unfinished stop/reconciliation state. Reject non-loopback callers with HTTP 403 and return no tokens, pairing codes or payload bodies.

- [ ] **Step 5: Test WebSocket and HTTP generation enforcement**

Extend WebSocket/public-security tests to assert old-generation authentication is rejected, a command frame contains `ingress_generation`, stale result/sample submission is rejected, and the cutover-readiness endpoint is loopback-only.

Run: `python -m unittest visualization_app.test_public_ingress_state visualization_app.test_helper_relay visualization_app.test_helper_websocket visualization_app.test_public_web_security -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

Run:

```powershell
git add visualization_app/public_ingress_state.py visualization_app/test_public_ingress_state.py visualization_app/helper_relay.py visualization_app/app.py visualization_app/test_helper_relay.py visualization_app/test_helper_websocket.py visualization_app/test_public_web_security.py
git commit -m "feat: bind helper sessions to ingress generation"
```

### Task 3: Helper endpoint authority and same-generation reconnect

**Files:**
- Modify: `visualization_app/local_capture_agent.py`
- Modify: `visualization_app/local_capture_helper_entry.py`
- Modify: `visualization_app/test_helper_transport.py`
- Modify: `visualization_app/test_helper_websocket.py`

**Interfaces:**
- Consumes: Task 2 pairing response and command field `ingress_generation: int`.
- Produces: `HelperTransport(server_url, pairing_token, device_id="local-helper", ingress_generation: int | None = None)`.
- Produces: `select_runtime_endpoint(saved: Mapping[str, Any] | None, current_hint: str) -> dict[str, Any]` with `server`, `pairing_token`, `ingress_generation`, and `pairing_required`.
- Produces: `save_runtime_config(..., ingress_generation: int | None = None, path: str | Path | None = None) -> Path`.
- Produces: `load_saved_runtime_config(*, path: str | Path | None = None) -> dict[str, Any] | None`.
- Produces: `HelperCommandDispatcher(..., ingress_generation: int | None = None)` rejecting mismatched commands before calling `LocalCaptureAgent`.

- [ ] **Step 1: Write failing Helper selection and generation tests**

Add tests:

- `test_current_hint_replaces_different_saved_url_and_discards_saved_token`
- `test_same_url_and_generation_reuses_saved_token`
- `test_pairing_response_generation_is_saved_atomically`
- `test_http_and_websocket_transport_send_generation`
- `test_dispatcher_rejects_stale_generation_without_agent_side_effect`
- `test_authentication_failure_requires_pairing_instead_of_old_url_fallback`
- `test_concurrent_generation_change_cannot_execute_start_capture`

The concurrent test pauses immediately before dispatch, changes the dispatcher's expected generation, then asserts `agent.start_capture.call_count == 0`.

- [ ] **Step 2: Run focused Helper tests and verify they fail**

Run: `python -m unittest visualization_app.test_helper_transport visualization_app.test_helper_websocket -v`

Expected: FAIL because saved configuration and transport do not carry generation and saved URL still takes precedence.

- [ ] **Step 3: Implement endpoint selection and persisted generation**

Replace `preferred_setup_server` call sites with `select_runtime_endpoint`. A non-empty current hint that differs after URL normalization wins and clears the token/generation; an identical URL may reuse the saved token only with a valid non-negative generation. Keep DPAPI token protection and same-directory atomic config replacement.

- [ ] **Step 4: Enforce generation in transport and dispatch**

Add `X-AFP-Ingress-Generation` to HTTPS requests and `ingress_generation` to the WebSocket query/hello. Preserve generation across WSS-to-HTTPS fallback. `HelperTransport.decode_command` returns generation; `HelperCommandDispatcher.submit` checks it under its control lock immediately before agent dispatch and returns terminal `ingress_generation_changed` without running the command when stale.

- [ ] **Step 5: Run Task 3 verification**

Run: `python -m unittest visualization_app.test_helper_transport visualization_app.test_helper_websocket visualization_app.test_helper_relay -v`

Expected: PASS, including existing reconnect, result outbox and stale WebSocket-detach regressions.

- [ ] **Step 6: Commit Task 3**

Run:

```powershell
git add visualization_app/local_capture_agent.py visualization_app/local_capture_helper_entry.py visualization_app/test_helper_transport.py visualization_app/test_helper_websocket.py
git commit -m "fix: require current ingress generation in helper"
```

### Task 4: Single-owner watchdog recovery and layered status

**Files:**
- Modify: `visualization_app/tailscale_funnel_watchdog.ps1`
- Modify: `visualization_app/public_tunnel_watchdog.ps1`
- Modify: `visualization_app/test_tailscale_funnel_watchdog.py`
- Modify: `visualization_app/test_public_tunnel_watchdog.py`
- Modify: `harness/tests/test_public_ingress_contract.py`

**Interfaces:**
- Consumes: Task 1 module and Task 2 loopback cutover-readiness endpoint.
- Produces: watchdog parameters `-Mode Run|Install|Uninstall|Once|Cutover`, `-ExpectedGeneration <long>`, and `-ApproveOwnerCutover`.
- Produces: layered status JSON fields `overall_status`, `origin`, `control_plane`, `dns`, `public_edge`, `ingress`, `helper`, and `non_sensitive_error`.

- [ ] **Step 1: Write failing watchdog ownership and status tests**

Add PowerShell behavior tests that import the production module and inject only process, DNS, curl, Helper and task-scheduler boundaries. Cover:

- non-owner `Run` and `Once` never start/stop a tunnel, publish URL or restart Helper;
- `Install` never claims ownership;
- `Cutover` requires both `ApproveOwnerCutover` and matching `ExpectedGeneration`;
- same-owner URL recovery publishes one new generation;
- active capture blocks URL/owner changes;
- Helper restart failure leaves public-edge layer passed but `overall_status=failed`;
- state/URL files remain unchanged on probe or atomic publication failure;
- two watchdog processes racing the same expected generation yield one winner.

- [ ] **Step 2: Run watchdog tests and verify current scripts fail**

Run: `python -m unittest visualization_app.test_tailscale_funnel_watchdog visualization_app.test_public_tunnel_watchdog harness.tests.test_public_ingress_contract -v`

Expected: FAIL because both scripts independently own recovery, write URL files directly, and collapse Helper failures into `online`.

- [ ] **Step 3: Refactor both watchdogs onto the shared module**

Import `PublicIngress.psm1` from `$PSScriptRoot`. Remove local public-address, forced-probe and direct URL-write implementations. The current owner may recover its provider and publish a new generation after the loopback cutover-readiness gate passes. Non-owner paths report only. `Cutover` is the sole path that can change `recovery_owner` or retire the previous provider.

- [ ] **Step 4: Make Helper outcome part of aggregate readiness**

After state publication, restart or ensure Helper and verify the expected executable path and process. Write each layer separately; only write `overall_status=online` when every required layer and Helper action passes. Keep non-sensitive error codes, not command output that could contain credentials.

- [ ] **Step 5: Run Task 4 verification**

Run: `python -m unittest visualization_app.test_public_ingress_module visualization_app.test_tailscale_funnel_watchdog visualization_app.test_public_tunnel_watchdog harness.tests.test_public_ingress_contract -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

Run:

```powershell
git add visualization_app/tailscale_funnel_watchdog.ps1 visualization_app/public_tunnel_watchdog.ps1 visualization_app/test_tailscale_funnel_watchdog.py visualization_app/test_public_tunnel_watchdog.py harness/tests/test_public_ingress_contract.py
git commit -m "fix: enforce single-owner ingress recovery"
```

### Task 5: Forced-edge deployment gate and exception-safe rollback

**Files:**
- Modify: `pre_sensor/SingleRuntimeDeployment.psm1`
- Modify: `pre_sensor/manage-single-runtime-delivery.ps1`
- Modify: `harness/tests/test_single_runtime_deployment.py`

**Interfaces:**
- Consumes: Task 1 `Invoke-AfpForcedPublicProbe`, `Read-AfpPublicIngressState`, and `Publish-AfpPublicIngressState`.
- Consumes: Task 2 `GET /api/ingress/cutover-readiness`.
- Produces: `Test-AfpRuntimeServices(..., -PublicEdgeProbeRunner <scriptblock>)` where `public_https` and `public_static_identity` use forced-edge results.
- Produces: `Invoke-AfpVerifiedRuntimePromotion(..., -StartRunner, -StopRunner, -ServiceProbeRunner, -IngressPublisher, -CompletionRunner, -RestoreRunner) -> psobject`.

- [ ] **Step 1: Write failing deployment tests**

Extend `test_single_runtime_deployment.py` with:

- `test_service_gate_rejects_split_dns_success_without_forced_edge`
- `test_service_gate_rejects_redirect_and_non_200`
- `test_promotion_does_not_complete_before_edge_and_state_gates`
- `test_each_exception_point_restores_runtime_and_ingress_snapshot`
- `test_active_capture_blocks_promotion_before_process_stop`
- `test_rollback_failure_is_terminal_and_preserves_both_evidence_paths`

Use injected runners and temporary directories; no real processes, network or fixed runtime.

- [ ] **Step 2: Run deployment tests and verify the current gate fails**

Run: `python -m unittest harness.tests.test_single_runtime_deployment -v`

Expected: FAIL because ordinary `Invoke-WebRequest`/WebClient can satisfy public checks and exceptions can bypass restoration.

- [ ] **Step 3: Replace same-host public evidence with the shared forced-edge probe**

Import the candidate's `PublicIngress.psm1`. `Test-AfpPublicStaticIdentity` downloads through the forced address/SNI path and compares SHA-256 without logging bytes. Remove ordinary public-domain requests from `Test-AfpRuntimeServices`; keep loopback and LAN probes as separate layers.

- [ ] **Step 4: Implement one exception-safe promotion transaction**

`Invoke-AfpVerifiedRuntimePromotion` checks loopback cutover readiness before stopping processes, snapshots ingress state, promotes, starts, waits for all service layers, publishes/revalidates ingress, and only then calls completion. A single `try/catch/finally` owns restoration; any exception restores ingress state and runtime, restarts prior roles, and returns or throws an explicit terminal result. `manage-single-runtime-delivery.ps1` delegates the switchover branch to this function instead of duplicating the sequence.

- [ ] **Step 5: Run Task 5 verification**

Run: `python -m unittest harness.tests.test_single_runtime_deployment harness.tests.test_single_runtime_startup_contract -v`

Expected: PASS with every injected exception restoring the pre-transaction state.

- [ ] **Step 6: Commit Task 5**

Run:

```powershell
git add pre_sensor/SingleRuntimeDeployment.psm1 pre_sensor/manage-single-runtime-delivery.ps1 harness/tests/test_single_runtime_deployment.py
git commit -m "fix: make runtime promotion public-edge safe"
```

### Task 6: Manifest-backed model provenance

**Files:**
- Modify: `pre_sensor/release-contract.json`
- Modify: `visualization_app/release_contract.py`
- Modify: `visualization_app/online_inference.py`
- Modify: `visualization_app/static/app.js`
- Modify: `visualization_app/test_online_inference_compat.py`
- Create: `visualization_app/test_model_selection_source_contract.py`
- Modify: `harness/tests/test_candidate_release_contract.py`

**Interfaces:**
- Produces: `ReleaseContract.managed_model_roots: tuple[str, ...]`.
- Produces: `managed_model_paths(contract: ReleaseContract) -> tuple[Path, ...]`.
- Produces: `classify_model_source(checkpoint: Path, managed_roots: Iterable[Path]) -> Literal["bundled", "external", "invalid"]`.
- Produces: model profile field `model_source` with `bundled` or `external`.
- Consumes: front-end `rememberModelPath(schemaMode, modelType, checkpoint, modelSource)` and server-provided `model_source`.

- [ ] **Step 1: Write failing model-provenance tests**

Add tests for an existing external path containing `models/new`, a sibling directory named `models-new`, case differences, `..` segments, a symlink/junction escaping a managed root, a real file inside each declared root, an absent file, and a candidate manifest missing `managed_model_roots`. Change the old missing-delivery-path test to require a diagnostic error rather than string-based relocation. Add a JavaScript contract test asserting `isBundledModelPath` is absent and history decisions use `model_source`.

- [ ] **Step 2: Run focused model tests and verify substring behavior fails**

Run: `python -m unittest visualization_app.test_online_inference_compat visualization_app.test_model_selection_source_contract harness.tests.test_candidate_release_contract -v`

Expected: FAIL because the release contract has no managed roots and Python/JavaScript classify by path substring.

- [ ] **Step 3: Add manifest model roots and canonical backend classification**

Add candidate-relative `managed_model_roots` to `managed_files`, validate non-empty normalized relative paths, resolve existing files and roots, and use `Path.resolve(strict=True)` plus `os.path.commonpath`/case normalization. Missing or unresolvable files are `invalid`; they are never upgraded to bundled by text. Return `model_source` in inspected profiles.

- [ ] **Step 4: Make the front end consume model provenance**

Remove `isBundledModelPath`. Store history as objects containing `checkpoint` and `model_source`; bundled selections are omitted, external selections are retained, and legacy string entries are treated as external inputs that the server must validate. Do not expose manifest filesystem roots to the browser.

- [ ] **Step 5: Run Task 6 verification**

Run: `python -m unittest visualization_app.test_online_inference_compat visualization_app.test_model_selection_source_contract harness.tests.test_candidate_release_contract -v`

Expected: PASS.

- [ ] **Step 6: Commit Task 6**

Run:

```powershell
git add pre_sensor/release-contract.json visualization_app/release_contract.py visualization_app/online_inference.py visualization_app/static/app.js visualization_app/test_online_inference_compat.py visualization_app/test_model_selection_source_contract.py harness/tests/test_candidate_release_contract.py
git commit -m "fix: classify bundled models by manifest roots"
```

### Task 7: Harness routing, current-state documentation, and whole-branch verification

**Files:**
- Modify: `harness/config/regression-matrix.json`
- Modify: `harness/config/profiles.json`
- Modify: `harness/engine/public_ingress.py`
- Modify: `harness/tests/test_public_ingress_contract.py`
- Modify: `harness/tests/test_pre_sensor_readiness_contract.py`
- Modify: `harness/tests/test_regression_matrix_contract.py`
- Modify: `harness/tests/test_manual_cli_contracts.py`
- Modify: `harness/README.md`
- Modify: `modular_runtime/README.md`
- Modify: `openspec/changes/complete-pre-sensor-readiness/tasks.md`

**Interfaces:**
- Consumes: production PowerShell/Python behavior from Tasks 1-6.
- Produces: mandatory requirement IDs `PRE_SENSOR_PUBLIC_INGRESS-001`, `PRE_SENSOR_INGRESS_GENERATION-001`, `PRE_SENSOR_INGRESS_CUTOVER-001`, `PRE_SENSOR_HELPER_GENERATION-001`, and `PRE_SENSOR_MODEL_PROVENANCE-001`.
- Produces: check `public-ingress-cutover-contracts` routed to `quick`, `full`, and `release`.

- [ ] **Step 1: Write failing Harness routing and anti-test-double assertions**

Require every ID above to be mandatory in all three profiles. Assert the check invokes tests that import/call `PublicIngress.psm1`, `public_ingress_state.py`, `HelperRegistry`, the deployment transaction and model classifier. Reject a matrix/check that relies only on `evaluate_public_ingress`, source keyword searches or a callback that bypasses production behavior.

- [ ] **Step 2: Run Harness contract tests and verify missing routing fails**

Run: `python -m unittest harness.tests.test_public_ingress_contract harness.tests.test_pre_sensor_readiness_contract harness.tests.test_regression_matrix_contract harness.tests.test_manual_cli_contracts -v`

Expected: FAIL until every requirement, check and command is routed.

- [ ] **Step 3: Update Harness adapter, matrix, profiles and current-state docs**

Keep `harness/engine/public_ingress.py` only as a report adapter over production result fields; remove test-only readiness decisions from it. Update READMEs with the current operator contract, exact non-mutating verification commands and the rule that deployment/restart/step 3 remain separately authorized. Do not add incident chronology, historical counts or phased development notes.

- [ ] **Step 4: Run focused Harness and OpenSpec verification**

Run: `python -m unittest discover -s harness/tests -v`

Expected: PASS.

Run: `openspec validate complete-pre-sensor-readiness --strict`

Expected: `Change 'complete-pre-sensor-readiness' is valid`.

- [ ] **Step 5: Run all product regression suites**

Run: `python -m unittest discover -s visualization_app -p "test_*.py"`

Expected: PASS with no failures or errors.

Run: `python -m unittest discover -s modular_runtime/tests -v`

Expected: PASS with no failures or errors.

Run: `git diff --check`

Expected: no output and exit code 0.

- [ ] **Step 6: Update OpenSpec task state and commit integration**

Mark only verified items complete in `tasks.md`. Stage exactly the Harness, README and OpenSpec task files listed above.

Run:

```powershell
git add harness/config/regression-matrix.json harness/config/profiles.json harness/engine/public_ingress.py harness/tests/test_public_ingress_contract.py harness/tests/test_pre_sensor_readiness_contract.py harness/tests/test_regression_matrix_contract.py harness/tests/test_manual_cli_contracts.py harness/README.md modular_runtime/README.md openspec/changes/complete-pre-sensor-readiness/tasks.md
git commit -m "test: enforce ingress generation readiness"
```

- [ ] **Step 7: Prepare whole-branch review and integration gate**

Generate a review package from `git merge-base origin/main HEAD` through `HEAD`. The reviewer must inspect the Review Focus items, every ledger ruling, secret exposure, fixed-runtime side effects and whether any test bypasses production behavior. Fix all Critical and Important findings with a failing regression test followed by the relevant green suite.

After review fixes, rerun the three full suites, OpenSpec strict, `git diff --check`, and a staged-file secret-pattern scan. Only a clean review and green gates permit the separately requested fast-forward merge to `main` and `git push origin main`.
