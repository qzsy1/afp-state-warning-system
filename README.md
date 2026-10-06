# AFP 实时预测、三级状态预警与模型训练系统

本项目面向自动铺丝（AFP）过程，集成多接口传感器采集、模拟回放、时序预测、在线健康指标、窗口级—铺层级—试样级状态预警、CSV/MySQL 保存、数据整合以及预测/预警模型训练。

当前开发分支 `feature/modular-runtime-v2` 已完成模块化改造：桌面 EXE 作为稳定启动器，采集、驱动、存储、预测、健康指标、预警、训练、数据整合、界面和配置均保留为外部模块。普通业务修改不再需要重新生成整个 EXE。

## 主要功能

- 旧数据、新数据（16 个传感器）和自定义数据模式；
- 真实采集与模拟采集严格分离，支持多接口、接口角色和通道双向映射；
- CSV 单文件、采集文件夹和采集格式 MySQL 数据源；
- 多算法预测模型与权重匹配，支持预测步长 1–600；
- TC-HI、T-HI、C-HI、RFHI、PR-HI、MPRF-HI、PCA-SPE-HI、KECA-SPE-HI；
- 窗口级、铺层级和按实际铺层数形成的试样级状态证据；
- 本地文件优先保存、MySQL 关系表/外键/索引/平面视图及数据整合；
- 预测模型训练和预测—预警联合训练，支持 epoch、patience、继续训练、停止并保存；
- 模块健康检查、文件完整性校验、独立更新和回滚。

## 模块化源码入口

- `modular_runtime/launcher_entry.py`：稳定启动器入口；
- `modular_runtime/app/bootstrap.py`：外部业务启动和诊断入口；
- `modular_runtime/app/core`：模块契约、装载、事件和更新回滚；
- `modular_runtime/app/modules`：九个可独立维护的业务模块；
- `visualization_app`：已经验证的原有业务兼容实现与前端；
- `modular_runtime/build_launcher.ps1`：仅构建稳定启动器EXE；
- `modular_runtime/assemble_modular_delivery.ps1`：复用已有启动器并组装外置业务文件、配置和完整性清单；
- `modular_runtime/build_modular_app.ps1`：保持原参数兼容的构建/组装编排入口；
- `modular_runtime/scripts/create_module_patch.py`：按 Git 差异制作模块补丁。

详细方案见 [模块化 Git 开发维护与发布方案](docs/模块化Git开发维护与发布方案.md)，当前复测见 [模块化 v2 验证报告](docs/模块化v2验证报告.md)。

## 开发验证

```powershell
python modular_runtime/launcher_entry.py --module-status
python modular_runtime/launcher_entry.py --self-test
python -m unittest discover -s modular_runtime/tests -v
```

Windows 发布软件还支持：

```powershell
AFP_Integrated_System_Modular.exe --self-test
AFP_Integrated_System_Modular.exe --verify-files
AFP_Integrated_System_Modular.exe --integration-smoke
AFP_Integrated_System_Modular.exe --functional-smoke
```

无控制台软件会把命令结果写入 `runtime/*_result.json`。

## 使用与发布边界

完整 Windows 软件文件夹可直接复制到其它 Windows 电脑，目标电脑不需要另装 Python 或 PyTorch，但必须保持 EXE、`_internal`、`app`、`config`、`models` 和 `native_dll` 的相对位置。使用 MySQL 时需要可访问的 MySQL Server；使用真实传感器时需要相应设备、驱动、通信参数与权限。

当前唯一受支持的完整交付版本为 `delivery/AFP_Integrated_System_Modular_v2.0.4_Agentic/`。该目录中的程序、依赖、模型和演示数据通过 Git LFS 同步；日志、运行状态、回滚目录、验证输出、采集数据和本机配置不进入 Git。

另一台电脑首次同步需要安装 Git LFS：

```powershell
git lfs install
git clone https://github.com/qzsy1/afp-state-warning-system.git
cd afp-state-warning-system
git lfs pull
```

已有仓库日常更新：

```powershell
git switch main
git pull --ff-only origin main
git lfs pull
.\delivery\AFP_Integrated_System_Modular_v2.0.4_Agentic\AFP_Integrated_System_Modular.exe --verify-files
.\delivery\AFP_Integrated_System_Modular_v2.0.4_Agentic\AFP_Integrated_System_Modular.exe --self-test
```

两台电脑的 `git rev-parse HEAD` 相同且 `--verify-files` 通过时，发布程序内容一致。原始采集数据、正式实验数据库、个人路径和运行时状态继续保留在各自电脑中。

模拟/OOD/弱标签预警只表示模型证据，不等同于独立确认的真实缺陷。生产部署和论文结论应使用真实传感器、独立缺陷检测或力学性能证据重新确认阈值、精度和适用范围。
