# 真实传感器接入前准备收口验证报告

## 结论

仓库内实现、隔离候选装配、本机协议模拟、双模式 30 分钟墙钟耐久和可在第二台电脑直接执行的操作包已经完成。OpenSpec 任务为 **60/60 完成，剩余 0**。

当前**不得声明全部传感器前准备已经通过**：目标 MySQL、第二台 Windows 电脑及当前主机的管理员级 Windows 设置仍缺少外部环境或授权；`D:\AFP_Capture` 还存在 1 个历史目标 MySQL 待重试记录。上述项目均为传感器无关的环境阻塞，不在“仅剩真实设备验证清单”中。

## OpenSpec实现一致性复核

| 维度 | 状态 |
|---|---|
| 完整性 | 60/60 任务完成；27/27 条 ADDED requirements 有实现证据 |
| 正确性 | 27/27 requirements、57/57 scenarios 具有代码、自动测试、实际演练或明确 `blocked` 环境证据 |
| 一致性 | design 的 8 项决策均得到遵循；未重复实现双模式核心，候选/证据可搬运，正式 delivery 只读 |

- CRITICAL：0。
- WARNING：0。
- SUGGESTION：0。
- 评估：OpenSpec 实现检查全部通过，可在用户决定后归档；本报告没有执行归档。这里的“可归档”仅表示实现与变更文档一致，不会把下述外部环境阻塞升级为运行环境通过。

## 分支、基线与正式发行保护

- 起始 `main`：`2047f358808c50ba8c5f85a036a100e6f738e6eb`，与 `codex/prepare-real-acquisition` 指向同一提交，证明 main 已包含真实采集加固。
- 实施分支：`codex/complete-pre-sensor-readiness`。
- 独立工作树：`D:\AFP_Integrated_Modular_v2\.worktrees\complete-pre-sensor-readiness`。
- 正式 `delivery/` 前后均为 6176 个受管文件，树摘要均为 `8fd1ab9709215101075bdea283b40895d52d107a389eea42e1683d0e0a6cc4d4`。
- `python -m harness.engine.delivery_readonly`：通过；正式 `delivery/` 未修改。

## 最终候选与全新目录演练

- 候选：`2.0.5-rc.1-3a56a28d1a2e-20261010T144936Z`。
- 源提交：`3a56a28d1a2e`；候选独立位于 `artifacts/pre-sensor-candidates/`，未覆盖正式发行。
- 候选规模：6225 个文件、1,079,078,053 bytes；`SHA256SUMS.txt` 6190 项。
- 发行身份：应用、Helper、启动器均为 `2.0.5-rc.1`；协议 2、统一帧 `unified_frame_v1`、配置/readiness schema 均为 1。
- 候选验证：包内导入、manifest、8 项静态资源、3 项配置资产、native DLL/SDK/驱动声明均通过；`--self-test`、`--module-status`、`--verify-files` 返回码均为 0。
- 从全新短路径 `D:\AFP-Drill-3a56a28` 按操作员顺序执行：双模式预检、双模式 1 秒采集、停止/对账和脱敏证据导出均可运行；两个耐久 wrapper 返回 0，证据导出返回 0 且 `sanitized=true`。
- 候选预检正确失败关闭：`local_direct` 为 7 passed / 1 failed / 1 blocked / 1 physical pending；`remote_helper` 为 6 passed / 1 failed / 2 blocked / 1 physical pending。失败为当前 Windows 设置，阻塞为目标 MySQL及第二台电脑，唯一 `physical_sensor_pending` 为实物身份/端口。

## RED→GREEN 与自动验证

逐项首次失败和修复命令记录在 `evidence/red-green.md`。最终结果如下：

- OpenSpec：`openspec validate complete-pre-sensor-readiness --strict` 通过。
- Harness 单元/合同：193/193 通过。
- 模块化运行时：34/34 通过。
- Harness quick：11/11 通过。
- Harness full：21/24 通过。失败的 3 项均为既有、明确超出本变更范围且禁止伪造的模型/历史产物：
  - `dashboard-runtime-contracts`：缺少 dashboard/model 派生数据和模型目录；
  - `causal-history-evidence`：缺少 v13.9 历史指标文件；
  - `model-prediction-contracts`：缺少 `new_collection_hi_artifacts.joblib`。
- 首次 full 中 `interface-monitor-contracts` 曾发生一次 30 秒启动自检超时，单项复跑 1/1 及最终 full 均通过。
- Helper 合同/传输/relay/HTTP fallback/WebSocket/journal/remote mirror：107/107 通过。
- MySQL、采集完整性、质量侧车、重试、恢复和 journal：104/104 通过。
- 双模式配置、发行门禁、候选、预检、存储、容量、Windows、MySQL、Helper、耐久与现场操作专项均通过；两种模式规范化输出等价。
- Python `compileall` 通过；候选包内导入通过；PowerShell 脚本 AST 检查通过；`git diff --check` 通过。

本轮最后一个 RED→GREEN：旧公网 Helper 测试仅声明 `real_capture`/`unified_frame_v1`，启动被正确拒绝并返回 `release_contract_mismatch`；测试夹具改为携带权威版本、build、协议、统一帧和 schema 能力后，目标用例 1/1、`frontend-public-contracts` 1/1 以及最终 full 中该检查均通过。生产失败关闭语义未放宽。

## 30 分钟双模式耐久

| 模式 | 墙钟时长 | 帧数 | 有效频率 | 最大间隔 | writer 队列峰值 | journal | 对账 |
|---|---:|---:|---:|---:|---:|---:|---|
| `local_direct` | 1800.156 s | 18001 | 9.999981 Hz | 119.534 ms | 1 | 不适用 | 通过 |
| `remote_helper` | 1800.171 s | 18001 | 9.999941 Hz | 112.663 ms | 1 | 20,013,056 bytes / 18001 行 | 通过 |

两者绝对序号均为 0–18000 且连续，CSV/质量侧车均为 18001 行，时间戳单调、逐通道质量完整、最终状态为 `complete`。网络中断重放、Helper 重启重放和重复停止幂等均通过。`mysql_rows=null` 明确表示未使用真实目标 MySQL，未把模拟结果升级为目标实例证据。证据等级为 `endurance_30m`、来源为确定性模拟、`physical_sensor_verified=false`。

## 环境阻塞项

| 阻塞项 | 当前证据 | 解除条件 | 证据等级 |
|---|---|---|---|
| 目标 MySQL | 未提供 `AFP_MYSQL_PROFILE`；未验证目标身份/schema/权限/事务/中断恢复 | 提供目标实例网络访问及受保护的连接 profile，允许执行 INSERT/SELECT/ROLLBACK 与断开恢复演练 | `blocked / remote_integration` |
| 历史数据库待重试 | `D:\AFP_Capture` 检出 1 个 `*_mysql*_pending.json`，正式采集门禁关闭 | 在目标实例可用后执行重试并完成最终对账，不删除或伪造该记录 | `failed / local_environment` |
| 第二台 Windows | 当前没有可访问的第二台电脑；本机 300 帧协议演练不能替代跨机 | 在第二台电脑运行候选内 `second-windows-helper.ps1`，完成安装/启停/重启、配对、HTTP/WebSocket、断网、睡眠恢复和网络切换 | `blocked / remote_integration` |
| Windows 管理员级设置 | 当前主机未满足 AC 睡眠禁用、活动网卡节能禁用、USB 选择性挂起禁用、时间同步 | 管理员审查 `review-windows-remediation.ps1` 后实际调整并重跑预检；代码未自动更改系统策略 | `failed / local_environment` |

正式发布、防火墙或管理员设置未被自动修改；如需把候选提升为正式 `delivery/`，仍需用户另行授权。

## 真实设备边界

`pre_sensor/real-device-only-checklist.json` 仅保留 9 类物理验证：设备身份/端口、真实通道、单位/量程/倍率/大小端/方向、校准/零点/噪声/漂移/串扰、全设备 10 Hz、USB 供电/带宽/电磁环境、真拔线/断电/重启、真实驱动关闭、接近正式时长的实物耐久。版本、配置、网络、存储、MySQL、Helper 部署和 Windows 设置均不在该清单中。
