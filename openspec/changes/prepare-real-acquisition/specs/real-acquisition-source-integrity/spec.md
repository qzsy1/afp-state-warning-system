# Spec Delta

## Purpose

定义真实采集业务源、装配过程和发布产物之间的唯一事实来源及一致性验证行为，避免直接修改 delivery 或重新装配时恢复旧实现、遗漏依赖和产生版本不一致。

## ADDED Requirements

### Requirement: 真实采集实现必须拥有唯一权威源
系统 SHALL 将真实采集业务实现维护在权威源目录中，delivery MUST 仅由受控装配生成。产品修复、测试和配置变更 MUST NOT 只存在于 delivery 中。

#### Scenario: 修复真实采集缺陷
- **WHEN** 开发者修复采集、helper、预测、前端或保存链路
- **THEN** 修复 MUST 位于权威源及其测试中，重新装配后 delivery SHALL 获得等价行为

#### Scenario: delivery存在独有实现
- **WHEN** 一项运行所需模块或行为只存在于当前 delivery 而权威源无法生成
- **THEN** 源完整性门禁 MUST 失败并指出缺失源文件或行为，不得把当前 delivery 视为可持续修复

### Requirement: 装配必须在隔离目标中可重复验证
装配检查 SHALL 在新建临时目标中运行，MUST NOT 为验证而覆盖工作区现有 delivery。装配结果 MUST 能完成导入、自检、清单生成和受影响回归。

#### Scenario: 验证候选装配
- **WHEN** Harness执行装配一致性检查
- **THEN** 它 MUST 使用隔离临时目录生成候选包并验证运行模块、入口、静态资源和清单，不得修改已存在 delivery 文件

#### Scenario: 装配遗漏依赖
- **WHEN** 候选包缺少权威源声明的模块或入口导入失败
- **THEN** 检查 MUST 返回失败并给出源文件、目标文件和失败入口

### Requirement: 发布产物必须绑定同一修订
服务器、前端、helper、运行模块和清单 SHALL 报告可比较的运行修订与文件摘要。关键组成的修订或摘要不一致时，真实启动证据 MUST 失效。

#### Scenario: helper与服务器修订不同
- **WHEN** helper能力或构建修订与服务器要求不一致
- **THEN** 系统 MUST 阻止正式真实采集或明确进入不具备正式结论资格的工程状态

#### Scenario: 清单与候选文件不一致
- **WHEN** 候选装配任一受管文件摘要与清单不一致
- **THEN** 发布门禁 MUST 失败且不得生成稳定交付结论

### Requirement: 既有公开合同必须保持兼容
源完整性修复 MUST 保持既有仿真入口、10 Hz 正常采集、前端字段和 API 语义。新增元数据 SHALL 采用向后兼容扩展，除非另有明确版本化迁移规格。

#### Scenario: 运行既有核心回归
- **WHEN** 权威源、装配脚本或真实采集模块发生变化
- **THEN** 既有模拟采集、接口映射、helper、CSV、MySQL、诊断、前端和模型合同测试 MUST 继续通过

