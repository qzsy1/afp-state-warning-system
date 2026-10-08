# Spec Delta

## Purpose

定义真实采集准备 Harness 对协议、跨进程传输、装配、故障恢复和既有功能的分层验证与证据等级，确保无硬件自动化与现场验收结论严格分离。

## ADDED Requirements

### Requirement: Harness必须分层验证真实采集准备项
Harness SHALL 提供协议与数据合同、进程与传输集成、隔离装配一致性和完整回归四层检查。每项准备需求 MUST 映射到稳定检查ID、执行命令、适用profile和证据等级。

#### Scenario: 运行quick profile
- **WHEN** 开发者修改真实采集权威源或相关Harness
- **THEN** quick MUST 运行受影响协议、状态、质量、保存和装配合同测试，并在失败时返回非零退出码

#### Scenario: 运行full profile
- **WHEN** 第一阶段准备改动完成
- **THEN** full MUST 在新增检查之外运行既有无硬件核心回归，任何必需检查失败 MUST 阻止交付结论

### Requirement: 协议测试必须使用可执行报文夹具
协议Harness MUST 通过受控套接字或字节流执行生产解析路径，并断言返回值、消费边界和错误分类。只搜索源码文本或仅替换整个驱动的测试 MUST NOT 作为协议通过证据。

#### Scenario: Modbus响应分片到达
- **WHEN** 一个有效响应以多个网络片段到达
- **THEN** 生产解析路径 MUST 正确重组并停止在声明长度，不得超读下一响应或等待不存在的字节

#### Scenario: Modbus异常响应
- **WHEN** 夹具返回协议异常或身份不匹配
- **THEN** 检查 MUST 断言该值未进入有效采集行且错误可定位

### Requirement: 跨进程Harness必须执行真实命令顺序
Helper集成检查 SHALL 使用实际调度器和独立检查执行边界，执行检查、启动、取数、停止的生产命令顺序。测试 MUST 断言主采集代理收到检查证据，而不是只断言子进程返回成功。

#### Scenario: 隔离检查后正式启动
- **WHEN** Harness通过helper调度器执行成功检查后立即启动相同配置
- **THEN** 启动 MUST 通过指纹门禁且使用同一检查证据

#### Scenario: 检查后篡改配置
- **WHEN** Harness在启动前改变一个影响指纹的字段
- **THEN** 启动 MUST 失败并要求重新检查

### Requirement: 故障注入必须验证失败关闭和恢复边界
Harness MUST 注入保存路径不可写、陈旧通道、帧序号断点、helper断线、驱动瞬时异常、慢磁盘和数据库不可用。每项检查 SHALL 同时验证状态、数据保留、预测资格和恢复行为。

#### Scenario: 保存路径不可写
- **WHEN** 正式真实采集使用不可写的受控测试目录
- **THEN** 启动 MUST 被拒绝，采集线程不得运行，错误 MUST 指向保存门禁

#### Scenario: 陈旧数据仍为有限值
- **WHEN** 故障夹具保持最后数值但把其质量推进到陈旧
- **THEN** Harness MUST 验证原始证据保留、模型窗口不可用且页面/API不产生有效预警

### Requirement: 装配检查不得修改正式delivery
装配一致性检查 MUST 使用临时目录并在结束后报告候选路径或清理测试产物。检查开始前后，正式 delivery 的受管文件摘要 MUST 保持不变。

#### Scenario: 执行装配回归
- **WHEN** Harness从权威源构建候选包
- **THEN** 正式 delivery 的文件摘要 MUST 前后一致，候选包 MUST 单独完成导入和清单验证

### Requirement: 自动化证据不得冒充现场硬件证据
没有真实PLC、ABB、SMRF、UVC、M3232、第二台Windows电脑或真实标签时，Harness MUST 把相应项目报告为 `field_pending`。自动协议夹具和模拟测试只能证明软件合同。

#### Scenario: 无硬件环境全部自动检查通过
- **WHEN** quick和full全部通过但未提供当前修订的现场证据
- **THEN** 报告 MUST 表述为软件准备通过且现场待验证，不得宣称真实采集或生产预警已验收

### Requirement: 新增检查必须保护既有功能
第一阶段每项修复 MUST 先具有可观察失败的回归测试，并 SHALL 纳入既有三级矩阵。新增测试通过但既有核心回归失败时，变更 MUST NOT 被标记完成。

#### Scenario: 新测试通过但模拟采集回归失败
- **WHEN** 真实采集准备测试全部通过而任一既有模拟、前端、helper、保存、诊断或模型检查失败
- **THEN** full MUST 返回失败并列出回归项，不得用新增检查结果覆盖

