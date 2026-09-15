from pathlib import Path
import unittest


class GuestSimulationFrontendContractTests(unittest.TestCase):
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
        self.assertIn('mysql_host: settings.mysql_host', body)
        self.assertIn('mysql_port: settings.mysql_port', body)
        self.assertNotIn('settings.mysql_local_host', body)
        self.assertNotIn('settings.mysql_local_port', body)

    def test_public_authorized_page_does_not_replace_visitor_mysql_defaults(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function loadMysqlDefaults")
        end = text.index("const LAYER_EVIDENCE_VISIBILITY_KEY", start)
        body = text[start:end]
        self.assertIn('state.accessRole === "local_admin"', body)
        self.assertIn("assign(controls.mysqlLocalHost, local.host)", body)

    def test_lan_admin_does_not_require_public_helper_pairing(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function renderHelperStatus")
        end = text.index("async function loadHelperStatus", start)
        body = text[start:end]
        self.assertIn('state.accessRole === "local_admin"', body)
        self.assertIn("局域网直连本机真实采集，无需辅助程序", body)

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

    def test_simulation_interface_checkbox_follows_loaded_channel_mapping(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("function refreshPhysicalInterfaceOptions")
        end = text.index("function itemEnabledForSimulation", start)
        body = text[start:end]
        self.assertIn("enabled.checked = true;", body)
        self.assertIn("不检查传感器是否接入", body)

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

    def test_guest_bootstrap_includes_only_read_only_interface_inventory(self):
        source = Path(__file__).with_name("app.py")
        text = source.read_text(encoding="utf-8")
        self.assertIn("bootstrap = self.dashboard.bootstrap(include_discovery=True)", text)
        self.assertIn('acquisition["interface_discovery"] = {', text)
        self.assertIn('"physical_interfaces": physical_interfaces', text)

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
        self.assertIn('state.accessRole === "authorized"', body)
        self.assertIn('requestLocalHelper("discover"', body)

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
        body = text[start:end]
        self.assertIn('state.accessRole === "guest"', body)
        self.assertIn("state.localSaveAuthorized", body)
        self.assertIn("保存位置为空，当前采集不保存数据", body)

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
        self.assertIn("requestLocalHelper(\"discover\"", text)
        self.assertIn("requestLocalHelper(\"check_capture\"", text)
        self.assertIn("requestLocalHelper(\"start_capture\"", text)
        self.assertIn("requestLocalHelper(\"stop_capture\"", text)


if __name__ == "__main__":
    unittest.main()
