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

