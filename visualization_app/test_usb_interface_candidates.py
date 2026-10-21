from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


APP_JS = Path(__file__).with_name("static") / "app.js"


def run_candidate_script(assertions: str) -> dict:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("const USB_SENSOR_ROLES")
    end = source.index("function refreshPhysicalInterfaceOptions", start)
    profiles = {
        "thermocouple": {"label": "八通道热电偶", "physical_kind": "usb_hid"},
        "thermocouple64": {"label": "慧谱TCP64", "physical_kind": "serial"},
        "thermal_uvc": {"label": "UVC", "physical_kind": "usb_uvc"},
        "pressure": {"label": "M3232", "physical_kind": "serial"},
        "esr1001_strain": {"label": "ESR1001-A四通道应变", "physical_kind": "serial"},
        "plc": {"label": "PLC", "physical_kind": "ethernet"},
        "robot": {"label": "ABB", "physical_kind": "ethernet"},
    }
    script = f"""
const state = {{physicalInterfaces: [], usbTopology: null}};
const profiles = {json.dumps(profiles, ensure_ascii=False)};
function sensorTypeProfile(role) {{ return profiles[role] || {{}}; }}
{source[start:end]}
{assertions}
"""
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def run_logical_interface_script(assertions: str) -> dict:
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function uniqueLogicalInterfaceConfigs")
    end = source.index("function renderInterfacePanel", start)
    result = subprocess.run(
        ["node", "-e", f"{source[start:end]}\n{assertions}"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


class UsbInterfaceCandidateTests(unittest.TestCase):
    def test_saved_serial_endpoint_can_recover_an_unlocated_candidate(self):
        result = run_candidate_script(r'''
state.usbTopology={usb_ports:[{id:"usb:9",state:"occupied",user_connectable:true}],devices:[]};
state.physicalInterfaces=[{id:"serial:COM5",kind:"serial",endpoint:"COM5",vid:6790,pid:29987,detected:true}];
const candidates=physicalCandidatesForRole("esr1001_strain");
console.log(JSON.stringify({exact:savedSerialCandidate(candidates,"esr1001_strain","COM5"),
  offline:savedSerialCandidate(candidates,"esr1001_strain","COM7"),
  empty:candidates.find(c=>c.id==="usb:9")}));
''')
        self.assertEqual(result["exact"]["physical_interface_id"], "serial:COM5")
        self.assertTrue(result["exact"]["detected"])
        self.assertIsNone(result["offline"])
        self.assertFalse(result["empty"]["compatible"])

    def test_tc64_check_budget_covers_driver_deadline_and_explicit_block_mode(self):
        source = APP_JS.read_text(encoding="utf-8")
        start = source.index("function hardwareCheckTimeoutMs")
        end = source.index("async function checkSimulationSourceReadiness", start)
        script = source[start:end] + r'''
const tc={enabled:true,driver:"huipu_tc64_modbus_rtu",baudrate:115200,timeout:.5};
console.log(JSON.stringify({normal:hardwareCheckTimeoutMs({interfaces:[tc]}),
  block:hardwareCheckTimeoutMs({interfaces:[{...tc,read_profile:"full_record_block"}]}),
  slow:hardwareCheckTimeoutMs({interfaces:[{...tc,baudrate:2400,timeout:2}]}),
  disabled:hardwareCheckTimeoutMs({interfaces:[{...tc,enabled:false}]}),
  other:hardwareCheckTimeoutMs({interfaces:[{driver:"modbus_tcp"}]})}));
'''
        result = subprocess.run(["node","-e",script],check=True,capture_output=True,text=True)
        budgets = json.loads(result.stdout)
        self.assertGreater(budgets["normal"], 104000)
        self.assertGreater(budgets["normal"], budgets["block"])
        self.assertGreater(budgets["slow"], 400000)
        self.assertEqual(budgets["disabled"], 20000)
        self.assertEqual(budgets["other"], 20000)

    def test_browser_plan_save_keeps_tc64_settings_without_ready_flags(self):
        source = APP_JS.read_text(encoding="utf-8")
        start = source.index("function saveDatasetInterfacePreferences")
        end = source.index("function queueSharedDatasetInterfacePreferences", start)
        script = r'''
const controls = {acquisitionMode:{value:"real"},processingMode:{value:"capture_only"}};
const state = {activeInterfaceSchema:"new_collection_v13_0"};
const original = {id:"tc",enabled:false,role:"thermocouple64",endpoint:"COM7",read_profile:"full_record_block",timeout:.28,
 channel_settings:{confirmed:true,channels:{"温度64":{unit:0,input_type:0,switch:0}}},device_identity:{serial_number:"TC64"},freshness_seconds:5,reconnect_window_seconds:60,
 physical_verified:true,protocol_verified:true,selection_origin:"manual"};
function interfaceConfigs(){return {interfaces:[original]};}
let stored;
function queueSharedDatasetInterfacePreferences(value){stored=value;}
function datasetInterfacePreferenceKey(){return "test";}
const localStorage={setItem(key,value){}};
''' + source[start:end] + '\nsaveDatasetInterfacePreferences(); console.log(JSON.stringify(stored));'
        completed = subprocess.run(["node", "-e", script],check=True,capture_output=True,text=True,encoding="utf-8")
        item = json.loads(completed.stdout)["plans"]["new_collection_v13_0"]["interfaces"][0]
        self.assertEqual(item["read_profile"], "full_record_block")
        self.assertEqual(item["timeout"], .28)
        self.assertEqual(item["device_identity"], {"serial_number": "TC64"})
        self.assertTrue(item["channel_settings"]["confirmed"])
        self.assertFalse(item["enabled"])
        self.assertNotIn("physical_verified", item)
        self.assertNotIn("protocol_verified", item)

    def test_tc64_never_guesses_and_preserves_manual_disabled_bindings(self):
        result = run_candidate_script(r'''
const ports = [6,7].map(n => ({id:`serial:COM${n}`,kind:"serial",protocol:"serial",endpoint:`COM${n}`,detected:true,vid:6790,pid:29987,device_identity:{vid:6790,pid:29987,serial_number:""}}));
state.physicalInterfaces = ports;
const unknown = {id:"tc",role:"thermocouple64",endpoint:"",enabled:false};
autoAssignPhysicalInterfaces([unknown]);
state.physicalInterfaces.reverse();
autoAssignPhysicalInterfaces([unknown]);
const offline = {id:"tc",role:"thermocouple64",endpoint:"COM2",physical_interface_id:"serial:COM2",selection_origin:"manual",enabled:false};
autoAssignPhysicalInterfaces([offline]);
const manual = {id:"tc",role:"thermocouple64",endpoint:"COM7",physical_interface_id:"serial:COM7",selection_origin:"manual",enabled:false};
autoAssignPhysicalInterfaces([manual]);
const occupied = {...manual};
autoAssignPhysicalInterfaces([occupied,{id:"strain",role:"esr1001_strain",physical_interface_id:"serial:COM7",endpoint:"COM7"}]);
console.log(JSON.stringify({unknown,offline,manual,occupied}));
''')
        self.assertEqual(result["unknown"]["endpoint"], "")
        self.assertFalse(result["unknown"]["enabled"])
        self.assertEqual(result["offline"]["endpoint"], "COM2")
        self.assertFalse(result["offline"]["physical_verified"])
        self.assertEqual(result["manual"]["endpoint"], "COM7")
        self.assertFalse(result["manual"]["enabled"])
        self.assertFalse(result["occupied"]["physical_verified"])

    def test_tc64_stable_identity_restores_endpoint_and_revokes_evidence(self):
        result = run_candidate_script(r'''
const identity = {vid:1027,pid:24577,serial_number:"TC64",location:"old"};
state.physicalInterfaces = [{id:"serial:COM9",kind:"serial",endpoint:"COM9",detected:true,vid:1027,pid:24577,device_identity:{...identity,location:"new"}}];
const tc = {role:"thermocouple64",endpoint:"COM6",physical_interface_id:"serial:COM6",enabled:false,device_identity:identity,protocol_verified:true,channel_settings:{confirmed:true}};
autoAssignPhysicalInterfaces([tc]);
console.log(JSON.stringify(tc));
''')
        self.assertEqual(result["endpoint"], "COM9")
        self.assertFalse(result["protocol_verified"])
        self.assertFalse(result["enabled"])
        self.assertEqual(result["channel_settings"], {})

    def test_tc64_lists_usb_serial_and_native_serial_without_hid(self) -> None:
        result = run_candidate_script(r'''
state.physicalInterfaces = [
  {id:"serial:COM3", kind:"serial", endpoint:"COM3", vid:6790, pid:29987, detected:true},
  {id:"serial:COM1", kind:"serial", endpoint:"COM1", detected:true},
  {id:"hid:other", kind:"usb_hid", endpoint:"HID", detected:true},
];
console.log(JSON.stringify(physicalCandidatesForRole("thermocouple64").filter(x=>x.compatible).map(x=>x.endpoint)));
''')
        self.assertEqual(set(result), {"COM3", "COM1"})

    def test_unconfirmed_usb_hub_remains_visible_without_dock_label(self) -> None:
        result = run_candidate_script(
            r"""
state.usbTopology = normalizeUsbTopology({
  schema_version: 1,
  docks: [{id:"legacy-dock", label:"拓展坞 1"}],
  usb_ports: [{id:"hub:port:1", owner_kind:"dock", dock_id:"legacy-dock", label:"拓展坞 1 · USB3-1", connector_form_factor:"type_a", state:"empty", user_connectable:true}],
  devices: [],
});
const byRole = Object.fromEntries(["thermocouple", "thermal_uvc", "pressure"].map((role) => [
  role,
  physicalCandidatesForRole(role).filter((item) => item.transport_family === "usb"),
]));
console.log(JSON.stringify({
  counts: Object.fromEntries(Object.entries(byRole).map(([role, items]) => [role, items.length])),
  owner: byRole.thermocouple[0]?.owner_kind,
  group: byRole.thermocouple[0]?.group_label,
  topologyDocks: state.usbTopology.docks.length,
}));
"""
        )
        self.assertEqual(result["counts"], {"thermocouple": 1, "thermal_uvc": 1, "pressure": 1})
        self.assertEqual(result["owner"], "hub")
        self.assertIn("USB 扩展设备", result["group"])
        self.assertNotIn("拓展坞", result["group"])
        self.assertEqual(result["topologyDocks"], 0)

    def test_empty_operator_port_remains_manually_selectable(self) -> None:
        source = APP_JS.read_text(encoding="utf-8")
        start = source.index("const USB_SENSOR_ROLES")
        end = source.index("function itemEnabledForSimulation", start)
        body = source[start:end]
        script = r'''
const profiles = {thermocouple:{label:"八通道热电偶", physical_kind:"usb_hid", protocol:"smrf_hid"}};
const state = {
  physicalInterfaces: [],
  usbTopology: {usb_ports:[{id:"dock:empty", owner_kind:"dock", dock_id:"d1", label:"拓展坞 1 · USB3-1", state:"empty", user_connectable:true}], devices:[]},
};
const controls = {acquisitionMode:{value:"real"}};
function sensorTypeProfile(role) { return profiles[role] || {}; }
function option(value, textContent) { return {value, textContent, dataset:{}, disabled:false}; }
function groupOptions(nodes) { return nodes.flatMap((node) => node.children || [node]); }
const document = {createElement(tag) {
  if (tag === "optgroup") return {label:"", children:[], append(node){this.children.push(node);}};
  return {className:"", textContent:"", classList:{toggle(){},remove(){} }};
}};
const select = {
  children:[], disabled:false, title:"", _value:"",
  replaceChildren(...nodes){this.children=[...nodes]; this._value="";},
  append(node){this.children.push(node);},
  set value(value){this._value=String(value);}, get value(){return this._value;},
  get selectedOptions(){return groupOptions(this.children).filter((node) => String(node.value) === this._value);},
};
const enabled = {checked:true, disabled:false};
const warning = {textContent:"", classList:{toggle(){},remove(){}}};
const row = {
  dataset:{physicalPortId:"", physicalKind:"", endpoint:""},
  querySelector(selector) {
    if (selector === ".interface-physical") return select;
    if (selector === ".interface-role") return {value:"thermocouple"};
    if (selector === ".interface-enabled") return enabled;
    if (selector === ".interface-physical-warning") return warning;
    return null;
  },
  append(){},
};
''' + body + r'''
refreshPhysicalInterfaceOptions(row, "dock:empty");
const selected = select.selectedOptions[0];
if (!selected || selected.disabled || select.disabled || enabled.disabled) process.exit(1);
if (selected.dataset.endpointPresent !== "false") process.exit(2);
if (!warning.textContent.includes("尚未检测到兼容设备")) process.exit(3);
'''
        completed = subprocess.run(["node", "-e", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_shared_ethernet_endpoint_keeps_plc_and_abb_logical_interfaces(self) -> None:
        result = run_logical_interface_script(
            r"""
const sharedEndpoint = "ethernet:asix";
const configs = [
  {id:"thermocouple_8ch", role:"thermocouple", endpoint:"SMRFCT08B"},
  {id:"plc_process", role:"plc", endpoint:sharedEndpoint},
  {id:"uvc_temperature", role:"thermal_uvc", endpoint:"BSV UVC"},
  {id:"abb_motion", role:"robot", endpoint:sharedEndpoint},
  {id:"m3232_pressure", role:"pressure", endpoint:"COM8"},
];
const normalized = uniqueLogicalInterfaceConfigs(configs);
console.log(JSON.stringify({
  ids: normalized.map((item) => item.id),
  shared: normalized.filter((item) => item.endpoint === sharedEndpoint).map((item) => item.id),
}));
"""
        )
        self.assertEqual(len(result["ids"]), 5)
        self.assertEqual(result["shared"], ["plc_process", "abb_motion"])

    def test_type_c_and_dock_upstream_are_excluded_from_usb_a_sensor_pool(self) -> None:
        result = run_candidate_script(
            r"""
state.usbTopology = {
  usb_ports: [
    {id:"host:a1", owner_kind:"host", label:"电脑本机 · USB-A-1", connector_form_factor:"type_a", state:"empty", user_connectable:true},
    {id:"host:a2", owner_kind:"host", label:"电脑本机 · USB-A-2", connector_form_factor:"type_a", state:"empty", user_connectable:true},
    {id:"host:c1", owner_kind:"host", label:"电脑本机 · Type-C-1", connector_form_factor:"type_c", internal_function:"dock_upstream", state:"occupied", user_connectable:true},
    {id:"host:c2", owner_kind:"host", label:"电脑本机 · Type-C-2", connector_form_factor:"type_c", state:"occupied", user_connectable:true},
    {id:"dock:1", owner_kind:"dock", dock_id:"d1", label:"拓展坞 1 · USB3-1", connector_form_factor:"type_a", state:"empty", user_connectable:true},
  ],
  devices: [],
};
const roles = ["thermocouple", "thermal_uvc", "pressure"];
const candidateIds = Object.fromEntries(roles.map((role) => [role,
  physicalCandidatesForRole(role).filter((item) => item.transport_family === "usb").map((item) => item.id).sort()
]));
const groups = physicalCandidatesForRole("thermocouple").map((item) => item.group_label);
console.log(JSON.stringify({candidateIds, groups}));
"""
        )
        expected = ["dock:1", "host:a1", "host:a2"]
        self.assertEqual(result["candidateIds"]["thermocouple"], expected)
        self.assertEqual(result["candidateIds"]["thermal_uvc"], expected)
        self.assertEqual(result["candidateIds"]["pressure"], expected)
        self.assertIn("电脑本机 USB-A 接口", result["groups"])

    def test_three_usb_sensor_roles_receive_same_physical_usb_pool(self) -> None:
        result = run_candidate_script(
            r"""
state.usbTopology = {
  usb_ports: [
    {id:"host:1", owner_kind:"host", label:"电脑本机 · USB3-1", state:"empty", user_connectable:true},
    {id:"dock:1", owner_kind:"dock", dock_id:"d1", label:"拓展坞 1 · USB3-1", state:"occupied", user_connectable:true},
    {id:"dock:2", owner_kind:"dock", dock_id:"d1", label:"拓展坞 1 · USB3-2", state:"occupied", user_connectable:true},
    {id:"dock:3", owner_kind:"dock", dock_id:"d1", label:"拓展坞 1 · USB3-3", state:"empty", user_connectable:true},
  ],
  devices: [],
};
state.physicalInterfaces = [
  {id:"hid:smrf", kind:"usb_hid", transport_family:"usb", parent_port_id:"dock:1", label:"SMRF", detected:true},
  {id:"serial:COM8", kind:"serial", transport_family:"usb", parent_port_id:"dock:2", endpoint:"COM8", label:"USB Serial COM8", detected:true},
  {id:"serial:COM1", kind:"serial", transport_family:"serial_native", endpoint:"COM1", label:"COM1", detected:true},
];
const roles = ["thermocouple", "thermal_uvc", "pressure"];
const usbIds = Object.fromEntries(roles.map((role) => [role,
  physicalCandidatesForRole(role).filter((item) => item.transport_family === "usb").map((item) => item.id).sort()
]));
const pressureGroups = physicalCandidatesForRole("pressure").map((item) => item.group_label);
console.log(JSON.stringify({usbIds, pressureGroups}));
"""
        )
        expected = sorted(["host:1", "dock:1", "dock:2", "dock:3"])
        self.assertEqual(result["usbIds"]["thermocouple"], expected)
        self.assertEqual(result["usbIds"]["thermal_uvc"], expected)
        self.assertEqual(result["usbIds"]["pressure"], expected)
        self.assertIn("主机原生串口", result["pressureGroups"])

    def test_strain_receives_usb_ports_from_host_and_dock_plus_native_serial(self) -> None:
        result = run_candidate_script(
            r"""
state.usbTopology = {
  usb_ports: [
    {id:"host:1", owner_kind:"host", label:"电脑本机 · USB3-1", state:"empty", user_connectable:true},
    {id:"dock:1", owner_kind:"dock", dock_id:"d1", label:"拓展坞 1 · USB3-1", state:"occupied", user_connectable:true},
    {id:"dock:2", owner_kind:"dock", dock_id:"d1", label:"拓展坞 1 · USB3-2", state:"empty", user_connectable:true},
  ],
  devices: [],
};
state.physicalInterfaces = [
  {id:"serial:COM8", kind:"serial", transport_family:"usb", parent_port_id:"dock:1", endpoint:"COM8", label:"USB Serial COM8", detected:true},
  {id:"serial:COM1", kind:"serial", transport_family:"serial_native", endpoint:"COM1", label:"COM1", detected:true},
];
const candidates = physicalCandidatesForRole("esr1001_strain");
const dockCandidate = candidates.find((item) => item.id === "dock:1");
console.log(JSON.stringify({
  usbIds: candidates.filter((item) => item.transport_family === "usb").map((item) => item.id).sort(),
  groups: candidates.map((item) => item.group_label),
  dockCandidate: {
    physicalInterfaceId: dockCandidate?.physical_interface_id,
    endpoint: dockCandidate?.endpoint,
    compatible: dockCandidate?.compatible,
    detected: dockCandidate?.detected,
  },
}));
"""
        )
        self.assertEqual(result["usbIds"], ["dock:1", "dock:2", "host:1"])
        self.assertIn("拓展坞 1 USB 接口", result["groups"])
        self.assertIn("主机原生串口", result["groups"])
        self.assertEqual(
            result["dockCandidate"],
            {
                "physicalInterfaceId": "serial:COM8",
                "endpoint": "COM8",
                "compatible": True,
                "detected": True,
            },
        )

    def test_default_assignment_uses_distinct_usb_ports_and_shared_ethernet(self) -> None:
        result = run_candidate_script(
            r"""
state.usbTopology = {
  usb_ports: [1,2,3].map((n) => ({id:`dock:${n}`, owner_kind:"dock", dock_id:"d1", label:`拓展坞 1 · USB3-${n}`, state:"empty", user_connectable:true})),
  devices: [],
};
state.physicalInterfaces = [
  {id:"ethernet:lan", kind:"ethernet", transport_family:"ethernet", endpoint:"以太网", detected:true},
  {id:"serial:COM1", kind:"serial", transport_family:"serial_native", endpoint:"COM1", detected:true},
];
const configs = [
  {id:"tc", role:"thermocouple"}, {id:"uvc", role:"thermal_uvc"}, {id:"p", role:"pressure"},
  {id:"plc", role:"plc"}, {id:"abb", role:"robot"},
];
autoAssignPhysicalInterfaces(configs);
console.log(JSON.stringify({
  usb: configs.slice(0,3).map((item) => item.physical_port_id),
  network: configs.slice(3).map((item) => item.physical_interface_id),
  origins: configs.map((item) => item.selection_origin),
  verified: configs.slice(0,3).map((item) => item.physical_verified),
}));
"""
        )
        self.assertEqual(len(set(result["usb"])), 3)
        self.assertEqual(result["network"], ["ethernet:lan", "ethernet:lan"])
        self.assertEqual(result["origins"], ["auto", "auto", None, "auto", "auto"])
        self.assertIsNone(result["usb"][2])
        self.assertEqual(result["verified"], [False, False, False])

    def test_missing_manual_port_is_preserved_instead_of_migrated(self) -> None:
        result = run_candidate_script(
            r"""
state.usbTopology = {usb_ports:[{id:"dock:new", owner_kind:"dock", dock_id:"d1", label:"拓展坞 1 · USB3-1", state:"empty", user_connectable:true}], devices:[]};
const configs = [{id:"tc", role:"thermocouple", physical_port_id:"dock:manual", physical_interface_id:"", selection_origin:"manual"}];
autoAssignPhysicalInterfaces(configs);
console.log(JSON.stringify(configs[0]));
"""
        )
        self.assertEqual(result["physical_port_id"], "dock:manual")
        self.assertEqual(result["selection_origin"], "manual")

    def test_same_hub_esr_endpoint_refresh_and_uvc_stable_identity_relocation(self) -> None:
        result = run_candidate_script(
            r"""
state.usbTopology = {usb_ports:[
  {id:"hub:port:3", owner_kind:"hub", hub_id:"hub:1", label:"USB扩展设备 · USB-3", state:"occupied", user_connectable:true},
  {id:"hub:port:4", owner_kind:"hub", hub_id:"hub:1", label:"USB扩展设备 · USB-4", state:"occupied", user_connectable:true},
], devices:[]};
state.physicalInterfaces = [
  {id:"serial:COM10", kind:"serial", transport_family:"usb", parent_port_id:"hub:port:3", endpoint:"COM10", detected:true,
   device_identity:{vid:6790,pid:29987,serial_number:"",location:"1-9.3"}},
  {id:"uvc:bsv:current", kind:"usb_uvc", transport_family:"usb", parent_port_id:"hub:port:4", endpoint:"UVC Camera", detected:true,
   device_identity:{vid:11231,pid:258,container_id:"{stable-container}",instance_id:"current"}},
];
const configs = [
  {id:"strain",role:"esr1001_strain",driver:"esr1001_usb_ttl",endpoint:"COM5",physical_port_id:"hub:port:3",
   physical_interface_id:"",selection_origin:"saved",protocol_verified:true,device_identity:{vid:6790,pid:29987,location:"1-9"}},
  {id:"uvc",role:"thermal_uvc",driver:"uvc_thermal",endpoint:"BSV UVC",physical_port_id:"hub:old:4",
   physical_interface_id:"uvc:bsv:old",selection_origin:"saved",protocol_verified:true,
   device_identity:{vid:11231,pid:258,container_id:"{stable-container}",instance_id:"old"}},
];
autoAssignPhysicalInterfaces(configs);
console.log(JSON.stringify(configs));
"""
        )
        esr, uvc = result
        self.assertEqual(esr["endpoint"], "COM10")
        self.assertEqual(esr["physical_interface_id"], "serial:COM10")
        self.assertEqual(esr["physical_port_id"], "hub:port:3")
        self.assertFalse(esr["protocol_verified"])
        self.assertEqual(uvc["physical_interface_id"], "uvc:bsv:current")
        self.assertEqual(uvc["physical_port_id"], "hub:port:4")
        self.assertFalse(uvc["protocol_verified"])


if __name__ == "__main__":
    unittest.main()
