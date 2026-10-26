from __future__ import annotations

import copy
import csv
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from thermocouple64 import (  # noqa: E402
    THERMOCOUPLE64_CHANNELS,
    project_specimen_layers,
)
import acquisition  # noqa: E402


class Thermocouple64LayerProjectionTests(unittest.TestCase):
    def test_csv_projection_preserves_raw_values_means_imputation_and_digests(self) -> None:
        for scope in ("causal_so_far", "completed_specimen"):
            with self.subTest(scope=scope):
                layers = [{"layer_id": 1, "completed": True, "rows": [
                    {name: str(index) for index, name in enumerate(THERMOCOUPLE64_CHANNELS)},
                    {name: float(index + 2) for index, name in enumerate(THERMOCOUPLE64_CHANNELS)}]},
                    {"layer_id": 2, "completed": False, "rows": [
                        {"温度1": None, "温度2": float("nan"), "温度3": float("inf")}, {}]}]
                detailed = project_specimen_layers(layers, sensor_columns=THERMOCOUPLE64_CHANNELS, scope=scope)
                csv_view = project_specimen_layers(layers, sensor_columns=THERMOCOUPLE64_CHANNELS,
                                                   scope=scope, include_row_metadata=False)
                self.assertEqual(detailed["manifest"], csv_view["manifest"])
                for expected_layer, actual_layer in zip(detailed["derived_layers"], csv_view["derived_layers"]):
                    for expected, actual in zip(expected_layer["rows"], actual_layer["rows"]):
                        expected.pop("_thermocouple64_derived")
                        self.assertEqual(expected, actual)

    @staticmethod
    def _config(layer: int, selected: list[str]) -> acquisition.AcquisitionConfig:
        return acquisition.AcquisitionConfig(
            acquisition_mode="simulation",
            dataset_schema="new_collection_v13_0",
            simulation_source_path="fixture.csv",
            interfaces=acquisition.default_capture_interfaces(),
            selected_sensors=selected,
            layer=layer,
            condition_id="C1",
            replicate=1,
        )

    @staticmethod
    def _write_csv(path: Path, fieldnames: list[str], row: dict) -> None:
        with path.open("w", encoding="gb18030", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(row)

    def test_manager_accepts_changed_layer_channels_and_builds_union_view(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = acquisition.AcquisitionManager(root)
            manager.session_dir = root
            manager.capture_record_dir = root
            manager.session_stamp = "fixture"
            first = self._config(0, list(THERMOCOUPLE64_CHANNELS))
            token = acquisition._parameter_token(first)
            first_path = root / f"{token}_第1层.CSV"
            first_row = {"时间": 1.0, "温度1": 11.0, "温度64": 64.0}
            self._write_csv(first_path, first.raw_columns, first_row)
            first_bytes = first_path.read_bytes()

            second = self._config(1, ["温度64"])
            manager._prepare_capture_identity(second, token)
            second_path = root / f"{token}_第2层.CSV"
            self._write_csv(second_path, second.raw_columns, {"时间": 2.0, "温度64": 65.0})
            manager.config = second
            combined = manager._rebuild_whole_specimen()

            with combined.open("r", encoding="gb18030", newline="") as stream:
                rows = list(csv.DictReader(stream))
                columns = stream.seek(0) or next(csv.reader(stream), [])
            self.assertEqual(len(rows), 2)
            self.assertIn("温度1", columns)
            self.assertIn("温度64", columns)
            self.assertEqual([name for name in columns if name in THERMOCOUPLE64_CHANNELS], list(THERMOCOUPLE64_CHANNELS))
            self.assertEqual(rows[0]["温度1"], "11.0")
            self.assertEqual(rows[0]["温度64"], "64.0")
            self.assertEqual(rows[1]["温度1"], "")
            self.assertEqual(rows[1]["温度64"], "65.0")
            self.assertEqual(first_path.read_bytes(), first_bytes)
            derived = root / f"{token}_完整试样_派生.CSV"
            manifest = derived.with_suffix(".manifest.json")
            self.assertTrue(derived.is_file())
            self.assertEqual(
                json.loads(manifest.read_text(encoding="utf-8"))["scope"],
                "completed_specimen",
            )

    def test_union_alignment_preserves_rows_and_does_not_impute_identity_fields(self) -> None:
        layers = [
            {"layer_id": 1, "rows": [{"specimen_id": "S1", "温度1": 1.0}, {"specimen_id": "S1", "温度1": 3.0}]},
            {"layer_id": 2, "rows": [{"specimen_id": "S1", "温度2": 10.0}]},
            {"layer_id": 3, "rows": [{"specimen_id": "S1", "温度1": None, "温度2": 14.0}]},
        ]
        original = copy.deepcopy(layers)
        result = project_specimen_layers(layers, sensor_columns=["温度1", "温度2"], scope="completed_specimen")
        self.assertEqual(layers, original)
        self.assertEqual([len(item["rows"]) for item in result["raw_layers"]], [2, 1, 1])
        self.assertIsNone(result["raw_layers"][1]["rows"][0]["温度1"])
        self.assertEqual(result["derived_layers"][1]["rows"][0]["温度1"], 2.0)
        self.assertEqual(result["derived_layers"][0]["rows"][0]["specimen_id"], "S1")

    def test_only_whole_layer_missing_is_filled(self) -> None:
        layers = [
            {"layer_id": 1, "rows": [{"温度1": 1.0}, {"温度1": 5.0}]},
            {"layer_id": 2, "rows": [{"温度1": None}, {"温度1": None}]},
            {"layer_id": 3, "rows": [{"温度1": 9.0}, {"温度1": None}]},
        ]
        result = project_specimen_layers(layers, sensor_columns=["温度1"], scope="completed_specimen")
        self.assertEqual([row["温度1"] for row in result["derived_layers"][1]["rows"]], [5.0, 5.0])
        self.assertEqual([row["温度1"] for row in result["derived_layers"][2]["rows"]], [9.0, None])

    def test_median_is_per_channel_and_ignores_nonfinite_values(self) -> None:
        layers = [
            {"layer_id": 1, "rows": [{"温度1": 1.0, "温度2": 2.0}, {"温度1": 9.0, "温度2": math.nan}]},
            {"layer_id": 2, "rows": [{"温度1": None, "温度2": None}]},
            {"layer_id": 3, "rows": [{"温度1": 3.0, "温度2": 8.0}, {"温度1": 7.0, "温度2": math.inf}]},
        ]
        result = project_specimen_layers(layers, sensor_columns=["温度1", "温度2"], scope="completed_specimen")
        row = result["derived_layers"][1]["rows"][0]
        self.assertEqual(row["温度1"], 5.0)
        self.assertEqual(row["温度2"], 5.0)

    def test_causal_scope_never_reads_future_layer(self) -> None:
        layers = [
            {"layer_id": 1, "rows": [{"温度1": None}]},
            {"layer_id": 2, "rows": [{"温度1": 20.0}]},
        ]
        causal = project_specimen_layers(layers, sensor_columns=["温度1"], scope="causal_so_far")
        completed = project_specimen_layers(layers, sensor_columns=["温度1"], scope="completed_specimen")
        self.assertIsNone(causal["derived_layers"][0]["rows"][0]["温度1"])
        self.assertEqual(completed["derived_layers"][0]["rows"][0]["温度1"], 20.0)

    def test_never_observed_channel_stays_unavailable_and_group_mean_requires_eight(self) -> None:
        rows = [{name: float(index) for index, name in enumerate(THERMOCOUPLE64_CHANNELS[:7], start=1)}]
        result = project_specimen_layers(
            [{"layer_id": 1, "rows": rows}],
            sensor_columns=THERMOCOUPLE64_CHANNELS[:8],
            scope="completed_specimen",
        )
        derived = result["derived_layers"][0]["rows"][0]
        self.assertIsNone(derived["温度8"])
        self.assertIsNone(derived["热电偶均值1"])
        self.assertFalse(derived["_thermocouple64_derived"]["热电偶均值1"]["model_usable"])

    def test_manifest_is_deterministic_and_records_scope_samples_and_masks(self) -> None:
        layers = [
            {"layer_id": 1, "rows": [{"温度1": 1.0}, {"温度1": 3.0}]},
            {"layer_id": 2, "rows": [{"温度1": None}, {"温度1": None}]},
        ]
        first = project_specimen_layers(layers, sensor_columns=["温度1"], scope="completed_specimen")
        second = project_specimen_layers(layers, sensor_columns=["温度1"], scope="completed_specimen")
        self.assertEqual(first["manifest"], second["manifest"])
        item = first["manifest"]["imputations"][0]
        self.assertEqual(item["sample_count"], 2)
        self.assertEqual(item["row_mask"], [0, 1])
        self.assertEqual(item["scope"], "completed_specimen")


if __name__ == "__main__":
    unittest.main()
