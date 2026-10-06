# 公网预计算模拟演示实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将公网访客和公网已授权用户的模拟模式改为一次下载、浏览器本地 10 Hz 时间轴播放的预计算 synthetic 演示，使数据就绪后开始和停止即时响应，同时不改变真实采集与局域网实际模拟链路。

**Architecture:** Python 生成器从现有批准 synthetic CSV 构建带内容哈希的紧凑演示包和版本清单，包内包含五类逻辑接口、17 通道数据、预计算预测/健康/预警/聚合/诊断。独立 `public_demo.js` 提供可注入时钟的本地播放控制器；`app.js` 只负责角色路由、资源预取、现有图表 payload 适配和页面显隐。公网演示运行期不调用采集、helper、实时 WebSocket、状态轮询、CSV/MySQL、模型或 LangChain。

**Tech Stack:** Python 3.11 标准库、现有 `ThreadingHTTPServer`、原生浏览器 JavaScript/Web Crypto/Canvas、Node.js 合同测试、unittest/pytest、OpenSpec。

**Spec:** `openspec/changes/fix-remote-acquisition-workflows/design.md` 第 18 节；`specs/remote-simulation-source/spec.md` 的“公网模拟必须使用预计算浏览器演示”；`tasks.md` 第 22 节。

## Global Constraints

- 公网演示数据 MUST 明确标记为 synthetic/precomputed，不得描述为真实采集、实时推理或已确认缺陷。
- 演示包就绪后首帧 MUST 不超过 200 ms；停止冻结 MUST 不超过 100 ms，停止后至少 5 秒不得继续变化。
- 前台逻辑时间轴为 10 Hz，绘图为 2–5 帧/秒并直接合并到最新索引，不建立补画队列；600 秒逻辑误差 MUST 不超过 0.5 秒。
- 公网演示运行期 MUST 不调用采集启动/停止、helper、实时 WebSocket、状态轮询、CSV/MySQL、运行时预测模型或 LangChain。
- 局域网/客户端实验室 helper 实际模拟、`local_admin` 服务器模拟以及公网真实采集 MUST 保持现有行为。
- PLC、ABB、SMRF 热电偶、M3232 薄膜压力和 BSV UVC 五类接口的协议、驱动和通道映射不得修改。
- 只同步外置 Python/HTML/JavaScript 与演示资产；主程序和 helper EXE 不重建，交付前后 SHA-256 必须一致。
- 当前工作位于既有 `feature/original-ui-langchain-agent` 脏工作树；只编辑本计划列出的文件，不覆盖或回滚用户已有改动，也不自动创建包含其它改动的提交。

---

### Task 1: 固定当前公网模拟实时链路缺陷

**Files:**
- Modify: `visualization_app/test_frontend_guest_simulation.py`
- Modify: `visualization_app/test_public_web_security.py`
- Create: `visualization_app/test_public_demo_frontend.py`

**Interfaces:**
- Consumes: 当前 `simulationExecutionSelection()`、`startAcquisition()`、`stopAcquisition()` 和实时连接函数。
- Produces: 可观察合同，要求公网 `guest`/`authorized` 模拟都选择 `browser_precomputed_demo`，且开始/停止代码不触发服务器或 helper。

- [ ] **Step 1: 写路由失败测试**

  在 Node 合同测试中执行 `simulationExecutionSelection()`，逐项断言：`guest` 与 `authorized` 的模拟执行端为 `browser_precomputed_demo`；`lan_operator` 保持 helper 能力检查；`local_admin` 保持 server。

- [ ] **Step 2: 写运行期零网络失败测试**

  提取公网演示开始/停止函数，以会抛错的 `fetch`、`postJson`、`requestLocalHelper` 和 `WebSocket` 运行，断言开始与停止完成且调用次数为 0；同时断言旧代次回调不能改变停止后的游标。

- [ ] **Step 3: 运行 RED 测试**

  Run: `python -m unittest visualization_app.test_public_demo_frontend -v`

  Expected: FAIL，原因是 `authorized` 仍选择 helper、开始仍调用 `/api/simulation/start` 或 helper、停止仍等待保存。

- [ ] **Step 4: 更新冲突的既有测试预期**

  将只针对公网授权模拟的旧 helper 默认行为断言改为新浏览器演示合同；保留 LAN helper、真实采集、服务器备用模式原测试。

### Task 2: 生成版本化预计算演示包

**Files:**
- Create: `visualization_app/public_demo_bundles.py`
- Create: `visualization_app/test_public_demo_bundles.py`
- Generate: `visualization_app/static/demo/manifest-v1.json`
- Generate: `visualization_app/static/demo/quick_240.<sha16>.json`
- Generate: `visualization_app/static/demo/endurance_6000.<sha16>.json`

**Interfaces:**
- Consumes: `simulation_packages.py` 的批准包目录、`acquisition.NEW_COLLECTION_SENSOR_COLUMNS`、`SENSOR_CHANNEL_METADATA` 与 `SENSOR_INTERFACE_PROFILES`。
- Produces: `build_demo_bundle(package_id) -> dict`、`write_demo_assets(output_dir) -> dict`；清单项提供 `package_id`、`url`、`sha256`、`bytes`、`points`、`sample_rate_hz`、`schema_version`。

- [ ] **Step 1: 写包结构和敏感信息失败测试**

  对 240/6000 点包分别断言：恰好 17 通道、五类接口各自协议和通道不合并、每个通道包含等长 actual/predicted 数组、窗口健康/预警和诊断存在、包内不含盘符路径、密码、token、API key 或生产标识。

- [ ] **Step 2: 写清单/哈希失败测试**

  在临时目录执行 `write_demo_assets()`，复算每个清单 URL 的 SHA-256，断言文件名含哈希前 16 位、大小一致、只有相对 `/demo/` URL，且重复生成字节完全一致。

- [ ] **Step 3: 运行 RED 测试**

  Run: `python -m unittest visualization_app.test_public_demo_bundles -v`

  Expected: FAIL with `ModuleNotFoundError: public_demo_bundles`。

- [ ] **Step 4: 实现最小生成器**

  读取批准 CSV，以确定性公式预计算每通道预测、每 24 点窗口健康值和 normal/warning/abnormal 状态；写入接口元数据、层/试样聚合、演示诊断和生成规则版本。使用 `json.dumps(..., separators=(",", ":"), sort_keys=True)` 形成确定字节并计算 SHA-256。

- [ ] **Step 5: 运行 GREEN 并生成正式资产**

  Run: `python -m unittest visualization_app.test_public_demo_bundles -v`

  Run: `python visualization_app/public_demo_bundles.py`

  Expected: 两组测试通过；`static/demo` 只包含当前清单和两个带哈希 JSON。

### Task 3: 实现可验证的浏览器本地播放控制器

**Files:**
- Create: `visualization_app/static/public_demo.js`
- Modify: `visualization_app/test_public_demo_frontend.py`

**Interfaces:**
- Consumes: 已解析且校验通过的演示包及注入的 `now/setTimeout/clearTimeout/requestAnimationFrame/cancelAnimationFrame/render/onState`。
- Produces: `AFP_PUBLIC_DEMO.createPlaybackController(options)`，暴露 `load(bundle)`、`start()`、`stop()`、`setVisible(visible)`、`snapshot()`；`start()` 创建新 generation，`stop()` 先失效 generation 再取消任务。

- [ ] **Step 1: 写单调时间与合并绘图失败测试**

  用虚拟 `performance.now()` 从 0 推进到 600000 ms，断言索引直接为 `floor(elapsed/100)`、最后索引为 5999、只按 250 ms 级绘图，不为漏过的每一点排队。

- [ ] **Step 2: 写即时停止和代次隔离失败测试**

  开始后保存旧回调，调用停止并推进虚拟时间 5 秒，再手动执行旧回调；断言游标、渲染次数和状态不变。连续开始/停止/开始后断言只有一个活动定时任务且从索引 0 开始。

- [ ] **Step 3: 写后台暂停失败测试**

  `setVisible(false)` 后推进时间，断言游标冻结；恢复时调整起点而不是追赶旧样本。

- [ ] **Step 4: 运行 RED 测试**

  Run: `python -m unittest visualization_app.test_public_demo_frontend -v`

  Expected: FAIL，原因是 `static/public_demo.js` 不存在。

- [ ] **Step 5: 实现控制器并运行 GREEN**

  Run: `python -m unittest visualization_app.test_public_demo_frontend -v`

  Expected: 所有虚拟时钟、停止、快速重启和后台暂停测试通过。

### Task 4: 接入公网角色、页面和现有图表

**Files:**
- Modify: `visualization_app/static/index.html`
- Modify: `visualization_app/static/app.js`
- Modify: `visualization_app/test_frontend_guest_simulation.py`
- Modify: `visualization_app/test_public_demo_frontend.py`

**Interfaces:**
- Consumes: `/demo/manifest-v1.json`、带哈希演示包、`AFP_PUBLIC_DEMO` 播放控制器。
- Produces: `isPublicPrecomputedSimulationMode()`、`loadPublicDemoManifest()`、`preparePublicDemoBundle()`、`buildPublicDemoPayload(index)`、`startPublicDemo()`、`stopPublicDemo()`。

- [ ] **Step 1: 写资源准备与 SHA 校验失败测试**

  用完整 manifest/bundle fixture 调用加载函数，断言开始按钮在 ready 前禁用、哈希不匹配时保持禁用并显示错误、校验成功后显示版本/点数/10 Hz 并允许开始。

- [ ] **Step 2: 写 UI 边界失败测试**

  对 `guest`/`authorized` 公网模拟断言：按钮文字为“开始演示/停止演示”；helper、文件上传、硬件检查、保存、两个 MySQL、运行时模型和 LangChain 控件隐藏或禁用；页面展示五类逻辑接口和“预计算 synthetic 演示”诊断。对 `lan_operator`、`local_admin` 和真实模式断言现有控件恢复。

- [ ] **Step 3: 写现有图表适配失败测试**

  给定 bundle 和索引 23/24/5999，断言 payload 包含正确 cursor、17 通道 actual/predicted、窗口状态、层/试样聚合、未来预测及 synthetic/precomputed 标识。

- [ ] **Step 4: 运行 RED 测试**

  Run: `python -m unittest visualization_app.test_public_demo_frontend visualization_app.test_frontend_guest_simulation -v`

  Expected: 新增合同失败，既有 LAN/真实链路测试保持可辨识。

- [ ] **Step 5: 实现最小前端接入**

  公网模拟初始化时预取清单和默认包；开始仅启动本地 controller，停止同步取消本地代次，不调用 `postJson/requestLocalHelper/fetch/WebSocket`。`configureDataMode/loadRealtime/openLiveWebSocket/startLiveHttpFallback` 对公网演示直接返回。既有 `render()` 继续绘图，由适配器提供预计算 payload。

- [ ] **Step 6: 运行 GREEN 与前端完整回归**

  Run: `python -m unittest visualization_app.test_public_demo_frontend visualization_app.test_frontend_guest_simulation visualization_app.test_public_web_security -v`

  Expected: 全部通过；LAN helper 模拟与公网真实 helper 测试不变。

### Task 5: 为静态演示资产提供压缩和版本化缓存

**Files:**
- Modify: `visualization_app/app.py`
- Create or Modify: `visualization_app/test_public_demo_static_delivery.py`

**Interfaces:**
- Consumes: `STATIC_DIR/demo` 下 JSON 和浏览器 `Accept-Encoding`。
- Produces: 演示包响应的正确 `Content-Type`、gzip、ETag 和 `Cache-Control: public, max-age=31536000, immutable`；HTML/普通 API 保持原缓存策略。

- [ ] **Step 1: 写静态交付失败测试**

  对同一演示包分别以支持/不支持 gzip 请求，断言解压后字节相同、ETag 等于内容哈希、带哈希包使用 immutable 缓存；manifest 使用短期可验证缓存，普通 `app.js` 和 API 行为不被误改。

- [ ] **Step 2: 运行 RED 测试**

  Run: `python -m unittest visualization_app.test_public_demo_static_delivery -v`

  Expected: FAIL，当前 `_send_file` 不压缩且未返回缓存头。

- [ ] **Step 3: 实现演示资产专用响应**

  只对 `STATIC_DIR/demo` 应用 JSON gzip、ETag 与缓存规则；禁止路径越界并保留现有安全响应头。

- [ ] **Step 4: 运行 GREEN**

  Run: `python -m unittest visualization_app.test_public_demo_static_delivery -v`

  Expected: 压缩、哈希、缓存和普通静态文件隔离断言通过。

### Task 6: 性能、零网络与关键功能回归

**Files:**
- Modify: `visualization_app/test_public_demo_frontend.py`
- Modify: `openspec/changes/fix-remote-acquisition-workflows/evidence/verification.md`
- Modify: `openspec/changes/fix-remote-acquisition-workflows/tasks.md`

**Interfaces:**
- Consumes: 完整演示包、虚拟 1200 ms 网络延迟、浏览器播放控制器和现有回归套件。
- Produces: 可复查的开始/停止/5 秒冻结/600 秒漂移/零运行期网络证据。

- [ ] **Step 1: 执行确定性性能测试**

  用虚拟时钟验证首帧同步产生且小于 200 ms 合同、停止调用小于 100 ms 合同、停止后 5 秒冻结、600 秒误差不超过 0.5 秒、活动 timer/RAF 至多各一个。

- [ ] **Step 2: 执行受控 RTT 测试**

  将 manifest/bundle 准备阶段的 fetch 延迟设为 1200 ms，ready 后清零网络计数并播放；断言开始、停止和 10 Hz 索引不受该延迟影响，运行期请求数为 0。

- [ ] **Step 3: 执行针对性回归**

  Run: `python -m unittest visualization_app.test_public_demo_bundles visualization_app.test_public_demo_frontend visualization_app.test_public_demo_static_delivery visualization_app.test_simulation_packages visualization_app.test_frontend_guest_simulation visualization_app.test_public_web_security -v`

- [ ] **Step 4: 执行重要功能回归**

  Run: `python -m pytest visualization_app/test_interface_agent.py visualization_app/test_agentic_diagnosis.py visualization_app/test_local_capture_agent.py visualization_app/test_helper_transport.py visualization_app/test_server_target_mysql.py -q`

  Run: `openspec validate fix-remote-acquisition-workflows --strict`

- [ ] **Step 5: 记录证据并标记 22.1–22.6**

  只在相应自动化证据全部通过后勾选任务；第二台电脑和真实五类设备仍保持未完成。

### Task 7: 同步交付目录并验证在线入口

**Files:**
- Sync: `visualization_app/app.py` → `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/app/legacy/app.py`
- Sync: `visualization_app/public_demo_bundles.py` → `delivery/.../app/legacy/public_demo_bundles.py`
- Sync: `visualization_app/static/app.js`、`index.html`、`public_demo.js`、`demo/*` → `delivery/.../app/legacy/static/` 和实际运行的 `delivery/.../app/ui/`
- Modify: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/SHA256SUMS.txt`
- Modify: `openspec/changes/fix-remote-acquisition-workflows/evidence/verification.md`
- Modify: `openspec/changes/fix-remote-acquisition-workflows/tasks.md`

**Interfaces:**
- Consumes: 已通过测试的源码和生成资产。
- Produces: 源码、legacy 副本、实际 UI 副本逐文件相同；现有启动器从同一目录加载新版资源。

- [ ] **Step 1: 记录不可变基线**

  记录主程序 EXE、helper EXE、`acquisition.py` 五类接口段、模型和 MySQL schema 文件的 SHA-256。

- [ ] **Step 2: 精确同步外置文件**

  仅复制本计划实际修改和生成的文件，不复制 runtime、日志、数据库、凭据、令牌、DPAPI 文件或其它工作树改动。

- [ ] **Step 3: 更新并验证交付哈希清单**

  运行既有 `--verify-files` 或等价校验，要求 0 missing、0 mismatched、0 malformed；逐文件比较源码、legacy 和 UI 副本。

- [ ] **Step 4: 重启现有交付进程并做本机烟测**

  验证回环、实际 LAN 和公网 `/api/health`；请求 manifest 和两个演示包，核对 gzip/ETag/SHA；浏览器验证 ready 后开始、停止、快速重启和 5 秒冻结。不得重建或替换两个 EXE。

- [ ] **Step 5: 最终回归与任务状态**

  再运行 Task 6 的全套命令、`git diff --check` 和秘密扫描；自动化与本机证据通过后标记 22.7，只有真实第二台电脑实际验收后才标记 22.8。
