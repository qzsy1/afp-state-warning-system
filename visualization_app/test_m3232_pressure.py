from __future__ import annotations

import json
from pathlib import Path
import unittest

from acquisition import M3232PressureDriver, M3232_BAUDRATE, NEW_COLLECTION_SENSOR_COLUMNS, SimulatorDriver


class M3232PressureDriverTests(unittest.TestCase):
    def test_manual_default_baudrate_is_used(self) -> None:
        self.assertEqual(M3232_BAUDRATE, 115200)

    def test_parser_accepts_fragmented_variable_size_matrix_json(self) -> None:
        driver = M3232PressureDriver("COM_TEST")
        matrix = [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]]
        encoded = json.dumps({"matrix": matrix}, separators=(",", ":"))
        driver.rx_buffer = encoded[:7]
        self.assertEqual(driver._extract_frames(), [])
        driver.rx_buffer += encoded[7:]
        frames = driver._extract_frames()
        self.assertEqual(frames, [matrix])
        self.assertEqual(driver.matrix_shape, (2, 3))

    def test_pressure_metrics_expose_scalar_and_matrix_quality(self) -> None:
        driver = M3232PressureDriver("COM_TEST")
        matrix = [[0.0, 10.0], [20.0, 30.0]]
        metrics = driver._frame_metrics(matrix)
        self.assertEqual(metrics["pressure_peak"], 30.0)
        self.assertEqual(metrics["contact_area"], 3)
        self.assertGreater(metrics["pressure_total"], 0.0)
        self.assertEqual(metrics["valid_fraction"], 0.75)

    def test_generated_full_channel_csv_is_readable_by_simulator(self) -> None:
        path = Path(r"F:\AFP_Capture\simulation_m3232_new_collection\SIM_PRESSURE_M3232_new_collection.csv")
        self.assertTrue(path.exists())
        driver = SimulatorDriver(path, NEW_COLLECTION_SENSOR_COLUMNS)
        driver.open()
        sample = driver.read_sample()
        self.assertIsNotNone(sample)
        self.assertEqual(set(NEW_COLLECTION_SENSOR_COLUMNS), set(sample))


if __name__ == "__main__":
    unittest.main()
