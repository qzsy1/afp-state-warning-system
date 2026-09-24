## Purpose

保证模块化软件在绝大多数业务修改中继续使用已验证的稳定启动器EXE，避免重复生成大型文件，同时对真正需要重建EXE的情况提供明确、可审计的判定与验证规则。

## ADDED Requirements

### Requirement: 默认必须复用稳定EXE
除非本次变更命中明确的EXE重建条件，否则发布流程 MUST 复用现有稳定EXE，只同步外部业务代码、界面、配置、模型或文档。

#### Scenario: 普通业务修改
- **WHEN** 本次变更仅涉及Python业务逻辑、HTML、CSS、JavaScript、SQL、配置、模型、测试或文档
- **THEN** 发布流程 MUST 使用原EXE且不得调用PyInstaller重建

#### Scenario: 外部文件更新
- **WHEN** 更新外部 `app`、`ui`、`config` 或其它可热更新目录
- **THEN** 原EXE MUST 能加载更新后的文件并通过对应回归测试

### Requirement: EXE重建条件必须显式限定
系统 MUST 仅在Python或关键运行时版本变化、新增原EXE未包含的第三方依赖、新增或更换原生DLL、修改启动器/PyInstaller/PyWebView入口、修改图标或签名、或产生模块API不兼容变化时允许重建EXE。

#### Scenario: 命中重建条件
- **WHEN** 变更清单命中至少一个明确重建条件
- **THEN** 发布记录 MUST 写明触发文件、触发规则、重建原因和审批结论后才允许执行重建

#### Scenario: 未知文件类型
- **WHEN** Harness无法判断某个变更是否影响EXE运行时
- **THEN** Harness MUST 返回需要人工确认，不得自动选择重建或自动判为无需重建

### Requirement: 复用路径必须验证EXE哈希不变
在无需重建EXE的发布中，Harness MUST 比较更新前后的EXE SHA-256，任何变化都必须导致发布门禁失败。

#### Scenario: 原EXE保持不变
- **WHEN** 外部业务文件同步完成且EXE哈希与基线一致
- **THEN** Harness MUST 记录哈希一致并继续执行原EXE验收

#### Scenario: EXE意外变化
- **WHEN** 无重建授权但EXE哈希发生变化
- **THEN** Harness MUST 立即阻止发布并报告旧哈希、新哈希和文件路径

### Requirement: 重建EXE必须执行增强验收
经授权重建的EXE MUST 记录构建环境、依赖、旧哈希、新哈希和重建原因，并通过完整交付验证后才能替换稳定版本。

#### Scenario: 重建成功但验证失败
- **WHEN** 新EXE生成成功但任一完整性、自检、集成或功能冒烟失败
- **THEN** 新EXE MUST NOT 替换稳定EXE，且发布结果必须为失败

### Requirement: 发布不得制造重复版本目录
普通外部文件更新 MUST 在现有受Git管理的源码和既定交付目录中进行，不得为每次修改创建新的重复源码树或版本目录。

#### Scenario: 无需重建的版本更新
- **WHEN** 发布普通业务修改
- **THEN** 系统 MUST 更新既定交付位置并依赖Git提交和标签管理版本，不得复制出新的完整项目目录
