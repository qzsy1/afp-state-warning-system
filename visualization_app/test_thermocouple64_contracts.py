from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

import acquisition  # noqa: E402
from real_acquisition import ChannelSampleCache, FrameAssembler  # noqa: E402
from thermocouple64 import (  # noqa: E402
    HARDWARE_FACT_FIELDS,
    THERMOCOUPLE64_AGGREGATION_VERSION,
    THERMOCOUPLE64_CHANNELS,
    THERMOCOUPLE64_DERIVED_CHANNELS,
    THERMOCOUPLE64_GROUPS,
    Thermocouple64AdapterContract,
    derived_catalog,
    hardware_release_readiness,
    raw_catalog,
    strict_group_means,
    validate_group_mean_model_contract,
)


class Thermocouple64ContractsTests(unittest.TestCase):
    def test_v13_mode_contains_77_unique_channels_without_changing_old_modes(self) -> None:
        self.assertEqual(len(acquisition.ACQUISITION_SCHEMAS["new_collection_v11_3"]["sensors"]), 17)
        self.assertEqual(len(acquisition.ACQUISITION_SCHEMAS["new_collection_v12_0"]["sensors"]), 21)
        sensors = acquisition.ACQUISITION_SCHEMAS["new_collection_v13_0"]["sensors"]
        self.assertEqual(len(sensors), 77)
        self.assertEqual(len(set(sensors)), 77)
        self.assertEqual(
            [name for name in sensors if name.startswith("温度") and name != "温度"],
            list(THERMOCOUPLE64_CHANNELS),
        )
        self.assertEqual(THERMOCOUPLE64_GROUPS["TC-G01"], tuple(f"温度{i}" for i in range(1, 9)))
        self.assertEqual(THERMOCOUPLE64_GROUPS["TC-G08"], tuple(f"温度{i}" for i in range(57, 65)))

    def test_all_64_channels_have_unambiguous_metadata(self) -> None:
        aliases = []
        for index, channel in enumerate(THERMOCOUPLE64_CHANNELS, start=1):
            metadata = acquisition.SENSOR_CHANNEL_METADATA[channel]
            self.assertEqual(metadata["unit"], "°C")
            self.assertEqual(metadata["dtype"], "float32")
            self.assertEqual(metadata["display_group"], f"TC-G{((index - 1) // 8) + 1:02d}")
            aliases.append(metadata["alias"])
        self.assertEqual(len(aliases), len(set(aliases)))

    def test_current_interface_options_exclude_historical_eight_channel_hardware(self) -> None:
        roles = {item["id"] for item in acquisition.sensor_interface_profiles()}
        self.assertIn("thermocouple64", roles)
        self.assertNotIn("thermocouple", roles)
        defaults = {item["role"] for item in acquisition.default_capture_interfaces()}
        self.assertIn("thermocouple64", defaults)
        self.assertNotIn("thermocouple", defaults)

    def test_strict_means_use_each_contiguous_group_of_eight(self) -> None:
        values = {name: float(index) for index, name in enumerate(THERMOCOUPLE64_CHANNELS, start=1)}
        means = strict_group_means(values)
        self.assertEqual(list(means), list(THERMOCOUPLE64_DERIVED_CHANNELS))
        self.assertEqual(means["热电偶均值1"]["value"], 4.5)
        self.assertEqual(means["热电偶均值2"]["value"], 12.5)
        self.assertEqual(means["热电偶均值8"]["value"], 60.5)
        self.assertTrue(all(item["valid_count"] == 8 for item in means.values()))
        values["温度64"] = None
        means = strict_group_means(values)
        self.assertIsNone(means["热电偶均值8"]["value"])
        self.assertEqual(means["热电偶均值8"]["valid_count"], 7)

    def test_vendor_neutral_adapter_classifies_missing_nonfinite_and_sequence_errors(self) -> None:
        adapter = Thermocouple64AdapterContract()
        values = list(range(1, 65))
        values[8] = math.nan
        snapshot = adapter.adapt(
            device_identity="fixture-device",
            source_sequence=7,
            source_timestamp=100.0,
            values=values,
        )
        self.assertEqual(snapshot.values["温度64"], 64.0)
        self.assertEqual(snapshot.quality["温度9"], "invalid")
        self.assertEqual(snapshot.errors["温度9"], "non_finite_value")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            adapter.adapt(device_identity="fixture-device", source_sequence=7, source_timestamp=101.0, values=values)
        with self.assertRaisesRegex(ValueError, "out_of_order"):
            adapter.adapt(device_identity="fixture-device", source_sequence=6, source_timestamp=101.0, values=values)

    def test_64_channel_normalization_is_role_scoped_and_never_truncates(self) -> None:
        payload = {"values": list(range(1, 65))}
        normalized = acquisition._normalize_interface_sample(payload, "thermocouple64")
        self.assertEqual(len(normalized), 64)
        self.assertEqual(normalized["温度64"], 64.0)
        with self.assertRaisesRegex(ValueError, "exactly 64"):
            acquisition._normalize_interface_sample({"values": list(range(63))}, "thermocouple64")
        with self.assertRaisesRegex(ValueError, "exactly 64"):
            acquisition._normalize_interface_sample({"values": list(range(65))}, "thermocouple64")
        self.assertNotIn(
            "温度64",
            acquisition._normalize_interface_sample({"CH64": 64.0}, "plc"),
        )
        self.assertEqual(
            acquisition._normalize_interface_sample({"CH64": 64.0}, "thermocouple64"),
            {"温度64": 64.0},
        )

    def test_partial_group_is_published_as_missing_without_blocking_other_groups(self) -> None:
        adapter = Thermocouple64AdapterContract()
        snapshot = adapter.adapt(
            device_identity="fixture-device",
            source_sequence=1,
            source_timestamp=10.0,
            values={f"CH{i}": i for i in range(9, 65)},
        )
        cache = ChannelSampleCache(max_events=128)
        acquisition.publish_thermocouple64_snapshot(
            cache, snapshot, received_wall_time=10.0, received_monotonic=5.0
        )
        assembler = FrameAssembler(
            THERMOCOUPLE64_CHANNELS,
            1.0,
            clock=lambda: 5.0,
            wall_clock=lambda: 10.0,
            start_monotonic=5.0,
            start_wall_time=10.0,
        )
        frame = assembler.assemble(cache, 5.0)
        self.assertTrue(all(frame.values()[f"温度{i}"] is None for i in range(1, 9)))
        self.assertEqual(frame.values()["温度9"], 9.0)
        self.assertEqual(frame.values()["温度64"], 64.0)

    def test_catalog_ids_change_with_contract_and_raw_derived_are_separate(self) -> None:
        units = {name: "°C" for name in THERMOCOUPLE64_CHANNELS}
        first = raw_catalog(THERMOCOUPLE64_CHANNELS, units)
        reordered = raw_catalog(reversed(THERMOCOUPLE64_CHANNELS), units)
        derived = derived_catalog("causal_so_far")
        completed = derived_catalog("completed_specimen")
        self.assertNotEqual(first["catalog_id"], reordered["catalog_id"])
        self.assertNotEqual(first["catalog_id"], derived["catalog_id"])
        self.assertNotEqual(derived["catalog_id"], completed["catalog_id"])

    def test_real_hardware_gate_cannot_be_satisfied_by_simulation(self) -> None:
        readiness = hardware_release_readiness({})
        self.assertFalse(readiness["ready"])
        self.assertEqual(set(readiness["missing_facts"]), set(HARDWARE_FACT_FIELDS))
        interface = acquisition.default_capture_interfaces()[0]
        interface["physical_interface_id"] = "serial:COM7"
        interface["physical_interface_kind"] = "serial"
        interface["endpoint"] = "COM7"
        config = acquisition.AcquisitionConfig(
            acquisition_mode="real",
            dataset_schema="new_collection_v13_0",
            interfaces=[interface],
            selected_sensors=list(THERMOCOUPLE64_CHANNELS),
        )
        self.assertEqual(config.interfaces[0]["driver"], "huipu_tc64_modbus_rtu")
        self.assertFalse(hardware_release_readiness(interface["hardware_facts"])["ready"])
        simulation = acquisition.AcquisitionConfig(
            acquisition_mode="simulation",
            dataset_schema="new_collection_v13_0",
            simulation_source_path="fixture.csv",
            interfaces=acquisition.default_capture_interfaces(),
            selected_sensors=list(acquisition.CURRENT_COLLECTION_SENSOR_COLUMNS),
        )
        self.assertEqual(simulation.dataset_schema, "new_collection_v13_0")

    def test_group_mean_model_contract_rejects_legacy_eight_channel_model(self) -> None:
        legacy = {
            "input_sensors": [f"温度{i}" for i in range(1, 9)],
            "aggregation_version": THERMOCOUPLE64_AGGREGATION_VERSION,
            "imputation_scope": "causal_so_far",
            "temperature_feature_dimension": 8,
            "temperature_feature_scaling": {
                name: {"mean": 0.0, "scale": 1.0}
                for name in THERMOCOUPLE64_DERIVED_CHANNELS
            },
        }
        self.assertTrue(validate_group_mean_model_contract(legacy))
        compatible = {
            **legacy,
            "input_sensors": list(THERMOCOUPLE64_DERIVED_CHANNELS),
            "temperature_features": list(THERMOCOUPLE64_DERIVED_CHANNELS),
        }
        self.assertEqual(validate_group_mean_model_contract(compatible), [])


if __name__ == "__main__":
    unittest.main()
