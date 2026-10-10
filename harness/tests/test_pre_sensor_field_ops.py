from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class EvidenceExporterTests(unittest.TestCase):
    def test_export_redacts_credentials_urls_environment_and_private_paths(self) -> None:
        from visualization_app.pre_sensor_evidence import export_sanitized_evidence

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.json"
            source.write_text(
                json.dumps(
                    {
                        "password": "secret-value",
                        "token": "token-value",
                        "url": "https://operator:pw@example.test/api?token=query-secret",
                        "environment": "AFP_MYSQL_PASSWORD=env-secret",
                        "device_path": r"C:\Users\Alice\private-device\COM8",
                        "safe": "kept",
                    }
                ),
                encoding="utf-8",
            )
            result = export_sanitized_evidence(
                [source], root / "export", secret_values=("secret-value", "token-value", "pw", "query-secret", "env-secret", "Alice")
            )
            payload = (root / "export" / "evidence.json").read_text(encoding="utf-8")
        self.assertTrue(result["ok"])
        self.assertEqual(result["files"], 1)
        self.assertIn('"safe": "kept"', payload)
        for forbidden in ("secret-value", "token-value", "operator:pw", "query-secret", "env-secret", "Alice"):
            self.assertNotIn(forbidden, payload)
        self.assertIn("sha256", result["manifest"][0])

    def test_export_fails_closed_if_known_secret_survives(self) -> None:
        from visualization_app.pre_sensor_evidence import export_sanitized_evidence

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "opaque.bin"
            source.write_bytes(b"prefix-opaque-secret-suffix")
            with self.assertRaisesRegex(ValueError, "cannot be safely redacted"):
                export_sanitized_evidence(
                    [source], root / "export", secret_values=("opaque-secret",)
                )


class RemainingPhysicalChecklistTests(unittest.TestCase):
    def test_only_physical_device_whitelist_is_accepted(self) -> None:
        from visualization_app.pre_sensor_evidence import validate_physical_checklist

        checklist = json.loads(
            (ROOT / "pre_sensor" / "real-device-only-checklist.json").read_text(encoding="utf-8")
        )
        validated = validate_physical_checklist(checklist)
        self.assertEqual(len(validated), 9)
        for forbidden in ("version", "config", "network", "storage", "mysql", "helper", "windows"):
            changed = json.loads(json.dumps(checklist))
            changed["items"].append({"id": f"forbidden.{forbidden}", "title": forbidden})
            with self.assertRaisesRegex(ValueError, "not a physical-device item"):
                validate_physical_checklist(changed)

    def test_operator_manual_and_candidate_assembly_cover_required_order(self) -> None:
        manual = (ROOT / "pre_sensor" / "FIELD-OPERATIONS.md").read_text(encoding="utf-8")
        positions = [
            manual.index(term)
            for term in (
                "安装候选包",
                "运行传感器前预检",
                "选择模式",
                "修复失败项",
                "启动采集",
                "观察状态",
                "正常停止",
                "数据库重试与最终对账",
                "导出脱敏证据",
            )
        ]
        self.assertEqual(positions, sorted(positions))
        assembly = (ROOT / "modular_runtime" / "assemble_modular_delivery.ps1").read_text(encoding="utf-8-sig")
        for name in (
            "FIELD-OPERATIONS.md",
            "real-device-only-checklist.json",
            "export-pre-sensor-evidence.ps1",
            "pre_sensor_evidence.py",
        ):
            self.assertIn(name, assembly)


if __name__ == "__main__":
    unittest.main()
