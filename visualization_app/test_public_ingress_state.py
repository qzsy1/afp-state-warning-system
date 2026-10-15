from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from visualization_app.public_ingress_state import (
    PublicIngressStateError,
    load_public_ingress_state,
    public_ingress_state_path,
)
from visualization_app.release_contract import release_contract_from_mapping


def valid_state(**updates):
    payload = {
        "schema_version": 1,
        "generation": 3,
        "public_url": "https://afp.example.test",
        "provider": "tailscale",
        "recovery_owner": "tailscale",
        "candidate_id": "candidate-1",
        "status": "online",
        "published_at": "2026-10-15T00:00:00+00:00",
        "layers": {"edge": {"ok": True}},
    }
    payload.update(updates)
    return payload


class PublicIngressStateTests(unittest.TestCase):
    def test_valid_load_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(valid_state()), encoding="utf-8")
            state = load_public_ingress_state(path, "candidate-1")
        self.assertEqual(state.generation, 3)
        self.assertEqual(state.layers["edge"]["ok"], True)
        with self.assertRaises((AttributeError, TypeError)):
            state.generation = 4

    def test_missing_malformed_and_missing_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            with self.assertRaises(PublicIngressStateError):
                load_public_ingress_state(path)
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(PublicIngressStateError):
                load_public_ingress_state(path)
            for field in valid_state():
                payload = valid_state()
                payload.pop(field)
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.subTest(field=field), self.assertRaises(PublicIngressStateError):
                    load_public_ingress_state(path)

    def test_negative_generation_and_candidate_mismatch_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(valid_state(generation=-1)), encoding="utf-8")
            with self.assertRaises(PublicIngressStateError):
                load_public_ingress_state(path)
            path.write_text(json.dumps(valid_state()), encoding="utf-8")
            with self.assertRaises(PublicIngressStateError):
                load_public_ingress_state(path, "candidate-2")

    def test_manifest_root_path_resolution(self):
        payload = {
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
            "static_assets": {"required": ["app/static/app.js"]},
            "managed_files": {"manifest": "SHA256SUMS.txt", "digest_algorithm": "sha256"},
        }
        contract = release_contract_from_mapping(
            payload,
            source_path=Path("C:/candidate/RELEASE-MANIFEST.json"),
        )
        self.assertEqual(
            public_ingress_state_path(contract),
            Path("C:/candidate/runtime/public-ingress-state.json"),
        )
        source_contract = release_contract_from_mapping(
            payload,
            source_path=Path("C:/repo/pre_sensor/release-contract.json"),
        )
        self.assertEqual(
            public_ingress_state_path(source_contract),
            Path("C:/repo/runtime/public-ingress-state.json"),
        )


if __name__ == "__main__":
    unittest.main()
