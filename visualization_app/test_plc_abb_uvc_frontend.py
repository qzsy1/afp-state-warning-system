from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

from visualization_app.test_usb_interface_candidates import run_candidate_script


APP_JS = Path(__file__).with_name("static") / "app.js"


class PlcMeasurementFrontendTests(unittest.TestCase):
    def test_abb_view_explains_measured_period_age_and_first_point(self):
        source=APP_JS.read_text(encoding="utf-8")
        function=source[source.index("function abbMeasurementText("):source.index("function plcMeasurementText(")]
        script = r'''
const status={interfaces:[{id:"abb",driver:"abb_robot",observed_poll_interval_seconds:.102,
  skipped_poll_cycles:2,abb_evidence:{age_seconds:.04,latest_record:{speed_quality:"unverified_first_sample"}}}]};
console.log(JSON.stringify(abbMeasurementText(status)));
'''
        result=subprocess.run(["node","-e",function+script],check=True,capture_output=True,text=True,encoding="utf-8")
        text=json.loads(result.stdout)
        for expected in ("目标100 ms","102.0 ms","0.040 s","主机单调接收间隔","首点速度未验证","过期留空"):
            self.assertIn(expected,text)

    def test_pressure_source_view_distinguishes_clamp_zero_and_missing(self):
        source = APP_JS.read_text(encoding="utf-8")
        function = source[source.index("function plcMeasurementText("):source.index("function renderAcquisitionStatus(")]
        script = r'''
const record = {register_address:37,words:[0,49184],decoded_value:-2.5,normalized_value:0,
  negative_pressure_clamped:true,source_sequence:3,host_wall_time:100,quality:"measured_new"};
const status = {running:true,interfaces:[{id:"plc",driver:"modbus_tcp",plc_evidence:{channel_records:{"压力":record}},
 observed_poll_interval_seconds:0.102,skipped_poll_cycles:2}],sensors:[{name:"压力",state:"ok"}]};
const clamped = plcMeasurementText(status);
record.decoded_value = record.normalized_value = null; record.negative_pressure_clamped = false;
record.quality = "invalid"; record.error_category = "nonfinite_value";
const missing = plcMeasurementText(status);
console.log(JSON.stringify({clamped,missing}));
'''
        result = subprocess.run(["node", "-e", function + script], check=True, capture_output=True, text=True, encoding="utf-8")
        text = json.loads(result.stdout)
        self.assertIn("未裁剪值 -2.5 N", text["clamped"])
        self.assertIn("规范压力 0 N", text["clamped"])
        self.assertIn("102.0 ms", text["clamped"])
        self.assertIn("规范压力 缺测 N", text["missing"])
        self.assertIn("invalid", text["missing"])


def run_remembered_selection_script(assertions: str) -> list[dict]:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function mergeRememberedInterfaceSelections")
    end = source.index("async function discoverInterfaces", start)
    result = subprocess.run(
        ["node", "-e", f"{source[start:end]}\n{assertions}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def run_hardware_status_script(assertions: str):
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function hardwareStateLabel")
    end = source.index("function clearHardwareRowStates", start)
    result = subprocess.run(
        ["node", "-e", f"{source[start:end]}\n{assertions}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def run_hardware_fingerprint_script(assertions: str):
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function readinessInterfaceFingerprint")
    end = source.index("function hardwareStateLabel", start)
    result = subprocess.run(
        ["node", "-e", f"{source[start:end]}\n{assertions}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def run_real_start_script(assertions: str):
    source = APP_JS.read_text(encoding="utf-8")
    sections = [
        ("function hardwareCheckTimeoutMs", "async function checkSimulationSourceReadiness"),
        ("function acquisitionConfig()", "function renderProcessParameterReadStatus"),
        ("function readinessInterfaceFingerprint", "function hardwareStateLabel"),
        ("async function testSensorConnection", "async function resetAndCheckHardware"),
        ("async function startAcquisition()", "async function waitForHelperFlushComplete"),
        ("async function stopAcquisition()", "function buildSensorChecklist"),
    ]
    functions = "\n".join(
        source[source.rindex(start):source.index(end, source.rindex(start))]
        for start, end in sections
    )
    script = REAL_START_SCRIPT + assertions
    result = subprocess.run(
        ["node"], input=functions + script, check=False,
        capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


REAL_START_SCRIPT = r'''
const window = {};
const controls = new Proxy({
  acquisitionMode:{value:"real"}, processingMode:{value:"capture_only"},
  datasetSchema:{value:"new_collection_v11_3"}, sampleRate:{value:"10"},
  saveRoot:{value:"D:\\AFP_Capture"},
}, {get:(target,key)=>target[key] || {value:"",checked:false}});
const state = {accessRole:"local_admin", acquisitionStatus:{running:false},
  bootstrap:{application:{}}, simulationPackages:[], helperStatus:{},
  mysqlConnectionTests:{}};
let helper = false;
let connectionReady = true;
async function fetch(){return {json:async()=>({data_connection:{ready:connectionReady,reason:'请连接数据'}})};}
let requests = [];
let messages = [];
let clock = 0;
const performance = {now:()=>clock};
let failures = [];
let mutateDuringCheck = false;
let abbEndpoint = "192.168.125.1";
const channels = ["温度","压力","张力","ABB_X","ABB_Y","ABB_Z","线速度"];
const check = {ok:true, readiness_snapshot:{ok:true,checked_at:1000,expires_at:1120}, interfaces:[
  {id:"plc",physical_interface_id:"ethernet:industrial",state:"data_ready",ok:true},
  {id:"abb",physical_interface_id:"ethernet:industrial",state:"data_ready",ok:true},
  {id:"disabled",physical_interface_id:"",state:"disabled",ok:true},
], sensors:channels.map(name=>({name,state:"ok",ok:true}))};
function usesLocalCaptureHelper(){return helper;}
function usesBrowserLocalExport(){return false;}
function isModernSchemaId(){return true;}
function interfaceConfigs(){return {interfaces:[
  {id:"plc",enabled:true,role:"plc",driver:"modbus_tcp",endpoint:"192.168.125.5:502",
    physical_interface_id:"ethernet:industrial",physical_verified:true},
  {id:"abb",enabled:true,role:"robot",driver:"abb_robot",endpoint:abbEndpoint,
    physical_interface_id:"ethernet:industrial",physical_verified:true},
  {id:"disabled",enabled:false,role:"custom",driver:"serial_json",endpoint:"",
    physical_interface_id:"",physical_verified:false},
],assignments:{plc:channels.slice(0,3),abb:channels.slice(3)}};}
function validatePhysicalInterfaceBindings(){}
function selectedLiveSensors(){return channels;}
function selectedPredictionSensors(){return [];}
function selectedModelInputSensors(){return [];}
function $(id){return null;}
function synchronizeAcquisitionButtons(){}
function isPublicPrecomputedSimulationMode(){return false;}
async function validateEnabledMysqlBeforeStart(){}
async function acquireRealControl(){}
function liveEvidenceScopeKey(){return "specimen";}
function resetLiveEvidenceDisplay(){}
function renderHardwareCheckResult(){}
function updateAgentFromHardwareResult(){}
function normalizeHardwareCheckResult(result){return result;}
function validateHelperStartResult(){}
async function waitForEdgeFirstSample(){return {running:true};}
function renderAcquisitionStatus(){}
function configureDataMode(){}
async function loadRealtime(){}
function toast(message){messages.push(message);}
async function postJson(path,payload){
  requests.push({path,payload:structuredClone(payload)});
  return dispatch(path.endsWith("/test"));
}
async function requestLocalHelper(command,payload){
  requests.push({path:command,payload:structuredClone(payload)});
  return dispatch(command==="check_capture");
}

function dispatch(isCheck){
  if (isCheck) {
    if (mutateDuringCheck) abbEndpoint = "192.168.125.2";
    return structuredClone(check);
  }
  if (failures.length) throw new Error(failures.shift());
  return {running:true};
}
'''


class PlcAbbPhysicalBindingFrontendTests(unittest.TestCase):
    def test_browser_check_then_start_keeps_the_same_readiness_configuration(self) -> None:
        from visualization_app.real_acquisition import ReadinessSnapshot

        payload = run_real_start_script(r'''
(async()=>{
  const results=[];
  for (helper of [false,true]) {
    requests=[];state.hardwareCheck=null;
    await testSensorConnection();
    await startAcquisition();
    results.push({helper,requests});
  }
  console.log(JSON.stringify({check,results}));
})().catch(error=>{console.error(error);process.exit(1);});
''')
        for flow in payload["results"]:
            with self.subTest(helper=flow["helper"]):
                check_request, start_request = flow["requests"]
                expected_host = "helper_local" if flow["helper"] else "server"
                self.assertEqual(check_request["payload"].get("execution_host"), expected_host)
                snapshot = ReadinessSnapshot.create(check_request["payload"], payload["check"])
                snapshot.validate(start_request["payload"])
                changed = json.loads(json.dumps(start_request["payload"]))
                changed["interfaces"][1]["endpoint"] = "192.168.125.2"
                with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                    snapshot.validate(changed)

    def test_start_refreshes_expired_missing_and_unversioned_checks_on_both_hosts(self) -> None:
        for helper in (False, True):
            for situation in ("fresh", "expired", "near_expiry", "missing", "unversioned", "no_snapshot"):
                with self.subTest(helper=helper, situation=situation):
                    result = run_real_start_script(
                        f"helper = {json.dumps(helper)};\n"
                        + r'''
(async()=>{
  await testSensorConnection();
  requests=[];
''' + {
                            "fresh": "clock=1000; Date.now=()=>1;",
                            "expired": "clock=121000;",
                            "near_expiry": "clock=115000;",
                            "missing": "state.hardwareCheck=null;",
                            "unversioned": "delete state.hardwareCheck.readinessDeadline;",
                            "no_snapshot": "delete check.readiness_snapshot; await testSensorConnection(); requests=[];",
                        }[situation] + r'''
  await startAcquisition();
  console.log(JSON.stringify({requests,messages,consumed:state.hardwareCheck===null}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
                    )
                    check_path = "check_capture" if helper else "/api/acquisition/test"
                    start_path = "start_capture" if helper else "/api/acquisition/start"
                    expected = [start_path] if not helper or situation == "fresh" else [check_path, start_path]
                    self.assertEqual([item["path"] for item in result["requests"]], expected)
                    self.assertEqual(result["messages"], [])
                    self.assertTrue(result["consumed"])

    def test_stopping_and_starting_requires_new_check(self) -> None:
        for helper in (False, True):
            with self.subTest(helper=helper):
                result = run_real_start_script(
                    f"helper = {json.dumps(helper)};\n" + r'''
(async()=>{
  await testSensorConnection();
  await startAcquisition();
  requests=[];
  await startAcquisition();
  console.log(JSON.stringify({requests,messages}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
                )
                self.assertEqual(len(result["requests"]), 2 if helper else 1)
                self.assertEqual(result["messages"], [])

    def test_normal_stop_save_and_next_layer_reuse_versioned_session_detection(self):
        for helper in (False, True):
            with self.subTest(helper=helper):
                result = run_real_start_script(f"helper={json.dumps(helper)};\n" + r'''
check.readiness_session_id='backend';check.readiness_version=3;
controls.newLayer={value:'0'};
renderAcquisitionStatus=status=>{state.acquisitionStatus=status;};
const originalPost=postJson, originalHelper=requestLocalHelper;
const finished={running:false,capture_saved:true,finalization_complete:true,completed_layers:[1]};
postJson=async(path,payload)=>{
  if(path.endsWith('/stop')){requests.push({path,payload});return finished;}
  return originalPost(path,payload);
};
requestLocalHelper=async(command,payload)=>{
  if(command==='stop_capture'){requests.push({path:command,payload});return finished;}
  return originalHelper(command,payload);
};
function stopLocalSimulationReplay(){}
function renderMysqlStatus(){}
async function saveFinishedCaptureLocally(){}
async function waitForHelperFlushComplete(result){return result;}
async function waitForServerTargetSave(result){return result;}
(async()=>{
  await testSensorConnection();requests=[];
  await startAcquisition();await stopAcquisition();await startAcquisition();
  console.log(JSON.stringify({paths:requests.map(item=>item.path),layer:controls.newLayer.value,ready:!!state.hardwareCheck?.ok,messages}));
})().catch(error=>{console.error(error);process.exit(1);});
''')
                self.assertEqual(result["paths"], ["start_capture", "stop_capture", "start_capture"] if helper
                                 else ["/api/acquisition/start", "/api/acquisition/stop", "/api/acquisition/start"])
                self.assertEqual(result["layer"], "1")
                self.assertTrue(result["ready"])

    def test_delayed_check_response_does_not_extend_reuse_window(self) -> None:
        result = run_hardware_fingerprint_script(r'''
const state = {};
let clock=130000;
const performance={now:()=>clock};
rememberHardwareCheck({ok:true,readiness_snapshot:{ok:true,checked_at:1000,expires_at:1120}}, "config", 0);
console.log(JSON.stringify({current:hardwareCheckIsCurrent("config")}));
''')
        self.assertFalse(result["current"])

    def test_start_rechecks_once_for_readiness_rejections_only(self) -> None:
        errors = (
            "readiness_required: readiness snapshot expired",
            "readiness_required: readiness snapshot fingerprint mismatch",
            "readiness_required: 请先执行当前配置的接口检查",
            "remote_readiness_required",
            "remote_readiness_required: readiness snapshot expired",
            "device_profile_incomplete: fixture",
            "请求超时（30秒）",
            "database_unavailable",
        )
        for helper in (False, True):
            for error in errors:
                for repeat in (False, True):
                    with self.subTest(helper=helper, error=error, repeat=repeat):
                        result = run_real_start_script(
                            f"helper={json.dumps(helper)}; failures={json.dumps([error] * (2 if repeat else 1))};\n"
                            + r'''
(async()=>{
  await testSensorConnection();
  requests=[];
  await startAcquisition();
  console.log(JSON.stringify({requests,messages,busy:state.acquisitionStartBusy}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
                        )
                        recoverable = helper and "readiness_required" in error
                        self.assertEqual(len(result["requests"]), 3 if recoverable else 1)
                        self.assertEqual(len(result["messages"]), int(repeat or not recoverable))
                        self.assertFalse(result["busy"])
                        if recoverable and repeat:
                            self.assertIn("自动重检后仍无法启动", result["messages"][0])

    def test_failed_recheck_or_changed_configuration_blocks_start(self) -> None:
        for helper in (True,):
            for situation in ("expired_failure", "retry_failure", "config_changed"):
                with self.subTest(helper=helper, situation=situation):
                    result = run_real_start_script(
                        f"helper={json.dumps(helper)};\n" + r'''
(async()=>{
  await testSensorConnection();
  requests=[];
''' + {
                            "expired_failure": "clock=121000; check.ok=false;",
                            "retry_failure": 'failures=["readiness_required: readiness snapshot expired"]; check.ok=false;',
                            "config_changed": "clock=121000; mutateDuringCheck=true;",
                        }[situation] + r'''
  await startAcquisition();
  console.log(JSON.stringify({requests,messages}));
})().catch(error=>{console.error(error);process.exit(1);});
'''
                    )
                    starts = [item for item in result["requests"] if item["path"].endswith(("/start", "start_capture"))]
                    self.assertEqual(len(starts), int(situation == "retry_failure"))
                    self.assertEqual(len(result["messages"]), 1)

    def test_local_start_requires_current_connection_and_never_runs_hidden_probe(self):
        result = run_real_start_script(r'''
(async()=>{connectionReady=false;await startAcquisition();
console.log(JSON.stringify({requests,messages}));})().catch(e=>{console.error(e);process.exit(1)});
''')
        self.assertEqual(result['requests'], [])
        self.assertIn('请连接数据', result['messages'][0])

    def test_failed_check_identifies_execution_host_without_claiming_device_failure(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        start = source.index("function normalizeHardwareCheckResult")
        end = source.index("async function checkSimulationSourceReadiness", start)
        script = r'''
const controls = {interfacePanel:{querySelectorAll:()=>[{
  dataset:{interfaceId:"plc"}, querySelector:(selector)=>({
    ".interface-role":{value:"plc"},
    ".interface-endpoint":{value:"192.168.125.5:502"},
    ".interface-enabled":{checked:true},
  }[selector] || null),
}]}};
function selectedAcquisitionChannelsForInterfaces(){return ["温度"];}
function sensorTypeProfile(){return {channels:["温度"]};}
console.log(JSON.stringify(["local_direct","helper_local"].map(executionHost=>
  normalizeHardwareCheckResult({ok:false,error:"请求超时（20秒）"}, {executionHost})
)));
'''
        result = subprocess.run(
            ["node", "-e", source[start:end] + script],
            check=True, capture_output=True, text=True, encoding="utf-8",
        )
        local, helper = json.loads(result.stdout)
        for item in (local, helper):
            self.assertFalse(item["ok"])
            self.assertEqual(item["interfaces"][0]["state"], "hardware_check_timeout")
            self.assertEqual(item["interfaces"][0]["endpoint"], "192.168.125.5:502")
        self.assertIn("本机检查请求", local["interfaces"][0]["message"])
        self.assertNotIn("辅助程序", local["sensors"][0]["message"])
        self.assertIn("辅助程序", helper["interfaces"][0]["message"])
        self.assertEqual(local["execution_host"], "local_direct")

    def test_usb_candidate_keeps_identity_and_driver_facts_for_check_invalidation(self) -> None:
        result = run_candidate_script(r'''
state.usbTopology = {usb_ports:[{id:"usbport:1", user_connectable:true}], devices:[]};
state.physicalInterfaces = [{
  id:"uvc:bsv", kind:"usb_uvc", protocol:"uvc", detected:true,
  parent_port_id:"usbport:1", instance_id:"USB\\VID_2BDF&PID_0102\\A",
  container_id:"container-a", vid:11231, pid:258,
  driver_service:"usbvideo", driver_provider:"Microsoft", driver_version:"10.0.1",
}];
console.log(JSON.stringify(physicalCandidatesForRole("thermal_uvc")[0]));
''')
        self.assertEqual(result["instance_id"], r"USB\VID_2BDF&PID_0102\A")
        self.assertEqual(result["driver_service"], "usbvideo")
        self.assertEqual(result["driver_version"], "10.0.1")

    def test_hardware_fingerprint_expires_for_every_readiness_identity_change(self) -> None:
        result = run_hardware_fingerprint_script(
            r'''
let currentConfig = {
  acquisition_mode:"real", dataset_schema:"new_collection_v11_3",
  selected_sensors:["ABB_X"], interface_channel_assignments:{abb_motion:["ABB_X"]},
  interfaces:[{
    id:"abb_motion", enabled:true, role:"robot", driver:"abb_robot",
    endpoint:"192.168.125.1", baudrate:0, slave_id:0,
    physical_interface_id:"ethernet:industrial", physical_port_id:"pcie:1",
    physical_interface_kind:"ethernet", physical_transport_family:"ethernet",
    credential_identifier:"sha256-a",
    device_identity:{controller_id:"abb-a"},
    driver_state:{available:true, version:"1.0"},
    device_profile:{id:"abb-rws", version:1, approval_status:"approved"},
  }],
};
const state = {
  bootstrap:{application:{version:"2.0.0", build_id:"build-a"}},
  simulationSourcePackageId:"", simulationSourceId:"", publicDemoBundle:null,
  simulationPackages:[], simulationSourceReady:null, publicDemoReady:false,
  simulationDatasetCache:null,
};
const controls = {simulationPackage:{value:""}};
function acquisitionConfig() { return currentConfig; }
const baseline = hardwareConfigFingerprint();
const changes = [];
function mutateInterface(field, value) {
  currentConfig = JSON.parse(JSON.stringify(currentConfig));
  currentConfig.interfaces[0][field] = value;
  changes.push(hardwareConfigFingerprint() !== baseline);
  currentConfig = JSON.parse(JSON.stringify(baseConfig));
}
const baseConfig = JSON.parse(JSON.stringify(currentConfig));
mutateInterface("endpoint", "192.168.125.2");
mutateInterface("physical_port_id", "pcie:2");
mutateInterface("credential_identifier", "sha256-b");
mutateInterface("device_identity", {controller_id:"abb-b"});
mutateInterface("driver_state", {available:true, version:"1.1"});
mutateInterface("device_profile", {id:"abb-rws", version:2, approval_status:"approved"});
state.bootstrap.application.build_id = "build-b";
changes.push(hardwareConfigFingerprint() !== baseline);
console.log(JSON.stringify({changes, baseline}));
'''
        )

        self.assertEqual(result["changes"], [True] * 7)
        self.assertNotIn("robotics", result["baseline"])

    def test_failure_detail_preserves_endpoint_physical_binding_stages_and_raw_code(self) -> None:
        result = run_hardware_status_script(
            r'''
console.log(JSON.stringify({
  known: hardwareInterfaceDetail({
    endpoint:"192.168.125.1", physical_interface_id:"ethernet:industrial",
    final_status:"authentication_failed", stages:{
      authentication:{state:"failed"}, protocol:{state:"not_reached"},
      network_path:{state:"passed"}, usb_binding:{state:"not_applicable"},
    },
  }),
  unknown: hardwareStateLabel("vendor_specific_failure"),
}));
'''
        )

        self.assertIn("192.168.125.1", result["known"])
        self.assertIn("ethernet:industrial", result["known"])
        self.assertIn("authentication_failed", result["known"])
        self.assertIn("authentication:failed", result["known"])
        self.assertIn("protocol:not_reached", result["known"])
        self.assertIn("vendor_specific_failure", result["unknown"])

    def test_auto_assignment_preserves_plc_and_abb_protocol_endpoints(self) -> None:
        result = run_candidate_script(
            r"""
state.physicalInterfaces = [
  {id:"ethernet:industrial", kind:"ethernet", endpoint:"以太网", addresses:["192.168.125.209"], detected:true, compatible:true, interface_type:"physical", target_subnet_match:true, best_route:true, auto_eligible:true},
];
const configs = [
  {id:"plc", role:"plc", endpoint:"192.168.125.5:502"},
  {id:"abb", role:"robot", endpoint:"192.168.125.1"},
];
autoAssignPhysicalInterfaces(configs);
console.log(JSON.stringify(configs));
"""
        )

        self.assertEqual(result[0]["endpoint"], "192.168.125.5:502")
        self.assertEqual(result[1]["endpoint"], "192.168.125.1")
        self.assertEqual(
            [item["physical_interface_id"] for item in result],
            ["ethernet:industrial", "ethernet:industrial"],
        )

    def test_only_serial_physical_selection_can_supply_protocol_endpoint(self) -> None:
        result = run_candidate_script(
            r"""
console.log(JSON.stringify({
  plc: physicalSelectionProvidesProtocolEndpoint("plc", {kind:"ethernet"}),
  abb: physicalSelectionProvidesProtocolEndpoint("robot", {kind:"ethernet"}),
  uvc: physicalSelectionProvidesProtocolEndpoint("thermal_uvc", {kind:"usb_uvc"}),
  pressure: physicalSelectionProvidesProtocolEndpoint("pressure", {kind:"serial"}),
  strain: physicalSelectionProvidesProtocolEndpoint("esr1001_strain", {kind:"com"}),
}));
"""
        )

        self.assertEqual(
            result,
            {"plc": False, "abb": False, "uvc": False, "pressure": True, "strain": True},
        )

    def test_rediscovery_mode_round_trip_keeps_protocol_endpoints_and_saved_bindings(self) -> None:
        result = run_remembered_selection_script(
            r"""
const state = {interfaceCatalog:[]};
const controls = {acquisitionMode:{value:"real"}, interfacePanel:{querySelectorAll(){return [{}];}}};
function interfaceConfigs() { return {interfaces:[
  {id:"plc_process", role:"plc", endpoint:"192.168.125.5:502", physical_interface_id:"ethernet:industrial", physical_interface_kind:"ethernet", selection_origin:"manual"},
  {id:"uvc_temperature", role:"thermal_uvc", endpoint:"BSV UVC (WinUSB)", physical_port_id:"usbport:external:1", physical_interface_id:"uvc:2bdf:0102", physical_interface_kind:"usb_uvc", selection_origin:"manual"},
  {id:"abb_motion", role:"robot", endpoint:"192.168.125.1", physical_interface_id:"ethernet:industrial", physical_interface_kind:"ethernet", selection_origin:"manual"},
]}; }
const merged = mergeRememberedInterfaceSelections([
  {id:"plc_process", role:"plc", endpoint:"192.168.125.5:502"},
  {id:"uvc_temperature", role:"thermal_uvc", endpoint:"BSV UVC (WinUSB)"},
  {id:"abb_motion", role:"robot", endpoint:"192.168.125.1"},
]);
console.log(JSON.stringify(merged));
"""
        )

        self.assertEqual(
            [item["endpoint"] for item in result],
            ["192.168.125.5:502", "BSV UVC (WinUSB)", "192.168.125.1"],
        )
        self.assertEqual(result[0]["physical_interface_id"], "ethernet:industrial")
        self.assertEqual(result[1]["physical_port_id"], "usbport:external:1")
        self.assertEqual(result[2]["physical_interface_id"], "ethernet:industrial")

    def test_auto_assignment_rejects_apipa_wlan_tun_and_selects_direct_industrial_route(self) -> None:
        result = run_candidate_script(
            r"""
state.physicalInterfaces = [
  {id:"ethernet:bluetooth", kind:"ethernet", name:"蓝牙网络连接", endpoint:"蓝牙网络连接", addresses:["169.254.123.214"], detected:true, interface_type:"physical"},
  {id:"ethernet:wlan", kind:"ethernet", name:"WLAN", endpoint:"WLAN", addresses:["192.168.15.162"], detected:true, interface_type:"wireless"},
  {id:"ethernet:tun", kind:"ethernet", name:"FlClash", endpoint:"FlClash", addresses:["198.18.0.1"], detected:true, interface_type:"virtual"},
  {id:"ethernet:industrial", kind:"ethernet", name:"以太网", endpoint:"以太网", addresses:["192.168.125.209"], detected:true, interface_type:"physical", target_subnet_match:true, best_route:true, route_source_address:"192.168.125.209"},
];
const configs = [{id:"plc", role:"plc", endpoint:"192.168.125.5:502"}];
autoAssignPhysicalInterfaces(configs);
console.log(JSON.stringify(configs[0]));
"""
        )

        self.assertEqual(result["physical_interface_id"], "ethernet:industrial")
        self.assertEqual(result["endpoint"], "192.168.125.5:502")


if __name__ == "__main__":
    unittest.main()
