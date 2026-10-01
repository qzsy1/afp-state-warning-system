# 验证记录

日期：2026-09-22（Asia/Shanghai）

## 自动化验证

- 核心回归：261 tests，全部通过，29.020 秒。覆盖 helper 传输/WebSocket/重连、远程镜像、浏览器上传、前端角色分流、公网安全、MySQL 作用域与诊断、Agent 诊断、本地规则以及远程状态路径脱敏。
- 采集与预测边界回归：46 tests，全部通过，1.932 秒。覆盖采集完整性、M3232、工艺参数和在线推理兼容。
- 全量源码测试：342 tests，1 error。唯一错误为 `test_app.DashboardTests` 缺少 4 个源码目录仪表盘生成物（该生成物不在本交付包内）；此前 NumPy 2.x 的 12 个健康指标错误已用等价单位间距梯形积分回退修复。相关健康指标、采集完整性、17 通道输入、M3232、在线推理、工艺参数和 MySQL 回归共 63 tests 全部通过；机器可读明细见 `full-test-results.json`。
- Python 编译与 JavaScript 语法检查通过。

## 交付目录验证

- `AFP_Integrated_System_Modular.exe --self-test`：通过（本次复核）；版本 2.0.3，9 个模块全部健康，MySQL/Excel/Tk 运行依赖可用，模型目录和健康指标工件存在。
- `AFP_Integrated_System_Modular.exe --verify-files`：通过；6480 个文件，0 missing、0 mismatched、0 malformed（本次复核完成）。
- `release-regression-harness` 尚未实现，按其规格执行组成入口：`--module-status` 退出码 0、9 个模块健康；`--integration-smoke` 退出码 0、`ok=true`；`--functional-smoke` 退出码 0、`ok=true`，采集 233 条样本，窗口/层/试样结果均生成，训练数据 30816 行、无非有限值。
- 主程序健康端点：`127.0.0.1:8770/api/health` 返回 `status=ok`。
- 新 helper 从交付目录启动并完成能力握手；`local_mysql_profile` 返回 `scope=helper_local, configured=false, state=missing`。
- helper 在无本机 MySQL 配置时执行预检，明确返回“本机 MySQL 未配置或凭据无法解密”，未回退为空密码。
- 交付 Web API 使用不存在的 `COM255` 执行 M3232 真实接口检查：117 ms、HTTP 200，接口进入 `not_connected`、薄膜压力进入非阻断 `no_data`，主程序未崩溃且未误报为有效数据。
- helper 本机 MySQL 预检命令经真实配对通道排队并返回 `ok=false` 与重新配置提示；采集/预测服务保持在线。
- 临时隔离 helper 配置路径测试验证新环境为 `missing`；模拟 DPAPI 解密失败后为 `decrypt_failed`，且没有空密码或服务器凭据回退。配对与跨会话隔离测试包含在 261 项核心回归中。
- `new_collection_health._integral` 增加 NumPy 1.x/2.x 等价的梯形积分兼容路径；不改变单位间距计算结果，健康指标回归及相关 63 项回归全部通过。

## 公网链路实测

- `https://desktop-410sfvi.tail97fe2c.ts.net/api/health`：40 ms。
- 公网 Bootstrap：276 ms，`interface_discovery.physical_interfaces` 为空，未触发服务器硬件发现。
- 浏览器模拟 CSV 上传：3 ms；不透明 `source_id` 长度 32；响应 `path` 为空；识别 12 个通道。
- 手填访问电脑路径：返回 `remote_path_not_accessible` 和专用中文提示。
- 公网模拟采集启动：31 ms；启动/状态/停止链路通过并保存 4 条样本；对三份响应递归检查，服务器绝对路径计数为 0。
- 公网访客本地诊断：17 ms，`model_used=false`，返回 1 条诊断；证据边界明确为软件流程验证而非硬件故障确认。
- LAN 操作员异步诊断：6 ms 返回冻结本地结果、后台 `job_id` 和 `model_pending` 阶段，不等待模型综合；本地结果 `model_used=false`。
- 交付实际 UI 复核：公网 `/` 返回 200 且包含 `edge-gateway-2`；公网 `/app.js?v=20260917-edge-gateway-2` 返回 200，SHA-256 为 `FB4F88A77585DF0B003BE66579152868599F2C8F7C36C3E3FAAC81D6C71372AF`，与 `visualization_app/static/app.js`、`app/ui/app.js` 一致，并包含模拟文件上传、本地 MySQL 保存、本地优先诊断逻辑。

## 传输压力验证

- 50 Hz、10 分钟虚拟时钟测试处理 30000 行；单在途 ACK 驱动，无丢失、无重复、无增长性积压，P95 延迟不超过 1 秒。
- 断线重连测试保持同一 `capture_uuid + sequence`，服务端幂等写入一次。

## 交付哈希

- 16 个源文件（14 个 Python、`app.js`、`index.html`）逐文件对比源码与交付副本，共 17 个映射副本（`app.js` 同步到 `app/legacy/static` 与实际运行的 `app/ui`），SHA-256 全部一致；其中 `guest_simulation.py` 最终哈希为 `13777E1C07B8D3EAF768D3419C1AFD8407FACA89886F83EB1A4B3FB3295540E8`。
- `SHA256SUMS.txt` 共 6480 项，包含交付目录中的不可变程序、模型、数据和说明文件；可变目录 `runtime/logs/updates/verification/rollback/__pycache__` 均未纳入。
- 对交付变更文件和说明执行凭据模式扫描：私钥、OpenAI 风格 Key、GitHub Token、AWS Key、URL 内嵌凭据和非空字面量密码命中数均为 0。
- 主程序 EXE（保持不变）：`AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`，56032416 bytes。
- 旧 helper：`845DCA5CDD37332D8E44F818B4FB65500C5E0A08AA12F5CFE8C5C51009D1EE9E`，91613768 bytes。
- 新 helper：`A743CF60E2AD768E1A599F17613C8BA439BD074E4A22D9157BEBFDA3BDF46564`，213071570 bytes。
- 新 helper 构建命令：`powershell -NoProfile -ExecutionPolicy Bypass -File visualization_app/build_local_capture_helper.ps1`。构建使用 Python 3.11.9 / PyInstaller 6.22.3；构建完成后因旧 helper 正在占用目标文件，停止两个旧 helper 进程并复制已生成的构建产物。

## 证据等级

- 自动化、本机交付启动、本机公网链路：已验证。
- 第二台电脑真实网络、DPAPI 用户上下文、本机 MySQL 实例与 10 分钟现场采集：待用户按 `SECOND_PC_ACCEPTANCE.md` 现场确认。

## 2026-09-23 模拟启动真实控制权回归

- 用户在授权公网页面启动模拟采集时复现 `409 real_control_required`。根因是后端统一门禁仅对模拟停止放行，模拟启动仍调用真实硬件控制租约检查。
- 新增 `test_authorized_simulation_start_does_not_require_real_control_lease`：修复前稳定失败并返回 `real_control_required`，修复后在无租约时返回 200，且硬件启动调用数为 0。
- 门禁现在仅将 `acquisition_mode=simulation` 的启动与停止作为会话内模拟控制放行；真实启动/停止、访客权限和其它受控入口保持原门禁。模拟停止租约过期、访客真实启动拒绝及请求幂等测试同时通过。
- 最终公网安全测试 66 项全部通过。期间一次完整测试出现 Windows 本地套接字 `WinError 10053`，对应权限用例随后独立重复 10 次全部通过，再次完整运行 66 项为 0 失败，判定为本机测试连接偶发中止而非业务断言回归。
- 源码 `visualization_app/app.py` 与交付 `app/legacy/app.py` SHA-256 均为 `E5421D588A4583A94912C335FC3A111529499EBEEF084F455ED6DEE414753D37`。主程序和 helper EXE 哈希保持 `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`、`A743CF60E2AD768E1A599F17613C8BA439BD074E4A22D9157BEBFDA3BDF46564` 不变。
- 主程序以 PID 50992 重新启动并加载交付文件；本机健康状态为 `ok`，公网 `https://desktop-410sfvi.tail97fe2c.ts.net/api/health` 返回 HTTP 200。`SHA256SUMS.txt` 共核验 6276 项，0 missing、0 mismatched、0 malformed。
- 重启后经公网域名新建独立访客会话实跑模拟启动、状态和停止：启动 70 ms，状态为运行且已采 7 点，停止 55 ms，生成 1 个保存文件。当前没有可复用的已授权浏览器标签页，因此该公网实跑只作为服务运行态证据；授权无租约 `/api/acquisition/start` 由进程内 HTTP 回归覆盖，仍需用户当前授权页面点击复验。
- 本轮证明后端授权会话逻辑、本机交付加载及公网可达；另一台电脑的浏览器会话、真实传感器、helper 本机 MySQL 和目标 MySQL 完整写入仍按未完成现场任务单独验收，不由本轮自动化替代。

## 2026-09-29 动态 LAN、WebSocket 与 helper 命令隔离

- RED 阶段新增测试先稳定捕获五个旧行为：缺少结构化推荐地址、Hyper-V 排序在物理网卡之前、LAN 不启用 WebSocket、HTTP 默认轮询低于 500 ms、硬件检查与控制共用单线程且握手无协议版本。实现后同一测试转绿。
- 核心前端/helper/诊断 156 项通过（2.766 秒）；排除因四个既有大型仪表盘生成物缺失而无法初始化的 `test_app.DashboardTests` 后，其余 28 个源码模块 394 项通过（55.512 秒）；模块运行时 20 项通过（0.182 秒）。全量发现共执行 400 项，唯一错误仍为上述测试数据缺失，并伴随既有 sklearn 1.3.2 模型在 1.8.0 下的版本警告。
- 运行态 `/api/network/status` 返回 `recommended_url=http://192.168.101.31:8770/`；物理 `以太网 3` 为推荐，`172.29.32.1` 的 `vEthernet (Default Switch)` 保留为非推荐候选。推荐逻辑不硬编码这两个地址，候选缓存最多五秒，页面每十秒刷新。
- 重启后 8770 与 8771 由同一 PID 35212 监听。LAN 健康接口 10 次最小/平均/最大为 0.92/1.35/3.09 ms；公网健康接口 10 次全部 200，最小/平均/最大为 9.29/42.89/303.94 ms。
- `ws://127.0.0.1:8770`、`ws://192.168.101.31:8770` 与 `wss://desktop-410sfvi.tail97fe2c.ts.net` 的模拟实时端点均完成握手，分别为 30.3/9.4/93.3 ms。公网实际加载的新 `app.js` 包含统一 WebSocket 与 500 ms 降级，不再包含旧 `usePublicLiveWebSocket` 分流。
- 公网新访客会话实际完成模拟启动、4 点进度和停止，三次请求均为 200，完整链路 767 ms；该证据验证公网服务和模拟启停，不替代真实 helper 50 Hz 或实物接口。
- 新 helper `--version` 输出 `20260929-isolated-check-v2 (protocol 2)`；阻塞检查子进程在测试中按 1 秒测试时限被终止，状态和开始命令在 0.5 秒断言边界内独立返回。生产总时限为 60 秒。主程序 SHA-256 保持 `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`；新 helper 为 `5B0740BA99B6645A1EB1AEF61B86EF8C6F8062D076A15C894E7FB16679E73059`，213075193 bytes。
- `SHA256SUMS.txt` 覆盖 6277 个非可变文件并通过验证：0 missing、0 mismatched、0 malformed。PLC Modbus TCP、ABB RWS、SMRF 热电偶 HID、M3232 串口、UVC 热成像接口代码及通道映射没有修改，相关接口/采集/M3232/预测/MySQL/公网安全回归均通过。
- 真实第二台电脑最新版 helper 握手、检查超时后控制响应、真实 50 Hz、MySQL 和五类物理设备保持待现场确认；不得由本机或公网模拟结果替代。

## 2026-09-29 检查超时诊断与本机数据库首次建库

- RED 阶段新增三个定向测试并分别稳定捕获旧行为：模型供应商异常会让快速诊断作业失败；helper-local 遇到 1049 未知数据库不会初始化；helper 检查超时/不完整结果会显示接口和通道 0/0 且没有诊断事件。修复后三个测试全部转绿。
- 相关完整回归 257 项、53.296 秒，全部通过；覆盖 Agent/LangChain、helper 传输与命令、前端模拟/真实分流、MySQL 诊断与作用域、五类接口、公网权限、原生集成入口。Python 编译、`git diff --check` 和 OpenSpec 严格校验同时通过。
- helper 检查异常现在按页面当前五类接口和已选通道补齐 `hardware_check_timeout/no_data` 事实，供本地诊断和模型综合使用；真实采集仍因 `ok=false` 被原门禁阻止，模拟采集原有豁免不变。
- 快速模型路径的供应商、工具或结构化响应异常现在返回 `failed_offline_fallback`，保留已生成的本地规则诊断并结束加载状态；默认模型调用次数仍为一次。
- helper-local 只有在用户明确点击首次检查、预检阶段为连接且错误为 1049/Unknown database 时才调用 `initialize_schema(create_database=True)`，随后重新执行表结构和回滚写测试。已有数据库缺表仍只执行建表；服务器目标库、1045、网络、服务、驱动和权限错误不进入自动建库分支。
- 交付目录七个运行副本与源码逐文件 SHA-256 一致；`SHA256SUMS.txt` 覆盖 6277 个非可变文件，`--verify-files` 退出码 0。主程序 EXE 未重建，SHA-256 为 `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`；helper 重建后为 `272FB53A4A622882E83CB86C5BDC3EDE13C7B4BBF745627BAEF65E17D5026001`，213078355 bytes，版本仍为 `20260929-isolated-check-v2 (protocol 2)`。
- 服务以 PID 32628 重启，8770/8771 由同一进程监听。本机与公网健康接口均正常；公网首页和脚本均加载 `20260929-lan-ws-helper-v3-diagnosis-mysql`，包含检查结果规范化和“模型增强诊断”文案。回环 WebSocket 握手为 8.5 ms，公网 WSS 握手为 49.6 ms；当前推荐局域网地址为 `http://192.168.101.31:8770/`。
- 本轮没有使用用户真实供应商 Key 发起模型请求，也没有代替第二台电脑创建其实际数据库；有效 Key/模型/公网出口、第二台电脑 MySQL 权限、真实 helper 与五类实物接口仍按现场验收项单独确认。

## 2026-09-30 helper 模拟预下载与固定链路低延迟回放

- RED/定向回归覆盖模拟源零字节、无有效表头/数值行、通道不兼容、helper 业务失败立即返回、旧采集在途批次隔离、预下载与开始拆分、内容缓存命中、1 MiB 有界分块、固定服务地址、ACK 事件唤醒和 LAN/公网自适应批量。定向 148 项全部通过。
- 排除因源码目录既有四个大型仪表盘生成物缺失而不能初始化的 `test_app.DashboardTests` 后，31 个测试模块及 `test_app.PoolingTests/LanServerTests` 共 449 项、48.432 秒，全部通过；覆盖五类接口、真实/模拟采集控制、停止保存、两个 MySQL 作用域、预测兼容、LangChain、本地 helper、WebSocket、公网权限与采集完整性。既有 sklearn 版本警告和一个测试 CSV 句柄警告未形成失败。
- Python 编译、`node --check static/app.js`、`git diff --check` 和 OpenSpec strict 均通过。`acquisition.py` 的改动只涉及模拟 CSV 流式读取和采集流活动通知，PLC Modbus TCP、ABB RWS、SMRF 热电偶 HID、M3232 串口与 UVC 热成像驱动/协议映射没有改写；预测和 MySQL 表结构文件未修改。
- 两个合成包通过实际 LAN API 下载并复算：快速包 54,577 字节/240 行，耐久包 1,365,578 字节/6000 行，声明 SHA-256 全部匹配。访客无权读取包目录；LAN/授权角色可见。
- 经验证的外置业务文件同步到 `app/legacy`，网页文件同时同步到实际运行的 `app/ui`；源与交付副本逐文件 SHA-256 一致。`SHA256SUMS.txt` 覆盖 6282 个不可变文件，主程序 `--verify-files` 退出码 0，0 missing、0 mismatched、0 malformed。
- 主程序 EXE 未重建，SHA-256 仍为 `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`。新 helper 版本为 `20260930-helper-prefetch-low-latency-v1 (protocol 2)`，SHA-256 为 `BB91B23FDC3994485B0305274267958902589B7DE449FBFEDCE530DCACE9BC20`。
- 交付服务以 PID 15924 重启并由同一进程监听 8770/8771。回环、实际 LAN `http://192.168.101.31:8770/` 与公网健康接口各连续 5 次均为 HTTP 200，平均约 1.4/0.9/6.3 ms；公网 WSS 实际握手 31.1 ms并收到 payload。公网首页已引用 `20260930-helper-prefetch-v2`，在线 `app.js` SHA-256 与源码完全一致。
- 公网独立访客会话实际完成模拟启动、2 秒后 21 点进度和停止保存，开始/停止约 50.6/52.3 ms。此结果验证服务器访客链路，不替代授权 helper 预下载、本机 MySQL、600 秒第二台电脑持续回放、真实 50 Hz 或五类实物接口。
- 任务 18.10 保持未完成：需用户在真实第二台电脑以本次 helper 分别经 LAN 与公网执行快速包和 6000 点/600 秒验收，返回行数、速率、序号、队列、P95、浏览器新鲜度和两个 MySQL 作用域的现场证据。

## 2026-09-30 公网高 RTT 吞吐与停止补传状态

- RED 阶段先稳定复现三类旧行为：10 Hz、1161 ms ACK 时公网批量上限 5 的理论吞吐只有约 4.3 行/秒；停止后待补传批次仍携带运行中状态且可能覆盖本地停止事实；页面把约 48 秒样本年龄误标为“浏览器发布”。修复后新增的吞吐、停止优先级、最终状态和前端指标测试全部转绿。
- 公网批量现在至少覆盖 `采样率 × (ACK RTT + 0.2 秒)`，并在队列年龄超过 2 秒时有界追赶，单批最大 50 行。仍保持单在途、服务端完整日志持久接受后 ACK、`capture_uuid + sequence` 幂等、断线重发及固定配对地址；没有增加 LAN 旁路或改变五类接口。
- helper 停止后立即冻结本地产生数，依次报告 `local_stopped_flushing` 和 `completed`。补传中的所有批次强制 `finalization_complete=false`，只有队列清空后的最终空状态批次可以终结服务器日志及目标 MySQL 保存；迟到的旧运行状态不能重新把页面变成“采集中”。
- 页面现分别显示队列预计秒数、ACK RTT、服务器墙钟差、样本端到端年龄、页面载荷更新频率和浏览器本机处理耗时；尚未测得的 `null` 延迟不会伪显示为 0 ms。在线公网 `app.js` SHA-256 为 `D8EE1950191862B0115642C9449968FF8FFEB7AD561D0E19B6BE855BE7438120`，与源码、`app/legacy/static` 和实际服务目录 `app/ui` 一致；旧“浏览器发布”字段已移除。
- 279 项 helper/模拟启停/前端/镜像/五类接口/MySQL/诊断高风险回归和 21 项模块运行时回归全部通过；Python 编译、JavaScript 语法、Git 空白检查及 OpenSpec strict 同时通过。全量发现此前执行 454 项，453 项通过，唯一初始化错误仍是源码目录缺少既有四个仪表盘预生成文件。
- 交付完整性核验 6279 项，0 missing、0 mismatched、0 malformed。运行状态目录 `app/runtime` 从不可变哈希清单生成规则中排除，避免 helper 配对状态变化造成伪校验失败；不改变运行数据内容或权限。主程序 EXE 保持 `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`。
- 新 helper 构建标识为 `20260930-helper-public-bdp-stop-v2 (protocol 2)`，SHA-256 为 `F59D63966F36B2980D2F80F941B4D3EBA2BFB8D4A46DF9DF3C880FD883336EBA`。本机、实际 LAN `http://192.168.101.31:8770/` 和公网 `https://desktop-410sfvi.tail97fe2c.ts.net/` 均返回 HTTP 200 并加载同一新版缓存键；公网脚本字节哈希与源码一致。
- 任务 19.6 保持未完成：本轮只完成可控高 RTT 逻辑验证、实际公网部署一致性和服务可达性，没有以真实第二台电脑完成 600 秒 helper 回放。用户现场仍需记录产生/接收/保存行数、队列峰值/最终值、停止阶段和端到端新鲜度，不能由本机自动化替代。

## 2026-09-30 目标 MySQL 配置标识兼容性修复

- 根因已定位为前后端配置契约不一致：页面在目标 MySQL 预检后会把非敏感 `mysql_target_config_id` 传给接口检查、工艺参数读取和采集启动，但 `AcquisitionConfig` 尚无该字段，三个直接构造入口因此返回 `unexpected keyword argument 'mysql_target_config_id'`。该问题不是采集代码被删除；源码和交付副本均存在。源码开发目录另有四个未受 Git 跟踪的仪表盘生成物缺失，属于既有测试夹具问题，与本异常相互独立。
- RED 测试稳定复现构造失败和 helper 配置字段边界；修复后 `AcquisitionConfig` 接受并规范化该服务器专用 ID，`helper_real_capture_payload` 和 helper 自身配置白名单均删除它。本机 MySQL 字段保持原值，服务器目标密码仍不进入浏览器或 helper。
- 新增进程内 HTTP 回归，授权会话携带该 ID 调用 `/api/acquisition/test` 与 `/api/acquisition/process-parameters` 均返回 200；采集启动使用同一数据类契约。没有修改 PLC Modbus TCP、ABB RWS、SMRF USB HID、M3232 串口、UVC 热成像五类协议/映射、MySQL 表结构、预测模型或 LangChain 诊断逻辑。
- 排除无法初始化的 `DashboardTests` 开发夹具后，全部 458 项源码测试在 50.715 秒内通过；21 项模块运行时测试通过。Python 编译、JavaScript 语法、Git 空白检查和 OpenSpec strict 均通过。生产交付已包含仪表盘数据和模型，公网页面烟测实际产生预测及预警结果。
- 源码与交付 `acquisition.py` SHA-256 均为 `491813DE4A3BFCAC1E34A220E49C921E41B477652EEC1218243E8E13B64B8E0A`；`local_capture_helper_entry.py` 均为 `7450ED27D7CF8734C36CB6406267614D52A9C900A796223D13F34DCBB8B7AB8A`。交付清单验证 6279 项，0 missing、0 mismatched、0 malformed。
- 主程序重启后，本机、实际 LAN `http://192.168.101.31:8770/` 和公网 `https://desktop-410sfvi.tail97fe2c.ts.net/` 健康接口均返回 `status=ok`。主程序 EXE 仍为 `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`；helper EXE 仍为 `F59D63966F36B2980D2F80F941B4D3EBA2BFB8D4A46DF9DF3C880FD883336EBA`，本次无需重新下载 helper。
- 公网页面实际保留五张接口卡及各自驱动；访客模拟采集约 2.5 秒达到 22 点，停止后固定为 115 点，实际采样 10.00 Hz、最大间隔 111.7 ms，17 通道实测、16 通道预测、预警与分层/完整试样保存均显示，浏览器错误日志为空。
- 任务 20.4 保持未完成：真实第二台电脑仍需在其已授权会话内复核目标 MySQL 预检后立即检查、模拟开始/停止和真实 helper 开始；五类物理设备与两类 MySQL 的实际连接/写入不能由本机模拟证据替代。

## 2026-09-30 模拟采集执行端元数据兼容性修复

- 用户页面复现 `AcquisitionConfig.__init__() got an unexpected keyword argument 'execution_host'`。数据流追踪确认页面把 `execution_host` 作为浏览器/服务器/helper 路由元数据加入启动载荷；远程授权服务器备用模式经 `GuestSimulationManager.safe_config` 已过滤该字段，但本机管理员服务器模拟和直接采集入口仍把整包载荷交给 `AcquisitionConfig`，因此在启动前返回 400。该异常与模拟 CSV 内容或文件删除无关。
- RED 回归以本机管理员 HTTP 路径携带 `execution_host=server` 稳定得到同一 400；修复后返回 200 并进入模拟采集。另一个保护测试确认未登记的 `unexpected_config_typo` 仍返回 400，证明实现没有采用“忽略所有未知字段”的宽松处理。
- 新增严格的 `acquisition_config_from_payload` 边界，只剥离显式列出的执行端、模拟选择和源传输元数据；接口检查、工艺参数、直接启动和服务器采集日志回放统一使用该边界。`execution_host` 没有加入 `AcquisitionConfig`，`mysql_target_config_id` 仍作为服务器配置身份保留。
- 排除既有四个源码仪表盘生成物夹具后，全部 462 项可运行源码测试在 62.518 秒内通过；21 项模块运行时测试通过。Python 编译、JavaScript 语法、Git 空白检查和 OpenSpec strict 均通过。一次较早的 221 项组合运行出现 Windows 测试 HTTP 套接字 `WinError 10053`，对应用例独立重跑通过，随后完整 462 项运行无失败。
- 源码与交付 `acquisition.py`、`app.py`、`server_capture_journal.py` 的 SHA-256 逐文件一致；`SHA256SUMS.txt` 共 6279 项并由主程序 `--verify-files` 以退出码 0 验证。主程序 EXE 保持 `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`，helper EXE 保持 `F59D63966F36B2980D2F80F941B4D3EBA2BFB8D4A46DF9DF3C880FD883336EBA`，本次无需重新下载 helper。
- 清理三个同路径旧实例后，以单一 PID 38364 重启交付服务，8770/8771 均加载新外置代码。携带 `execution_host=server` 的本机服务器模拟实跑 3 秒得到 31 点，随后停止为 `running=false`；实际 LAN `http://192.168.101.31:8770/api/health` 与公网首页均返回 HTTP 200。
- 任务 21.4 保持未完成：真实第二台电脑仍需分别复核 helper 默认模拟与明确服务器备用模拟；本机结果不替代异机 helper、两类 MySQL 或五类真实设备现场验收。

## 2026-09-30 公网预计算合成演示与即时启停

- 公网访客和已授权用户的模拟入口现统一路由到 `browser_precomputed_demo`；局域网/客户端实验室实际模拟仍走 helper，`local_admin` 服务器模拟和公网真实采集仍走原链路。公网演示页面只显示版本化 synthetic/precomputed 数据包和“开始演示/停止演示”，隐藏 helper、文件上传、硬件检查、CSV、两类 MySQL、运行时模型与 LangChain 控件。
- 确定性生成器产出快速包 240 点/24 秒/83,643 字节，SHA-256 `d164f0752b9cac2f472cae1c0bdd1e57d382c155270eaf90bfd1249bbfa762c3`；耐久包 6000 点/600 秒/2,006,954 字节，SHA-256 `cdf7aaf3e0c4bb5a0ff76753e9876c99691dd28da097bcf0025439605b72e5a6`。两包均为 10 Hz、17 通道并分别保留 SMRF 热电偶、PLC、UVC、ABB、M3232 五类逻辑接口元数据；测试确认不含凭据、绝对路径或生产实采声明。
- 浏览器本地控制器使用单调时间计算逻辑索引，250 ms 合并绘图，停止先失效播放代次再清理计时器，后台标签页暂停且不追赶。虚拟时钟测试确认准备阶段 1200 ms 延迟不会传入播放阶段，首帧同步生成且小于 200 ms，停止小于 100 ms，停止后 5 秒冻结，600 秒末索引为 5999、逻辑误差小于 0.5 秒，timer/RAF 有界且运行期网络计数为 0。
- 在线公网浏览器实际打开 `?acceptance=20260930v4` 后，数据包显示已就绪；开始后页面直接出现 17 通道曲线、预测/预警和分层结果。快速包停止于 224 点后等待 5 秒仍为 224 点；6000 点耐久包实际下载校验为已就绪，启停后停止于 32 点并等待 5 秒仍为 32 点。两次均为开始按钮恢复、停止按钮禁用，页面明确显示“未启动采集、保存、MySQL、运行时模型或 LangChain”，浏览器控制台无 warning/error。
- 静态交付测试确认带哈希 JSON 使用 ETag、gzip 与一年 immutable 缓存；公网快速包 GET 返回 HTTP 200、gzip 30,717 字节，解压后 83,643 字节且 SHA-256 匹配。回环、实际 LAN `http://192.168.101.31:8770/` 和公网域名读取同一快速包均为 HTTP 200、字节数与哈希一致；三条 `/api/health` 同样为 HTTP 200。
- 公网演示/前端/安全定向回归 174 项全部通过；五类接口、Agent/LangChain、helper 传输和目标 MySQL 定向回归 130 项全部通过；模块运行时 9 项全部通过。JavaScript 语法、Git 空白检查和 OpenSpec strict 均通过。全量源码发现运行 478 项，唯一错误仍为 `test_app.DashboardTests.setUpClass` 找不到源码目录中的 4 个既有仪表盘预生成文件；该夹具缺失早于本改动，交付目录的生产仪表盘数据和本轮相关测试不受影响。
- 源静态目录与 `app/ui`、`app/legacy/static` 两份交付副本共比较 22 个文件，0 不一致；构建脚本已固定以后同时同步两份静态资产。`SHA256SUMS.txt` 更新为 6293 项并由主程序 `--verify-files` 以退出码 0 验证。主程序 EXE 保持 `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`，helper EXE 保持 `F59D63966F36B2980D2F80F941B4D3EBA2BFB8D4A46DF9DF3C880FD883336EBA`，本次无需重新下载 helper；五类驱动、预测模型和 MySQL 表结构未修改。
- 任务 22.8 保持未完成：本机回环与实际 LAN 已完成健康/静态资产烟测，公网已完成实际浏览器启停烟测；真实第二台电脑仍需由用户验收公网即时开始/停止和长时播放。公网真实采集、两类 MySQL 与五类物理设备继续作为独立现场验收，不以预计算演示结果替代。

## 2026-10-01 公网模拟稳定渲染与真实设置隔离

- RED 测试先复现四个旧行为：预计算载荷没有固定坐标范围和接口成功状态；公网演示每 250 ms 进入完整 `render()`；接口/诊断区被整体隐藏；模拟态再次发现接口会把模拟目录写入真实接口快照。修复后对应 14 项公网演示前端测试全部通过。
- 演示包现按完整通道序列缓存固定显示范围；首次建立 17 张卡片后只更新现有数值节点和 Canvas，窗口概率及层汇总只在窗口边界变化时刷新。五类逻辑接口、17 个通道和预计算诊断显示成功，同时明确“未连接物理设备、未调用运行时模型或 LangChain”。
- 真实采集状态与模拟成功状态分离：进入公网模拟前保存真实检查结果，切回时恢复；模拟接口发现不再调用真实快照写入。该轮未修改采集 Python 后端、helper、MySQL、五类真实驱动、预测模型或 LangChain 后端。
- 公网模拟、数据包、静态交付、模拟包、真实/模拟界面与公网安全共 177 项回归在 Python 3.11 环境全部通过；`node --check`、OpenSpec strict 和交付 `--verify-files` 均退出码 0。
- 最终审计进一步消除了停止按钮的一次性完整重绘：停止帧也复用稳定增量渲染路径，并增加回归断言。实际公网域名加载缓存键 `20261001-stable-public-demo-v2`，浏览器实跑显示接口 5/5、通道 17/17、预计算诊断成功和 17 张通道卡片；首卡播放与停止后位置及尺寸均为 x=293、y=217、332.5×141，未发生布局位移。停止于 29/240 点后等待 5 秒仍为 29/240，状态与波形冻结，控制台 warning/error 为 0。
- 源文件 SHA-256：`app.js=6520E5F56EEF67FFEF7E22259D289D85812F7CE77EFF7E568E4D63B7CA6009FC`，`public_demo.js=5C5F4AF522A717D0BC2C899238B39F80F8D386C80116315E464BF509EAD8E721`，`styles.css=CC7164B9F9EE3360126315B85E84C34A9999ECB22002CB46C5953589097A7BBF`，`index.html=CDE0A46CAB80675176868DF810957AD5C862014EAA26B83F4664D1EEC07A9234`；两份交付静态副本与源码一致，清单仍为 6293 项。
- 主程序 EXE 保持 `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`，helper EXE 保持 `F59D63966F36B2980D2F80F941B4D3EBA2BFB8D4A46DF9DF3C880FD883336EBA`，本次无需重新下载 helper。任务 23.7 保持未完成：真实第二台电脑仍需复核公网视觉稳定性和已授权真实/模拟往返，演示成功状态不得代替真实硬件验收。
