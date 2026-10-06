# SPEC 验证记录（2026-09-22）

## 本次反馈的验收映射

| SPEC 要求 | 代码与测试证据 | 本机结论 |
| --- | --- | --- |
| `remote-simulation-source`：停止并保存可观察、租约隔离、超时确认 | 前端阻止并发停止且显示保存进度；`test_frontend_guest_simulation` 与 `test_public_web_security` 覆盖模拟停止和真实租约保护；`test_guest_web` 实际启动/停止模拟并检查 CSV | 重启后公网浏览器点击保存 725 点，状态明确为“采集已停止（仅保存）”，刷新后的会话导出包含 6 个文件；浏览器本机目录未授权，不冒称已写入访问电脑 |
| `remote-simulation-source`：服务器路径不得出现在远程实时状态 | `test_public_web_security.PublicWebHttpTests.test_remote_simulation_live_and_status_hide_server_paths` 先复现原始路径泄露，再验证 `/api/simulation/live`、`/api/live?acquisition_mode=simulation`、`/api/acquisition/status?acquisition_mode=simulation` 的会话路径投影 | 重启后公网 50 Hz 模拟启动/停止/刷新接口只返回 `public_simulation/<session>` 和“管理员批准的模拟数据源”，无服务器绝对路径；浏览器保存结果同样为相对会话路径 |
| `remote-acquisition-transport`：五类接口独立 | `test_guest_web` 验证 PLC/ABB/热电偶/薄膜压力/UVC 的五组驱动及通道映射在模拟配置保留，实际数据读取仍为 SimulatorDriver；前端测试验证模拟源切换保留接口选择、回真实模式恢复绑定、连接检查不统一报告 `simulator` | 自动化通过；真实传感器实物连接待现场验收 |
| `mysql-scope-portability`：预检与停止保存一致、表单不被轮询覆盖 | `test_guest_web` 使用隔离伪数据库验证目标库停止写入和 CSV 保存；前端测试覆盖本机表单编辑、密码重输和不支持的远程模拟到 helper-local 数据库组合 | 自动化通过；无可用验收凭据，真实 MySQL 登录/写入待现场验收 |
| `remote-acquisition-transport`：非有限预测值不阻断实时推送 | `test_websocket_live` 先复现 WebSocket 严格 JSON 对 NaN/Inf 抛错，修复后验证与 HTTP 一样转换为 `null`；未修改预测运算 | 15:57 重启后的公网默认预测页显示实时数据已连接，不再出现 JSON 编码错误 |
| `remote-acquisition-transport`：授权网页五类类型目录 | 新增授权公网 Bootstrap HTTP 用例，先复现只返回空接口目录，再验证 PLC/ABB/热电偶/UVC/M3232 五种静态角色和驱动完整、物理清单仍为空且未探测服务器硬件 | 源码与交付副本回归通过；当前 15:57 的公网进程尚未加载新增后端修复，授权网页仍待再次重启与解锁后验收 |
| `remote-acquisition-transport`：访客本地回放时服务器进度可见 | 两个 Node 行为测试先复现本地回放从不查询服务器，修复后验证最多每秒一次状态请求、停止后迟到响应不覆盖结果 | 公网页面热更新后采集中点数从 208 增至 252，停止并保存 1311 点；本次管理员预设回放实际为 10.00 Hz，不当作真实 helper 50 Hz |
| `remote-simulation-source`：远程浏览器上传与手填路径拒绝 | 公网独立访客会话依次上传合成单 CSV、含两个子目录 CSV 的文件夹，并手填合成 `C:\client.csv` 访问服务器选择器 | 单 CSV 与文件夹上传均返回不透明标识、相对文件名及解析到的通道且不返回服务器绝对路径；手填路径 HTTP 400 并给出浏览器上传提示。浏览器文件系统授权操作本身待异机现场验收 |

## 回归与交付完整性

- `py -3.11 -m unittest test_guest_web test_public_web_security test_frontend_guest_simulation test_acquisition_integrity test_remote_mysql_setup test_mysql_visibility test_mysql_identity test_mysql_diagnostics test_mysql_credentials test_local_capture_agent test_helper_transport test_helper_websocket test_websocket_live test_interface_agent test_agentic_diagnosis -q`：302 项通过。
- `py -3.11 -m unittest discover -p 'test_*.py' -q`：共运行 361 项，唯一错误为 `test_app.DashboardTests.setUpClass` 缺少源码目录下四个预生成仪表盘文件；未将该错误算作通过。另有 sklearn 旧模型序列化版本警告（1.3.2→本机 1.8.0），未据此声称预测结果已重新标定。
- `openspec validate fix-remote-acquisition-workflows --strict`：通过。
- 本次更新后交付 EXE `--self-test`（18:11）、`--integration-smoke`（18:09）和 `--functional-smoke`（18:10）的 `runtime/*_result.json` 均为 `ok=true`；再次运行交付 `verify_files` 实现，6478 个文件无缺失、不匹配或格式错误。
- 交付 EXE 的 `--self-test`、`--integration-smoke`、`--functional-smoke`：`ok=true`；`--verify-files`：6478 个文件，0 缺失、0 不匹配、0 格式错误。
- 源码与交付副本的 `app.py`、`guest_simulation.py`、`static/app.js`、`websocket_live.py` SHA-256 相等；主程序 EXE 保持 `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`，helper EXE 保持 `A743CF60E2AD768E1A599F17613C8BA439BD074E4A22D9157BEBFDA3BDF46564`。
- 15:57 重启后公网预测页、保存范围提示已复验；随后新增授权远程接口目录修复仍需再次重启加载 Python 模块。前端本地回放进度修复已热更新，缓存键为 `app.js?v=20260922-remote-acquisition-3`。
- 本机端口 3306 正在监听，但 helper 本机配置元数据为 `scope=helper_local, configured=false, state=missing`；服务监听不等于有有效登录凭据，也不能把目标 MySQL 的账号密码推断为本机 helper 配置。

## 未完成边界

1. 运行中的服务器需要再次正常退出并重启，然后在授权真实模式下核对五类接口目录和 helper 实际物理接口；当前公网运行进程加载的是修复前的 Python Bootstrap。访客页面的五类显示不能代替授权页面验收。
2. 第二台电脑的 helper 配对、浏览器本机保存目录授权、真实 50 Hz 输入、五类实物传感器和该电脑 MySQL 凭据均待现场验收。模拟回放不能替代真实传感器 50 Hz 传输验收。
3. `tasks.md` 中 3.5、8.2、9.6、9.10 保持未勾选，直到相应全量环境/公网/真实数据库/授权网页验证具备证据。

## OpenSpec 三维核验（本轮）

| 维度 | 状态 | 证据边界 |
| --- | --- | --- |
| 完整性 | 48/52 任务完成；22 项要求、47 个场景已逐项对照 | 4 项待办见下；不归档 change |
| 正确性 | 新增授权 Bootstrap 与回放进度场景均先失败再通过；现有远程上传、MySQL、诊断、helper 传输及五类协议均有定向回归 | 302 项定向通过；真实异机硬件/MySQL 和授权公网 UI 未获得现场证据 |
| 一致性 | 仍由 helper 发现访问电脑实际端口；静态五类目录与真实设备发现分离，浏览器回放仅按低频状态请求观察服务器进度 | 未改传感器协议解析、模型计算、阈值、数据库表结构和主程序 EXE |

### CRITICAL：归档前必须处理

1. `tasks.md` 3.5：`test_app.DashboardTests` 所需四个预生成源数据缺失，需在受控基线下生成/提供，再跑完整类和本机管理员选择器；不可把其余 361 项计数误写成全通过。
2. `tasks.md` 8.2：公网受控真实 helper 50 Hz 持续输入、浏览器文件上传和诊断即时本地结果尚未完成整组链路测试；需要按验收清单执行并记录队列与延迟。
3. `tasks.md` 9.6：目标 MySQL 无有效验收凭据、另一台电脑 helper 本机库未现场预检；需以真实库连接与停止写入结果补证。
4. `tasks.md` 9.10：授权公网服务需重启加载 `app.py:4835-4836`，由用户解锁真实模式后检查五类类型目录并进行相关操作；当前访客五类界面不是该场景的证明。

### WARNING：代码与现场的区别

- 传输、上传、MySQL 与模型诊断在进程内/模拟和错误降级测试中有实现证据，不能据此宣称第二台电脑的设备发现、文件夹授权、账号权限或外部模型时延通过。
- sklearn 反序列化版本警告是已有模型环境兼容性信号；本变更没有重新训练或替换模型，现场预测数值可信度仍按原方案验收。

结论：有 4 项 CRITICAL 待补证，不归档、不宣称公网授权模式或异机全部功能已验收。
