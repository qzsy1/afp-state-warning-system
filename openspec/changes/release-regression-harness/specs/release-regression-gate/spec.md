## Purpose

建立可重复执行、可追踪、失败即阻断的三级质量门禁，使每次变更在合并或发布前验证本次需求及全部核心能力，并生成足以审计和复现的验证证据。

## ADDED Requirements

### Requirement: Harness必须提供三级验证配置
Harness MUST 提供开发快速检查、合并完整回归和发布交付验收三个配置，并为每个配置声明必需检查、可选检查和现场检查。

#### Scenario: 开发快速检查
- **WHEN** 开发者运行快速配置
- **THEN** Harness MUST 运行受影响功能的单元测试、核心契约测试、敏感信息检查和变更范围检查

#### Scenario: 合并完整回归
- **WHEN** 变更准备合并到稳定分支
- **THEN** Harness MUST 运行全部自动化核心回归，任一必需检查失败时返回非零退出码

#### Scenario: 发布交付验收
- **WHEN** 变更准备打稳定标签或形成正式交付
- **THEN** Harness MUST 在完整回归基础上验证交付文件、原EXE运行能力和发布证据完整性

#### Scenario: Windows用户双击运行三级配置
- **WHEN** Windows用户在 `harness/` 目录双击quick、full或release入口
- **THEN** Harness MUST 运行对应配置、保留命令窗口直至用户确认，并返回与命令行入口一致的退出码

### Requirement: Harness必须提供独立的人工检查目录和单项入口
Harness MUST 将执行引擎、声明式配置、测试、日志和入口集中在仓库根目录的独立 `harness/` 文件夹，并为回归矩阵中的每个检查项提供一个可直接双击的 `.cmd` 入口。批处理入口 MUST 只负责定位项目和转发检查ID，不得复制实际检查逻辑。

#### Scenario: 人工运行单项检查
- **WHEN** 用户双击某个 `harness/checks/*.cmd`
- **THEN** Harness MUST 仅运行该入口映射的矩阵检查，并在窗口中显示检查名称、结果、耗时和后续操作

#### Scenario: 检查定义发生变化
- **WHEN** 维护者修改某个检查的命令、工作目录、超时或证据等级
- **THEN** 维护者 MUST 能通过修改声明式配置影响单项和组合入口，而无需在多个 `.cmd` 中同步复制命令

#### Scenario: 旧入口仍被调用
- **WHEN** GitHub工作流、旧文档或用户调用原 `tools/verification` 入口
- **THEN** 旧入口 MUST 转发到 `harness/` 的唯一实现并保持原参数和退出码兼容

### Requirement: 需求与测试必须可追踪
每项强制需求 MUST 映射到一个或多个具体测试或人工验收项，并记录需求编号、验证命令、适用配置、证据层级和阻断级别。

#### Scenario: 生成覆盖报告
- **WHEN** Harness完成一次运行
- **THEN** 报告 MUST 列出需求总数、已验证、通过、失败、跳过和现场待验证数量，并指出每个失败需求对应的测试

#### Scenario: 必需需求没有验证项
- **WHEN** 回归矩阵存在没有测试或验收方式的强制需求
- **THEN** Harness MUST 将其判为配置错误并阻止完整或发布验证通过

### Requirement: 门禁必须失败关闭
Harness MUST 对必需步骤采用失败关闭策略；测试进程异常退出、超时、命令不存在、报告损坏或结果不可解析均不得被当作通过。

#### Scenario: 测试命令失败
- **WHEN** 任一必需命令返回非零退出码或超过规定时间
- **THEN** Harness MUST 记录失败原因、返回非零退出码并阻止后续发布动作

#### Scenario: 可选现场测试不可执行
- **WHEN** 当前环境没有真实硬件且现场测试被标记为待验证
- **THEN** Harness MAY 完成自动化验证，但报告 MUST 明确现场未验证；涉及驱动或真实采集的稳定发布 MUST 要求补充现场验收

### Requirement: 验证输出必须可复现且错误报告只展示问题
每次Harness运行 MUST 保存足以复现结果的机器状态和原始日志，包含提交号、分支、工作区状态、运行环境、配置名称、开始结束时间、命令、退出码和检查结果。人工可读报告 MUST 仅在存在失败、超时、无法启动、环境缺失、策略失败、非阻塞未验证或现场待验证事项时生成，并且 MUST 只展示这些问题项，不得展开已通过检查。

#### Scenario: 工作区存在未提交修改
- **WHEN** Harness在脏工作区运行
- **THEN** 报告 MUST 记录具体状态；发布配置 MUST 拒绝生成稳定发布结论

#### Scenario: 重复查看历史结果
- **WHEN** 用户打开某次保存的验证报告
- **THEN** 用户 MUST 能确认该结果对应的提交、配置、每个问题的检查ID、失败原因、源码位置、原始日志和建议重跑命令，且旧报告不得替代当前提交的新验证

#### Scenario: 全部自动检查通过且没有待验证事项
- **WHEN** 本次运行没有失败、环境缺失、策略失败、非阻塞未验证或现场待验证事项
- **THEN** Harness MUST 只在命令窗口显示通过数量和耗时，不得生成新的错误报告，并 MUST 防止旧的 `latest-errors.html` 被误认为本次结果

### Requirement: Harness必须提供可操作的错误定位
Harness MUST 将命令失败解析为测试失败、运行时异常、环境错误、超时、发布策略失败或现场待验证，并在可获得证据时记录测试名称、异常类型、源码文件、行号、退出码和原始日志路径。

#### Scenario: Python或JavaScript测试失败
- **WHEN** unittest、Python traceback或Node.js测试输出包含源文件位置
- **THEN** Harness MUST 在窗口和错误报告中展示检查ID、错误消息、源文件和行号，并给出对应单项 `.cmd` 的重跑路径

#### Scenario: 环境或命令缺失
- **WHEN** Python、Node.js、依赖、工作目录、配置或交付EXE缺失
- **THEN** Harness MUST 在执行受影响检查前或启动失败后报告具体缺失项、影响范围和修复建议，不得仅显示通用非零退出码

#### Scenario: 检查超时
- **WHEN** 检查超过矩阵声明的超时时间
- **THEN** Harness MUST 标记超时、记录实际运行时长和上限、终止直接启动的进程，并提示检查可能存在的后代进程

### Requirement: GitHub合并必须支持质量门禁
仓库 MUST 提供可由GitHub执行的自动化检查入口，且该入口不得依赖现场硬件或仓库外的私密凭据。

#### Scenario: Pull Request自动检查
- **WHEN** 功能分支向稳定分支发起Pull Request
- **THEN** GitHub MUST 能运行无硬件完整回归并给出成功或失败状态

#### Scenario: 检查失败
- **WHEN** GitHub质量检查失败
- **THEN** 稳定分支的保护策略 MUST 能配置为禁止合并

### Requirement: Harness必须复用现有验证入口
Harness MUST 编排现有单元测试、模块状态、自检、文件校验、集成冒烟和功能冒烟入口，不得为同一行为维护相互矛盾的重复测试流程。

#### Scenario: 现有测试入口可用
- **WHEN** 回归矩阵引用现有测试命令
- **THEN** Harness MUST 原样记录其命令和退出码，并把结果归集到对应需求编号
