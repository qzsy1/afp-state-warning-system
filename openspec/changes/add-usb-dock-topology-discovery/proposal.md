# Proposal

## Why

当前接口发现只返回已枚举的串口、HID/UVC 设备和网卡，无法显示 USB Hub 上尚未插入设备的物理端口，也无法说明设备或网卡属于哪个拓展坞。Windows 还会把同一组物理 USB 插口分别暴露为 USB 2.x 与 SuperSpeed 伴随 Hub，若直接枚举会重复计算端口，因此需要建立可合并、可验证的 USB 拓扑模型。

## What Changes

- 增加 Windows USB Hub 拓扑发现，识别 Hub、下行物理端口、端口连接状态、所接设备及父子关系。
- 将同一拓展坞的 USB 2.x 与 USB 3.x 伴随 Hub 合并为一组逻辑物理端口，避免把三个插口重复显示为六个。
- 识别拓展坞内置 USB 网卡等固定功能，并将其显示为所属拓展坞的网口，而不是额外的外部 USB 口。
- 扩展接口发现契约，分别表达拓展坞、物理端口和已接设备，并为物理端口提供跨刷新稳定的标识。
- 接口选择界面按类型和拓展坞分组；USB 类传感器显示所有 USB 物理端口，包括空闲、已占用、断开和未知状态的端口；串口和网卡继续显示全部同类型接口，并在能够确定父级时标注其拓展坞端口关系。
- 允许操作员预选空闲 USB 端口，但真实采集仍必须通过兼容设备和有效数据检查；端口存在不得被当作传感器已验证。
- 本地采集 helper 在访问电脑执行同一拓扑发现并向授权页面返回结果；服务器硬件不得混入远程 helper 模式或访客模拟模式。
- Windows 拓扑 API 不可用或查询失败时，保留当前接口发现结果并返回可诊断的降级状态，不阻断现有串口、网卡、HID、UVC 和模拟采集流程。
- 保持 PLC、ABB、热电偶、薄膜压力和 UVC 五类逻辑接口、驱动协议、通道映射及 PLC/ABB 共用网卡规则不变。

## Capabilities

### New Capabilities

- `usb-dock-interface-discovery`: 定义 USB Hub 拓扑发现、伴随 Hub 合并、拓展坞端口与内置网口建模、同类型接口显示、远程 helper 边界及真实采集验证行为。

### Modified Capabilities

无。

## Impact

- 主要影响 `visualization_app/acquisition.py` 的接口发现契约、前端接口选择与状态展示、`visualization_app/local_capture_agent.py` 的 helper 发现结果，以及对应单元测试、前端测试和 helper 边界测试。
- 预计新增独立的 Windows USB 拓扑模块，使用 Windows SetupAPI、CfgMgr32 和 USB Hub `DeviceIoControl` 查询；不增加必须联网的运行时依赖。
- 源码验证完成后，需要将相关外置文件同步到唯一发行版本 `delivery/AFP_Integrated_System_Modular_v2.0.4_Agentic`，更新缓存键、版本化校验和 `SHA256SUMS.txt`，并按现有 Git LFS 发布流程同步完整 v2.0.4。
- 非 Windows 平台和不支持拓扑查询的 Windows 环境继续使用现有扁平接口发现结果。
