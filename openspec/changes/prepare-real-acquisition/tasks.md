# Tasks

## 1. Harness基线与只读发布边界（已完成）

- [x] 1.1 在 `harness/tests` 先增加失败合同，锁定真实采集准备需求ID、四层检查、证据等级和profile映射，再更新 `harness/config/regression-matrix.json` 使合同测试通过
- [x] 1.2 为正式 `delivery/` 受管文件建立运行前后摘要测试，验证任何准备Harness执行都不会修改现有delivery
- [x] 1.3 增加临时目录装配执行器及单元测试，验证它使用显式临时目标、保留原始退出码并在装配失败时报告命令与日志
- [x] 1.4 增加自动化与现场证据边界测试，验证无五类硬件，或选用远程helper但无第二台Windows电脑时报告保持 `field_pending`

## 2. 权威源与隔离装配一致性（已完成）

- [x] 2.1 先增加会在当前源/产物漂移上失败的源完整性测试，列出运行模块、入口、静态资源和关键行为合同，并验证报告能指出只存在于delivery的项
- [x] 2.2 在不修改delivery的前提下，把真实采集运行所需模块和行为迁移到 `visualization_app/` 权威源，验证权威源测试与模块导入通过
- [x] 2.3 更新 `modular_runtime/assemble_modular_delivery.ps1` 的显式源清单和装配规则，验证临时候选包包含全部运行依赖且没有从当前delivery反向覆盖权威源
- [x] 2.4 为候选包增加导入、自检、前端资源和manifest摘要验证，确认临时装配测试通过且正式delivery摘要保持不变
- [x] 2.5 运行既有模块化运行时和Harness合同测试，验证装配改动没有破坏启动、LAN/公网入口与原EXE复用语义

## 3. Harness双模式与共享合同（先于软件实现）

- [x] 3.1 [Harness/RED] 增加合同测试，要求矩阵显式覆盖 `local_direct`、`remote_helper`、模式字段互斥、统一帧/质量合同、规范化输出等价、本机无Helper依赖、远程readiness/能力门禁、`field_pending` 和delivery不变性；运行并记录当前矩阵或实现的预期失败
- [x] 3.2 [Harness/GREEN] 最小更新需求ID、`profiles.json`、`regression-matrix.json` 和检查入口，使两种模式及共享合同都有阻断检查且不能以地址、端口或设备状态推断模式
- [x] 3.3 [Harness/GREEN] 更新报告语义，分别呈现本机设备故障与远程网络/Helper故障，并统一映射采集状态；无当前现场证据时始终输出 `field_pending`
- [x] 3.4 [Harness/GREEN] 运行全部Harness单元和合同测试，确认新增合同通过且正式delivery摘要与步骤1基线相同

## 4. 共享采集核心与统一通道合同

- [x] 4.1 [共享/RED] 增加配置测试：真实采集必须显式选择 `local_direct` 或 `remote_helper`，缺失模式、未知模式及模式专属字段混用均失败；模拟采集和旧调用方兼容行为保持
- [x] 4.2 [共享/GREEN] 最小实现正交的 `real_acquisition_mode` 解析、规范化与冲突校验，不根据端点或在线状态猜测模式
- [x] 4.3 [共享/RED] 增加统一帧测试：`capture_uuid`、绝对 `frame_sequence`、帧时间、逐通道值/单位/来源时间/年龄/`is_new`/质量必须同位，内存截断后绝对序号不重置，旧 `rows`/`timestamps` 仍可读取
- [x] 4.4 [共享/GREEN] 提取唯一的通道规范化、统一采样时钟、帧组装和质量判定核心；模式适配器只能提交共享样本/帧，不复制通道映射、单位或质量逻辑
- [x] 4.5 [共享/RED] 增加缺失、非有限值、陈旧、断线、恢复新值及单通道故障夹具，验证统一质量和采集状态映射且其他通道不被误停
- [x] 4.6 [共享/GREEN] 实现统一错误到状态/质量投影，并向后兼容扩展API和前端字段；保存、展示及后续消费者只读取统一帧或其兼容投影
- [x] 4.7 [共享/RED] 增加标准FC03分片、连续响应、短头/短PDU、异常码、错误身份、奇数字节数和数量不符测试，确认旧解析因超读或校验不足失败
- [x] 4.8 [共享/GREEN] 实现严格MBAP解析并运行PLC映射、字节序、缩放、真实readiness和采集完整性回归

## 5. `local_direct`本机直连适配层

- [x] 5.1 [local_direct/RED] 增加无硬件集成测试，使用模拟设备验证主程序直接执行检查、打开、读取、重连、关闭，且任何Helper/WebSocket/HTTP调用都会使测试失败
- [x] 5.2 [local_direct/GREEN] 将本机驱动访问收敛到独立适配层并接入共享核心，复用已有驱动能力，不重写已通过的设备实现
- [x] 5.3 [local_direct/RED] 增加必需接口opened/首帧门禁、瞬时异常、持续断线、有限超时、最小轮询、上限退避和停止超时测试
- [x] 5.4 [local_direct/GREEN] 最小实现本机门禁与恢复策略，分别报告设备打开/读取/重连/关闭故障并映射统一状态和逐通道质量
- [x] 5.5 [local_direct/回归] 运行SMRF、UVC、M3232、PLC、ABB接口合同、确定性10 Hz采样、模拟采集和旧单驱动调用回归

## 6. `remote_helper`远程适配层

- [x] 6.1 [remote_helper/RED] 增加实际 `HardwareCheckProcess → HelperCommandDispatcher → LocalCaptureAgent` 测试，确认当前主进程缺失可验证readiness快照时正式启动失败
- [x] 6.2 [remote_helper/GREEN] 实现readiness快照安全导出、导入和重新验证，覆盖配置指纹、设备身份、时间、接口/通道结果与秘密排除；WebSocket和HTTP复用同一路径
- [x] 6.3 [remote_helper/RED] 增加能力协商测试：正式模式缺少统一帧合同必须失败，显式工程降级持续禁用正式结论，Helper/服务器修订不匹配不得静默继续
- [x] 6.4 [remote_helper/GREEN] 最小实现统一帧能力协商与版本门禁，并保留旧Helper的明确工程兼容状态
- [x] 6.5 [remote_helper/RED] 增加ACK、批量、重复帧、同键冲突、帧缺口、断线重传、乱序和最终对账测试；修正10 Hz/50 Hz队列年龄与批次指标只以规范采样率字段计算
- [x] 6.6 [remote_helper/GREEN] 实现按 `capture_uuid + frame_sequence` 的幂等接收、缺口追踪、冲突失败和有界补传；网络/Helper错误与本机设备错误分别诊断
- [x] 6.7 [remote_helper/回归] 运行现有Helper、WebSocket、HTTP fallback、relay、远程镜像、服务器捕获日志、模拟源传输、配对、ACK、重连和停止回归

## 7. 输出等价、共同持久化与停止收尾

- [x] 7.1 [等价性/RED] 使用同一手工样本、虚拟时钟和故障时间线驱动两种模式，比较值、单位、绝对序号、时间和逐通道质量，确认当前输出尚不等价
- [x] 7.2 [等价性/GREEN] 让两个适配层在持久化前汇入同一统一帧入口，消除消费者按来源模式分支，并保持旧API/前端/`rows`/`timestamps`兼容
- [x] 7.3 [持久化/RED] 增加两种模式的空路径、不存在/不可写/过长路径、会话目录创建失败、慢写入、队列溢出、保存失败及MySQL不可用测试
- [x] 7.4 [持久化/GREEN] 在打开本机驱动或发送Helper启动命令前执行共同保存探针；实现有界写入队列、CSV/质量侧车/服务器/MySQL统一身份对账及显式无保存工程预览
- [x] 7.5 [停止/RED] 增加两种模式共同停止状态机测试，覆盖采样/远程传输停止、ACK补传、写入排空、文件终结、数据库同步、对账和各阶段超时
- [x] 7.6 [停止/GREEN] 实现共同停止收尾及明确降级，允许同时表达本地已保存、数据库待重试、阻塞设备或Helper和整体未完成；旧停止调用保持幂等兼容

## 8. 集成门禁与交付证据

- [x] 8.1 将新增测试加入quick/full/release矩阵，运行全部Harness单元/合同、两种模式各自无硬件集成、输出等价、协议、传输、持久化、恢复和隔离装配检查
- [x] 8.2 运行相关既有回归、quick和full，修复本变更引起的问题；任何既有失败按检查ID、原因和归因单独报告
- [x] 8.3 运行OpenSpec严格校验、Python语法/导入检查和 `git diff --check`，重新计算正式delivery摘要并与基线比较
- [x] 8.4 生成软件准备报告，列出RED→GREEN证据、自动化/协议模拟证据、OpenSpec任务进度、delivery不变性及仍需真实传感器或第二台Windows电脑完成的 `field_pending` 项目
