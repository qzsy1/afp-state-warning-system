# 访问电脑 helper 本地模拟回放验证报告

日期：2026-09-29
OpenSpec change：`fix-remote-acquisition-workflows`，任务 17.1–17.8

## 实现边界

- 已授权公网/LAN 模拟在兼容 helper 在线时由访问电脑执行；旧版或离线 helper 只允许在启动前明确回退服务器，运行期不静默迁移。
- 浏览器上传源通过会话绑定票据和有界分块交付；helper 验证相对 CSV 路径、大小和 SHA-256 后原子发布到内容哈希缓存。
- helper 以单调绝对期限调度 10 Hz 回放，不启动五类物理驱动；原 PLC、ABB、SMRF 热电偶、M3232 薄膜压力和 UVC 元数据/真实驱动边界保持不变。
- helper 本机 CSV/本机 MySQL 与服务器目标 MySQL 分开执行并分别显示状态；目标库凭据不进入浏览器、helper、状态或日志。

## 自动验证证据

- 高风险组合回归：245 项，全部通过。覆盖源交付、helper 路由、增量队列、ACK 重发、五类接口、真实/模拟采集、本地/目标 MySQL、前端、诊断和公网安全。
- 扩展全量回归：435 项，全部通过。测试环境补用既有 `torch_geometric` 环境，并从全局环境末位补充 WebSocket 客户端。
- 唯一未覆盖的旧测试：`test_app.DashboardTests.test_causal_online_accuracy_is_close_to_offline_method`。原因是仓库和交付包均没有历史生成物 `outputs_causal_online_consistency_v13_9/causal_online_level_metrics.csv`；未伪造该离线指标文件。
- 调度验证：虚拟时钟 10 Hz × 600 秒恰好 6000 行，最终有效速率 10.0 Hz；每点增加 30 ms 处理耗时仍在 600.0 秒绝对期限结束。
- 传输验证：50 Hz × 600 秒产生 30,000 行，单在途 ACK，最终队列 0，P95 不超过 1 秒；断线后未确认批次原样重发。
- OpenSpec：`openspec validate fix-remote-acquisition-workflows --strict` 通过。
- Python 编译和 `git diff --check` 通过。

## 构建与交付

- 新 helper 版本：`20260929-helper-simulation-replay-v1 (protocol 2)`。
- helper SHA-256：`E8C312E92235826185D0738C1D1B939DDC120266EF308AB81DA14A3E74E5A267`。
- 主程序 EXE 未重建，更新前后 SHA-256 均为 `AA7BC2F636E862E9F603F7B4D4D9D8FC9B71390FFB20F33A011A42EF9D56FD65`。
- `SHA256SUMS.txt` 已重建，共 6279 项；交付自检 `--verify-files`、`--module-status`、`--self-test` 退出码均为 0。
- 静态缓存标识：`20260929-helper-simulation-replay-v4`。

## 重启后网络验证

- 主程序进程：PID 24704；同一进程监听 `0.0.0.0:8770` 与 `127.0.0.1:8771`。
- 本机、实际 LAN `http://192.168.101.31:8770/`、公网 `https://desktop-410sfvi.tail97fe2c.ts.net/` 健康接口各连续 5 次返回 `ok`。
- 5 次平均：回环 25.22 ms（含首次 120.71 ms）、LAN 1.00 ms、公网 10.80 ms。
- 回环/LAN `ws://` 及公网 `wss://` 的 `/api/simulation/ws` 握手均成功。
- 公网页面和 `app.js` 均已出现新缓存标识、`simulation_replay_v1` 能力判断及“执行端：访问电脑 helper”状态文本。
- 动态 LAN 推荐地址为 `http://192.168.101.31:8770/`，物理默认网关网卡优先于 Hyper-V 地址。

## 仍需第二台电脑现场验收

- 第二台电脑必须替换新版 helper，旧 helper 不具备模拟源下载与本地回放能力。
- 需实际跑满 600 秒，核对 6000 点、9.8–10.2 Hz、P95 不超过 1 秒、队列不增长、停止保存、CSV 和所选 MySQL 行数。
- 需现场验证第二台电脑的 DPAPI 本机 MySQL 配置、真实网络抖动/断网恢复，以及实际五类硬件；本机自动化和公网握手不能替代这些现场证据。
