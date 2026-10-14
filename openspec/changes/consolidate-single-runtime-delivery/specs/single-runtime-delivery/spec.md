## Purpose

本能力用于把 AFP 主程序的构建、部署、运行、清理和回滚约束在 `F:\new_AFP_exam` 内，并确保任意时刻只有一个受支持的可运行版本，同时不牺牲发布完整性、证据可追溯性和受控回滚能力。

## ADDED Requirements

### Requirement: 固定目录与唯一运行版本
系统 SHALL 使用 `F:\new_AFP_exam\AFP_Integrated_System_Modular` 作为本机唯一受支持的 AFP 运行目录，使用 `F:\new_AFP_exam\AFP-Readiness-Repo` 作为源码目录，并使用 `F:\new_AFP_exam\AFP-Main-Evidence` 作为证据目录。除部署期间的临时 staging 外，系统 MUST NOT 创建或长期保留第二套完整可运行 AFP 目录。

#### Scenario: 正常运行目录识别
- **WHEN** 操作员检查已部署主程序、源码和证据位置
- **THEN** 主程序仅从固定运行目录启动，源码和证据分别位于约定目录，且不会被计作额外可运行版本

#### Scenario: 拒绝固定根目录之外的新交付
- **WHEN** 构建或部署请求把 staging、运行目录或新证据写到 `F:\new_AFP_exam` 之外
- **THEN** 系统拒绝该请求并报告不合规的绝对目标路径

### Requirement: 临时 staging 生命周期
系统 SHALL 在 `F:\new_AFP_exam` 下的隔离临时目录装配待部署内容，并 MUST 在成功部署或失败退出后清理该 staging。staging MUST NOT 被注册为启动目标、正式运行目录或长期候选存档。

#### Scenario: staging 验证成功
- **WHEN** 临时装配完成且所有部署前验证通过
- **THEN** 系统允许进入受控切换阶段，并在切换结束后删除该 staging

#### Scenario: staging 装配或验证失败
- **WHEN** 临时装配、摘要校验、自检或模块检查失败
- **THEN** 系统停止部署、不触碰当前运行目录，并安全删除本次 staging

### Requirement: 部署前身份与完整性门
系统 MUST 在切换前校验 `RELEASE-MANIFEST.json`、`VERSION.json`、`SHA256SUMS.txt`、候选身份、主程序与 Helper 版本、Helper 协议、配置 schema、readiness schema 和统一帧合同，并运行 `--verify-files`、`--self-test` 与 `--module-status`。任何不一致或失败 SHALL 阻止切换。

#### Scenario: 完整验证通过
- **WHEN** staging 的身份、摘要、版本合同和三个可执行检查均成功
- **THEN** 系统记录脱敏结果并允许请求切换审批

#### Scenario: 身份或完整性不一致
- **WHEN** 任一 manifest、摘要、版本、协议、schema、合同或可执行检查不一致
- **THEN** 系统把部署标记为 failed、保留当前运行版本并清理失败 staging

### Requirement: 切换与破坏性操作审批
系统 MUST 在停止 8770 监听者、替换固定运行目录、修改自启动、删除历史目录或改变公网配置前，输出准确目标、PID 或路径、影响、预计中断和回滚方法，并等待用户明确授权。授权范围 SHALL 仅覆盖已列出的目标。

#### Scenario: 未获得明确授权
- **WHEN** 已发现运行中的旧版本或待修改、待删除的系统状态，但用户尚未明确批准列出的操作
- **THEN** 系统仅保存只读证据并停止在审批门前，不停止进程、不修改系统状态且不删除目录

#### Scenario: 获得针对准确目标的授权
- **WHEN** 用户明确批准了已列出的进程、启动项和目录操作
- **THEN** 系统仅对这些目标执行切换或清理，并为每项操作保存执行前后状态

### Requirement: 固定运行目录提升与服务验证
系统 SHALL 只把已通过部署前门禁的内容提升到固定运行目录，并 MUST 在清理旧版本前确认 8770 由固定目录中的新程序监听、8771 仅本机可用、本机与局域网 8770 可访问、现有公网 HTTPS 地址可访问、Funnel 源站仍为 `127.0.0.1:8770`，且公网静态资源属于新身份。

#### Scenario: 新运行版本验证成功
- **WHEN** 新程序已从固定运行目录启动且全部本机、局域网、公网与静态资源检查通过
- **THEN** 系统把新版本标记为当前唯一可运行版本，并允许进入证据迁移和历史目录清理

#### Scenario: 新运行版本验证失败
- **WHEN** 新程序未监听预期端口或任一本机、局域网、公网、Funnel、静态资源检查失败
- **THEN** 系统停止清理，保留所有回滚材料，并按切换前记录恢复旧程序

### Requirement: 启动目标一致性
系统 SHALL 使 AFP 公网 watchdog、Tailscale Funnel watchdog 和 Helper 自启动配置解析到固定运行目录。系统 MUST NOT 自动改变 Funnel 公网域名或其 `127.0.0.1:8770` 源站。

#### Scenario: 启动路径检查
- **WHEN** 操作员审查登录启动项、计划任务和 watchdog 状态
- **THEN** 所有 AFP 可执行目标均位于固定运行目录，且不存在指向已清理候选或旧 delivery 目录的活动启动项

#### Scenario: 公网源站保持不变
- **WHEN** 运行目录完成切换
- **THEN** 现有 HTTPS 公网入口继续指向 `127.0.0.1:8770`，不因目录迁移而生成新域名或改用其他源站

### Requirement: 证据迁移与秘密保护
系统 SHALL 把本次运行需要保留的证据集中到固定证据目录，并 MUST 在删除原证据前完成逐文件复制校验与秘密扫描。证据、命令、脚本和配置 MUST NOT 包含密码、Token、配对码、Tailscale 凭据、MySQL 密码或其他环境秘密；MySQL 密码仍只能通过本机安全提示输入。

#### Scenario: 证据迁移成功
- **WHEN** 原证据已复制到固定证据目录且文件清单、摘要与脱敏检查全部通过
- **THEN** 系统允许在单独授权后删除原证据副本，并保留迁移 manifest

#### Scenario: 证据校验或秘密扫描失败
- **WHEN** 复制摘要不一致、文件缺失或发现疑似秘密
- **THEN** 系统停止原证据删除，把该项标记为 failed，并仅记录脱敏的失败原因

### Requirement: 精确且安全的历史目录清理
系统 SHALL 仅清理迁移清单中逐一列出的历史候选和旧版本化 delivery 目录。清理前 MUST 解析绝对路径，确认目标不是源码目录、固定运行目录、固定证据目录或其父目录，且没有进程、启动项或监听者引用目标。

#### Scenario: 安全目标可清理
- **WHEN** 新运行版本验证通过、证据已迁移、用户已批准，且目标通过路径与引用检查
- **THEN** 系统删除该准确目标并记录删除结果与释放空间

#### Scenario: 目标仍被引用或越界
- **WHEN** 待删目录仍被进程或启动项引用，或目标解析为受保护目录、父目录、通配符或未确认路径
- **THEN** 系统拒绝删除并把该目标标记为 blocked

### Requirement: Git 重建回滚
系统 SHALL 用已记录的 Git 提交或标签、发布合同和构建输入重建历史版本，并把回滚产物重新部署到同一固定运行目录。系统 MUST 记录回滚引用、重建验证结果和预期中断，且 MUST NOT 以保留第二套完整二进制目录作为常规回滚机制。

#### Scenario: 回滚重建成功
- **WHEN** 操作员选择已记录的回滚提交或标签并批准切换
- **THEN** 系统在临时 staging 重建和验证历史版本，再通过相同审批与服务验证流程替换固定运行目录

#### Scenario: 回滚输入不可用
- **WHEN** 指定 Git 引用、Git LFS 内容、构建工具或发布输入不完整
- **THEN** 系统把回滚标记为 blocked，不修改当前运行目录，并明确报告缺失项

### Requirement: 验收语义保持不变
目录收敛 SHALL NOT 改变真实传感器、Helper、模拟数据和状态报告语义。只有真实传感器检查可以标记为 `physical_sensor_pending`；其他检查只能为 `passed`、`failed` 或 `blocked`，并且模拟采集 MUST NOT 被表述为真实双机验证通过。

#### Scenario: Helper 尚未配对
- **WHEN** 固定运行目录已验证但远程 Helper 尚未连接或配对
- **THEN** remote_helper 项被标记为 blocked，而不是 `physical_sensor_pending`，系统也不宣称双机验收完成

#### Scenario: 仅完成模拟采集
- **WHEN** 系统只完成模拟源采集和保存验证
- **THEN** 证据明确标识模拟性质，不把该结果提升为真实传感器或完整双机验收通过
