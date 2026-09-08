from __future__ import annotations

import unittest
from unittest import mock

from acquisition import AcquisitionConfig
from app import validate_read_only_mysql_query
from mysql_storage import MySQLCaptureStore, MySQLSettings, process_condition_key


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple | None]] = []
        self._rows: list[tuple] = []

    def execute(self, sql, parameters=None) -> None:
        self.executed.append((str(sql), parameters))
        upper = str(sql).upper()
        if "INFORMATION_SCHEMA.TABLES" in upper:
            from mysql_storage import REQUIRED_SCHEMA_OBJECTS
            self._rows = [(name,) for name in sorted(REQUIRED_SCHEMA_OBJECTS)]
        elif "COUNT(*)" in upper:
            self._rows = [(0,)]
        elif upper.strip().startswith("SELECT 1"):
            self._rows = [(1,)]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def close(self) -> None:
        pass


class _FakeConnection:
    def __init__(self) -> None:
        self.cursor_instance = _FakeCursor()
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def close(self) -> None:
        self.closed = True


class MySQLIdentityTests(unittest.TestCase):
    def test_remote_settings_accept_host_and_mask_secret(self) -> None:
        settings = MySQLSettings.from_mapping({
            "mysql_enabled": "true",
            "mysql_host": "192.168.10.25",
            "mysql_port": "3307",
            "mysql_user": "afp_app",
            "mysql_password": "secret",
            "mysql_database": "afp_remote",
        })
        self.assertEqual(settings.host, "192.168.10.25")
        self.assertEqual(settings.port, 3307)
        public = settings.public_dict()
        self.assertNotIn("password", public)
        self.assertTrue(public["password_configured"])

    def test_verify_existing_schema_does_not_run_ddl(self) -> None:
        settings = MySQLSettings(enabled=True, host="10.0.0.8", database="afp_remote")
        store = MySQLCaptureStore(settings)
        connection = _FakeConnection()
        with mock.patch.object(store, "_connect", return_value=("fake", connection)):
            result = store.verify_connection(require_schema=True)
        self.assertTrue(result["ok"])
        self.assertTrue(connection.closed)
        statements = "\n".join(sql for sql, _ in connection.cursor_instance.executed).upper()
        self.assertNotIn("CREATE ", statements)
        self.assertNotIn("ALTER ", statements)

    def test_browser_mysql_query_rejects_mutation(self) -> None:
        self.assertEqual(
            validate_read_only_mysql_query("SELECT * FROM afp_flat_all"),
            "SELECT * FROM afp_flat_all",
        )
        for unsafe in (
            "DELETE FROM afp_layer",
            "WITH x AS (SELECT 1) DELETE FROM afp_layer",
            "SELECT * FROM afp_flat_all INTO OUTFILE 'x.csv'",
            "SELECT 1; DROP TABLE afp_layer",
        ):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                validate_read_only_mysql_query(unsafe)

    def test_legacy_process_parameters_change_condition_and_specimen_keys(self) -> None:
        first = AcquisitionConfig(
            dataset_schema="legacy_original", specimen_id="S1", replicate=1,
            p=600, v=100, pr=600,
        )
        second = AcquisitionConfig(
            dataset_schema="legacy_original", specimen_id="S1", replicate=1,
            p=600, v=110, pr=600,
        )
        self.assertNotEqual(process_condition_key(first), process_condition_key(second))
        self.assertNotEqual(
            MySQLCaptureStore.specimen_key(first),
            MySQLCaptureStore.specimen_key(second),
        )

    def test_new_process_parameters_change_condition_and_specimen_keys(self) -> None:
        first = AcquisitionConfig(
            dataset_schema="new_collection_v11_3", specimen_id="S1", replicate=1,
            initial_compaction_force_N=400, placement_speed_mm_s=80,
            pid_angle_deg=5, temperature_setpoint_C=360,
        )
        second = AcquisitionConfig(
            dataset_schema="new_collection_v11_3", specimen_id="S1", replicate=1,
            initial_compaction_force_N=410, placement_speed_mm_s=80,
            pid_angle_deg=5, temperature_setpoint_C=360,
        )
        self.assertNotEqual(process_condition_key(first), process_condition_key(second))
        self.assertNotEqual(
            MySQLCaptureStore.specimen_key(first),
            MySQLCaptureStore.specimen_key(second),
        )

    def test_replicate_changes_specimen_but_not_condition_key(self) -> None:
        first = AcquisitionConfig(
            dataset_schema="legacy_original", specimen_id="S1", replicate=1,
            p=600, v=100, pr=600,
        )
        second = AcquisitionConfig(
            dataset_schema="legacy_original", specimen_id="S1", replicate=2,
            p=600, v=100, pr=600,
        )
        self.assertEqual(process_condition_key(first), process_condition_key(second))
        self.assertNotEqual(
            MySQLCaptureStore.specimen_key(first),
            MySQLCaptureStore.specimen_key(second),
        )


if __name__ == "__main__":
    unittest.main()
