# Proposal

## Why

UVC 的设备身份与 USB 端口能够枚举，Windows `usbvideo` 的扩展控制能够读取协议和流类型，但实际原始载荷与现有测温帧资料的适用性尚未确认，正式温度检查保持阻断。需要完成接口识别、真实温度矩阵、ROI和保存闭环，同时保留其他已正常采集的通道、数据语义和保存能力。

## What Changes

- 核对目标设备、实际 DLL 的 ABI、Windows 绑定与温度协议，修复打开/启动返回值判定、首帧等待、新帧识别和有界生命周期；设备档只填写有依据的事实。
- 在现有 PnP/USB 拓扑、独立采集线程和统一就绪合同中完成 UVC，不以通用相机画面、DLL 存在或重复旧帧代替温度数据。
- 实施前冻结当前已正常采集通道集合及配置、质量、源观测与保存基线；启用 UVC 后保留该集合及其已有指标，覆盖热电偶64路、PLC、ABB、ESR1001-A和实际正常的其他接口。
- 完成 UVC 单设备、与全部基线正常接口联合的600秒采集、故障隔离、正常停止、再次启动及全部启用保存目标的逐通道对账。
- 集成验收名称为“UVC 相机正确识别接口并正常采集到数据”，数据仅指真实温度矩阵及AFP ROI温度通道。温度量值精度单独验收；没有参考温度、适用参数和允许误差时保持“精度未验收”。
- 贯通桌面/Web、本机/Helper 的配置、身份、就绪和温度通道，保留当前 `new_collection_v13_0`、历史方案及 ESR 原始ADC/标定模式边界。

## Capabilities

### New Capabilities

- `uvc-temperature-acquisition`: 目标 UVC 温度设备识别、批准后端、真实新温度帧、ROI、生命周期及与既有正常通道共同采集和保存的验收合同。

### Modified Capabilities

无。主规格目录暂无已归档能力。本变更细化 `fix-plc-abb-uvc-real-acquisition` 中尚未完成的 UVC 项及联合采集要求；原变更继续负责 PLC/ABB 和其原有三设备验收，不因本计划创建而勾选完成。实施时按本变更执行 UVC 专项，复用原合同，不另造冲突状态体系。

## Impact

- 驱动与采集：`visualization_app/acquisition.py`、`local_direct_acquisition.py`、`real_acquisition.py`、`windows_usb_topology.py`；按需要增加独立 UVC 模块，不重写无关设备协议。
- 界面与传输：`static/app.js`、`native_integrated_app.py`、`local_capture_agent.py`、现有就绪/公共状态、配置持久化及 Helper 能力合同。
- 依赖与验证：`hardware_dlls`、发布原生组件清单、UVC DLL/就绪/故障隔离测试、热电偶/应变/PLC/ABB回归、Harness矩阵与单运行目录发布门禁。
- 参考资料：只读核对现有上位机资料包的 `BsvUvcNativeTransport.cs`、`BsvUvcService.cs` 与同摘要 DLL，来源与适用性见设计；参考实现不替代厂商驱动批准或实物验收。
- 软件验证通过后按仓库既有授权自动更新唯一正式目录并复验；系统驱动或温度流配置变更须单独取得具体授权。源码、夹具或候选通过不能替代正式交付与实物验收。
