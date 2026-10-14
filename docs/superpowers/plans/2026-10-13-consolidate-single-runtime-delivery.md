# AFP 单一运行目录交付 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 AFP 主程序收敛为 `F:\new_AFP_exam\AFP_Integrated_System_Modular` 这一套固定可运行版本，并用临时 staging、受控审批、证据迁移和 Git 重建提供安全发布与回滚。

**Architecture:** 用版本受控 JSON 定义四个固定根目录，PowerShell 模块分别承担路径安全、发布门禁和运行时编排；现有候选构建只产出本次部署的临时 staging。首次迁移在“切换”和“清理”处设置独立审批门，任何验证失败都保留当前 D 盘程序或恢复它。

**Tech Stack:** PowerShell 5.1、Python 3.11 `unittest`、JSON 发布合同、Git/Git LFS、Windows CIM/注册表/计划任务、AFP EXE 自检接口。

**Spec:** `openspec/changes/consolidate-single-runtime-delivery/specs/single-runtime-delivery/spec.md` 与 `openspec/changes/consolidate-single-runtime-delivery/design.md`

## Global Constraints

- 生产工作区 MUST 固定为 `F:\new_AFP_exam`；源码、运行、证据和 staging 分别为 `AFP-Readiness-Repo`、`AFP_Integrated_System_Modular`、`AFP-Main-Evidence` 和 `.afp-staging`。
- 除本次部署的 staging 外，MUST NOT 创建或长期保留第二套完整可运行 AFP 目录。
- 不得修改已有候选包中的受管文件；新内容必须由源码重新装配到 staging。
- 停止 8770 监听者、替换运行目录、修改自启动、删除目录或改变公网配置前 MUST 输出准确目标、影响和回滚方法并等待明确授权。
- MySQL 密码只能通过本机安全提示输入；密码、Token、配对码、Tailscale 凭据和环境秘密不得进入聊天、命令行、脚本、配置或证据。
- Funnel 公网域名与 `127.0.0.1:8770` 源站保持不变；不得自动修改防火墙、Windows 服务或系统策略。
- 只有真实传感器检查可使用 `physical_sensor_pending`；其余状态只能是 `passed`、`failed` 或 `blocked`，模拟采集不得宣称为真实双机通过。
- 当前工作树已有其他已确认修改；实现时必须保留并避免覆盖 `fix-remote-acquisition-workflows` 与访客模拟文案相关改动。

## Review Focus

- 路径含 `..`、大小写差异、尾随分隔符或 reparse point 时，删除保护仍应基于最终绝对路径而不是字符串前缀；Task 1 测试固定该行为。
- inventory 后 PID、可执行路径、启动项值或监听者发生变化时，旧授权必须失效并重新请求；Task 3 测试固定该行为。
- staging 装配中断、磁盘不足或 EXE 自检失败时，当前运行目录必须保持字节不变且只清理本次 staging；Task 2 测试固定该行为。
- 工作树未提交、Git LFS 对象缺失或构建输入不可读取时，清理旧版本必须 blocked；Task 2 与 Task 5 测试固定该行为。
- UTF-8/UTF-16 日志、二进制文件及秘密扫描误报不得导致秘密值回显；原证据在任何不确定结果下均保留；Task 5 测试固定该行为。

---

### Task 1: 固定路径合同与安全解析

**Files:**
- Create: `pre_sensor/single-runtime-paths.json`
- Create: `pre_sensor/SingleRuntimePaths.psm1`
- Create: `harness/tests/test_single_runtime_paths.py`

**Interfaces:**
- Produces: `Get-AfpDeploymentPaths -ConfigPath <string> [-WorkspaceRootOverride <string>] -> PSCustomObject`
- Produces: `Resolve-AfpSafePath -Path <string> -WorkspaceRoot <string> -> string`
- Produces: `Assert-AfpMutableTarget -Path <string> -Paths <PSCustomObject> -Kind <Staging|Runtime|Evidence|Cleanup> -> string`
- Produces: `Test-AfpProtectedTarget -Path <string> -Paths <PSCustomObject> -> bool`

- [ ] **Step 1: 写失败测试 `test_production_paths_are_fixed_under_f_workspace`、`test_rejects_outside_parent_glob_and_reparse_targets`**

  断言 JSON 的四个绝对路径与 spec 完全一致；构造 `..`、父目录、通配符、大小写变化、尾随斜杠和测试 reparse point，断言危险目标返回非零且不被修改。

- [ ] **Step 2: 运行测试确认 RED**

  Run: `.\.venv\Scripts\python.exe -m unittest harness.tests.test_single_runtime_paths -v`
  Expected: FAIL，原因是合同和模块尚不存在。

- [ ] **Step 3: 实现路径合同和三个导出函数**

  使用 `[IO.Path]::GetFullPath()` 与 resolved provider path 比较；禁止未解析变量、通配符、workspace 根、三个保护根及其父目录。测试覆盖用 `WorkspaceRootOverride`，生产默认值不得从环境变量静默改变。

- [ ] **Step 4: 运行测试确认 GREEN**

  Run: `.\.venv\Scripts\python.exe -m unittest harness.tests.test_single_runtime_paths -v`
  Expected: PASS。

- [ ] **Step 5: 提交**

  ```powershell
  git add pre_sensor/single-runtime-paths.json pre_sensor/SingleRuntimePaths.psm1 harness/tests/test_single_runtime_paths.py
  git commit -m "feat: define single runtime delivery paths"
  ```

### Task 2: 临时 staging 与发布门禁

**Files:**
- Modify: `pre_sensor/build-candidate.ps1`
- Create: `pre_sensor/SingleRuntimeRelease.psm1`
- Modify: `harness/tests/test_candidate_release_contract.py`
- Create: `harness/tests/test_single_runtime_release.py`

**Interfaces:**
- Consumes: Task 1 的 `Get-AfpDeploymentPaths`、`Assert-AfpMutableTarget`
- Produces: `New-AfpStagedRelease -Paths <PSCustomObject> -ReferenceRelease <string> [-ExistingExecutable <string>] -> PSCustomObject`
- Produces: `Test-AfpReleaseGate -CandidatePath <string> [-ExpectedCandidateId <string>] -> PSCustomObject`，字段为 `status`, `candidate_id`, `source_revision`, `checks`, `issues`
- Produces: `Remove-AfpDeploymentStaging -StagingPath <string> -Paths <PSCustomObject> -> void`
- Produces: build 脚本参数 `-WorkspaceRoot`, `-KeepStagingOnSuccess`（仅测试/诊断显式使用），默认输出一个 JSON 元数据对象。

- [ ] **Step 1: 把旧候选测试改为新合同并新增失败清理测试**

  将 `artifacts\pre-sensor-candidates` 断言改为 `.afp-staging`；新增成功时返回 candidate ID 但不把它作为持久父目录、异常时仅删除本次 deployment ID、磁盘不足时不触碰 runtime 的断言。

- [ ] **Step 2: 运行定向测试确认 RED**

  Run: `.\.venv\Scripts\python.exe -m unittest harness.tests.test_candidate_release_contract harness.tests.test_single_runtime_release -v`
  Expected: FAIL，旧构建器仍写长期候选且缺少发布模块。

- [ ] **Step 3: 实现临时装配和门禁**

  `New-AfpStagedRelease` 复用现有 `assemble_modular_delivery.ps1`；`Test-AfpReleaseGate` 校验 manifest、VERSION、SHA256、Helper、协议/schema/帧合同，并依次执行三个 EXE 检查。所有输出只记录退出码与脱敏摘要。

- [ ] **Step 4: 增加 Git/LFS/构建输入与峰值磁盘检查**

  测试断言工作树无法重建、引用不存在、LFS 缺失、构建输入缺失或可用空间小于“当前运行大小 + staging 估算 + 安全余量”时返回 `blocked`。

- [ ] **Step 5: 运行定向测试确认 GREEN**

  Run: `.\.venv\Scripts\python.exe -m unittest harness.tests.test_candidate_release_contract harness.tests.test_single_runtime_release -v`
  Expected: PASS。

- [ ] **Step 6: 提交**

  ```powershell
  git add pre_sensor/build-candidate.ps1 pre_sensor/SingleRuntimeRelease.psm1 harness/tests/test_candidate_release_contract.py harness/tests/test_single_runtime_release.py
  git commit -m "feat: stage and verify releases temporarily"
  ```

### Task 3: Inventory、审批绑定与运行目录提升

**Files:**
- Create: `pre_sensor/SingleRuntimeDeployment.psm1`
- Create: `pre_sensor/manage-single-runtime-delivery.ps1`
- Create: `harness/tests/test_single_runtime_deployment.py`

**Interfaces:**
- Consumes: Task 1 路径对象与 Task 2 发布门禁结果
- Produces: `Get-AfpRuntimeInventory -Paths <PSCustomObject> -> PSCustomObject`
- Produces: `New-AfpApprovalSummary -Inventory <PSCustomObject> -Operation <Switchover|Startup|Cleanup> -> PSCustomObject`
- Produces: `Test-AfpInventoryUnchanged -Expected <PSCustomObject> -Current <PSCustomObject> -> bool`
- Produces: `Invoke-AfpRuntimePromotion -StagingPath <string> -Paths <PSCustomObject> -ApprovedInventoryDigest <string> -> PSCustomObject`
- Produces: CLI modes `Inventory`, `Stage`, `Switchover`, `StartupPreview`, `Evidence`, `CleanupPreview`, `Cleanup`, `Rollback`；所有变更模式必须接收 inventory digest。

- [ ] **Step 1: 写失败测试覆盖只读 inventory、审批摘要和状态漂移**

  断言 Inventory 模式不调用 Stop-Process、注册表写入或 Remove-Item；摘要包含 PID、路径、身份、影响、中断和回滚；PID/路径/启动值任一变化都使 digest 无效。

- [ ] **Step 2: 运行测试确认 RED**

  Run: `.\.venv\Scripts\python.exe -m unittest harness.tests.test_single_runtime_deployment -v`
  Expected: FAIL，编排模块与入口尚不存在。

- [ ] **Step 3: 实现 inventory 与审批摘要**

  inventory 读取 8770/8771 监听、Win32_Process 路径、HKCU Run、相关计划任务、Funnel 状态及运行 identity；对 URL 仅保留 scheme/host/port，对敏感字段只记录是否存在。

- [ ] **Step 4: 实现同卷完整提升和失败恢复**

  提升仅接受通过 Task 2 门禁且 inventory digest 未变化的 staging。目录交换使用固定运行目录旁的 deployment-id 临时名；异常时恢复原目录，并拒绝停止 inventory 未列出的进程。

- [ ] **Step 5: 运行测试确认 GREEN**

  Run: `.\.venv\Scripts\python.exe -m unittest harness.tests.test_single_runtime_deployment -v`
  Expected: PASS，包括 inventory 漂移、部分交换失败与恢复场景。

- [ ] **Step 6: 提交**

  ```powershell
  git add pre_sensor/SingleRuntimeDeployment.psm1 pre_sensor/manage-single-runtime-delivery.ps1 harness/tests/test_single_runtime_deployment.py
  git commit -m "feat: add approved single runtime promotion"
  ```

### Task 4: 服务探针与启动路径收敛

**Files:**
- Modify: `visualization_app/public_tunnel_watchdog.ps1`
- Modify: `visualization_app/tailscale_funnel_watchdog.ps1`
- Modify: `visualization_app/install_local_helper_autostart.ps1`
- Modify: `visualization_app/test_public_tunnel_watchdog.py`
- Modify: `visualization_app/test_tailscale_funnel_watchdog.py`
- Create: `harness/tests/test_single_runtime_startup_contract.py`
- Modify: `pre_sensor/SingleRuntimeDeployment.psm1`

**Interfaces:**
- Consumes: Task 1 固定运行路径与 Task 3 inventory
- Produces: `Test-AfpRuntimeServices -Paths <PSCustomObject> -PublicUrl <uri> -LanIPv4 <string> -> PSCustomObject`
- Produces: `Get-AfpStartupRepairPreview -Inventory <PSCustomObject> -Paths <PSCustomObject> -> PSCustomObject`
- Produces: `Set-AfpApprovedStartupTargets -PreviewDigest <string> -Paths <PSCustomObject> -> PSCustomObject`

- [ ] **Step 1: 写失败测试固定服务门禁与脚本路径行为**

  断言生产脚本不再包含 `F:\AFP_Integrated_Modular_v2` 或版本化 delivery 回退；服务结果必须分别包含 8770、8771、本机、LAN、公网、Funnel origin 和静态 candidate identity。

- [ ] **Step 2: 运行测试确认 RED**

  Run: `.\.venv\Scripts\python.exe -m unittest visualization_app.test_public_tunnel_watchdog visualization_app.test_tailscale_funnel_watchdog harness.tests.test_single_runtime_startup_contract -v`
  Expected: FAIL，现有脚本仍含旧回退路径。

- [ ] **Step 3: 修改 watchdog、Helper 安装器并实现服务探针**

  运行目录内脚本以 `$PSScriptRoot` 为根；源码调用从 Task 1 合同解析。保留 Funnel `https:443 -> http://127.0.0.1:8770`，不得执行域名或 Funnel 重配。

- [ ] **Step 4: 实现启动修复预览和受批准写入**

  预览包含注册表/任务旧值、新值与恢复值；只有 digest 与当前状态一致才写入。测试使用隔离 provider/mock，不接触正式 HKCU Run 或计划任务。

- [ ] **Step 5: 运行测试确认 GREEN 并提交**

  Run: `.\.venv\Scripts\python.exe -m unittest visualization_app.test_public_tunnel_watchdog visualization_app.test_tailscale_funnel_watchdog harness.tests.test_single_runtime_startup_contract -v`
  Expected: PASS。

  ```powershell
  git add visualization_app/public_tunnel_watchdog.ps1 visualization_app/tailscale_funnel_watchdog.ps1 visualization_app/install_local_helper_autostart.ps1 visualization_app/test_public_tunnel_watchdog.py visualization_app/test_tailscale_funnel_watchdog.py harness/tests/test_single_runtime_startup_contract.py pre_sensor/SingleRuntimeDeployment.psm1
  git commit -m "fix: point AFP startup at fixed runtime"
  ```

### Task 5: 证据迁移、秘密扫描与精确清理

**Files:**
- Create: `pre_sensor/SingleRuntimeEvidence.psm1`
- Create: `harness/tests/test_single_runtime_evidence.py`
- Modify: `pre_sensor/manage-single-runtime-delivery.ps1`

**Interfaces:**
- Consumes: Task 1 路径保护与 Task 3 inventory digest
- Produces: `Copy-AfpEvidenceSafely -Source <string> -Destination <string> -Paths <PSCustomObject> -> PSCustomObject`
- Produces: `Get-AfpCleanupPreview -Targets <string[]> -Paths <PSCustomObject> -Inventory <PSCustomObject> -> PSCustomObject`
- Produces: `Remove-AfpApprovedTargets -PreviewDigest <string> -Paths <PSCustomObject> -> PSCustomObject`
- Produces: `Write-AfpSingleRuntimeReport -State <PSCustomObject> -OutputPath <string> -> string`

- [ ] **Step 1: 写失败测试覆盖复制校验、编码、秘密与删除边界**

  夹具包括 UTF-8、UTF-16、二进制、摘要不一致、疑似密码/Token、父目录、glob、活动进程引用、启动项引用和 inventory 漂移；断言失败输出不含匹配秘密值。

- [ ] **Step 2: 运行测试确认 RED**

  Run: `.\.venv\Scripts\python.exe -m unittest harness.tests.test_single_runtime_evidence -v`
  Expected: FAIL，证据模块尚不存在。

- [ ] **Step 3: 实现复制 manifest 与秘密扫描**

  manifest 记录相对路径、大小、SHA-256、来源和 UTC 时间；扫描器只输出规则 ID 和文件相对路径。无法可靠读取或判定的文件将整个迁移标记为 failed 并保留来源。

- [ ] **Step 4: 实现清理预览、审批绑定和最终报告**

  删除函数只消费已批准 preview 中的逐项绝对路径；每项删除前重新检查保护根、活动引用和 digest。最终报告使用 `passed|failed|blocked|physical_sensor_pending` 的既有语义，并分别列出模拟与真实状态。

- [ ] **Step 5: 运行测试确认 GREEN 并提交**

  Run: `.\.venv\Scripts\python.exe -m unittest harness.tests.test_single_runtime_evidence -v`
  Expected: PASS。

  ```powershell
  git add pre_sensor/SingleRuntimeEvidence.psm1 pre_sensor/manage-single-runtime-delivery.ps1 harness/tests/test_single_runtime_evidence.py
  git commit -m "feat: migrate evidence and clean exact targets safely"
  ```

### Task 6: 文档、活动路径配置与完整回归

**Files:**
- Modify: `pre_sensor/FIELD-OPERATIONS.md`
- Modify: `README.md`
- Modify: `harness/config/delivery-readonly-baseline.json`
- Modify: `harness/config/local-settings.example.json`
- Modify: `harness/config/regression-matrix.json`
- Modify: `harness/tests/test_delivery_readonly_baseline.py`
- Modify: `harness/tests/test_regression_matrix_contract.py`
- Create: `harness/tests/test_single_runtime_documentation.py`

**Interfaces:**
- Consumes: Tasks 1–5 的 CLI、固定路径和状态字段
- Produces: 面向现场操作员的固定目录发布、两段审批、失败恢复和 Git 重建回滚说明

- [ ] **Step 1: 写失败的文档与配置合同测试**

  断言活动文档不再要求长期保存 `artifacts/pre-sensor-candidates/<candidate-id>`，配置指向固定运行目录，并说明 source/evidence 不算第二个运行版本。

- [ ] **Step 2: 运行测试确认 RED**

  Run: `.\.venv\Scripts\python.exe -m unittest harness.tests.test_single_runtime_documentation harness.tests.test_delivery_readonly_baseline harness.tests.test_regression_matrix_contract -v`
  Expected: FAIL，现有文档和配置仍引用版本化 delivery/候选目录。

- [ ] **Step 3: 更新文档和活动配置**

  保留历史设计文档中的历史路径，不进行无意义全仓替换；只修改当前操作入口、回归矩阵和默认配置。明确真实传感器、Helper blocked 与模拟验证边界。

- [ ] **Step 4: 运行定向与完整回归**

  Run: `.\.venv\Scripts\python.exe -m unittest harness.tests.test_single_runtime_documentation harness.tests.test_delivery_readonly_baseline harness.tests.test_regression_matrix_contract -v`
  Expected: PASS。

  Run: `.\.venv\Scripts\python.exe -m unittest discover -s . -p "test_*.py"`
  Expected: 全部 PASS；若第三方环境缺失，必须区分依赖错误与产品回归，不得把不完整运行表述为通过。

- [ ] **Step 5: 校验 OpenSpec 并提交**

  Run: `openspec validate consolidate-single-runtime-delivery --strict --json`
  Expected: `valid: true` 且 issues 为空。

  ```powershell
  git add pre_sensor/FIELD-OPERATIONS.md README.md harness/config/delivery-readonly-baseline.json harness/config/local-settings.example.json harness/config/regression-matrix.json harness/tests/test_delivery_readonly_baseline.py harness/tests/test_regression_matrix_contract.py harness/tests/test_single_runtime_documentation.py openspec/changes/consolidate-single-runtime-delivery docs/superpowers/plans/2026-10-13-consolidate-single-runtime-delivery.md
  git commit -m "docs: specify single runtime delivery workflow"
  ```

### Task 7: 只读迁移演练与切换审批检查点

**Files:**
- Create at runtime: `F:\new_AFP_exam\AFP-Main-Evidence\SINGLE-RUNTIME-MIGRATION\inventory.json`
- Create at runtime: `F:\new_AFP_exam\AFP-Main-Evidence\SINGLE-RUNTIME-MIGRATION\switchover-approval.txt`

**Interfaces:**
- Consumes: Task 3 CLI 的 `Inventory`、Task 2 的 `Stage`、Task 4 服务配置
- Produces: `MAIN_SWITCHOVER_APPROVAL_REQUIRED` 检查点；此任务结束时仍不得切换

- [ ] **Step 1: 运行只读 inventory 并审查输出**

  Run: `.\pre_sensor\manage-single-runtime-delivery.ps1 -Mode Inventory`
  Expected: 当前 8770/8771、PID、D 盘可执行路径、身份、公网 URL（仅 scheme/host/port）、Funnel origin、启动项和目录大小写入固定证据目录；不发生状态改变。

- [ ] **Step 2: 在 staging 装配并运行全部发布门禁**

  Run: `.\pre_sensor\manage-single-runtime-delivery.ps1 -Mode Stage`
  Expected: 返回 `verified`，三个 EXE 检查均通过；失败时当前 D 盘 PID 与监听保持不变且 staging 被清理。

- [ ] **Step 3: 输出并核对切换审批文本**

  必须准确列出当前 PID、当前路径、当前 identity、目标 identity、公网 URL、Funnel origin、回滚启动命令、预计中断和证据路径；不得含秘密。

- [ ] **Step 4: 停止并等待用户明确授权**

  未获得新的明确授权前不得运行 `-Mode Switchover`，不得改自启动，不得删除任何目录。

### Task 8: 首次切换、证据迁移与最终清理（分段授权）

**Files:**
- Modify at runtime: `F:\new_AFP_exam\AFP_Integrated_System_Modular\**`
- Modify at runtime after approval: `HKCU:\Software\Microsoft\Windows\CurrentVersion\Run\AFP_Tailscale_Funnel_Watchdog`
- Create at runtime: `F:\new_AFP_exam\AFP-Main-Evidence\SINGLE-RUNTIME-MIGRATION\final-report.json`

**Interfaces:**
- Consumes: Task 7 的 inventory digest 与用户切换授权
- Produces: 固定 F 盘运行版本、服务验证结果、第二个清理审批检查点、最终单一版本报告

- [ ] **Step 1: 切换前重新验证 inventory digest**

  Expected: PID、路径、监听者、启动项或候选身份任一变化都返回 `approval_required` 并停止。

- [ ] **Step 2: 获得切换授权后执行 `Switchover` 并验证服务**

  仅停止已批准旧 AFP PID；确认 8770 来自固定 F 盘 EXE、8771 只绑定 loopback、本机/LAN/公网均可访问、Funnel origin 为 `127.0.0.1:8770`、静态资源属于新 identity。失败时恢复 D 盘旧程序并保留目录。

- [ ] **Step 3: 为启动项修改单独输出预览并等待授权**

  Expected: 预览列出准确注册表值的旧值、新值和恢复值。授权后只更新该值并复核，不修改防火墙、Funnel 配置、Windows 服务或系统策略。

- [ ] **Step 4: 迁移并校验证据**

  把 `D:\AFP-Main-Evidence\CROSS-20261013-01` 复制到固定证据根；只有清单、SHA-256 和秘密扫描全部通过才把原副本列入清理预览。

- [ ] **Step 5: 输出逐项清理预览并等待新的明确授权**

  预览必须重新解析并列出 design 中六个现有目标、大小、活动引用、预计释放空间和 Git 重建回滚条件。未授权时全部保留。

- [ ] **Step 6: 获得清理授权后逐项删除并复核**

  只删除预览 digest 未变化且通过保护检查的目标；不得删除源码、固定运行目录、固定证据目录或它们的父目录。记录每项是否删除及释放空间。

- [ ] **Step 7: 最终验证**

  重新运行固定 EXE 的 `--verify-files`、`--self-test`、`--module-status` 和服务门禁；确认 staging 为空、没有活动启动项引用旧路径、本机仅剩一个受支持的完整运行目录。

- [ ] **Step 8: 保存结论但不越权宣布双机完成**

  最终报告分别记录 pairing、remote_helper、模拟采集、真实传感器和跨机步骤的 `passed|failed|blocked|physical_sensor_pending` 状态，并注明最终结论由保存 OpenSpec 仓库的验证电脑结合两端证据作出。
