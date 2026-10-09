# Spec Delta

## Purpose

定义真实采集从接口检查、协议读取、统一帧传输到持久化的失败关闭和可恢复行为，使异常不能被静默解释为有效测量或完整采集证据。

## ADDED Requirements

### Requirement: 真实采集模式必须显式且并列
系统 SHALL 将 `local_direct` 与 `remote_helper` 作为并列一等真实采集模式。真实采集配置 MUST 显式选择其中一种，MUST NOT 根据地址、端口、设备状态或Helper可用性推断模式。模式专属字段混用 MUST 失败关闭。

#### Scenario: 选择本机直连
- **WHEN** 配置明确选择 `local_direct`
- **THEN** 主程序 MUST 直接检查、打开、读取、重连和关闭本机设备，并 MUST NOT 依赖Helper、WebSocket或HTTP

#### Scenario: 选择远程Helper
- **WHEN** 配置明确选择 `remote_helper`
- **THEN** 主程序 MUST 通过Helper readiness、能力协商和受控传输获取数据，并 MUST NOT 直接打开远端设备

#### Scenario: 混用模式专属字段
- **WHEN** `local_direct` 配置包含远程Helper会话字段，或 `remote_helper` 配置要求主程序直接打开设备
- **THEN** 配置 MUST 以可诊断的模式冲突失败，不得静默忽略、猜测或切换模式

### Requirement: 两种模式必须共享唯一采集核心
两种真实采集模式 MUST 共享设备和通道定义、通道映射、单位、统一采样时钟、`capture_uuid`、绝对 `frame_sequence`、时间戳、逐通道质量、采集状态机、持久化格式和停止收尾语义。模式适配层 MUST NOT 分别实现通道或质量判断。

#### Scenario: 相同样本通过两种模式
- **WHEN** 同一设备/通道定义、同一虚拟时钟和同一输入样本分别进入本机适配层与Helper适配层
- **THEN** 两者 MUST 产生值、单位、序号、时间和逐通道质量等价的规范化统一帧

#### Scenario: 消费统一帧
- **WHEN** 帧进入保存、展示或后续消费者
- **THEN** 消费者 MUST NOT 根据帧来自本机还是Helper来解释数值、单位、质量、持久化结构或完成条件

### Requirement: readiness证据必须跨执行边界保持
通过隔离进程完成的真实接口检查 SHALL 返回可验证的配置指纹、设备身份、检查时间和分层结果，并 MUST 由实际执行启动的采集代理保存。不同传输路径 MUST 使用同一门禁语义。

#### Scenario: WebSocket检查后启动
- **WHEN** helper通过隔离检查进程完成当前配置的真实检查并随后收到启动命令
- **THEN** 主采集代理 MUST 能验证并使用该检查快照，启动不得因快照仅存在于已退出子进程而失败

#### Scenario: 配置在检查后改变
- **WHEN** 启动配置、设备身份或检查有效期与缓存快照不匹配
- **THEN** 系统 MUST 拒绝正式启动并要求重新检查

#### Scenario: 绕过readiness或能力协商
- **WHEN** `remote_helper` 收到没有当前有效快照或统一帧能力的启动请求
- **THEN** 正式远程采集 MUST 被拒绝；只有显式工程降级可继续且 MUST 持续标记不可形成完整采集证据

### Requirement: Modbus TCP响应必须完整验证
PLC读取 SHALL 验证事务标识、协议标识、Unit ID、MBAP长度、功能码、异常响应、字节数和寄存器数量。读取边界 MUST 严格遵循MBAP长度含Unit ID的定义。

#### Scenario: 标准读取响应
- **WHEN** PLC返回一个MBAP长度与PDU相符的保持寄存器响应
- **THEN** 读取 MUST 只消费该响应所属字节并返回请求数量的寄存器，不得等待额外字节

#### Scenario: 响应身份或长度错误
- **WHEN** 事务、协议、Unit ID、长度、功能码或字节数与请求不一致
- **THEN** 驱动 MUST 拒绝该响应、记录可诊断错误并不得把其值写入有效帧

### Requirement: 正式采集必须验证可持久化性
正式真实采集在启动前 MUST 验证保存根目录、会话目录创建和最小写入能力。验证失败时 MUST 拒绝启动；无持久化运行只能通过显式工程预览策略启用并持续标记。

#### Scenario: 保存根目录不可写
- **WHEN** 操作员以正式模式启动且保存根目录缺失、不可写或无法建立会话目录
- **THEN** 启动 MUST 失败并返回具体路径和原因，不得只在内存中继续正式采集

#### Scenario: 工程预览不保存
- **WHEN** 授权人员显式启用无持久化工程预览
- **THEN** 页面、API和导出 MUST 持续标记数据不可用于正式结论，并 MUST 报告内存保留边界

### Requirement: 逐帧身份和质量必须端到端保留
每个统一帧 SHALL 使用 `capture_uuid + frame_sequence` 唯一标识，并携带帧时间、逐通道来源时间、年龄、质量和新值标志。本地、helper、WebSocket、服务器镜像和持久化 MUST 保持这些字段可一一对账。

#### Scenario: helper批量上传帧
- **WHEN** helper上传包含多个统一帧的批次
- **THEN** 服务器 MUST 能把每行值与其帧序号及逐通道质量对应，断线重传不得产生重复或错配

#### Scenario: 旧helper缺少质量合同
- **WHEN** helper能力协商表明无法传输当前逐帧身份和质量
- **THEN** 系统 MUST 阻止正式远程采集或明确降级为不可形成完整采集证据的工程模式

### Requirement: 模式专属故障必须分别诊断并统一投影
本机设备打开、读取、重连和关闭故障 MUST 与远程网络、Helper进程、readiness、能力和传输故障使用不同诊断类别。两类故障 SHALL 通过共享核心映射到统一采集状态和逐通道质量。

#### Scenario: 本机单设备断线
- **WHEN** `local_direct` 的一个设备断线而其他设备继续
- **THEN** 系统 MUST 报告本机设备故障，仅降级受影响通道，并保持其他通道、状态查询和已保存证据可用

#### Scenario: Helper连接断线
- **WHEN** `remote_helper` 的控制或数据连接断开
- **THEN** 系统 MUST 报告远程网络或Helper故障，按统一新鲜度规则推进通道质量，并保留ACK、缺口和重传状态

### Requirement: 设备读取必须可恢复且受控
每个物理接口 SHALL 使用设备适配的最小轮询间隔、有限超时和退避重连。单设备异常 MUST NOT 停止其他设备或使统一帧静默显示全部正常，停止操作 MUST 在有界时间内结束或报告阻塞设备。

#### Scenario: 设备发生瞬时读取异常
- **WHEN** 任一驱动读取抛出可恢复异常
- **THEN** 该接口 MUST 进入重连状态并按上限退避重试，其他接口、状态查询和本地证据保存 MUST 继续

#### Scenario: ABB或PLC被高频轮询
- **WHEN** 配置轮询间隔低于设备档允许的最小值
- **THEN** 系统 MUST 使用设备档下限并在状态中报告实际轮询频率

### Requirement: 文件写入和停止终结必须可观察
采集线程 MUST 与持久化写入隔离，并使用有界队列、高水位和溢出策略。停止 SHALL 依次暴露停止采样、排空写入、文件终结、数据库同步、对账及完成或失败状态。

#### Scenario: 磁盘写入暂时变慢
- **WHEN** 写入速度低于统一帧产生速度
- **THEN** 采样 MUST 保持设备隔离，系统 MUST 报告队列水位；达到安全上限时 MUST 按声明策略失败关闭而非无限占用内存

#### Scenario: 停止时数据库不可用
- **WHEN** 本地文件已完成但已启用数据库无法同步
- **THEN** 状态 MUST 标记本地已保存、数据库待重试和整体未完全终结，并保留可重试证据
