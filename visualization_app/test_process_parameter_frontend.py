from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _run_node(script: str) -> dict:
    module_path = ROOT / "static" / "process_parameters.js"
    completed = subprocess.run(
        ["node", "-e", script, str(module_path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


class ProcessParameterFrontendTests(unittest.TestCase):
    def test_partial_result_updates_only_successful_values(self) -> None:
        """Catches failed reads overwriting the operator's existing setpoints."""
        payload = _run_node(
        r"""
const reader = require(process.argv[1]);
const controls = {
  initialForce: {value: "400"},
  placementSpeed: {value: "80"},
  pidAngle: {value: "5"},
  temperatureSetpoint: {value: "360"},
};
const summary = reader.applyResult({
  values: {placement_speed_mm_s: 125, pid_angle_deg: 7},
  parameters: [
    {key: "initial_compaction_force_N", ok: false, message: "未配置"},
    {key: "placement_speed_mm_s", ok: true, value: 125, source: "ABB"},
    {key: "pid_angle_deg", ok: true, value: 7, source: "PLC"},
    {key: "temperature_setpoint_C", ok: false, message: "未配置"},
  ],
}, controls);
console.log(JSON.stringify({controls, summary}));
"""
    )

        self.assertEqual(payload["controls"], {
            "initialForce": {"value": "400"},
            "placementSpeed": {"value": "125"},
            "pidAngle": {"value": "7"},
            "temperatureSetpoint": {"value": "360"},
        })
        self.assertEqual(payload["summary"]["updated"], 2)
        self.assertEqual(payload["summary"]["failed"], 2)
        self.assertIn("保留原值", payload["summary"]["message"])


    def test_invalid_numeric_value_never_replaces_existing_input(self) -> None:
        """Catches malformed device payloads becoming NaN in acquisition config."""
        payload = _run_node(
        r"""
const reader = require(process.argv[1]);
const controls = {
  initialForce: {value: "400"},
  placementSpeed: {value: "80"},
  pidAngle: {value: "5"},
  temperatureSetpoint: {value: "360"},
};
const summary = reader.applyResult({
  values: {placement_speed_mm_s: "not-a-number"},
  parameters: [{key: "placement_speed_mm_s", ok: true, value: "not-a-number"}],
}, controls);
console.log(JSON.stringify({controls, summary}));
"""
    )

        self.assertEqual(payload["controls"]["placementSpeed"]["value"], "80")
        self.assertEqual(payload["summary"]["updated"], 0)
        self.assertEqual(payload["summary"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
