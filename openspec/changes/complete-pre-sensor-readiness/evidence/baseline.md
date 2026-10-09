# 实施基线

- 日期：2026-10-09（Asia/Shanghai）
- 起始主分支：`main@2047f358808c50ba8c5f85a036a100e6f738e6eb`
- 起始提交：`feat: harden dual-mode real acquisition`
- `codex/prepare-real-acquisition`：`2047f358808c50ba8c5f85a036a100e6f738e6eb`
- 实施分支：`codex/complete-pre-sensor-readiness`
- 独立工作树：`D:\AFP_Integrated_Modular_v2\.worktrees\complete-pre-sensor-readiness`
- OpenSpec 严格校验：通过，`Change 'complete-pre-sensor-readiness' is valid`
- Harness 单元与合同：128/128 通过
- Harness quick：9/9 通过，32.59 秒

## 正式 delivery 只读基线

- 根目录：`delivery/AFP_Integrated_System_Modular_v2.0.4_Agentic`
- 受管文件数：6176
- 树摘要：`8fd1ab9709215101075bdea283b40895d52d107a389eea42e1683d0e0a6cc4d4`
- 逐文件清单：正式目录内 `SHA256SUMS.txt`，6175 项；清单自身 SHA-256 为 `7e050aec030a9945df8ba4fcc5388536f1afc78d435f50f6ccf33d7e9b9681ab`
- 固化元数据：`harness/config/delivery-readonly-baseline.json`
- 摘要算法：按相对路径排序，对每项的 `相对路径 + NUL + 文件SHA-256 + LF` 计算整体 SHA-256；使用 `harness.engine.real_acquisition_preflight.capture_delivery_snapshot` 的受管文件边界。

该摘要必须在最终验证中重新计算。任何受管文件增加、删除或改变均阻止完成结论。

- 合同 RED：`python -m unittest harness.tests.test_delivery_readonly_baseline -v` 因 `harness.engine.delivery_readonly` 不存在而导入失败。
- 合同 GREEN：同一测试 1/1 通过；`python -m harness.engine.delivery_readonly` 实际验证 6176 个受管文件通过。
- Harness 接入 GREEN：矩阵/profile/包装入口合同 31/31 通过，`delivery-readonly-baseline` 为 quick/full/release 阻塞检查。

