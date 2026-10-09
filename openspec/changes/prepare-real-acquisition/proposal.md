# Proposal

## Why

现有真实采集链路已经具备设备检查、统一采样、远程 helper 和保存的基础能力，但权威源与 delivery 存在漂移，若干协议、跨进程状态、逐帧质量和正式保存门禁仍可能在首次实物联调时造成阻断或产生看似正常但无法追溯的采集结果。现在需要在不破坏仿真、10 Hz 采集及既有前后端合同的前提下，把可在无硬件环境提前消除的采集风险固化为可执行准备规范和失败关闭的 Harness。

## What Changes

- 建立“权威源修复、装配生成、临时目录验证”的发布纪律：只在 `visualization_app`、`modular_runtime`、`harness` 和 OpenSpec 中维护源实现，禁止把 delivery 当作产品源直接修改。
- 为装配过程增加源文件、导入、行为和 manifest 一致性验证，防止重新装配恢复旧实现或遗漏运行模块。
- 修复 helper 隔离硬件检查与正式启动之间的 readiness 快照传递，使 WebSocket 和 HTTP 路径使用相同启动门禁语义。
- 使用确定性 Modbus TCP 报文夹具验证事务、协议、Unit ID、长度、功能码、异常响应和寄存器数量，消除真实 PLC 读取边界歧义。
- 正式真实采集在保存根目录缺失、不可写或不可建立会话目录时失败关闭；只有显式工程预览模式可以无持久化运行并持续显示不可用于正式结论。
- 为本地、helper、WebSocket、服务器镜像和持久化统一逐帧身份与质量合同：`capture_uuid`、`frame_sequence`、帧时间、来源时间、年龄和质量必须可对账。
- 将真实采集明确为两种并列一等模式：`local_direct` 由主程序直接管理本机设备，`remote_helper` 由另一台 Windows 电脑上的 Helper 管理设备并通过 WebSocket/HTTP 受控传输；模式必须显式配置且不可混用。
- 提取两种模式共享的设备/通道定义、统一采样时钟、帧包络、质量、状态机、持久化和停止语义；模式专属适配层不得复制这些判断。
- 用相同输入验证两种模式产生等价规范化帧，使保存、展示和后续消费者不依赖帧来源。
- 为设备读取增加受控轮询、异常退避重连和可停止边界；为文件写入增加有界队列和高水位状态；为停止过程提供可观察的终结阶段。
- 在现有三级回归矩阵上新增真实采集准备 Harness，覆盖协议夹具、跨进程命令链、逐帧质量、保存失败、断线恢复、装配一致性和既有功能回归。
- 真实硬件和采用远程 helper 时所需的第二台 Windows 电脑仍保留为现场门禁；自动测试不得把这些项目伪报为已通过。

## Capabilities

### New Capabilities

- `real-acquisition-source-integrity`: 规定权威源、装配生成、delivery 只读边界及源代码到发布产物的一致性门禁。
- `real-acquisition-runtime-safety`: 规定 readiness 传递、协议读取、正式保存、逐帧质量、重连和停止终结的运行安全合同。
- `real-acquisition-preparation-harness`: 规定真实采集准备 Harness 的自动化层级、故障注入、回归保护、证据边界和现场待验证语义。

### Modified Capabilities

无。当前仓库尚无已归档主规格；本变更与进行中的 `harden-real-acquisition-pipeline` 和 `release-regression-harness` 保持需求编号和测试证据映射，不改变其已声明行为。

## Impact

- 权威业务源：`visualization_app/`。
- 装配与发布校验：`modular_runtime/assemble_modular_delivery.ps1` 及相关验证入口。
- 回归基础设施：`harness/config/`、`harness/engine/`、`harness/tests/` 和必要的测试夹具。
- 远程链路：helper 命令调度、采集代理、WebSocket/HTTP 传输和服务器镜像。
- 采集核心：共享通道/帧/质量/状态/持久化核心，本机直连适配层，远程Helper适配层及PLC协议驱动。
- 前后端合同：仅允许向后兼容地增加状态和元数据；既有字段、仿真流程和 10 Hz 正常流程必须保持。
- delivery：实现期间不得直接编辑；只允许在临时目录装配验证，正式发布产物更新应作为后续独立发布动作。
