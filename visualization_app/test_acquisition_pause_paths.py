"""Simulation HTTP, helper transport, and both user interfaces without hardware."""
import json
import http.client
import re
from pathlib import Path
import subprocess
import tempfile
import time
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app
import test_public_web_security as web_fixture
from test_acquisition_pause import CountingDriver, wait_until
from acquisition import AcquisitionManager, AcquisitionConfig
from local_capture_agent import LocalCaptureAgent
from local_capture_helper_entry import dispatch_command, helper_capabilities
from edge_capture import RemoteAcquisitionMirror


class PauseHttpTests(unittest.TestCase):
    setUp = web_fixture.PublicWebHttpTests.setUp
    tearDown = web_fixture.PublicWebHttpTests.tearDown
    request_json = web_fixture.PublicWebHttpTests.request_json

    def test_guest_pause_survives_page_refresh_and_is_scoped_to_browser(self):
        self.request_json("GET", "/api/auth/session")
        code, started, _ = self.request_json("POST", "/api/simulation/start", {
            "processing_mode": "capture_only", "sample_rate_hz": 50, "selected_sensors": ["温度1"]})
        self.assertEqual(code, 200, started)
        guest = self.server.guest_manager.ensure_session(self.cookies["afp_guest"]).acquisition
        try:
            wait_until(lambda: guest.total_sample_count >= 2)
            code, paused, _ = self.request_json("POST", "/api/simulation/pause", {"capture_uuid": started["capture_uuid"]})
            self.assertEqual(code, 200, paused)
            self.assertTrue(paused["paused"])
            code, refreshed, _ = self.request_json("GET", "/api/simulation/status")
            self.assertEqual(code, 200)
            self.assertTrue(refreshed["paused"])
            count = refreshed["sample_count"]
            time.sleep(.1)
            self.assertEqual(guest.total_sample_count, count)
            owner_cookies = self.cookies
            self.cookies = {}
            self.request_json("GET", "/api/auth/session")
            code, denied, _ = self.request_json("POST", "/api/simulation/resume", {"capture_uuid": started["capture_uuid"]})
            self.assertEqual(code, 400, denied)
            self.assertTrue(guest.status()["paused"])
            self.cookies = owner_cookies
            code, resumed, _ = self.request_json("POST", "/api/simulation/resume", {"capture_uuid": started["capture_uuid"]})
            self.assertEqual(code, 200, resumed)
            self.assertTrue(resumed["sampling"])
            self.assertEqual(resumed["capture_uuid"], started["capture_uuid"])
            self.request_json("POST", "/api/simulation/pause", {})
            code, stopped, _ = self.request_json("POST", "/api/simulation/stop", {"capture_uuid": started["capture_uuid"]})
            self.assertEqual(code, 200, stopped)
            self.assertTrue(stopped["finalization_complete"])
            self.assertTrue(stopped["capture_saved"])
            self.assertEqual(self.hardware_start_calls, 0)
        finally:
            guest.stop()

    def test_pause_requires_existing_permission_and_csrf(self):
        self.request_json("GET", "/api/auth/session")
        code, _, _ = self.request_json("POST", "/api/acquisition/pause", {"acquisition_mode": "real"})
        self.assertEqual(code, 403)
        code, result, _ = self.request_json("POST", "/api/simulation/pause", {}, csrf=False)
        self.assertEqual(code, 403)
        self.assertEqual(result["error"], "csrf_failed")


class PauseHelperTests(unittest.TestCase):
    def test_helper_dispatch_preserves_pause_status_and_one_stream_identity(self):
        with tempfile.TemporaryDirectory() as root:
            manager = AcquisitionManager(Path(root), recovery_scan=False)
            agent = LocalCaptureAgent(manager, simulation_cache_root=Path(root) / "cache")
            cfg = AcquisitionConfig(acquisition_mode="simulation", processing_mode="capture_only",
                selected_sensors=["温度1"], save_root=root, sample_rate_hz=50)
            driver = CountingDriver()
            driver.read_sample = lambda: {"温度1": float(driver.count)}
            with patch("acquisition.build_driver", return_value=driver):
                started = agent.start_capture(cfg)
            def command(action):
                return dispatch_command(agent, json.dumps({"type": "command", "request_id": action,
                    "command": action + "_capture", "payload": {"capture_uuid": started["capture_uuid"]}}))["payload"]
            mirror = RemoteAcquisitionMirror()
            try:
                wait_until(lambda: manager.total_sample_count >= 2)
                self.assertTrue(command("pause")["paused"])
                batch = agent.next_sample_batch()
                self.assertTrue(mirror.ingest(batch)["ok"])
                agent.ack_sample_batch(batch["capture_uuid"], batch["sequence"])
                self.assertEqual(mirror.status()["capture_phase"], "paused")
                self.assertTrue(mirror.status()["session_active"])
                self.assertFalse(mirror.status()["sampling"])
                self.assertTrue(command("resume")["sampling"])
                wait_until(lambda: manager.total_sample_count > batch["status"]["sample_count"] + 1)
                next_batch = agent.next_sample_batch()
                self.assertTrue(mirror.ingest(next_batch)["ok"])
                self.assertEqual(mirror.status()["capture_uuid"], started["capture_uuid"])
                self.assertEqual(mirror.status()["capture_phase"], "running")
                self.assertTrue(helper_capabilities()["capture_pause_v1"])
            finally:
                manager.stop()


class PauseInterfaceTests(unittest.TestCase):
    def test_runtime_header_distinguishes_paused_sampling_from_active_session(self):
        source = (Path(__file__).parent / "static/app.js").read_text(encoding="utf-8")
        phase = source[source.index("function acquisitionDisplayPhase(status)"):source.index("function optionalFiniteNumber(")]
        header = source[source.index("function renderRuntimeStatus("):source.index("function applyPredictionModelProfile(")]
        script = r'''
const assert=require('assert'),node={textContent:''},dot={classList:{toggle(name,value){this[name]=value}}};
const $=()=>node,document={querySelector:()=>dot},state={},controls={dataMode:{value:'live'},processingMode:{value:'capture_only'}};
''' + phase + header + r'''
renderRuntimeStatus({acquisition:{running:true,session_active:true,sampling:false,paused:true,capture_phase:'paused',sample_count:1200}});
assert.match(node.textContent,/采集已中断/);assert.equal(dot.classList.active,false);
'''
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_disconnect_uses_existing_connection_without_revalidating_start_configuration(self):
        source = (Path(__file__).parent / "static/app.js").read_text(encoding="utf-8")
        function = source[source.index("async function toggleDataConnection(event)"):source.index("async function startAcquisition()")]
        script = r'''
const assert=require('assert'),calls=[];
const state={},controls={acquisitionMode:{value:'real'}},window=globalThis;
const usesLocalCaptureHelper=()=>false,synchronizeAcquisitionButtons=()=>{};
const acquireRealControl=async()=>calls.push('permission');
const fetch=async()=>({json:async()=>({data_connection:{state:'connected'}})});
const acquisitionConfig=()=>{throw Error('Disconnect must not parse acquisition settings')};
const postJson=async(path,payload)=>{calls.push(path);assert.deepEqual(payload,{});return {data_connection:{state:'disconnected'},finalization_complete:true}};
const renderAcquisitionStatus=()=>{},toast=message=>{throw Error(message)},$=()=>({textContent:''});
''' + function + r'''
toggleDataConnection().then(()=>{assert.deepEqual(calls,['permission','/api/acquisition/disconnect']);assert.equal(state.dataConnectionBusy,false)}).catch(e=>{console.error(e);process.exitCode=1});
'''
        result = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_web_buttons_restore_paused_state_and_keep_failed_save_locked(self):
        script = (Path(__file__).parent / "static/app.js").read_text(encoding="utf-8")
        start = script.index("function synchronizeAcquisitionButtons()")
        end = script.index("async function toggleAcquisitionPause()", start)
        javascript = r'''
const assert=require('assert'); const nodes=new Map();
const $=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',disabled:false,classList:{toggle(name,on){this[name]=on}}});return nodes.get(id)};
const state={acquisitionStatus:{running:false}}, controls={acquisitionMode:{value:'simulation'}};
const isPublicPrecomputedSimulationMode=()=>false,usesLocalCaptureHelper=()=>false;
''' + script[start:end] + r'''
synchronizeAcquisitionButtons();assert.equal($('startAcquisitionButton').disabled,false);
assert.equal($('pauseAcquisitionButton').disabled,true);assert.equal($('stopAcquisitionButton').disabled,true);
assert.equal($('connectDataButton').classList.hidden,false);assert.equal($('connectDataButton').disabled,true);
controls.acquisitionMode.value='real';state.acquisitionStatus={running:false,data_connection:{state:'connected'}};
synchronizeAcquisitionButtons();assert.equal($('connectDataButton').textContent,'断开数据');assert.equal($('connectDataButton').disabled,false);
state.acquisitionStatus={running:true,session_active:true,paused:true,capture_phase:'paused'};
synchronizeAcquisitionButtons();assert.equal($('pauseAcquisitionButton').textContent,'继续采集');
assert.equal($('pauseAcquisitionButton').disabled,false);assert.equal($('stopAcquisitionButton').disabled,false);
assert.equal($('startAcquisitionButton').disabled,true);
assert.equal($('connectDataButton').textContent,'连接数据');assert.equal($('connectDataButton').disabled,true);
state.acquisitionStatus={running:false,session_active:true,save_failed:true,capture_phase:'finalization_failed',capture_uuid:'x',config:{}};
synchronizeAcquisitionButtons();assert.equal($('startAcquisitionButton').disabled,true);
state.acquisitionStatus={running:true,capture_phase:'resuming'};
synchronizeAcquisitionButtons();assert.equal($('pauseAcquisitionButton').disabled,true);
assert.equal($('stopAcquisitionButton').disabled,false);
state.acquisitionStatus={running:false,session_active:true,capture_phase:'pausing'};
synchronizeAcquisitionButtons();assert.equal($('stopAcquisitionButton').disabled,false);
state.acquisitionStatus={running:false,session_active:true,capture_phase:'finalizing',saving:true};
synchronizeAcquisitionButtons();assert.equal($('stopAcquisitionButton').disabled,true);
'''
        result = subprocess.run(["node", "-e", javascript], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_native_paused_controls_and_failed_save_use_execution_state(self):
        from native_integrated_app import AcquisitionPanel
        class Widget:
            def __init__(self): self.values = {}
            def config(self, **values): self.values.update(values)
        panel = SimpleNamespace(_control_busy=False, _locked_widgets={}, winfo_children=lambda: [])
        for name in ("start_button", "stop_button", "pause_button", "test_button"):
            setattr(panel, name, Widget())
        AcquisitionPanel._sync_capture_controls(panel, {"running": True, "session_active": True, "paused": True, "capture_phase": "paused"})
        self.assertEqual(panel.pause_button.values, {"text": "继续采集", "state": "normal"})
        self.assertEqual(panel.start_button.values["state"], "disabled")
        self.assertEqual(panel.stop_button.values["state"], "normal")
        AcquisitionPanel._sync_capture_controls(panel, {"session_active": True, "save_failed": True, "capture_phase": "finalization_failed"})
        self.assertEqual(panel.start_button.values["state"], "disabled")
        self.assertEqual(panel.pause_button.values["state"], "disabled")
        AcquisitionPanel._sync_capture_controls(panel, {"session_active": True, "capture_phase": "pausing"})
        self.assertEqual(panel.stop_button.values["state"], "normal")
        AcquisitionPanel._sync_capture_controls(panel, {"session_active": True, "capture_phase": "finalizing", "saving": True})
        self.assertEqual(panel.stop_button.values["state"], "disabled")

    def test_paused_live_response_skips_prediction_and_resume_window_is_segment_scoped(self):
        acquisition = SimpleNamespace(status=lambda: {"running": True, "paused": True, "sample_count": 10,
            "segment_start_index": 7, "config": {"processing_mode": "prediction_warning", "selected_sensors": ["温度1"]}},
            numeric_matrix=lambda: ([{"温度1": i} for i in range(10)], list(range(10))),
            frame_quality=[{}] * 10, frame_sources=[{}] * 10)
        captured = {}
        def capture_only(**kwargs):
            captured.update(kwargs)
            return {"acquisition": kwargs["status"]}
        dashboard = SimpleNamespace(acquisition=acquisition, _capture_only_live=capture_only)
        app.DashboardData.live(dashboard, sensor_id=0, history=240, step=1, threshold=.5, rho=.5,
            indicator="TC-HI", model_kind="random_forest", prediction_horizon=24)
        self.assertEqual(captured["rows"], [{"温度1": 7}, {"温度1": 8}, {"温度1": 9}])
        self.assertTrue(captured["status"]["paused"])


class IsolatedPreviewTests(unittest.TestCase):
    @unittest.skipUnless(Path("D:/AFP_Runtime/.afp-staging").is_dir(), "Windows D-drive preview entry")
    def test_preview_rejects_external_control_and_forces_isolated_simulation(self):
        from preview_acquisition import create_preview
        staging = Path("D:/AFP_Runtime/.afp-staging")
        with tempfile.TemporaryDirectory(dir=staging, prefix="pause-preview-test-") as root:
            server = create_preview(Path(root))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def request(method, path, body=None, token=None, host=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
                headers = {"Host": host or f"127.0.0.1:{server.server_port}", "Content-Type": "application/json"}
                if token: headers["X-Preview-Token"] = token
                try:
                    connection.request(method, path, json.dumps(body) if body is not None else None, headers)
                    response = connection.getresponse()
                    return response.status, response.read().decode("utf-8")
                finally:
                    connection.close()
            try:
                self.assertEqual(server.server_address[0], "127.0.0.1")
                code, page = request("GET", "/")
                self.assertEqual(code, 200)
                token = re.search("X-Preview-Token':'([^']+)'", page).group(1)
                self.assertEqual(request("POST", "/api/start", {})[0], 403)
                self.assertEqual(request("GET", "/api/status", host="external.example")[0], 403)
                code, result = request("POST", "/api/start", {"acquisition_mode": "real", "mysql_enabled": True,
                    "save_root": "D:/forbidden-preview-target", "rate": 30}, token)
                self.assertEqual(code, 200, result)
                started = json.loads(result)["status"]
                self.assertEqual(started["config"]["acquisition_mode"], "simulation")
                self.assertFalse(started["config"]["mysql_enabled"])
                self.assertTrue(Path(root) in Path(started["save_root"]).parents)
                wait_until(lambda: server.acquisition.total_sample_count >= 2)
                code, result = request("POST", "/api/pause", {"capture_uuid": started["capture_uuid"]}, token)
                self.assertEqual(code, 200, result)
                paused = json.loads(result)["status"]
                time.sleep(.08)
                refreshed = json.loads(request("GET", "/api/status")[1])["status"]
                self.assertTrue(refreshed["paused"])
                self.assertEqual(refreshed["sample_count"], paused["sample_count"])
                code, result = request("POST", "/api/stop", {"capture_uuid": started["capture_uuid"]}, token)
                self.assertEqual(code, 200, result)
                self.assertTrue(json.loads(result)["status"]["capture_saved"])
            finally:
                server.acquisition.stop()
                server.shutdown()
                server.server_close()
                thread.join(2)


if __name__ == "__main__":
    unittest.main()
