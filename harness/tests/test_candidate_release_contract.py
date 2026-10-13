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
        self.assertIn("build_local_capture_helper.ps1", source)
        self.assertIn("-OutputDirectory", source)
        self.assertIn("-HelperExecutable", source)

    def test_candidate_runtime_requires_the_local_helper(self) -> None:
        source = (
            ROOT / "harness" / "engine" / "real_acquisition_preflight.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"local_helper/AFP_Local_Capture_Helper.exe"', source)

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
            helper = candidate / "local_helper" / "AFP_Local_Capture_Helper.exe"
            helper.parent.mkdir()
            helper.write_bytes(b"helper")
            template_hashes = {}
            for relative in source_contract["configuration_templates"]["required"]:
                template = candidate / relative
                template.parent.mkdir(parents=True, exist_ok=True)
                template.write_bytes(relative.encode("utf-8"))
                template_hashes[relative] = hashlib.sha256(
                    relative.encode("utf-8")
                ).hexdigest()
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
                "configuration_templates": {
                    **source_contract["configuration_templates"],
                    "sha256": template_hashes,
                },
                "runtime_requirements": {
                    **source_contract["runtime_requirements"],
                    "required_executables": [
                        {
                            "id": "local_capture_helper",
                            "path": "local_helper/AFP_Local_Capture_Helper.exe",
                            "sha256": hashlib.sha256(b"helper").hexdigest(),
                        }
                    ],
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

    def test_release_contract_binds_the_installable_helper_executable(self) -> None:
        source_contract = json.loads(
            (ROOT / "pre_sensor" / "release-contract.json").read_text(encoding="utf-8")
        )
        required = source_contract["runtime_requirements"]["required_executables"]
        self.assertEqual(
            required,
            [
                {
                    "id": "local_capture_helper",
                    "path": "local_helper/AFP_Local_Capture_Helper.exe",
                }
            ],
        )

        assembly = (ROOT / "modular_runtime" / "assemble_modular_delivery.ps1").read_text(
            encoding="utf-8-sig"
        )
        builder = (ROOT / "pre_sensor" / "build-candidate.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.assertIn("HelperExecutable", assembly)
        self.assertIn("AFP_Local_Capture_Helper.exe", assembly)
        self.assertIn("build_local_capture_helper.ps1", builder)
        self.assertIn("HelperExecutable", builder)

        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary)
            manifest = {
                **source_contract,
                "candidate": {
                    "candidate_id": "2.0.5-rc.1-test",
                    "source_revision": "abc123",
                    "created_at": "2026-10-13T00:00:00Z",
                },
                "static_assets": {"required": [], "sha256": {}},
                "configuration_templates": {"required": [], "sha256": {}},
                "runtime_requirements": {
                    **source_contract["runtime_requirements"],
                    "native_components": [],
                    "vendor_sdks": [],
                    "drivers": [],
                    "required_executables": [
                        {
                            "id": "local_capture_helper",
                            "path": "local_helper/AFP_Local_Capture_Helper.exe",
                            "sha256": "0" * 64,
                        }
                    ],
                },
            }
            (candidate / "RELEASE-MANIFEST.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            self.assertIn(
                "release required executable missing: "
                "local_helper/AFP_Local_Capture_Helper.exe",
                validate_candidate_release_contract(candidate),
            )

    def test_release_manifest_binds_both_mode_templates_and_schema(self) -> None:
        source_contract = json.loads(
            (ROOT / "pre_sensor" / "release-contract.json").read_text(encoding="utf-8")
        )
        expected = {
            "config/pre_sensor/schema.json",
            "config/pre_sensor/local_direct.json",
            "config/pre_sensor/remote_helper.json",
        }
        declared = set(source_contract["configuration_templates"]["required"])
        self.assertEqual(declared, expected)

        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary)
            digests = {}
            for relative in expected:
                path = candidate / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                content = relative.encode("utf-8")
                path.write_bytes(content)
                digests[relative] = hashlib.sha256(content).hexdigest()
            manifest = {
                **source_contract,
                "candidate": {
                    "candidate_id": "2.0.5-rc.1-test",
                    "source_revision": "abc123",
                    "created_at": "2026-10-09T00:00:00Z",
                },
                "configuration_templates": {
                    "required": sorted(expected),
                    "sha256": digests,
                },
                "static_assets": {"required": [], "sha256": {}},
                "runtime_requirements": {
                    **source_contract["runtime_requirements"],
                    "native_components": [],
                },
            }
            (candidate / "RELEASE-MANIFEST.json").write_text(
                json.dumps(manifest), encoding="utf-8"
            )
            # This focused test ignores unrelated fixture declarations and
            # asserts that configuration tampering is independently detected.
            issues = validate_candidate_release_contract(candidate)
            self.assertFalse(any("configuration template" in item for item in issues))
            changed = candidate / "config" / "pre_sensor" / "local_direct.json"
            changed.write_bytes(b"tampered")
            self.assertIn(
                "release configuration template hash mismatch: "
                "config/pre_sensor/local_direct.json",
                validate_candidate_release_contract(candidate),
            )


if __name__ == "__main__":
    unittest.main()
