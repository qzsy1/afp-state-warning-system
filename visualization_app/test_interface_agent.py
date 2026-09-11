from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from interface_agent import (
    AgentGateError,
    build_agent_event,
    run_interface_diagnosis,
    validate_agent_payload,
)


ROOT = Path(__file__).resolve().parent


def m3232_result() -> dict:
    return {
        "simulated": False,
        "interfaces": [
            {
                "id": "m3232_pressure",
                "role": "pressure",
                "driver": "m3232_pressure",
                "endpoint": "COM8",
                "expected_channels": ["薄膜压力"],
                "detected_channels": [],
                "missing_channels": ["薄膜压力"],
                "invalid_channels": ["薄膜压力"],
                "sample_counts": {},
                "invalid_sample_counts": {"薄膜压力": 3},
                "state": "invalid_data",
                "message": "收到非数值数据：薄膜压力",
                "ok": False,
            }
        ],
        "sensors": [
            {
                "name": "薄膜压力",
                "selected": True,
                "received_samples": 0,
                "invalid_samples": 3,
                "state": "invalid_data",
                "message": "收到非数值数据",
                "ok": False,
            }
        ],
    }


def mixed_interface_result() -> dict:
    result = m3232_result()
    result["interfaces"].insert(0, {
        "id": "plc_process",
        "role": "process",
        "driver": "modbus_tcp",
        "endpoint": "192.168.1.10:502",
        "expected_channels": ["压力"],
        "detected_channels": [],
        "missing_channels": ["压力"],
        "invalid_channels": [],
        "state": "no_data",
        "message": "PLC 未返回数据",
        "ok": False,
    })
    result["sensors"].append({
        "name": "压力", "selected": True, "received_samples": 0,
        "invalid_samples": 0, "state": "no_data", "message": "没有采集数据", "ok": False,
    })
    return result


class InterfaceAgentTests(unittest.TestCase):
    def test_build_event_keeps_m3232_thin_film_pressure_identity(self) -> None:
        event = build_agent_event(m3232_result())

        self.assertIsNotNone(event)
        self.assertEqual(event["interface_id"], "m3232_pressure")
        self.assertEqual(event["endpoint"], "COM8")
        self.assertEqual(event["sensor_name"], "薄膜压力")
        self.assertEqual(event["channels"], ["薄膜压力"])
        self.assertEqual(event["state"], "invalid_data")

    def test_build_event_matches_sensor_to_its_interface(self) -> None:
        event = build_agent_event(mixed_interface_result())

        self.assertEqual(event["interface_id"], "m3232_pressure")
        self.assertEqual(event["endpoint"], "COM8")
        self.assertEqual(event["sensor_name"], "薄膜压力")

    def test_run_diagnosis_returns_auditable_local_langchain_trace(self) -> None:
        result = run_interface_diagnosis(
            build_agent_event(m3232_result()),
            api_key_present=True,
            model_name="local-demo-model",
        )

        self.assertEqual(
            [item["id"] for item in result["trace"]],
            [
                "event_received",
                "gate_checked",
                "get_interface_context",
                "inspect_error_evidence",
                "lookup_local_rule",
                "compose_diagnostic_prompt",
                "complete",
            ],
        )
        self.assertEqual(result["diagnosis"]["interface_id"], "m3232_pressure")
        self.assertEqual(result["diagnosis"]["sensor_name"], "薄膜压力")
        self.assertNotIn("PLC压力", result["diagnosis"]["summary"])
        self.assertEqual(result["execution_mode"], "langchain_local_runnable_simulation")

    def test_gate_blocks_when_key_or_model_is_missing(self) -> None:
        event = build_agent_event(m3232_result())
        with self.assertRaisesRegex(AgentGateError, "API Key"):
            run_interface_diagnosis(event, api_key_present=False, model_name="local-demo-model")
        with self.assertRaisesRegex(AgentGateError, "模型名称"):
            run_interface_diagnosis(event, api_key_present=True, model_name=" ")

    def test_result_never_contains_secret_material(self) -> None:
        result = run_interface_diagnosis(
            build_agent_event(m3232_result()),
            api_key_present=True,
            model_name="local-demo-model",
        )

        serialized = json.dumps(result, ensure_ascii=False).lower()
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("authorization", serialized)

    def test_global_langsmith_tracing_is_disabled_for_local_flow(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "LANGSMITH_TRACING": "true",
                    "LANGCHAIN_TRACING_V2": "true",
                    "LANGSMITH_API_KEY": "must-not-be-used",
                },
            ),
            patch(
                "langchain_core.tracers.langchain.LangChainTracer",
                side_effect=AssertionError("external tracer must not be created"),
            ) as tracer,
        ):
            run_interface_diagnosis(
                build_agent_event(m3232_result()),
                api_key_present=True,
                model_name="local-demo-model",
            )
        tracer.assert_not_called()

    def test_original_frontend_contains_agent_additions(self) -> None:
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        app_source = (ROOT / "app.py").read_text(encoding="utf-8")
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        for element_id in (
            "agentApiKeyInput",
            "agentModelNameInput",
            "agentGateStatus",
            "agentDiagnoseButton",
            "agentDiagnosisPanel",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn("/api/agent/diagnose", script)
        self.assertIn("api_key_present", script)
        self.assertNotIn("api_key: agentApiKeyInput.value", script)
        self.assertIn("薄膜压力", html)
        self.assertIn("LangChain", html)
        self.assertIn("/api/agent/diagnose", app_source)
        self.assertIn("run_interface_diagnosis", app_source)
        self.assertIn("langchain-core==1.6.2", requirements)

    def test_app_payload_accepts_only_presence_flag_and_event(self) -> None:
        payload = {
            "api_key_present": True,
            "model_name": "local-demo-model",
            "event": build_agent_event(m3232_result()),
        }
        self.assertEqual(validate_agent_payload(payload), payload["event"])

    def test_app_payload_rejects_secret_or_unknown_fields(self) -> None:
        payload = {
            "api_key_present": True,
            "model_name": "local-demo-model",
            "event": build_agent_event(m3232_result()),
            "api_key": "sk-secret",
        }
        with self.assertRaisesRegex(ValueError, "未允许字段"):
            validate_agent_payload(payload)


if __name__ == "__main__":
    unittest.main()
