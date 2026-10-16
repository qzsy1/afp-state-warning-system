# Spec Delta

## Purpose

定义两种真实采集模式的无密配置模板、启动前门禁和统一环境预检，使所有非物理传感器问题在现场采集前以明确状态和可执行修复建议暴露。

## ADDED Requirements

### Requirement: 系统必须提供两套互斥无密模板
系统 SHALL 提供 `local_direct` 与 `remote_helper` 两套版本化模板，包含通道、单位、接口、保存策略、超时、重连、队列、日志和容量参数，且 MUST NOT 包含密码、Token 或生产凭据。

#### Scenario: 验证local_direct模板
- **WHEN** 操作员选择 `local_direct`
- **THEN** 模板 MUST 拒绝 Helper 地址、配对会话和远程能力字段，并完整声明本机接口与持久化策略

#### Scenario: 验证remote_helper模板
- **WHEN** 操作员选择 `remote_helper`
- **THEN** 模板 MUST 拒绝主程序直接打开设备的字段，并声明 Helper 端点、合同、传输、超时和重连策略

#### Scenario: 模板包含秘密
- **WHEN** 模板或导出的有效配置含密码、Token、凭据或未经脱敏的机器秘密
- **THEN** 配置验证 MUST 失败且报告不得回显秘密值

### Requirement: 配置必须在启动副作用前完整验证
系统 SHALL 在打开设备、创建正式会话或发送 Helper 启动命令前验证模式选择、模式互斥、通道/单位、接口、保存、超时、重连、队列、日志和容量参数。

#### Scenario: 模式未选择或字段混用
- **WHEN** 真实采集未显式选择模式或同时包含两种模式专属字段
- **THEN** 启动 MUST 失败并指出冲突字段，且设备和 Helper 启动调用次数 MUST 为零

#### Scenario: 参数超出安全范围
- **WHEN** 队列、容量、超时、重连或日志保留参数为空、非数值或超出声明范围
- **THEN** 配置门禁 MUST 失败并给出字段级原因，不得静默钳制为未知值

### Requirement: 统一预检必须提供双格式结果
系统 SHALL 提供单一预检入口，同时输出人可读报告和稳定 schema 的 JSON；每项结果 MUST 包含检查 ID、状态、证据、影响和不含秘密的修复建议。

#### Scenario: 预检完成
- **WHEN** 操作员运行任一模式的预检
- **THEN** 两种报告 MUST 来自同一结果对象，并包含候选身份、所选模式、开始/结束时间和总状态

#### Scenario: 检查本身异常
- **WHEN** 某项检查因权限、超时或未预期异常无法完成
- **THEN** 该项 MUST 为 `blocked` 或 `failed` 并保留诊断，不得被省略或默认为通过

### Requirement: 预检状态必须严格区分物理设备和环境阻塞
预检项状态 SHALL 仅为 `passed`、`failed`、`blocked` 或 `physical_sensor_pending`；只有实际设备读取、物理端口或真实信号验证可使用 `physical_sensor_pending`。

#### Scenario: 缺少外部环境
- **WHEN** 第二台 Windows 电脑、目标 MySQL、管理员权限或所需网络访问不可用
- **THEN** 对应项目 MUST 报告 `blocked`，并标记需要用户提供的外部环境

#### Scenario: 未连接真实传感器
- **WHEN** 软件、驱动和配置检查可执行但没有物理传感器可读
- **THEN** 只有设备读取项目 MAY 为 `physical_sensor_pending`，所有其他项目仍 MUST 得到确定状态

### Requirement: 预检必须验证Windows运行环境
预检 SHALL 检查候选运行库、必需模块、DLL、厂商 SDK/驱动安装状态、代理、端口、防火墙可观察状态、睡眠、网卡节能、USB 选择性挂起和时间同步。

#### Scenario: 缺少运行库或SDK
- **WHEN** 候选声明的 Python/打包运行库、DLL、SDK 或驱动不可发现
- **THEN** 对应检查 MUST 失败并给出组件名称、检测方法和无秘密修复建议

#### Scenario: 需要管理员权限读取系统状态
- **WHEN** 当前账户无法读取某项 Windows 策略或防火墙状态
- **THEN** 检查 MUST 报告 `blocked` 和所需权限，不得自动改变系统设置

#### Scenario: 时钟或节能设置不满足门限
- **WHEN** 时间同步异常，或睡眠、网卡节能、USB 选择性挂起可能中断采集
- **THEN** 预检 MUST 明确失败或阻塞并指出受影响链路，不得把它归入传感器待验证

### Requirement: 预检必须验证保存与容量
预检 SHALL 验证保存目录创建、写入、关闭、原子重命名和清理，并检查路径长度、可用磁盘、日志、journal 与 retry spool 的目录和容量限制。

#### Scenario: 保存目录不可用
- **WHEN** 路径为空、不可写、过长、无法重命名或可用容量低于配置门限
- **THEN** 检查 MUST 失败并证明测试文件已清理或指出清理失败

#### Scenario: 容量限制缺失
- **WHEN** 日志、journal 或 retry spool 没有最大容量、保留策略或阈值告警
- **THEN** 预检 MUST 失败，不得允许正式真实采集启动

### Requirement: 预检必须验证MySQL事务能力
启用 MySQL 时，预检 SHALL 对声明的目标实例验证连接、schema、权限、写入和回滚，且不得把模拟数据库结果当作目标环境证据。

#### Scenario: 目标MySQL可用
- **WHEN** 用户提供目标实例的受保护凭据引用和网络访问
- **THEN** 预检 MUST 执行可回滚探针、验证所需 schema 与权限，并确认探针未留下业务数据

#### Scenario: 目标MySQL不可访问
- **WHEN** 目标实例、凭据权限或网络不可用
- **THEN** 项目 MUST 报告 `blocked` 或 `failed`，本机模拟结果只能作为单独的自动化证据

### Requirement: 预检必须验证Helper和网络合同
`remote_helper` 预检 SHALL 检查 Helper 进程、候选身份、可执行文件 SHA-256、发行身份、健康、能力、配对、端口、HTTP/WebSocket 和代理路径，并验证不一致能在启动前失败。

#### Scenario: Helper健康但合同不一致
- **WHEN** Helper 可连接但发行、协议、配置或统一帧合同不匹配
- **THEN** 预检 MUST 失败并禁止发送远程采集启动命令

#### Scenario: 仅本机模拟Helper
- **WHEN** 检查使用同一台电脑上的临时 HTTP/WebSocket 服务
- **THEN** 结果 MUST 标记为协议模拟，不得标记为真实双机部署通过

### Requirement: 预检必须阻止未完成恢复状态
预检 SHALL 检查上一采集的数据库待重试、未完成 journal、retry spool、未终结停止状态和未完成对账。

#### Scenario: 上一采集仍待数据库重试
- **WHEN** 存在未完成数据库写入或未对账的持久证据
- **THEN** 下一次正式采集 MUST 被阻止，并提供重试与对账入口

#### Scenario: 恢复完成
- **WHEN** 重试成功且 CSV、质量侧车、journal 与 MySQL 完成身份和计数对账
- **THEN** 阻止状态 MUST 可持久地清除，随后预检可通过

### Requirement: Helper端点和会话必须匹配当前入口代次
`remote_helper` 的端点选择、自动重连、配对、readiness 和命令通道 SHALL 使用服务器当前公网入口代次。Helper 保存的端点仅可用于同一代次内重连，不得覆盖操作员或候选交接提供的更新入口，也不得在代次不匹配时形成就绪状态。

#### Scenario: 保存端点与当前交接入口不同
- **WHEN** Helper 启动时保存的 URL 属于旧代次，而操作员或候选交接提供当前入口及其新代次
- **THEN** Helper MUST 使用当前入口，MUST NOT 静默回退到旧 URL，并在重新配对前保持 `ready=false`

#### Scenario: 同一代次连接暂时中断
- **WHEN** Helper 与服务器的连接中断但权威 URL、候选身份和入口代次未变化
- **THEN** Helper MAY 使用保存端点自动重连，并 MUST 清理陈旧连接状态后重新声明同一代次 readiness

#### Scenario: 服务器在同一代次重启且命令状态丢失
- **WHEN** 持久化记录显示启动命令已排队或投递，但重启后的服务器无法证明其执行状态
- **THEN** 服务器 MUST 将该命令终结为 `command_state_lost` 且不得自动重发；迟到结果 MUST NOT 覆盖或新增用户可见终态，已投递的启动命令 MUST 在新的显式状态对账完成前阻止入口切换

#### Scenario: Helper收到旧代次命令
- **WHEN** Helper 收到的采集命令代次与当前会话代次不一致
- **THEN** Helper MUST 拒绝执行并返回明确失败，不得打开设备、创建采集或把命令重新解释为当前代次

#### Scenario: 无法取得当前入口代次
- **WHEN** Helper 无法读取或验证服务器当前入口代次
- **THEN** 进程 MAY 保持运行并报告诊断，但预检 MUST 为 `blocked` 或 `failed`，不得报告已配对、在线或可执行采集

