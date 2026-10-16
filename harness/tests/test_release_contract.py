from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import visualization_app.release_contract as release_contract_module

from visualization_app.release_contract import (
    ReleaseContractError,
    compare_release_contracts,
    helper_capability_mismatches,
    load_release_contract,
    release_manifest_helper_identity,
)


ROOT = Path(__file__).resolve().parents[2]


class ReleaseContractTests(unittest.TestCase):
    def test_authoritative_contract_binds_every_release_surface(self) -> None:
        contract = load_release_contract(
            explicit=ROOT / "pre_sensor" / "release-contract.json"
        )
        self.assertEqual(contract.schema_version, 1)
        self.assertEqual(contract.application_version, contract.helper_version)
        self.assertEqual(contract.application_version, contract.launcher_version)
        self.assertTrue(contract.release_revision)
        self.assertEqual(contract.helper_protocol_version, 2)
        self.assertEqual(contract.unified_frame_contract, "unified_frame_v1")
        self.assertEqual(contract.configuration_schema_version, 1)
        payload = json.loads(
            (ROOT / "pre_sensor" / "release-contract.json").read_text(encoding="utf-8")
        )
        required_modules = payload["runtime_requirements"]["required_modules"]
        self.assertIn("mysql.connector", required_modules)
        self.assertNotIn("pymysql", required_modules)
        self.assertEqual(contract.readiness_schema_version, 1)
        self.assertIn("app/ui/app.js", contract.required_static_assets)
        self.assertEqual(contract.managed_file_manifest, "SHA256SUMS.txt")
        self.assertEqual(contract.digest_algorithm, "sha256")

    def test_missing_or_mismatched_contract_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "RELEASE-MANIFEST.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ReleaseContractError):
                load_release_contract(explicit=path)

        expected = load_release_contract(
            explicit=ROOT / "pre_sensor" / "release-contract.json"
        )
        mismatched = json.loads(
            (ROOT / "pre_sensor" / "release-contract.json").read_text(encoding="utf-8")
        )
        mismatched["identity"]["helper_version"] = "wrong"
        self.assertIn("helper_version", compare_release_contracts(expected, mismatched))

    def test_contract_and_loader_contain_no_credentials(self) -> None:
        text = (ROOT / "pre_sensor" / "release-contract.json").read_text(
            encoding="utf-8"
        ).lower()
        for marker in ("password", "token", "credential", "api_key", "secret"):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, text)

    def test_frozen_helper_loads_manifest_beside_executable(self) -> None:
        source = ROOT / "pre_sensor" / "release-contract.json"
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "candidate"
            helper = candidate / "local_helper" / "AFP_Local_Capture_Helper.exe"
            helper.parent.mkdir(parents=True)
            helper.write_bytes(b"synthetic")
            (candidate / "RELEASE-MANIFEST.json").write_bytes(source.read_bytes())
            extraction_module = Path(temporary) / "_MEI-test" / "acquisition.py"
            with patch.object(
                release_contract_module.sys, "frozen", True, create=True
            ), patch.object(
                release_contract_module.sys, "executable", str(helper)
            ):
                contract = load_release_contract(start=extraction_module)
        self.assertEqual(contract.source_path, candidate / "RELEASE-MANIFEST.json")

    def test_manifest_binds_helper_candidate_and_executable_hash(self) -> None:
        payload = json.loads(
            (ROOT / "pre_sensor" / "release-contract.json").read_text(encoding="utf-8")
        )
        payload["candidate"] = {"candidate_id": "candidate-1"}
        payload["runtime_requirements"]["required_executables"][0]["sha256"] = "a" * 64
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "RELEASE-MANIFEST.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            contract = load_release_contract(explicit=path)
            self.assertEqual(
                release_manifest_helper_identity(contract),
                ("candidate-1", "a" * 64),
            )
            capabilities = {
                **contract.identity_payload(),
                "protocol_version": contract.helper_protocol_version,
                "helper_version": contract.helper_version,
                "build_id": contract.release_revision,
                "unified_frame_contract": contract.unified_frame_contract,
                "configuration_schema_version": contract.configuration_schema_version,
                "readiness_schema_version": contract.readiness_schema_version,
                "unified_frame_v1": True,
                "candidate_id": "candidate-1",
                "helper_sha256": "b" * 64,
            }
            self.assertEqual(
                helper_capability_mismatches(contract, capabilities),
                ("helper_sha256",),
            )


if __name__ == "__main__":
    unittest.main()
