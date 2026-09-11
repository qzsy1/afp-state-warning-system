from __future__ import annotations

import json
import unittest

from interface_monitor_demo.agent_flow import AgentGateError, run_diagnosis
from interface_monitor_demo.interface_catalog import build_interface_catalog
from interface_monitor_demo.monitor import InterfaceMonitor


class AgentFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = build_interface_catalog()
        self.event = InterfaceMonitor(self.catalog).apply_scenario(
            "m3232_pressure", "parse_error"
        )

    def test_gate_rejects_missing_api_key_presence(self) -> None:
        with self.assertRaisesRegex(AgentGateError, "API Key"):
            run_diagnosis(
                self.event,
                self.catalog,
                api_key_present=False,
                model_name="local-demo-model",
            )

    def test_gate_rejects_missing_model_name(self) -> None:
        with self.assertRaisesRegex(AgentGateError, "模型名称"):
            run_diagnosis(
                self.event,
                self.catalog,
                api_key_present=True,
                model_name="  ",
            )

    def test_gate_rejects_missing_fault_event(self) -> None:
        with self.assertRaisesRegex(AgentGateError, "异常事件"):
            run_diagnosis(
                None,
                self.catalog,
                api_key_present=True,
                model_name="local-demo-model",
            )

    def test_flow_returns_visible_steps_in_execution_order(self) -> None:
        result = run_diagnosis(
            self.event,
            self.catalog,
            api_key_present=True,
            model_name="local-demo-model",
        )

        self.assertEqual(
            [step["id"] for step in result["trace"]],
            [
                "event_received",
                "gate_checked",
                "get_interface_config",
                "inspect_latest_observation",
                "lookup_fault_rule",
                "compose_diagnostic_report",
                "complete",
            ],
        )
        self.assertTrue(all(step["status"] == "complete" for step in result["trace"]))

    def test_film_pressure_diagnosis_does_not_claim_plc_pressure(self) -> None:
        result = run_diagnosis(
            self.event,
            self.catalog,
            api_key_present=True,
            model_name="local-demo-model",
        )
        diagnosis = result["diagnosis"]

        self.assertEqual(diagnosis["interface_id"], "m3232_pressure")
        self.assertEqual(diagnosis["sensor_name"], "M3232薄膜压力")
        self.assertEqual(diagnosis["channels"], ["薄膜压力"])
        self.assertNotIn("PLC压力", diagnosis["summary"])
        self.assertEqual(diagnosis["confidence_label"], "规则高度匹配")
        self.assertTrue(result["simulated"])
        self.assertEqual(result["model_name"], "local-demo-model")

    def test_result_never_contains_api_key_material(self) -> None:
        result = run_diagnosis(
            self.event,
            self.catalog,
            api_key_present=True,
            model_name="local-demo-model",
        )
        encoded = json.dumps(result, ensure_ascii=False).lower()

        self.assertNotIn("api_key", encoded)
        self.assertNotIn("authorization", encoded)
        self.assertNotIn("secret", encoded)


if __name__ == "__main__":
    unittest.main()
