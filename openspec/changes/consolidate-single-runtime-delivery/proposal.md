## Why

当前主程序的正式交付、候选目录和验收证据分散在 D 盘与 F 盘，并长期保留多套约 1.1–1.2 GiB 的完整可运行副本，既占用存储空间，也让公网启动、自启动和回滚目标难以辨认。现在需要把交付生命周期收敛到 `F:\new_AFP_exam`，用一个固定运行目录承载唯一可运行版本，同时保留 Git 历史作为可重建的回滚依据。

## What Changes

- 新增固定运行目录 `F:\new_AFP_exam\AFP_Integrated_System_Modular`，它是本机唯一受支持的可运行 AFP 版本。
- 固定源码目录为 `F:\new_AFP_exam\AFP-Readiness-Repo`，固定证据目录为 `F:\new_AFP_exam\AFP-Main-Evidence`；两者不计为额外可运行版本。
- 将候选构建改为临时 staging 流程：先在 F 盘隔离装配和验证，再经明确切换审批部署到固定运行目录，成功或失败后清理 staging。
- 将回滚模型改为“Git 提交/标签 + 可重复构建 + 同一固定运行目录”，不长期保留前一套二进制目录。
- 增加运行进程、监听端口、路径边界、证据迁移与秘密扫描保护；任何停止 8770 监听者、修改自启动或删除旧目录的操作仍须在执行时列出准确目标、影响和回滚方式并获得明确授权。
- 将公网 watchdog、Helper 自启动和相关配置的默认交付路径统一到固定 F 盘运行目录，公网 Funnel 源站保持 `127.0.0.1:8770`。
- 在新运行目录完成完整性、自检、模块状态、本机、局域网和公网验证后，清理 D 盘候选、F 盘历史候选及旧版本化 delivery 目录；D 盘证据仅在复制、校验及脱敏确认完成后删除。
- **BREAKING**：不再把 `artifacts/pre-sensor-candidates/<candidate-id>` 或版本化 `delivery/AFP_Integrated_System_Modular_*` 目录作为长期保留或直接运行的交付位置。

## Capabilities

### New Capabilities

- `single-runtime-delivery`: 规定单一固定运行目录、临时 staging、受控切换、验证后清理、Git 重建回滚以及 F 盘路径边界。

### Modified Capabilities

无。

## Impact

- 构建与候选流程：`pre_sensor/build-candidate.ps1`、`modular_runtime/assemble_modular_delivery.ps1`、`modular_runtime/build_modular_app.ps1`。
- 启动与公网维护：`visualization_app/public_tunnel_watchdog.ps1`、`visualization_app/tailscale_funnel_watchdog.ps1`、`visualization_app/install_local_helper_autostart.ps1`。
- 验收脚本、测试与文档：`harness/tests/`、`pre_sensor/FIELD-OPERATIONS.md`、`README.md`、回归矩阵及路径配置。
- 本机运行状态：当前 D 盘 AFP 进程、8770/8771 监听、自启动注册项、Tailscale Funnel 和历史候选目录；实现阶段必须遵守单独的破坏性操作与切换审批门。
- 不改变 Helper 协议、统一帧合同、MySQL 密码输入方式，也不把模拟结果声明为真实传感器通过。
