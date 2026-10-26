from __future__ import annotations

import json
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


class PlcAbbUvcDocumentationTests(unittest.TestCase):
    def test_operator_docs_define_current_api_readiness_and_helper_contract(self) -> None:
        readme = (REPO / "visualization_app" / "README.md").read_text(encoding="utf-8")
        field = (REPO / "pre_sensor" / "FIELD-OPERATIONS.md").read_text(
            encoding="utf-8"
        )
        combined = readme + "\n" + field

        for required in (
            "/api/acquisition/test",
            "/api/acquisition/start",
            "plc_abb_uvc_readiness_v1",
            "data_ready",
            "partial_channels",
            "authentication_failed",
            "driver_incompatible",
            "device_profile_incomplete",
            "physical_binding",
            "device_identity",
            "freshness",
            "stability",
        ):
            with self.subTest(required=required):
                self.assertIn(required, combined)

        self.assertIn("不得静默安装、替换或回滚 UVC 驱动", field)
        self.assertIn("旧检查快照", field)
        for runtime_state in (
            "waiting_first_sample",
            "reconnecting",
            "stale",
            "failed",
            "stop_timed_out",
        ):
            with self.subTest(runtime_state=runtime_state):
                self.assertIn(runtime_state, combined)
        self.assertIn("其他接口继续采集", field)

    def test_harness_maps_automated_and_field_requirements(self) -> None:
        payload = json.loads(
            (REPO / "harness" / "config" / "regression-matrix.json").read_text(
                encoding="utf-8"
            )
        )
        requirements = {item["id"] for item in payload["requirements"]}
        checks = {item["id"]: item for item in payload["checks"]}

        self.assertIn("PLC_ABB_UVC-001", requirements)
        self.assertIn("PLC_ABB_UVC-FIELD-001", requirements)
        automated = checks["plc-abb-uvc-readiness-contracts"]
        self.assertTrue(automated["blocking"])
        self.assertEqual(automated["evidence_tier"], "automated")
        self.assertIn("PLC_ABB_UVC-001", automated["requirements"])
        self.assertIn(
            "visualization_app.test_plc_abb_uvc_documentation",
            automated["command"],
        )
        field = checks["field-real-hardware"]
        self.assertIn("PLC_ABB_UVC-FIELD-001", field["requirements"])
        self.assertEqual(field["evidence_tier"], "field")


if __name__ == "__main__":
    unittest.main()
