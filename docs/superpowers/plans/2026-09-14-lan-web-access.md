# AFP 局域网网页版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with checkpoints.

**Goal:** 让现有模块化 EXE 在固定 `8770` 端口同时提供原桌面窗口和可信局域网浏览器访问。

**Architecture:** 采集电脑继续运行唯一的 `DashboardData` 和全部硬件驱动；`ThreadingHTTPServer` 绑定配置指定的地址，桌面窗口使用 `127.0.0.1:8770`，远程浏览器使用采集电脑的局域网 IPv4。前端仍使用现有静态资源和 HTTP 轮询，不引入第二套网页、WebSocket 或云服务。

**Tech Stack:** Python 3、`http.server.ThreadingHTTPServer`、现有 PyWebView、原生 HTML/CSS/JavaScript、`unittest`/现有测试脚本、PowerShell PyInstaller 打包脚本。

**Spec:** `docs/superpowers/specs/2026-09-14-lan-web-access-design.md`

## Global Constraints

- 只维护当前 Git 仓库、现有交付目录和同名 EXE，不复制工程、不创建第二套前端。
- 正式服务固定使用配置端口 `8770`；端口占用时必须明确失败，不得静默改用随机端口。
- 传感器只由采集电脑访问；远程浏览器不扫描 USB、串口、网卡或摄像头。
- API Key 不得进入 HTML、bootstrap、网络状态接口、日志或 Git 文件。
- 第一步先测试失败，再写最小实现；每个任务完成后运行对应测试并提交小步 Git 提交。
- 只有源代码、测试和 EXE 验证完成后，才覆盖现有交付目录中的同名 EXE。

---

### Task 1: 网络配置与可访问地址计算

**Files:**
- Modify: `modular_runtime/config/runtime.json`
- Modify: `modular_runtime/config/runtime.delivery.json`
- Create: `modular_runtime/app/core/lan_web.py`
- Test: `modular_runtime/tests/test_lan_web.py`

**Interfaces:**
- Produces `LanWebConfig.from_mapping(mapping) -> LanWebConfig` with fields `enabled: bool`, `bind_host: str`, `port: int`, `open_desktop_window: bool`.
- Produces `discover_lan_urls(port: int, bind_host: str = "0.0.0.0") -> list[str]`, returning only `http://<IPv4>:<port>/` URLs for non-loopback, non-APIPA addresses; when no candidate exists it returns `[]`.
- Produces `safe_network_status(config, urls, started_at, error=None) -> dict` with keys `enabled`, `bind_host`, `port`, `urls`, `desktop_url`, `error`; it contains no secret or sensor payload.

- [ ] **Step 1: Write failing tests for configuration and address filtering.**

```python
class LanWebConfigTests(unittest.TestCase):
    def test_defaults_use_fixed_lan_port(self):
        config = LanWebConfig.from_mapping({})
        self.assertTrue(config.enabled)
        self.assertEqual(config.bind_host, "0.0.0.0")
        self.assertEqual(config.port, 8770)
        self.assertTrue(config.open_desktop_window)

    def test_rejects_invalid_port(self):
        with self.assertRaises(ValueError):
            LanWebConfig.from_mapping({"port": 80})

    def test_filters_loopback_and_apipa(self):
        with patch("lan_web._iter_ipv4_addresses", return_value=[
            "127.0.0.1", "169.254.20.3", "192.168.1.20", "10.0.0.8"
        ]):
            self.assertEqual(
                discover_lan_urls(8770),
                ["http://10.0.0.8:8770/", "http://192.168.1.20:8770/"],
            )
```

- [ ] **Step 2: Run the focused test and verify it fails because the module is absent.**

Run: `python -m unittest modular_runtime.tests.test_lan_web -v`
Expected: FAIL with an import error for `lan_web`.

- [ ] **Step 3: Implement the configuration contract.**

```python
@dataclass(frozen=True)
class LanWebConfig:
    enabled: bool = True
    bind_host: str = "0.0.0.0"
    port: int = 8770
    open_desktop_window: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "LanWebConfig":
        raw = dict(value or {})
        port = int(raw.get("port", 8770))
        if not 1024 <= port <= 65535:
            raise ValueError("局域网端口必须在 1024-65535 之间")
        bind_host = str(raw.get("bind_host", "0.0.0.0")).strip() or "127.0.0.1"
        if bind_host not in {"0.0.0.0", "127.0.0.1", "localhost"}:
            raise ValueError("局域网绑定地址只允许 0.0.0.0 或本机地址")
        return cls(bool(raw.get("enabled", True)), bind_host, port,
                   bool(raw.get("open_desktop_window", True)))
```

  `_iter_ipv4_addresses` uses `socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)` and returns a sorted unique list. `discover_lan_urls` removes `127.*`, `169.254.*`, and invalid addresses, then sorts private addresses before other non-loopback addresses.

- [ ] **Step 4: Add the same `lan_web` section to both development and delivery runtime JSON.**

```json
"lan_web": {
  "enabled": true,
  "bind_host": "0.0.0.0",
  "port": 8770,
  "open_desktop_window": true
}
```

- [ ] **Step 5: Run the focused tests and commit.**

Run: `python -m unittest modular_runtime.tests.test_lan_web -v`
Expected: all configuration and address tests pass.

```text
git add modular_runtime/config/runtime.json modular_runtime/config/runtime.delivery.json modular_runtime/app/core/lan_web.py modular_runtime/tests/test_lan_web.py
git commit -m "feat: add LAN web configuration"
```

### Task 2: Server status, request protection, and operation mutex

**Files:**
- Modify: `visualization_app/app.py:3879-4278`
- Modify: `visualization_app/test_app.py`
- Modify: `visualization_app/test_interface_agent.py` only if the handler fixture imports shared state

**Interfaces:**
- `create_server(host="127.0.0.1", port=8765, network_status=None) -> ThreadingHTTPServer` stores a sanitized `network_status` dictionary on the configured handler.
- `GET /api/network/status` returns the sanitized network status and `service_uptime_seconds`.
- `AppHandler._is_allowed_origin() -> bool` rejects a mutation request whose `Origin` is neither absent nor an HTTP origin for the current Host.
- `AppHandler._operation(name)` is a context manager; a second operation with the same name returns HTTP `409` and JSON `{"error":"operation_in_progress","operation":name}`.

- [ ] **Step 1: Add failing HTTP tests for network status, origin rejection, and duplicate operation rejection.**

```python
def test_network_status_never_contains_secrets(self):
    server = create_server("127.0.0.1", 0, {
        "enabled": True, "bind_host": "0.0.0.0", "port": 8770,
        "urls": ["http://192.168.1.20:8770/"], "api_key": "must-not-leak"
    })
    response = self.get(server, "/api/network/status")
    self.assertNotIn("api_key", json.dumps(response))
    self.assertEqual(response["urls"], ["http://192.168.1.20:8770/"])

def test_cross_origin_mutation_is_rejected(self):
    response = self.post(server, "/api/acquisition/reset-check", {},
                         headers={"Origin": "http://attacker.example"})
    self.assertEqual(response.status, 403)

def test_same_operation_returns_409_while_in_progress(self):
    with operation_lock("acquisition-check"):
        response = self.post(server, "/api/acquisition/reset-check", {})
    self.assertEqual(response.status, 409)
```

- [ ] **Step 2: Run the focused tests and verify the new endpoints/guards fail.**

Run: `python -m unittest visualization_app.test_app -v`
Expected: failures for missing `/api/network/status`, missing origin guard, and missing `409` response.

- [ ] **Step 3: Implement sanitized network status and request guards.**

```python
def _send_network_status(self):
    payload = dict(self.network_status or {})
    payload.pop("api_key", None)
    payload.pop("model_name", None)
    payload["service_uptime_seconds"] = round(max(0.0, time.time() - self.service_started_at), 1)
    self._send_json(payload)
```

  Validate the `Host` header against `localhost`, `127.0.0.1`, the server bind address, and discovered LAN URL hosts. For `POST`, require `Content-Type` beginning with `application/json`; if an `Origin` is present, compare its scheme/host/port to the request Host. Return `403` for origin/host failures before parsing or executing the payload.

- [ ] **Step 4: Wrap state-changing routes with one shared mutex.**

  Use a module-level `threading.Lock` keyed by operation name. Apply `acquisition-check` to `/api/acquisition/test`, `/api/acquisition/reset-check`, and `/api/agent/diagnose`; apply `acquisition-control` to `/api/acquisition/start` and `/api/acquisition/stop`; apply `training-control` to training start/stop. Acquire non-blocking, return `409` on contention, and release in `finally`.

- [ ] **Step 5: Run the focused tests and commit.**

Run: `python -m unittest visualization_app.test_app -v`
Expected: existing app tests plus new network/guard/mutex tests pass.

```text
git add visualization_app/app.py visualization_app/test_app.py visualization_app/test_interface_agent.py
git commit -m "feat: expose safe LAN status and operation locking"
```

### Task 3: Modular launcher binds the configured fixed port

**Files:**
- Modify: `modular_runtime/app/bootstrap.py:401-426`
- Modify: `modular_runtime/tests/test_modular_runtime.py`

**Interfaces:**
- `bootstrap.launch(context, manager)` reads `LanWebConfig.from_mapping(context.config.get("lan_web"))`.
- It calls `legacy_app.create_server(config.bind_host, config.port, network_status)` and opens the desktop window at `http://127.0.0.1:<port>/`.
- It records a sanitized `runtime/lan_web_status.json` for packaged diagnostics.

- [ ] **Step 1: Add failing launcher tests using a fake webview and fake legacy server.**

```python
def test_launch_uses_configured_fixed_port_and_lan_bind(self):
    context = make_context({"lan_web": {"enabled": True, "bind_host": "0.0.0.0", "port": 8770}})
    fake_server = FakeServer(port=8770)
    with patch("bootstrap._legacy_module"), patch("bootstrap.webview") as webview:
        legacy.create_server.return_value = fake_server
        webview.start.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            bootstrap.launch(context, FakeManager())
    legacy.create_server.assert_called_once()
    self.assertEqual(legacy.create_server.call_args.args[:2], ("0.0.0.0", 8770))
    self.assertTrue(webview.create_window.call_args.args[1].endswith("127.0.0.1:8770/"))
```

- [ ] **Step 2: Run the launcher test and verify it fails because launch still uses `127.0.0.1`, port `0`.**

Run: `python -m unittest modular_runtime.tests.test_modular_runtime -v`
Expected: the new launch assertion fails with the current hard-coded call.

- [ ] **Step 3: Implement configuration-driven launch and explicit startup errors.**

  Construct the sanitized network status from `discover_lan_urls`. If `enabled` is false, pass `127.0.0.1` as the bind host. If `ThreadingHTTPServer` raises `OSError` for the configured port, persist a Chinese error explaining that TCP 8770 is occupied, then raise a runtime error instead of retrying with port `0`. Start the server before creating the PyWebView window and shut it down in the existing `finally` block.

- [ ] **Step 4: Run modular tests and commit.**

Run: `python -m unittest modular_runtime.tests.test_modular_runtime -v`
Expected: all existing modular tests plus fixed-port launcher tests pass.

```text
git add modular_runtime/app/bootstrap.py modular_runtime/tests/test_modular_runtime.py
git commit -m "feat: launch modular app on fixed LAN port"
```

### Task 4: Existing UI displays LAN address and reconnect state

**Files:**
- Modify: `visualization_app/static/index.html`
- Modify: `visualization_app/static/app.js`
- Modify: `visualization_app/static/styles.css`
- Modify: `visualization_app/test_interface_agent.py`

**Interfaces:**
- Adds a compact status element with id `lan-web-status` and child elements `lan-web-mode`, `lan-web-url`, `lan-web-copy`.
- Adds `refreshLanWebStatus()` that fetches `/api/network/status` and updates the status without exposing secrets.
- Existing polling catches `fetch` errors, sets a reconnect label, and retries on the current interval; it must not clear the last hardware or LangChain result.

- [ ] **Step 1: Add failing static contract tests.**

```python
def test_lan_status_controls_are_present(self):
    html = INDEX.read_text(encoding="utf-8")
    self.assertIn('id="lan-web-status"', html)
    self.assertIn('id="lan-web-url"', html)
    self.assertIn('id="lan-web-copy"', html)
    self.assertIn("/api/network/status", APP_JS.read_text(encoding="utf-8"))
```

- [ ] **Step 2: Run the frontend contract test and verify it fails.**

Run: `python -m unittest visualization_app.test_interface_agent.InterfaceAgentTests.test_existing_ui_exposes_lan_access_status -v`
Expected: missing LAN status marker failure.

- [ ] **Step 3: Add the status markup and minimal styles.**

  Place the element in the existing top status/header region. Use current color tokens and compact inline layout; do not alter the existing collapsible module geometry. The copy button copies the first URL with `navigator.clipboard.writeText` and falls back to selecting a hidden text input when clipboard permission is unavailable.

- [ ] **Step 4: Add status refresh and reconnect behavior.**

```javascript
async function refreshLanWebStatus() {
  const response = await fetch('/api/network/status', { cache: 'no-store' });
  const status = await response.json();
  lanMode.textContent = status.error ? '启动失败' : (status.enabled ? '局域网已开启' : '仅本机');
  lanUrl.textContent = (status.urls || [])[0] || '请查看采集电脑网络设置';
}

function markServerDisconnected() {
  lanMode.textContent = '服务器连接中断，采集仍在服务器运行';
}
```

  Call `refreshLanWebStatus()` during initial page setup and after reconnect. Existing data render functions retain their last payload when a poll fails.

- [ ] **Step 5: Run frontend tests and commit.**

Run: `python -m unittest visualization_app.test_interface_agent.InterfaceAgentTests.test_existing_ui_exposes_lan_access_status -v`
Expected: all frontend contract tests pass.

```text
git add visualization_app/static/index.html visualization_app/static/app.js visualization_app/static/styles.css interface_monitor_demo/tests/test_frontend_contract.py
git commit -m "feat: show LAN access status in existing UI"
```

### Task 5: End-to-end verification and same-delivery EXE build

**Files:**
- Modify: `modular_runtime/build_modular_app.ps1` only if the existing build needs a non-destructive in-place target option
- Modify: existing delivery files under `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/`
- Test: existing modular, app, agent, acquisition, and frontend test files

**Interfaces:**
- The current delivery directory remains the only delivery target.
- The generated EXE is `delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic/AFP_Integrated_System_Modular.exe`.
- `runtime/lan_web_status.json` reports the actual bind host, fixed port, candidate URLs, and any startup error.

- [ ] **Step 1: Run the full source regression suite before packaging.**

Run: `python -m unittest discover -s modular_runtime/tests -p "test_*.py" -v` and `python -m unittest discover -s visualization_app -p "test_*.py" -v`
Expected: exit code `0` and zero failures.

- [ ] **Step 2: Build in a temporary PyInstaller workspace, then update only the existing delivery directory.**

Use the existing `modular_runtime/build_modular_app.ps1` with the current reference release and a staging output under `%TEMP%`. Before replacing the delivery EXE, verify the target directory is exactly `F:\AFP_Integrated_Modular_v2\delivery\AFP_Integrated_System_Modular_v2.0.3_Agentic`; do not pass a new version folder. Copy the verified executable and synchronized external `app`, `config`, `docs`, `models`, and `native_dll` files into that existing directory.

- [ ] **Step 3: Start the EXE and verify both local and LAN HTTP paths.**

Use the existing EXE with no command-line override. Verify:

```text
GET http://127.0.0.1:8770/api/health       -> 200
GET http://127.0.0.1:8770/api/network/status -> 200
GET http://<采集电脑局域网IPv4>:8770/api/health -> 200
GET http://<采集电脑局域网IPv4>:8770/       -> 200, contains the existing page shell
```

Also verify the PyWebView window loads `http://127.0.0.1:8770/`, the page shows the LAN URL, and a failed poll preserves the last displayed diagnostic result.

- [ ] **Step 4: Run the packaged self-test and integrity checks.**

Run the EXE with `--self-test`, `--verify-files`, and the existing functional smoke command. Expected: each command exits `0`; `SHA256SUMS.txt` has no mismatches; API Key text is absent from the package and `runtime/lan_web_status.json`.

- [ ] **Step 5: Capture a verification report and commit only the source/build changes.**

Run: `git status --short`, `git diff --check`, and the full test commands again after packaging. Commit the source/config/test changes and the single updated delivery artifact with:

```text
git add modular_runtime visualization_app interface_monitor_demo delivery/AFP_Integrated_System_Modular_v2.0.3_Agentic
git commit -m "feat: deliver LAN web executable"
```

The final response must report the actual EXE path, actual URL discovered on this machine, test counts, and any firewall action still required; it must not claim LAN access until the second-device or equivalent LAN-IP request returns HTTP 200.
