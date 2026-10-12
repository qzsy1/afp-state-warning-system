from __future__ import annotations

import json
import unittest

from visualization_app.mysql_storage import (
    REQUIRED_SCHEMA_OBJECTS,
    MySQLCaptureStore,
    MySQLSettings,
)
from visualization_app.pre_sensor_preflight import (
    ExternalEnvironmentBlocked,
    probe_mysql_readiness,
)


class _Cursor:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.last = ""

    def execute(self, sql, params=None) -> None:
        self.last = " ".join(str(sql).split()).lower()
        if self.last.startswith("insert into afp_mysql_upload_log"):
            self.connection.pending_probe = True

    def fetchone(self):
        if "count(*)" in self.last:
            return (1 if self.connection.pending_probe else 0,)
        return (1,)

    def fetchall(self):
        if "information_schema.tables" in self.last:
            return [(name,) for name in REQUIRED_SCHEMA_OBJECTS]
        return []

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self) -> None:
        self.pending_probe = False
        self.rollback_calls = 0

    def cursor(self):
        return _Cursor(self)

    def rollback(self) -> None:
        self.rollback_calls += 1
        self.pending_probe = False

    def close(self) -> None:
        pass


class _Store(MySQLCaptureStore):
    def __init__(self) -> None:
        super().__init__(
            MySQLSettings(
                enabled=True,
                host="target-db",
                user="afp",
                password="never-report-this",
                database="afp_test",
            )
        )
        self.connection = _Connection()

    def _connect(self, database=None):
        return "fake-target", self.connection


class MySQLReadinessProbeTests(unittest.TestCase):
    def test_staged_preflight_proves_schema_write_and_rollback_without_residue(self) -> None:
        store = _Store()
        result = store.preflight(require_schema=True, write_test=True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stage"], "ready")
        self.assertTrue(result["schema_ready"])
        self.assertTrue(result["write_test"])
        self.assertTrue(result["rollback_verified"])
        self.assertEqual(store.connection.rollback_calls, 1)
        self.assertFalse(store.connection.pending_probe)
        self.assertEqual(
            result["stages"],
            {"connect": True, "schema": True, "write": True, "rollback": True},
        )
        self.assertNotIn("never-report-this", json.dumps(result))

    def test_target_profile_is_required_and_simulation_cannot_satisfy_it(self) -> None:
        with self.assertRaises(ExternalEnvironmentBlocked):
            probe_mysql_readiness({"mysql_profile": None})
        with self.assertRaisesRegex(ValueError, "target environment"):
            probe_mysql_readiness(
                {
                    "mysql_profile": {
                        "host": "simulated",
                        "user": "test",
                        "database": "afp_test",
                        "password": "hidden",
                    },
                    "mysql_evidence_tier": "simulation",
                }
            )

    def test_probe_reports_sanitized_target_stages_and_failure_stage(self) -> None:
        profile = {
            "host": "target-db",
            "port": 3306,
            "user": "afp",
            "database": "afp_test",
            "password": "never-report-this",
        }
        outcome = probe_mysql_readiness(
            {
                "mysql_profile": profile,
                "mysql_evidence_tier": "target_environment",
                "mysql_store_factory": lambda _settings: _Store(),
            }
        )
        rendered = json.dumps(outcome.details)
        self.assertEqual(outcome.status, "passed")
        self.assertNotIn(profile["password"], rendered)
        self.assertEqual(outcome.details["host"], "target-db")

        class FailedStore:
            def preflight(self, **_kwargs):
                return {"ok": False, "stage": "schema", "error": "denied"}

        with self.assertRaisesRegex(ValueError, "schema"):
            probe_mysql_readiness(
                {
                    "mysql_profile": profile,
                    "mysql_evidence_tier": "target_environment",
                    "mysql_store_factory": lambda _settings: FailedStore(),
                }
            )


if __name__ == "__main__":
    unittest.main()
