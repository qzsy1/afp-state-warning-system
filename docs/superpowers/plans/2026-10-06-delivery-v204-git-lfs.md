# v2.0.4 交付包 Git LFS 实施计划

1. 审计 `delivery/AFP_Integrated_System_Modular_v2.0.4_Agentic/` 的敏感信息、机器专属状态和生成文件。
2. 调整 `.gitignore`，仅放行 v2.0.4 的不可变发布内容。
3. 调整 `.gitattributes`，将大型二进制、模型与数据类型交给 Git LFS。
4. 更新根 README，记录第二台电脑首次同步、日常更新和完整性验证命令。
5. 暂存交付目录并审计文件数量、Git LFS 覆盖及未由 LFS 管理的大文件。
6. 运行源码测试、JavaScript 语法检查、交付包 `--verify-files` 和 `--self-test`。
7. 提交全部发布同步改动，推送普通 Git 对象和 Git LFS 对象到 `origin/main`。
8. 重新获取远端引用，确认本地与远端提交一致。
