## Context

参见 `proposal.md`。当前仓库已经存在大量 `unittest` 测试、模块化启动器诊断参数、EXE完整性检查、集成冒烟和功能冒烟，但它们分散在多个目录和历史实施计划中，没有统一需求编号、执行配置、失败语义和报告格式。

模块化交付由稳定启动器EXE和外部 `app`、`config`、`models`、`native_dll` 等目录组成。大多数业务修改可以更新外部文件，只有运行时或启动器边界变化才需要重新打包。

## Goals / Non-Goals

**Goals:**

- 用一个统一入口执行快速、完整和发布三类检查。
- 在仓库根目录建立可独立理解和维护的 `harness/` 子系统，并允许Windows用户通过双击 `.cmd` 运行任一单项检查或三级组合检查。
- 把每项稳定需求映射到可执行测试或显式人工验收项。
- 默认复用原EXE，并用变更规则和哈希校验防止无意重建。
- 保存机器状态和原始日志，并仅在出现问题时生成只展示问题项的HTML错误报告。
- 从unittest、Python、Node.js、PowerShell和打包EXE输出中提取可操作的错误原因、源码文件和行号。
- 在不修改现有业务逻辑的前提下编排现有测试入口。

**Non-Goals:**

- 本变更不修复现有采集、诊断、MySQL或公网业务缺陷。
- 本变更不把模拟检查描述为真实硬件验收。
- 本变更不自动保存API Key、MySQL密码或其它凭据。
- 本变更不要求每次代码修改都重新生成EXE。
- 本变更不把大型EXE或运行数据纳入普通Git源码提交。
- 本变更不复制Python、Node.js、项目源码或业务依赖到 `harness/`；它复用项目 `.venv`、系统Node.js和现有测试代码。

## Decisions

### 1. 将Harness集中为根目录独立子系统

现有 `tools/verification/`、`verification/regression-matrix.json` 和 `verification/exe-rebuild-rules.json` 迁移到根目录 `harness/`。目标结构按职责拆分为 `checks/`、`engine/`、`config/`、`tests/`、`logs/` 和 `reports/`；`quick.cmd`、`full.cmd`、`release.cmd` 和 `setup.cmd` 位于 `harness/` 顶层。

`harness/` 是唯一实现和配置来源。原 `tools/verification/run_quality_gate.ps1` 在兼容期保留为薄转发层，GitHub工作流改为调用新入口。替代方案是复制现有实现后同时维护新旧两套Harness；该方案会造成矩阵、错误语义和测试漂移，因此不采用。

### 2. 使用声明式回归矩阵作为唯一编排来源

`harness/config/regression-matrix.json` 为每个检查项声明唯一ID、关联需求、适用配置、命令、工作目录、超时、阻断级别和证据层级。`harness/config/profiles.json` 明确quick、full和release的稳定组合，避免人工双击时因工作区变更推断产生不可预期的检查范围。选择JSON是因为Python标准库和PowerShell均可直接解析，不引入YAML第三方依赖。

替代方案是把命令直接硬编码在PowerShell脚本中；该方案难以审查需求覆盖率，也不利于GitHub和本地复用，因此不采用。

### 3. 使用Python标准库实现核心Harness，PowerShell和CMD提供Windows入口

`harness/engine/harness_cli.py` 负责矩阵校验、单项/profile选择、命令执行、超时、结果归集、Git元数据、诊断和退出码；`harness/engine/run.ps1` 负责定位可用Python、加载本机设置并传递参数。顶层及 `harness/checks/` 下的纯ASCII `.cmd` 只调用 `run.ps1` 并在结束时暂停窗口，不包含具体测试命令。

CLI支持 `list`、`check <id>` 和 `profile <quick|full|release>`。每个矩阵检查都有一个稳定 `.cmd` 映射；单项入口和profile入口最终调用同一个 `run_check` 实现，保证人工运行、PowerShell和CI语义一致。

Harness提供 `quick`、`full`、`release` 三个profile。后一级包含前一级的必需能力，但可采用更适合该层级的聚合命令以避免重复执行同一测试。

### 4. 增加统一环境预检但不复制运行时

`setup.cmd` 检测项目 `.venv`、Python 3.11、Node.js 22、测试依赖、项目目录和交付EXE，并生成被Git忽略的 `harness/config/local-settings.json`。本机设置只保存解释器路径、EXE路径和可信基线SHA-256，不保存API Key、数据库密码或Token。

每次单项或profile运行执行与目标检查相关的轻量预检；`00_environment.cmd` 提供完整独立预检。环境错误必须指出缺失项、最低版本、受影响检查和修复建议。

### 5. 对失败采用明确的失败关闭语义

必需命令的非零退出、超时、启动失败、缺失命令、矩阵错误或报告写入失败都产生非零Harness退出码。现场硬件项只有在矩阵明确标记为 `field` 时可以显示为待验证；涉及对应驱动变化时，发布profile将其升级为人工阻断项。

### 6. 把EXE重建判定独立为可测试规则

`harness/config/exe-rebuild-rules.json` 把文件模式分成“外部更新”“必须重建”和“需要人工确认”。Harness基于Git比较范围或显式文件清单作出判定。

当判定为复用时，发布检查接收基线EXE路径和基线SHA-256，更新后再次计算并强制一致。Harness本身不复制或覆盖EXE，减少误操作风险。

当判定为重建时，Harness只输出 `rebuild_required` 及理由；真正的构建仍由现有模块化构建脚本执行，并在构建后重新运行release profile。

### 6.1 拆分启动器构建与交付目录组装

`modular_runtime/build_launcher.ps1` 只负责校验Python与启动器输入、运行PyInstaller并生成包含EXE和配套运行时的启动器目录；该文件属于明确的EXE重建边界。

`modular_runtime/assemble_modular_delivery.ps1` 只负责把已有启动器目录或稳定EXE与外置 `app`、`config`、`models`、`native_dll`、文档和运维脚本组装到既定交付目录，并生成 `VERSION.json`、`README.txt` 和 `SHA256SUMS.txt`。修改该文件不得单独触发EXE重建，但发布门禁仍必须验证稳定EXE哈希不变及交付完整性。

`modular_runtime/build_modular_app.ps1` 保留为兼容编排入口，维持原参数，按需调用上述两个脚本。由于兼容入口能够决定是否进入构建路径，其自身变化进入人工确认；若同一变更同时修改 `build_launcher.ps1`、启动器入口、依赖或原生运行时，优先采用 `rebuild_required`。

Harness规则分别映射为：`build_launcher.ps1` 属于“必须重建”，`assemble_modular_delivery.ps1` 属于“外部更新”，`build_modular_app.ps1` 属于“人工确认”。契约测试必须锁定该映射并验证混合变更仍由最高风险规则决定。

### 7. 将原始结果与人工错误报告分离

每个命令的原始stdout/stderr和最小机器状态写入 `harness/logs/<run-id>/`，供CI、诊断和复现使用。`harness/engine/diagnostics.py` 从unittest traceback、Python异常、Node.js错误、PowerShell/进程错误及EXE诊断结果中归一化 `DiagnosticIssue`，记录分类、严重度、检查ID、测试名称、消息、文件、行号、退出码、日志和建议重跑命令。

只有存在失败、超时、无法启动、环境缺失、release策略失败、非阻塞未验证或现场待验证事项时，才生成 `harness/reports/latest-errors.html` 和历史副本。HTML只展示问题项，不列出通过项；全部通过时删除或标记旧latest报告为过期，并仅在窗口显示通过数量和耗时。

替代方案是继续为每次运行生成包含全部通过项的Markdown/JSON报告；该方案不符合人工排错目标，且会淹没真正错误，因此机器状态与人工报告分层处理。

### 8. GitHub只执行无硬件检查，本机承担交付和现场证据

`.github/workflows/quality-gate.yml` 在Windows runner执行矩阵结构测试和可移植的自动化回归。依赖本机EXE、MySQL、Tailscale、Cloudflare或真实传感器的检查不在无凭据GitHub环境运行，而在报告中标为本机或现场层级。

稳定分支可以把GitHub的 `quality-gate` 状态设为必需检查。现场验收证据通过本机release报告补充，不伪造成云端自动检查。

### 9. 运行产物与源码提交分离

日志和错误报告写入 `harness/logs/` 与 `harness/reports/`，默认作为运行产物排除出普通源码提交；规格、矩阵、Harness、CMD入口和固定测试进入Git。需要留档时可把选定错误报告与机器状态作为GitHub Release附件或正式验收材料保存。

## Risks / Trade-offs

- [完整回归耗时增加] → quick profile用于日常开发，full和release只在合并、发布时运行。
- [历史测试存在环境依赖] → 矩阵明确工作目录、超时和证据层级；缺少依赖时失败或明确待验证，不静默跳过。
- [路径和Python环境在不同电脑不一致] → PowerShell入口按项目配置和常见解释器顺序定位，并在报告中记录最终解释器。
- [Windows批处理中文乱码] → `.cmd` 文件名和脚本内容使用ASCII，中文只由UTF-8 PowerShell/Python输出。
- [错误输出格式不统一] → 保留完整原始日志；解析失败时仍报告检查ID、命令、退出码和日志路径，不把“无法提取行号”误判为通过。
- [旧latest报告被误认为当前结果] → 每次运行先写运行状态，全部通过时删除或显式失效 `latest-errors.html`。
- [GitHub无法验证真实设备] → 将云端、本机和现场证据分层，涉及硬件的发布要求人工签署现场结果。
- [变更文件模式误判EXE重建] → 未匹配文件进入人工确认，不自动重建；规则本身具有单元测试。
- [兼容入口掩盖启动器构建变化] → 独立构建脚本进入强制重建规则，兼容入口只负责转发且由契约测试限制不得重新嵌入PyInstaller参数。
- [门禁脚本被绕过] → GitHub分支保护负责合并门禁，正式发布入口检查最新release报告的提交号和通过状态。

## Migration Plan

1. 为独立目录、单项执行、profile组合、环境预检、错误解析和仅错误报告补充失败测试。
2. 创建 `harness/` 结构并迁移现有Python引擎、矩阵、EXE规则和Harness测试，保持原检查ID、命令、超时、证据层级和退出码。
3. 实现 `check` 与 `profile` CLI、环境预检、诊断归一化和错误报告，使新增测试通过。
4. 添加顶层profile `.cmd`、环境/setup入口和每项检查的 `.cmd`，验证双击等价命令能够保留窗口与退出码。
5. 将旧PowerShell入口改为兼容转发，更新GitHub Actions和使用文档。
6. 运行单项、quick、full和release验证；确认原EXE复用与SHA-256策略不变，现场项仍为明确待验证。
7. 兼容期结束后可移除旧Python实现，但保留旧PowerShell转发入口直到所有外部调用迁移完成。
8. 拆分模块化启动器构建和交付目录组装，保留 `build_modular_app.ps1` 参数兼容，并以契约测试和EXE规则验证职责边界。

回滚时恢复旧入口和配置路径即可；Harness不迁移业务数据，也不修改现有EXE，因此不会影响现有软件继续运行。`harness/logs/`、`harness/reports/` 和本机设置均为可删除运行产物。
