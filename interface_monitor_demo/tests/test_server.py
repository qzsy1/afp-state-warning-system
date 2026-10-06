from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from interface_monitor_demo.server import create_server


class ServerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = create_server(port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def get_json(self, path: str) -> tuple[int, dict[str, object]]:
        with urllib.request.urlopen(self.base_url + path, timeout=2) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def post_json(
        self, path: str, payload: dict[str, object]
    ) -> tuple[int, dict[str, object]]:
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_bootstrap_exposes_five_interfaces_and_scenarios(self) -> None:
        status, payload = self.get_json("/api/bootstrap")

        self.assertEqual(status, 200)
        self.assertEqual(len(payload["interfaces"]), 5)
        self.assertEqual(payload["interfaces"][4]["id"], "m3232_pressure")
        self.assertEqual(payload["interfaces"][4]["channels"], ["薄膜压力"])
        self.assertTrue(any(item["id"] == "parse_error" for item in payload["scenarios"]))
        self.assertTrue(payload["simulated"])

    def test_scenario_then_diagnosis_returns_langchain_trace(self) -> None:
        scenario_status, event = self.post_json(
            "/api/scenario",
            {"interface_id": "m3232_pressure", "scenario": "parse_error"},
        )
        diagnosis_status, result = self.post_json(
            "/api/diagnose",
            {
                "api_key_present": True,
                "model_name": "local-demo-model",
                "event_id": event["event_id"],
            },
        )

        self.assertEqual(scenario_status, 200)
        self.assertEqual(diagnosis_status, 200)
        self.assertEqual(result["diagnosis"]["channels"], ["薄膜压力"])
        self.assertEqual(len(result["trace"]), 7)
        self.assertEqual(result["execution_mode"], "langchain_local_runnable_simulation")

    def test_diagnosis_gate_rejects_missing_key_presence(self) -> None:
        _, event = self.post_json(
            "/api/scenario", {"interface_id": "plc_process", "scenario": "timeout"}
        )
        status, payload = self.post_json(
            "/api/diagnose",
            {
                "api_key_present": False,
                "model_name": "local-demo-model",
                "event_id": event["event_id"],
            },
        )

        self.assertEqual(status, 400)
        self.assertIn("API Key", payload["error"])

    def test_server_rejects_secret_fields_before_dispatch(self) -> None:
        status, payload = self.post_json(
            "/api/diagnose",
            {
                "api_key": "must-not-cross-boundary",
                "api_key_present": True,
                "model_name": "local-demo-model",
                "event_id": "evt-0001",
            },
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "请求不得包含 API Key、密码或令牌原文")
        self.assertNotIn("must-not-cross-boundary", json.dumps(payload, ensure_ascii=False))

    def test_diagnosis_rejects_all_fields_outside_allowlist(self) -> None:
        for field_name in ("openai_api_key", "auth_token", "credentials", "x-api-key"):
            with self.subTest(field_name=field_name):
                status, payload = self.post_json(
                    "/api/diagnose",
                    {
                        "api_key_present": True,
                        "model_name": "local-demo-model",
                        "event_id": "evt-0001",
                        field_name: "must-not-cross-boundary",
                    },
                )

                self.assertEqual(status, 400)
                self.assertIn("未允许字段", payload["error"])
                self.assertNotIn(
                    "must-not-cross-boundary", json.dumps(payload, ensure_ascii=False)
                )

    def test_unknown_event_is_rejected(self) -> None:
        status, payload = self.post_json(
            "/api/diagnose",
            {
                "api_key_present": True,
                "model_name": "local-demo-model",
                "event_id": "evt-missing",
            },
        )

        self.assertEqual(status, 404)
        self.assertIn("异常事件", payload["error"])

    def test_reset_restores_healthy_status(self) -> None:
        self.post_json(
            "/api/scenario", {"interface_id": "abb_motion", "scenario": "stale"}
        )
        status, payload = self.post_json("/api/reset", {})

        self.assertEqual(status, 200)
        self.assertIsNone(payload["active_event"])
        self.assertTrue(all(item["state"] == "healthy" for item in payload["interfaces"]))


if __name__ == "__main__":
    unittest.main()
