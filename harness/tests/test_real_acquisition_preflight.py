from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from harness.engine.matrix import load_matrix
from harness.engine.runner import run_check


class RealAcquisitionPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.repo = Path(__file__).parents[2]
        cls.matrix = load_matrix(cls.repo / "harness" / "config" / "regression-matrix.json")

    def _preflight(self):
        module_name = "harness.engine.real_acquisition_preflight"
        self.assertIsNotNone(
            importlib.util.find_spec(module_name),
            f"missing production module: {module_name}",
        )
        return importlib.import_module(module_name)

    @staticmethod
    def _write_minimal_candidate(candidate: Path, *, omit: set[str] | None = None) -> None:
        omitted = omit or set()
        contents = {
            "AFP_Integrated_System_Modular.exe": b"synthetic-launcher",
            "_internal/runtime.txt": b"runtime",
            "app/legacy/acquisition.py": b"from real_acquisition import ChannelQuality\n",
            "app/legacy/real_acquisition.py": (
                b"from enum import Enum\nclass ChannelQuality(str, Enum):\n    OK = 'ok'\n"
            ),
            "app/legacy/local_direct_acquisition.py": b"MODE = 'local_direct'\n",
            "app/ui/index.html": b"index",
            "app/ui/app.js": b"app",
            "app/ui/styles.css": b"styles",
            "app/ui/mysql_visibility.js": b"mysql",
            "app/ui/process_parameters.js": b"process",
            "app/ui/public_demo.js": b"public",
            "app/ui/training.html": b"training",
            "app/ui/training.js": b"training-js",
        }
        for relative, content in contents.items():
            if relative in omitted:
                continue
            path = candidate / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        manifest_lines = []
        for path in sorted(candidate.rglob("*")):
            if path.is_file():
                relative = path.relative_to(candidate).as_posix()
                digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest().upper()
                manifest_lines.append(f"{digest} *{relative}")
        (candidate / "SHA256SUMS.txt").write_text(
            "\n".join(manifest_lines) + "\n", encoding="utf-8"
        )

    def test_delivery_snapshot_detects_changed_managed_file(self) -> None:
        preflight = self._preflight()
        with tempfile.TemporaryDirectory() as temporary:
            delivery = Path(temporary) / "delivery"
            managed = delivery / "app" / "legacy" / "acquisition.py"
            managed.parent.mkdir(parents=True)
            managed.write_bytes(b"before")

            before = preflight.capture_delivery_snapshot(delivery)
            managed.write_bytes(b"after")
            after = preflight.capture_delivery_snapshot(delivery)

        self.assertEqual(before.files, ("app/legacy/acquisition.py",))
        self.assertEqual(
            before.sha256["app/legacy/acquisition.py"],
            "6db7d803e74f1ffa7d8f5adc0bf95b3e15bf4c8373fffadf546227cc6c6742cb",
        )
        with self.assertRaisesRegex(AssertionError, "changed.*app/legacy/acquisition.py"):
            preflight.assert_delivery_unchanged(before, after)

    def test_missing_field_evidence_remains_pending(self) -> None:
        expected = {
            "field-real-hardware": {"REAL_PREP_PROTOCOL-001", "REAL_PREP_RECOVERY-001"},
            "field-second-windows-helper": {"REAL_PREP_TRANSPORT-001"},
            "field-target-mysql": {"REAL_PREP_PERSISTENCE-001", "REAL_PREP_RECOVERY-001"},
        }
        checks = {check.id: check for check in self.matrix.checks}

        for check_id, requirement_ids in expected.items():
            with self.subTest(check=check_id):
                self.assertIn(check_id, checks)
                check = checks.get(check_id)
                if check is None:
                    continue
                self.assertEqual(check.kind, "field")
                self.assertEqual(check.evidence_tier, "field")
                self.assertFalse(check.blocking)
                self.assertTrue(requirement_ids.issubset(check.requirements))
                result = run_check(check, self.repo, os.environ)
                self.assertEqual(result.status, "pending_field")
                self.assertIsNone(result.exit_code)

    def test_source_inventory_reports_delivery_only_runtime(self) -> None:
        preflight = self._preflight()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "visualization_app"
            release = root / "release" / "app" / "legacy"
            source.mkdir(parents=True)
            release.mkdir(parents=True)
            (source / "acquisition.py").write_text("VALUE = 1\n", encoding="utf-8")
            (release / "acquisition.py").write_text("VALUE = 1\n", encoding="utf-8")
            (release / "real_acquisition.py").write_text("VALUE = 2\n", encoding="utf-8")

            issues = preflight.source_inventory_issues(root, root / "release")

        self.assertEqual(
            issues,
            (
                "delivery-only runtime: app/legacy/real_acquisition.py "
                "has no authoritative source visualization_app/real_acquisition.py",
            ),
        )

    def test_isolated_assembly_never_targets_official_delivery(self) -> None:
        preflight = self._preflight()
        release = self.repo / "delivery" / "AFP_Integrated_System_Modular_v2.0.4_Agentic"
        launcher = release / "AFP_Integrated_System_Modular.exe"

        with self.assertRaisesRegex(ValueError, "official delivery"):
            preflight.run_isolated_assembly(
                self.repo,
                release,
                launcher,
                self.repo / "delivery",
            )

    def test_isolated_assembly_preserves_failure_command_and_logs(self) -> None:
        preflight = self._preflight()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "modular_runtime" / "assemble_modular_delivery.ps1"
            script.parent.mkdir(parents=True)
            script.write_text(
                "param($ReferenceRelease,$TargetDir,$ApplicationVersion,$ExistingExecutable)\n"
                "Write-Output 'assembly stdout'\n"
                "Write-Error 'assembly stderr'\n"
                "exit 7\n",
                encoding="utf-8",
            )
            release = root / "reference"
            release.mkdir()
            launcher = root / "launcher.exe"
            launcher.write_bytes(b"launcher")
            target_parent = root / "candidates"

            result = preflight.run_isolated_assembly(
                root, release, launcher, target_parent
            )

        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.target.parent, target_parent)
        self.assertIn("-TargetDir", result.command)
        self.assertIn(str(result.target), result.command)
        self.assertIn("assembly stdout", result.stdout)
        self.assertIn("assembly stderr", result.stderr)

    def test_candidate_imports_acquisition_without_missing_module(self) -> None:
        preflight = self._preflight()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            self._write_minimal_candidate(candidate)

            real_run = subprocess.run

            def run_with_synthetic_self_test(command, *args, **kwargs):
                if Path(command[0]).resolve() == (
                    candidate / "AFP_Integrated_System_Modular.exe"
                ).resolve():
                    return subprocess.CompletedProcess(command, 0, "self-test ok", "")
                return real_run(command, *args, **kwargs)

            with patch.object(
                preflight.subprocess,
                "run",
                side_effect=run_with_synthetic_self_test,
            ):
                issues = preflight.validate_candidate_runtime(candidate)

        self.assertEqual(issues, ())

    def test_candidate_self_test_failure_is_reported(self) -> None:
        preflight = self._preflight()
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary)
            self._write_minimal_candidate(candidate)
            real_run = subprocess.run

            def run_with_failed_self_test(command, *args, **kwargs):
                if Path(command[0]).resolve() == (
                    candidate / "AFP_Integrated_System_Modular.exe"
                ).resolve():
                    return subprocess.CompletedProcess(
                        command,
                        9,
                        "self-test stdout",
                        "self-test stderr",
                    )
                return real_run(command, *args, **kwargs)

            with patch.object(
                preflight.subprocess,
                "run",
                side_effect=run_with_failed_self_test,
            ):
                issues = preflight.validate_candidate_runtime(candidate)

        self.assertTrue(
            any(
                "candidate self-test failed (exit 9)" in issue
                and "self-test stderr" in issue
                for issue in issues
            ),
            issues,
        )

    def test_candidate_manifest_matches_managed_files(self) -> None:
        preflight = self._preflight()
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary)
            self._write_minimal_candidate(candidate)
            legacy = candidate / "app" / "legacy"
            (legacy / "acquisition.py").write_bytes(b"tampered")

            issues = preflight.validate_candidate_runtime(candidate)

        self.assertIn(
            "manifest hash mismatch: app/legacy/acquisition.py",
            issues,
        )

    def test_candidate_requires_all_linked_frontend_assets(self) -> None:
        preflight = self._preflight()
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary)
            self._write_minimal_candidate(
                candidate,
                omit={"app/ui/training.js"},
            )

            issues = preflight.validate_candidate_runtime(candidate)

        self.assertIn("candidate file missing: app/ui/training.js", issues)

    def test_candidate_requires_local_direct_mode_adapter(self) -> None:
        preflight = self._preflight()
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary)
            self._write_minimal_candidate(
                candidate,
                omit={"app/legacy/local_direct_acquisition.py"},
            )

            issues = preflight.validate_candidate_runtime(candidate)

        self.assertIn(
            "candidate file missing: app/legacy/local_direct_acquisition.py",
            issues,
        )

    def test_candidate_manifest_rejects_path_outside_candidate(self) -> None:
        preflight = self._preflight()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "candidate"
            self._write_minimal_candidate(candidate)
            outside = root / "outside.txt"
            outside.write_bytes(b"outside")
            digest = __import__("hashlib").sha256(outside.read_bytes()).hexdigest().upper()
            with (candidate / "SHA256SUMS.txt").open("a", encoding="utf-8") as manifest:
                manifest.write(f"{digest} *../outside.txt\n")

            issues = preflight.validate_candidate_runtime(candidate)

        self.assertIn("manifest path escapes candidate: ../outside.txt", issues)

    def test_authoritative_acquisition_keeps_fast_interface_observable(self) -> None:
        source = self.repo / "visualization_app"
        sys.path.insert(0, str(source))
        self.addCleanup(lambda: sys.path.remove(str(source)))
        with patch.dict(sys.modules, {"pandas": types.ModuleType("pandas")}):
            acquisition = importlib.import_module("acquisition")
        self.addCleanup(lambda: sys.modules.pop("acquisition", None))
        release_slow = threading.Event()

        class FakeDriver:
            def __init__(self, endpoint: str, *_args, **_kwargs) -> None:
                self.endpoint = endpoint

            def open(self) -> None:
                return None

            def read_sample(self):
                if self.endpoint == "slow":
                    release_slow.wait(2.0)
                    return {"压力": 1.0}
                return {"温度": 42.0}

            def close(self) -> None:
                release_slow.set()

        configs = [
            {"id": "slow", "driver": "serial_json", "endpoint": "slow"},
            {"id": "fast", "driver": "serial_json", "endpoint": "fast"},
        ]
        driver = acquisition.MultiInterfaceDriver(
            configs,
            ["温度", "压力"],
            assignments={"slow": ["压力"], "fast": ["温度"]},
        )
        started = time.monotonic()
        try:
            with patch.object(acquisition, "SerialJsonDriver", FakeDriver):
                driver.open()
                sample = None
                while time.monotonic() - started < 0.5:
                    sample = driver.read_sample()
                    if sample and sample.get("温度") == 42.0:
                        break
                    time.sleep(0.01)
        finally:
            release_slow.set()
            driver.close()

        self.assertIsNotNone(sample)
        self.assertEqual(sample.get("温度"), 42.0)
        self.assertLess(time.monotonic() - started, 0.75)

    def test_authoritative_build_driver_keeps_legacy_single_driver_fallback(self) -> None:
        source = self.repo / "visualization_app"
        sys.path.insert(0, str(source))
        self.addCleanup(lambda: sys.path.remove(str(source)))
        with patch.dict(sys.modules, {"pandas": types.ModuleType("pandas")}):
            acquisition = importlib.import_module("acquisition")
        self.addCleanup(lambda: sys.modules.pop("acquisition", None))
        config = types.SimpleNamespace(
            acquisition_mode="real",
            interfaces=[],
            driver="serial_json",
            endpoint="COM9",
            baudrate=9600,
            selected_sensors=[],
            source_file="",
            interface_channel_assignments={},
        )

        driver = acquisition.build_driver(config)

        self.assertIsInstance(driver, acquisition.SerialJsonDriver)
        self.assertEqual(driver.endpoint, "COM9")

    def test_authoritative_multi_interface_open_propagates_worker_failure(self) -> None:
        source = self.repo / "visualization_app"
        sys.path.insert(0, str(source))
        self.addCleanup(lambda: sys.path.remove(str(source)))
        with patch.dict(sys.modules, {"pandas": types.ModuleType("pandas")}):
            acquisition = importlib.import_module("acquisition")
        self.addCleanup(lambda: sys.modules.pop("acquisition", None))

        class FailingDriver:
            def __init__(self, *_args, **_kwargs) -> None:
                self.closed = False

            def open(self) -> None:
                raise RuntimeError("device-open-failed")

            def close(self) -> None:
                self.closed = True

        driver = acquisition.MultiInterfaceDriver(
            [{"id": "broken", "driver": "serial_json", "endpoint": "COM9"}],
            ["温度"],
            assignments={"broken": ["温度"]},
        )

        with patch.object(acquisition, "SerialJsonDriver", FailingDriver):
            with self.assertRaisesRegex(RuntimeError, "device-open-failed"):
                driver.open()

        self.assertEqual(driver.workers, [])
        self.assertEqual(driver.drivers, [])

    def test_authoritative_acquisition_assembles_quality_bearing_real_frame(self) -> None:
        source = self.repo / "visualization_app"
        sys.path.insert(0, str(source))
        self.addCleanup(lambda: sys.path.remove(str(source)))
        with patch.dict(sys.modules, {"pandas": types.ModuleType("pandas")}):
            acquisition = importlib.import_module("acquisition")
        self.addCleanup(lambda: sys.modules.pop("acquisition", None))
        core = importlib.import_module("real_acquisition")
        self.addCleanup(lambda: sys.modules.pop("real_acquisition", None))

        with tempfile.TemporaryDirectory() as temporary:
            manager = acquisition.AcquisitionManager(Path(temporary) / "captures")
            config = acquisition.AcquisitionConfig(
                acquisition_mode="real",
                dataset_schema="new_collection_v11_3",
                driver="serial_json",
                interfaces=[{
                    "id": "fast",
                    "enabled": True,
                    "driver": "serial_json",
                    "endpoint": "COM1",
                    "role": "custom",
                    "physical_interface_id": "serial:COM1",
                    "physical_port_id": "COM1",
                    "physical_interface_kind": "serial",
                    "physical_verified": True,
                    "channel_map": {"temperature": "温度"},
                }],
                interface_channel_assignments={"fast": ["温度"]},
                selected_sensors=["温度"],
                prediction_sensors=["温度"],
                sample_rate_hz=10.0,
            )
            driver = acquisition.MultiInterfaceDriver([], ["温度"])
            driver.cache.publish(
                core.ChannelSample(
                    interface_id="fast",
                    channel_name="温度",
                    value=42.0,
                    received_monotonic=time.monotonic(),
                    source_sequence=1,
                )
            )
            manager.config = config
            manager.driver = driver
            thread = threading.Thread(target=manager._run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 0.5
            while manager.total_sample_count < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            manager.stop_event.set()
            thread.join(1.0)

        self.assertGreaterEqual(manager.total_sample_count, 1)
        self.assertEqual(manager.rows[0]["温度"], 42.0)
        self.assertEqual(
            manager.frame_quality[0]["温度"],
            "measured_new",
        )


if __name__ == "__main__":
    unittest.main()
