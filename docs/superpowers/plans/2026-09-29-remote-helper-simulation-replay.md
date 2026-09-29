# Remote Helper Simulation Replay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让已授权公网/局域网用户把浏览器选择的模拟 CSV 一次性交付到访问电脑 helper，并由 helper 在本机以稳定 10 Hz 回放、保存和上传，消除浏览器到服务器逐点往返造成的累积延迟。

**Architecture:** 浏览器仍把模拟源上传到服务器的会话隔离暂存区；服务器为已配对且支持 `simulation_replay_v1` 的 helper 创建短时、会话绑定的下载票据和内容清单。helper 通过现有配对认证分块下载并校验相对路径、大小和 SHA-256，写入内容寻址缓存，然后复用 `AcquisitionManager`、本机 CSV/MySQL、单在途 ACK 数据泵和服务器完整采集日志。helper 在启动前不可用时明确回退服务器；helper 启动后掉线只本地继续与重传，绝不静默开启第二个服务器模拟会话。

**Tech Stack:** Python 3.11、标准库 HTTP/ZIP/SHA-256、现有 `ThreadingHTTPServer`、WebSocket helper relay、浏览器 JavaScript、pytest、PyInstaller。

**Spec:** `openspec/changes/fix-remote-acquisition-workflows/design.md` 与 `openspec/changes/fix-remote-acquisition-workflows/specs/remote-simulation-source/spec.md`

## Global Constraints

- 已授权远程模拟在兼容 helper 在线时 SHALL 由访问电脑 helper 执行；访客与回环 `local_admin` 保持既有执行路径。
- 旧版或离线 helper MUST 在启动前显示服务器回退；运行中 helper 掉线 MUST NOT 静默迁移到服务器。
- 模拟源交付总量不超过现有 64 MB、单文件 16 MB、最多 500 个 CSV；所有路径 MUST 为安全相对路径。
- helper MUST 校验清单大小与 SHA-256，并只在校验成功后原子写入内容寻址缓存。
- 10 Hz、600 秒测试 MUST 恰好产生 6000 行；有效速率为 9.8–10.2 Hz，上传 P95 延迟不超过 1 秒且不得随时间增长。
- helper 模拟 MUST 复用真实 helper 的 CSV、本机 MySQL、ACK、断线重传及服务器完整采集日志边界。
- 本机 MySQL 和服务器目标 MySQL 状态 MUST 独立；任一数据库失败不得中断 CSV、另一数据库、预测或停止。
- MUST NOT 改变五类传感器协议/解析、17 通道模型输入、预测模型、MySQL 表结构或主程序 EXE。

---

### Task 1: 路由与能力门禁

**Files:**
- Modify: `visualization_app/helper_relay.py`
- Modify: `visualization_app/app.py`
- Modify: `visualization_app/static/app.js`
- Test: `visualization_app/test_helper_relay.py`
- Test: `visualization_app/test_frontend_guest_simulation.py`
- Test: `visualization_app/test_guest_web.py`

**Interfaces:**
- Consumes: `HelperRegistry.status(session_id) -> dict`、现有 `/api/helper/command` 和 `/api/acquisition/start`。
- Produces: `simulation_replay_v1` 能力、`execution_host`（`helper_local` 或 `server`）、明确的 `fallback_reason`，以及 helper 模拟启动/停止路由。

- [ ] **Step 1: 写兼容 helper、旧 helper、离线 helper 和运行期掉线的失败测试**

```python
def test_authorized_remote_simulation_prefers_compatible_helper():
    status = helper_status(capabilities={"simulation_replay_v1": True}, online=True)
    assert select_simulation_execution(status) == ("helper_local", None)

def test_old_or_offline_helper_falls_back_before_start_only():
    assert select_simulation_execution(helper_status(capabilities={}, online=True))[0] == "server"
    assert select_simulation_execution(helper_status(online=False))[0] == "server"
    assert "server" not in runtime_disconnect_actions()
```

- [ ] **Step 2: 运行新增测试并确认因缺少能力选择和模拟 helper 路由而失败**

Run: `python -m pytest visualization_app/test_helper_relay.py visualization_app/test_frontend_guest_simulation.py visualization_app/test_guest_web.py -q`

Expected: FAIL，失败点指向缺少 `simulation_replay_v1`、`execution_host` 或模拟 `start_capture` 被拒绝。

- [ ] **Step 3: 实现最小能力门禁和显式回退**

```python
def supports_simulation_replay(status: dict[str, Any]) -> bool:
    capabilities = status.get("capabilities") or {}
    return bool(status.get("online") and status.get("protocol_compatible")
                and capabilities.get("simulation_replay_v1"))
```

前端只在此函数为真时向 helper 发 `start_capture`；否则调用服务器 `/api/acquisition/start` 并显示回退原因。服务端只允许兼容 helper 的 `acquisition_mode=simulation`，并保持真实采集门禁原样。

- [ ] **Step 4: 运行路由测试并确认通过**

Run: `python -m pytest visualization_app/test_helper_relay.py visualization_app/test_frontend_guest_simulation.py visualization_app/test_guest_web.py -q`

Expected: PASS。

- [ ] **Step 5: 提交路由与能力门禁**

```powershell
git add visualization_app/helper_relay.py visualization_app/app.py visualization_app/static/app.js visualization_app/test_helper_relay.py visualization_app/test_frontend_guest_simulation.py visualization_app/test_guest_web.py
git commit -m "feat: route remote simulation through compatible helper"
```

### Task 2: 会话绑定的模拟源清单与分块交付

**Files:**
- Create: `visualization_app/simulation_source_transfer.py`
- Modify: `visualization_app/guest_simulation.py`
- Modify: `visualization_app/app.py`
- Modify: `visualization_app/local_capture_agent.py`
- Modify: `visualization_app/local_capture_helper_entry.py`
- Test: `visualization_app/test_simulation_source_transfer.py`
- Test: `visualization_app/test_guest_web.py`

**Interfaces:**
- Consumes: `GuestSimulationManager.resolve_uploaded_source(session_id, source_id)`。
- Produces: `SimulationSourceManifest`、`SimulationSourceTicketStore.issue/authorize/consume`、helper 端 `SimulationSourceCache.fetch(manifest, reader) -> Path`。

- [ ] **Step 1: 写清单、会话隔离、限额、路径穿越、中断和哈希错误的失败测试**

```python
def test_cache_rejects_hash_mismatch(tmp_path):
    manifest = manifest_for("data.csv", b"expected")
    cache = SimulationSourceCache(tmp_path)
    with pytest.raises(SimulationSourceTransferError, match="SHA-256"):
        cache.install(manifest, [("data.csv", b"changed")])

def test_ticket_cannot_cross_helper_session(ticket_store):
    ticket = ticket_store.issue("web-a", "helper-a", "source-a", ttl_seconds=60)
    with pytest.raises(SimulationSourceTransferError):
        ticket_store.authorize(ticket.token, "helper-b")
```

- [ ] **Step 2: 运行测试并确认缺少传输模块而失败**

Run: `python -m pytest visualization_app/test_simulation_source_transfer.py -q`

Expected: FAIL with import error for `simulation_source_transfer`。

- [ ] **Step 3: 实现安全清单、短时票据和内容寻址缓存**

```python
@dataclass(frozen=True)
class SimulationSourceFile:
    relative_path: str
    size: int
    sha256: str

@dataclass(frozen=True)
class SimulationSourceManifest:
    source_id: str
    source_type: str
    files: tuple[SimulationSourceFile, ...]
    total_bytes: int
    content_sha256: str
```

服务器仅返回不含绝对路径的清单和短时票据；下载接口以 helper 配对令牌和设备 ID 鉴权，按单文件索引分块响应。helper 先写临时文件，完整校验后原子移动到 `<helper-state>/simulation-cache/<content_sha256>/`，失败时删除临时文件并保留旧缓存。

- [ ] **Step 4: 运行源交付与公网安全测试**

Run: `python -m pytest visualization_app/test_simulation_source_transfer.py visualization_app/test_guest_web.py visualization_app/test_public_web_security.py -q`

Expected: PASS，响应和日志中不出现服务器暂存绝对路径。

- [ ] **Step 5: 提交模拟源交付**

```powershell
git add visualization_app/simulation_source_transfer.py visualization_app/guest_simulation.py visualization_app/app.py visualization_app/local_capture_agent.py visualization_app/local_capture_helper_entry.py visualization_app/test_simulation_source_transfer.py visualization_app/test_guest_web.py
git commit -m "feat: transfer simulation sources to local helper"
```

### Task 3: helper 本地单调时钟回放

**Files:**
- Create: `visualization_app/simulation_replay.py`
- Modify: `visualization_app/acquisition.py`
- Modify: `visualization_app/local_capture_agent.py`
- Test: `visualization_app/test_simulation_replay.py`
- Test: `visualization_app/test_acquisition_integrity.py`

**Interfaces:**
- Consumes: helper 缓存返回的本地 CSV/文件夹路径和现有 `AcquisitionConfig`。
- Produces: `MonotonicReplayScheduler(rate_hz, clock, sleep).run(rows, emit)` 和 `execution_host=helper_local` 的模拟采集状态。

- [ ] **Step 1: 写 10 Hz×600 秒、处理耗时和物理驱动隔离的失败测试**

```python
def test_absolute_deadlines_do_not_accumulate_processing_delay():
    clock = FakeClock(processing_seconds=0.03)
    emitted = []
    MonotonicReplayScheduler(10.0, clock.monotonic, clock.sleep).run(range(6000), emitted.append)
    assert len(emitted) == 6000
    assert 599.8 <= clock.monotonic() <= 600.2

def test_helper_simulation_does_not_discover_physical_interfaces(manager):
    manager.start(simulation_config())
    assert manager.discover_calls == 0
```

- [ ] **Step 2: 运行时钟测试并确认缺少调度器而失败**

Run: `python -m pytest visualization_app/test_simulation_replay.py -q`

Expected: FAIL with import error for `MonotonicReplayScheduler`。

- [ ] **Step 3: 实现绝对期限调度并接入现有模拟驱动**

```python
deadline = started_at + emitted_count / rate_hz
remaining = deadline - clock()
if remaining > 0:
    sleep(remaining)
emit(row)
```

调度依据起始单调时间和样本序号计算，不使用“处理后再 sleep 一个周期”；helper 传入缓存本地路径，`AcquisitionManager` 继续复用既有 CSV、通道映射和五类模拟元数据。

- [ ] **Step 4: 运行模拟回放和采集完整性测试**

Run: `python -m pytest visualization_app/test_simulation_replay.py visualization_app/test_acquisition_integrity.py -q`

Expected: PASS，6000 行且速率在 9.8–10.2 Hz。

- [ ] **Step 5: 提交本地回放**

```powershell
git add visualization_app/simulation_replay.py visualization_app/acquisition.py visualization_app/local_capture_agent.py visualization_app/test_simulation_replay.py visualization_app/test_acquisition_integrity.py
git commit -m "feat: replay simulation locally at stable sample rate"
```

### Task 4: 增量样本队列、保存边界与运行指标

**Files:**
- Modify: `visualization_app/acquisition.py`
- Modify: `visualization_app/local_capture_agent.py`
- Modify: `visualization_app/app.py`
- Modify: `visualization_app/static/app.js`
- Test: `visualization_app/test_local_capture_agent.py`
- Test: `visualization_app/test_helper_transport.py`
- Test: `visualization_app/test_edge_capture.py`
- Test: `visualization_app/test_remote_mysql_setup.py`

**Interfaces:**
- Consumes: `AcquisitionManager.stream_rows_since(cursor, limit)` 和既有 `ServerCaptureJournal`。
- Produces: 只复制新增样本的单调游标；`source_transfer`、`local_rate_hz`、`queue_depth`、`server_receive_latency_ms`、`browser_publish_latency_ms`；分离的 `helper_local`/`server_target` 保存结果。

- [ ] **Step 1: 写全历史复制、断线续传、双 MySQL 独立失败和指标的失败测试**

```python
def test_next_batch_reads_only_rows_after_cursor(agent, manager):
    manager.append_rows(50)
    first = agent.next_sample_batch(limit=20)
    manager.append_rows(1)
    second = agent.acknowledge_and_next(first["sequence"], limit=20)
    assert manager.stream_slice_requests[-1].start == 20

def test_local_mysql_failure_does_not_block_server_target_or_csv(result):
    assert result["local_csv"]["saved"] is True
    assert result["helper_local"]["state"] == "failed"
    assert result["server_target"]["state"] in {"saved", "pending"}
```

- [ ] **Step 2: 运行测试并确认现有 `numeric_matrix()` 全历史复制和模拟本机 MySQL 限制导致失败**

Run: `python -m pytest visualization_app/test_local_capture_agent.py visualization_app/test_helper_transport.py visualization_app/test_edge_capture.py visualization_app/test_remote_mysql_setup.py -q`

Expected: FAIL，断言指出全量快照调用或本机 MySQL 被前端拒绝。

- [ ] **Step 3: 实现增量游标、分离保存状态和只读运行指标**

```python
def stream_rows_since(self, cursor: int, limit: int) -> tuple[int, list[dict[str, Any]], list[str]]:
    with self.lock:
        end = min(self.total_sample_count, cursor + max(1, limit))
        return end, self._rows_for_absolute_range(cursor, end), list(self.timestamps_for(cursor, end))
```

helper 数据泵不再调用全历史 `numeric_matrix()`；断线只增长有界磁盘/内存队列并在恢复后按序 ACK。浏览器状态展示实际执行端和指标，但绘图仍按既有降采样/合并路径，不参与 10 Hz 调度。

- [ ] **Step 4: 运行传输、保存与五类接口回归**

Run: `python -m pytest visualization_app/test_local_capture_agent.py visualization_app/test_helper_transport.py visualization_app/test_edge_capture.py visualization_app/test_remote_mysql_setup.py visualization_app/test_interface_agent.py -q`

Expected: PASS，五类接口期望值不变。

- [ ] **Step 5: 提交增量队列与状态**

```powershell
git add visualization_app/acquisition.py visualization_app/local_capture_agent.py visualization_app/app.py visualization_app/static/app.js visualization_app/test_local_capture_agent.py visualization_app/test_helper_transport.py visualization_app/test_edge_capture.py visualization_app/test_remote_mysql_setup.py
git commit -m "perf: stream helper simulation samples incrementally"
```

### Task 5: 针对性、全量和 OpenSpec 验证

**Files:**
- Modify: `openspec/changes/fix-remote-acquisition-workflows/tasks.md`
- Create: `reports/remote-helper-simulation-replay-verification-2026-09-29.md`

**Interfaces:**
- Consumes: Tasks 1–4 的实现与测试。
- Produces: 机器可复查的测试记录、性能证据和未现场验证边界。

- [ ] **Step 1: 运行针对性测试**

Run: `python -m pytest visualization_app/test_simulation_source_transfer.py visualization_app/test_simulation_replay.py visualization_app/test_local_capture_agent.py visualization_app/test_helper_transport.py visualization_app/test_guest_web.py visualization_app/test_frontend_guest_simulation.py visualization_app/test_remote_mysql_setup.py visualization_app/test_interface_agent.py -q`

Expected: PASS。

- [ ] **Step 2: 运行核心行为回归**

Run: `python -m pytest visualization_app/test_acquisition_integrity.py visualization_app/test_edge_capture.py visualization_app/test_mysql_visibility.py visualization_app/test_mysql_identity.py visualization_app/test_agentic_diagnosis.py visualization_app/test_public_web_security.py -q`

Expected: PASS。

- [ ] **Step 3: 运行完整测试并保存结果**

Run: `python -m pytest visualization_app -q --junitxml=reports/remote-helper-simulation-replay-pytest-2026-09-29.xml`

Expected: PASS；若有既有失败，报告必须列出基线复现与不受本变更影响的证据。

- [ ] **Step 4: 验证规格和不变边界**

Run: `openspec validate fix-remote-acquisition-workflows --strict`

Expected: PASS；同时记录主程序 EXE、预测模型、五类协议文件和 MySQL schema 的变更前后 SHA-256。

- [ ] **Step 5: 写验证报告并只勾选已有证据的 17.1–17.7**

报告必须包含 6000 行、9.8–10.2 Hz、P95≤1 秒、无缺失重复、两个 MySQL 保存端状态及“真实第二台电脑现场验收待用户完成”。

- [ ] **Step 6: 提交验证证据**

```powershell
git add reports/remote-helper-simulation-replay-verification-2026-09-29.md reports/remote-helper-simulation-replay-pytest-2026-09-29.xml openspec/changes/fix-remote-acquisition-workflows/tasks.md
git commit -m "test: verify remote helper simulation replay"
```

### Task 6: 交付同步与 helper 重建

**Files:**
- Modify: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/app/`
- Modify: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/local_helper/AFP_Local_Capture_Helper.exe`
- Modify: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/DELIVERY_SHA256.txt`
- Modify: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/README.md`
- Modify: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/SECOND_PC_ACCEPTANCE.md`
- Modify: `openspec/changes/fix-remote-acquisition-workflows/tasks.md`

**Interfaces:**
- Consumes: 全部已验证业务源文件和 `visualization_app/build_local_capture_helper.ps1`。
- Produces: 同步的交付源码、新 helper EXE、哈希清单、版本说明和第二台电脑验收步骤。

- [ ] **Step 1: 记录构建前不可变资产哈希**

Run: `Get-FileHash delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/AFP_Integrated_System_Modular.exe -Algorithm SHA256`

同时记录 MySQL schema、预测模型和五类传感器协议实现文件哈希。

- [ ] **Step 2: 按明确文件清单同步验证过的源码**

只复制 Tasks 1–4 实际修改的 Python/JavaScript 到 `delivery/.../app/`；逐文件比较 SHA-256，不复制 `runtime/`、日志、令牌、密码或 DPAPI 数据。

- [ ] **Step 3: 重建 helper 并验证能力握手**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File visualization_app/build_local_capture_helper.ps1`

Expected: 新 helper 报告协议兼容并包含 `simulation_replay_v1: true`，无新增第三方运行时依赖。

- [ ] **Step 4: 更新交付说明、哈希和第二台电脑验收清单**

验收清单要求重新下载新 helper，覆盖：配对、源交付、10 Hz 回放、断线恢复、CSV、本机 MySQL、目标 MySQL、停止结果与日志导出。

- [ ] **Step 5: 复核主程序和不变边界**

主程序 EXE SHA-256 必须与 Step 1 一致；五类协议、预测模型与 MySQL 表结构行为必须保持不变。

- [ ] **Step 6: 勾选 17.8 并提交交付更新**

```powershell
git add delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic openspec/changes/fix-remote-acquisition-workflows/tasks.md
git commit -m "build: deliver helper simulation replay"
```
