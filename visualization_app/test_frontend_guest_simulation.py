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


if __name__ == "__main__":
    unittest.main()
