from pathlib import Path
import subprocess
import unittest


class GuestSimulationFrontendContractTests(unittest.TestCase):
    def test_target_mysql_preflight_identifier_is_reused_for_start_and_server_save_state(self):
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        settings_start = text.index("function unifiedMysqlSettings")
        settings_end = text.index("function unifiedLocalMysqlSettings", settings_start)
        settings = text[settings_start:settings_end]
        self.assertIn("mysql_target_config_id", settings)
        acquire_start = text.index("function acquisitionConfig()")
        acquire_end = text.index("function renderProcessParameterReadStatus", acquire_start)
        acquisition = text[acquire_start:acquire_end]
        self.assertIn("mysql_target_config_id", acquisition)
        render_start = text.index("function renderMysqlStatus(mysql")
        render_end = text.index("function agentEscapeHtml", render_start)
        rendered = text[render_start:render_end]
        self.assertIn("serverTarget", rendered)
        self.assertIn("服务器/目标 MySQL（服务器执行）", rendered)
        self.assertIn("target.error_detail?.message", rendered)

    def test_real_helper_stop_waits_for_server_target_mysql_terminal_state(self):
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        start = text.index("async function waitForServerTargetSave(")
        end = text.index("async function stopAcquisition()", start)
        function = text[start:end]
        script = """
const controls = {mysqlEnabled: {checked: true}, acquisitionMode: {value: "real"}};
const usesLocalCaptureHelper = () => true;
const window = {setTimeout: (fn) => { fn(); return 1; }};
const fetch = async (url, options) => ({ok: true, json: async () => ({
  capture_uuid: "capture-a", server_target: {state: "saved", saved_rows: 3, received_rows: 3}
})});
""" + function + """
(async () => {
  const result = await waitForServerTargetSave({capture_uuid: "capture-a", capture_saved: true});
  if (result.server_target?.state !== "saved" || result.server_target.saved_rows !== 3) process.exit(1);
})().catch((error) => { console.error(error); process.exit(2); });
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_failed_server_target_mysql_has_a_session_bound_retry_control(self):
        html = (Path(__file__).with_name("static") / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="retryTargetMysqlButton"', html)
        self.assertIn('/app.js?v=20260923-mysql-init-1', html)
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        start = text.index("async function retryServerTargetMysql()")
        end = text.index("async function stopAcquisition()", start)
        function = text[start:end]
        script = """
const calls = [];
const state = {acquisitionStatus: {capture_uuid: "capture-a", server_target: {state: "failed"}}};
const controls = {retryTargetMysql: {disabled: false}};
const postJson = async (url, payload) => { calls.push([url, payload]); return {ok: true, server_target: {state: "saving"}}; };
const waitForServerTargetSave = async () => ({server_target: {state: "saved", saved_rows: 3}});
const renderMysqlStatus = () => {};
const toast = () => {};
""" + function + """
(async () => {
  await retryServerTargetMysql();
  if (calls.length !== 1 || calls[0][0] !== "/api/mysql/target/retry" ||
      calls[0][1].capture_uuid !== "capture-a" || state.acquisitionStatus.server_target.state !== "saved") process.exit(1);
})().catch((error) => { console.error(error); process.exit(2); });
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_guest_replay_polls_server_progress_at_most_once_per_second(self):
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        start = text.index("function startLocalSimulationReplay()")
        end = text.index("function stopLocalSimulationReplay()", start)
        function = text[start:end]
        script = """
const state = {simulationDatasetCache: {rows: [{}, {}, {}, {}]}, payload: {},
  guestSimulationStarted: true, simulationLocalReplay: false, simulationStatusLastAt: 0};
const isGuestSimulationMode = () => true;
const stopPlayback = () => {};
const closeLiveWebSocket = () => {};
const renderCachedSimulationSample = () => true;
const playbackInterval = () => 100;
let now = 1000;
Date.now = () => now;
let nextTick;
const window = {clearInterval(){}, clearTimeout(){}, setTimeout(callback) {nextTick = callback; return 1;}};
const $ = () => ({textContent: ""});
const document = {querySelector: () => ({classList: {add(){}, remove(){}}})};
let statusRequests = 0;
const refreshGuestSimulationStatus = () => {statusRequests += 1;};
""" + function + """
startLocalSimulationReplay();
if (statusRequests !== 1) process.exit(1);
now = 1500;
nextTick();
if (statusRequests !== 1) process.exit(2);
now = 2100;
nextTick();
if (statusRequests !== 2) process.exit(3);
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_guest_local_replay_refreshes_actual_server_sample_progress_without_stale_stop_update(self):
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        try:
            start = text.index("async function refreshGuestSimulationStatus()")
        except ValueError:
            self.fail("访客本地回放没有同步服务器实际采样进度")
        end = text.index("function startLocalSimulationReplay()", start)
        function = text[start:end]
        script = """
const state = {simulationLocalReplay: true, guestSimulationStarted: true,
  simulationStatusBusy: false, acquisitionStatus: {capture_uuid: "old"}};
let requests = 0;
let shown = [];
let release;
const fetch = async (url, options) => {
  if (url !== "/api/simulation/status" || options.credentials !== "same-origin") process.exit(1);
  requests += 1;
  const captureUuid = state.acquisitionStatus.capture_uuid;
  await new Promise((resolve) => {release = resolve;});
  return {ok: true, json: async () => ({running: true, capture_uuid: captureUuid, sample_count: 123})};
};
const renderAcquisitionStatus = (payload) => {shown.push(payload.sample_count);};
""" + function + """
(async () => {
  const active = refreshGuestSimulationStatus();
  await Promise.resolve();
  const duplicate = refreshGuestSimulationStatus();
  release();
  await Promise.all([active, duplicate]);
  if (requests !== 1 || shown.join() !== "123" || state.simulationStatusBusy) process.exit(2);
  const stale = refreshGuestSimulationStatus();
  await Promise.resolve();
  state.simulationLocalReplay = false;
  release();
  await stale;
  if (shown.join() !== "123") process.exit(3);
  state.simulationLocalReplay = true;
  const oldRun = refreshGuestSimulationStatus();
  await Promise.resolve();
  state.acquisitionStatus = {capture_uuid: "new"};
  release();
  await oldRun;
  if (shown.join() !== "123") process.exit(5);
})().catch((error) => {console.error(error); process.exit(4);});
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_helper_status_poll_does_not_erase_unsaved_mysql_edits(self):
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        start = text.index("function applyLocalMysqlProfileMetadata(profile")
        end = text.index("async function loadMysqlDefaults", start)
        function = text[start:end]
        script = """
const state = {localMysqlFormDirty: true, localMysqlProfile: null};
const field = (value) => ({value, placeholder: ""});
const controls = {
  mysqlLocalHost: field("192.0.2.44"), mysqlLocalPort: field("3307"),
  mysqlLocalUser: field("new_user"), mysqlLocalDatabase: field("new_db"),
  mysqlLocalPassword: field("new-secret")
};
const $ = () => ({classList: {remove(){}, add(){}}, textContent: ""});
const usesLocalCaptureHelper = () => true;
""" + function + """
applyLocalMysqlProfileMetadata({configured: true, host: "127.0.0.1", port: 3306,
  user: "old_user", database: "old_db"});
if (controls.mysqlLocalHost.value !== "192.0.2.44" ||
    controls.mysqlLocalPassword.value !== "new-secret") process.exit(1);
state.localMysqlFormDirty = false;
applyLocalMysqlProfileMetadata({configured: true, host: "127.0.0.1", port: 3306,
  user: "old_user", database: "old_db"}, {afterSave: true});
if (controls.mysqlLocalHost.value !== "127.0.0.1" ||
    controls.mysqlLocalPassword.value !== "") process.exit(2);
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_simulation_cards_keep_distinct_roles_without_shared_physical_binding(self):
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        start = text.index("function refreshPhysicalInterfaceOptions")
        end = text.index("function itemEnabledForSimulation", start)
        function = text[start:end]
        script = """
const controls = {acquisitionMode: {value: "simulation"}};
const state = {physicalInterfaces: []};
const profiles = {plc: {label: "PLC", physical_kind: "ethernet"},
  robot: {label: "ABB", physical_kind: "ethernet"}};
const sensorTypeProfile = (role) => profiles[role];
const physicalCandidatesForRole = () => [];
const option = (value, label) => ({value, textContent: label, dataset: {}});
const document = {createElement: () => ({classList: {remove(){}, toggle(){}}, textContent: ""})};
""" + function + """
function rowFor(role) {
  const select = {value: "stale-binding", disabled: false, replaceChildren(...items) {
    this.items = items;
  }};
  const enabled = {checked: false, disabled: true};
  const row = {querySelector(name) {
    return name === ".interface-physical" ? select
      : name === ".interface-role" ? {value: role}
      : name === ".interface-enabled" ? enabled : null;
  }, append(node) {this.warning = node;}};
  return {row, select, enabled};
}
const plc = rowFor("plc"), abb = rowFor("robot");
refreshPhysicalInterfaceOptions(plc.row);
refreshPhysicalInterfaceOptions(abb.row);
if (plc.select.value || abb.select.value || !plc.select.disabled || !abb.select.disabled) process.exit(1);
if (plc.select.items[0].textContent === abb.select.items[0].textContent) process.exit(2);
if (plc.enabled.checked || abb.enabled.checked) process.exit(3);
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_simulation_connection_result_reports_each_original_protocol(self):
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        start = text.rindex("async function testSensorConnection(")
        end = text.index("async function ", start + 1)
        body = text[start:end]
        self.assertIn('driver: row.querySelector(".interface-driver")?.value', body)
        self.assertNotIn('driver: "simulator",', body)

    def test_changing_simulation_source_keeps_five_interface_card_choices(self):
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        start = text.index("function updateSimulationSettings()")
        end = text.index("function updateIntegrationSource()", start)
        function = text[start:end]
        script = """
const state = {accessRole: "authorized", interfaceModeRendered: "real",
  interfaceCatalog: [{id: "plc", role: "plc"}]};
let current = [{id: "plc", role: "plc", driver: "modbus_tcp", enabled: true,
  physical_interface_id: "ethernet:plant"}];
let savedReal = null;
let renders = 0;
const controls = {acquisitionMode: {value: "real"},
  simulationSourceType: {value: "single_csv"},
  simulationSettings: {classList: {toggle(){}}},
  simulationMysqlSettings: {classList: {toggle(){}}},
  simulationSourcePathLabel: {classList: {toggle(){}}}};
const document = {querySelector() {return null;}, querySelectorAll() {return [];}};
const rememberRealInterfaceSnapshot = () => {savedReal = current.map((item) => ({...item}));};
const buildSimulationInterfaceCatalog = () => [{id: "plc", role: "plc", driver: "modbus_tcp", enabled: true}];
const interfaceConfigs = () => ({interfaces: current.map((item) => ({...item}))});
const renderInterfacePanel = (items) => {renders += 1; current = items.map((item) => ({...item}));};
const restoreCachedRealInterfaceSnapshot = () => {current = savedReal.map((item) => ({...item})); return true;};
const loadHelperStatus = async () => {};
const discoverInterfaces = async () => {};
const updateRealAcquisitionVisibility = () => {};
""" + function + """
controls.acquisitionMode.value = "simulation";
updateSimulationSettings();
current[0].enabled = false;
controls.simulationSourceType.value = "folder_csv";
updateSimulationSettings();
if (current[0].enabled !== false || current[0].driver !== "modbus_tcp") process.exit(1);
controls.acquisitionMode.value = "real";
updateSimulationSettings();
if (current[0].physical_interface_id !== "ethernet:plant") process.exit(3);
controls.acquisitionMode.value = "simulation";
updateSimulationSettings();
if (current[0].enabled !== false || current[0].driver !== "modbus_tcp") process.exit(2);
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_stop_and_save_has_visible_progress_and_does_not_repeat_request(self):
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        start = text.index("async function stopAcquisition()")
        end = text.index("function buildSensorChecklist", start)
        function = text[start:end]
        script = """
const state = {stopBusy: false, accessRole: "authorized"};
const status = {textContent: "", classList: {add(){}, remove(){}}};
const button = {disabled: false, textContent: "停止并保存"};
const $ = (name) => name === "stopAcquisitionButton" ? button : status;
const controls = {datasetSchema: {value: "legacy_original"}, liveLayer: {value: "0"},
  newLayer: {value: "0"}, acquisitionMode: {value: "simulation"}};
const stopLocalSimulationReplay = () => {};
const usesLocalCaptureHelper = () => false;
let count = 0;
const postJson = async (url) => {
  count += 1;
  if (url !== "/api/acquisition/stop") throw Error("wrong endpoint");
  await new Promise((resolve) => setTimeout(resolve, 10));
  return {running: false, capture_saved: true, mysql: {}, completed_layers: []};
};
const requestLocalHelper = async () => {throw Error("unexpected helper");};
const renderAcquisitionStatus = () => {};
const renderMysqlStatus = () => {};
const saveFinishedCaptureLocally = async (mode) => {if (mode !== "simulation") throw Error("wrong mode");};
const loadRealtime = async () => {};
const toast = () => {};
const window = {setTimeout};
""" + function + """
(async () => {
  const first = stopAcquisition();
  if (!button.disabled || !status.textContent.includes("正在停止")) process.exit(1);
  const second = stopAcquisition();
  await Promise.all([first, second]);
  if (count !== 1 || button.disabled || state.stopBusy) process.exit(2);
})().catch(() => process.exit(3));
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_render_acquisition_status_records_running_state(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function renderAcquisitionStatus(status)")
        end = text.index("function renderRuntimeStatus", start)
        body = text[start:end]
        self.assertIn(
            "state.acquisitionStatus = status;",
            body,
            "the guest auto-check guard needs the latest acquisition status",
        )

    def test_guest_simulation_does_not_auto_start_on_refresh(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn(
            "guestSimulationStoppedByUser",
            text,
            "an explicit stop must prevent loadRealtime from starting a new guest run",
        )
        start = text.index("async function loadRealtime()")
        end = text.index("function scheduleLoad", start)
        self.assertNotIn('postJson("/api/simulation/start"', text[start:end])

    def test_initialize_refreshes_simulation_controls_after_guest_mode_is_applied(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function initialize()")
        end = text.index('$("playButton")', start)
        body = text[start:end]
        self.assertIn(
            "updateSimulationSettings();",
            body,
            "guest access changes the acquisition mode after the initial control setup",
        )

    def test_guest_upload_waits_for_explicit_start_without_download_ui(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function uploadSimulationSource()")
        end = text.index("function acquisitionConfig()", start)
        body = text[start:end]
        self.assertNotIn('"/api/simulation/start"', body)
        self.assertIn("请点击“开始采集”后才开始读取", body)
        self.assertIn("确认并授权本地保存", text)
        index = source.with_name("index.html").read_text(encoding="utf-8")
        self.assertNotIn("downloadSimulationButton", index)
        self.assertNotIn("downloadSimulationSource", text)

    def test_every_remote_role_uses_browser_upload_for_simulation_sources(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function selectSimulationSource()")
        end = text.index("function readSimulationFile", start)
        body = text[start:end]
        self.assertIn('state.accessRole !== "local_admin"', body)
        self.assertNotIn('state.accessRole === "guest"', body)
        self.assertIn('"/api/acquisition/upload-source"', text)
        self.assertIn("simulationSourceId", text)

    def test_pairing_feedback_is_rendered_in_helper_panel(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function pairLocalHelper()")
        end = text.index("function showRealAccessModal", start)
        body = text[start:end]
        self.assertIn("正在生成配对码", body)
        self.assertIn("配对码生成失败：", body)
        self.assertIn("button.disabled = false", body)

    def test_guest_pairing_entry_explains_unlock_requirement_instead_of_hiding(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function renderHelperStatus()")
        end = text.index("async function loadHelperStatus", start)
        body = text[start:end]
        self.assertIn("解锁后生成配对码", body)
        self.assertIn("button?.classList.toggle(\"hidden\", false)", body)
        self.assertIn("HTTPS 公网地址", body)

    def test_pairing_refreshes_session_before_calling_protected_route(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function pairLocalHelper()")
        end = text.index("function showRealAccessModal", start)
        body = text[start:end]
        self.assertIn("await loadAccessSession()", body)
        self.assertIn("showRealAccessModal()", body)
        self.assertIn('error?.code !== "csrf_failed"', body)

    def test_api_errors_keep_machine_code_and_show_actionable_helper_text(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function postJson")
        end = text.index("async function requestLocalHelper", start)
        body = text[start:end]
        self.assertIn("error.code = code", body)
        self.assertIn("authorized_session_required", body)
        self.assertIn("网页会话已刷新，请重试当前操作", body)

    def test_local_mysql_helper_receives_unified_mysql_setting_keys(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function testMysqlConnection")
        end = text.index("async function refreshRelationMap", start)
        body = text[start:end]
        self.assertIn('requestLocalHelper("mysql_profile_save"', body)
        self.assertIn('use_saved_profile: true', body)
        self.assertIn('initialize_if_missing: true', body)
        self.assertIn('write_test: true', body)
        self.assertNotIn('settings.mysql_local_host', body)
        self.assertNotIn('settings.mysql_local_port', body)

    def test_start_preflights_every_enabled_mysql_destination(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        validate_start = text.index("async function validateEnabledMysqlBeforeStart")
        validate_end = text.index("async function refreshRelationMap", validate_start)
        validate_body = text[validate_start:validate_end]
        self.assertIn("controls.mysqlEnabled?.checked", validate_body)
        self.assertIn("controls.mysqlLocalEnabled?.checked", validate_body)
        self.assertIn("await testMysqlConnection(false)", validate_body)
        self.assertIn("await testMysqlConnection(true)", validate_body)

        start = text.index("async function startAcquisition()")
        end = text.index("async function stopAcquisition()", start)
        start_body = text[start:end]
        self.assertIn("await validateEnabledMysqlBeforeStart();", start_body)

    def test_authorized_simulation_never_waits_for_physical_hardware_check(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")

        start = text.index("async function startAcquisition()")
        end = text.index("async function stopAcquisition()", start)
        start_body = text[start:end]
        self.assertIn(
            'const simulation = controls.acquisitionMode?.value === "simulation";',
            start_body,
        )
        real_gate = start_body.index("if (!simulation) {")
        next_scope = start_body.index("const nextScope", real_gate)
        self.assertIn("state.hardwareCheckInProgress", start_body[real_gate:next_scope])
        self.assertNotIn("state.hardwareCheckInProgress", start_body[:real_gate])
        self.assertIn("await acquireRealControl();", start_body[real_gate:next_scope])

        check_start = text.index("async function testSensorConnection({automatic = false} = {})", 5000)
        check_body = text[check_start:]
        self.assertIn(
            'if (controls.acquisitionMode?.value === "simulation")',
            check_body,
        )
        self.assertNotIn(
            'state.accessRole === "guest" && controls.acquisitionMode?.value === "simulation"',
            check_body,
        )

    def test_public_authorized_page_does_not_replace_visitor_mysql_defaults(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function loadMysqlDefaults")
        end = text.index("const LAYER_EVIDENCE_VISIBILITY_KEY", start)
        body = text[start:end]
        self.assertIn('state.accessRole === "local_admin"', body)
        self.assertIn("assign(controls.mysqlLocalHost, local.host)", body)

    def test_unlock_shows_progress_and_refreshes_defaults_in_background(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function unlockRealMode()")
        end = text.index("async function lockRealMode()", start)
        body = text[start:end]
        self.assertIn('submit.disabled = true', body)
        self.assertIn('submit.textContent = "正在解锁…"', body)
        self.assertIn('note.textContent = "正在验证授权密码，请稍候……"', body)
        self.assertIn("void loadAccessSession()", body)
        self.assertIn("state.mysqlConnectionTests.target = null;", body)
        self.assertIn("void loadMysqlDefaults()", body)

    def test_initialize_starts_independent_bootstrap_requests_in_parallel(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function initialize()")
        end = text.index('$("playButton")', start)
        body = text[start:end]
        self.assertIn("const bootstrapPromise = fetch(\"/api/bootstrap\"", body)
        self.assertIn("await Promise.all([", body)
        self.assertIn("loadHelperStatus({deferDiscovery: true})", body)
        self.assertIn("loadAgentDefaults()", body)
        self.assertIn("loadMysqlDefaults()", body)

    def test_live_requests_identify_simulation_or_real_acquisition_source(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function queryString()")
        end = text.index("async function loadRealtime()", start)
        body = text[start:end]
        self.assertIn("acquisition_mode: controls.acquisitionMode.value", body)

        wait_start = text.index("async function waitForEdgeFirstSample")
        wait_end = text.index("function renderRuntimeStatus", wait_start)
        wait_body = text[wait_start:wait_end]
        self.assertIn('/api/acquisition/status?acquisition_mode=real', wait_body)

    def test_only_loopback_admin_skips_helper_pairing(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function renderHelperStatus")
        end = text.index("async function loadHelperStatus", start)
        body = text[start:end]
        self.assertIn('state.accessRole === "local_admin"', body)
        self.assertIn("服务器本机采集，无需辅助程序", body)

    def test_lan_and_public_real_modes_share_helper_routing_policy(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function usesLocalCaptureHelper")
        end = text.index("function renderHelperStatus", start)
        policy = text[start:end]
        self.assertIn('state.accessRole === "lan_operator"', policy)
        self.assertIn('state.accessRole === "authorized"', policy)

        discover_start = text.index("async function discoverInterfaces()")
        discover_end = text.index("function recognizedInterfacePortsFrom", discover_start)
        discover = text[discover_start:discover_end]
        self.assertIn("usesLocalCaptureHelper()", discover)
        self.assertNotIn('state.accessRole === "authorized"', discover)

    def test_runtime_status_identifies_simulation_streams(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function renderRuntimeStatus(payload = state.payload)")
        end = text.index("function renderRemoteMysqlPreview", start)
        body = text[start:end]
        self.assertIn('const simulation = acquisition.config?.acquisition_mode === "simulation";', body)
        self.assertIn('simulation ? "模拟采集" : "真实采集"', body)

    def test_simulation_source_is_bootstrapped_for_authorized_and_guest_flows(self):
        source = Path(__file__).with_name("app.py")
        text = source.read_text(encoding="utf-8")
        self.assertIn("SUPPLIED_SIMULATION_SOURCE", text)
        self.assertIn('acquisition["simulation_source_name"] = default_source', text)

    def test_large_json_responses_are_compressed_for_public_clients(self):
        source = Path(__file__).with_name("app.py")
        text = source.read_text(encoding="utf-8")
        self.assertIn("def encode_json_response", text)
        self.assertIn('"gzip" not in str(accept_encoding or "").lower()', text)
        self.assertIn('self.send_header("Content-Encoding", "gzip")', text)

    def test_public_live_routes_expose_websocket_upgrade(self):
        source = Path(__file__).with_name("web_access.py")
        text = source.read_text(encoding="utf-8")
        self.assertIn('"/api/simulation/ws"', text)
        self.assertIn('"/api/live/ws"', text)

    def test_simulation_dataset_route_is_public_read_only(self):
        source = Path(__file__).with_name("web_access.py")
        text = source.read_text(encoding="utf-8")
        self.assertIn('"/api/simulation/dataset"', text)

    def test_public_live_frontend_uses_websocket_stream(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn("new WebSocket", text)
        self.assertIn("/api/simulation/ws", text)
        self.assertIn("/api/live/ws", text)

    def test_simulation_uses_one_time_dataset_load_and_twenty_row_playback_cache(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn("/api/simulation/dataset", text)
        self.assertIn("SIMULATION_PLAYBACK_CACHE_ROWS = 20", text)
        self.assertIn("loadSimulationDatasetOnce", text)
        self.assertIn("simulationDatasetCache", text)

    def test_guest_start_replays_loaded_dataset_in_browser_after_server_start(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function startAcquisition()")
        end = text.index("async function stopAcquisition()", start)
        body = text[start:end]
        self.assertIn("await loadSimulationDatasetOnce();", body)
        self.assertIn('postJson("/api/simulation/start"', body)
        self.assertIn("startLocalSimulationReplay();", body)
        self.assertNotIn("await loadRealtime();\n      return;", body)

        replay_start = text.index("function startLocalSimulationReplay()")
        replay_end = text.index("function stopLocalSimulationReplay()", replay_start)
        replay_body = text[replay_start:replay_end]
        self.assertIn("state.livePollTimer", replay_body)
        self.assertIn("clearInterval", replay_body)

        cache_start = text.index("function simulationCacheFor(index)")
        cache_end = text.index("function buildCachedSimulationPayload", cache_start)
        cache_body = text[cache_start:cache_end]
        self.assertIn("Math.floor(safeIndex / SIMULATION_PLAYBACK_CACHE_ROWS)", cache_body)
        self.assertIn("cache_start", cache_body)
        self.assertIn("cache_rows", cache_body)

    def test_local_helper_websocket_route_keeps_http_fallback_contract(self):
        access = Path(__file__).with_name("web_access.py").read_text(encoding="utf-8")
        source = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
        helper = Path(__file__).with_name("local_capture_helper_entry.py").read_text(encoding="utf-8")
        self.assertIn('"/api/helper/ws"', access)
        self.assertIn("def _serve_helper_websocket", source)
        self.assertIn("api/helper/poll", helper)
        self.assertIn("run_auto_forever", helper)

    def test_simulation_interface_checkbox_follows_loaded_channel_mapping(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function refreshPhysicalInterfaceOptions")
        end = text.index("function itemEnabledForSimulation", start)
        body = text[start:end]
        self.assertNotIn("enabled.checked = true;", body)
        self.assertIn("保留${profile.label || role}的独立协议和通道映射", body)
        self.assertIn('select.value = "";', body)

    def test_simulation_channels_present_in_source_are_auto_enabled_for_collection(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn(
            "function autoEnableSimulationChannels",
            text,
            "simulation source channels must drive the sensor collection checkboxes",
        )
        start = text.index("async function uploadSimulationSource()")
        end = text.index("function acquisitionConfig()", start)
        body = text[start:end]
        self.assertIn("autoEnableSimulationChannels(state.simulationSourceChannels);", body)
        helper_start = text.index("function autoEnableSimulationChannels")
        helper_end = text.index("function isTemperatureChannel", helper_start)
        helper = text[helper_start:helper_end]
        self.assertIn("save-sensor-checkbox", helper)
        self.assertIn("available.has", helper)

    def test_guest_simulation_keeps_server_physical_mapping_read_only(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn("payload.acquisition?.interface_discovery", text)
        self.assertIn("autoAssignPhysicalInterfaces", text)
        self.assertIn("只识别接口，不检查传感器连接", text)

    def test_remote_bootstrap_skips_server_hardware_discovery(self):
        source = Path(__file__).with_name("app.py")
        text = source.read_text(encoding="utf-8")
        start = text.index('if parsed.path == "/api/bootstrap"')
        end = text.index('if parsed.path == "/api/mysql/defaults"', start)
        body = text[start:end]
        self.assertIn(
            'include_discovery=identity.role == "local_admin"',
            body,
        )
        self.assertNotIn("bootstrap(include_discovery=True)", body)

    def test_public_read_only_relation_refresh_does_not_require_capture_control(self):
        source = Path(__file__).with_name("app.py")
        text = source.read_text(encoding="utf-8")
        start = text.index("controlled_paths = {")
        end = text.index("}", start)
        body = text[start:end]
        self.assertNotIn('"/api/mysql/relation-map"', body)

    def test_discover_interfaces_has_one_authoritative_definition_with_helper_branch(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertEqual(text.count("async function discoverInterfaces()"), 1)
        body = text[text.index("async function discoverInterfaces()"):]
        self.assertIn("usesLocalCaptureHelper()", body)
        self.assertIn('requestLocalHelper("discover"', body)

    def test_authorized_discovery_enables_every_protocol_bound_interface(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function discoverInterfaces()")
        end = text.index("function recognizedInterfacePortsFrom", start)
        body = text[start:end]
        self.assertIn("autoAssignPhysicalInterfaces", body)
        self.assertIn("physical_interface_id: physicalId", body)
        self.assertIn("enabled: Boolean(physicalId)", body)

    def test_initialize_does_not_render_unassigned_defaults_before_discovery(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function initialize()")
        end = text.index("function hideLegacyInterfaceFields", start)
        body = text[start:end]
        self.assertNotIn(
            "renderInterfacePanel(payload.acquisition.interface_defaults || []);",
            body,
        )

    def test_helper_reconnect_triggers_public_interface_rediscovery(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function loadHelperStatus")
        end = text.index("async function pairLocalHelper", start)
        body = text[start:end]
        self.assertIn("wasOnline", body)
        self.assertIn("discoverInterfaces()", body)
        self.assertIn("state.helperStatus.online", body)

    def test_public_reset_check_acquires_real_control_before_protected_reset(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function resetAndCheckHardware()")
        end = text.index("function renderLiveHardwareMonitor", start)
        body = text[start:end]
        self.assertIn("await acquireRealControl();", body)
        self.assertIn("if (!usesLocalCaptureHelper())", body)
        self.assertIn('postJson("/api/acquisition/reset-check"', body)

    def test_agent_diagnosis_uses_resumable_job_submission_and_polling(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function runAgentDiagnosis")
        end = text.index("async function testSensorConnection", start)
        body = text[start:end]
        self.assertIn('postJson("/api/agent/diagnose/start"', body)
        self.assertIn('fetch(`/api/agent/diagnose/result?job_id=', text)
        self.assertIn("state.agentJobId", body)
        self.assertIn('state.accessRole === "guest"', body)
        self.assertIn('postJson("/api/agent/diagnose",', body)

    def test_agent_job_polling_survives_request_timeout_without_aborting_server_job(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function runAgentDiagnosis")
        end = text.index("async function testSensorConnection", start)
        body = text[start:end]
        self.assertIn("pollAgentDiagnosisJob", body)
        self.assertIn("state.agentJobId =", body)
        self.assertNotIn("state.agentController?.abort()", body)

    def test_simulation_mapping_does_not_cross_assign_serial_to_usb_profiles(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn("allowSerialFallback = true", text)
        self.assertIn("allowSerialFallback: false", text)

    def test_real_mapping_never_uses_serial_fallback_for_usb_profiles(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function autoAssignPhysicalInterfaces")
        end = text.index("function refreshPhysicalInterfaceOptions", start)
        body = text[start:end]
        self.assertIn(
            "allowSerialFallback && profile.physical_kind === \"serial\"",
            body,
            "real discovery must keep USB HID/UVC roles on protocol-compatible interfaces",
        )

    def test_real_mapping_accepts_protocol_driver_placeholder_without_sensor_data(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function autoAssignPhysicalInterfaces")
        end = text.index("function refreshPhysicalInterfaceOptions", start)
        body = text[start:end]
        self.assertIn(
            "candidate.auto_assignable || candidate.driver_available",
            body,
            "a detected protocol driver can be mapped before sensor data is streamed",
        )

    def test_switching_to_simulation_rebuilds_logical_catalog_instead_of_reusing_real_bindings(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn(
            "function buildSimulationInterfaceCatalog",
            text,
            "simulation mode must have an isolated logical interface catalog",
        )
        start = text.index("function updateSimulationSettings()")
        end = text.index("function updateIntegrationSource", start)
        body = text[start:end]
        self.assertIn("buildSimulationInterfaceCatalog()", body)
        self.assertNotIn(
            "state.interfaceCatalog?.length",
            body,
            "real-mode physical bindings must not leak into simulation mode",
        )

    def test_mode_switch_preserves_last_real_mapping_for_immediate_restore(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn("realInterfaceSnapshot", text)
        discover_start = text.index("async function discoverInterfaces()")
        discover_end = text.index("function recognizedInterfacePortsFrom", discover_start)
        discover = text[discover_start:discover_end]
        self.assertIn("state.realInterfaceSnapshot", discover)
        self.assertIn("最后一次真实接口映射", discover)
        self.assertIn("cached real mapping", discover)

    def test_returning_to_real_mode_refreshes_helper_status_and_mapping(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function updateSimulationSettings()")
        end = text.index("function updateIntegrationSource", start)
        body = text[start:end]
        self.assertIn("loadHelperStatus()", body)
        self.assertIn("discoverInterfaces()", body)
        self.assertIn("rememberRealInterfaceSnapshot()", body)
        self.assertIn("restoreCachedRealInterfaceSnapshot()", body)
        self.assertIn('state.interfaceModeRendered = simulation ? "simulation" : "real"', body)

    def test_local_save_picker_does_not_require_a_prior_name_and_refreshes_status(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function confirmLocalSave()")
        end = text.index("async function saveFinishedCaptureLocally", start)
        body = text[start:end]
        self.assertNotIn("!requestedName || !state.localSaveNameDirty", body)
        self.assertIn("renderSaveRootStatus({", body)
        self.assertIn("ok: true", body)
        self.assertIn("showDirectoryPicker", body)

    def test_guest_server_session_save_status_cannot_turn_empty_local_path_green(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function renderSaveRootStatus")
        end = text.index("async function refreshSaveRootStatus", start)
        function = text[start:end]
        script = """
const node = {textContent: "", classList: {toggle(name, enabled) {
  this[name] = enabled;
}}};
const controls = {saveRootStatus: node, saveRoot: {value: ""},
  acquisitionMode: {value: "simulation"}};
const state = {accessRole: "guest", localSaveAuthorized: false,
  localSaveDirectoryHandle: null};
""" + function + """
renderSaveRootStatus({ok: false, message: "保存位置为空，当前采集不保存数据"});
if (!node.textContent.includes("当前会话的服务器目录") ||
    !node.textContent.includes("未保存到访问电脑") ||
    node.textContent.includes("当前采集不保存数据") || !node.classList.error) process.exit(1);
state.accessRole = "authorized";
renderSaveRootStatus({ok: false, message: "保存位置为空，当前采集不保存数据"});
if (!node.textContent.includes("当前会话的服务器目录") ||
    node.textContent.includes("当前采集不保存数据")) process.exit(3);
state.accessRole = "local_admin";
renderSaveRootStatus({ok: false, message: "保存位置为空，当前采集不保存数据"});
if (node.textContent !== "保存位置为空，当前采集不保存数据") process.exit(2);
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_remote_simulation_mysql_off_does_not_claim_visitor_local_save(self):
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        start = text.index("function renderMysqlStatus(mysql, serverTarget = null)")
        end = text.index("function agentEscapeHtml", start)
        function = text[start:end]
        script = """
const status = {textContent: "", classList: {toggle(){}}};
const $ = () => status;
const state = {accessRole: "guest", mysqlConnectionTests: {target: null, local: null}};
const controls = {mysqlEnabled: {checked: false}, mysqlLocalEnabled: {checked: false},
  acquisitionMode: {value: "simulation"}};
""" + function + """
renderMysqlStatus({enabled: false, ok: false});
if (!status.textContent.includes("当前会话的服务器目录") ||
    status.textContent.includes("已完成本地保存")) process.exit(1);
state.accessRole = "local_admin";
renderMysqlStatus({enabled: false, ok: false});
if (status.textContent !== "MySQL 保存未启用；原始文件已完成本地保存。") process.exit(2);
"""
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_save_directory_status_is_checked_against_server_folder(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn("/api/acquisition/save-status?path=", text)
        self.assertIn("当前采集不保存数据", text)

    def test_frontend_contains_local_helper_pairing_controls(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        index = source.with_name("index.html").read_text(encoding="utf-8")
        self.assertIn('id="helperStatus"', index)
        self.assertIn('id="pairHelperButton"', index)
        self.assertIn("/api/helper/status", text)
        self.assertIn("/api/helper/pair/start", text)

    def test_real_capture_routes_through_online_local_helper(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn("function livePollIntervalMs()", text)
        self.assertIn("livePollIntervalMs()", text)
        self.assertIn("requestLocalHelper(\"discover\"", text)
        self.assertIn("requestLocalHelper(\"check_capture\"", text)
        self.assertIn("timeoutMs: 120000", text)
        self.assertIn("requestLocalHelper(\"start_capture\"", text)
        self.assertIn("requestLocalHelper(\"stop_capture\"", text)

    def test_helper_capture_waits_for_server_to_receive_first_sample(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn("async function waitForEdgeFirstSample", text)
        start = text.index("async function startAcquisition()")
        end = text.index("async function stopAcquisition()", start)
        body = text[start:end]
        self.assertIn("await waitForEdgeFirstSample", body)
        self.assertIn("capture_uuid", body)

    def test_helper_backed_bootstrap_does_not_expose_server_interface_inventory(self):
        source = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
        start = source.index('if parsed.path == "/api/bootstrap"')
        end = source.index('if parsed.path == "/api/mysql/defaults"', start)
        body = source[start:end]
        self.assertIn("uses_local_capture_helper(identity.role)", body)
        self.assertIn('acquisition["interface_discovery"] = {"physical_interfaces": []}', body)

    def test_helper_backed_save_folder_never_uses_server_picker_or_server_path_check(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        select_start = text.index("async function selectSaveRoot()")
        select_end = text.index("function updateLocalSaveStatus", select_start)
        select_body = text[select_start:select_end]
        self.assertIn('requestLocalHelper("select_folder"', select_body)

        status_start = text.index("async function refreshSaveRootStatus")
        status_end = text.index("async function confirmLocalSave", status_start)
        status_body = text[status_start:status_end]
        self.assertIn('requestLocalHelper("check_save_root"', status_body)

    def test_remote_local_mysql_uses_helper_encrypted_profile(self):
        text = (Path(__file__).with_name("static") / "app.js").read_text(encoding="utf-8")
        self.assertIn("localMysqlProfile", text)
        self.assertIn('requestLocalHelper("mysql_profile_save"', text)
        self.assertIn("use_saved_profile: true", text)
        config_start = text.index("function acquisitionConfig()")
        config_end = text.index("async function readProcessParameters", config_start)
        config_body = text[config_start:config_end]
        self.assertIn('usesLocalCaptureHelper() ? ""', config_body)

    def test_helper_backed_real_capture_does_not_repeat_server_export(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function saveFinishedCaptureLocally(")
        end = text.index("async function testMysqlConnection", start)
        body = text[start:end]
        self.assertIn("usesLocalCaptureHelper()", body)
        self.assertIn('mode !== "simulation"', body)
        self.assertIn('mode === "simulation"', body)
        self.assertIn('"/api/simulation" : "/api/acquisition"', body)
        self.assertIn("return", body)

    def test_diagnosis_uses_helper_hardware_evidence_and_remote_acquisition_status(self):
        source = Path(__file__).with_name("app.py").read_text(encoding="utf-8")
        self.assertIn("def _diagnostic_context", source)
        self.assertIn("uses_local_capture_helper(identity.role)", source)
        self.assertIn('"local_helper"', source)
        self.assertIn('"remote_browser_snapshot"', source)
        self.assertNotIn(
            "discovery = deepcopy(self.dashboard.acquisition.discover_interfaces())",
            source,
        )


if __name__ == "__main__":
    unittest.main()
