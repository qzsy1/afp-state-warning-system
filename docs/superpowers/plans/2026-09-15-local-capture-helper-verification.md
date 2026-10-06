# Local capture helper acceptance record

Date: 2026-09-15

## Verified

- Focused Python tests: 61 passed (`mysql_diagnostics`, `helper_relay`, `helper_transport`, `local_capture_agent`, `acquisition_integrity`, `frontend_guest_simulation`).
- Python byte-compilation and `node --check static/app.js` passed.
- Public route/security tests that do not import the optional model runtime passed; the remaining HTTP fixture tests require the delivery runtime's bundled PyTorch and are not runnable in the development interpreter.
- Local MySQL `127.0.0.1:3306/afp_state_warning` preflight passed with schema and transaction write test.
- A unique verification specimen was written to MySQL (`saved_rows=1`, visible through `afp_flat_all`) and removed from the underlying tables afterward.
- Local helper discovery enumerated the installed COM ports, USB/UVC placeholders and Ethernet adapters with `pyserial`/`psutil` available.
- Helper EXE `--help` and a bounded startup probe completed successfully.
- Source modules and static assets match the existing delivery tree by SHA-256.

## Delivery boundary

The browser can run guest simulation without hardware. Authorized real capture and saving to the visitor computer require pairing the helper EXE; the browser itself cannot open COM/USB/UVC or write the visitor's MySQL directly. Physical sensor data streaming still requires the corresponding devices and vendor drivers to be connected.

The public internet URL remains dependent on the user's Cloudflare named Tunnel and domain configuration; the local server and helper transport are ready for that deployment.
