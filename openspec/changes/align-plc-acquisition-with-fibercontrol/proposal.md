# Proposal

## Why

AFP 与最新版 `D:\铺丝头\铺丝头上位机\程序\FiberControl_Fusion` 使用相同的 PLC 测量地址与浮点字序，但负压力处理、故障表示和采样组织不同，影响两个程序的数据比较。需要建立可追溯的测量兼容行为，并通过寄存器级对照区分软件处理差异与尚未确认的设备原因。

## What Changes

- 以最新版 `PlcAddressMap.cs`、`PlcManager.cs`、`PressureManager.cs`、`DataCollector.cs` 为只读参考，保留温度 DT28/29、压力 DT37/38、张力 DT23/24、float32 低字在前及两位小数。
- **BREAKING**：真实 PLC 主标量通道 `压力/N` 的有限负值按最新版处理为 0；原始两个寄存器、未裁剪解码值和处理依据保留在来源记录中，避免丢失负偏移证据。有效测量的显示、主 CSV、MySQL 和模型入口使用同一规范值与现有质量准入。
- 将默认真实 PLC 采集改为独立的 100 ms 周期，按温度、张力、压力分别读取；按实际完成时间记录来源，保留统一帧和单接口故障隔离。
- 保留 AFP 的严格协议校验与缺测质量语义，不将通信失败、NaN、无穷值替换为有效的 0 N；明确这是与参考程序有意保留的差异。
- 本机直采与远程 helper 使用一致的处理规则与来源证据；旧配置采用兼容默认值，已保存端点、站号、网卡绑定和显式映射保持有效。
- 增加可执行协议、数值、时间与保存对账测试；验证通过后按项目受控流程更新唯一正式运行目录。

## Capabilities

### New Capabilities

- `plc-measurement-compatibility`：PLC 三通道测量与参考上位机兼容的解码、负值规范化、采样、来源证据及跨执行模式一致性。

### Modified Capabilities

无。当前 `openspec/specs/` 没有已归档主规范。本能力与在途 `fix-plc-abb-uvc-real-acquisition` 的协议校验、网络路径和故障隔离合同衔接，不取代其范围及实物验收。

## Impact

- 主要涉及 `visualization_app/acquisition.py` 的 PLC 驱动与配置、`local_direct_acquisition.py` 的 PLC 专用调度与样本发布、统一来源元数据、helper 传输、前端压力说明、CSV/MySQL 来源记录及对应测试。
- 不增加 C#/.NET 生产运行依赖，不修改参考源码，不移植气缸、激光、伺服、PLC 写入、报警输出、ABB 运动或压力闭环。
- 八路跳辊及平均值是独立扩展项，本次不新增通道或改变模型输入列；薄膜压力、热电偶、应变、UVC、ABB 速度和模拟采集规则保持各自合同。
- 可能改变负压力历史消费语义与 PLC 读取负载，需要显式来源标记和回归测试。正常正压力持续偏移或固定倍率差异的现场根因仍未确认，不以本计划宣称已解决。
