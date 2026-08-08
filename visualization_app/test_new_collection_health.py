from __future__ import annotations

import unittest
from pathlib import Path

import numpy as np

from new_collection_health import (
    INDICATOR_FEATURES,
    NEW_ABNORMAL_STATES,
    PROCESS_COLUMNS,
    SENSOR_COLUMNS,
    build_calibration,
    build_feature_vector,
    cause_probabilities,
)


class NewCollectionHealthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        rng = np.random.default_rng(20260801)
        cls.actual = rng.normal(size=(12, 24, len(SENSOR_COLUMNS)))
        cls.prediction = cls.actual + rng.normal(
            0.0, 0.08, size=cls.actual.shape
        )
        cls.process_points = np.asarray(
            [[400.0, 80.0, 5.0, 360.0], [450.0, 80.0, 5.0, 360.0]]
        )
        cls.artifact = build_calibration(
            cls.actual, cls.prediction, cls.process_points
        )
        cls.process = dict(zip(PROCESS_COLUMNS, cls.process_points[0]))

    def test_all_twelve_indicators_generate_finite_features(self) -> None:
        self.assertEqual(len(INDICATOR_FEATURES), 12)
        for indicator, names in INDICATOR_FEATURES.items():
            with self.subTest(indicator=indicator):
                values = build_feature_vector(
                    indicator,
                    self.actual[0],
                    self.prediction[0],
                    self.process,
                    self.artifact,
                )
                self.assertEqual(values.shape, (len(names),))
                self.assertTrue(np.isfinite(values).all())

    def test_new_process_causes_form_a_probability_distribution(self) -> None:
        probabilities = cause_probabilities(
            {
                "initial_compaction_force_N": 250.0,
                "placement_speed_mm_s": 120.0,
                "pid_angle_deg": 10.0,
                "temperature_setpoint_C": 400.0,
            }
        )
        self.assertEqual(set(probabilities), set(NEW_ABNORMAL_STATES))
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=10)

    def test_prediction_horizon_has_direct_numeric_input(self) -> None:
        app_dir = Path(__file__).resolve().parent
        html = (app_dir / "static" / "index.html").read_text(encoding="utf-8")
        javascript = (app_dir / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="horizonNumber" type="number"', html)
        self.assertIn('event.key === "Enter"', javascript)
        self.assertIn(
            "document.activeElement !== controls.horizonNumber", javascript
        )


if __name__ == "__main__":
    unittest.main()
