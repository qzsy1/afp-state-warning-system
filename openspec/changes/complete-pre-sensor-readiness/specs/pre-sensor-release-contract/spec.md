# Spec Delta

## Purpose

定义接入真实传感器前必须满足的统一发行身份、组件合同和隔离候选装配行为，确保现场运行的主程序、Helper、配置、静态资源及协议来自同一可审计修订。

## ADDED Requirements

### Requirement: 候选发行必须具有统一发行身份
系统 SHALL 生成一个不含秘密的发行 manifest，绑定应用版本、构建修订、Helper 版本、Helper 协议、统一帧合同、配置 schema、静态资源摘要及所有受管文件摘要。

#### Scenario: 读取一致的发行身份
- **WHEN** 主程序或 Helper 加载候选包
- **THEN** 两者 MUST 从同一发行 manifest 获得可比较的版本、合同和构建修订，不得各自依赖无法关联的硬编码版本

#### Scenario: 受管文件与 manifest 不一致
- **WHEN** 任一受管模块、静态资源、配置模板或 Helper 文件缺失或摘要不匹配
- **THEN** 候选验证 MUST 失败并指出相对路径、预期摘要和实际状态

### Requirement: 组件不一致必须在启动副作用前失败
正式真实采集 SHALL 在打开本机设备、发送远程启动命令或创建正式采集会话前比较应用、Helper、协议、统一帧和配置合同。

#### Scenario: Helper版本或协议不兼容
- **WHEN** `remote_helper` 返回的发行修订或协议合同不满足候选 manifest
- **THEN** 系统 MUST 以可诊断的不一致错误拒绝正式启动，且不得向 Helper 发送采集启动命令

#### Scenario: 配置schema不兼容
- **WHEN** 所选配置模板的 schema 或发行约束与主程序不一致
- **THEN** 系统 MUST 在任何设备或远程副作用前失败并要求使用匹配模板

### Requirement: 候选包必须隔离且可重复验证
候选装配 SHALL 写入独立、带版本和摘要的目录，MUST NOT 自动覆盖或更新正式 `delivery/`。

#### Scenario: 生成现场候选包
- **WHEN** 操作员执行候选装配入口
- **THEN** 系统 MUST 创建新目录、生成 manifest，并完成模块导入、自检、静态资源、运行依赖和摘要验证

#### Scenario: 候选目录已存在
- **WHEN** 目标候选目录已存在且未显式选择新的唯一目录
- **THEN** 装配 MUST 失败而不是合并、覆盖或删除现有候选内容

### Requirement: 候选验证必须覆盖运行时完整性
候选验证 SHALL 通过候选自身的运行环境检查 Python/打包运行库、必需模块、静态资源、DLL 和声明的厂商 SDK；源工作树中的成功导入 MUST NOT 替代候选内验证。

#### Scenario: 候选缺少运行模块或DLL
- **WHEN** 候选包缺少启动或两种真实采集模式所需的模块、DLL 或静态资源
- **THEN** 验证 MUST 失败并给出候选内缺失项及修复来源

#### Scenario: 候选自检使用错误工作目录
- **WHEN** 候选只有依赖源仓库路径才能导入或启动
- **THEN** 隔离自检 MUST 失败，不得把源仓库依赖解释为候选完整

### Requirement: 正式delivery必须保持只读
准备、演练和候选装配前后 SHALL 对正式 `delivery/` 受管文件生成可比较摘要；未经独立发布授权，摘要 MUST 保持完全一致。

#### Scenario: 完成候选验证
- **WHEN** 候选装配、自检、耐久演练和证据导出结束
- **THEN** 报告 MUST 包含正式 `delivery/` 前后文件数和树摘要，并证明两者相同

#### Scenario: 正式delivery发生变化
- **WHEN** 任一准备动作导致正式 `delivery/` 受管文件增加、删除或改变
- **THEN** 交付门禁 MUST 失败并列出差异，且不得宣称候选准备完成

