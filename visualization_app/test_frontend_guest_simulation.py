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

    def test_guest_auto_start_is_suppressed_after_explicit_stop(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        self.assertIn(
            "guestSimulationStoppedByUser",
            text,
            "an explicit stop must prevent loadRealtime from starting a new guest run",
        )
        self.assertIn(
            "!state.guestSimulationStoppedByUser",
            text,
            "guest auto-start must honor the explicit-stop latch",
        )

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

    def test_guest_upload_restarts_simulation_without_download_ui(self):
        source = Path(__file__).with_name("static") / "app.js"
        text = source.read_text(encoding="utf-8")
        start = text.index("async function uploadSimulationSource()")
        end = text.index("function acquisitionConfig()", start)
        body = text[start:end]
        self.assertIn('"/api/simulation/start"', body)
        self.assertIn("正在用新数据重新开始模拟采集", body)
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


if __name__ == "__main__":
    unittest.main()
