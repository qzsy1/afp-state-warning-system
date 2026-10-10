# AFP 现场无传感器准备与运行手册

本手册只使用隔离候选包。不要复制到或覆盖既有正式 `delivery`；不要在命令行、配置或证据中填写密码、Token。

## 1. 安装候选包

将完整候选目录复制到现场电脑的短路径（建议 `D:\AFP-Candidate`），保持目录结构不变。运行 `AFP_Integrated_System_Modular.exe --verify-files`，失败时停止，不得从其他目录补文件。

第二台 Windows 电脑使用 `second-windows-helper.ps1 -Action install`；启动、停止、重启分别使用 `start`、`stop`、`restart`。系统策略只运行 `review-windows-remediation.ps1` 查看，实际更改必须另行获得管理员授权。

## 2. 运行传感器前预检

运行 `run-pre-sensor-preflight.ps1 -Mode local_direct` 或 `-Mode remote_helper`。保存 JSON 和 Markdown 报告。除白名单物理设备检查外，任何 `failed` 或 `blocked` 都会禁止正式启动。

## 3. 选择模式

只选择一套配置：本机直连使用 `config\pre_sensor\local_direct.json`，第二台电脑 Helper 使用 `config\pre_sensor\remote_helper.json`。禁止混用两套模式字段。

## 4. 修复失败项

按报告中的 remediation 逐项处理。目标 MySQL、第二台 Windows、管理员权限、网络/防火墙和电源策略缺失属于环境阻塞，不是传感器待验证。修复后重新运行完整预检。

## 5. 启动采集

先确认候选摘要、模式配置和预检报告属于同一候选版本。远程模式还必须确认 Helper 在线、已配对、readiness 已导入且能力协商一致，再从主程序启动。

## 6. 观察状态

持续观察逐通道质量、绝对序号、10 Hz有效频率、writer/transport 队列高水位、网络 ACK/重传、CSV/质量侧车、journal、MySQL 与 retry spool 状态。达到容量警告时正常停止并处理，禁止继续堆积。

## 7. 正常停止

只执行一次停止并等待 `finalization_complete`。重复停止应返回相同结果。超时、断网或数据库不可用时保留工作文件/journal/spool，不得手工删除后宣称完成。

## 8. 数据库重试与最终对账

目标 MySQL 恢复后执行既有 retry 操作，确认待重试文件清零，再核对同一 `capture_uuid` 的帧数、绝对序号、时间戳、CSV、质量侧车、journal 和 MySQL。任一不一致均阻止下一次正式采集。

## 9. 导出脱敏证据

运行 `export-pre-sensor-evidence.ps1 -Source <报告文件> -Output <新目录>`。检查生成的 manifest 摘要和 `sanitized=true`；导出失败时不得手工复制含凭据、URL 用户信息、私有设备路径或环境变量的原始日志。

完成上述步骤后，只按 `real-device-only-checklist.json` 执行真实设备现场验证。该清单不得加入版本、配置、网络、存储、MySQL、Helper 部署或 Windows 设置问题。
