from __future__ import annotations

import hashlib
import json
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
