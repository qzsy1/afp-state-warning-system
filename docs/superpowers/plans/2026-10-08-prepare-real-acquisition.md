# 真实采集所需准备 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不直接修改现有 delivery 的前提下，从权威源完成真实采集第一阶段加固，并用新增Harness与既有回归证明协议、readiness、持久化、逐帧质量、模型频率和恢复行为符合OpenSpec。

**Architecture:** `visualization_app/` 是产品事实来源，`modular_runtime/` 只负责从事实来源装配候选包，`harness/` 负责失败关闭的自动检查。实现按五个可独立验收的执行包推进：源与Harness、协议与readiness、持久化与远程帧、模型安全、运行恢复与总回归；所有对delivery的操作仅为只读摘要和临时目录装配对比。

**Tech Stack:** Python 3标准库、`unittest`、PowerShell、JSON、现有OpenSpec CLI、现有前端JavaScript测试入口。

**Spec:** `openspec/changes/prepare-real-acquisition/design.md` 及 `openspec/changes/prepare-real-acquisition/specs/` 下三个能力规格。

## Global Constraints

- 禁止直接编辑、覆盖或重新生成 `delivery/AFP_Integrated_System_Modular_v2.0.4_Agentic`；隔离装配只能写入临时目录。
- 保留工作区已有未提交修改；禁止 `git reset --hard`、`git checkout --`、批量目录同步和未经确认的提交。
- 产品行为只改 `visualization_app/`，装配只改 `modular_runtime/`，Harness只改 `harness/`。
- 新增字段必须向后兼容；仿真流程、现有10 Hz正常采集、公开API和前端既有字段必须继续工作。
- 每个行为变更必须先运行新增测试并观察预期失败，再实现最小代码使其通过。
- 自动化结果只能标记为 `automated` 或 `protocol_simulation`；G1–G5保持 `field_pending`。
- 每完成一个执行包，立即更新 `openspec/changes/prepare-real-acquisition/tasks.md` 中对应复选框。
- 当前共享工作区不创建提交；交付时仅报告本次文件清单、差异和验证证据，避免把用户既有修改混入提交。

## Review Focus

- Modbus响应的MBAP长度包含Unit ID；连续两帧或分片到达不得造成多读、少读或把下一帧吞入当前帧，Task 3覆盖。
- 隔离硬件检查成功后，主helper代理必须拥有同一配置的readiness快照；修改配置、过期或身份变化必须拒绝，Task 4覆盖。
- 正式真实模式保存根不可写时不得创建驱动或采集线程；显式工程预览必须持续禁用生产结论，Task 5覆盖。
- 有限但陈旧的值、帧序号缺口和远程重传不得进入有效模型窗口或产生重复帧，Tasks 6–7覆盖。
- 慢磁盘、一次性驱动异常、阻塞关闭或数据库不可用必须保持其他设备可观察且停止状态不谎报完成，Task 8覆盖。

---

## 执行包一：源与Harness

### Task 1: 真实采集准备矩阵与delivery只读保护

**Files:**
- Create: `harness/engine/real_acquisition_preflight.py`
- Create: `harness/tests/test_real_acquisition_preflight.py`
- Modify: `harness/config/regression-matrix.json`
- Modify: `harness/tests/test_regression_matrix_contract.py`

**Interfaces:**
- Produces: `DeliverySnapshot(root: Path, files: tuple[str, ...], sha256: dict[str, str])`。
- Produces: `capture_delivery_snapshot(delivery_root: Path) -> DeliverySnapshot`。
- Produces: `assert_delivery_unchanged(before: DeliverySnapshot, after: DeliverySnapshot) -> None`。
- Requirement families: `REAL_PREP_SOURCE-001`、`REAL_PREP_PROTOCOL-001`、`REAL_PREP_TRANSPORT-001`、`REAL_PREP_PERSISTENCE-001`、`REAL_PREP_MODEL-001`、`REAL_PREP_RECOVERY-001`。

- [ ] **Step 1: 写失败测试**：新增 `test_prepare_real_acquisition_requirements_have_blocking_checks`、`test_delivery_snapshot_detects_changed_managed_file`、`test_missing_field_evidence_remains_pending`，用字面量断言需求ID、profile、证据等级及摘要差异。
- [ ] **Step 2: 验证RED**：运行 `python -m unittest harness.tests.test_real_acquisition_preflight harness.tests.test_regression_matrix_contract -v`，确认因模块/矩阵项缺失而失败。
- [ ] **Step 3: 最小实现**：实现流式SHA-256快照与差异报告，在矩阵增加六个需求族及后续任务使用的检查占位；现场项只引用 `field` 检查。
- [ ] **Step 4: 验证GREEN**：重跑上述命令，要求零失败；标记OpenSpec 1.1、1.2、1.4完成。

### Task 2: 隔离装配与权威源一致性

**Files:**
- Modify: `harness/engine/real_acquisition_preflight.py`
- Modify: `harness/tests/test_real_acquisition_preflight.py`
- Modify: `modular_runtime/assemble_modular_delivery.ps1`
- Modify: `modular_runtime/tests/test_modular_runtime.py`
- Modify: `visualization_app/acquisition.py`
- Create: `visualization_app/real_acquisition.py`

**Interfaces:**
- Produces: `AssemblyResult(command: tuple[str, ...], returncode: int, target: Path, stdout: str, stderr: str)`。
- Produces: `run_isolated_assembly(repo_root: Path, reference_release: Path, launcher: Path, target_parent: Path) -> AssemblyResult`。
- Produces: `validate_candidate_runtime(candidate_root: Path) -> tuple[str, ...]`，返回问题列表，空元组表示通过。
- PowerShell现有参数保持：`ReferenceRelease`、`TargetDir`、`ApplicationVersion`、`ExistingExecutable`/`LauncherSourceDir`。

- [ ] **Step 1: 写失败测试**：增加 `test_source_inventory_reports_delivery_only_runtime`、`test_isolated_assembly_never_targets_official_delivery`、`test_candidate_imports_acquisition_without_missing_module` 和 `test_candidate_manifest_matches_managed_files`。
- [ ] **Step 2: 验证RED**：运行 `python -m unittest harness.tests.test_real_acquisition_preflight modular_runtime.tests.test_modular_runtime -v`，确认当前源/候选漂移被检测。
- [ ] **Step 3: 最小实现**：在权威源创建 `real_acquisition.py`，迁移并测试 `ChannelQuality`、`DriverWorker`、`FrameAssembler` 等确定性采集核心，再让源 `acquisition.py` 使用这些边界并更新装配显式清单；不得从delivery批量覆盖其他源文件，不得写正式delivery。
- [ ] **Step 4: 验证GREEN**：用临时目录运行候选装配、导入和manifest校验，并断言正式delivery快照前后一致。
- [ ] **Step 5: 回归**：运行 `python -m unittest discover -s modular_runtime/tests -v` 与 `python -m unittest discover -s harness/tests -v`；标记OpenSpec 1.3、2.1–2.5完成。

## 执行包二：协议与readiness

### Task 3: 严格Modbus TCP响应解析

**Files:**
- Modify: `visualization_app/acquisition.py`
- Create: `visualization_app/test_real_acquisition_preparation.py`

**Interfaces:**
- Keeps: `ModbusTcpDriver._read_holding_registers(start_address: int, quantity: int) -> list[int]`。
- Adds focused helper if extraction improves testability: `_parse_modbus_read_response(header: bytes, pdu: bytes, *, transaction_id: int, unit_id: int, quantity: int) -> list[int]`。
- Protocol rule: 7-byte MBAP已包含Unit ID，后续读取长度严格为 `length - 1`。

- [ ] **Step 1: 写失败测试**：加入 `test_modbus_fc03_reads_only_length_minus_unit`、`test_modbus_fragmented_response_stops_at_frame_boundary`、`test_modbus_rejects_transaction_protocol_unit_and_quantity_mismatch`、`test_modbus_rejects_short_or_exception_response`。
- [ ] **Step 2: 验证RED**：运行 `python -m unittest test_real_acquisition_preparation.ModbusTcpContractTests -v`（cwd=`visualization_app`），确认现实现多读或未校验导致预期失败。
- [ ] **Step 3: 最小实现**：严格读取与验证完整响应；异常不得返回部分寄存器。
- [ ] **Step 4: 验证GREEN**：重跑测试类及 `python -m unittest test_acquisition_integrity -v`；标记OpenSpec 3.1–3.4完成。

### Task 4: readiness快照跨进程传递及helper采样率

**Files:**
- Modify: `visualization_app/acquisition.py`
- Modify: `visualization_app/local_capture_agent.py`
- Modify: `visualization_app/local_capture_helper_entry.py`
- Modify: `visualization_app/test_helper_transport.py`
- Modify: `visualization_app/test_local_capture_agent.py`

**Interfaces:**
- Produces: `AcquisitionManager.import_readiness_snapshot(config: AcquisitionConfig, snapshot: Mapping[str, Any]) -> dict[str, Any]`。
- Produces: `LocalCaptureAgent.import_readiness_snapshot(config: Any, snapshot: Mapping[str, Any]) -> dict[str, Any]`。
- Snapshot fields: `schema_version`、`checked_at`、`expires_at`、`config_fingerprint`、`device_identity`、`ok`、`interfaces`、`sensors`、`configuration_issues`；禁止密码和token。
- `HelperCommandDispatcher.submit()` wraps the hardware-check callback: import valid snapshots before forwarding the response.
- `LocalCaptureAgent.stream_metrics()` reads `sample_rate_hz`, falling back to legacy `sample_rate` only when the new field is absent.

- [ ] **Step 1: 写失败测试**：增加 `test_isolated_check_snapshot_allows_same_agent_start`、`test_changed_or_expired_snapshot_is_rejected`、`test_snapshot_round_trip_excludes_secrets`、`test_websocket_and_http_share_snapshot_semantics`、`test_stream_metrics_uses_fifty_hz_config`。
- [ ] **Step 2: 验证RED**：运行 `python -m unittest test_helper_transport test_local_capture_agent -v`，确认主代理没有快照及50 Hz指标错误。
- [ ] **Step 3: 最小实现**：增加导入边界、callback包装和字段兼容；启动仍由采集管理器重新验证指纹、身份和有效期。
- [ ] **Step 4: 验证GREEN**：重跑上述测试，再运行 `python -m unittest test_helper_websocket test_helper_relay test_simulation_source_transfer -v`；标记OpenSpec 4.1–4.6完成。

## 执行包三：持久化与远程帧

### Task 5: 正式持久化门禁与工程预览

**Files:**
- Modify: `visualization_app/acquisition.py`
- Modify: `visualization_app/app.py`
- Modify: `visualization_app/test_acquisition_integrity.py`
- Modify: `visualization_app/test_app.py`

**Interfaces:**
- Adds `AcquisitionConfig.persistence_policy: str` with normalized values `required_local`、`engineering_memory`、`simulation_compatible`。
- Adds `preflight_capture_persistence(config: AcquisitionConfig) -> dict[str, Any]`。
- Status fields: `persistence_policy`、`save_required`、`save_status`、`memory_retention_rows`、`production_conclusion_enabled`。

- [ ] **Step 1: 写失败测试**：增加正式真实模式空/不存在/不可写/无法建会话目录时拒绝启动且 `build_driver` 未调用；增加工程内存模式可启动但生产结论为false；保留现有 `test_empty_save_root_keeps_simulation_in_memory_without_files`。
- [ ] **Step 2: 验证RED**：运行 `python -m unittest test_acquisition_integrity.AcquisitionIntegrityTests -v`，确认真实模式仍错误进入内存采集。
- [ ] **Step 3: 最小实现**：在驱动构建前执行门禁；工程预览显式化；模拟旧调用走 `simulation_compatible`。
- [ ] **Step 4: 验证GREEN**：运行 `python -m unittest test_acquisition_integrity test_process_parameters test_server_capture_journal test_mysql_credentials test_mysql_identity -v`；标记OpenSpec 5.1–5.5完成。

### Task 6: 不可变帧包络、helper传输和服务器幂等

**Files:**
- Create: `visualization_app/frame_envelope.py`
- Create: `visualization_app/test_frame_envelope.py`
- Modify: `visualization_app/acquisition.py`
- Modify: `visualization_app/local_capture_agent.py`
- Modify: `visualization_app/edge_capture.py`
- Modify: `visualization_app/server_capture_journal.py`
- Modify: `visualization_app/test_local_capture_agent.py`
- Modify: `visualization_app/test_server_capture_journal.py`
- Modify: `visualization_app/test_helper_websocket.py`

**Interfaces:**
- Produces immutable `FrameEnvelope` with `capture_uuid`、`frame_sequence`、`frame_time`、`values`、`channels`。
- Produces `FrameEnvelope.to_wire() -> dict[str, Any]` and `FrameEnvelope.from_wire(payload: Mapping[str, Any]) -> FrameEnvelope`。
- `AcquisitionManager.stream_rows_since()` adds `frames` while retaining `rows`、`timestamps`、cursor fields.
- `LocalCaptureAgent.next_sample_batch()` adds `frames` and capability `frame_envelope_v1`.
- `RemoteAcquisitionMirror.ingest()` and `ServerCaptureJournal.ingest()` use `(capture_uuid, frame_sequence)` for idempotency and conflict detection.

- [ ] **Step 1: 写失败测试**：覆盖包络往返、绝对序号在deque截断后保持、批次重放不重复、同键不同内容冲突、序号缺口、旧helper正式拒绝和工程降级。
- [ ] **Step 2: 验证RED**：运行 `python -m unittest test_frame_envelope test_local_capture_agent test_server_capture_journal test_helper_websocket -v`，确认当前只传rows/timestamps。
- [ ] **Step 3: 最小实现**：增加包络序列和兼容字段；不得移除旧消费者依赖的rows/timestamps。
- [ ] **Step 4: 验证GREEN**：重跑上述命令并运行 `python -m unittest test_edge_capture test_websocket_live -v`。
- [ ] **Step 5: 对账回归**：增加本地/远程序号、时间、质量对账用例，标记OpenSpec 6.1–6.6完成。

## 执行包四：模型安全

### Task 7: 模型采样契约、因果重采样和审批权限

**Files:**
- Create: `visualization_app/model_input_safety.py`
- Create: `visualization_app/test_model_input_safety.py`
- Create: `visualization_app/prediction_approval.py`
- Modify: `visualization_app/online_inference.py`
- Modify: `visualization_app/prediction_model_metadata.example.json`
- Modify: `visualization_app/app.py`
- Modify: `visualization_app/test_online_inference_compat.py`
- Modify: `visualization_app/test_app.py`
- Modify: `visualization_app/static/app.js`
- Modify: `visualization_app/test_frontend_guest_simulation.py`

**Interfaces:**
- Produces `ModelSamplingContract(schema_version: int, sampling_hz: float, seq_len: int, pred_len: int, units: dict[str, str], resample_policy: str)`。
- Produces `WindowEligibility(ok: bool, reason: str, start_sequence: int | None, end_sequence: int | None)`。
- Produces `build_model_points(frames: Sequence[FrameEnvelope], contract: ModelSamplingContract) -> tuple[list[dict[str, float]], WindowEligibility]`。
- Produces `evaluate_prediction_approval(model_path: Path, metadata: Mapping[str, Any], approval: Mapping[str, Any] | None) -> dict[str, Any]` with modes `available_shadow`/`approved_production`.
- 50→10 Hz policy identifier: `causal_mean_5x_v1`; windows are right-closed and never read future samples.

- [ ] **Step 1: 写失败测试**：元数据缺少Schema/单位/频率/重采样策略必须失败；50 Hz手算序列输出10 Hz点且24点覆盖2.4秒；10 Hz直接映射；陈旧有限值、缺口和断线均不可判定。
- [ ] **Step 2: 验证RED**：运行 `python -m unittest test_model_input_safety test_online_inference_compat -v`，确认当前缺少合同与重采样器。
- [ ] **Step 3: 最小实现频率与质量边界**：实现合同、因果重采样及统一窗口资格；实时与历史预警调用同一入口。
- [ ] **Step 4: 写审批失败测试**：默认影子、审批摘要匹配才可生产、模型/元数据变化使批准失效；API与前端必须显示“不可用于停机、放行或判废”。
- [ ] **Step 5: 实现审批和状态投影**：不生成生产批准，只验证外部批准证据；旧前端字段保留。
- [ ] **Step 6: 验证GREEN与回归**：运行 `python -m unittest test_model_input_safety test_online_inference_compat test_app test_new_collection_health test_simulation_replay test_frontend_guest_simulation -v`；标记OpenSpec 7.1–7.8完成。

## 执行包五：恢复与总回归

### Task 8: 驱动恢复、轮询下限、异步写入和停止状态机

**Files:**
- Create: `visualization_app/async_capture_writer.py`
- Create: `visualization_app/test_capture_runtime_recovery.py`
- Modify: `visualization_app/acquisition.py`
- Modify: `visualization_app/test_acquisition_integrity.py`

**Interfaces:**
- Device policy fields: `min_poll_interval_seconds`、`read_timeout_seconds`、`failure_threshold`、`retry_backoff_initial_seconds`、`retry_backoff_max_seconds`。
- Produces `AsyncCaptureWriter(max_frames: int, high_watermark: int, overflow_policy: str)` with `start()`、`submit(frame)`、`drain(timeout)`、`close(timeout)`、`status()`。
- Stop phases: `stopping_sampling`、`draining_writer`、`finalizing_files`、`syncing_database`、`reconciling`、`completed`、`failed`。

- [ ] **Step 1: 写驱动RED测试**：一次可恢复异常后重开、持续失败达到上限、其他接口继续、PLC/ABB低于档案下限时被钳制、启动等待opened/首帧。
- [ ] **Step 2: 实现驱动策略**：有限超时、指数退避上限、状态计数和有界停止；验证驱动测试变绿。
- [ ] **Step 3: 写写入/停止RED测试**：慢写入不阻塞组帧、高水位可见、正式溢出失败关闭、数据库失败保留本地完成与待重试、阻塞驱动使整体未完成。
- [ ] **Step 4: 实现异步写入和停止阶段**：移除采集线程逐帧flush，终结阶段分别计时并保留错误。
- [ ] **Step 5: 验证GREEN与回归**：运行 `python -m unittest test_capture_runtime_recovery test_acquisition_integrity test_process_parameters test_server_capture_journal -v`，再运行已有确定性采样测试；标记OpenSpec 8.1–8.7完成。

### Task 9: Harness接线、quick/full和OpenSpec证据

**Files:**
- Modify: `harness/config/regression-matrix.json`
- Modify: `harness/tests/test_regression_matrix_contract.py`
- Create: `verification/real-acquisition-preparation-report.md`
- Modify: `openspec/changes/prepare-real-acquisition/tasks.md`

**Interfaces:**
- New check IDs map Tasks 1–8 tests to quick/full/release; full includes existing core regression checks.
- Preparation report records Git revision, dirty status, commands, exit codes, delivery before/after digest, automated evidence, protocol-simulation evidence and G1–G5 `field_pending` rows.

- [ ] **Step 1: 矩阵合同**：为每个新需求族加入至少一个blocking自动检查或明确field项，运行 `python -m unittest harness.tests.test_regression_matrix_contract -v`。
- [ ] **Step 2: 新增Harness全量**：运行所有新增协议、transport、persistence、model、recovery和隔离装配检查，要求正式delivery摘要前后一致。
- [ ] **Step 3: quick**：运行仓库现有Harness quick入口，记录全部检查ID、耗时和退出码；修复本变更引起的失败。
- [ ] **Step 4: full**：运行仓库现有Harness full入口，记录全部检查ID、耗时和退出码；不得隐藏既有失败。
- [ ] **Step 5: 静态门禁**：运行 `openspec validate prepare-real-acquisition --strict`、`python -m compileall`（仅受影响模块）和 `git diff --check`（仅本次文件）。
- [ ] **Step 6: 只读边界**：再次捕获正式delivery摘要并与Task 1基线比较；任何变化都使交付失败。
- [ ] **Step 7: 报告与任务状态**：生成准备报告，只勾选有新鲜验证证据的OpenSpec任务；现场项保持pending。

### Task 10: 独立代码审查与修正

**Files:**
- Review all files changed by Tasks 1–9; no delivery files are eligible for modification.

**Interfaces:**
- Reviewer receives OpenSpec paths、计划路径、变更前SHA、当前SHA和工作区diff；输出Critical/Important/Minor问题。

- [ ] **Step 1: 请求独立审查**：重点检查delivery只读、Modbus边界、快照安全、兼容字段、50→10因果性和停止一致性。
- [ ] **Step 2: 处理审查**：修复全部Critical和Important问题；每个修复先增加或强化会失败的测试。
- [ ] **Step 3: 最终验证**：重新运行受影响测试、quick、full、OpenSpec严格校验和delivery摘要比较。
- [ ] **Step 4: 交付**：报告OpenSpec完成度、测试精确计数、未验证现场项、已知限制和所有本次修改文件。
