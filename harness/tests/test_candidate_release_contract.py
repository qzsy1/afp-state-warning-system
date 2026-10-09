from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from harness.engine.real_acquisition_preflight import validate_candidate_release_contract


ROOT = Path(__file__).resolve().parents[2]


class CandidateReleaseContractTests(unittest.TestCase):
    def test_candidate_builder_is_isolated_and_refuses_existing_target(self) -> None:
        script = ROOT / "pre_sensor" / "build-candidate.ps1"
        source = script.read_text(encoding="utf-8")
        self.assertIn("artifacts\\pre-sensor-candidates", source)
        self.assertIn("Target already exists", source)
        self.assertNotIn("AllowExistingTarget", source)
        self.assertNotIn("delivery\\AFP_Integrated", source)

    def test_release_manifest_binds_static_native_and_managed_assets(self) -> None:
        source_contract = json.loads(
            (ROOT / "pre_sensor" / "release-contract.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            {item["id"] for item in source_contract["runtime_requirements"]["native_components"]},
            {"bsv_uvc", "libusb"},
        )
        self.assertTrue(source_contract["runtime_requirements"]["vendor_sdks"])
        self.assertTrue(source_contract["runtime_requirements"]["drivers"])

        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary)
            asset = candidate / "app" / "ui" / "app.js"
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"asset")
            native = candidate / "native_dll" / "BsvUvcNative.dll"
            native.parent.mkdir()
            native.write_bytes(b"dll")
            manifest = {
                **source_contract,
                "candidate": {
                    "candidate_id": "2.0.5-rc.1-test",
                    "source_revision": "abc123",
                    "created_at": "2026-10-09T00:00:00Z",
                },
                "static_assets": {
                    "required": ["app/ui/app.js"],
                    "sha256": {"app/ui/app.js": hashlib.sha256(b"asset").hexdigest()},
                },
                "runtime_requirements": {
                    **source_contract["runtime_requirements"],
                    "native_components": [
                        {
                            "id": "bsv_uvc",
                            "path": "native_dll/BsvUvcNative.dll",
                            "sha256": hashlib.sha256(b"dll").hexdigest(),
                        }
                    ],
                },
                "managed_files": {
                    **source_contract["managed_files"],
                    "manifest_sha256": "0" * 64,
                },
            }
            (candidate / "RELEASE-MANIFEST.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )

            self.assertEqual(validate_candidate_release_contract(candidate), ())
            asset.write_bytes(b"tampered")
            self.assertIn(
                "release static hash mismatch: app/ui/app.js",
                validate_candidate_release_contract(candidate),
            )


if __name__ == "__main__":
    unittest.main()
