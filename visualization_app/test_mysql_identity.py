from __future__ import annotations

import unittest

from acquisition import AcquisitionConfig
from mysql_storage import MySQLCaptureStore, process_condition_key


class MySQLIdentityTests(unittest.TestCase):
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
