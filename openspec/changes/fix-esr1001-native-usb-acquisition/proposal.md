# Proposal

## Why

ESR1001-A 原生 USB 使用专用 USB/TTL 协议，现有 AFP 应变驱动只支持 RS485 Modbus RTU，导致 USB 串口可以打开却无法通过接口检查或采集数据。修复目标是让软件正确关联实际接口、验证设备协议，并在软件内连续显示、采集和保存四通道数据。

## What Changes

- 为同一个 `esr1001_strain` 逻辑接口提供原生 USB 和 RS485 两种明确区分的协议配置；保留已有 RS485 配置和数据兼容。
- 增加原生 USB 115200-8-N-1 参数查询、应答校验、受控启停、流式分帧、四路有符号 24 位 ADC 解码和通道映射。
- 将设备发现、物理绑定、协议识别、原始数据可用和微应变标定状态分开；CH340、COM 号、供电灯或端口打开成功均不能单独证明设备就绪。
- 支持缺少标定系数时的独立 ADC 诊断和正常方案联合采集、显示和保存；正常方案按接口数据模式使用独立 ADC 通道，只有有效逐通道 K/B 才可生成单位为 `με` 的 `应变1～4`，不得把 ADC 冒充微应变。
- 贯通接口配置、统一就绪快照、后端驱动、本机直连、helper、界面、保存和发布合同，避免界面选中 USB 后被固定 RS485 档案覆盖。
- 将“正确识别接口并采集到数据”落实为正式程序内可重复执行的单设备检查、四路数据采集、600 秒连续性、保存对账和断线恢复验收；标定精度与多设备联采分别判定。

## Capabilities

### New Capabilities

- `esr1001-native-usb-acquisition`：ESR1001-A 原生 USB 的接口识别、协议驱动、原始数据与微应变语义、软件采集及实物验收。

### Modified Capabilities

无已归档主规格需要修改。`add-esr1001-strain-acquisition` 中的 Modbus 寄存器、CRC 和固件要求限定于 RS485 路径；本能力规定原生 USB 路径。两条路径共用逻辑接口和已有规范应变通道，不增加新的传感器数量。

## Impact

- 采集与配置：`visualization_app/acquisition.py`、拟新增的独立原生 USB 驱动模块、`local_direct_acquisition.py`、`real_acquisition.py`。
- 发现与展示：`windows_usb_topology.py`、`interface_transport_catalog.py`、`app.py`、`public_status.py`、`static/app.js`、`native_integrated_app.py`。
- helper 与交付：`local_capture_agent.py`、`pre_sensor_config.py`、`pre_sensor/config`、`pre_sensor/release-contract.json`、模块驱动清单及现有装配脚本。
- 验证：原生 USB 协议夹具、接口误识别、配置往返、就绪失效、采集保存和断线恢复测试，以及对应 Harness 回归项。复用 `pyserial`，不要求新硬件或改装 RS485 接线。
- 本变更只规划修复，不授权安装设备驱动、更改计量参数或发布正式软件；发布与实物验收按各自条件执行。
