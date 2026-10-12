from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from visualization_app.pre_sensor_preflight import (
    CapacityPolicy,
    probe_capacity_readiness,
)


class CapacityReadinessTests(unittest.TestCase):
    def _config(self) -> dict:
        return {
            "queues": {
                "writer_rows": 100,
                "transport_rows": 200,
                "high_water_ratio": 0.8,
                "overflow_policy": "fail_closed",
            },
            "logging": {
                "max_file_mb": 10,
                "max_total_mb": 100,
                "retention_days": 14,
            },
            "capacity": {
                "minimum_free_mb": 100,
                "journal_max_mb": 50,
                "retry_spool_max_mb": 75,
                "warning_ratio": 0.8,
                "hard_limit_ratio": 1.0,
            },
        }

    def test_every_queue_and_directory_has_a_finite_limit(self) -> None:
        policy = CapacityPolicy.from_config(self._config())
        self.assertEqual(policy.writer_rows, 100)
        self.assertEqual(policy.transport_rows, 200)
        self.assertEqual(policy.log_max_bytes, 100 * 1024 * 1024)
        self.assertEqual(policy.journal_max_bytes, 50 * 1024 * 1024)
        self.assertEqual(policy.spool_max_bytes, 75 * 1024 * 1024)

        mutations = (
            ("queues", "writer_rows"),
            ("queues", "transport_rows"),
            ("logging", "max_total_mb"),
            ("logging", "retention_days"),
            ("capacity", "journal_max_mb"),
            ("capacity", "retry_spool_max_mb"),
            ("capacity", "minimum_free_mb"),
        )
        for section, field in mutations:
            with self.subTest(section=section, field=field):
                config = deepcopy(self._config())
                config[section][field] = 0
                with self.assertRaisesRegex(ValueError, field):
                    CapacityPolicy.from_config(config)

    def test_usage_projection_reports_warning_and_rejects_hard_limit(self) -> None:
        policy = CapacityPolicy.from_config(self._config())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = {
                "capacity_policy": policy,
                "capacity_directories": {
                    "logs": str(root / "logs"),
                    "journal": str(root / "journal"),
                    "spool": str(root / "spool"),
                },
                "capacity_usage_bytes": {
                    "logs": 85 * 1024 * 1024,
                    "journal": 1,
                    "spool": 1,
                },
                "queue_depths": {"writer": 85, "transport": 1},
                "capacity_free_bytes": 200 * 1024 * 1024,
            }
            outcome = probe_capacity_readiness(context)
            self.assertEqual(outcome.status, "passed")
            self.assertIn("logs", outcome.details["warnings"])
            self.assertIn("writer_queue", outcome.details["warnings"])

            context["capacity_usage_bytes"]["spool"] = 75 * 1024 * 1024
            with self.assertRaisesRegex(ValueError, "spool.*hard limit"):
                probe_capacity_readiness(context)

    def test_minimum_disk_and_overflow_are_fail_closed(self) -> None:
        policy = CapacityPolicy.from_config(self._config())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = {
                "capacity_policy": policy,
                "capacity_directories": {
                    "logs": str(root / "logs"),
                    "journal": str(root / "journal"),
                    "spool": str(root / "spool"),
                },
                "capacity_usage_bytes": {"logs": 0, "journal": 0, "spool": 0},
                "queue_depths": {"writer": 0, "transport": 0},
                "capacity_free_bytes": 99 * 1024 * 1024,
            }
            with self.assertRaisesRegex(ValueError, "minimum free"):
                probe_capacity_readiness(base)
            base["capacity_free_bytes"] = 200 * 1024 * 1024
            base["queue_depths"]["transport"] = 201
            with self.assertRaisesRegex(ValueError, "transport_queue.*hard limit"):
                probe_capacity_readiness(base)


if __name__ == "__main__":
    unittest.main()
