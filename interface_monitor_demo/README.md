# AFP 接口状态监控与 LangChain 诊断 Demo

这是基于 `AFP_Integrated_System_M3232_v2.0.6` 接口契约制作的独立可视化 Demo。它不会修改或启动正式 v2.0.6 系统，不读取真实传感器，也不调用任何外部模型服务。

## 启动

双击 `start_demo.ps1`，或在 PowerShell 中运行：

```powershell
cd F:\AFP_Integrated_Modular_v2
.\interface_monitor_demo\start_demo.ps1
```

首次启动会在 Demo 目录内创建独立的 `.venv` 并安装固定版本的 `langchain-core`。浏览器随后打开：

```text
http://127.0.0.1:8770
```

如果 PowerShell 阻止双击脚本，可运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\interface_monitor_demo\start_demo.ps1
```

## 页面展示内容

页面直接展示五个接口：

1. SMRF 八通道热电偶：`温度1`～`温度8`；
2. 松下 PLC：`温度`、`压力`、`张力`；
3. BSV UVC 热像仪：`ROI平均温度`；
4. ABB 机器人：`ABB_X`、`ABB_Y`、`ABB_Z`、`线速度`；
5. M3232 薄膜压力：独立 `薄膜压力` 通道，默认 `COM8 / 115200`。

其中 PLC 的`压力`与 M3232 的`薄膜压力`是两个物理传感器、两个接口和两个数据通道，不会合并或互相替代。

## 演示步骤

1. 在“故障场景”中选择一个接口和异常类型；
2. 点击“注入异常”，观察对应接口卡从正常变为警告或严重异常；
3. 在右侧填写任意非空的演示 API Key 和模型名称，例如 `local-demo-model`；
4. 点击“运行 LangChain 本地诊断”；
5. 观察流程图依次点亮：确定性监控、标准事件、安全门控、LangChain 编排、结构化诊断；
6. 查看下方七步执行轨迹、异常证据、可能原因和处理建议；
7. 点击“全部恢复”清空异常和诊断结果。

建议重点演示“M3232 薄膜压力 → M3232矩阵帧解析失败”。最终结果应定位到`薄膜压力`，而不是 PLC 的`压力`。

## LangChain 思路

本 Demo 使用 LangChain Core 的 Tool 和 Runnable，把工业诊断过程拆成四个可审计工具：

```text
get_interface_config
        ↓
inspect_latest_observation
        ↓
lookup_fault_rule
        ↓
compose_diagnostic_report
```

异常发现由确定性监控程序负责。LangChain 不直接“盯传感器”，只在形成标准异常事件后组织配置、观测和规则，最终输出结构化报告。页面显示的七步时间线来自实际 Runnable 执行结果，不是预先写死的一张说明图片。

## API Key 安全边界

- API Key 输入值只存在于当前浏览器输入框；
- 页面仅向后端发送 `api_key_present: true/false`，不发送 Key 原文；
- 后端会主动拒绝包含 `api_key`、`token`、`secret`、`authorization`或`password`字段的请求；
- 页面刷新后 Key 清空；
- 模型名称仅作为演示标签，不加载模型、不访问网络。

## 真实硬件边界

所有接口状态、故障事件、证据、原因和建议均为本地模拟。该 Demo 不能证明真实设备已经连接，也不能证明 M3232 串口协议已经完成现场验证。

M3232 当前演示路径支持矩阵帧异常的诊断展示，但真实接入仍需捕获原始串口帧，核对实际 COM 口、115200/8N1、矩阵尺寸、包装字段、零点和标定规则。模拟诊断结果不得直接用于调整 PLC、机器人或工艺参数。

## 测试

```powershell
cd F:\AFP_Integrated_Modular_v2
& .\interface_monitor_demo\.venv\Scripts\python.exe -m unittest discover -s .\interface_monitor_demo\tests -v
node --check .\interface_monitor_demo\static\app.js
.\interface_monitor_demo\start_demo.ps1 -SelfTest
```
