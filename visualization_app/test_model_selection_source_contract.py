from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import online_inference  # noqa: E402
from release_contract import (  # noqa: E402
    ReleaseContractError,
    managed_model_paths,
    release_contract_from_mapping,
)


def contract_payload() -> dict:
    return {
        "schema_version": 1,
        "identity": {
            "application_version": "2.0.5",
            "helper_version": "2.0.5",
            "launcher_version": "2.0.5",
            "release_revision": "revision-1",
        },
        "contracts": {
            "helper_protocol_version": 2,
            "unified_frame_contract": "unified_frame_v1",
            "configuration_schema_version": 1,
            "readiness_schema_version": 1,
        },
        "static_assets": {"required": ["app/ui/app.js"]},
        "managed_files": {
            "manifest": "SHA256SUMS.txt",
            "digest_algorithm": "sha256",
            "managed_model_roots": ["models", "new_collection_demo_v11_3/models"],
        },
    }


class ModelSelectionSourceContractTests(unittest.TestCase):
    def test_real_path_boundary_distinguishes_bundled_external_and_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managed = root / "models"
            bundled = managed / "new" / "model.pth"
            bundled.parent.mkdir(parents=True)
            bundled.write_bytes(b"bundled")
            external = root / "external" / "models" / "new" / "model.pth"
            external.parent.mkdir(parents=True)
            external.write_bytes(b"external")
            similar = root / "models-new" / "model.pth"
            similar.parent.mkdir()
            similar.write_bytes(b"similar")

            classify = online_inference.classify_model_source
            self.assertEqual(classify(bundled, [managed]), "bundled")
            self.assertEqual(classify(managed / "new" / ".." / "new" / "model.pth", [managed]), "bundled")
            self.assertEqual(classify(external, [managed]), "external")
            self.assertEqual(classify(similar, [managed]), "external")
            self.assertEqual(classify(root / "missing.pth", [managed]), "invalid")

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_symlink_escape_is_external(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managed = root / "models"
            managed.mkdir()
            outside = root / "outside.pth"
            outside.write_bytes(b"outside")
            link = managed / "linked.pth"
            try:
                link.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            self.assertEqual(
                online_inference.classify_model_source(link, [managed]),
                "external",
            )

    def test_manifest_roots_resolve_from_candidate_root(self):
        contract = release_contract_from_mapping(
            contract_payload(),
            source_path=Path("C:/candidate/RELEASE-MANIFEST.json"),
        )
        self.assertEqual(
            managed_model_paths(contract),
            (
                Path("C:/candidate/models").resolve(),
                Path("C:/candidate/new_collection_demo_v11_3/models").resolve(),
            ),
        )

    def test_missing_or_escaping_manifest_roots_are_rejected(self):
        payload = contract_payload()
        payload["managed_files"] = {
            "manifest": "SHA256SUMS.txt",
            "digest_algorithm": "sha256",
        }
        with self.assertRaises(ReleaseContractError):
            release_contract_from_mapping(payload)
        payload["managed_files"]["managed_model_roots"] = ["../models"]
        with self.assertRaises(ReleaseContractError):
            release_contract_from_mapping(payload)

    def test_frontend_consumes_explicit_model_source(self):
        source = Path(__file__).with_name("static").joinpath("app.js").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("isBundledModelPath", source)
        self.assertIn("modelSource === \"bundled\"", source)
        self.assertIn("model_source: \"external\"", source)
        self.assertIn('typeof remembered === "string"', source)

    def test_server_history_keeps_external_path_with_bundled_name_fragment(self):
        with tempfile.TemporaryDirectory() as directory:
            history_path = Path(directory) / "history.json"
            checkpoint = Path(directory) / "external" / "models" / "new" / "model.pth"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_bytes(b"external")
            original = online_inference._selection_history_path
            try:
                online_inference._selection_history_path = lambda: history_path
                online_inference._remember_model_selection(
                    "i_T_G", "new_collection_v11_3", checkpoint, "external"
                )
            finally:
                online_inference._selection_history_path = original
            payload = json.loads(history_path.read_text(encoding="utf-8"))
            remembered = next(iter(payload.values()))
            self.assertEqual(remembered["checkpoint"], str(checkpoint))
            self.assertEqual(remembered["model_source"], "external")


if __name__ == "__main__":
    unittest.main()
