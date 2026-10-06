from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from edge_capture import RemoteAcquisitionRegistry
from server_capture_journal import ServerCaptureJournal, TargetMySQLSaveCoordinator
from server_target_mysql import ServerTargetProfiles


def sample(sequence, values, *, final=False, count=None):
    return {
        "capture_uuid": "transport-capture-a", "sequence": sequence,
        "rows": [{"温度": value} for value in values],
        "timestamps": [float(value) for value in values],
        "status": {
            "running": not final, "finalization_complete": final,
            "sample_count": count if count is not None else sequence + len(values),
            "config": {"capture_uuid": "specimen-a", "specimen_id": "specimen-a",
                       "run_id": "specimen-a-L1", "layer": 0,
                       "schema_sensors": ["温度"], "process_columns": []},
        },
    }


class ServerCaptureJournalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.journal = ServerCaptureJournal(Path(self.temp.name) / "journal.sqlite3", max_capture_bytes=300000)
        self.mirrors = RemoteAcquisitionRegistry(max_rows=24)
        self.journal.arm("session-a", "selection-a")

    def tearDown(self):
        self.journal.close()
        self.temp.cleanup()

    def test_ack_is_idempotent_and_final_status_waits_for_all_rows(self):
        first = sample(0, [0, 1])
        self.assertTrue(self.journal.ingest("session-a", first, self.mirrors)["ok"])
        duplicate = self.journal.ingest("session-a", first, self.mirrors)
        self.assertTrue(duplicate["duplicate"])
        self.assertFalse(self.journal.ingest("session-a", sample(2, [2]), self.mirrors)["ok"])
        self.assertFalse(self.journal.ingest("session-b", sample(0, [7]), self.mirrors)["ok"])
        self.assertTrue(self.journal.ingest("session-a", sample(1, [2], final=True, count=3), self.mirrors)["ok"])
        record = self.journal.ready_capture("session-a", "transport-capture-a")
        self.assertEqual(record.row_count, 3)
        self.assertEqual([row["温度"] for row in record.rows], [0, 1, 2])
        self.assertEqual(record.target_config_id, "selection-a")

    def test_more_than_mirror_capacity_remains_in_full_journal(self):
        for index in range(55):
            ack = self.journal.ingest(
                "session-a", sample(index, [index], final=index == 54, count=index + 1), self.mirrors
            )
            self.assertTrue(ack["ok"], ack)
        record = self.journal.ready_capture("session-a", "transport-capture-a")
        self.assertEqual(record.row_count, 55)
        self.assertEqual(len(self.mirrors.for_session("session-a").numeric_matrix()[0]), 24)
        self.assertEqual([row["温度"] for row in record.rows], list(range(55)))

    def test_fifty_thousand_and_one_rows_are_retained_beyond_live_mirror_limit(self):
        journal = ServerCaptureJournal(Path(self.temp.name) / "large-journal.sqlite3", max_capture_bytes=32 * 1024 * 1024)
        mirrors = RemoteAcquisitionRegistry(max_rows=50_000)
        journal.arm("large-session", "selection-a")
        total = 50_001
        batch_size = 1_000
        for sequence, start in enumerate(range(0, total, batch_size)):
            values = list(range(start, min(start + batch_size, total)))
            ack = journal.ingest(
                "large-session",
                sample(sequence, values, final=start + len(values) == total, count=start + len(values)),
                mirrors,
            )
            self.assertTrue(ack["ok"], ack)
        record = journal.ready_capture("large-session", "transport-capture-a")
        self.assertEqual(record.row_count, total)
        self.assertEqual(len(mirrors.for_session("large-session").numeric_matrix()[0]), 50_000)
        recovered = iter(record.rows)
        self.assertEqual(next(recovered)["温度"], 0)
        for last in recovered:
            pass
        self.assertEqual(last["温度"], 50_000)

    def test_quota_refuses_batch_without_ack_or_mirror_change(self):
        self.journal.max_capture_bytes = 20
        rejected = self.journal.ingest("session-a", sample(0, [1]), self.mirrors)
        self.assertFalse(rejected["ok"])
        self.assertEqual(self.mirrors.for_session("session-a").numeric_matrix()[0], [])

    def test_incomplete_final_count_does_not_report_ready(self):
        self.journal.ingest("session-a", sample(0, [1], final=True, count=2), self.mirrors)
        self.assertIsNone(self.journal.ready_capture("session-a", "transport-capture-a"))

    def test_capture_keeps_safe_start_config_and_reconstructs_timestamps(self):
        self.journal.disarm("session-a")
        self.journal.arm(
            "session-a", "selection-a",
            config={"specimen_id": "specimen-a", "selected_sensors": ["温度"],
                    "mysql_password": "must-not-persist"},
            target={"scope": "server_target", "host": "db.example", "database": "afp"},
        )
        self.assertTrue(self.journal.ingest(
            "session-a", sample(0, [12], final=True, count=1), self.mirrors
        )["ok"])
        record = self.journal.ready_capture("session-a", "transport-capture-a")
        self.assertEqual(record.config["specimen_id"], "specimen-a")
        self.assertNotIn("mysql_password", str(record.config))
        self.assertEqual(list(record.rows)[0]["timestamp_unix"], 12.0)
        status = self.journal.capture_status("session-a", "transport-capture-a")
        self.assertEqual(status["state"], "ready")
        self.assertNotIn("must-not-persist", str(status))

    def test_start_intent_binds_only_the_helper_returned_capture_uuid(self):
        self.journal.disarm("session-a")
        self.journal.arm_start(
            "session-a", "request-a", "selection-a", config={"specimen_id": "specimen-a"}
        )
        self.assertFalse(self.journal.ingest("session-a", sample(0, [1]), self.mirrors)["ok"])
        self.assertTrue(self.journal.bind_start_result(
            "session-a", "request-a", {"running": True, "capture_uuid": "transport-capture-a"}
        ))
        self.assertTrue(self.journal.ingest("session-a", sample(0, [1]), self.mirrors)["ok"])
        wrong = sample(0, [2])
        wrong["capture_uuid"] = "transport-capture-b"
        self.assertFalse(self.journal.ingest("session-a", wrong, self.mirrors)["ok"])

    def test_server_target_save_uses_bound_profile_and_keeps_failed_capture_retryable(self):
        profiles = ServerTargetProfiles(lambda: {"target": {
            "host": "db.example", "port": 3306, "user": "afp_app",
            "database": "afp", "password": "server-only-secret",
        }})
        selection = profiles.resolve("session-a", {
            "mysql_enabled": True, "mysql_host": "db.example", "mysql_port": 3306,
            "mysql_user": "afp_app", "mysql_database": "afp", "mysql_password": "",
        })
        self.journal.disarm("session-a")
        self.journal.arm(
            "session-a", selection.config_id,
            config={"dataset_schema": "new_collection_v11_3", "driver": "simulator",
                    "processing_mode": "capture_only", "selected_sensors": ["温度"],
                    "specimen_id": "specimen-a"},
            target=selection.public(),
        )
        self.assertTrue(self.journal.ingest(
            "session-a", sample(0, [1], final=True, count=1), self.mirrors
        )["ok"])
        calls = []

        class Store:
            def __init__(self, settings):
                self.settings = settings

            def save_layer(self, config, **kwargs):
                calls.append((self.settings, config, list(kwargs["rows"])))
                return {"ok": len(calls) > 1, "saved_rows": 1, "error": "temporary"}

        coordinator = TargetMySQLSaveCoordinator(self.journal, profiles, Store)
        first = coordinator.save_now("session-a", "transport-capture-a")
        self.assertFalse(first["ok"])
        self.assertEqual(self.journal.capture_status("session-a", "transport-capture-a")["state"], "failed")
        second = coordinator.save_now("session-a", "transport-capture-a")
        self.assertTrue(second["ok"])
        state = self.journal.capture_status("session-a", "transport-capture-a")
        self.assertEqual(state["state"], "saved")
        self.assertEqual(calls[0][0].password, "server-only-secret")
        self.assertEqual(calls[0][1].capture_uuid, "transport-capture-a")
        self.assertEqual(calls[0][2][0]["timestamp_unix"], 1.0)
        self.assertNotIn("server-only-secret", str(state))

    def test_target_write_1045_is_exposed_as_safe_actionable_diagnostic(self):
        profiles = ServerTargetProfiles(lambda: {"target": {
            "host": "db.example", "port": 3306, "user": "afp_app",
            "database": "afp", "password": "server-only-secret",
        }})
        selection = profiles.resolve("session-a", {
            "mysql_enabled": True, "mysql_host": "db.example", "mysql_port": 3306,
            "mysql_user": "afp_app", "mysql_database": "afp", "mysql_password": "",
        })
        self.journal.disarm("session-a")
        self.journal.arm(
            "session-a", selection.config_id,
            config={"dataset_schema": "new_collection_v11_3", "driver": "simulator",
                    "processing_mode": "capture_only", "selected_sensors": ["温度"],
                    "specimen_id": "specimen-a"},
            target=selection.public(),
        )
        self.assertTrue(self.journal.ingest(
            "session-a", sample(0, [1], final=True, count=1), self.mirrors
        )["ok"])

        class Store:
            def __init__(self, _settings):
                pass

            def save_layer(self, *_args, **_kwargs):
                return {"ok": False, "saved_rows": 0,
                        "error": "[1045] Access denied for user 'afp_app'@'DESKTOP-410SFVI'"}

        result = TargetMySQLSaveCoordinator(self.journal, profiles, Store).save_now(
            "session-a", "transport-capture-a"
        )
        self.assertEqual(result["error_detail"]["category"], "authentication")
        status = self.journal.capture_status("session-a", "transport-capture-a")
        self.assertEqual(status["error_detail"]["code"], "1045")
        self.assertIn("用户@来源主机", status["error_detail"]["message"])
        self.assertNotIn("server-only-secret", str(status))


if __name__ == "__main__":
    unittest.main()
