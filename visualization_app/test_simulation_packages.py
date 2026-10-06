from __future__ import annotations

import csv
import importlib
import unittest


class SimulationPackageTests(unittest.TestCase):
    def _module(self):
        try:
            return importlib.import_module("simulation_packages")
        except ModuleNotFoundError:
            self.fail("simulation_packages module must provide the approved synthetic catalog")

    def test_catalog_contains_quick_and_endurance_synthetic_packages(self):
        packages = self._module().simulation_package_catalog()
        by_id = {item["package_id"]: item for item in packages}

        self.assertEqual(by_id["quick_240"]["points"], 240)
        self.assertEqual(by_id["endurance_6000"]["points"], 6000)
        self.assertEqual(by_id["quick_240"]["sample_rate_hz"], 10.0)
        self.assertEqual(by_id["endurance_6000"]["duration_seconds"], 600.0)
        self.assertTrue(all(item["synthetic"] for item in packages))
        self.assertTrue(all(len(item["sha256"]) == 64 for item in packages))
        self.assertTrue(all("path" not in item for item in packages))

    def test_catalog_files_have_declared_rows_and_full_new_collection_channels(self):
        module = self._module()
        packages = module.simulation_package_catalog()
        for item in packages:
            path = module.resolve_simulation_package(item["package_id"])
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = list(reader)
            self.assertEqual(len(rows), item["points"])
            self.assertEqual(len(item["channels"]), 17)
            self.assertEqual(set(item["channels"]), set(reader.fieldnames or []) & set(item["channels"]))


if __name__ == "__main__":
    unittest.main()
