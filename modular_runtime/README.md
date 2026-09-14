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

## Public HTTPS access (Cloudflare Tunnel)

The delivery contains two listeners that share one dashboard: public guests
use `http://<LAN-IP>:8770/` (or the Cloudflare HTTPS hostname), while the
desktop administrator window uses `http://127.0.0.1:8771/`. The public page
starts in simulation mode; real serial/USB/PLC/ABB/UVC/M3232 controls and the
server-stored SiliconFlow key are unlocked only after the administrator
password is entered over HTTPS. The browser never receives the key.

To publish it, add your domain to Cloudflare, create a named Tunnel, and route
the selected hostname to `http://127.0.0.1:8770`. Add a catch-all route that
returns HTTP 404. Install the dashboard-provided `cloudflared` service on the
same PC; keep its token out of Git, screenshots, logs, and chat. Do not enable
router port forwarding or open WAN port 8770. Configure no-cache rules for
HTML and `/api/*`, plus rate limits for `/api/auth/login`,
`/api/agent/diagnose`, simulation start, and downloads (availability depends
on the Cloudflare account plan).

Set the same hostname in the local administrator settings. Use the public
HTTPS URL when unlocking from LAN or the Internet; direct LAN HTTP remains
guest-only. Revoke any previously exposed SiliconFlow key, enter a newly
created key locally, and verify it is absent from browser responses and logs.
If the Tunnel is unavailable, continue with the local desktop or LAN guest URL
and inspect the local Tunnel status panel.
