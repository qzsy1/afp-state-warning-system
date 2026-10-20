# AFP 现场无传感器准备与运行手册

本手册使用固定单一运行目录 `D:\AFP_Runtime\AFP_Integrated_System_Modular`。源码位于 `D:\AFP_Integrated_Modular_v2`，证据位于 `D:\AFP_Runtime\AFP-Main-Evidence`，临时组装只允许位于 `D:\AFP_Runtime\.afp-staging`。不要在命令行、配置或证据中填写密码、Token、配对码或环境秘密。

## 1. 安装候选包到固定运行目录

使用 `manage-single-runtime-delivery.ps1 -Mode Stage` 在 staging 装配最新交付并完成 manifest、版本、Helper、`--verify-files`、`--self-test` 和 `--module-status` 门禁。失败时停止并清理本次 deployment 子目录，不得从其他目录补文件。

软件交付完成前，必须把已验证且获得用户发布授权的版本更新到固定运行目录，从该目录启动，并核对实际进程路径、运行修订、发布清单、磁盘与页面资源摘要一致，再完成授权接口实读、短时采集、文件终结与保存复验。仅源码或候选通过不能称为正式软件已交付；未获授权时明确说明正式目录尚未更新。软件更新与实物最终验收分别报告，未完成的参考核对、UVC 测温和三设备联合验收继续保持未完成。

切换前必须输出准确 PID、当前可执行路径、当前和目标身份、预计中断及回滚方法，等待独立的“切换审批”。未批准时不得停止进程、交换运行目录或修改公网源站。批准后也只能操作 inventory 锁定的 AFP 目标；服务门禁失败时恢复切换前目录。

启动项修改与旧目录删除不包含在切换审批内。旧候选、旧 delivery 和旧证据副本必须逐项生成清理预览，等待独立的“清理审批”；禁止通配符、父目录或工作区根删除。历史版本通过 Git 提交/标签和 Git LFS 在 staging 重建，再经过同一发布与服务门禁，不依赖长期保存多套运行目录。

第二台 Windows 电脑使用 `second-windows-helper.ps1 -Action install`；启动、停止、重启分别使用 `start`、`stop`、`restart`。系统策略只运行 `review-windows-remediation.ps1` 查看，实际更改必须另行获得管理员授权。

## 2. 运行传感器前预检

从固定运行目录运行 `run-pre-sensor-preflight.ps1 -Mode local_direct` 或 `-Mode remote_helper`。入口默认使用运行目录所在盘符下的 `AFP_Capture`，也可用 `-SaveRoot <短路径>` 明确指定；它会自动设置当前模式对应的保存/日志引用，并仅为尚未连接的物理接口建立预检占位，结束后恢复进程环境。远程模式的诊断占位不会冒充真实配对，第二台电脑未连接时仍为 `blocked`。目标 MySQL 可用时增加 `-PromptForMySQL`，只在本机安全提示中输入密码。保存 JSON 和 Markdown 报告。除真实传感器现场项可使用 `physical_sensor_pending` 外，其他项目只能是 `passed`、`failed` 或 `blocked`。

## 3. 选择模式

只选择一套配置：本机直连使用 `config\pre_sensor\local_direct.json`，第二台电脑 Helper 使用 `config\pre_sensor\remote_helper.json`。禁止混用两套模式字段。

## 4. 修复失败项

按报告中的 remediation 逐项处理。目标 MySQL、第二台 Windows、管理员权限、网络/防火墙和电源策略缺失属于环境阻塞，不是传感器待验证。修复后重新运行完整预检。

设备地址与实际物理接口必须分别核对。PLC 设备地址保留 Modbus TCP 主机与端口，ABB 设备地址保留 RWS 主机，BSV UVC 设备地址保留批准设备档端点；重新发现或选择网卡、USB 端口时不得把接口名称写入设备地址。只有串口设备允许所选 COM 端点更新设备地址。先执行接口重新发现，确认 PLC/ABB 的目标地址、实际源地址和最佳路由均指向所选工业有线网卡，再确认 UVC 的 Windows 设备身份、VID/PID、驱动服务和 USB 物理端口。随后执行立即检查；任何地址、绑定、设备身份或驱动状态变化后都必须重新发现并再次检查。出现 `network_path_invalid` 时保留当前选择供诊断，但不得启动正式真实采集。

本机直连和远程 Helper 都使用 `plc_abb_uvc_readiness_v1`。远程电脑的 Helper 必须在线，并在能力中返回 `unified_device_readiness_v1=true`；旧版或缺少能力的 Helper 必须停止在重新检查之前，不得回退到服务器电脑的接口。检查使用 `POST /api/acquisition/test`，正式启动使用 `POST /api/acquisition/start`。逐台确认接口阶段 `physical_binding`、`device_identity`、`driver`、路径或 USB、认证、协议、通道、`freshness` 和 `stability`；PLC、ABB、BSV UVC 只有最终状态均为 `data_ready` 才进入联合现场采集。`partial_channels`、`authentication_failed`、`driver_incompatible`、`device_open_failed`、`no_valid_frame` 和 `device_profile_incomplete` 均为阻断状态。

设备端点、物理绑定、凭据配置、设备身份、驱动状态、设备档版本或运行修订变化后，旧检查快照立即失效，必须重新发现并重新检查。程序只检测和报告 UVC 驱动，不得静默安装、替换或回滚 UVC 驱动。需要改变系统驱动时，先停止 AFP，核对经批准的厂商资料，向操作员说明预览/测温能力风险、当前驱动身份和回滚步骤，再由操作员在维护窗口执行；变更后重新枚举、重新检查，失败时按批准步骤恢复。

PLC 默认目标为 `192.168.125.5:502`，Unit ID 为 255，FC03 起始寄存器为温度 28、压力 37、张力 23，每通道读取两个寄存器并按低字在前的浮点映射解析。TCP 握手成功不等于三个通道就绪；必须逐通道取得有限新鲜值，并与已知工艺值核对倍率、单位和偏差。不得因现场值为零而补造样本，也不得把未经参考核对的寄存器数值作为最终验收结论。

ABB 默认目标为 `192.168.125.1`，只读资源为 `/rw/motionsystem/mechunits/ROB_1/robtarget?coordinate=Base&json=1`。凭据在接口的 `username`、`password` 字段配置，不在文档、日志或诊断证据中填写明文；账户只需读取相应 RWS 资源，不申请主控权或运动权限。会话协商 Digest 并显式绕过系统 HTTP 代理。核对控制器、`ROB_1`、Base 坐标系及毫米单位；首个位置样本不能验证速度，两个有效位置按时间差计算的毫米每秒值仍须与示教器或批准受控运动核对。`authentication_failed`、资源错误、解析错误与 `transport_timeout` 分别处理，不能统称设备断线。

检查与重连前的 `network_path` 使用实际 TCP 连接源地址；系统 UDP 路由指向工业网卡仍可能存在 TCP 被 TUN 接管的情况。源地址属于 WLAN、APIPA 或 TUN 时返回 `network_path_invalid`，由操作员核对系统分流规则后重新检查，不自动改变系统代理或路由。浏览器检查请求超时只说明本次未取得完整响应；本机请求与 Helper 请求分开标明，超时生成的失败行不能证明各设备均存在协议故障。先核对正在运行的修订、页面当前设备地址、物理绑定和请求执行端，再读取逐接口协议结果。

PLC 与 ABB 的协议连接绑定到路径检查通过的工业 IPv4 源地址，并再次核对实际连接源地址；打开和重连不会退回未绑定的默认网络连接。ABB HTTP/HTTPS 请求使用同一绑定与 Digest 会话。

ABB 同一驱动实例复用内存中的会话 Cookie，结束时通过 `/logout` 只释放本客户端会话，不记录 Cookie 值。`session_limit_exceeded` 表示控制器报告会话已满，`session_close_failed` 表示本客户端退出未成功；不要清除其他客户端会话或通过不断重新登录重试。[ABB 官方会话说明](https://developercenter.robotstudio.com/api/rwsApi/index.html)要求复用 Cookie，[退出接口](https://developercenter.robotstudio.com/api/rwsApi/root_resource_logout_page.html)用于释放关联会话。

### PLC 独立采集配置

独立排查 PLC 时选择“仅采集”和“本机直连”，仅启用 PLC 接口及温度、压力、张力三个采集/保存通道。先选择实际网卡“以太网”，再确认设备地址仍为 `192.168.125.5:502`，工业 IPv4 为 `192.168.125.209`；设备地址不能填写“以太网”或其他网卡名称。重新发现或改变绑定后，再核对地址并执行立即检查。仅选三通道时不能套用要求其他传感器输入的预测模型；需要预测时另行选择已验证兼容模型与完整输入。

PLC 独立检查必须显示三个通道均收到有效数据，之后以 10 Hz 启动并保存到可写的 D 盘目录，结束时核对样本数、CSV 行数和质量侧车。其他设备未参与本次独立验证时，不将它们计为通过，也不据此形成三设备验收结论。PLC 参考值、单位和倍率仍须单独核对。

### PLC 与 ABB 只读复现入口

先核对拓展坞网口经交换机连接 PLC 和 ABB；工业网卡使用设备同网段的固定 IPv4，默认网关保持为空。翻墙软件可以继续运行，但到两个设备的实际 TCP 路径必须属于工业网卡；固定源地址并不能使无应答的设备成为已连接。不要修改 PLC 寄存器、ABB 运动或系统驱动。

在固定源码目录运行以下命令，使用权威生产驱动按 10 Hz 并行读取 PLC 与 ABB，独立打开并关闭连接共三轮，每轮 10 秒：

```powershell
.\.venv\Scripts\python.exe .\visualization_app\industrial_acquisition_probe.py --interface-id 'ethernet:以太网' --duration 10 --repeat 3
```

已包含该入口的候选或批准运行程序可执行：

```powershell
& '<运行目录>\AFP_Integrated_System_Modular.exe' --industrial-readonly-probe --industrial-interface-id 'ethernet:以太网' --industrial-probe-duration 10 --industrial-probe-repeat 3
```

仅验证 PLC 时，源码命令增加 `--role plc`，候选程序命令增加 `--industrial-probe-role plc`；该模式完全不检查或打开 ABB。证据中的 `requested_roles` 明确本次选择范围，不将未选设备记为成功。

证据默认写入已存在且可写的 `D:\AFP_Capture\diagnostics\fix-plc-abb-uvc-real-acquisition` 下独立的 `industrial-readonly-*\samples.json`，包含驱动内容摘要、运行修订、每轮端点、实际源地址、逐次读数、通道有限值比例、最长无有效数据间隔和错误。一个设备失败不会跳过另一个设备；任一设备未通过时退出码为 2。ABB 首个位置样本的速度为空，保留在统计分母中；不补零，不沿用旧样本。

`read_only_probe_passed` 要求每轮所选设备的每个必需通道至少取得两个有限样本、有限值比例不低于 99%、最长无有效数据间隔不超过 2 秒且无读取或关闭错误。它只证明本次只读读取复现；结果的 `reference_semantics_verified` 和 `field_acceptance_passed` 保持 false，不替代工艺参考核对、UVC 测温或三设备 600 秒联合验收。

## 5. 启动采集

先确认固定运行目录的摘要、模式配置和预检报告属于同一 candidate ID。远程模式还必须确认 Helper 在线、已配对、readiness 已导入且能力协商一致，再从主程序启动。模拟采集只能证明协议、队列、保存和恢复路径，不得写成真实传感器或真实双机通过。

当前正式采集配置为 `new_collection_v13_0`，包含77路原始传感器和64路热电偶。真实热电偶采集在设备型号、说明书、协议版本、接口与电气拓扑、采样率、量程、冷端补偿、故障码以及厂商通道映射完成核验前保持阻断；不要根据模拟数据推断硬件协议。历史8路文件仍可只读回放，不能作为实物采集入口。ESR1001-A 按实际连接选择原生USB/TTL（115200 8N1）或独立RS485（9600 8N1、默认从站地址1），不得与M3232共用串口。只有RS485需要核对A/B、终端条件及FC04 `0x0030` 固件；原生USB通过参数应答及四路有效ADC确认。CH340只是候选，必须选择实际端口，不自动遍历串口探测。没有合法K/B时选择独立 `esr1001_raw_adc` 诊断方案、`capture_only` 与 `raw_adc`，仅启用该设备，检查后启动、显示四路adc_counts、停止并逐帧回读CSV。标定微应变须匹配设备、桥路和增益，精度须使用已确认的参考输入及允许误差。

原始文件按顺序保存 `温度1`～`温度64`。派生视图将连续每8路严格取平均，形成8个组均值。仅当某层某通道整层没有有限值时，派生数据才用同一试样、同一通道的中位数填满该层；部分空行不补，原始数据不改写。实时派生只读取当前及此前完成层；试样结束后可用全部完成层重新计算。页面的完整64通道和平均8通道选项只控制显示。旧8路模型不能自动接收组均值。

## 6. 观察状态

持续观察77个原始通道的逐通道质量、绝对序号、当前配置目标速率下的有效频率、writer/transport 队列高水位、网络 ACK/重传、CSV/质量侧车、journal、MySQL 与 retry spool 状态。64路热电偶还需核对各组有效率、缺失/过期区间和协议错误计数；应变质量证据包括四通道观测数、最后有效时间、缺失/过期区间和协议错误计数。达到容量警告时正常停止并处理，禁止继续堆积。

接口状态为 `waiting_first_sample` 时等待首个有效样本；进入 `reconnecting` 时记录受影响设备、开始时间和重连次数，同时确认其他接口继续采集和保存。超过设备档新鲜度后通道进入 `stale`，旧值不得作为新的有限值保存；恢复点必须出现新的来源序号和 `measured_new`。状态变为 `failed` 时停止本次正式验收并保留已写入证据。降级期间不得把缺失或陈旧通道计入有效数据比例，也不得用其他接口的值填充。

## 7. 正常停止

桌面曲线沿用64路热电偶分组与其它传感器视图。每次开始通过 `start_request_id`、后台 `operation_id` 和独立的 `capture_generation` 关联；试样跨层可以使用相同 `capture_uuid`，启动代次不能相同。显示完成要求每个通道在本轮有两次有效新源观测且实际曲线经过显示帧；数值相同可以形成水平线，保持值、旧代次、空图和后台样本计数不能替代。折叠、滚动区域外及隐藏窗口的曲线不能作为可见绘制通过证据。

设备缓存中的本轮新源观测经过协议、稳定性及新鲜度检查，对应的值、接口、源序号、接收时间、请求及启动代次保存到 `*_start_observations.json`。这些诊断证据不替代原有曲线的可见绘制；不能从先前采集或初检补点。

ESR1001 的初检和重连使用保守等待。同一后端会话内检测有效，且参数应答完整匹配 `26016306040001086800000000ff` 时，正式启动使用有界策略：端口保护间隔 0.5 秒、匹配 QUERY 应答、从 QUERY 发送起至少 1.3 秒的 START 间隔，并继续等待本轮稳定新源帧。命令超时、身份、参数、标定和停止应答检查保持有效；取消会中断启动等待，STOP 仍须完成有界释放。策略及分段事件记录在 USB 证据中；其它参数档及故障重连不会继承此优化授权。USB 接收泵的轮询间隔上限为 2 ms，以及时读取独立源帧；仪表参数中的源频率和统一保存采样率各自保持配置含义。

端到端耗时从可信开始按钮输入事件的 `event.timeStamp` 开始，在当前窗口的 `performance` 单调时钟域计算，包含输入排队、请求前检查、网络请求、后台启动及绘制。`C_handler` 保留处理器进入时间，`C1` 为请求交给网络，`C2` 为本轮数据接收，`C3_all` 为全部通道绘制后的显示帧确认。服务器 `request_timing`、`capture_timeline` 和接口 `start_events` 属于服务器时钟域，只使用各自分段，不跨域相减。后台“全部通道启动耗时”与点击到显示耗时分别记录，严格三秒通过需端到端值小于 3000 ms，并由可见桌面帧证据核对。

初次设备准备与点击计时分别记录。相同后端会话内，未失效的检测证据可用于正常停止保存后的再次启动；层号和普通状态显示不改变设备身份。断线、故障、物理绑定、参数、标定及运行会话变化需要重新检查。检测版本用于拒绝延迟状态对新检测结果的误失效。启动仍须取得本轮真实观测，保存未完成或端口未释放不能开始下一次采集。

桌面测量工具为 `harness/tools/measure_click_to_curve.py`，只连接回环诊断端口，并通过 Windows 原生输入激活正式窗口按钮，保存输入身份、逐通道证明、时间线及实际桌面截图。诊断端口只在测量进程中临时启用。API、无头页面或函数测试只证明对应软件行为，不能作为桌面性能验收。全部 75 通道基准为 TC64 温度 64 路、PLC 3 路、ABB 4 路和 ESR1001 原始 ADC 4 路；原始 ADC 单位为计数，通信有效不等于微应变量值标定通过。

只执行一次停止并等待 `finalization_complete`。重复停止应返回相同结果。超时、断网或数据库不可用时保留工作文件/journal/spool，不得手工删除后宣称完成。

若返回 `stop_timed_out`，表示至少一个设备或写入线程未在声明上限内退出；此时 `finalization_complete` 必须为 false。不要强制覆盖保存文件或开始下一次采集，先停止 AFP 进程、保存诊断证据并按设备维护步骤恢复。

## 8. 数据库重试与最终对账

目标 MySQL 恢复后运行 `run-pre-sensor-preflight.ps1 -Mode local_direct -PromptForMySQL -RetryPendingMySQL`。入口先执行零残留事务探针；只有目标探针通过才调用既有幂等 retry，随后自动复跑完整预检。密码只输入一次且在子进程结束后从环境清除。确认待重试文件清零，再核对同一 `capture_uuid` 的帧数、绝对序号、时间戳、CSV、质量侧车、journal 和 MySQL。任一不一致均阻止下一次正式采集。

## 9. 导出脱敏证据

运行 `export-pre-sensor-evidence.ps1 -Source <报告文件> -Output D:\AFP_Runtime\AFP-Main-Evidence\<run-id>`。检查生成的 manifest 摘要和 `sanitized=true`；导出失败时不得手工复制含凭据、URL 用户信息、私有设备路径或环境变量的原始日志。

完成上述步骤后，只按 `real-device-only-checklist.json` 执行真实设备现场验证。该清单不得加入版本、配置、网络、存储、MySQL、Helper 部署或 Windows 设置问题。

原生USB基础实物验收使用已授权并从固定正式目录启动的AFP：在确认至少支持10帧/秒的四通道配置下，执行不含预检的600秒原始诊断采集，核对至少6000个源帧、四路观测数一致、最大间隔不超过0.25秒，无未说明的解析丢弃或溢出。停止后逐行用原始帧复算ADC并与源计数对账，再次检查确认端口释放。拔插恢复与占用故障单独验收；断线超过0.25秒后应显示过期，重新识别后恢复，缺口不补造。合法K/B下的600秒10Hz微应变、精度和多设备联采分别判定。
