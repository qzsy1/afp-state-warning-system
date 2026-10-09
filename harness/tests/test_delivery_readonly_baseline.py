from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from harness.engine.delivery_readonly import (
    snapshot_tree_digest,
    verify_delivery_baseline,
)
from harness.engine.real_acquisition_preflight import capture_delivery_snapshot


class DeliveryReadonlyBaselineTests(unittest.TestCase):
    def test_pinned_per_file_manifest_detects_delivery_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "delivery"
            root.mkdir()
            managed = root / "app.py"
            managed.write_text("stable\n", encoding="utf-8")
            file_digest = hashlib.sha256(managed.read_bytes()).hexdigest()
            manifest = root / "SHA256SUMS.txt"
            manifest.write_text(f"{file_digest} *app.py\n", encoding="utf-8")
            snapshot = capture_delivery_snapshot(root)
            baseline = Path(temporary) / "baseline.json"
            baseline.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "managed_file_count": len(snapshot.files),
                        "tree_sha256": snapshot_tree_digest(snapshot),
                        "per_file_manifest": "SHA256SUMS.txt",
                        "per_file_manifest_entries": 1,
                        "per_file_manifest_sha256": hashlib.sha256(
                            manifest.read_bytes()
                        ).hexdigest(),
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(verify_delivery_baseline(root, baseline), ())

            managed.write_text("changed\n", encoding="utf-8")
            issues = verify_delivery_baseline(root, baseline)
            self.assertTrue(any("tree_sha256" in issue for issue in issues), issues)
            self.assertTrue(any("manifest digest mismatch" in issue for issue in issues), issues)


if __name__ == "__main__":
    unittest.main()
