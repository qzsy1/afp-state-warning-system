from __future__ import annotations

import math
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


DELIVERY_ROOT = Path(__file__).resolve().parents[1]
LEGACY_APP = DELIVERY_ROOT / "app" / "legacy"
sys.path.insert(0, str(LEGACY_APP))

import acquisition  # noqa: E402
import public_status  # noqa: E402


class _SerialProbe:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False

    @property
    def in_waiting(self) -> int:
        return len(self.payload)

    def read(self, size: int) -> bytes:
        value, self.payload = self.payload[:size], self.payload[size:]
        return value

    def close(self) -> None:
        self.closed = True


class _PartialInterfaceDriver:
    def __init__(self, *args, **kwargs) -> None:
        self.calls = 0

    def open(self) -> None:
        return None

    def read_sample(self):
        self.calls += 1
        return {"温度": 23.5} if self.calls == 1 else None

    def close(self) -> None:
        return None


class _FastDriver:
    def __init__(self) -> None:
        self.value = 0

    def open(self) -> None:
        return None

    def read_sample(self):
        self.value += 1
        return {"压力": float(self.value)}

    def close(self) -> None:
        return None


class _BlockingDriver(_FastDriver):
    def read_sample(self):
        time.sleep(0.25)
        return super().read_sample()


class _PayloadDriver:
    def __init__(self, payload) -> None:
        self.payload = payload

    def open(self) -> None:
        return None

    def read_sample(self):
        return dict(self.payload)

    def close(self) -> None:
        return None


class _ManagedFakeMultiInterface(acquisition.MultiInterfaceDriver):
    def __init__(self) -> None:
        self.cache = acquisition.ChannelSampleCache()
        self.workers = []
        self.drivers = []

    def open(self) -> None:
        self.cache.publish(
            acquisition.ChannelSample(
                interface_id="plc_process",
                channel_name="压力",
                value=10.0,
                source_sequence=1,
            )
        )

    def close(self) -> None:
        return None


class RealAcquisitionRegressionTests(unittest.TestCase):
    def test_simulation_readiness_is_isolated_from_cached_real_failure(self) -> None:
        manager = acquisition.AcquisitionManager()
        real_failure = {
            "acquisition_mode": "real",
            "checked_at": 123.0,
            "interfaces": [{"role": "plc", "state": "network_path_invalid", "ok": False}],
            "sensors": [],
        }
        manager._latest_check_result = real_failure
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "simulation.csv"
            source.write_text("压力\n12.5\n", encoding="utf-8-sig")
            config = acquisition.AcquisitionConfig(
                acquisition_mode="simulation",
                dataset_schema="new_collection_v11_3",
                source_file=str(source),
                simulation_source_path=str(source),
                selected_sensors=["压力"],
                interfaces=[{
                    "id": "simulation_source", "enabled": True, "role": "custom",
                    "driver": "simulator", "endpoint": str(source),
                }],
                interface_channel_assignments={"simulation_source": ["压力"]},
            )

            result = manager.test_connection(config, timeout_seconds=0.01)

        self.assertEqual("simulation_source", result["readiness_namespace"])
        self.assertEqual([], result["interfaces"])
        self.assertTrue(result["simulation_readiness"]["replay_ready"])
        self.assertEqual(
            ["source_open", "schema_compatible", "channels_complete", "values_valid", "replay_ready"],
            [item["name"] for item in result["simulation_readiness"]["stages"]],
        )
        self.assertEqual(real_failure, manager.latest_check_result("real"))
        self.assertEqual(result, manager.latest_check_result("simulation"))

    def test_simulation_missing_channel_reports_only_source_mapping_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "simulation.csv"
            source.write_text("压力\n12.5\n", encoding="utf-8-sig")
            config = acquisition.AcquisitionConfig(
                acquisition_mode="simulation",
                dataset_schema="new_collection_v11_3",
                source_file=str(source),
                simulation_source_path=str(source),
                selected_sensors=["压力", "温度"],
                interfaces=[{
                    "id": "simulation_source", "enabled": True, "role": "custom",
                    "driver": "simulator", "endpoint": str(source),
                }],
                interface_channel_assignments={"simulation_source": ["压力", "温度"]},
            )

            result = acquisition.AcquisitionManager().test_connection(config, timeout_seconds=0.01)

        self.assertFalse(result["ok"])
        readiness = result["simulation_readiness"]
        self.assertEqual("failed", readiness["stages"][2]["state"])
        message = "；".join(result["errors"] + [readiness["message"]])
        self.assertIn("模拟数据", message)
        for physical_word in ("USB", "串口", "PLC", "ABB", "UVC"):
            self.assertNotIn(physical_word, message)

    def test_discovery_evidence_never_auto_binds_placeholders_or_generic_serial(self) -> None:
        candidates = acquisition.annotate_discovery_evidence([
            {
                "id": "uvc:bsv", "kind": "usb_uvc", "protocol": "uvc",
                "detected": False, "driver_available": True,
            },
            {
                "id": "serial:COM3", "kind": "serial", "protocol": "serial",
                "detected": True, "description": "Standard Serial over Bluetooth link (COM3)",
                "vid": None, "pid": None, "serial": "", "location": "",
            },
            {
                "id": "hid:smrf", "kind": "usb_hid", "protocol": "smrf_hid",
                "detected": True, "vid": 0x1234, "pid": 0x5678,
                "serial": "SMRF-001", "product": "SMRFCT08B",
            },
            {
                "id": "ethernet:flclash", "kind": "ethernet", "protocol": "ethernet",
                "detected": True, "name": "FlClash TUN", "addresses": ["198.18.0.1"],
            },
        ])

        by_id = {item["id"]: item for item in candidates}
        self.assertFalse(by_id["uvc:bsv"]["endpoint_present"])
        self.assertFalse(by_id["uvc:bsv"]["auto_bind_eligible"])
        self.assertFalse(by_id["serial:COM3"]["identity_verified"])
        self.assertTrue(by_id["serial:COM3"]["bluetooth_virtual"])
        self.assertFalse(by_id["serial:COM3"]["auto_bind_eligible"])
        self.assertTrue(by_id["hid:smrf"]["identity_verified"])
        self.assertTrue(by_id["hid:smrf"]["auto_bind_eligible"])
        self.assertFalse(by_id["ethernet:flclash"]["identity_verified"])
        self.assertFalse(by_id["ethernet:flclash"]["auto_bind_eligible"])
        self.assertTrue(all("protocol_ready" in item for item in candidates))

    def test_serial_identity_move_requires_confirmation_and_ignores_bluetooth(self) -> None:
        saved = {
            "vid": 0x1A86, "pid": 0x7523, "serial": "M3232-001",
            "location": "Port_#0001.Hub_#0002", "endpoint": "COM8",
            "device_profile_id": "m3232-v1",
        }
        ports = [
            {
                "endpoint": "COM3", "description": "Standard Serial over Bluetooth link",
                "vid": None, "pid": None, "serial": "", "location": "",
            },
            {
                "endpoint": "COM11", "description": "USB-SERIAL CH340",
                "vid": 0x1A86, "pid": 0x7523, "serial": "M3232-001",
                "location": "Port_#0004.Hub_#0002",
            },
        ]

        result = acquisition.resolve_serial_identity(saved, ports)

        self.assertEqual("moved_requires_confirmation", result["state"])
        self.assertEqual("COM11", result["resolved_endpoint"])
        self.assertFalse(result["confirmed"])
        self.assertNotEqual("COM3", result["resolved_endpoint"])

    def test_network_discovery_does_not_promote_tun_tcp_probe_to_device_reachable(self) -> None:
        summary = acquisition.build_network_discovery_summary(
            "192.168.125.5", 502, True,
            [{
                "id": "ethernet:flclash", "kind": "ethernet",
                "name": "FlClash TUN", "addresses": ["198.18.0.1"],
                "detected": True,
            }],
        )

        self.assertTrue(summary["tcp_probe_reachable"])
        self.assertFalse(summary["network_path_valid"])
        self.assertFalse(summary["protocol_ready"])
        self.assertEqual("network_path_invalid", summary["state"])
        self.assertFalse(summary["device_reachable"])

    def test_public_status_preserves_canonical_and_unknown_failure_states(self) -> None:
        payload = public_status.build_public_device_status(
            {},
            {"checked_at": 10.0, "interfaces": [
                {"role": "plc", "state": "network_path_invalid", "ok": False},
                {"role": "pressure", "state": "hardware_protocol_unverified", "ok": False},
                {"role": "thermal_uvc", "state": "vendor_future_failure", "ok": False},
            ]},
            {},
            now=11.0,
        )
        by_role = {item["role"]: item for item in payload["interfaces"]}

        self.assertEqual("network_path_invalid", by_role["plc"]["state"])
        self.assertEqual("hardware_protocol_unverified", by_role["pressure"]["state"])
        self.assertEqual("unknown_failure", by_role["thermal_uvc"]["state"])
        self.assertEqual("vendor_future_failure", by_role["thermal_uvc"]["raw_state"])
        self.assertFalse(by_role["thermal_uvc"]["ok"])

    def test_real_start_rejects_check_from_other_runtime_identity(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            execution_host="server",
            execution_device_id="station-a",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["压力"],
            interfaces=[{
                "id": "plc_process", "enabled": True, "role": "plc",
                "driver": "modbus_tcp", "endpoint": "192.168.125.5:502",
                "physical_interface_id": "ethernet:test", "physical_verified": True,
                "source_address": "192.168.125.20",
            }],
            interface_channel_assignments={"plc_process": ["压力"]},
        )
        manager = acquisition.AcquisitionManager()
        manager._latest_check_result = {
            "config_fingerprint": acquisition.readiness_config_fingerprint(config),
            "cache_identity": {
                "acquisition_mode": "real", "execution_host": "helper_local",
                "execution_device_id": "station-b", "runtime_revision": "old-revision",
                "config_fingerprint": acquisition.readiness_config_fingerprint(config),
            },
            "readiness_reports": [{
                "interface_id": "plc_process", "state": "ready", "ready": True,
            }],
        }

        with patch.dict("os.environ", {"AFP_RUNTIME_REVISION": "current-revision"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "检查缓存.*过期"):
                manager.start(config)

    def test_uvc_open_failure_preserves_vendor_stage_and_codes(self) -> None:
        class _FakeFunction:
            def __init__(self, result):
                self.result = result
                self.argtypes = None
                self.restype = None

            def __call__(self, *args):
                return self.result

        class _FakeDll:
            def __init__(self):
                self.BsvSetTempRangeCode = _FakeFunction(1)
                self.BsvOpen = _FakeFunction(-2)
                self.BsvStart = _FakeFunction(1)
                self.BsvHasFrame = _FakeFunction(0)
                self.BsvGetFrame = _FakeFunction(0)
                self.BsvGetLastError = _FakeFunction(17)
                self.BsvStop = _FakeFunction(None)
                self.BsvClose = _FakeFunction(None)

        driver = acquisition.UvcThermalDriver()
        with patch.object(driver, "_find_dll", return_value=Path("BsvUvcNative.dll")), \
                patch.object(acquisition.ctypes, "CDLL", return_value=_FakeDll()):
            with self.assertRaises(acquisition.VendorDriverError) as captured:
                driver.open()

        self.assertEqual("device_open", captured.exception.stage)
        self.assertEqual(-2, captured.exception.return_code)
        self.assertEqual(17, captured.exception.last_error_code)
        self.assertEqual(-2, driver.quality_metadata["vendor_return_code"])
        self.assertEqual(17, driver.quality_metadata["vendor_last_error_code"])

    def test_frontend_uses_separate_simulation_and_real_readiness_contracts(self) -> None:
        source = (DELIVERY_ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")
        start = source.rindex("async function testSensorConnection(")
        end = source.index("async function resetAndCheckHardware", start)
        body = source[start:end]

        self.assertIn("simulationSourceCheck", source)
        self.assertIn("realHardwareCheck", source)
        self.assertIn("renderSimulationSourceCheckResult", body)
        simulation_branch = body[:body.index("if (usesLocalCaptureHelper()")]
        self.assertNotIn("renderHardwareCheckResult(result", simulation_branch)

    def test_frontend_names_the_active_readiness_source_in_controls(self) -> None:
        source = (DELIVERY_ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")
        marker = "function updateReadinessModePresentation()"
        self.assertIn(marker, source)
        start = source.index(marker)
        end = source.index("\nfunction ", start + len(marker))
        function = source[start:end]
        script = r'''
const mode = {value: "simulation"};
const indicator = {textContent: "", dataset: {}};
const checkButton = {textContent: ""};
const resetButton = {textContent: ""};
const controls = {acquisitionMode: mode, resetSensorCheck: resetButton};
const nodes = {
  acquisitionSourceIndicator: indicator,
  testSensorsButton: checkButton,
};
const $ = (id) => nodes[id] || null;
const currentReadinessMode = () => mode.value === "simulation" ? "simulation" : "real";
''' + function + r'''
updateReadinessModePresentation();
if (indicator.dataset.mode !== "simulation") process.exit(1);
if (!indicator.textContent.includes("模拟数据源")) process.exit(2);
if (!checkButton.textContent.includes("模拟数据源")) process.exit(3);
if (!resetButton.textContent.includes("模拟数据源")) process.exit(4);
mode.value = "real";
updateReadinessModePresentation();
if (indicator.dataset.mode !== "real") process.exit(5);
if (!indicator.textContent.includes("真实物理接口")) process.exit(6);
if (!checkButton.textContent.includes("真实接口")) process.exit(7);
if (!resetButton.textContent.includes("真实接口")) process.exit(8);
'''
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_simulation_interface_cards_default_to_normal_without_hardware_probe(self) -> None:
        source = (DELIVERY_ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")
        start = source.index("function renderSimulationSourceCheckResult(")
        end = source.index("\nfunction renderHardwareCheckResult", start)
        function = source[start:end]
        script = r'''
const statusNode = {
  className: "", children: [],
  replaceChildren(...items) { this.children = items; },
  appendChild(item) { this.children.push(item); },
};
const makeRow = () => ({
  dataset: {}, title: "", added: [],
  classList: {add(value) { this.owner.added.push(value); }, owner: null},
  querySelector() { return null; },
});
const interfaceRows = [makeRow(), makeRow(), makeRow(), makeRow(), makeRow()];
interfaceRows.forEach((row) => { row.classList.owner = row; });
const controls = {hardwareCheckStatus: statusNode};
const clearHardwareRowStates = () => {};
const document = {
  createElement() { return {textContent: "", className: ""}; },
  querySelectorAll(selector) {
    if (selector === ".interface-config-row") return interfaceRows;
    return [];
  },
};
''' + function + r'''
renderSimulationSourceCheckResult({
  ok: false,
  simulation_readiness: {replay_ready: false, stages: []},
  sensors: [],
  interfaces: [],
});
if (interfaceRows.some((row) => !row.added.includes("check-ok"))) process.exit(1);
if (interfaceRows.some((row) => row.dataset.checkState !== "模拟接口正常")) process.exit(2);
if (interfaceRows.some((row) => row.added.includes("check-error"))) process.exit(3);
'''
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_simulation_mode_ignores_stale_physical_kind_mismatch(self) -> None:
        source = (DELIVERY_ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")
        start = source.index("function validatePhysicalInterfaceBindings(")
        end = source.index("\ncontrols.acquisitionMode?.addEventListener", start)
        function = source[start:end]
        script = r'''
const sensorTypeProfile = () => ({physical_kind: "usb_hid"});
const staleRealBinding = [{
  id: "thermocouple_8ch", enabled: true, role: "thermocouple",
  physical_interface_id: "ethernet:stale", physical_interface_kind: "ethernet",
  physical_fallback: false,
}];
''' + function + r'''
validatePhysicalInterfaceBindings(staleRealBinding, false);
let realBlocked = false;
try { validatePhysicalInterfaceBindings(staleRealBinding, true); }
catch (error) { realBlocked = error.message.includes("物理接口类型与协议不匹配"); }
if (!realBlocked) process.exit(1);
'''
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_real_readiness_failure_can_run_rules_and_langchain_diagnosis(self) -> None:
        source = (DELIVERY_ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")
        events_start = source.index("function buildAgentEvents(")
        events_end = source.index("\nfunction renderAgentGate", events_start)
        events_function = source[events_start:events_end]
        run_start = source.index("async function runAgentDiagnosis(")
        run_end = source.index("\nasync function testSensorConnection", run_start)
        run_function = source[run_start:run_end]
        script = r'''
const hardwareResult = {
  ok: false,
  acquisition_mode: "real",
  interfaces: [{
    id: "thermocouple_8ch", label: "SMRF 八通道热电偶", role: "thermocouple",
    driver: "smrf_hid", endpoint: "SMRFCT08B", enabled: true, ok: false,
    state: "not_connected", message: "未连接真实设备",
    expected_channels: ["温度1"], missing_channels: ["温度1"],
    detected_channels: [], invalid_channels: [],
  }],
  sensors: [{name: "温度1", selected: true, ok: false, state: "no_data", message: "没有数据"}],
};
''' + events_function + r'''
const events = buildAgentEvents(hardwareResult);
if (events.length !== 1 || events[0].interface_id !== "thermocouple_8ch") process.exit(1);
const calls = [];
const state = {
  agentEvents: events, agentRequestId: 0, agentBusy: false, agentJobId: "",
  agentResult: null, hardwareCheck: hardwareResult, accessRole: "local_admin",
};
const controls = {};
const agentApiKeyInput = {value: "test-key"};
const agentModelNameInput = {value: "test-model"};
const autoStatus = {textContent: ""};
const $ = (id) => id === "agentAutoStatus" ? autoStatus : null;
const renderAgentGate = () => true;
const renderHardwareCheckResult = () => {};
const postJson = async (url, payload) => {
  calls.push({url, payload});
  return {job_id: "", local_result: {execution_mode: "local_rules", diagnoses: [{}]}};
};
''' + run_function + r'''
(async () => {
  const result = await runAgentDiagnosis({automatic: false});
  if (calls.length !== 1 || calls[0].url !== "/api/agent/diagnose/start") process.exit(2);
  if (calls[0].payload.hardware_result !== hardwareResult) process.exit(3);
  if (!result || state.agentResult !== result) process.exit(4);
})().catch((error) => { console.error(error); process.exit(5); });
'''
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_m3232_uses_canonical_thin_film_pressure_channel(self) -> None:
        driver = acquisition.M3232PressureDriver("COM8")
        driver.serial = _SerialProbe(b"[[0, 12], [8, 0]]\n")

        sample = driver.read_sample()

        self.assertIn("薄膜压力", sample)
        self.assertNotIn("压力", sample)

    def test_all_five_real_drivers_publish_independently_through_multi_interface(self) -> None:
        configs = [
            {"id": "smrf", "driver": "smrf_hid", "role": "thermocouple"},
            {"id": "plc", "driver": "modbus_tcp", "role": "plc"},
            {"id": "abb", "driver": "abb_robot", "role": "robot"},
            {"id": "uvc", "driver": "uvc_thermal", "role": "thermal_uvc"},
            {"id": "m3232", "driver": "m3232_pressure", "role": "pressure"},
        ]
        assignments = {
            "smrf": ["温度1"],
            "plc": ["温度", "压力", "张力"],
            "abb": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
            "uvc": ["ROI平均温度"],
            "m3232": ["薄膜压力"],
        }
        selected = [name for values in assignments.values() for name in values]
        factories = (
            patch.object(acquisition, "SmrfHidDriver", side_effect=lambda *a, **k: _PayloadDriver({"温度1": 1.0})),
            patch.object(acquisition, "ModbusTcpDriver", side_effect=lambda *a, **k: _PayloadDriver({"温度": 2.0, "压力": 3.0, "张力": 4.0})),
            patch.object(acquisition, "AbbRobotDriver", side_effect=lambda *a, **k: _PayloadDriver({"ABB_X": 5.0, "ABB_Y": 6.0, "ABB_Z": 7.0, "线速度": 8.0})),
            patch.object(acquisition, "UvcThermalDriver", side_effect=lambda *a, **k: _PayloadDriver({"ROI平均温度": 9.0})),
            patch.object(acquisition, "M3232PressureDriver", side_effect=lambda *a, **k: _PayloadDriver({"薄膜压力": 10.0})),
        )
        for factory in factories:
            factory.start()
        driver = acquisition.MultiInterfaceDriver(configs, selected, assignments=assignments)
        try:
            driver.open()
            deadline = time.monotonic() + 0.5
            sample = {}
            while time.monotonic() < deadline:
                sample = driver.read_sample() or {}
                if set(sample) == set(selected):
                    break
                time.sleep(0.005)
        finally:
            driver.close()
            for factory in reversed(factories):
                factory.stop()

        self.assertEqual(set(selected), set(sample))
        self.assertEqual(10.0, sample["薄膜压力"])
        self.assertEqual(3.0, sample["压力"])

    def test_partial_interface_data_does_not_pass_readiness(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            dataset_schema="new_collection_v11_3",
            driver="modbus_tcp",
            selected_sensors=["温度", "压力", "张力"],
            interfaces=[
                {
                    "id": "plc_process",
                    "enabled": True,
                    "role": "plc",
                    "driver": "modbus_tcp",
                    "endpoint": "192.168.125.5:502",
                    "physical_interface_id": "ethernet:test",
                    "physical_verified": True,
                    "source_address": "192.168.125.20",
                    "actual_source_address": "192.168.125.20",
                    "network_interface_name": "Intel Industrial Ethernet",
                }
            ],
            interface_channel_assignments={
                "plc_process": ["温度", "压力", "张力"]
            },
        )
        manager = acquisition.AcquisitionManager()

        with patch.object(acquisition, "MultiInterfaceDriver", _PartialInterfaceDriver):
            result = manager.test_connection(config, timeout_seconds=0.05)

        interface = result["interfaces"][0]
        self.assertFalse(result["ok"])
        self.assertEqual("partial", interface["state"])
        self.assertEqual(["压力", "张力"], interface["missing_channels"])
        self.assertEqual(
            acquisition.READINESS_STAGE_NAMES,
            tuple(stage["name"] for stage in interface["stages"]),
        )

    def test_old_real_config_migrates_to_formal_policy(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            dataset_schema="new_collection_v11_3",
            driver="modbus_tcp",
            selected_sensors=["温度"],
            interfaces=[
                {
                    "id": "plc_process",
                    "enabled": True,
                    "role": "plc",
                    "driver": "modbus_tcp",
                    "endpoint": "192.168.125.5:502",
                    "physical_interface_id": "ethernet:test",
                    "physical_verified": True,
                    "source_address": "192.168.125.20",
                    "actual_source_address": "192.168.125.20",
                    "network_interface_name": "Intel Industrial Ethernet",
                }
            ],
            interface_channel_assignments={"plc_process": ["温度"]},
        )

        self.assertGreaterEqual(config.config_version, 2)
        self.assertEqual("formal", config.capture_policy)

    def test_channel_sample_rejects_non_finite_valid_values(self) -> None:
        sample = acquisition.ChannelSample(
            interface_id="plc_process",
            channel_name="压力",
            value=math.nan,
            quality=acquisition.ChannelQuality.MEASURED_NEW,
            received_wall_time=100.0,
            received_monotonic=20.0,
            source_sequence=7,
        )

        restored = acquisition.ChannelSample.from_dict(sample.to_dict())

        self.assertIsNone(restored.value)
        self.assertEqual(acquisition.ChannelQuality.INVALID, restored.quality)
        self.assertEqual(7, restored.source_sequence)

    def test_frame_assembler_uses_session_origin_without_drift(self) -> None:
        clock = acquisition.VirtualClock()
        assembler = acquisition.FrameAssembler(
            channels=["压力"],
            frame_rate_hz=10.0,
            freshness_by_channel={"压力": 0.25},
            clock=clock.monotonic,
            wall_clock=clock.time,
            wait=clock.wait,
        )
        cache = acquisition.ChannelSampleCache()
        deadlines = []
        for _ in range(6000):
            deadline = assembler.wait_next_deadline()
            deadlines.append(deadline)
            assembler.assemble(cache, deadline)
            clock.advance_processing(0.003)

        self.assertEqual(6000, len(deadlines))
        self.assertAlmostEqual(599.9, deadlines[-1], places=9)
        self.assertAlmostEqual(0.1, deadlines[1001] - deadlines[1000], places=9)

    def test_recent_window_recovers_after_early_missing_row(self) -> None:
        rows = [{"压力": None}] + [{"压力": float(index)} for index in range(24)]

        readiness = acquisition.recent_complete_window(
            rows,
            required_channels=["压力"],
            seq_len=24,
        )

        self.assertTrue(readiness["ready"])
        self.assertEqual(24, readiness["continuous_points"])

    def test_blocking_driver_does_not_stop_other_interface_worker(self) -> None:
        cache = acquisition.ChannelSampleCache()
        blocked = acquisition.DriverWorker(
            "abb_motion", _BlockingDriver(), ["压力"], cache
        )
        fast = acquisition.DriverWorker(
            "plc_process", _FastDriver(), ["压力"], cache
        )
        blocked.start()
        fast.start()
        time.sleep(0.06)

        fast_status = fast.status()
        blocked_status = blocked.status()
        fast_stopped = fast.stop(0.1)
        blocked_stopped_immediately = blocked.stop(0.01)

        self.assertGreater(fast_status["source_sequence"], 0)
        self.assertTrue(blocked_status["running"])
        self.assertTrue(fast_stopped)
        self.assertFalse(blocked_stopped_immediately)
        time.sleep(0.25)

    def test_five_hz_source_is_marked_new_then_held_in_ten_hz_frames(self) -> None:
        clock = acquisition.VirtualClock()
        cache = acquisition.ChannelSampleCache()
        assembler = acquisition.FrameAssembler(
            ["温度1"],
            10.0,
            {"温度1": 0.25},
            clock=clock.monotonic,
            wall_clock=clock.time,
            wait=clock.wait,
        )
        cache.publish(
            acquisition.ChannelSample(
                interface_id="smrf",
                channel_name="温度1",
                value=20.0,
                received_monotonic=0.0,
                received_wall_time=clock.time(),
                source_sequence=1,
            )
        )
        first_deadline = assembler.wait_next_deadline()
        first = assembler.assemble(cache, first_deadline)
        second_deadline = assembler.wait_next_deadline()
        second = assembler.assemble(cache, second_deadline)
        cache.publish(
            acquisition.ChannelSample(
                interface_id="smrf",
                channel_name="温度1",
                value=21.0,
                received_monotonic=0.2,
                received_wall_time=clock.wall_start + 0.2,
                source_sequence=2,
            )
        )
        third_deadline = assembler.wait_next_deadline()
        third = assembler.assemble(cache, third_deadline)

        self.assertEqual("measured_new", first.quality()["温度1"])
        self.assertEqual("held_within_freshness", second.quality()["温度1"])
        self.assertEqual("measured_new", third.quality()["温度1"])

    def test_virtual_clock_produces_exact_fifty_hz_endurance_count(self) -> None:
        clock = acquisition.VirtualClock()
        cache = acquisition.ChannelSampleCache()
        assembler = acquisition.FrameAssembler(
            ["压力"], 50.0, clock=clock.monotonic, wall_clock=clock.time, wait=clock.wait
        )
        last = None
        for _ in range(30000):
            deadline = assembler.wait_next_deadline()
            last = assembler.assemble(cache, deadline)

        self.assertEqual(30000, assembler.next_sequence)
        self.assertEqual(29999, last.capture_sequence)
        self.assertAlmostEqual(599.98, last.target_monotonic, places=9)

    def test_m3232_raw_capture_is_read_only_and_redacted(self) -> None:
        serial = _SerialProbe(b"\x01\x02secret-looking-bytes\x03")
        clock = acquisition.VirtualClock()
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "m3232-capture.json"
            result = acquisition.capture_m3232_raw_serial(
                "COM8",
                output,
                duration_seconds=0.02,
                serial_instance=serial,
                clock=clock.monotonic,
                wall_clock=clock.time,
                wait=clock.wait,
            )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual("hardware_protocol_unverified", result["hardware_protocol_status"])
        self.assertEqual([], payload["commands_sent"])
        self.assertNotIn("password", json.dumps(payload).lower())
        self.assertNotIn("token", json.dumps(payload).lower())
        self.assertGreater(payload["total_bytes"], 0)

    def test_manager_real_mode_is_clock_driven_not_driver_return_driven(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            dataset_schema="new_collection_v11_3",
            driver="modbus_tcp",
            sample_rate_hz=10.0,
            selected_sensors=["压力"],
            interfaces=[
                {
                    "id": "plc_process",
                    "enabled": True,
                    "role": "plc",
                    "driver": "modbus_tcp",
                    "endpoint": "192.168.125.5:502",
                    "physical_interface_id": "ethernet:test",
                    "physical_verified": True,
                }
            ],
            interface_channel_assignments={"plc_process": ["压力"]},
        )
        manager = acquisition.AcquisitionManager()
        fake = _ManagedFakeMultiInterface()
        manager._latest_check_result = {
            "config_fingerprint": acquisition.readiness_config_fingerprint(config),
            "cache_identity": acquisition.readiness_cache_identity(config),
            "readiness_reports": [{
                "interface_id": "plc_process",
                "state": "ready",
                "ready": True,
                "message": "test fixture ready",
            }],
        }

        with patch.object(acquisition, "build_driver", return_value=fake):
            manager.start(config)
            time.sleep(0.34)
            manager.stop_event.set()
            manager.thread.join(1.0)

        self.assertGreaterEqual(manager.total_sample_count, 3)
        self.assertLessEqual(manager.total_sample_count, 5)
        quality = manager.status()["data_quality"]
        self.assertEqual(10.0, quality["target_frame_rate_hz"])
        self.assertEqual(manager.total_sample_count, quality["frame_count"])
        self.assertIn("压力", quality["channels"])

    def test_readiness_report_keeps_all_eight_evidence_layers(self) -> None:
        now = time.time()
        report = acquisition.ReadinessReport(
            interface_id="plc_process",
            state="ready",
            stages=tuple(
                acquisition.ReadinessStage(
                    name=name,
                    state="passed",
                    evidence={"fixture": name},
                    started_at=now,
                    finished_at=now,
                )
                for name in acquisition.READINESS_STAGE_NAMES
            ),
            expected_channels=("温度", "压力", "张力"),
            detected_channels=("温度", "压力", "张力"),
        ).to_dict()

        self.assertTrue(report["ready"])
        self.assertEqual(list(acquisition.READINESS_STAGE_NAMES), [item["name"] for item in report["stages"]])
        self.assertTrue(all("evidence" in item and "remediation" in item for item in report["stages"]))

    def test_industrial_network_path_rejects_tun_and_accepts_direct_nic(self) -> None:
        direct = acquisition.validate_industrial_network_path(
            "192.168.125.5",
            {
                "physical_interface_id": "ethernet:intel",
                "network_interface_name": "Intel I210 Industrial Ethernet",
                "source_address": "192.168.125.20",
                "device_subnet": "192.168.125.0/24",
            },
            target_port=502,
            actual_source_address="192.168.125.20",
            route_interface_id="ethernet:intel",
        )
        tunnel = acquisition.validate_industrial_network_path(
            "192.168.125.5",
            {
                "physical_interface_id": "ethernet:flclash",
                "network_interface_name": "FlClash TUN",
                "source_address": "198.18.0.1",
            },
            target_port=502,
            actual_source_address="198.18.0.1",
            route_interface_id="ethernet:flclash",
        )

        self.assertTrue(direct["ok"])
        self.assertEqual("direct_physical", direct["route_type"])
        self.assertFalse(tunnel["ok"])
        self.assertEqual("network_path_invalid", tunnel["state"])

    def test_formal_start_requires_current_readiness_fingerprint(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["压力"],
            interfaces=[{
                "id": "plc_process", "enabled": True, "role": "plc",
                "driver": "modbus_tcp", "endpoint": "192.168.125.5:502",
                "physical_interface_id": "ethernet:test", "physical_verified": True,
                "source_address": "192.168.125.20",
            }],
            interface_channel_assignments={"plc_process": ["压力"]},
        )

        with self.assertRaisesRegex(RuntimeError, "分层接口检查"):
            acquisition.AcquisitionManager().start(config)

    def test_degraded_engineering_requires_confirmation_and_disables_prediction(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            capture_policy="degraded_engineering",
            degraded_confirmed=False,
            processing_mode="prediction_warning",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["压力"],
            interfaces=[
                {
                    "id": "custom_pressure", "enabled": True, "role": "custom",
                    "driver": "serial_json", "endpoint": "COM4",
                    "physical_interface_id": "serial:COM4", "physical_verified": True,
                },
                {
                    "id": "m3232_pressure", "enabled": False, "role": "pressure",
                    "driver": "m3232_pressure", "endpoint": "COM8",
                },
            ],
            interface_channel_assignments={"custom_pressure": ["压力"]},
        )
        manager = acquisition.AcquisitionManager()
        manager._latest_check_result = {
            "config_fingerprint": acquisition.readiness_config_fingerprint(config),
            "cache_identity": acquisition.readiness_cache_identity(config),
            "readiness_reports": [{
                "interface_id": "custom_pressure", "state": "ready", "ready": True,
            }],
        }

        self.assertEqual("capture_only", config.processing_mode)
        self.assertEqual([], config.prediction_sensors)
        with self.assertRaisesRegex(RuntimeError, "显式确认"):
            manager.start(config)

    def test_plc_semantics_reject_finite_value_outside_reference_tolerance(self) -> None:
        item = {
            "role": "plc", "profile_id": acquisition.DEFAULT_PLC_PROFILE_ID,
            "profile_version": 1, "slave_id": 255, "function_code": 3,
            "register_map": {"温度": 28}, "word_order": "high_low",
            "byte_order": "big", "scales": {"温度": 1.0}, "units": {"温度": "°C"},
            "valid_ranges": {"温度": [-80, 800]},
            "reference_values": {"温度": 100.0},
            "reference_tolerances": {"温度": 2.0},
        }
        evidence = {}

        errors = acquisition._validate_device_semantics(item, {"温度": 23.0}, evidence)

        self.assertTrue(any("字序" in error for error in errors))
        self.assertEqual("high_low", evidence["word_order"])

    def test_abb_first_position_does_not_fabricate_verified_zero_speed(self) -> None:
        driver = acquisition.AbbRobotDriver("192.168.125.1")
        with patch.object(driver, "_fetch_position", side_effect=[(0.0, 0.0, 0.0), (1.0, 0.0, 0.0)]):
            first = driver.read_sample()
            time.sleep(0.002)
            second = driver.read_sample()

        self.assertNotIn("线速度", first)
        self.assertIn("线速度", second)
        self.assertTrue(driver.quality_metadata["speed_source_time_verified"])

    def test_abb_profile_rejects_wrong_mechanical_unit_path(self) -> None:
        item = {
            "role": "robot", "profile_id": acquisition.DEFAULT_ABB_PROFILE_ID,
            "profile_version": 1, "mechanical_unit": "ROB_2",
            "coordinate_system": "Base",
            "robtarget_path": "/rw/motionsystem/mechunits/ROB_1/robtarget?coordinate=Base&json=1",
            "task": "T_ROB1", "module": "MainModule", "speed_variable": "zMovespeed",
            "units": {}, "valid_ranges": {},
        }

        errors = acquisition._validate_device_semantics(item, {"ABB_X": 1.0}, {})

        self.assertTrue(any("机械单元" in error for error in errors))

    def test_smrf_reported_channel_type_conflict_requires_confirmation(self) -> None:
        item = {
            "role": "thermocouple",
            "channel_types": ["K"] * 8,
            "channel_types_confirmed": False,
            "cold_junction_compensation": True,
        }
        evidence = {"reported_channel_types": ["J"] + ["K"] * 7}

        errors = acquisition._validate_device_semantics(item, {}, evidence)

        self.assertTrue(any("必须确认" in error for error in errors))

    def test_uvc_driver_open_without_valid_frame_is_not_ready(self) -> None:
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            dataset_schema="new_collection_v11_3",
            selected_sensors=["ROI平均温度"],
            interfaces=[{
                "id": "uvc_temperature", "enabled": True, "role": "thermal_uvc",
                "driver": "uvc_thermal", "endpoint": "BSV UVC (WinUSB)",
                "physical_interface_id": "uvc:test", "physical_verified": True,
            }],
            interface_channel_assignments={"uvc_temperature": ["ROI平均温度"]},
        )
        manager = acquisition.AcquisitionManager()

        with patch.object(acquisition, "MultiInterfaceDriver", _PartialInterfaceDriver):
            result = manager.test_connection(config, timeout_seconds=0.05)

        interface = result["interfaces"][0]
        self.assertEqual("no_valid_frame", interface["state"])
        self.assertFalse(interface["readiness"]["ready"])

    def test_frontend_lists_only_complete_ready_as_green_state(self) -> None:
        source = (DELIVERY_ROOT / "app" / "ui" / "app.js").read_text(encoding="utf-8")
        for state in (
            "ready", "partial", "no_valid_frame", "stale",
            "network_path_invalid", "hardware_protocol_unverified",
        ):
            self.assertIn(state, source)
        self.assertIn('const completeReady = item.state === "ready"', source)
        self.assertIn("capturePolicySelect", source)
        index = (DELIVERY_ROOT / "app" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn("degradedConfirmationInput", index)

    def test_field_docs_distinguish_m3232_ports_and_require_static_direct_nic(self) -> None:
        m3232 = (DELIVERY_ROOT / "docs" / "M3232压力传感器集成说明.md").read_text(encoding="utf-8")
        readiness = (DELIVERY_ROOT / "docs" / "五设备真实采集预检与工业网卡.md").read_text(encoding="utf-8")

        self.assertIn("蓝牙虚拟 COM", m3232)
        self.assertIn("USB 转串口 COM", m3232)
        self.assertIn("真实 M3232", m3232)
        self.assertIn("静态 IPv4", readiness)
        self.assertIn("FlClash", readiness)
        self.assertIn("TUN", readiness)


if __name__ == "__main__":
    unittest.main()
