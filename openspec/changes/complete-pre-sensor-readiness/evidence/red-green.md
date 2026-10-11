# RED→GREEN 证据

## Harness需求矩阵合同

- RED：`python -m unittest harness.tests.test_pre_sensor_readiness_contract -v`
- 结果：3 个测试运行，8 个预期失败。
- 原因：缺少 `PRE_SENSOR_RELEASE-001`、`PRE_SENSOR_ENVIRONMENT-001`、`PRE_SENSOR_ENDURANCE-001`、`PRE_SENSOR_FIELD_OPS-001`，缺少三个自动检查入口及两个环境检查入口。
- GREEN：`python -m unittest harness.tests.test_pre_sensor_readiness_contract harness.tests.test_regression_matrix_contract harness.tests.test_manual_cli_contracts -v`
- 结果：30 个测试全部通过；新增需求族、阻塞检查、环境检查、profile 顺序和 ASCII 一键入口均受合同约束。
- 裁定：既有 full/CI 合同要求 CI 检查均为 `automated`，因此用 `pre-sensor-endurance-unit-contracts` 提供 CI 自动合同，同时保留仅本机的 `pre-sensor-endurance-contracts` 为 `protocol_simulation`，避免把短单测冒充耐久证据。

## Harness预检四态结果

- RED：`python -m unittest harness.tests.test_pre_sensor_result_schema -v`
- 结果：测试模块导入失败；现有模型没有 `PreSensorResult`、物理检查白名单或四态汇总接口。
- GREEN：同一命令 3/3 通过；随后 `python -m unittest discover -s harness/tests -p "test_*.py" -v` 134/134 通过。
- 兼容性：既有 `pending_field` 汇总与 field 检查保持不变；新预检严格使用 `passed`、`failed`、`blocked`、`physical_sensor_pending`，阻塞/失败/物理待验证必须附修复建议。

## 统一发行合同

- RED：`python -m unittest harness.tests.test_release_contract -v`
- 结果：模块导入失败，现有应用版本、Helper build ID 和启动器版本没有共享加载器或权威合同。
- GREEN：同一命令 3/3 通过；权威源为 `pre_sensor/release-contract.json`，绑定应用/Helper/启动器版本、发行修订、Helper 协议、统一帧、配置/readiness schema、静态资源清单和 SHA-256 受管文件合同。
- 相关回归：Helper 握手、应用健康、双模式核心 45/45 通过；旧 Helper build ID 测试快照已改为引用权威合同。

## 启动前发行门禁

- RED：在 `visualization_app` 运行 `python -m unittest test_release_contract_gate -v`，2/2 失败：本机错误修订到达 `build_driver`，远程错误 Helper 身份仍发送 `start_capture`。
- GREEN：同一命令 2/2 通过；不匹配时设备构建、会话目录和 Helper sender 均为零副作用。
- 回归：Helper transport/WebSocket/relay、remote mirror、双模式与新门禁共 135/135 通过；兼容 Helper 能继续启动，缺失或错误版本/修订/协议/schema/统一帧能力失败关闭。

## 隔离候选装配

- RED：`python -m unittest harness.tests.test_candidate_release_contract -v` 因候选发行清单验证器不存在而导入失败。
- GREEN：同一命令 2/2 通过；候选入口拒绝既有目标并固定在 `artifacts/pre-sensor-candidates/<candidate-id>/`，清单绑定静态资源和 native DLL 摘要，声明 SDK/驱动要求。
- 装配兼容：现有启动器复用装配测试 1/1 通过；候选与既有真实采集预检 17/17 通过。
- 阶段全量：Harness 140/140、modular runtime 34/34、`git diff --check` 通过。
- 干净提交候选：`2.0.5-rc.1-9ee31829532f-20261009T112336Z`，`source_revision=9ee31829532f`，6175 项 `SHA256SUMS.txt`。
- 候选实测：候选内导入、发行清单/静态/native 验证无问题；`--self-test`、`--module-status`、`--verify-files` 返回码均为 0。
- 正式目录复验：仍为 6176 个受管文件，树摘要检查通过；正式 `delivery` 未修改。

## 双模式配置模板

- RED：`python -m unittest harness.tests.test_pre_sensor_config.PreSensorConfigurationTemplateTests -v`，2/2 因 `pre_sensor/config/local_direct.json` 与 `remote_helper.json` 不存在而报错。
- GREEN：同一命令 2/2 通过；两套完整模板显式互斥，覆盖 17 个通道/单位、5 类接口、保存、超时、重连、队列、日志和容量，所有端点使用 `AFP_*` 引用且无敏感字段或机器绝对路径。

## 双模式配置启动前门禁

- RED：`python -m unittest harness.tests.test_pre_sensor_config.PreSensorConfigurationValidationTests -v` 首次因 `visualization_app.pre_sensor_config` 不存在而导入失败；补充远程 Helper 合同用例后，错误 Helper 版本仍会触发启动回调，1/1 失败。
- GREEN：配置测试 7/7 通过；缺失/混用模式、接口位置、通道、单位、引用、容量、超时、重连、队列和日志边界均在启动回调前失败，远程 Helper 发行合同不一致时发送次数为零。
- 复用与兼容：验证器映射到既有 `AcquisitionConfig`，双模式/共享采集核心和发行门禁 45/45 通过，前端合同 84/84 通过。`test_app.DashboardTests` 仍因仓库既有 dashboard/model 历史数据缺失无法初始化，未生成任何伪数据；与本配置变更无关。

## 候选配置资产合同

- RED：`python -m unittest harness.tests.test_candidate_release_contract.CandidateReleaseContractTests.test_release_manifest_binds_both_mode_templates_and_schema -v` 因发行合同缺少 `configuration_templates` 报错。
- GREEN：候选发行合同 3/3、候选真实采集预检 15/15 通过；两套模板和 schema 具有发行清单 SHA-256，任一篡改会被候选验证器拒绝。
- 干净提交候选：`2.0.5-rc.1-cc7b43d45893-20261010T063146Z`；候选综合验证问题数为 0，候选内 `pre_sensor_config` 成功解析两种模式各 17 通道，秘密扫描通过，`--self-test`、`--module-status`、`--verify-files` 均返回 0。
- 正式目录复验：`delivery-readonly: passed (6176 managed files)`，正式发行目录没有被候选装配修改。

## 统一预检编排与一键入口

- RED：`python -m unittest harness.tests.test_pre_sensor_preflight -v` 首次因 `visualization_app.pre_sensor_preflight` 不存在而导入失败；补充报告写入/候选入口合同后又分别因缺少 `write_preflight_report` 和 `pre_sensor/run-preflight.ps1` 失败。
- GREEN：预检编排 5/5、候选真实采集预检 15/15 通过。每个探针具有稳定 ID 和独立超时，异常不会阻止后续检查；`failed`、`blocked`、`physical_sensor_pending` 分类失败关闭；同一不可变报告对象生成 JSON/Markdown 原子文件对。
- 候选入口：`run-pre-sensor-preflight.ps1` 转发明确模式、配置和输出目录到候选启动器，启动器调用包内预检模块；失败或外部阻塞返回非成功进程状态，真实设备未连接仅允许白名单物理检查为 `physical_sensor_pending`。

## 候选运行环境探针

- RED：`python -m unittest harness.tests.test_pre_sensor_runtime_probe -v` 因缺少 `probe_candidate_runtime` 导入失败。
- GREEN：4/4 通过；从候选 manifest 校验包内 Python 运行库、模块、native DLL 摘要/PE 架构、SDK 检测文件和驱动状态。删除包内运行库时，即使提供源工作树路径仍失败，禁止借用开发环境。

## 保存目录与慢写探针

- RED：`python -m unittest harness.tests.test_pre_sensor_storage_probe -v` 因缺少 `probe_save_directory` 导入失败。
- GREEN：3/3 通过；覆盖空路径、创建权限、路径长度、最小剩余空间、写入/flush/close、慢写、原子重命名、内容复验和清理。成功无残留，失败会尝试清理；清理本身失败时保留残留证据并明确失败阶段。

## Windows只读状态探针

- RED：`python -m unittest harness.tests.test_pre_sensor_windows_probe -v` 因缺少 Windows 探针与修复脚本生成器导入失败。
- GREEN：4/4 通过；只读覆盖防火墙查询、端口、代理、本机睡眠、活动网卡节能、USB 选择性挂起、W32Time 和同步状态；访问拒绝转换为外部环境 `blocked`，脚本只显示检查/建议，不含设置命令。
- 授权后的 RED→GREEN 1：Intel WLAN 驱动不支持 `Get-NetAdapterPowerManagement`，旧探针无法证明活动物理网卡已禁用系统断电。新增测试先因 `_adapter_power_disabled_from_registry` 不存在而 RED；实现只检查活动物理网卡并以 `PnPCapabilities` 的 `0x20` 位作驱动兼容回退后，目标测试通过。
- 授权后的 RED→GREEN 2：时间源已离开 Local CMOS 但 `w32tm` 仍报告 Leap Indicator 3 时，旧逻辑会假通过。新增测试先因 `_windows_time_status_synchronized` 不存在而 RED；实现显式拒绝 Leap 3、Local CMOS 和 free-running clock 后通过。Windows 专项现为 6/6。
- 当前主机实测：经用户明确授权，已生成变更前快照与回滚脚本，然后禁用 AC 睡眠、活动 WLAN 系统断电和 AC USB 选择性挂起；为 W32Time 添加经 WLAN 网关的目标 NTP `/32` 持久路由并完成有效同步。最终报告位于 `artifacts/pre-sensor-evidence/current-host-windows-after/pre-sensor-readiness.{json,md}`，8 项状态全部通过；未更改 Codex 防火墙规则。

## 统一容量策略

- RED：`python -m unittest harness.tests.test_pre_sensor_capacity -v` 因缺少 `CapacityPolicy` 导入失败。
- GREEN：3/3 通过；writer/transport 队列、日志总量/保留天数、journal、retry spool、最小剩余空间均要求有限正值；软阈值投影为明确警告，硬阈值和队列溢出失败关闭。

## MySQL分阶段事务预检

- RED：`python -m unittest harness.tests.test_pre_sensor_mysql_probe -v` 首次因缺少 `probe_mysql_readiness` 导入失败；既有写探针仅调用 rollback，未查询并证明探针行已消失。
- GREEN：3/3 通过；使用唯一探针身份依次证明 connect、schema、INSERT 权限、rollback 和零残留，报告只保留脱敏目标字段。缺少目标 profile 返回外部 `blocked`，`simulation` 证据明确拒绝升级为目标实例通过。

## 持久化身份、重试与恢复

- RED：质量侧车合同因摘要中缺少 `quality_sidecar` 失败；`python -m unittest harness.tests.test_pre_sensor_recovery_gate -v` 因缺少持久恢复扫描和预检探针导入失败。
- GREEN：质量侧车携带与 CSV/时间戳逐通道质量相同的 `capture_uuid`、样本数和绝对序号范围；启动恢复扫描识别 `*_mysql*_pending.json` 与 `.partial`，证据仅含文件名/相对路径摘要，不泄露机器根路径；待重试或未完成收尾会阻止下一正式采集。
- 迁移根目录 RED→GREEN：实际旧记录引用已不存在的 `F:\AFP_Capture`，而正确 CSV 已按同一 `capture_uuid` 归档到当前 `D:\AFP_Capture`。新增测试先因 `_resolve_pending_layer_file` 不存在而 RED；实现按不可变采集身份和归档会话唯一匹配，拒绝误选后来同名层文件。恢复专项现为 4/4；实测 `missing_retry_sources=0`、`relocated_retry_sources=1`。
- 相关回归：MySQL、采集完整性、双模式持久化、server journal 共 104/104 通过，包含运行中断开、耐久 retry spool、重复 retry/stop、journal 重启、重复 ACK、配额和目标保存身份覆盖。

## 当前目标MySQL与历史待重试实测

- 目标 MySQL：`192.168.101.31:3306` TCP 已连通；实际 MySQL 握手确认 `afp_app` 空密码被 1045 拒绝，因此当前精确阻塞为“缺少受保护的目标账号凭据”，不是网络或传感器。报告位于 `artifacts/pre-sensor-evidence/current-target-mysql-live/`；模拟数据库证据没有升级为目标实例通过。
- 持久恢复：`D:\AFP_Capture` 检出 1 个既有 target MySQL 待重试记录；记录中的密码字段是脱敏占位符 `***`，没有可复用秘密。层 CSV 已可从匹配 `capture_uuid` 的归档会话安全定位；在通过本机安全提示取得目标凭据并完成重试/对账前，正式采集门禁保持关闭。

## Helper预检、协议故障演练与第二台Windows

- RED：`python -m unittest harness.tests.test_pre_sensor_helper_probe -v` 因缺少 `probe_helper_readiness` 导入失败；版本/合同、安装、进程、端口、健康、配对、readiness、HTTP、WebSocket 和代理任一阶段都要求失败关闭。
- GREEN：Helper 预检 4/4 通过；本机模式明确不适用，远程模式缺少第二台电脑返回外部 `blocked`，协议模拟与真实双机只允许两个独立证据等级。
- RED：`python -m unittest harness.tests.test_pre_sensor_helper_drill -v` 因缺少协议演练器导入失败；报告写入合同随后因 writer 不存在失败。
- GREEN：协议演练 3/3 通过；实际生成 300 帧、10 Hz、绝对序号 0–299，覆盖 ACK/批量、缺口拒绝、重复幂等、断线重放、Helper 重启重放、server journal 重启恢复和最终 300/300 对账。证据位于 `artifacts/pre-sensor-evidence/local-helper-protocol/`，固定标记 `protocol_simulation` 且 `two_machine_verified=false`。
- RED→GREEN：第二台 Windows 脚本合同先因文件不存在 2/2 报错，候选装配断言再因缺失文件名失败；实现后 3/3 通过。脚本提供 install/start/stop/restart/check/export，不接受或输出凭据，不自动修改防火墙、电源、网卡、USB 或时钟策略，检查失败/阻塞分别返回非零退出码。
- 既有回归：Helper transport/relay、HTTP fallback、WebSocket、live protocol、server journal、remote mirror、local agent 共 107/107 通过。
- 外部环境：当前未提供可访问的第二台 Windows 主机，`artifacts/pre-sensor-evidence/second-windows-blocked/` 明确为 `blocked`；未把本机协议模拟冒充真实跨机、睡眠恢复或网络切换证据。

## 双模式耐久、故障边界与统一对账

- RED：`python -m unittest harness.tests.test_pre_sensor_endurance -v` 首次 3/3 因 `pre_sensor_endurance` 不存在而导入失败；候选一键入口合同随后因 wrapper 不存在失败。
- GREEN：耐久合同 5/5 通过；默认计划固定每种模式 1800 秒/10 Hz，低于 1800 秒只能标记 `short_test`。运行器复用 `AcquisitionManager`、`LocalCaptureAgent`、`HelperSamplePump`、`RemoteAcquisitionRegistry` 和 `ServerCaptureJournal`，不另造采集核心。
- 双模式短跑：各 5 秒实跑通过；local_direct 51 帧/CSV 51/质量 51，remote_helper 51 帧/CSV 51/质量 51/journal 51，绝对序号 0–50、逐通道质量与单调时间戳对账，频率分别约 9.988 Hz 和 9.980 Hz。证据固定标记 `short_test`。
- 故障边界：`fault_injection_simulation` 报告通过慢写背压、队列溢出失败关闭、停止超时、容量软告警/硬拒绝、数据库中断 spool、停止时数据库不可用和恢复对账；writer 队列峰值 2，最终 spool 为 0。
- 差异注入：CSV、质量侧车或 journal 任一计数变化均使统一对账失败并定位字段；MySQL 目标实例不可用时保留 `mysql_rows=null`，不会把模拟恢复写成目标 MySQL 通过。
- 30 分钟墙钟：`local_direct` 与 `remote_helper` 已并行启动，完成并核验前 7.7 保持未完成。

## 现场手册、脱敏证据与真实设备白名单

- RED：`python -m unittest harness.tests.test_pre_sensor_field_ops -v` 首次 4/4 因导出器、手册和白名单文件不存在而报错。
- GREEN：同一命令 4/4 通过。证据导出器按结构脱敏密码/Token/授权/Cookie、URL 用户信息与敏感 query、环境变量和 Windows 用户私有路径；已知秘密仍存在或二进制无法安全脱敏时失败关闭，并为每个源保留 SHA-256/字节数清单。
- 现场手册按安装、预检、模式选择、修复、启动、观察、停止、数据库重试/对账、导出证据的固定顺序编排；所有高影响 Windows 设置仅检查，不自动修改。
- `real-device-only-checklist.json` 恰含 9 个实物验证 ID；版本、配置、网络、存储、MySQL、Helper 或 Windows 项注入会被验证器拒绝。
- 最终候选首次从全新短路径运行时，模板的 `save_root_ref` 因操作员未预设环境引用而正确失败；这暴露了“一键预检”仍依赖隐含环境步骤。新增合同测试先因入口缺少保存根目录设置与安全 MySQL 提示而 RED；入口现默认使用候选盘符下的 `AFP_Capture`，支持显式 `-SaveRoot`，并用 `-PromptForMySQL` 的 SecureString 本机提示临时传递目标 profile，子进程退出后恢复环境且不把密码放入命令行/配置/报告。现场操作专项现为 5/5，PowerShell AST 通过。

## 候选窗口化命令失败关闭

- RED：首次候选短跑把输出放在深层候选目录，已有 235 字符路径保护正确拒绝，但窗口化启动器的未捕获 `RuntimeError` 留下错误对话框；同时 PowerShell 直接调用 GUI EXE 不等待，无法可靠取得 `$LASTEXITCODE`。新增三个静态合同测试均失败。
- GREEN：预检/耐久的已知不通过结论现在写完报告后使用 `SystemExit(1)`；预检、耐久和证据导出 wrapper 统一以 `Start-Process -Wait -PassThru` 等待并返回 `.ExitCode`。3/3 合同通过，所有脚本 PowerShell AST 语法检查通过。
- 候选短路径复测：在 `D:\AFP-Drill-11d304a` 分别执行 local_direct 与 remote_helper 1 秒候选短跑，均返回 0；各 11 帧且 CSV/质量/绝对序号/时间戳对账，远程 journal 11/11。深路径失败不被写成采集缺陷，现场手册已明确要求短安装路径。

## 候选MySQL驱动声明一致性

- RED：候选统一预检的 `runtime.candidate_dependencies` 发现发行合同要求 `pymysql`，但实际冻结运行时只打包并使用首选 `mysql.connector`；该候选因此正确失败，未把自检通过冒充完整依赖通过。
- GREEN：发行合同 `required_modules` 改为生产构建明确收集且存储实现优先使用的 `mysql.connector`；新增合同断言禁止再次把可选 fallback `pymysql` 写成必需模块。目标 MySQL 环境验证仍独立要求真实实例，并未因驱动声明修正而降级。
- RED：修正 MySQL 声明后审查默认驱动探针，发现 SMRF 实现通过 `ctypes` 使用 Windows `hid.dll`，探针却错误要求第三方 Python `hid` 包，会造成候选假失败。
- GREEN：新增 Windows HID 检测合同并改为实际加载系统 `hid.dll`；同时继续独立验证串口平台、以太网、UVC native DLL 和厂商 SDK。运行时探针 5/5 通过，未把物理传感器存在性混入驱动检测。

## Helper发行能力测试夹具收口

- RED：最终 Harness full 的 `frontend-public-contracts` 中，`test_real_helper_target_mysql_is_server_only_and_finalizes_after_last_batch` 返回 `release_contract_mismatch`。测试只给 Helper 声明 `real_capture` 与 `unified_frame_v1`，却仍期待生产启动成功，违反本变更的启动前版本/协议门禁。
- GREEN：测试从权威发行合同填入 helper version、build ID、协议、统一帧及配置/readiness schema；没有放宽生产门禁。目标用例 1/1、`frontend-public-contracts` 单项及最终 full 中该检查均通过。

## 最终墙钟耐久与综合验证

- `local_direct`：1800.156 秒、18001 帧、9.999981 Hz、最大帧间隔 119.534 ms、writer 队列峰值 1；CSV/质量侧车各 18001 行，绝对序号 0–18000 连续，对账通过。
- `remote_helper`：1800.171 秒、18001 帧、9.999941 Hz、最大帧间隔 112.663 ms、writer 队列峰值 1、journal 20,013,056 bytes/18001 行；CSV/质量侧车/journal 对账通过。
- 两种模式证据均为 `endurance_30m`、确定性模拟、`physical_sensor_verified=false`；网络重放、Helper 重启重放和停止幂等通过，`mysql_rows=null` 未被写成真实目标数据库通过。
- 本轮加固后：Harness 单元/合同 196/196、Windows+恢复专项 10/10、modular runtime 34/34、Python compileall 和 `git diff --check` 通过；候选、quick/full、OpenSpec strict 与正式 delivery 只读基线将在最终候选重建后再次执行并记录。
- full 剩余 3 项为仓库既有模型/历史数据缺失：`dashboard-runtime-contracts`、`causal-history-evidence`、`model-prediction-contracts`。按范围限制未生成缺失模型或历史数据。
