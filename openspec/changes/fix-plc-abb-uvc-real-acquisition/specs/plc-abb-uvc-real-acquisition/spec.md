# Spec Delta

## Purpose

本能力定义松下 PLC、ABB 机器人与 BSV UVC 测温热像仪从物理接口发现、协议身份确认到持续采集有效数据的统一合同，使真实采集能够准确区分接口存在、设备兼容、协议成功和数据就绪。

## ADDED Requirements

### Requirement: 物理接口绑定与设备协议端点必须分离
系统 SHALL 分别保存逻辑传感器、物理接口或端口、实时设备身份和协议目标端点。选择、自动分配、刷新或恢复物理接口时 MUST NOT 用网卡名称、USB 标签或端口标识覆盖 PLC、ABB 或 UVC 的协议参数。

#### Scenario: 为 PLC 和 ABB 选择工业网卡
- **WHEN** 操作员将 PLC 和 ABB 绑定到同一工业网卡
- **THEN** 系统 MUST 保留 PLC 的主机与端口以及 ABB 的 RWS 主机，且 MUST 仅更新两者的物理网卡标识、源地址和验证状态

#### Scenario: 刷新物理接口
- **WHEN** 已配置设备端点后执行接口重新发现、页面刷新或 helper 重连
- **THEN** 系统 MUST 保持设备端点不变，并 MUST 将端点变化视为需要操作员确认的新配置

### Requirement: 工业网卡选择必须依据设备路径
PLC 和 ABB 的自动建议 SHALL 优先保留人工或已保存选择；没有选择时，系统 MUST 依据目标地址、同网段 IPv4、Windows 最佳路由和物理网卡类型推荐工业网卡。APIPA、回环、WLAN、VPN、TUN 和代理接口 MUST NOT 自动成为工业设备的已验证绑定。

#### Scenario: 多块网卡同时存在
- **WHEN** 电脑同时具有 WLAN、代理虚拟网卡、APIPA 接口和与设备同网段的有线网卡
- **THEN** 系统 MUST 推荐实际承载 PLC/ABB 直连路由的有线网卡，并 MUST 显示目标地址、源地址和路由接口

#### Scenario: 人工选择与实际路由不一致
- **WHEN** 操作员选择的网卡与 Windows 到设备的实际源地址或最佳路由接口不一致
- **THEN** 系统 MUST 返回 `network_path_invalid`、保留人工选择供诊断并阻止正式真实采集

### Requirement: PLC 必须以完整 Modbus 数据证明就绪
PLC 就绪检查 SHALL 使用配置的 Modbus TCP 端点、Unit ID、寄存器映射、字节序、字序、倍率和单位读取温度、压力、张力。三个已启用通道都收到有限、新鲜且语义合理的数据后，PLC 才能进入 `data_ready`。

#### Scenario: PLC 三通道读取成功
- **WHEN** PLC 通过选定工业网卡返回身份、长度、功能码和寄存器数量均有效的响应，且温度、压力、张力均可按配置解析
- **THEN** 系统 MUST 分别报告三个通道的数值、单位、来源时间和质量，并 MUST 将 PLC 标记为 `data_ready`

#### Scenario: TCP 可连接但 Modbus 无效
- **WHEN** TCP 502 可连接但事务、协议、Unit ID、功能码、长度、寄存器数量或通道映射不符合配置
- **THEN** 系统 MUST 报告对应协议阶段与原始错误，MUST NOT 将 PLC 标记为已连接或数据就绪

#### Scenario: PLC 只返回部分通道
- **WHEN** 三个已启用通道中任一通道缺失、非有限、陈旧或语义验证失败
- **THEN** 系统 MUST 精确列出已通过和未通过通道，并 MUST 阻止 PLC 整体通过正式采集门禁

### Requirement: ABB RWS 必须协商控制器要求的认证
ABB 驱动 SHALL 根据 RWS 认证质询执行受支持的 HTTP 认证，不得在控制器要求 Digest 时仅发送 Basic。认证凭据 MUST 可配置且不得出现在状态、日志、诊断事件或保存数据中。

#### Scenario: 控制器要求 Digest
- **WHEN** ABB RWS 返回 Digest 认证质询且配置凭据有效
- **THEN** 系统 MUST 完成质询响应并继续读取目标资源，不得把首次 401 直接转化为无数据

#### Scenario: ABB 凭据无效
- **WHEN** Digest 认证完成后控制器仍拒绝访问
- **THEN** 系统 MUST 返回 `authentication_failed` 及不含凭据的响应证据，并 MUST 不将错误静默转换为 `no_data`

#### Scenario: ABB 资源路径无效
- **WHEN** 已认证但机械单元、目标资源或坐标系不受控制器支持
- **THEN** 系统 MUST 返回 `resource_mismatch` 或等价明确状态，并 MUST 保留 HTTP 状态和 RWS 错误摘要

### Requirement: ABB 必须验证坐标与线速度数据语义
ABB 就绪检查 SHALL 验证控制器身份、机械单元、坐标系及 X、Y、Z 数据。线速度 MUST 由两个具有来源时间的有效位置样本计算；首个样本不得被当作已经验证的速度数据。

#### Scenario: 静止位置读取成功
- **WHEN** 系统连续读取到有限的 X、Y、Z 且机器人保持静止
- **THEN** 系统 MUST 报告坐标及其来源时间，并 MUST 在取得第二个有效样本后报告接近零的线速度

#### Scenario: 执行受控运动
- **WHEN** 操作员按现场验收步骤让机器人沿已知方向进行受控运动
- **THEN** 系统 MUST 记录对应坐标变化、以毫米和毫米每秒输出，并 MUST 将结果与示教器或批准参考值比较

### Requirement: BSV UVC 发现必须关联 Windows 身份与物理端口
系统 SHALL 使用批准的 BSV 设备档、Windows 设备实例、VID/PID、设备容器和位置路径识别 UVC，并 SHALL 将实时相机端点关联到其实际 USB 物理端口。仅 DLL 存在、端口占用或通用相机名称 MUST NOT 构成设备已验证。

#### Scenario: 目标 BSV 设备已枚举
- **WHEN** Windows 枚举到符合批准设备档的 BSV UVC 设备并能解析其 USB 位置
- **THEN** 系统 MUST 返回设备身份、Windows 服务、驱动提供者和物理端口，且 MUST 不再把该实时端点列为位置未知

#### Scenario: 通用相机不匹配设备档
- **WHEN** 系统发现其他 USB Camera 或 FHD Camera，但其身份不符合 BSV 设备档
- **THEN** 系统 MUST 保留这些设备供诊断并标记为不兼容，MUST NOT 将其绑定到 BSV 温度接口

### Requirement: BSV UVC 必须显式验证驱动兼容性
系统 SHALL 在调用厂商采集 API 前报告目标设备的 Windows 驱动绑定及其与厂商 DLL 的兼容状态。应用 MUST NOT 自动安装、替换或回滚设备驱动；不兼容时 MUST 阻止正式采集并给出受控现场处理要求。

#### Scenario: 设备绑定通用视频驱动而厂商 API 需要其他绑定
- **WHEN** 目标设备使用的 Windows 服务不能被当前厂商 DLL 打开
- **THEN** 系统 MUST 返回 `driver_incompatible` 或等价状态，显示当前驱动与所需设备档，并 MUST 保留厂商打开返回码而不猜测未确认含义

#### Scenario: 现场批准驱动变更
- **WHEN** 操作员依据厂商资料完成受控驱动安装或绑定并重新发现设备
- **THEN** 系统 MUST 重新验证设备身份、物理端口和厂商 API，不得沿用变更前的检查通过状态

### Requirement: BSV UVC 必须以有效温度矩阵证明就绪
BSV UVC 只有在厂商接口成功打开设备、启动流并连续取得符合设备档的温度帧后才能进入 `data_ready`。检查 SHALL 验证帧尺寸、温度量程、换算规则、ROI 边界、有限数值和来源时间；标准视频预览成功不能替代温度数据。

#### Scenario: 连续温度帧有效
- **WHEN** 设备连续返回符合配置尺寸和量程的温度矩阵，且 ROI 内数值均可按设备档转换
- **THEN** 系统 MUST 输出有限的 ROI 平均温度、单位、来源时间与质量，并 MUST 将 UVC 标记为 `data_ready`

#### Scenario: 设备打开失败
- **WHEN** 厂商接口加载成功但打开、启动或取帧返回失败
- **THEN** 系统 MUST 报告调用阶段、返回码、最后错误码、设备身份和驱动状态，并 MUST 不把接口标记为就绪

#### Scenario: 只有标准视频帧
- **WHEN** Windows 或标准 UVC 软件能够显示视频但厂商温度矩阵不可用
- **THEN** 系统 MUST 标记视频能力与测温能力不同，正式温度采集门禁 MUST 保持失败

### Requirement: 就绪检查必须保留分层状态和具体错误
真实接口检查 SHALL 对每台设备分别报告物理接口、实时身份、驱动、网络路径或 USB 绑定、认证、协议、通道、数值、新鲜度和连续性。已知失败 MUST 保留规范状态码和原始证据，不得降级为笼统的 `unchecked`、`not_connected` 或空数据。

#### Scenario: 三台设备产生不同结果
- **WHEN** PLC 数据有效、ABB 认证失败且 UVC 驱动不兼容
- **THEN** 同一次检查 MUST 分别返回 PLC `data_ready`、ABB `authentication_failed` 和 UVC `driver_incompatible`，并 MUST 完成全部已启用接口检查

#### Scenario: 检查后配置变化
- **WHEN** 设备端点、物理绑定、凭据标识、设备身份、驱动状态或运行修订发生变化
- **THEN** 原检查快照 MUST 立即失效，正式启动 MUST 要求使用当前配置重新检查

#### Scenario: 检查通过后直接启动
- **WHEN** 操作员在同一执行位置使用相同设备配置完成检查并立即开始采集
- **THEN** 检查请求与启动请求 MUST 使用一致的执行位置字段和就绪指纹，正式启动 MUST 接受该有效快照；仅由页面在启动阶段补充默认执行位置 MUST NOT 造成指纹不匹配

### Requirement: 本机直连与远程 helper 必须使用同一设备合同

`local_direct` 与 `remote_helper` SHALL 使用相同的设备档、端点字段、物理绑定语义、状态码、通道名称、单位、质量和门禁。helper MUST 在设备所在电脑执行发现与读取，服务器不得用自身接口替代。

#### Scenario: PLC 和 ABB 与未标定 ESR 联合采集
- **WHEN** 正常方案启用 PLC、ABB 与符合 ESR 原生 USB 合同的原始 ADC 接口，显式禁用 UVC 等其他接口
- **THEN** 系统 MUST 分别验证三个启用接口，保留 PLC/ABB 网络和协议门禁，允许独立 ADC 名称及单位的仅采集会话；ESR 未标定不得报告为 PLC/ABB 通信失败，禁用接口不得冒充检查通过

#### Scenario: 两种执行模式读取同一夹具
- **WHEN** 本机路径和 helper 路径接收相同的 PLC、ABB 与 UVC 协议样本和错误
- **THEN** 两种路径 MUST 产生等价的规范通道、质量状态和接口检查结果

#### Scenario: helper 未返回设备证据
- **WHEN** 远程模式 helper 离线、版本不兼容或检查响应缺少设备身份和数据层证据
- **THEN** 远程真实采集 MUST 失败关闭，且 MUST 不回退到服务器本机设备

### Requirement: 三设备并行采集必须相互隔离并可恢复
PLC、ABB 和 BSV UVC SHALL 使用相互隔离且可停止的读取生命周期。任一设备超时、断开或重连 MUST 只影响其所属通道；其他设备、状态查询、持久化和停止流程 MUST 保持可用。

#### Scenario: UVC 在采集中断开
- **WHEN** 三设备正在采集且 UVC 被断开
- **THEN** UVC 通道 MUST 转为缺失或陈旧并进入有界重连，PLC 与 ABB 数据 MUST 继续采集和保存

#### Scenario: 设备重新连接
- **WHEN** 断开的设备以相同身份和有效配置重新出现
- **THEN** 系统 MUST 重新完成受影响层级的检查并恢复新鲜数据，且 MUST 不伪造断线期间的有效样本

### Requirement: 实物验收必须证明接口识别与持续数据采集
最终现场验收 SHALL 在当前候选运行程序上逐台验证 PLC、ABB、BSV UVC，再执行三设备并行采集。验收证据 MUST 绑定构建修订、设备身份、物理接口、端点、配置、开始结束时间、样本统计、错误统计和保存文件；自动化夹具不能替代实物结论。

#### Scenario: 单设备现场验收
- **WHEN** 分别检查 PLC、ABB 和 BSV UVC
- **THEN** 每台设备 MUST 达到 `data_ready`，PLC MUST 获得温度、压力、张力，ABB MUST 获得 X、Y、Z 及经两点验证的线速度，UVC MUST 获得经过参考温度核对的 ROI 平均温度

#### Scenario: 三设备十分钟并行采集
- **WHEN** 三台设备同时运行不少于 600 秒且统一输出频率配置为 10 Hz
- **THEN** 采集 MUST 正常开始和停止、保存到批准的 D 盘目录，并 MUST 满足全部启用通道有限值比例不低于 99%、按设备档新鲜度计算的最长无有效数据间隔不超过 2 秒、未解释协议错误为零、进程崩溃和无法停止为零

#### Scenario: 参考语义核对
- **WHEN** 现场使用 PLC 已知工艺值、ABB 示教器或受控运动、UVC 批准温度参考源进行核对
- **THEN** 三类数据 MUST 在验收清单批准的量程和公差内，且实际公差、参考值和比较结果 MUST 记录在证据中

#### Scenario: 保存数据对账
- **WHEN** 并行采集完成并终结文件
- **THEN** 接口样本计数、统一帧计数、逐通道质量统计和保存文件行数 MUST 可解释地对账，且每个通道 MUST 保持规定名称、单位和设备来源

### Requirement: 自动化与交付门禁必须覆盖已定位缺陷
合并前自动化 SHALL 证明网卡选择不改写设备端点、ABB Digest 质询可成功或明确失败、UVC 身份与物理端口可关联、驱动不兼容可诊断、三设备检查互不短路，并验证权威源与候选运行程序一致。

#### Scenario: 前端接口重新发现回归
- **WHEN** 测试提供多块网卡和多个 USB 候选并执行自动分配、人工选择与刷新
- **THEN** PLC/ABB 设备端点 MUST 保持不变，正确工业网卡与 UVC 物理端口 MUST 被稳定保存

#### Scenario: 协议错误夹具
- **WHEN** ABB 返回 Digest 质询或认证失败、UVC 返回打开错误、PLC 返回异常或不完整 Modbus 响应
- **THEN** 生产解析路径 MUST 返回对应规范错误并拒绝无效数据，测试 MUST 不以源码文本搜索代替执行路径

#### Scenario: 候选运行程序验证
- **WHEN** 权威源自动化通过并装配候选运行程序
- **THEN** 候选程序 MUST 通过相同接口与协议合同测试，且正式运行目录在发布前不得被测试过程直接修改

### Requirement: 软件交付必须与用户实际启动版本一致
软件交付完成前 SHALL 将已验证且获得用户发布授权的版本受控更新到 `D:\AFP_Runtime\AFP_Integrated_System_Modular`，从该目录重新启动并完成健康检查、已授权接口的实机检查及短时采集、停止和保存复验。权威源、发布清单、磁盘文件摘要、实际进程路径、运行修订及页面静态资源 MUST 对应同一交付版本；仅源码或临时候选通过 MUST NOT 被表述为正式软件已交付。

#### Scenario: 用户已授权更新固定运行目录
- **WHEN** 用户明确授权发布已验证的软件修复
- **THEN** 系统 MUST 核对切换目标与当前采集状态，受控更新唯一运行目录，启动该目录中的 EXE，并核验进程、文件、页面资源及接口实读证据一致后才能报告软件交付完成；复验失败 MUST 恢复切换前版本并保留明确失败结论

#### Scenario: 磁盘版本与运行进程不一致
- **WHEN** 文件已经更新但旧进程、旧页面资源或其他目录中的程序仍在提供界面或检查结果
- **THEN** 系统 MUST 判定交付未完成，并在授权范围内重新启动固定目录程序、重新加载页面和复验，MUST NOT 仅根据磁盘文件或候选测试宣称完成

#### Scenario: 发布尚未获得授权
- **WHEN** 源码与候选验证完成但用户尚未授权正式更新
- **THEN** 系统 MUST 明确报告正式运行目录尚未更新、软件尚未交付，不得擅自停止用户程序或替换正式目录

#### Scenario: 软件已更新但实物最终验收未完成
- **WHEN** 用户授权范围内的软件更新及复验完成，但参考语义、UVC 测温或三设备联合验收仍未完成
- **THEN** 系统 MUST 分别报告源码修复、候选验证、正式程序更新和实物最终验收状态，保留所有未完成项及失败门禁；软件更新 MUST NOT 改变原定三设备、参考核对和 600 秒验收标准
