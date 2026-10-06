## 1. Harness契约与测试基线

- [x] 1.1 新增Harness单元测试骨架，先验证缺失矩阵、未知profile、必需检查失败和超时均返回非零退出码，并确认测试在实现前按预期失败
- [x] 1.2 新增EXE判定测试，覆盖普通外部文件修改、明确重建文件、未知文件人工确认和未授权EXE哈希变化，并确认测试在实现前按预期失败
- [x] 1.3 新增报告契约测试，验证JSON与Markdown包含提交号、分支、工作区状态、时间、命令、退出码、需求覆盖和未验证事项，并确认测试在实现前按预期失败

## 2. 声明式回归矩阵

- [x] 2.1 创建 `verification/regression-matrix.json`，为quick、full、release配置登记现有单元测试、模块状态、自检、完整性、集成冒烟和功能冒烟入口，并通过矩阵结构测试
- [x] 2.2 为启动、模拟/真实采集状态、接口映射、边缘辅助程序、CSV/MySQL、本地/模型诊断、局域网/公网、WebSocket、17通道和M3232建立需求编号映射，并验证不存在无验证方式的强制需求
- [x] 2.3 创建 `verification/exe-rebuild-rules.json`，明确外部更新、必须重建和人工确认模式，并通过全部EXE判定测试

## 3. Harness核心实现

- [x] 3.1 实现矩阵加载与严格校验，使缺失字段、重复ID、未知需求、未知profile和无验证项的强制需求测试通过
- [x] 3.2 实现命令执行、工作目录、超时、输出捕获和失败关闭语义，使命令成功、失败、超时和不可启动测试通过
- [x] 3.3 实现Git提交、分支和工作区状态采集，使release配置在脏工作区拒绝生成通过结论，并通过对应测试
- [x] 3.4 实现需求结果聚合与退出码，使任一必需自动化检查失败时Harness返回非零，并通过聚合测试
- [x] 3.5 实现JSON和Markdown报告原子写入，使报告契约测试全部通过，并将运行结果写入被Git忽略的 `verification/results/`

## 4. 稳定EXE复用门禁

- [x] 4.1 实现基于Git比较范围或显式文件清单的EXE重建判定，使外部更新、必须重建和人工确认测试通过
- [x] 4.2 实现基线EXE路径与SHA-256校验，使无需重建时哈希变化必定导致release失败，并通过哈希回归测试
- [x] 4.3 在重建判定结果中记录触发文件、匹配规则和原因，验证Harness不会主动调用PyInstaller或创建重复交付目录

## 5. Windows与GitHub入口

- [x] 5.1 创建PowerShell入口，自动定位项目支持的Python解释器并透明传递profile、比较范围、EXE路径和报告目录，验证quick配置可从仓库根目录运行
- [x] 5.2 创建GitHub Actions Windows工作流，只运行无硬件、无私密凭据的检查，并验证工作流引用统一Harness入口而非维护第二套命令
- [x] 5.3 增加分支保护配置说明，明确GitHub检查失败禁止合并、现场检查不能由云端冒充，并通过文档契约检查

## 6. 现有核心回归接入

- [x] 6.1 运行quick配置并修复Harness编排问题，记录实际检查数量、通过数量和总耗时
- [x] 6.2 运行full配置，确认现有模块化与可视化测试结果被归集到需求编号，任何现有业务失败按真实结果报告而不被静默忽略
- [x] 6.3 使用当前稳定EXE运行release配置，验证默认不重建EXE、前后SHA-256一致，并执行可用的 `--module-status`、`--self-test`、`--verify-files`、`--integration-smoke` 和 `--functional-smoke`
- [x] 6.4 对无法在当前环境执行的真实SMRF、PLC、ABB、UVC、M3232和目标MySQL检查标记正确证据层级，确认报告不将其描述为已现场验证

## 7. 规格验证与版本管理

- [x] 7.1 运行OpenSpec严格校验，确认proposal、三项能力Spec、design和tasks无结构错误或未解析占位内容
- [x] 7.2 检查Git差异，确认没有生成新的重复EXE或交付目录，没有提交运行凭据、API Key、数据库密码和运行报告
- [ ] 7.3 提交规格与Harness源码到当前功能分支，运行最终full门禁后推送GitHub，并记录提交号与远端分支

## 8. 独立Harness目录与兼容迁移

- [x] 8.1 为根目录 `harness/` 布局、唯一配置来源和旧入口转发编写失败契约测试，验证现有分散路径不能满足新契约
- [x] 8.2 创建 `harness/engine`、`harness/config`、`harness/tests`、`harness/checks`、`harness/logs` 和 `harness/reports`，迁移现有引擎、矩阵、EXE规则和测试并验证原检查ID、超时、证据层级及退出码保持一致
- [x] 8.3 将 `tools/verification/run_quality_gate.ps1` 改为兼容转发入口并更新GitHub工作流，验证旧命令和新命令执行同一实现

## 9. 单项检查与三级组合入口

- [x] 9.1 为 `list`、`check <id>` 和 `profile <quick|full|release>` 编写失败测试，覆盖未知检查、单项选择、稳定profile组合和必需需求覆盖
- [x] 9.2 实现单项及profile选择，确保quick固定执行环境、Harness和模块化运行时检查，full执行全部软件回归，release在full上追加五项EXE与两项现场检查，并通过选择测试
- [x] 9.3 创建纯ASCII的 `quick.cmd`、`full.cmd`、`release.cmd`、`check_all.cmd` 以及每个矩阵检查对应的 `harness/checks/*.cmd`，验证每个入口只转发检查ID/profile、保留退出码并在人工双击模式暂停窗口

## 10. 环境预检与本机发布设置

- [x] 10.1 为Python 3.11、项目 `.venv`、Node.js 22、依赖、工作目录、矩阵和EXE缺失编写失败测试，验证错误包含缺失项、影响检查和修复建议
- [x] 10.2 实现目标感知的轻量预检和独立 `00_environment.cmd`，验证缺失依赖在受影响命令执行前被准确报告
- [x] 10.3 实现 `setup.cmd` 与被Git忽略的 `harness/config/local-settings.json`，验证仅保存解释器路径、EXE路径和基线SHA-256且不会保存API Key、密码或Token

## 11. 精确错误诊断与仅错误报告

- [x] 11.1 为unittest、Python traceback、Node.js、命令启动失败、超时、release策略和现场待验证输出编写失败解析测试，验证期望的检查ID、文件、行号、分类和日志路径
- [x] 11.2 实现统一 `DiagnosticIssue` 解析和每次运行的原始日志/机器状态保存，验证无法解析源码位置时仍保留命令、退出码和日志证据
- [x] 11.3 为仅错误HTML报告编写失败测试，验证报告不包含通过项、包含建议重跑 `.cmd`，且全部通过时不生成新报告并使旧 `latest-errors.html` 失效
- [x] 11.4 实现 `latest-errors.html`、历史错误报告和自动打开行为，验证报告写入失败继续返回退出码5

## 12. 文档与人工运行体验

- [x] 12.1 编写 `harness/README.md`，列出每个 `.cmd` 对应功能、三级模式、首次setup、退出码、日志位置和现场验证边界，并逐条验证文档命令可执行
- [x] 12.2 验证含空格路径、非仓库当前目录启动、中文Windows区域设置和无管理员权限场景，确认CMD内容不发生编码乱码且窗口保持到用户确认

## 13. 集成验证

- [x] 13.1 逐个运行环境、Harness、启动、接口、原生函数、前端、采集保存、Dashboard、诊断、因果证据、Helper/WebSocket及模型预测单项入口，记录每项退出码并确认错误报告仅包含真实问题
- [x] 13.2 运行quick和full新入口，确认检查组合、需求覆盖、退出码与迁移前语义一致，并确认本机缺少Node.js时给出明确环境错误而非模糊启动失败
- [x] 13.3 使用可信基线EXE运行release新入口，确认五项EXE诊断、SHA-256策略、脏工作区阻断和两项现场待验证均按规格工作
- [x] 13.4 运行Harness全部单元与契约测试、OpenSpec严格校验和Git差异检查，确认没有第二套引擎、重复EXE、凭据或未跟踪运行报告后再进入提交步骤

## 14. 启动器构建与交付组装职责拆分

- [x] 14.1 新增失败契约测试，要求启动器构建、交付组装和兼容编排分别位于独立脚本，并锁定Harness对三者的重建、复用和人工确认分类
- [x] 14.2 提取 `build_launcher.ps1` 和 `assemble_modular_delivery.ps1`，将 `build_modular_app.ps1` 改为保持原参数与退出语义的薄编排入口
- [x] 14.3 调整EXE重建规则、模块化运行时测试和使用文档，确认交付组装修改复用稳定EXE而启动器构建修改仍要求授权重建
- [x] 14.4 运行Harness与模块化运行时相关测试、OpenSpec严格校验和Git差异检查，确认未生成或修改交付EXE及运行凭据
