from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mysql_storage import MySQLCaptureStore, MySQLSettings  # noqa: E402
from remote_mysql_setup import classify_mysql_error  # noqa: E402


def _settings() -> MySQLSettings:
    return MySQLSettings(
        enabled=True,
        host="127.0.0.1",
        port=3306,
        user="root",
        password="secret",
        database="afp_state_warning",
    )


class MySQLDiagnosticsTests(unittest.TestCase):
    def test_missing_driver_is_actionable(self) -> None:
        result = classify_mysql_error("未安装 MySQL 驱动，请安装 mysql-connector-python")
        self.assertEqual(result["category"], "driver")

    def test_preflight_reports_port_unreachable_without_calling_schema(self) -> None:
        store = MySQLCaptureStore(_settings())

        def fail_connect(database=None):
            raise ConnectionRefusedError("2003 Can't connect to MySQL server")

        store._connect = fail_connect  # type: ignore[method-assign]
        result = store.preflight()

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "connect")
        self.assertEqual(result["error_detail"]["category"], "network")
        self.assertEqual(result["host"], "127.0.0.1")
        self.assertEqual(result["port"], 3306)

    def test_preflight_reports_missing_schema_objects(self) -> None:
        connection = Mock()
        cursor = Mock()
        connection.cursor.return_value = cursor
        cursor.fetchall.return_value = []
        cursor.fetchone.return_value = (1,)
        store = MySQLCaptureStore(_settings())
        store._connect = lambda database=None: ("pymysql", connection)  # type: ignore[method-assign]

        result = store.preflight()

        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "schema")
        self.assertFalse(result["schema_ready"])
        self.assertTrue(result["missing_objects"])

    def test_preflight_write_test_rolls_back(self) -> None:
        connection = Mock()
        cursor = Mock()
        connection.cursor.return_value = cursor
        cursor.fetchone.return_value = (1,)
        cursor.fetchall.return_value = []
        store = MySQLCaptureStore(_settings())
        store._connect = lambda database=None: ("pymysql", connection)  # type: ignore[method-assign]

        result = store.preflight(write_test=True, require_schema=False)

        self.assertTrue(result["ok"])
        self.assertTrue(result["write_test"])
        connection.rollback.assert_called_once_with()
        self.assertTrue(
            any("afp_mysql_upload_log" in str(call.args[0]) for call in cursor.execute.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
