from __future__ import annotations

import unittest

from remote_mysql_setup import build_remote_setup_sql, classify_mysql_error


class RemoteMySQLSetupTests(unittest.TestCase):
    def test_build_remote_setup_sql_grants_new_database_to_remote_client(self) -> None:
        sql = build_remote_setup_sql(
            database="afp_remote_2026",
            user="afp_app",
            password="p@ss'word",
            allowed_host="192.168.101.%",
        )
        self.assertIn("CREATE DATABASE IF NOT EXISTS `afp_remote_2026`", sql)
        self.assertIn("CREATE USER IF NOT EXISTS 'afp_app'@'192.168.101.%'", sql)
        self.assertIn("IDENTIFIED BY 'p@ss''word'", sql)
        self.assertIn("ON `afp_remote_2026`.*", sql)
        self.assertIn("FLUSH PRIVILEGES", sql)

    def test_build_remote_setup_sql_rejects_invalid_identifiers(self) -> None:
        with self.assertRaises(ValueError):
            build_remote_setup_sql(
                database="afp-remote",
                user="afp_app",
                password="secret",
                allowed_host="192.168.101.%",
            )

    def test_classify_mysql_error_distinguishes_auth_schema_and_network(self) -> None:
        self.assertEqual(classify_mysql_error("1045 Access denied" )["category"], "authentication")
        detailed = classify_mysql_error(
            "1045 Access denied for user 'root'@'192.168.1.20' (using password: YES)"
        )
        self.assertEqual(detailed["category"], "authentication")
        self.assertIn("root@192.168.1.20", detailed["message"])
        self.assertEqual(classify_mysql_error("1044 Access denied to database")["category"], "authorization")
        self.assertEqual(classify_mysql_error("1044 Access denied for user")["category"], "authorization")
        self.assertEqual(classify_mysql_error("1049 Unknown database")["category"], "database")
        self.assertEqual(classify_mysql_error("2003 Can't connect")["category"], "network")
        self.assertEqual(classify_mysql_error("1130 Host '10.0.0.2' is not allowed to connect")["category"], "host")


if __name__ == "__main__":
    unittest.main()
