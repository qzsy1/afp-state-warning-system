# Proposal

## Why

AFP 已集成慧谱 TCP64，但接口归属、协议通信和真实温度采集仍需验证。用户提供的 C# 上位机源码及附带原始报文给出了可复用的逐通道 Modbus RTU 读取链路，应将其通信行为整合到现有采集模块，以正确识别实际接口并持续采集真实数据为最终验收标准。

## What Changes

- 在现有 `huipu_tc64_modbus_rtu` 驱动内增加参考程序的逐通道温度读取方式：FC03，起始寄存器 `18 + (CH - 1) × 20`，每次读取 2 个寄存器，按低字在前的 float32 解码；以此作为默认兼容方式，保留显式选择的完整记录批量读取方式。
- 统一本机、页面和 Helper 的候选发现、明确绑定与只读验证，去除依赖固定 COM2 或串口排序的设备认定；实际 USB 归属和协议支持以现场证据确认。
- 将逐通道通信接入既有 64 路原子快照、通道质量、单位换算、八组均值、CSV/MySQL 和远端传输链路；补足参考程序未处理的首通道无效、异常帧、非有限数和配置失效边界。
- 保留用户启用状态和手动设置，持久化读取方式及已确认的通道设置；提供请求范围、收发计数、耗时、分类错误和可追踪的原始报文诊断。
- 验证真实接口、全部 64 路映射、至少 10 分钟持续采集、断线重连和保存一致性；正式目录复验与实物最终验收分别成立。

## Capabilities

### New Capabilities

- `thermocouple64-usb-acquisition`: TCP64 原生 USB 的发现、明确绑定、参考程序兼容采集、只读协议验证、质量诊断与实机验收合同。

### Modified Capabilities

无。主规格库尚无已归档能力；本变更补充 `expand-thermocouple-to-64-channels` 的设备接入边界，不重复定义通道、均值、跨层补值和传输规则。

## Impact

- 外部参考根目录：`D:\software\上位机\本机上机调试资料包-20260909\02-源程序\2-源码\运行版-无硬件模拟`；仅读取其通信源码和少量报文常量，不复制整个程序，不增加 C# 运行时依赖。
- `visualization_app/hp_thermocouple64.py`、`acquisition.py`、`local_capture_agent.py`、`local_direct_acquisition.py`、`real_acquisition.py`、`pre_sensor_config.py`、`static/app.js` 及相关协议、接口、持久化和传输测试。
- 复用现有 USB 拓扑、接口目录、单运行目录部署流程；更新现有 `docs/慧谱TCP64热电偶集成说明.md`。保持驱动标识、通道命名、数据 schema 和派生规则兼容。
- 本规划不授权修改仪表参数、部署或发布；不将参考源码和附带 CSV 视为当前 USB 实物验收证据。
