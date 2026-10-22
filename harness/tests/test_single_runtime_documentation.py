from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXED_RUNTIME = r"D:\AFP_Runtime\AFP_Integrated_System_Modular"
FIXED_EVIDENCE = r"D:\AFP_Runtime\AFP-Main-Evidence"
STAGING_ROOT = r"D:\AFP_Runtime\.afp-staging"
LEGACY_MARKERS = (
    "AFP_Integrated_System_Modular_v2.0.4_Agentic",
    r"F:\AFP_Integrated_Modular_v2",
)


class SingleRuntimeDocumentationTests(unittest.TestCase):
    def test_operator_docs_define_fixed_runtime_and_current_authorization(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        field_ops = (ROOT / "pre_sensor" / "FIELD-OPERATIONS.md").read_text(
            encoding="utf-8"
        )
        combined = readme + "\n" + field_ops

        for required in (
            FIXED_RUNTIME,
            FIXED_EVIDENCE,
            STAGING_ROOT,
            "切换审批",
            "清理审批",
            "Git",
            "physical_sensor_pending",
        ):
            with self.subTest(required=required):
                self.assertIn(required, combined)
        for marker in LEGACY_MARKERS:
            self.assertNotIn(marker, combined)

    def test_active_configs_point_to_the_fixed_runtime(self) -> None:
        baseline = json.loads(
            (ROOT / "harness" / "config" / "delivery-readonly-baseline.json").read_text(
                encoding="utf-8"
            )
        )
        settings = json.loads(
            (ROOT / "harness" / "config" / "local-settings.example.json").read_text(
                encoding="utf-8"
            )
        )
        matrix_text = (
            ROOT / "harness" / "config" / "regression-matrix.json"
        ).read_text(encoding="utf-8")

        self.assertEqual(baseline["delivery_path"], FIXED_RUNTIME)
        self.assertEqual(
            settings["baseline_exe"],
            FIXED_RUNTIME + r"\AFP_Integrated_System_Modular.exe",
        )
        self.assertIn('"{baseline_exe}"', matrix_text)
        self.assertNotIn(
            FIXED_RUNTIME.replace("\\", "\\\\")
            + r"\\verification\\test_real_acquisition_pipeline.py",
            matrix_text,
        )
        for marker in LEGACY_MARKERS:
            self.assertNotIn(marker, matrix_text)

    def test_active_build_and_helper_defaults_do_not_use_versioned_roots(self) -> None:
        active_files = (
            ROOT / "modular_runtime" / "build_modular_app.ps1",
            ROOT / "visualization_app" / "build_local_capture_helper.ps1",
            ROOT / "validation" / "client_lab" / "New-ClientLabStage.ps1",
        )
        combined = "\n".join(path.read_text(encoding="utf-8-sig") for path in active_files)

        self.assertIn(STAGING_ROOT, combined)
        self.assertIn(FIXED_RUNTIME, combined)
        for marker in LEGACY_MARKERS:
            self.assertNotIn(marker, combined)


if __name__ == "__main__":
    unittest.main()
