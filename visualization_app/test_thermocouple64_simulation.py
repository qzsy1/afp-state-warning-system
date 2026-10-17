from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from visualization_app import generate_thermocouple64_simulation as simulator
from visualization_app.generate_thermocouple64_simulation import CORE_CHANNELS, IDENTITY_COLUMNS
from visualization_app.acquisition import PROCESS_PARAMETER_COLUMNS
from visualization_app.thermocouple64 import THERMOCOUPLE64_CHANNELS, strict_group_means


class Thermocouple64SimulationTests(unittest.TestCase):
    def test_simulation_outputs_64_channels_and_heterogeneous_layer_fixtures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            simulator.generate(root)

            with (root / "quick_240.csv").open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                start = 1 + len(CORE_CHANNELS)
                self.assertEqual(reader.fieldnames[start:start + 64], list(THERMOCOUPLE64_CHANNELS))
                rows = list(reader)
            self.assertEqual(len(rows), 240)
            values = {name: float(rows[0][name]) for name in THERMOCOUPLE64_CHANNELS}
            means = strict_group_means(values)
            self.assertEqual([item["valid_count"] for item in means.values()], [8] * 8)

            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["dataset_schema"], "new_collection_v13_0")
            self.assertTrue(manifest["simulation_only"])
            for layer, missing_range in ((2, range(17, 25)), (3, range(41, 49))):
                with (root / "heterogeneous_layers" / f"layer_{layer}.csv").open(
                    encoding="utf-8-sig", newline=""
                ) as handle:
                    fields = csv.DictReader(handle).fieldnames
                self.assertFalse(any(f"温度{index}" in fields for index in missing_range))
                self.assertEqual(
                    len(fields),
                    1 + len(CORE_CHANNELS) + 56 + len(PROCESS_PARAMETER_COLUMNS) + len(IDENTITY_COLUMNS),
                )


if __name__ == "__main__":
    unittest.main()
