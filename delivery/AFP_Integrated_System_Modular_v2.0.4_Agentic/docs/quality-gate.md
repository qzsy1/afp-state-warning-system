# 质量门禁使用说明

统一入口为 `tools/verification/run_quality_gate.ps1`。它定位可用 Python 后调用同一套 Python Harness，并原样返回 Harness 退出码。

本机运行需要 Python 3.11、`visualization_app/requirements.txt` 中的 Python 依赖，以及 Node.js 22 或更高版本。GitHub 工作流会安装 Python 3.11 和 Node.js 22；Windows 本机可安装当前 Node.js LTS。首次配置示例：

```powershell
py -3.11 -m venv .venv
./.venv/Scripts/python.exe -m pip install --extra-index-url https://download.pytorch.org/whl/cpu -r visualization_app/requirements.txt
```

## 验证配置

- `quick`：运行 Harness 契约和低成本核心契约，适合日常开发。
- `full`：运行全部可移植核心回归，覆盖启动、采集、接口、保存、诊断、网络、WebSocket、模型通道和预警链路。
- `release`：在 full 基础上运行交付 EXE 的模块状态、自检、文件完整性、集成冒烟和功能冒烟，并检查稳定 EXE 的 SHA-256。

```powershell
./tools/verification/run_quality_gate.ps1 -Profile quick
./tools/verification/run_quality_gate.ps1 -Profile full
./tools/verification/run_quality_gate.ps1 `
  -Profile release `
  -BaseRef origin/main `
  -BaselineExe delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/AFP_Integrated_System_Modular.exe `
  -BaselineExeSha256 <发布前记录的64位SHA-256>
```

也可以使用多个 `-ChangedFile` 代替 `-BaseRef`，用于审核一份明确的变更清单。Harness 不会调用 PyInstaller、复制 EXE 或创建交付目录。

## 退出码和报告

| 退出码 | 含义 |
|---:|---|
| 0 | 选定门禁通过 |
| 2 | 矩阵、规则或参数配置错误 |
| 3 | 必需检查失败、超时或无法启动 |
| 4 | release 工作区、EXE 判定或哈希策略失败 |
| 5 | JSON/Markdown 报告写入失败 |

每次可执行运行生成 JSON 和 Markdown 报告，默认写入 `verification/results/`。该目录是运行产物并被 Git 忽略。报告包含提交号、分支、工作区状态、命令、退出码、需求覆盖和未验证事项；旧提交的报告不得代替当前提交的新结果。

历史 `v13.9` 因果在线准确率依赖未随仓库或交付包保存的三份 `v13.7` 上游结果。Harness 将缺少 `causal_online_level_metrics.csv` 记录为非阻塞、未验证证据，不得生成或填入虚构指标。

稳定 EXE 的 `--functional-smoke` 最长运行 600 秒。超时后 Harness 会终止直接启动的进程并把检查记为失败。Windows 下若被测程序另外派生 GUI 或服务子进程，当前 Harness 不保证清理整个后代进程树；发布机仍需检查并关闭残留进程。

## GitHub 和分支保护

GitHub Actions 的必需检查名称为 `quality-gate`。稳定分支应在 GitHub 分支保护中把该检查设置为必需状态；检查失败时禁止合并。工作流运行 `full/ci`，不使用仓库外私密凭据，也不连接现场硬件或目标 MySQL。

云端检查不得冒充现场验收。真实 SMRF、PLC、ABB、UVC、M3232 和目标 MySQL 在 release 报告中保持“现场待验证”，直到负责人补充真实设备证据。模拟采集通过只说明软件链路通过，不得表述为真实采集通过。

## 凭据边界

API Key、数据库密码、Token 和其他凭据只能通过运行环境或用户输入提供，不得写入矩阵、工作流或报告。Harness 会脱敏已知凭据环境变量及常见键值形式，但调用方仍应避免让测试命令主动输出秘密。
