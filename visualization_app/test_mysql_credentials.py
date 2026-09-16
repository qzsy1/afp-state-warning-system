from __future__ import annotations

import unittest
import types
import sys
from unittest import mock

from mysql_storage import MySQLCaptureStore, MySQLSettings


class MySQLCredentialBoundaryTests(unittest.TestCase):
    def test_non_root_account_without_password_is_forwarded_to_connector(self) -> None:
        settings = MySQLSettings(
            enabled=True,
            host="192.168.101.31",
            user="afp_app",
            password="",
        )
        store = MySQLCaptureStore(settings)
        connect = mock.Mock(return_value=mock.sentinel.connection)
        mysql_module = types.ModuleType("mysql")
        connector_module = types.ModuleType("mysql.connector")
        connector_module.connect = connect
        mysql_module.connector = connector_module
        with mock.patch.dict(
            sys.modules,
            {"mysql": mysql_module, "mysql.connector": connector_module},
        ):
            driver, connection = store._connect(settings.database)

        self.assertEqual(driver, "mysql.connector")
        self.assertIs(connection, mock.sentinel.connection)
        connect.assert_called_once()
        self.assertEqual(connect.call_args.kwargs["user"], "afp_app")
        self.assertEqual(connect.call_args.kwargs["password"], "")


if __name__ == "__main__":
    unittest.main()
