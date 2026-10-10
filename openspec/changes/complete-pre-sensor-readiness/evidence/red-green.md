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

