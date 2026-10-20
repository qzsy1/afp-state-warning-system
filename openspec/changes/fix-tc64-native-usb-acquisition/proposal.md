# Proposal

## Why

慧谱 TCP64 的实际 USB 接口尚未定位：单独拔下及插回其 USB 线时，Windows 当前 USB 和串口目录未发生变化；用户确认现有 COM5 属于应变片，COM6 的 FTDI 设备不能据枚举顺序认定为热电偶。需先验证物理数据连接和 Windows 枚举，再确认 USB 协议及真实温度读回；商家公开 Modbus 协议仅明确内部通讯 1 的 RS232 通路。

## What Changes

- 统一本机页面、采集核心与 Helper 的 TCP64 串口候选、协议选择和设备验证行为；通用 USB 串口仅代表候选，不代表已确认的热电偶。
- 保持原生 USB 接线目标，先通过受控拔插确认实际枚举设备；无设备变化时排查数据线、通信接口和仪表 USB 模式，枚举成立后再以仪表实际设置和厂商依据确认可用协议。若 USB 仅支持 PC 模式，取得可验证协议或 SDK 后接入独立适配器。
- 修复按枚举顺序误绑其他串口和 Helper 协议匹配不到串口的缺陷，保留用户停用状态及手动绑定。
- 提供端口缺失、占用、无响应、协议错误、部分通道无效和验证过期的诊断；协议设置未知时保持未验证。
- 验证完整 64 路扫描、持续更新、断线恢复和原始保存，并将正式目录复验与设备最终验收分别设为交付条件。

## Capabilities

### New Capabilities

- `thermocouple64-usb-acquisition`: TCP64 原生 USB 的发现、候选绑定、只读协议验证、故障诊断与实机验收合同。

### Modified Capabilities

无。主规格库尚无已归档能力；本变更补充 `expand-thermocouple-to-64-channels` 的设备接入边界，不重复定义通道、均值、跨层补值和传输规则。

## Impact

- `visualization_app/acquisition.py`、`local_capture_agent.py`、`static/app.js`、`hp_thermocouple64.py` 及相关接口和协议测试。
- `windows_usb_topology.py` / `interface_transport_catalog.py` 的现有本机 USB 位置证据按需复用；COM 号、通用芯片和固定 USB 位置不能成为设备身份映射。
- 若需 PC 模式适配器，其依赖、位数和打包合同须经厂商协议或 SDK 确认；不默认安装驱动或替换 Windows 绑定。
- 本次只形成规划，不修改采集代码、仪表设置或固定运行目录。
