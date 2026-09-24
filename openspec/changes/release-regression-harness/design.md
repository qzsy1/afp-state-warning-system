## Context

参见 `proposal.md`。当前仓库已经存在大量 `unittest` 测试、模块化启动器诊断参数、EXE完整性检查、集成冒烟和功能冒烟，但它们分散在多个目录和历史实施计划中，没有统一需求编号、执行配置、失败语义和报告格式。

模块化交付由稳定启动器EXE和外部 `app`、`config`、`models`、`native_dll` 等目录组成。大多数业务修改可以更新外部文件，只有运行时或启动器边界变化才需要重新打包。

## Goals / Non-Goals

**Goals:**

- 用一个统一入口执行快速、完整和发布三类检查。
- 把每项稳定需求映射到可执行测试或显式人工验收项。
- 默认复用原EXE，并用变更规则和哈希校验防止无意重建。
- 生成JSON与Markdown验证报告，为本地、GitHub和发布验收提供相同证据语义。
- 在不修改现有业务逻辑的前提下编排现有测试入口。

**Non-Goals:**

- 本变更不修复现有采集、诊断、MySQL或公网业务缺陷。
- 本变更不把模拟检查描述为真实硬件验收。
- 本变更不自动保存API Key、MySQL密码或其它凭据。
- 本变更不要求每次代码修改都重新生成EXE。
- 本变更不把大型EXE或运行数据纳入普通Git源码提交。

## Decisions

### 1. 使用声明式回归矩阵作为唯一编排来源

新增 `verification/regression-matrix.json`，每个检查项包含唯一ID、关联需求、适用配置、命令、工作目录、超时、阻断级别和证据层级。选择JSON是因为Python标准库和PowerShell均可直接解析，不引入YAML第三方依赖。

替代方案是把命令直接硬编码在PowerShell脚本中；该方案难以审查需求覆盖率，也不利于GitHub和本地复用，因此不采用。

### 2. 使用Python标准库实现核心Harness，PowerShell提供Windows入口

`tools/verification/quality_gate.py` 负责矩阵校验、命令执行、超时、结果归集、Git元数据、报告和退出码；`tools/verification/run_quality_gate.ps1` 只负责定位可用Python并传递参数。这样核心逻辑可以被单元测试，Windows用户仍然获得一条直接命令。

Harness提供 `quick`、`full`、`release` 三个profile。后一级包含前一级的必需能力，但可采用更适合该层级的聚合命令以避免重复执行同一测试。

### 3. 对失败采用明确的失败关闭语义

必需命令的非零退出、超时、启动失败、缺失命令、矩阵错误或报告写入失败都产生非零Harness退出码。现场硬件项只有在矩阵明确标记为 `field` 时可以显示为待验证；涉及对应驱动变化时，发布profile将其升级为人工阻断项。

### 4. 把EXE重建判定独立为可测试规则

新增 `verification/exe-rebuild-rules.json`，规则把文件模式分成“外部更新”“必须重建”和“需要人工确认”。Harness基于Git比较范围或显式文件清单作出判定。

当判定为复用时，发布检查接收基线EXE路径和基线SHA-256，更新后再次计算并强制一致。Harness本身不复制或覆盖EXE，减少误操作风险。

当判定为重建时，Harness只输出 `rebuild_required` 及理由；真正的构建仍由现有模块化构建脚本执行，并在构建后重新运行release profile。

### 5. GitHub只执行无硬件检查，本机承担交付和现场证据

`.github/workflows/quality-gate.yml` 在Windows runner执行矩阵结构测试和可移植的自动化回归。依赖本机EXE、MySQL、Tailscale、Cloudflare或真实传感器的检查不在无凭据GitHub环境运行，而在报告中标为本机或现场层级。

稳定分支可以把GitHub的 `quality-gate` 状态设为必需检查。现场验收证据通过本机release报告补充，不伪造成云端自动检查。

### 6. 发布报告与源码提交分离

报告写入 `verification/results/`，默认作为运行产物排除出普通源码提交；规格、矩阵、Harness和固定测试进入Git。需要留档时可把选定报告作为GitHub Release附件或正式验收材料保存。

## Risks / Trade-offs

- [完整回归耗时增加] → quick profile用于日常开发，full和release只在合并、发布时运行。
- [历史测试存在环境依赖] → 矩阵明确工作目录、超时和证据层级；缺少依赖时失败或明确待验证，不静默跳过。
- [路径和Python环境在不同电脑不一致] → PowerShell入口按项目配置和常见解释器顺序定位，并在报告中记录最终解释器。
- [GitHub无法验证真实设备] → 将云端、本机和现场证据分层，涉及硬件的发布要求人工签署现场结果。
- [变更文件模式误判EXE重建] → 未匹配文件进入人工确认，不自动重建；规则本身具有单元测试。
- [门禁脚本被绕过] → GitHub分支保护负责合并门禁，正式发布入口检查最新release报告的提交号和通过状态。

## Migration Plan

1. 先加入规格、矩阵、Harness单元测试和空实现，验证测试能够正确失败。
2. 实现矩阵校验、命令执行、报告和退出码，使Harness测试通过。
3. 接入现有测试入口并运行quick/full配置，修正仅属于编排或环境声明的问题。
4. 加入EXE重建判定与哈希不变测试，不执行实际EXE重建。
5. 加入GitHub Actions，并在远端验证无硬件检查可运行。
6. release profile本机验证通过后，再把质量检查配置为稳定分支必需状态。

回滚时可移除GitHub必需检查并回退本变更提交；Harness不迁移业务数据，也不修改现有EXE，因此不会影响现有软件继续运行。
