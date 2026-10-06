# AFP 回归 Harness

本目录是项目唯一的回归检查实现。无需 Navicat，也不复制 Python、Node.js、项目源码或依赖；它复用项目 `.venv`、系统 Node.js 和已有测试。

## 直接双击

- `setup.cmd`：首次配置可信 Python 和稳定 EXE，设置仅写入被 Git 忽略的 `config/local-settings.json`。
- `quick.cmd`：环境预检、Harness 契约和模块化运行时检查。
- `full.cmd`：全部软件回归；不运行 EXE 和现场检查。
- `release.cmd`：full 加五项稳定 EXE 检查及两项现场待验证。
- `check_all.cmd`：与 release 范围相同，用于人工查看全部检查。
- `checks/00_environment.cmd`：只检查 Python 3.11、项目 `.venv`、Node.js 22、目录、配置和 EXE。

`checks/` 中其余每个 `.cmd` 对应 `config/regression-matrix.json` 中同名检查 ID，只运行该检查。所有 `.cmd` 都是 ASCII 文本，使用自身路径定位仓库，保留 Harness 退出码并在结束时等待按键。

双击后会立即显示 `AFP Harness launcher started`，随后显示预检状态及 `[当前项/总项] START`。具体测试的输出会被捕获到日志，因此某一项运行期间暂时没有新文字属于正常现象；窗口中会同时显示该项最长超时时间，结束后显示 `PASSED`、`FAILED`、`TIMED_OUT` 或 `PENDING_FIELD`。

## 命令行

```powershell
./harness/engine/run.ps1 -List
./harness/engine/run.ps1 -Check modular-runtime-contracts
./harness/engine/run.ps1 -Profile quick
./harness/engine/run.ps1 -Profile full
./harness/engine/run.ps1 -Profile release
```

旧入口 `tools/verification/run_quality_gate.ps1` 只转发到上述实现。GitHub 的 `quality-gate` 也调用相同入口；分支保护应把它设为必需状态，失败时禁止合并。

## 输出与退出码

每次运行的机器状态和 stdout/stderr 写入 `harness/logs/<run-id>/`。只有失败、环境缺失、发布策略问题、非阻塞未验证或现场待验证时，才生成 `harness/reports/latest-errors.html` 和历史错误报告；报告只列问题，不列通过项。全部通过时旧的 latest 报告会失效，窗口只显示通过数量和耗时。

| 退出码 | 含义 |
|---:|---|
| 0 | 自动检查通过（可能仍有明确的非阻塞现场边界） |
| 2 | 参数、矩阵或环境预检错误 |
| 3 | 必需检查失败、超时或无法启动 |
| 4 | release 脏工作区、EXE 规则或 SHA-256 策略失败 |
| 5 | 日志或错误报告写入失败 |

## 现场验证边界

自动化和模拟结果不得冒充真实 SMRF、PLC、ABB、UVC、M3232 或目标 MySQL 的现场硬件证据。`field-real-hardware` 和 `field-target-mysql` 会明确显示为待人工验证。Harness 不保存 API Key、密码或 Token。

## 单项入口索引

软件检查：`harness-contracts`、`modular-runtime-contracts`、`interface-monitor-contracts`、`native-function-contracts`、`frontend-public-contracts`、`acquisition-storage-contracts`、`dashboard-runtime-contracts`、`diagnosis-interface-contracts`、`causal-history-evidence`、`edge-helper-websocket-contracts`、`model-prediction-contracts`。

EXE 检查：`release-module-status`、`release-self-test`、`release-verify-files`、`release-integration-smoke`、`release-functional-smoke`。现场项：`field-real-hardware`、`field-target-mysql`。
