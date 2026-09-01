# AFP modular native runtime

This version keeps the desktop experience while separating the product into
independently maintainable modules.  The EXE is a stable loader; acquisition,
drivers, prediction, health indicators, warning, storage, training, data
integration, UI and configuration remain external.

## Source-mode checks

```powershell
& "F:\program\channel_independent_MTSF-main\.venv\Scripts\python.exe" .\modular_runtime\launcher_entry.py --module-status
& "F:\program\channel_independent_MTSF-main\.venv\Scripts\python.exe" .\modular_runtime\launcher_entry.py --self-test
```

The packaged delivery also supports `--integration-smoke` for the embedded UI
service and `--functional-smoke` for the full simulation acquisition,
prediction/warning and one-epoch training chain.  Run `--verify-files` to check
all immutable delivery files against `SHA256SUMS.txt`; acquisition records,
logs, rollback snapshots and verification outputs are deliberately excluded.

## One-time launcher build

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\modular_runtime\build_modular_app.ps1
```

After that build, changing `app/modules`, `app/legacy`, `app/ui`, `config` or
model files does not require a new EXE.  Restart the affected module/application
and run `--self-test`.  Rebuild only when the Python runtime, PyTorch, pywebview,
PyInstaller or a native dependency must change.
