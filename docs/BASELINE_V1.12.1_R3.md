# AFP integrated system baseline

- Baseline date: 2026-09-01
- Source snapshot: `state_monitor_v13/visualization_app`
- Reference release: `AFP_Integrated_System_SMRF_HID_Restored_20260824_v1.12.1`
- Development branch: `feature/modular-runtime-v2`
- Installed Git: `F:\software\Git\cmd\git.exe`

## Acceptance evidence

- Reference EXE `--self-test`: passed, exit code 0.
- Reference EXE `--integration-smoke`: passed, exit code 0.
- Source-only test subset: 12 tests and 12 subtests passed.
- The legacy source test suite still has a pre-existing prediction checkpoint/model-structure mismatch for one I-ModernTCN-GAT artifact. The reference packaged EXE remains the behavioral baseline while the modular runtime introduces explicit model compatibility checks.

The original release and the original workspace are not overwritten. All modular work is isolated in this repository and branch.
