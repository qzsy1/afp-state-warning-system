from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

APP = Path(__file__).resolve().parent
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from acquisition import AcquisitionManager
from app import create_server
from mysql_storage import MySQLSettings


CONFIRMATION = "重置并丢弃"


def recovery_request(manager: AcquisitionManager, *, ids: list[str] | None = None) -> dict:
    state = manager.refresh_persistence_recovery()
    selected = ids if ids is not None else [item["id"] for item in state["files"]]
    return {
        "confirm_discard": True,
        "confirmation": CONFIRMATION,
        "scan_fingerprint": state["scan_fingerprint"],
        "item_ids": selected,
    }


class PersistenceRecoveryDiscardTests(unittest.TestCase):
    def _blocked_root(self, temporary: str) -> tuple[Path, Path, Path, Path, Path]:
        root = Path(temporary)
        records = root / "specimen" / "采集记录"
        records.mkdir(parents=True)
        completed = records / "layer.csv"
        completed.write_text("capture_uuid,value\ncapture-a,1\n", encoding="utf-8")
        config = records / "当前试样会话.json"
        config.write_text('{"capture_uuid":"capture-a"}', encoding="utf-8")
        pending = records / "layer_mysql_target_pending.json"
        pending.write_text(
            json.dumps(
                {
                    "config": {
                        "capture_uuid": "capture-a",
                        "mysql_host": "127.0.0.1",
                        "mysql_port": 3306,
                        "mysql_user": "root",
                        "mysql_database": "afp_test",
                    },
                    "layer_file": str(completed),
                }
            ),
            encoding="utf-8",
        )
        partial = records / "layer.csv.partial"
        partial.write_bytes(b"unfinished")
        return root, completed, config, pending, partial

    def test_status_and_failed_mysql_retry_preserve_every_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, completed, config, pending, partial = self._blocked_root(temporary)
            manager = AcquisitionManager(root)
            before = {path: path.read_bytes() for path in (completed, config, pending, partial)}

            status = manager.status()
            self.assertTrue(status["persistence_recovery"]["blocked"])
            failing_store = SimpleNamespace(
                verify_connection=lambda **_kwargs: {"ok": False, "error": "connection refused"},
                test_connection=lambda: {"ok": False, "error": "connection refused"},
            )
            with patch("acquisition.MySQLCaptureStore", return_value=failing_store):
                result = manager.retry_pending_mysql(
                    MySQLSettings(enabled=True, database="afp_test"), root=root
                )

            self.assertTrue(result["connection_error"])
            self.assertEqual(before, {path: path.read_bytes() for path in before})
            self.assertTrue(manager.status()["persistence_recovery"]["blocked"])

    def test_discard_requires_all_confirmation_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _completed, _config, pending, partial = self._blocked_root(temporary)
            manager = AcquisitionManager(root)
            request = recovery_request(manager)
            for missing in ("confirm_discard", "confirmation", "scan_fingerprint", "item_ids"):
                with self.subTest(missing=missing):
                    candidate = dict(request)
                    candidate.pop(missing)
                    with self.assertRaises(ValueError):
                        manager.discard_persistence_recovery(candidate, actor="local_admin")
                    self.assertTrue(pending.exists())
                    self.assertTrue(partial.exists())

    def test_discard_is_exact_and_preserves_completed_csv_and_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, completed, config, pending, partial = self._blocked_root(temporary)
            completed_before = completed.read_bytes()
            config_before = config.read_bytes()
            manager = AcquisitionManager(root)
            state = manager.refresh_persistence_recovery()
            partial_id = next(item["id"] for item in state["files"] if item["kind"] == "partial")

            result = manager.discard_persistence_recovery(
                recovery_request(manager, ids=[partial_id]), actor="local_admin"
            )

            self.assertEqual(result["processed_count"], 1)
            self.assertFalse(partial.exists())
            self.assertTrue(pending.exists())
            self.assertEqual(completed.read_bytes(), completed_before)
            self.assertEqual(config.read_bytes(), config_before)
            self.assertTrue(result["recovery"]["blocked"])
            self.assertEqual(result["recovery"]["database_pending_retry"], 1)
            self.assertNotIn(str(root), json.dumps(result, ensure_ascii=False))

    def test_audit_write_failure_restores_every_selected_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _completed, _config, pending, partial = self._blocked_root(temporary)
            manager = AcquisitionManager(root)
            request = recovery_request(manager)

            with patch("acquisition._atomic_write_json", side_effect=OSError("audit failed")):
                with self.assertRaises(OSError):
                    manager.discard_persistence_recovery(request, actor="local_admin")

            self.assertTrue(pending.exists())
            self.assertTrue(partial.exists())
            self.assertTrue(manager.status()["persistence_recovery"]["blocked"])

    def test_discard_rejects_active_capture_stale_scan_and_unknown_item(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _completed, _config, pending, partial = self._blocked_root(temporary)
            manager = AcquisitionManager(root)
            request = recovery_request(manager)

            manager.thread = SimpleNamespace(is_alive=lambda: True)
            with self.assertRaises(RuntimeError):
                manager.discard_persistence_recovery(request, actor="local_admin")
            manager.thread = None
            self.assertTrue(pending.exists())
            self.assertTrue(partial.exists())

            stale = dict(request)
            partial.write_bytes(b"changed-after-scan")
            with self.assertRaises(ValueError):
                manager.discard_persistence_recovery(stale, actor="local_admin")
            unknown = recovery_request(manager)
            unknown["item_ids"] = ["unknown"]
            with self.assertRaises(ValueError):
                manager.discard_persistence_recovery(unknown, actor="local_admin")

    def test_reparse_candidate_is_never_eligible_or_processed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            candidate = root / "outside.csv.partial"
            candidate.write_bytes(b"outside")
            with patch(
                "acquisition._path_has_reparse_point",
                side_effect=lambda path, _root: path == candidate,
            ):
                manager = AcquisitionManager(root)
                state = manager.refresh_persistence_recovery()
                self.assertFalse(state["files"][0]["eligible"])
                request = {
                    "confirm_discard": True,
                    "confirmation": CONFIRMATION,
                    "scan_fingerprint": state["scan_fingerprint"],
                    "item_ids": [state["files"][0]["id"]],
                }
                with self.assertRaises(ValueError):
                    manager.discard_persistence_recovery(request, actor="local_admin")
            self.assertEqual(candidate.read_bytes(), b"outside")

    def test_last_blocker_discard_refreshes_gate_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "layer.csv.partial"
            partial.write_bytes(b"unfinished")
            manager = AcquisitionManager(root)
            self.assertEqual(manager.status()["capture_phase"], "finalization_failed")

            result = manager.discard_persistence_recovery(
                recovery_request(manager), actor="local_admin"
            )

            self.assertFalse(result["recovery"]["blocked"])
            status = manager.status()
            self.assertFalse(status["persistence_recovery"]["blocked"])
            self.assertEqual(status["capture_phase"], "idle")
            self.assertTrue(status["finalization_complete"])


class PersistenceRecoveryHttpTests(unittest.TestCase):
    def test_local_admin_http_path_requires_explicit_scope_and_refreshes_status(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            partial = root / "layer.csv.partial"
            partial.write_bytes(b"unfinished")
            manager = AcquisitionManager(root)
            dashboard = SimpleNamespace(acquisition=manager)
            server = create_server(
                "127.0.0.1",
                0,
                {"enabled": True, "urls": []},
                dashboard=dashboard,
                access_context="local_admin",
                ingress_state_file=root / "runtime" / "public-ingress-state.json",
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                state = manager.refresh_persistence_recovery()
                body = {
                    "confirm_discard": True,
                    "confirmation": CONFIRMATION,
                    "scan_fingerprint": state["scan_fingerprint"],
                    "item_ids": [state["files"][0]["id"]],
                }
                connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                connection.request(
                    "POST",
                    "/api/admin/acquisition/recovery-discard",
                    json.dumps(body).encode("utf-8"),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertEqual(response.status, 200, payload)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["recovery"]["blocked"])
            self.assertFalse(partial.exists())


if __name__ == "__main__":
    unittest.main()
