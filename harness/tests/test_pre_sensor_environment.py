from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest.mock import patch

from harness.engine import pre_sensor_environment


class PreSensorEnvironmentTests(unittest.TestCase):
    def run_probe(self, name: str) -> tuple[int, dict]:
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), contextlib.redirect_stdout(output):
            code = pre_sensor_environment.main(["--check", name])
        return code, json.loads(output.getvalue())

    def test_second_windows_without_external_evidence_is_blocked(self) -> None:
        code, payload = self.run_probe("second-windows")
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["check_id"], "environment.second_windows")
        self.assertTrue(payload["external_environment"])
        self.assertTrue(payload["remediation"])

    def test_target_mysql_without_profile_is_blocked(self) -> None:
        code, payload = self.run_probe("target-mysql")
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["check_id"], "environment.target_mysql")
        self.assertTrue(payload["external_environment"])
        self.assertNotIn("password", json.dumps(payload).lower())


if __name__ == "__main__":
    unittest.main()
