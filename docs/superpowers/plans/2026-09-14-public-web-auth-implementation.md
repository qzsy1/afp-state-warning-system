# AFP Public Web Access and Persistent Authorization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build one HTTPS public AFP website that opens in isolated simulation mode, exposes sanitized real-interface status, and unlocks full real acquisition plus server-side SiliconFlow model use after password authentication.

**Architecture:** Keep the current hardware acquisition process authoritative on the acquisition PC. Add persistent server-side authorization, a separate per-browser guest simulation manager, a sanitized public hardware-status projection, and a single-owner real-control lease; serve the public/LAN surface on port 8770 and the trusted desktop-admin surface on loopback port 8771 using one shared `DashboardData`. Publish only port 8770 through a named Cloudflare Tunnel, while the application remains the final permission boundary.

**Tech Stack:** Python 3.11 standard library (`http.server`, `sqlite3`, `hashlib`, `hmac`, `secrets`, `ctypes`, `threading`), existing NumPy/Pandas acquisition stack, vanilla HTML/CSS/JavaScript, Windows DPAPI, Cloudflare Tunnel, PowerShell build scripts, `unittest`, Git.

**Spec:** `docs/superpowers/specs/2026-09-14-public-web-auth-design.md`

## Global Constraints

- Continue on Git branch `feature/original-ui-langchain-agent`; do not create a duplicate project checkout, delivery version directory, or second delivered EXE.
- Keep the existing delivery target `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/AFP_Integrated_System_Modular.exe` and update it in place only after all source tests pass.
- Public/LAN traffic uses port `8770`; the local desktop-admin surface uses `127.0.0.1:8771` and is never included in Cloudflare Tunnel ingress.
- Anonymous visitors may run only isolated simulation flows and read sanitized real-interface status. Real samples, hardware mutation, file/database configuration, training on real data, and SiliconFlow model use require authorization.
- Authorized server sessions have no application time expiration. Their persistent browser cookie is refreshed within browser limits; clearing browser storage removes the local token and requires login again. Server sessions become invalid only on logout, explicit revocation, password change, or security-store reset.
- The browser may use the configured SiliconFlow key but must never receive, display, copy, export, log, or persist the key.
- The old SiliconFlow key previously exposed in chat must be revoked before public deployment; only a newly issued key may be entered into the local desktop-admin settings.
- Public responses never expose passwords, password hashes, session tokens, API keys, database passwords, internal absolute paths, full device IPs, or USB serial numbers.
- Use server-side authorization for every protected route; disabled or hidden frontend controls are not a security boundary.
- Use TDD for each task and commit each independently testable result.

## File Structure

**Create**

- `visualization_app/web_auth.py` — password hashing, DPAPI secret protection, SQLite-backed permanent sessions, revocation, audit records, and safe public security status.
- `visualization_app/web_access.py` — request identity, CSRF checks, trusted-proxy HTTPS detection, application rate limits, and route permission decisions.
- `visualization_app/public_status.py` — convert discovery/check/acquisition state into an anonymous-safe real-interface status projection.
- `visualization_app/guest_simulation.py` — create isolated `AcquisitionManager` instances, force safe simulation configuration, enforce disk/session quotas, and package downloads.
- `visualization_app/control_lease.py` — single-owner real-control lease with heartbeat release and local-admin takeover.
- `visualization_app/test_public_web_security.py` — authentication, authorization, CSRF, rate-limit, model-secret, and HTTP integration tests.
- `visualization_app/test_guest_web.py` — public-status sanitization, guest simulation isolation, quota, and safe-download tests.
- `modular_runtime/app/core/public_web.py` — public-web configuration validation and read-only `cloudflared` Windows-service status inspection.
- `modular_runtime/tests/test_public_web.py` — public-web configuration, service-status, and dual-server bootstrap tests.

**Modify**

- `visualization_app/acquisition.py` — persist and safely return the most recent hardware-check snapshot without reopening hardware.
- `visualization_app/app.py` — inject shared services, create public/local-admin handler contexts, add auth/public/simulation/admin endpoints, and enforce permission policy before dispatch.
- `visualization_app/interface_agent.py` — keep model execution internal while allowing credentials supplied only by the authenticated server path.
- `visualization_app/static/index.html` — add access-mode badge, unlock dialog, logout/lock action, local-only security settings, and public Tunnel status.
- `visualization_app/static/app.js` — restore permanent sessions, send CSRF headers, select guest/real endpoints, remove browser API-key submission, and render permission errors.
- `visualization_app/static/styles.css` — style the compact authorization and Tunnel controls without changing the existing dashboard layout.
- `modular_runtime/app/bootstrap.py` — create one shared dashboard, start public and local-admin servers, inject runtime security paths/config, and shut both down cleanly.
- `modular_runtime/config/runtime.json` — add development `public_web` defaults.
- `modular_runtime/config/runtime.delivery.json` — add delivery `public_web` defaults.
- `modular_runtime/build_modular_app.ps1` — copy the new source modules into the existing delivery tree and verify they exist.
- `modular_runtime/README.md` — document local password/key setup, named Tunnel configuration, permanent-session revocation, and external acceptance steps.

---

### Task 1: Persistent Password, Secret, Session, and Audit Store

**Files:**
- Create: `visualization_app/web_auth.py`
- Create: `visualization_app/test_public_web_security.py`

**Interfaces:**
- Produces: `PasswordHasher.hash_password(password: str) -> str`
- Produces: `PasswordHasher.verify_password(password: str, encoded: str) -> bool`
- Produces: `SecretProtector.protect(value: bytes) -> bytes` and `unprotect(value: bytes) -> bytes`
- Produces: `SecurityStore(path: Path, protector: SecretProtector)`
- Produces: `SecurityStore.configure_owner(password: str, api_key: str, model_name: str) -> None`
- Produces: `SecurityStore.authenticate(password: str, user_agent: str, remote_label: str) -> tuple[str, AuthSession]`
- Produces: `SecurityStore.resolve_session(token: str) -> AuthSession | None`
- Produces: `SecurityStore.logout(token: str) -> None`, `revoke(session_id: str) -> None`, and `revoke_all() -> None`
- Produces: `SecurityStore.model_credentials() -> tuple[str, str]`
- Produces: `SecurityStore.safe_status() -> dict[str, object]`

- [ ] **Step 1: Write failing tests for password hashing and encrypted key storage**

```python
class ReversibleTestProtector:
    def protect(self, value: bytes) -> bytes:
        return b"enc:" + value[::-1]
    def unprotect(self, value: bytes) -> bytes:
        assert value.startswith(b"enc:")
        return value[4:][::-1]

def test_owner_configuration_never_persists_plain_secrets(self):
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "security.sqlite3"
        store = SecurityStore(db, ReversibleTestProtector())
        store.configure_owner("Correct-Horse-2026", "sk-private-value", "deepseek-ai/DeepSeek-V3")
        raw = db.read_bytes()
        self.assertNotIn(b"Correct-Horse-2026", raw)
        self.assertNotIn(b"sk-private-value", raw)
        self.assertEqual(store.model_credentials(), ("sk-private-value", "deepseek-ai/DeepSeek-V3"))
```

- [ ] **Step 2: Run the focused test and verify the module is missing**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.PublicWebAuthTests.test_owner_configuration_never_persists_plain_secrets -v
```

Expected: `ERROR` with `ModuleNotFoundError: No module named 'web_auth'`.

- [ ] **Step 3: Implement PBKDF2 hashing, DPAPI protection, and the SQLite schema**

Use the following stable formats and tables:

```python
PBKDF2_ITERATIONS = 600_000
SESSION_TOKEN_BYTES = 32

@dataclass(frozen=True, slots=True)
class AuthSession:
    session_id: str
    created_at: float
    last_seen_at: float
    user_agent: str
    remote_label: str

class PasswordHasher:
    @staticmethod
    def hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return "pbkdf2_sha256$%d$%s$%s" % (
            PBKDF2_ITERATIONS,
            base64.urlsafe_b64encode(salt).decode("ascii"),
            base64.urlsafe_b64encode(digest).decode("ascii"),
        )

    @staticmethod
    def verify_password(password: str, encoded: str) -> bool:
        algorithm, count, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(count))
        return hmac.compare_digest(actual, expected)
```

Create SQLite tables `settings`, `sessions`, and `audit_events`. Store only SHA-256 hashes of issued session tokens. `configure_owner` must increment an integer `auth_version`, revoke all existing sessions, encrypt the key with the injected protector, and reject passwords shorter than 12 characters.

- [ ] **Step 4: Add permanent-session and revocation tests**

```python
def test_session_survives_store_restart_until_explicit_revocation(self):
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "security.sqlite3"
        first = SecurityStore(db, ReversibleTestProtector())
        first.configure_owner("Correct-Horse-2026", "sk-private-value", "deepseek-ai/DeepSeek-V3")
        token, issued = first.authenticate("Correct-Horse-2026", "Chrome", "test-client")
        reopened = SecurityStore(db, ReversibleTestProtector())
        self.assertEqual(reopened.resolve_session(token).session_id, issued.session_id)
        reopened.revoke(issued.session_id)
        self.assertIsNone(reopened.resolve_session(token))

def test_password_change_revokes_every_existing_session(self):
    token, _ = self.store.authenticate("Correct-Horse-2026", "Chrome", "test-client")
    self.store.configure_owner("New-Password-2026", "sk-new", "deepseek-ai/DeepSeek-V3")
    self.assertIsNone(self.store.resolve_session(token))
```

- [ ] **Step 5: Implement authentication, safe status, and audit writes**

`authenticate` returns a raw token only once, stores `sha256(token)`, and records `login_success`/`login_failure`. `safe_status` returns only:

```python
{
    "configured": bool,
    "model_configured": bool,
    "model_name": str,
    "authorized_sessions": [{"session_id", "created_at", "last_seen_at", "user_agent", "remote_label"}],
}
```

No method returning browser-visible data may contain the encrypted key or password hash.

- [ ] **Step 6: Run the authentication test module**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.PublicWebAuthTests -v
```

Expected: all `PublicWebAuthTests` pass.

- [ ] **Step 7: Commit the security-store boundary**

```powershell
git add visualization_app/web_auth.py visualization_app/test_public_web_security.py
git commit -m "feat: add persistent public web authorization store"
```

---

### Task 2: Public Real-Interface Status Snapshot and Sanitization

**Files:**
- Create: `visualization_app/public_status.py`
- Create: `visualization_app/test_guest_web.py`
- Modify: `visualization_app/acquisition.py:2063-2122,2359-2561,3686-3857`

**Interfaces:**
- Produces: `AcquisitionManager.latest_check_result() -> dict[str, object]`
- Produces: `build_public_device_status(discovery: dict, check_result: dict, acquisition_status: dict, now: float | None = None) -> dict`
- Consumes later: public `GET /api/public/device-status` uses only these snapshot methods and never calls `test_connection` or opens a driver.

- [ ] **Step 1: Write a failing test proving sensitive endpoints are removed**

```python
def test_public_status_exposes_state_but_masks_physical_identifiers(self):
    discovery = {"physical_interfaces": [{"id": "serial:COM9", "endpoint": "COM9", "serial_number": "USB-SECRET"}]}
    check = {"checked_at": 100.0, "interfaces": [{
        "id": "film_pressure", "role": "film_pressure", "driver": "m3232_pressure",
        "endpoint": "COM9", "physical_interface_id": "serial:COM9", "state": "not_connected",
        "message": "访问 COM9 被拒绝", "ok": False,
    }]}
    payload = build_public_device_status(discovery, check, {"running": False}, now=101.0)
    encoded = json.dumps(payload, ensure_ascii=False)
    self.assertIn("M3232", encoded)
    self.assertIn("not_connected", encoded)
    self.assertNotIn("COM9", encoded)
    self.assertNotIn("USB-SECRET", encoded)
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_guest_web.PublicDeviceStatusTests.test_public_status_exposes_state_but_masks_physical_identifiers -v
```

Expected: `ERROR` because `public_status` does not exist.

- [ ] **Step 3: Cache the last hardware test inside `AcquisitionManager`**

Initialize `_latest_check_result = {"checked_at": None, "interfaces": [], "sensors": []}`. Before every return from `test_connection`, deep-copy the completed result, add `checked_at = time.time()`, and store it under `self.lock`. Implement:

```python
def latest_check_result(self) -> dict[str, Any]:
    with self.lock:
        return deepcopy(self._latest_check_result)
```

`reset_check_state` clears observation counters but preserves the last completed check so anonymous status does not disappear. A subsequent completed check replaces the snapshot atomically.

- [ ] **Step 4: Implement fixed role labels and message sanitization**

`build_public_device_status` must map roles to fixed public labels:

```python
PUBLIC_ROLE_LABELS = {
    "thermocouple": "SMRF八通道热电偶",
    "plc": "松下PLC",
    "uvc_temperature": "BSV UVC热像仪",
    "abb_robot": "ABB机器人",
    "film_pressure": "M3232薄膜压力传感器",
}
```

Return `role`, `label`, `driver`, `protocol`, `state`, `ok`, `message_code`, `checked_at`, and `age_seconds`. Convert raw messages into fixed public text such as “接口无法打开”“未检测到数据”“设备响应超时”; never copy raw exception text into the anonymous response.

- [ ] **Step 5: Add a no-probe regression test**

```python
def test_public_status_reads_cached_result_without_reopening_hardware(self):
    manager = AcquisitionManager(capture_root=self.root)
    manager._latest_check_result = {"checked_at": 10.0, "interfaces": [], "sensors": []}
    with patch.object(manager, "test_connection", side_effect=AssertionError("must not probe")):
        self.assertEqual(manager.latest_check_result()["checked_at"], 10.0)
```

- [ ] **Step 6: Run the public-status and existing acquisition tests**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_guest_web.PublicDeviceStatusTests visualization_app.test_acquisition_integrity -v
```

Expected: all selected tests pass and no driver is opened by the public projection.

- [ ] **Step 7: Commit the public-status projection**

```powershell
git add visualization_app/public_status.py visualization_app/test_guest_web.py visualization_app/acquisition.py
git commit -m "feat: expose sanitized cached hardware status"
```

---

### Task 3: Isolated Guest Simulation Sessions, Quotas, and Downloads

**Files:**
- Create: `visualization_app/guest_simulation.py`
- Modify: `visualization_app/test_guest_web.py`
- Modify: `visualization_app/app.py:2607-3621`

**Interfaces:**
- Produces: `GuestSimulationManager(root: Path, source_profiles: dict[str, dict], per_session_bytes: int, total_bytes: int, max_running: int)`
- Produces: `ensure_session(session_id: str) -> GuestSimulationSession`
- Produces: `safe_config(session_id: str, payload: dict) -> AcquisitionConfig`
- Produces: `start(session_id: str, payload: dict) -> dict`, `stop(session_id: str) -> dict`, `status(session_id: str) -> dict`
- Produces: `numeric_matrix(session_id: str) -> tuple[list[dict], list[float]]`
- Produces: `download_archive(session_id: str) -> tuple[bytes, str]`
- Modifies: `DashboardData.live(..., acquisition: AcquisitionManager | None = None) -> dict` defaults to the real manager but accepts a guest manager.

- [ ] **Step 1: Write failing isolation and forced-path tests**

```python
def test_two_guest_sessions_use_distinct_managers_and_save_roots(self):
    manager = GuestSimulationManager(self.root, {"builtin": self.profile}, 256 * 1024 * 1024, 2 * 1024**3, 4)
    first = manager.ensure_session("guest-a")
    second = manager.ensure_session("guest-b")
    self.assertIsNot(first.acquisition, second.acquisition)
    self.assertEqual(first.save_root, self.root / "guest-a")
    self.assertEqual(second.save_root, self.root / "guest-b")

def test_guest_payload_cannot_select_real_driver_or_arbitrary_path(self):
    config = self.manager.safe_config("guest-a", {
        "acquisition_mode": "real", "driver": "m3232_pressure", "save_root": "C:\\Windows",
        "simulation_source_type": "single_csv", "source_profile": "builtin",
    })
    self.assertEqual(config.acquisition_mode, "simulation")
    self.assertEqual(config.driver, "simulator")
    self.assertTrue(str(config.save_root).startswith(str(self.root)))
```

- [ ] **Step 2: Run the isolation tests and verify failure**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_guest_web.GuestSimulationTests -v
```

Expected: `ERROR` because `GuestSimulationManager` is missing.

- [ ] **Step 3: Implement guest-session construction and strict config rewriting**

Use a random 32-byte guest cookie value as `session_id`; accept only lowercase URL-safe IDs matching `^[A-Za-z0-9_-]{32,64}$`. Create each `AcquisitionManager` with:

```python
save_root = (public_root / session_id).resolve()
if public_root.resolve() not in save_root.parents:
    raise ValueError("invalid_guest_session")
acquisition = AcquisitionManager(capture_root=save_root)
```

`safe_config` selects an administrator-defined source profile and overwrites `acquisition_mode`, `driver`, `save_root`, source path, and all MySQL credentials. Reject profile names absent from `source_profiles`.

- [ ] **Step 4: Implement quota and concurrency enforcement**

Defaults are exactly:

```python
DEFAULT_PER_SESSION_BYTES = 256 * 1024 * 1024
DEFAULT_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_MAX_RUNNING = 4
```

Before starting and before final save, calculate files with `Path.rglob`; reject with `guest_quota_exceeded`, `guest_total_quota_exceeded`, or `guest_capacity_reached`. Stopped sessions remain downloadable. Cleanup is explicit from the local-admin settings; no timer may delete a running session.

- [ ] **Step 5: Extract acquisition selection from `DashboardData.live`**

Change the start of `live` to:

```python
active_acquisition = acquisition or self.acquisition
status = active_acquisition.status()
rows, timestamps = active_acquisition.numeric_matrix()
```

Leave all prediction, health, anomaly probability, and evidence calculations unchanged.

- [ ] **Step 6: Write and pass safe ZIP-download tests**

```python
def test_download_contains_only_current_guest_directory(self):
    own = self.root / "guest-a" / "result.csv"
    own.parent.mkdir(parents=True)
    own.write_text("x\n1\n", encoding="utf-8")
    other = self.root / "guest-b" / "secret.csv"
    other.parent.mkdir(parents=True)
    other.write_text("secret", encoding="utf-8")
    raw, name = self.manager.download_archive("guest-a")
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        self.assertEqual(archive.namelist(), ["result.csv"])
    self.assertNotIn(b"secret", raw)
    self.assertTrue(name.endswith(".zip"))
```

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_guest_web.GuestSimulationTests visualization_app.test_app.DashboardTests -v
```

Expected: all selected tests pass.

- [ ] **Step 7: Commit isolated guest simulations**

```powershell
git add visualization_app/guest_simulation.py visualization_app/test_guest_web.py visualization_app/app.py
git commit -m "feat: isolate public simulation sessions"
```

---

### Task 4: Request Identity, CSRF, Rate Limits, and Protected HTTP Routes

**Files:**
- Create: `visualization_app/web_access.py`
- Modify: `visualization_app/app.py:3893-4413`
- Modify: `visualization_app/test_public_web_security.py`

**Interfaces:**
- Consumes: `SecurityStore.resolve_session(token)` and `GuestSimulationManager`
- Produces: `RequestIdentity(role: Literal['guest','authorized','local_admin'], session_id: str | None, guest_id: str)`
- Produces: `PermissionPolicy.authorize(method: str, path: str, identity: RequestIdentity) -> AccessDecision`
- Produces: `SlidingWindowLimiter.allow(bucket: str, key: str, limit: int, window_seconds: float) -> bool`
- Produces: `is_secure_request(peer_host: str, headers: Mapping[str, str], access_context: str) -> bool`
- Produces: `AppHandler._identity()`, `_require_permission(path)`, `_require_csrf()`, and cookie helpers.

- [ ] **Step 1: Write a failing route-policy test**

```python
def test_guest_can_read_public_status_but_cannot_call_real_control(self):
    guest = RequestIdentity("guest", None, "guest-a")
    policy = PermissionPolicy()
    self.assertTrue(policy.authorize("GET", "/api/public/device-status", guest).allowed)
    denied = policy.authorize("POST", "/api/acquisition/start", guest)
    self.assertFalse(denied.allowed)
    self.assertEqual(denied.error, "real_access_required")
```

- [ ] **Step 2: Run the policy test and verify failure**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.PublicWebAccessTests.test_guest_can_read_public_status_but_cannot_call_real_control -v
```

Expected: `ERROR` because `web_access` is missing.

- [ ] **Step 3: Implement explicit route classes**

Use exact groups:

```python
PUBLIC_GET = {
    "/api/health", "/api/network/status", "/api/auth/session",
    "/api/public/device-status", "/api/simulation/status", "/api/simulation/live",
    "/api/simulation/download",
}
PUBLIC_POST = {"/api/auth/login", "/api/simulation/start", "/api/simulation/stop"}
AUTHORIZED_PREFIXES = ("/api/acquisition/", "/api/training/", "/api/mysql/", "/api/real/")
AUTHORIZED_EXACT = {"/api/agent/diagnose", "/api/auth/logout"}
LOCAL_ADMIN_PREFIX = "/api/admin/"
```

Unknown `/api/` paths remain `404`; they are not allowed by prefix accident. `/api/bootstrap`, `/api/live`, `/api/view`, and `/api/realtime` require authorization until Task 7 changes the guest UI to simulation endpoints.

- [ ] **Step 4: Add cookie, HTTPS, and CSRF handling**

- `afp_session`: `HttpOnly`, `SameSite=Strict`, persistent, `Secure` only when `_is_secure_request()` is true. The server record has no expiry; each authorized response refreshes the cookie with `Max-Age=2147483647`, while browser deletion or browser-enforced cookie retention limits still require login again.
- `afp_guest`: random identifier, `HttpOnly`, `SameSite=Lax`.
- `afp_csrf`: random 32-byte value, `SameSite=Strict`; JavaScript sends the same value as `X-AFP-CSRF`.
- Treat `X-Forwarded-Proto: https` as secure only when the TCP peer is loopback, because `cloudflared` connects locally.
- Reject state-changing requests with `403 csrf_failed` when the CSRF header and cookie differ.
- Raw LAN HTTP may use guest simulation and public status but must reject password login with `426 https_required`; loopback local-admin requests are exempt.

- [ ] **Step 5: Add HTTP tests proving backend denial occurs before hardware calls**

```python
def test_guest_real_start_is_denied_before_acquisition_manager(self):
    with patch.object(self.server.dashboard.acquisition, "start", side_effect=AssertionError("must not start")):
        status, payload, _ = self.request_json("POST", "/api/acquisition/start", {"acquisition_mode": "real"})
    self.assertEqual(status, 403)
    self.assertEqual(payload["error"], "real_access_required")

def test_spoofed_forwarded_proto_from_non_loopback_is_not_trusted(self):
    request = FakeRequest(peer=("192.168.1.50", 50000), headers={"X-Forwarded-Proto": "https"})
    self.assertFalse(is_secure_request(request))
```

- [ ] **Step 6: Implement login and session-status endpoints**

`POST /api/auth/login` accepts only `{"password": "..."}` and returns:

```json
{"authenticated": true, "role": "authorized", "model_access": true}
```

It sets `afp_session` but never includes the token in JSON. `GET /api/auth/session` returns role, `model_access`, `secure_transport`, and CSRF status. Login uses an application limiter of 5 failures per remote key per 600 seconds and blocks further attempts for 900 seconds.

- [ ] **Step 7: Add public-status and simulation HTTP endpoints**

- `GET /api/public/device-status` calls `latest_check_result` and `build_public_device_status` only.
- `POST /api/simulation/start` and `/stop` use the `afp_guest` session.
- `GET /api/simulation/status` and `/live` read the matching guest manager.
- `GET /api/simulation/download` calls `_send_download` with the safe guest ZIP.

- [ ] **Step 8: Run HTTP security and guest tests**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.PublicWebAccessTests visualization_app.test_guest_web -v
```

Expected: every guest denial, login, CSRF, public status, simulation, and download test passes.

- [ ] **Step 9: Commit the server-side permission boundary**

```powershell
git add visualization_app/web_access.py visualization_app/app.py visualization_app/test_public_web_security.py visualization_app/test_guest_web.py
git commit -m "feat: enforce public and authorized web permissions"
```

---

### Task 5: Single-Owner Real-Control Lease and Local Takeover

**Files:**
- Create: `visualization_app/control_lease.py`
- Modify: `visualization_app/app.py:4180-4392`
- Modify: `visualization_app/test_public_web_security.py`

**Interfaces:**
- Produces: `RealControlLease(heartbeat_timeout_seconds: float = 30.0)`
- Produces: `acquire(owner_id: str, label: str, now: float | None = None) -> LeaseDecision`
- Produces: `heartbeat(owner_id: str, now: float | None = None) -> LeaseDecision`
- Produces: `release(owner_id: str) -> bool`
- Produces: `force_takeover(owner_id: str = 'local-admin', label: str = '本机软件') -> LeaseDecision`
- Produces: `status(now: float | None = None) -> dict[str, object]`

- [ ] **Step 1: Write failing ownership, timeout, and takeover tests**

```python
def test_only_one_authorized_session_controls_real_acquisition(self):
    lease = RealControlLease(heartbeat_timeout_seconds=30)
    self.assertTrue(lease.acquire("session-a", "Chrome", now=0).granted)
    denied = lease.acquire("session-b", "Edge", now=10)
    self.assertFalse(denied.granted)
    self.assertEqual(denied.error, "real_control_busy")
    self.assertTrue(lease.acquire("session-b", "Edge", now=31).granted)

def test_local_admin_can_take_over_without_expiring_login(self):
    lease = RealControlLease()
    lease.acquire("session-a", "Chrome", now=0)
    self.assertTrue(lease.force_takeover().granted)
    self.assertEqual(lease.status(now=1)["owner_id"], "local-admin")
```

- [ ] **Step 2: Run and verify the missing-module failure**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.RealControlLeaseTests -v
```

Expected: `ERROR` because `control_lease` is missing.

- [ ] **Step 3: Implement the thread-safe lease**

Use `threading.Lock` and `time.monotonic`. A lease expires only when evaluating `acquire`, `heartbeat`, or `status`; expiration clears control ownership, not the `SecurityStore` session. Never stop acquisition when a lease expires.

- [ ] **Step 4: Add real-control endpoints and command guards**

Add:

- `POST /api/real/control/acquire`
- `POST /api/real/control/heartbeat`
- `POST /api/real/control/release`
- `GET /api/real/control/status`
- `POST /api/admin/real-control/takeover`

Require the current lease owner for real acquisition start/stop, interface reset/test, mapping changes, real training/import, and MySQL mutations. Repeated commands carry `X-AFP-Request-ID`; keep the most recent 256 IDs and return the stored response for a duplicate ID instead of executing twice.

- [ ] **Step 5: Test disconnect behavior and idempotency**

```python
def test_lease_expiry_does_not_stop_running_acquisition(self):
    self.lease.acquire("session-a", "Chrome", now=0)
    with patch.object(self.acquisition, "stop", side_effect=AssertionError("must keep running")):
        self.assertIsNone(self.lease.status(now=31)["owner_id"])

def test_duplicate_start_request_returns_first_response_once(self):
    with patch.object(self.server.dashboard.acquisition, "start", return_value={"running": True}) as start:
        first = self.authorized_post("/api/acquisition/start", self.real_payload, request_id="start-001")
        second = self.authorized_post("/api/acquisition/start", self.real_payload, request_id="start-001")
    self.assertEqual(start.call_count, 1)
    self.assertEqual(first, second)
```

- [ ] **Step 6: Run control and existing operation-lock tests**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.RealControlLeaseTests visualization_app.test_app.LanServerTests -v
```

Expected: lease tests and existing mutex regression tests pass.

- [ ] **Step 7: Commit real-control arbitration**

```powershell
git add visualization_app/control_lease.py visualization_app/app.py visualization_app/test_public_web_security.py
git commit -m "feat: arbitrate remote real acquisition control"
```

---

### Task 6: Authorized Server-Side SiliconFlow Credentials and Model Rate Limit

**Files:**
- Modify: `visualization_app/app.py:4028-4031,4186-4219`
- Modify: `visualization_app/interface_agent.py:602-619,1099-1150`
- Modify: `visualization_app/test_interface_agent.py`
- Modify: `visualization_app/test_public_web_security.py`

**Interfaces:**
- Consumes: `SecurityStore.model_credentials()` only for authorized/local-admin identities.
- Produces: guest `/api/agent/diagnose` response with local-rule results and `model_used=false`.
- Produces: authorized `/api/agent/diagnose` response from server-stored credentials and `model_used=true` on successful model output.
- Produces: `GET /api/agent/defaults` with `model_name`, `model_access`, and `default_key_available`; never the key.

- [ ] **Step 1: Write a failing test proving request-supplied keys are ignored**

```python
def test_guest_cannot_smuggle_an_api_key_in_request(self):
    with patch("interface_agent.run_interface_diagnoses", return_value={"diagnoses": []}) as run:
        status, payload, _ = self.request_json("POST", "/api/agent/diagnose", {
            "api_key": "sk-attacker", "model_name": "attacker/model",
            "events": [self.event], "hardware_result": {},
        })
    self.assertEqual(status, 200)
    self.assertEqual(run.call_args.kwargs["api_key"], "")
    self.assertNotEqual(run.call_args.kwargs["model_name"], "attacker/model")
```

- [ ] **Step 2: Run the focused test and confirm it fails against current behavior**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.ModelCredentialTests.test_guest_cannot_smuggle_an_api_key_in_request -v
```

Expected: `FAIL` because the current endpoint accepts browser credentials.

- [ ] **Step 3: Move credential resolution to the authenticated handler**

Keep `run_interface_diagnoses(events, api_key=..., model_name=...)` unchanged as an internal function. In the handler:

```python
if identity.role in {"authorized", "local_admin"}:
    api_key, model_name = self.security_store.model_credentials()
else:
    api_key, model_name = "", DEFAULT_SILICONFLOW_MODEL
```

Discard any `api_key` and `model_name` received from the public request. Build diagnostic events on the server from `hardware_result`, cached public status, and acquisition status. Continue the existing local-rule fallback when no key exists or model validation fails.

- [ ] **Step 4: Add model limit and audit behavior**

Allow one concurrent external diagnosis and at most 3 external model calls per authorized session per 60 seconds. A rejected call returns `429 model_rate_limited` while retaining the latest local-rule diagnosis. Audit `model_success`, `model_failure`, and `model_rate_limited` with model name and elapsed time only.

- [ ] **Step 5: Add secret-leak tests across responses and logs**

```python
def test_authorized_model_call_uses_store_but_never_returns_key(self):
    self.security.configure_owner("Correct-Horse-2026", "sk-private-value", "deepseek-ai/DeepSeek-V3")
    with patch("interface_agent.run_interface_diagnoses", return_value={"diagnoses": [], "model_used": True}) as run:
        status, payload, headers = self.authorized_post("/api/agent/diagnose", {"events": [self.event]})
    self.assertEqual(status, 200)
    self.assertEqual(run.call_args.kwargs["api_key"], "sk-private-value")
    self.assertNotIn("sk-private-value", json.dumps(payload, ensure_ascii=False))
    self.assertNotIn("sk-private-value", json.dumps(headers))
    self.assertNotIn(b"sk-private-value", self.security_path.read_bytes())
```

- [ ] **Step 6: Run model and agent regression tests**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.ModelCredentialTests visualization_app.test_interface_agent visualization_app.test_agentic_diagnosis -v
```

Expected: server-only credential tests and all existing agent tests pass.

- [ ] **Step 7: Commit model credential isolation**

```powershell
git add visualization_app/app.py visualization_app/interface_agent.py visualization_app/test_interface_agent.py visualization_app/test_public_web_security.py
git commit -m "feat: gate server-side model credentials by authorization"
```

---

### Task 7: Access-Aware Existing Frontend

**Files:**
- Modify: `visualization_app/static/index.html:20-285`
- Modify: `visualization_app/static/app.js:1-2550,3300-4050`
- Modify: `visualization_app/static/styles.css`
- Modify: `visualization_app/test_public_web_security.py`

**Interfaces:**
- Consumes: `/api/auth/session`, `/api/auth/login`, `/api/auth/logout`, public device-status, guest simulation, real-control lease, and safe agent-default endpoints.
- Produces: one existing dashboard UI with `guest`, `authorized`, and `local_admin` states.

- [ ] **Step 1: Add failing static-contract tests**

```python
def test_frontend_contains_access_state_controls_without_key_field(self):
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    self.assertIn('id="access-mode-badge"', html)
    self.assertIn('id="unlock-real-mode"', html)
    self.assertIn('id="real-access-password"', html)
    self.assertNotIn('id="agentApiKeyInput"', html)
    self.assertNotIn("api_key: controls", script)
    self.assertIn("/api/auth/session", script)
```

- [ ] **Step 2: Run the static-contract test and verify failure**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.FrontendAccessContractTests -v
```

Expected: `FAIL` because the access controls are absent and the old API-key input remains.

- [ ] **Step 3: Add the compact access UI without moving existing panels**

Add to the existing header:

- `#access-mode-badge` with “访客模拟模式”, “真实模式已解锁”, or “本机管理模式”.
- `#unlock-real-mode`, `#lock-real-mode`, and `#real-control-owner`.
- A modal containing only `#real-access-password`, submit, cancel, and HTTPS-required text.

Replace the public API-key field with “服务器模型：未授权 / 已授权可用 / 未配置”. Move password, API Key, model name, authorized-session list, revoke buttons, and Tunnel settings into a collapsible `#local-security-settings` panel rendered only for `local_admin`.

- [ ] **Step 4: Implement access bootstrap and CSRF headers**

```javascript
const accessState = { role: "guest", authenticated: false, csrf: "", secureTransport: false };

async function loadAccessSession() {
  const response = await fetch("/api/auth/session", {cache: "no-store", credentials: "same-origin"});
  Object.assign(accessState, await response.json());
  renderAccessState();
}

async function postJson(path, payload, options = {}) {
  const headers = {"Content-Type": "application/json", "X-AFP-CSRF": accessState.csrf, ...(options.headers || {})};
  return fetch(path, {method: "POST", headers, body: JSON.stringify(payload), credentials: "same-origin"});
}
```

Preserve the current timeout/abort behavior inside the existing `postJson`; merge these headers into it rather than introducing a second implementation.

- [ ] **Step 5: Route simulation and real actions by role**

- Guest start/stop/status/live use `/api/simulation/*`.
- Authorized/local-admin real start/stop continue to use `/api/acquisition/*` after acquiring the control lease.
- Clicking a protected control as guest opens the password modal instead of silently changing mode.
- Raw LAN HTTP displays “请使用公网 HTTPS 地址登录” and does not submit the password.
- Start a 10-second real-control heartbeat only while the current browser owns the lease; stopping the heartbeat does not log out.
- Model requests contain diagnostic events only; remove `api_key` and browser-supplied model credentials.

- [ ] **Step 6: Render sanitized and full hardware status in the same existing diagnostic panel**

Guest mode calls `/api/public/device-status` and labels the results “真实接口状态（只读）”. Authorized/local-admin mode uses the existing full discovery/check results. Do not create a second diagnostic-results interface.

- [ ] **Step 7: Run static contracts and Python HTTP regressions**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.FrontendAccessContractTests visualization_app.test_app.LanServerTests -v
```

Expected: all selected tests pass.

- [ ] **Step 8: Open the local test page and visually verify both roles**

Start the test server on a free local port, open the page, and verify:

- No large blank area or duplicated panel appears.
- Guest mode defaults to simulation.
- Real interface status appears in the existing diagnostic area.
- Protected actions open one password modal.
- Authorized mode exposes the original full controls.
- The right warning column, probability panel, and evidence borders remain aligned at full screen and narrow width.

Capture one guest screenshot and one authorized screenshot under `verification/public_web/`; keep this generated directory outside Git.

- [ ] **Step 9: Commit the frontend access flow**

```powershell
git add visualization_app/static/index.html visualization_app/static/app.js visualization_app/static/styles.css visualization_app/test_public_web_security.py
git commit -m "feat: add guest and unlocked modes to existing dashboard"
```

---

### Task 8: Dual Public/Local-Admin Servers and Local Security Settings

**Files:**
- Create: `modular_runtime/app/core/public_web.py`
- Create: `modular_runtime/tests/test_public_web.py`
- Modify: `visualization_app/app.py:4395-4413`
- Modify: `modular_runtime/app/bootstrap.py:406-460`
- Modify: `modular_runtime/config/runtime.json`
- Modify: `modular_runtime/config/runtime.delivery.json`

**Interfaces:**
- Produces: `PublicWebConfig.from_mapping(value: Mapping[str, Any]) -> PublicWebConfig`
- Produces: `inspect_cloudflared_service(service_name: str = 'cloudflared') -> dict[str, object]`
- Modifies: `create_server(host, port, network_status=None, *, dashboard=None, security_store=None, guest_manager=None, control_lease=None, access_context='public')`
- Produces local-only: `GET/POST /api/admin/security/settings`, `POST /api/admin/security/revoke`, `POST /api/admin/security/revoke-all`, and `POST /api/admin/simulation/cleanup`.

- [ ] **Step 1: Write failing public-web config tests**

```python
def test_public_web_config_has_fixed_public_and_admin_ports(self):
    config = PublicWebConfig.from_mapping({"enabled": True, "hostname": "afp.example.com"})
    self.assertEqual(config.public_port, 8770)
    self.assertEqual(config.local_admin_port, 8771)
    self.assertEqual(config.origin_url, "http://127.0.0.1:8770")

def test_public_hostname_rejects_scheme_path_and_ip(self):
    for value in ("https://afp.example.com", "afp.example.com/path", "192.168.1.2"):
        with self.subTest(value=value), self.assertRaises(ValueError):
            PublicWebConfig.from_mapping({"enabled": True, "hostname": value})
```

- [ ] **Step 2: Run the config tests and verify failure**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\modular_runtime\app\core;F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest modular_runtime.tests.test_public_web -v
```

Expected: `ERROR` because `public_web` is missing.

- [ ] **Step 3: Implement public-web configuration and service inspection**

```python
@dataclass(frozen=True, slots=True)
class PublicWebConfig:
    enabled: bool = False
    hostname: str = ""
    public_port: int = 8770
    local_admin_port: int = 8771
    cloudflared_service: str = "cloudflared"
    guest_session_quota_mb: int = 256
    guest_total_quota_mb: int = 2048
    max_running_guest_sessions: int = 4
```

When `enabled=True`, require a DNS hostname with no scheme, port, slash, or IP literal. `inspect_cloudflared_service` calls `sc.exe query <service>` with a 3-second timeout and returns only `installed`, `running`, `service_name`, and a fixed error code; never return the service command output because it may reveal installation paths.

- [ ] **Step 4: Refactor server creation so both handlers share one dashboard**

Create one `DashboardData`, one `SecurityStore` at `<runtime>/public_web_security.sqlite3`, one `GuestSimulationManager` rooted at `<capture_root>/public_simulation`, and one `RealControlLease`. Pass the same objects to both servers:

```python
public_server = create_server("0.0.0.0", 8770, status, dashboard=dashboard,
                              security_store=security, guest_manager=guests,
                              control_lease=lease, access_context="public")
admin_server = create_server("127.0.0.1", 8771, status, dashboard=dashboard,
                             security_store=security, guest_manager=guests,
                             control_lease=lease, access_context="local_admin")
```

Assign `dashboard`, `security_store`, `guest_manager`, `control_lease`, and `access_context` as attributes on each server object so integration tests and shutdown logic can inspect the shared services. Open the desktop WebView at `http://127.0.0.1:8771/`. Start both server threads before opening the WebView and shut down both in `finally`. A port conflict must name 8770 or 8771 explicitly.

- [ ] **Step 5: Implement local-only security settings endpoints**

`GET` returns `SecurityStore.safe_status`, configured public hostname, guest quotas, and `cloudflared` status. `POST` accepts `password`, optional replacement `api_key`, and `model_name`; it requires local-admin context, calls `configure_owner`, and returns only safe status. Empty `api_key` means preserve the current encrypted key; a separate boolean `clear_api_key=true` clears it.

- [ ] **Step 6: Add dual-server tests**

```python
def test_public_context_cannot_change_security_settings(self):
    status, payload = self.public_request("POST", "/api/admin/security/settings", self.settings)
    self.assertEqual(status, 403)
    self.assertEqual(payload["error"], "local_admin_required")

def test_admin_and_public_servers_share_dashboard_but_not_access_context(self):
    self.assertIs(self.public_server.dashboard, self.admin_server.dashboard)
    self.assertEqual(self.public_server.access_context, "public")
    self.assertEqual(self.admin_server.access_context, "local_admin")
```

- [ ] **Step 7: Run runtime and dual-server tests**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\modular_runtime\app\core;F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest modular_runtime.tests.test_public_web modular_runtime.tests.test_lan_web visualization_app.test_public_web_security -v
```

Expected: public/local context, fixed ports, config, auth, and existing LAN tests pass.

- [ ] **Step 8: Commit the dual-server runtime**

```powershell
git add modular_runtime/app/core/public_web.py modular_runtime/tests/test_public_web.py modular_runtime/app/bootstrap.py modular_runtime/config/runtime.json modular_runtime/config/runtime.delivery.json visualization_app/app.py
git commit -m "feat: separate public and local admin web surfaces"
```

---

### Task 9: Cloudflare Deployment Configuration and In-Place Packaging

**Files:**
- Modify: `modular_runtime/build_modular_app.ps1:135-159,200-215`
- Modify: `modular_runtime/README.md`
- Modify: `visualization_app/test_public_web_security.py`

**Interfaces:**
- Consumes: `PublicWebConfig.hostname`, `inspect_cloudflared_service`, and the existing in-place build parameters.
- Produces: delivery tree containing all new runtime modules without Cloudflare tokens, password stores, session databases, or API keys.

- [ ] **Step 1: Write a failing build-manifest test**

```python
def test_build_script_copies_every_public_web_module(self):
    script = (ROOT / "modular_runtime" / "build_modular_app.ps1").read_text(encoding="utf-8-sig")
    for name in ("web_auth.py", "web_access.py", "public_status.py", "guest_simulation.py", "control_lease.py"):
        self.assertIn(f'"{name}"', script)
```

- [ ] **Step 2: Run the build-manifest test and verify failure**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.BuildManifestTests -v
```

Expected: `FAIL` because the new modules are not yet listed.

- [ ] **Step 3: Update the existing in-place build script**

Add the five modules to `$legacyFiles`. After copying, verify each exact file exists under `app/legacy`. Do not copy `runtime/public_web_security.sqlite3`, Cloudflare credentials, logs, screenshots, or generated guest data. Keep `-AllowExistingTarget` behavior unchanged.

- [ ] **Step 4: Add exact Cloudflare deployment and recovery instructions to the existing README**

Document:

1. Add the user's domain to Cloudflare.
2. Create a named Tunnel and route the selected hostname to `http://127.0.0.1:8770`.
3. Add the required catch-all `http_status:404` route.
4. Install the dashboard-provided `cloudflared` service command locally; never paste its token into Git, this plan, screenshots, or chat.
5. Keep router port forwarding disabled and do not open WAN port 8770.
6. Set `/api/*` and HTML to no-cache and create rate-limit rules for `/api/auth/login`, `/api/agent/diagnose`, simulation start, and downloads; the README must note that Cloudflare rule availability depends on the account plan.
7. Set the same hostname in the local EXE settings.
8. Use the public HTTPS URL from both LAN and external networks when entering the password; direct LAN HTTP remains guest-only.
9. Revoke the previously exposed SiliconFlow key, enter a newly created key locally, and verify the key is absent from responses/logs.
10. If Tunnel fails, continue using the local desktop or LAN guest URL and inspect the local Tunnel status panel.

- [ ] **Step 5: Add a delivery secret scan**

Extend the build verification to reject files named `public_web_security.sqlite3`, `cert.pem`, `*.cfargotunnel.com.json`, and any text match for `sk-[A-Za-z0-9_-]{20,}` in copied configuration, HTML, JavaScript, README-generated runtime summaries, and logs. Do not scan binary model files as text.

- [ ] **Step 6: Run build-manifest, secret-scan, and runtime tests**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\AFP_Integrated_Modular_v2\modular_runtime\app\core;F:\AFP_Integrated_Modular_v2\visualization_app'
& $afpPython -m unittest visualization_app.test_public_web_security.BuildManifestTests modular_runtime.tests.test_public_web -v
```

Expected: module-copy and secret-exclusion tests pass.

- [ ] **Step 7: Commit packaging and deployment guidance**

```powershell
git add modular_runtime/build_modular_app.ps1 modular_runtime/README.md visualization_app/test_public_web_security.py
git commit -m "docs: add secure Cloudflare public deployment workflow"
```

---

### Task 10: Full Regression, External Acceptance, and Same-EXE Delivery

**Files:**
- Verify: all files changed in Tasks 1-9
- Update in place after verification: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/AFP_Integrated_System_Modular.exe`
- Update in place after verification: `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/app/legacy/*`, `app/ui/*`, `config/runtime.json`, and integrity manifest generated by the existing build script

**Interfaces:**
- Consumes: complete public-web feature, user-supplied Cloudflare domain, local password, and newly rotated SiliconFlow key.
- Produces: one verified local/LAN/public deployment with a Git rollback point and one existing EXE.

- [ ] **Step 1: Record the implementation baseline without modifying files**

Run:

```powershell
git status --short
git log -1 --oneline
git tag public-web-implementation-baseline
```

Expected: clean working tree before tagging; baseline tag points to the approved design and implementation plan immediately before production-code changes.

- [ ] **Step 2: Run all focused and existing regression suites**

Run:

```powershell
$afpPython = 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe'
$env:PYTHONPATH = 'F:\program\channel_independent_MTSF-main\.venv\Lib\site-packages;F:\AFP_Integrated_Modular_v2\visualization_app;F:\AFP_Integrated_Modular_v2\modular_runtime\app\core'
& $afpPython -m unittest `
  visualization_app.test_public_web_security `
  visualization_app.test_guest_web `
  modular_runtime.tests.test_public_web `
  modular_runtime.tests.test_lan_web `
  modular_runtime.tests.test_modular_runtime `
  visualization_app.test_acquisition_integrity `
  visualization_app.test_interface_agent `
  visualization_app.test_agentic_diagnosis `
  visualization_app.test_app `
  visualization_app.test_m3232_pressure `
  visualization_app.test_mysql_identity `
  visualization_app.test_mysql_visibility `
  visualization_app.test_native_integrated_app `
  visualization_app.test_new_collection_health `
  visualization_app.test_online_inference_compat `
  visualization_app.test_remote_mysql_setup -v
```

Expected: all tests pass with exit code 0.

- [ ] **Step 3: Run source-tree secret and generated-artifact checks**

Run:

```powershell
rg -n --hidden --glob '!delivery/**' --glob '!runtime/**' --glob '!*.pth' --glob '!*.joblib' 'sk-[A-Za-z0-9_-]{20,}' .
git status --short
git diff --check
```

Expected: no API-key match; only intentional source changes are tracked; `git diff --check` has no output.

- [ ] **Step 4: Verify local-admin and public servers before Cloudflare**

Launch the modular app and verify:

- Desktop WebView opens `127.0.0.1:8771` in local-admin mode.
- `http://127.0.0.1:8770` opens guest mode.
- Guest simulation runs and saves beneath `public_simulation`.
- Guest public hardware status contains five interface roles and no raw endpoint identifiers.
- Guest direct POST to real acquisition returns 403.
- Local settings save the new password, model name, and newly rotated key without displaying it afterward.
- HTTPS-login behavior is tested through a local trusted-proxy test harness before the live Tunnel exists.

- [ ] **Step 5: Configure the named Cloudflare Tunnel with the user-provided domain**

Perform the account-side configuration only after the user supplies the active Cloudflare domain and authorizes the account changes. Route exactly one hostname to `http://127.0.0.1:8770`; do not expose port 8771 or any MySQL port. Record the public hostname in the local-admin settings without recording the Tunnel token.

- [ ] **Step 6: Run external-network acceptance**

From a phone on mobile data, not the same Wi-Fi:

- Open the HTTPS hostname without login and confirm guest simulation works.
- Confirm sanitized real-interface status loads and no real sample values appear.
- Confirm protected buttons request a password.
- Enter the password once, close and reopen the browser, and confirm the session remains authorized.
- Acquire real control, test the five interfaces, release control, and confirm another browser cannot control concurrently.
- Trigger one SiliconFlow diagnosis and verify `model_used=true` with no API Key visible in browser developer responses or logs.
- Disconnect Tunnel networking and confirm local acquisition/save continues; reconnect and confirm the webpage resynchronizes.
- Revoke the phone session from the local EXE and confirm its next protected request returns 403.

- [ ] **Step 7: Rebuild the existing delivery in place**

Stop the running EXE, verify the resolved target is exactly the existing delivery directory, then run:

```powershell
.\modular_runtime\build_modular_app.ps1 `
  -PythonExecutable 'C:\Users\xlq\AppData\Local\Programs\Python\Python311\python.exe' `
  -TargetDir 'F:\AFP_Integrated_Modular_v2\delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic' `
  -ApplicationVersion '2.0.5-public' `
  -AllowExistingTarget `
  -ExistingExecutable 'F:\AFP_Integrated_Modular_v2\delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic\AFP_Integrated_System_Modular.exe'
```

Expected: the existing directory and same EXE are updated; no sibling version directory or second EXE is created.

- [ ] **Step 8: Run packaged verification and repeat the key acceptance paths**

Run the delivery's file verification, self-test, module status, integration smoke, and functional smoke commands supported by the current launcher. Then launch the delivered EXE and repeat local-admin, LAN guest, public guest, authorization persistence, session revocation, model call, and Tunnel-loss checks against the packaged files.

- [ ] **Step 9: Commit the verified implementation and mark the rollback point**

```powershell
git status --short
git add modular_runtime visualization_app docs/superpowers/plans/2026-09-14-public-web-auth-implementation.md
git commit -m "feat: deliver authenticated public AFP web access"
git tag public-web-v2.0.5-verified
git status --short
```

Expected: final working tree is clean; the verified tag points to the implementation commit. Generated delivery files remain excluded by `.gitignore`; source, tests, configuration templates, and documentation remain fully Git-managed.

## Plan Self-Review Result

- Spec coverage: all 15 design sections map to Tasks 1-10, including anonymous simulation, sanitized hardware state, permanent revocable sessions, server-only API key use, single-owner control, local admin, Cloudflare Tunnel, quotas, tests, Git, and same-EXE delivery.
- Placeholder scan: the plan contains no unfinished requirement or unspecified implementation action; the public hostname is explicitly an external value supplied by the user at deployment.
- Type consistency: `SecurityStore`, `RequestIdentity`, `GuestSimulationManager`, `RealControlLease`, `PublicWebConfig`, and their method names are defined once in their producing tasks and consumed with the same names in later tasks.
- Scope: the feature is large but sequentially cohesive because every task builds the same public/authorized access boundary around the existing acquisition server; no unrelated model or sensor refactor is included.
