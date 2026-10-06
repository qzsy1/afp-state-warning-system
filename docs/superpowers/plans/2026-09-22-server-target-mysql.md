# Server Target MySQL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 授权公网真实与模拟采集的目标 MySQL 预检和最终写入统一在服务器进行，完整保存 helper 的所有样本。

**Architecture:** 服务器仅为精确匹配的默认目标解析已存密码；显式自定义密码绑定授权会话。真实采集由 helper 继续采样并保存本机目的地，服务器在 ACK 前将目标库所需批次写入有界持久日志，并在最终状态到达后异步写入现有 MySQL 表。实时镜像保持原有 50,000 点上限。

**Tech Stack:** Python 标准库、现有 Flask-like HTTP/WebSocket 服务、MySQLCaptureStore、原生 JavaScript、pytest/unittest。

**Spec:** `openspec/changes/fix-remote-acquisition-workflows/specs/mysql-scope-portability/spec.md` 和 `design.md`。

## Global Constraints

- 不改变五类传感器驱动、协议、解析、预测模型、CSV 与 MySQL 表结构。
- 主程序 EXE 不重建；只有 helper 源码变更时才重建 helper EXE。
- 密码不得进入响应、helper 采集命令、批次持久日志、状态和日志。
- 第二台电脑实测必须由用户完成，不得用本机公网验证代替。

---

### Task 1: 会话目标配置与只读预检

**Files:** `visualization_app/server_target_mysql.py` (create), `visualization_app/app.py` (modify), `visualization_app/test_server_target_mysql.py` (create).

**Interfaces:** `ServerTargetProfiles.resolve(session_id: str, payload: dict) -> TargetSelection`，`TargetSelection.settings` 仅服务器内部可见；`public()` 返回不含秘密的配置标识；`resolve_id(session_id, config_id)` 同会话获取同一目标。

- [ ] 测试默认目标密码框为空仍用匹配已存密码，自定义字段变化/错误显式密码不回退，跨会话标识不可用；以假存储断言真实连接配置。
- [ ] 运行 `python -m unittest visualization_app.test_server_target_mysql -v`，确认预期失败。
- [ ] 实现精确匹配、安全标识和短暂会话保存；接入 `/api/mysql/test` 和模拟启动，密码不回传。
- [ ] 重跑新增测试及 `test_remote_mysql_setup.py`、`test_guest_web.py`，确认通过。

### Task 2: 持久批次与 ACK 顺序

**Files:** `visualization_app/server_capture_journal.py` (create), `visualization_app/app.py`, `visualization_app/test_server_capture_journal.py` (create).

**Interfaces:** `ServerCaptureJournal.arm(session_id, target_config_id, config)`，`ingest(session_id, batch, mirror) -> ack`，`ready_capture(session_id, capture_uuid) -> CaptureRecord`。磁盘记录以 `(session_id,capture_uuid,sequence)` 唯一，成功持久化后才调用镜像并 ACK。

- [ ] 测试重复、断线重发、序号缺口、跨会话、配额/磁盘错误、最终状态和超过 50,000 点的完整读取。
- [ ] 运行新增测试，确认实现缺失导致失败。
- [ ] 实现有界持久存储、重启可恢复状态与 ACK 安全顺序；真实 helper 开始命令剥离目标库凭据，本机库保留。
- [ ] 重跑新增及 helper 传输/五接口测试。

### Task 3: 异步、完整且可重试的服务器写库

**Files:** `visualization_app/mysql_storage.py`, `visualization_app/server_capture_journal.py`, `visualization_app/app.py`, `visualization_app/test_server_capture_journal.py`。

**Interfaces:** `MySQLCaptureStore.save_layer` 接收可重复遍历且可求长度的磁盘行视图，执行有界 `executemany`；写库工作项引用先前会话绑定标识而不是日志中的密码。

- [ ] 测试连续样本编号和 >50,000 行、重复停止幂等、失败保留待补传、后台写入不阻塞 ACK。
- [ ] 运行新增测试确认失败；实现分批事务和异步状态/重试。
- [ ] 运行 MySQL、CSV、预测与五类传感器相关回归并核对行为。

### Task 4: 页面、交付和分级验收

**Files:** `visualization_app/static/app.js`, 前端回归测试、`openspec/changes/fix-remote-acquisition-workflows/tasks.md`、交付目录和哈希清单。

- [ ] 测试空密码框预检语义、服务器与本机库分别显示预检/最终保存、访客禁用。
- [ ] 先运行失败测试，再实现页面与安全状态，重跑通过。
- [ ] 同步仅通过测试的文件，核对 EXE 哈希、交付哈希清单、OpenSpec strict 验证、全量回归和本机公网路径。
- [ ] 将异机目标库写入、完整行数与来源授权交给用户现场验收；现场前保持“待确认”。
