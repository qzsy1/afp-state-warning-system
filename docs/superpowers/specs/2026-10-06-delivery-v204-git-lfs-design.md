# v2.0.4 交付包 Git LFS 同步设计

## 目标

将 `delivery/AFP_Integrated_System_Modular_v2.0.4_Agentic/` 作为仓库中唯一通过 GitHub 同步的完整交付版本，使第二台 Windows 电脑可以通过 Git 和 Git LFS 获得与当前电脑一致的可运行软件。

源码、OpenSpec、构建脚本继续使用普通 Git。大型二进制、模型和数据文件使用 Git LFS。旧版 `v2.0.3` 继续保留在本机，但不纳入版本控制。

## 当前约束

- `v2.0.4` 当前约 1.264 GB，共 6,335 个文件。
- 最大文件 `_internal/torch/lib/torch_cpu.dll` 约 239.62 MB，不能作为普通 Git 对象推送到 GitHub。
- 完整交付目录包含程序、Python 运行时、模型、演示数据，也包含日志、运行状态、验证输出和缓存等机器专属或可再生成内容。
- 当前机器已安装 Git LFS；第二台电脑也必须执行 `git lfs install`。

## 版本控制边界

### 纳入同步

- `AFP_Integrated_System_Modular.exe` 与本地采集 Helper。
- `_internal/` 中运行软件所需的解释器、依赖库和资源。
- `app/`、`config/`、`docs/`、`models/`、`native_dll/` 中构成发布版本的不可变内容。
- 安装、启动和网络访问脚本。
- `README.txt`、`VERSION.json`、`SHA256SUMS.txt` 等版本与完整性文件。

### 排除同步

- `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/` 及其他旧交付版本。
- `logs/`、`runtime/`、`rollback/`。
- `updates/inbox/`、`updates/staging/` 中的临时更新文件。
- `verification/` 中生成的运行证据和功能测试输出。
- `__pycache__/`、`*.pyc`、PID、日志和临时文件。
- 本机令牌、密码、DPAPI 数据、机器专属路径与本地配置。

上述可变目录由程序在首次运行时创建，不要求 Git 保存空目录。

## Git 与 Git LFS 设计

1. 保留 `.gitignore` 对 `/delivery/` 的默认排除，在文件末尾增加只针对 `AFP_Integrated_System_Modular_v2.0.4_Agentic` 的放行规则。
2. 在放行规则之后重新排除可变目录和机器专属内容，确保它们不会因目录放行而被加入。
3. 在 `.gitattributes` 中为交付目录内的 EXE、DLL、PYD、模型、压缩包和大型数据类型配置 Git LFS。
4. 提交前执行大小审计；任何未由 LFS 管理且达到 45 MiB 的文件都使发布失败，防止触发 GitHub 普通对象限制。
5. 使用 `git lfs ls-files` 和 `git check-attr` 验证大型文件确实由 LFS 管理，再提交和推送。

## 第二台电脑同步流程

首次同步：

```powershell
git lfs install
git clone https://github.com/qzsy1/afp-state-warning-system.git
cd afp-state-warning-system
git lfs pull
```

已有仓库：

```powershell
git switch main
git pull --ff-only origin main
git lfs pull
```

若已有仓库中存在此前被忽略、未由 Git 管理的同名 v2.0.4 目录，首次更新前先将该目录重命名为 `_local_backup`，再执行上述命令，避免 Git 拒绝覆盖未跟踪文件。新目录通过完整性检查和自检后再清理备份。

同步后执行：

```powershell
.\delivery\AFP_Integrated_System_Modular_v2.0.4_Agentic\AFP_Integrated_System_Modular.exe --verify-files
.\delivery\AFP_Integrated_System_Modular_v2.0.4_Agentic\AFP_Integrated_System_Modular.exe --self-test
```

两台电脑的 `git rev-parse HEAD`、`VERSION.json` 和 `SHA256SUMS.txt` 一致，且完整性检查通过时，认定交付版本一致。本机运行日志和采集数据允许不同。

## 上传与验证顺序

1. 扫描交付目录中的密钥、令牌、本机路径和可变文件。
2. 调整 `.gitignore` 和 `.gitattributes`。
3. 只暂存 `v2.0.4` 的发布内容，核对暂存文件数量、大小和 LFS 属性。
4. 执行源码回归测试、交付包 `--verify-files` 与 `--self-test`。
5. 提交到当前 `main`，推送 Git 对象和 LFS 对象。
6. 从远端重新获取引用并核对本地、远端提交号。
7. 更新 README，记录另一台电脑的首次同步和日常更新命令。

## 失败处理

- 若 GitHub LFS 配额不足或服务器拒绝大对象，停止推送并保留本地提交，不改写远端历史；后续改用 GitHub Release 资产。
- 若敏感信息扫描命中，先从交付目录移除或改为运行时生成，再重新创建完整性清单。
- 若交付包自检或文件校验失败，不提交交付目录，先修复组装结果。
- 不使用强制推送，不改写现有 `main` 历史。

## 完成标准

- 远端 `main` 包含 `v2.0.4` 交付目录的受控内容，不包含 `v2.0.3`。
- 所有超过审计阈值的大文件均由 Git LFS 管理。
- GitHub 推送成功，本地与 `origin/main` 提交号一致。
- 源码回归测试、交付完整性检查和交付自检通过。
- 第二台电脑具备明确的 Git LFS 同步与验证说明。
