# Proposal

## Why

PLC、ABB 和 BSV UVC 已连接到采集电脑，但真实采集界面仍将其判定为无法连接：物理接口绑定会覆盖设备协议端点，ABB 驱动使用了控制器不接受的认证方式，UVC 的 Windows 设备身份、USB 拓扑、驱动绑定与厂商 DLL 打开结果也没有形成一致证据。必须修复从物理发现、协议探测到持续采集的完整链路，才能把“端口存在”与“能够稳定采集有效数据”区分开并形成可验收结论。

## What Changes

- 分离逻辑传感器的协议端点与物理接口绑定；选择或自动分配网卡、USB 端口时不得改写 PLC/ABB 的目标地址或 UVC 的设备档参数。
- 使 PLC 与 ABB 只共享经过目标网段、源地址和直连路由验证的工业网卡，并保留人工选择；不得自动选择 APIPA、WLAN、VPN、TUN、代理或回环接口。
- 保持 PLC Modbus TCP 的严格响应校验，并将温度、压力、张力三个通道的有效值、新鲜度和错误阶段作为独立就绪证据。
- 为 ABB RWS 实现设备实际要求的 HTTP Digest 认证及可诊断错误分类，验证控制器、机械单元、坐标系、X/Y/Z 和线速度语义。
- 依据 BSV UVC 的 VID/PID、Windows 设备接口和 USB 位置建立稳定的物理端口关联，明确报告 `usbvideo`、WinUSB/libusb 或厂商驱动兼容状态；只有厂商 DLL 成功打开设备并连续取得有效温度矩阵时才判定就绪。
- 统一真实接口检查、采集启动和运行状态中的分层状态码，保留物理枚举、驱动、认证、协议、通道、数值、新鲜度和连续性证据，不再把已知失败投影为笼统的未连接或未检查。
- 增加无硬件协议夹具、前端绑定回归、Windows 设备发现回归、交付装配一致性检查，以及 PLC、ABB、BSV UVC 实物逐项和并行持续采集验收。

## Capabilities

### New Capabilities

- `plc-abb-uvc-real-acquisition`: 定义 PLC、ABB 与 BSV UVC 的物理接口识别、协议端点保持、协议读取、分层就绪、持续采集和现场验收合同。

### Modified Capabilities


## Impact

- 权威实现：`visualization_app/acquisition.py`、`visualization_app/real_acquisition.py`、`visualization_app/windows_usb_topology.py`、本地 helper 与相关状态投影。
- 前端：`visualization_app/static/app.js` 的物理接口候选、自动/人工绑定、端点编辑和检查结果展示。
- 原生依赖：`BsvUvcNative.dll`、`libusb-1.0.dll` 及 BSV UVC 所需 Windows 驱动绑定；驱动切换必须作为受控现场步骤，不得由应用静默执行。
- 测试与门禁：设备协议夹具、USB 拓扑测试、真实采集测试、Harness 回归矩阵、候选装配与现场验收证据。
- 发布：交付完成前必须将已验证且获得用户授权的软件更新到唯一运行目录，从该目录启动并核验实际进程、运行修订、文件及页面资源摘要与权威源一致，完成授权接口实读和采集保存复验；源码或候选通过不能替代正式交付，软件更新不能替代三台实物的最终现场验收。
