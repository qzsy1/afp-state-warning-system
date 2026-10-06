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
        "thermal_uvc": {"label": "UVC", "physical_kind": "usb_uvc"},
        "pressure": {"label": "M3232", "physical_kind": "serial"},
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


class UsbInterfaceCandidateTests(unittest.TestCase):
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
        self.assertEqual(result["origins"], ["auto"] * 5)
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


if __name__ == "__main__":
    unittest.main()
