from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest


class PublicDemoBundleTests(unittest.TestCase):
    def _module(self):
        try:
            import public_demo_bundles
        except ModuleNotFoundError:
            self.fail("public_demo_bundles must generate the approved offline demo assets")
        return public_demo_bundles

    def test_quick_and_endurance_bundles_are_complete_and_precomputed(self):
        module = self._module()
        for package_id, expected_points in (("quick_240", 240), ("endurance_6000", 6000)):
            bundle = module.build_demo_bundle(package_id)
            self.assertEqual(bundle["schema_version"], "afp-public-demo-v1")
            self.assertTrue(bundle["synthetic"])
            self.assertTrue(bundle["precomputed"])
            self.assertEqual(bundle["sample_rate_hz"], 10.0)
            self.assertEqual(bundle["points"], expected_points)
            self.assertEqual(len(bundle["interfaces"]), 5)
            self.assertEqual(
                {item["role"] for item in bundle["interfaces"]},
                {"thermocouple", "plc", "thermal_uvc", "robot", "pressure"},
            )
            self.assertEqual(len(bundle["channels"]), 17)
            for channel in bundle["channels"]:
                self.assertEqual(len(channel["actual"]), expected_points)
                self.assertEqual(len(channel["predicted"]), expected_points)
                self.assertIn(channel["interface_id"], {item["id"] for item in bundle["interfaces"]})
            self.assertEqual(len(bundle["window_evidence"]), (expected_points + 23) // 24)
            self.assertIn("diagnosis", bundle)
            self.assertTrue(bundle["diagnosis"]["precomputed"])
            self.assertIn("aggregate", bundle)

    def test_generated_assets_are_content_addressed_and_reproducible(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            first = Path(first_dir)
            second = Path(second_dir)
            first_manifest = module.write_demo_assets(first)
            second_manifest = module.write_demo_assets(second)
            self.assertEqual(first_manifest, second_manifest)
            self.assertEqual(first_manifest["schema_version"], "afp-public-demo-manifest-v1")
            self.assertEqual(len(first_manifest["packages"]), 2)
            for item in first_manifest["packages"]:
                filename = Path(item["url"]).name
                raw = (first / filename).read_bytes()
                second_raw = (second / filename).read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                self.assertEqual(raw, second_raw)
                self.assertEqual(item["sha256"], digest)
                self.assertEqual(item["bytes"], len(raw))
                self.assertIn(digest[:16], filename)
                self.assertTrue(item["url"].startswith("/demo/"))
            persisted = json.loads((first / "manifest-v1.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted, first_manifest)

    def test_bundles_do_not_expose_secrets_paths_or_production_claims(self):
        module = self._module()
        forbidden = (
            "f:\\",
            "c:\\",
            "password",
            "api_key",
            "token",
            "sk-",
            "已确认缺陷",
            "confirmed defect",
        )
        for package_id in ("quick_240", "endurance_6000"):
            encoded = json.dumps(module.build_demo_bundle(package_id), ensure_ascii=False).lower()
            for marker in forbidden:
                self.assertNotIn(marker, encoded)


if __name__ == "__main__":
    unittest.main()
