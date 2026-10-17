# AFP 实时预测、三级状态预警与模型训练系统

本项目面向自动铺丝（AFP）过程，集成多接口传感器采集、模拟回放、时序预测、在线健康指标、窗口级—铺层级—试样级状态预警、CSV/MySQL 保存、数据整合以及预测/预警模型训练。

当前开发分支 `feature/modular-runtime-v2` 已完成模块化改造：桌面 EXE 作为稳定启动器，采集、驱动、存储、预测、健康指标、预警、训练、数据整合、界面和配置均保留为外部模块。普通业务修改不再需要重新生成整个 EXE。

## 主要功能

- 当前 `new_collection_v12_0` 采集模式包含六个逻辑接口和 21 个传感器通道；模型仍按检查点元数据使用 16 个输入通道；
- ESR1001-A 通过独立 RS485 串口以 Modbus RTU 读取四个微应变通道；
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

本机只维护一个受支持的完整运行目录：`F:\new_AFP_exam\AFP_Integrated_System_Modular`。源码固定在 `F:\new_AFP_exam\AFP-Readiness-Repo`，脱敏证据固定在 `F:\new_AFP_exam\AFP-Main-Evidence`。构建中间产物只能进入 `F:\new_AFP_exam\.afp-staging`，发布完成或失败后受控清理；candidate ID 只作为 manifest 和审计身份，不再作为长期目录名。

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
& 'F:\new_AFP_exam\AFP_Integrated_System_Modular\AFP_Integrated_System_Modular.exe' --verify-files
& 'F:\new_AFP_exam\AFP_Integrated_System_Modular\AFP_Integrated_System_Modular.exe' --self-test
```

更新流程先在 `.afp-staging` 完成组装、完整性、自检和模块检查；随后输出准确的监听进程、目录、影响和恢复方法，等待“切换审批”。只有明确批准后才停止已锁定的旧 AFP 进程，并以同卷目录交换方式提升到固定运行目录。服务门禁全部通过后，启动项修改仍需单独授权。

旧候选、旧 delivery 或旧证据副本不会自动删除。系统先逐项输出绝对路径、大小、活动引用和 Git 重建回滚方法，再等待独立的“清理审批”。回滚使用已记录的 Git 提交或标签重新在 staging 构建并通过相同门禁，不依赖长期保留多套可运行目录。

两台电脑的 `git rev-parse HEAD` 相同且 `--verify-files` 通过时，发布程序内容一致。原始采集数据、正式实验数据库、个人路径和运行时状态继续保留在各自电脑中。

模拟采集、OOD 和弱标签预警只表示模型或协议证据，不等同于真实传感器或真实双机验收。只有尚未执行的真实传感器现场项可以标记 `physical_sensor_pending`；Helper、网络、MySQL、Windows readiness 等未完成项必须使用 `blocked` 或 `failed`。生产部署和论文结论应使用真实传感器、独立缺陷检测或力学性能证据重新确认阈值、精度和适用范围。

ESR1001-A 现场接线使用设备独占的 USB-RS485 或原生串口，参数为 9600、8N1、默认从站地址 1。必须核对 A/B 极性、总线终端条件和设备固件对 FC04 `0x0030` 实数寄存器的支持；软件测试通过不替代逐通道已知输入和 600 秒实物验收。
