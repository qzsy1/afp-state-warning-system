# AFP 现场无传感器准备与运行手册

本手册使用固定单一运行目录 `F:\new_AFP_exam\AFP_Integrated_System_Modular`。源码位于 `F:\new_AFP_exam\AFP-Readiness-Repo`，证据位于 `F:\new_AFP_exam\AFP-Main-Evidence`，临时组装只允许位于 `F:\new_AFP_exam\.afp-staging`。不要在命令行、配置或证据中填写密码、Token、配对码或环境秘密。

## 1. 安装候选包到固定运行目录

使用 `manage-single-runtime-delivery.ps1 -Mode Stage` 在 staging 装配最新交付并完成 manifest、版本、Helper、`--verify-files`、`--self-test` 和 `--module-status` 门禁。失败时停止并清理本次 deployment 子目录，不得从其他目录补文件。

切换前必须输出准确 PID、当前可执行路径、当前和目标身份、预计中断及回滚方法，等待独立的“切换审批”。未批准时不得停止进程、交换运行目录或修改公网源站。批准后也只能操作 inventory 锁定的 AFP 目标；服务门禁失败时恢复切换前目录。

启动项修改与旧目录删除不包含在切换审批内。旧候选、旧 delivery 和旧证据副本必须逐项生成清理预览，等待独立的“清理审批”；禁止通配符、父目录或工作区根删除。历史版本通过 Git 提交/标签和 Git LFS 在 staging 重建，再经过同一发布与服务门禁，不依赖长期保存多套运行目录。

第二台 Windows 电脑使用 `second-windows-helper.ps1 -Action install`；启动、停止、重启分别使用 `start`、`stop`、`restart`。系统策略只运行 `review-windows-remediation.ps1` 查看，实际更改必须另行获得管理员授权。

## 2. 运行传感器前预检

从固定运行目录运行 `run-pre-sensor-preflight.ps1 -Mode local_direct` 或 `-Mode remote_helper`。入口默认使用运行目录所在盘符下的 `AFP_Capture`，也可用 `-SaveRoot <短路径>` 明确指定；它会自动设置当前模式对应的保存/日志引用，并仅为尚未连接的物理接口建立预检占位，结束后恢复进程环境。远程模式的诊断占位不会冒充真实配对，第二台电脑未连接时仍为 `blocked`。目标 MySQL 可用时增加 `-PromptForMySQL`，只在本机安全提示中输入密码。保存 JSON 和 Markdown 报告。除真实传感器现场项可使用 `physical_sensor_pending` 外，其他项目只能是 `passed`、`failed` 或 `blocked`。

## 3. 选择模式

只选择一套配置：本机直连使用 `config\pre_sensor\local_direct.json`，第二台电脑 Helper 使用 `config\pre_sensor\remote_helper.json`。禁止混用两套模式字段。

## 4. 修复失败项

按报告中的 remediation 逐项处理。目标 MySQL、第二台 Windows、管理员权限、网络/防火墙和电源策略缺失属于环境阻塞，不是传感器待验证。修复后重新运行完整预检。

## 5. 启动采集

先确认固定运行目录的摘要、模式配置和预检报告属于同一 candidate ID。远程模式还必须确认 Helper 在线、已配对、readiness 已导入且能力协商一致，再从主程序启动。模拟采集只能证明协议、队列、保存和恢复路径，不得写成真实传感器或真实双机通过。

当前正式采集配置为 `new_collection_v13_0`，包含77路原始传感器和64路热电偶。真实热电偶采集在设备型号、说明书、协议版本、接口与电气拓扑、采样率、量程、冷端补偿、故障码以及厂商通道映射完成核验前保持阻断；不要根据模拟数据推断硬件协议。历史8路文件仍可只读回放，不能作为实物采集入口。ESR1001-A 使用独立RS485串口、9600 8N1、默认从站地址1；不得与M3232共用COM口。上电前核对A/B极性和终端条件，实物检查确认固件支持FC04 `0x0030`，再分别向CH1至CH4施加已知输入。

原始文件按顺序保存 `温度1`～`温度64`。派生视图将连续每8路严格取平均，形成8个组均值。仅当某层某通道整层没有有限值时，派生数据才用同一试样、同一通道的中位数填满该层；部分空行不补，原始数据不改写。实时派生只读取当前及此前完成层；试样结束后可用全部完成层重新计算。页面的完整64通道和平均8通道选项只控制显示。旧8路模型不能自动接收组均值。

## 6. 观察状态

持续观察77个原始通道的逐通道质量、绝对序号、当前配置目标速率下的有效频率、writer/transport 队列高水位、网络 ACK/重传、CSV/质量侧车、journal、MySQL 与 retry spool 状态。64路热电偶还需核对各组有效率、缺失/过期区间和协议错误计数；应变质量证据包括四通道观测数、最后有效时间、缺失/过期区间和协议错误计数。达到容量警告时正常停止并处理，禁止继续堆积。

## 7. 正常停止

只执行一次停止并等待 `finalization_complete`。重复停止应返回相同结果。超时、断网或数据库不可用时保留工作文件/journal/spool，不得手工删除后宣称完成。

## 8. 数据库重试与最终对账

目标 MySQL 恢复后运行 `run-pre-sensor-preflight.ps1 -Mode local_direct -PromptForMySQL -RetryPendingMySQL`。入口先执行零残留事务探针；只有目标探针通过才调用既有幂等 retry，随后自动复跑完整预检。密码只输入一次且在子进程结束后从环境清除。确认待重试文件清零，再核对同一 `capture_uuid` 的帧数、绝对序号、时间戳、CSV、质量侧车、journal 和 MySQL。任一不一致均阻止下一次正式采集。

## 9. 导出脱敏证据

运行 `export-pre-sensor-evidence.ps1 -Source <报告文件> -Output F:\new_AFP_exam\AFP-Main-Evidence\<run-id>`。检查生成的 manifest 摘要和 `sanitized=true`；导出失败时不得手工复制含凭据、URL 用户信息、私有设备路径或环境变量的原始日志。

完成上述步骤后，只按 `real-device-only-checklist.json` 执行真实设备现场验证。该清单不得加入版本、配置、网络、存储、MySQL、Helper 部署或 Windows 设置问题。
