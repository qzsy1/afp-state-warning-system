from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch


RUNTIME_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = RUNTIME_ROOT / "app" / "core"
for path in (CORE_DIR, RUNTIME_ROOT / "app"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from contracts import PredictionFrame, SampleFrame  # noqa: E402
from context import RuntimeContext  # noqa: E402
import bootstrap  # noqa: E402
from update_manager import UpdateManager  # noqa: E402


class ContractTests(unittest.TestCase):
    def test_sample_contract_rejects_empty_channels(self) -> None:
        sample = SampleFrame("2026-09-01T00:00:00Z", "new", "C1", 1, 1, 0, {})
        with self.assertRaises(ValueError):
            sample.validate()

    def test_prediction_contract_enforces_horizon(self) -> None:
        frame = PredictionFrame(
            "2026-09-01T00:00:00Z", 24, 3, "model", ["温度"], ["温度"], {"温度": [1.0, 2.0]}
        )
        with self.assertRaises(ValueError):
            frame.validate()


class BuildScriptContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compatibility = (RUNTIME_ROOT / "build_modular_app.ps1").read_text(encoding="utf-8-sig")
        cls.launcher = (RUNTIME_ROOT / "build_launcher.ps1").read_text(encoding="utf-8-sig")
        cls.assembly = (RUNTIME_ROOT / "assemble_modular_delivery.ps1").read_text(encoding="utf-8-sig")

    def test_build_responsibilities_are_split_behind_compatible_entrypoint(self) -> None:
        self.assertIn('"build_launcher.ps1"', self.compatibility)
        self.assertIn('"assemble_modular_delivery.ps1"', self.compatibility)
        self.assertNotIn("PyInstaller", self.compatibility)
        self.assertNotIn("SHA256SUMS.txt", self.compatibility)
        self.assertNotIn("$launcherSource = &", self.compatibility)
        for parameter in (
            "PythonExecutable", "ReferenceRelease", "TargetDir", "ApplicationVersion",
            "SkipExecutableBuild", "AllowExistingTarget", "ExistingExecutable",
        ):
            self.assertIn(f"${parameter}", self.compatibility)

    def test_integrity_manifest_excludes_nested_app_runtime_state(self) -> None:
        self.assertIn('$nestedRuntime = $segments.Count -ge 2 -and $segments[0] -eq "app" -and $segments[1] -eq "runtime"', self.assembly)
        self.assertIn("-not $nestedRuntime", self.assembly)

    def test_build_keeps_primary_and_legacy_static_assets_in_sync(self) -> None:
        self.assertIn('$legacyStaticTarget = Join-Path $legacyTarget "static"', self.assembly)
        self.assertIn('Copy-Item -LiteralPath $_.FullName -Destination $legacyStaticTarget -Recurse -Force', self.assembly)
        self.assertIn('Copy-Item -LiteralPath $_.FullName -Destination $uiTarget -Recurse -Force', self.assembly)

    def test_build_keeps_pywebview_windows_runtime_dependencies(self) -> None:
        self.assertIn('"--hidden-import", "webview.platforms.winforms"', self.launcher)
        self.assertNotIn('"--exclude-module", "clr_loader"', self.launcher)
        self.assertNotIn('"--exclude-module", "pythonnet"', self.launcher)

    def test_build_places_launcher_at_delivery_root(self) -> None:
        self.assertIn("Get-ChildItem -LiteralPath $LauncherSourceDir -Force", self.assembly)
        self.assertNotIn("Copy-Item -LiteralPath $LauncherSourceDir -Destination $TargetDir -Recurse", self.assembly)

    def test_build_copies_simulation_and_diagnosis_runtime_modules(self) -> None:
        self.assertIn('"diagnosis_jobs.py"', self.assembly)
        self.assertIn('"simulation_packages.py"', self.assembly)
        self.assertIn('Join-Path $LegacySource "simulation_packages"', self.assembly)

    def test_build_copies_real_acquisition_from_authoritative_source(self) -> None:
        self.assertIn('"real_acquisition.py"', self.assembly)
        self.assertTrue((RUNTIME_ROOT.parent / "visualization_app" / "real_acquisition.py").is_file())
        self.assertNotIn(
            'Join-Path $referenceLegacy "real_acquisition.py"',
            self.assembly,
        )

    def test_build_stamps_requested_version_into_runtime_config(self) -> None:
        self.assertIn("$runtimeConfig.application_version = $ApplicationVersion", self.assembly)

    def test_build_copies_release_operational_scripts(self) -> None:
        for name in (
            "install_local_helper_autostart.ps1",
            "public_tunnel_watchdog.ps1",
            "tailscale_funnel_watchdog.ps1",
        ):
            self.assertIn(f'"{name}"', self.assembly)

    def test_build_does_not_embed_external_legacy_entrypoint(self) -> None:
        self.assertNotIn('"--hidden-import", "interface_agent"', self.launcher)

    def test_delivery_assembly_reuses_existing_launcher_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference"
            (reference / "_internal" / "data").mkdir(parents=True)
            (reference / "_internal" / "data" / "reference.txt").write_text("data", encoding="utf-8")
            (reference / "models").mkdir()
            (reference / "models" / "model.txt").write_text("model", encoding="utf-8")

            launcher = root / "trusted-launcher"
            (launcher / "_internal").mkdir(parents=True)
            executable = launcher / "AFP_Integrated_System_Modular.exe"
            executable.write_bytes(b"trusted-launcher-bytes")
            (launcher / "_internal" / "runtime.txt").write_text("runtime", encoding="utf-8")
            (launcher / "VERSION.json").write_text(
                json.dumps({"application_version": "2.0.4", "launcher_version": "2.0.4"}),
                encoding="utf-8",
            )
            expected_hash = hashlib.sha256(executable.read_bytes()).hexdigest()

            target = root / "delivery"
            completed = subprocess.run(
                [
                    "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(RUNTIME_ROOT / "assemble_modular_delivery.ps1"),
                    "-ReferenceRelease", str(reference),
                    "-TargetDir", str(target),
                    "-ApplicationVersion", "test-split",
                    "-ExistingExecutable", str(executable),
                ],
                cwd=RUNTIME_ROOT.parent,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
            assembled = target / "AFP_Integrated_System_Modular.exe"
            self.assertEqual(hashlib.sha256(assembled.read_bytes()).hexdigest(), expected_hash)
            self.assertTrue((target / "_internal" / "runtime.txt").is_file())
            self.assertTrue((target / "config" / "runtime.json").is_file())
            self.assertIn("AFP_Integrated_System_Modular.exe", (target / "SHA256SUMS.txt").read_text(encoding="utf-8-sig"))
            version = json.loads((target / "VERSION.json").read_text(encoding="utf-8-sig"))
            self.assertEqual(version["application_version"], "test-split")
            self.assertEqual(version["launcher_version"], "2.0.4")


class UpdateTests(unittest.TestCase):
    def test_patch_install_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "runtime.json").write_text(
                json.dumps({"api_version": "2.0", "paths": {}}), encoding="utf-8"
            )
            target = root / "app" / "modules" / "demo" / "value.txt"
            target.parent.mkdir(parents=True)
            target.write_text("old", encoding="utf-8")
            payload = b"new"
            archive = root / "update.zip"
            manifest = {
                "api_version": "2.0",
                "patch_version": "test",
                "files": [
                    {
                        "path": "app/modules/demo/value.txt",
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("patch_manifest.json", json.dumps(manifest))
                package.writestr("payload/app/modules/demo/value.txt", payload)
            context = RuntimeContext.load(root)
            context.prepare()
            updater = UpdateManager(context)
            record = updater.install(archive)
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            updater.rollback(Path(record["backup"]))
            self.assertEqual(target.read_text(encoding="utf-8"), "old")

    def test_failed_multi_file_patch_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "runtime.json").write_text(
                json.dumps({"api_version": "2.0", "paths": {}}), encoding="utf-8"
            )
            first = root / "app" / "modules" / "demo" / "first.txt"
            first.parent.mkdir(parents=True)
            first.write_text("original", encoding="utf-8")
            archive = root / "broken_update.zip"
            valid_payload = b"replacement"
            invalid_payload = b"unexpected"
            manifest = {
                "api_version": "2.0",
                "patch_version": "broken-test",
                "files": [
                    {
                        "path": "app/modules/demo/first.txt",
                        "sha256": hashlib.sha256(valid_payload).hexdigest(),
                    },
                    {
                        "path": "app/modules/demo/second.txt",
                        "sha256": hashlib.sha256(b"expected").hexdigest(),
                    },
                ],
            }
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("patch_manifest.json", json.dumps(manifest))
                package.writestr("payload/app/modules/demo/first.txt", valid_payload)
                package.writestr("payload/app/modules/demo/second.txt", invalid_payload)
            context = RuntimeContext.load(root)
            context.prepare()
            updater = UpdateManager(context)
            with self.assertRaises(ValueError):
                updater.install(archive)
            self.assertEqual(first.read_text(encoding="utf-8"), "original")
            self.assertFalse((first.parent / "second.txt").exists())

    def test_patch_rejects_parent_path_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "runtime.json").write_text(
                json.dumps({"api_version": "2.0", "paths": {}}), encoding="utf-8"
            )
            archive = root / "unsafe_update.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../outside.txt", b"unsafe")
            context = RuntimeContext.load(root)
            context.prepare()
            with self.assertRaises(ValueError):
                UpdateManager(context).install(archive)
            self.assertFalse((root.parent / "outside.txt").exists())


class LaunchTests(unittest.TestCase):
    def test_existing_afp_server_probe_requires_healthy_json(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"status":"ok","version":"2.0"}'
        with patch.object(bootstrap.urllib.request, "urlopen", return_value=response):
            self.assertTrue(
                bootstrap._afp_server_available("http://127.0.0.1:8771/api/health")
            )

        response.read.return_value = b'{"status":"other"}'
        with patch.object(bootstrap.urllib.request, "urlopen", return_value=response):
            self.assertFalse(
                bootstrap._afp_server_available("http://127.0.0.1:8771/api/health")
            )

    def test_existing_server_revision_must_match_current_delivery(self) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = (
            b'{"status":"ok","version":"2.0","runtime_revision":"new-revision"}'
        )
        url = "http://127.0.0.1:8771/api/health"
        with patch.object(bootstrap.urllib.request, "urlopen", return_value=response):
            self.assertTrue(
                bootstrap._afp_server_available(
                    url, expected_revision="new-revision"
                )
            )
            self.assertFalse(
                bootstrap._afp_server_available(
                    url, expected_revision="old-revision"
                )
            )

    def test_runtime_revision_uses_integrity_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest = root / "SHA256SUMS.txt"
            manifest.write_bytes(b"first")
            first = bootstrap._runtime_revision(root)
            manifest.write_bytes(b"second")
            self.assertNotEqual(first, bootstrap._runtime_revision(root))

    def test_stale_listener_stop_is_limited_to_same_executable(self) -> None:
        current = str(Path(sys.executable).resolve())
        matching = MagicMock()
        matching.pid = 41
        matching.exe.return_value = current
        other = MagicMock()
        other.pid = 42
        other.exe.return_value = str(Path(current).with_name("other.exe"))
        fake_psutil = types.SimpleNamespace(
            CONN_LISTEN="LISTEN",
            AccessDenied=RuntimeError,
            NoSuchProcess=LookupError,
            net_connections=MagicMock(
                return_value=[
                    types.SimpleNamespace(laddr=types.SimpleNamespace(port=8770), status="LISTEN", pid=41),
                    types.SimpleNamespace(laddr=types.SimpleNamespace(port=8771), status="LISTEN", pid=42),
                ]
            ),
            Process=lambda pid: matching if pid == 41 else other,
            wait_procs=MagicMock(return_value=([matching], [])),
        )
        with patch.dict(sys.modules, {"psutil": fake_psutil}):
            stopped = bootstrap._stop_stale_local_afp_servers(
                {8770, 8771}, executable=current
            )
        self.assertEqual(stopped, [41])
        matching.terminate.assert_called_once_with()
        other.terminate.assert_not_called()

    def test_launch_uses_configured_fixed_port_and_lan_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "config").mkdir()
            (root / "config" / "runtime.json").write_text(
                json.dumps(
                    {
                        "api_version": "2.0",
                        "paths": {"runtime_dir": "runtime"},
                        "lan_web": {
                            "enabled": True,
                            "bind_host": "0.0.0.0",
                            "port": 8770,
                            "open_desktop_window": True,
                        },
                    }
                ),
                encoding="utf-8",
            )
            context = RuntimeContext.load(root)
            context.prepare()
            fake_server = _FakeServer(8770)
            legacy = types.SimpleNamespace(create_server=MagicMock(return_value=fake_server))
            webview = types.SimpleNamespace(
                create_window=MagicMock(),
                start=MagicMock(side_effect=KeyboardInterrupt),
            )
            with patch.object(bootstrap, "_legacy_module", return_value=legacy), patch.dict(
                sys.modules, {"webview": webview}
            ):
                with self.assertRaises(KeyboardInterrupt):
                    bootstrap.launch(context, MagicMock())
            legacy.create_server.assert_called_once()
            self.assertEqual(
                legacy.create_server.call_args.args[:2], ("0.0.0.0", 8770)
            )
            self.assertTrue(
                webview.create_window.call_args.args[1].endswith("127.0.0.1:8770/")
            )
            status = json.loads(
                (root / "runtime" / "lan_web_status.json").read_text(encoding="utf-8")
            )
            self.assertEqual(status["port"], 8770)


class _FakeServer:
    def __init__(self, port: int) -> None:
        self.server_port = port
        self.service_started_at = time.time()
        self._stopped = threading.Event()

    def serve_forever(self) -> None:
        while not self._stopped.wait(0.01):
            pass

    def shutdown(self) -> None:
        self._stopped.set()

    def server_close(self) -> None:
        self._stopped.set()


if __name__ == "__main__":
    unittest.main()
