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
