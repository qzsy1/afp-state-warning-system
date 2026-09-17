# AFP Edge Capture Gateway Design

## Goal

Make a LAN or authorized public browser use the serial, USB, Ethernet, UVC,
ABB, PLC and MySQL resources of the computer that opened the page, while the
server continues to run prediction, warning and diagnosis.

## Identity and execution boundary

- `local_admin`: loopback browser on the server; hardware executes on server.
- `lan_operator`: direct RFC1918 browser; unique cookie-backed session; real
  hardware executes through its paired local helper.
- `authorized`: password-unlocked public browser; real hardware executes
  through its paired local helper.
- `guest`: simulation only.

Authorization role and acquisition execution target are separate concepts.
Only loopback is allowed to use server-local hardware without a helper.

## Data path

The helper owns the existing `AcquisitionManager`. It sends ordered batches of
10-20 rows with `capture_uuid`, `sequence`, timestamps, status and schema. The
server keeps one `RemoteAcquisitionMirror` per browser session. `/api/live` and
`/api/live/ws` select that mirror for `lan_operator` and `authorized` sessions,
so server-side prediction and warning consume the same rows captured on the
visitor computer.

Each batch remains pending in the helper until acknowledged. The server
deduplicates `(capture_uuid, sequence)` and returns an acknowledgement. A
reconnect resends the unacknowledged batch instead of silently losing it.

## Pairing and transport

LAN and public real-control sessions expose the same one-time pairing flow.
The helper token remains DPAPI-protected on Windows and only its hash is stored
by the server. HTTPS/WSS is preferred. Plain HTTP/WS is accepted only for a
loopback or literal private-network address; public plaintext origins remain
rejected.

The helper advertises protocol and schema versions. An incompatible helper is
shown as paired but unavailable for real capture, with an explicit upgrade
message. Cached interface mappings are display-only while the helper is
offline and must never fall back to server-local discovery.

## Mode isolation

Simulation remains session-local and does not require the helper. Switching
between simulation and real capture restores the mapping and status belonging
to that mode. PLC process pressure and M3232 thin-film pressure remain separate
channels.

## Persistence boundary

CSV and "local MySQL" operations for helper-backed real capture execute on the
visitor computer. Existing server/target MySQL behavior is not refactored.
Passwords and API keys are not included in sample messages or logs.

## Failure semantics

- No helper or stale heartbeat: show offline and block real start.
- Helper online but no first sample: remain in waiting state, not collecting.
- Browser/network disconnect: helper continues capture and local persistence;
  pending sample batches are replayed after reconnect.
- Duplicate/out-of-order sample: deduplicate or reject without corrupting the
  active mirror.
- Helper crash: preserve acquisition partial files using existing integrity
  behavior; do not report a finalized capture.

## Acceptance boundary

A complete software loop requires two computers: browser/helper B pairs with
server A; only B's interfaces appear; B starts capture; B's rows reach the
server mirror; prediction/warning use those rows; the browser receives live
updates; B saves CSV/MySQL; stop flushes and reports a final manifest; reconnect
does not duplicate rows. Actual sensor-driver validity still requires field
hardware testing.
