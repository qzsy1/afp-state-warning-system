from __future__ import annotations

import json
import re
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from visualization_app.pre_sensor_config import (
    PreSensorConfigError,
    prepare_pre_sensor_start,
    validate_pre_sensor_config,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG_ROOT = ROOT / "pre_sensor" / "config"
SECRET_KEY = re.compile(r"password|token|secret|credential|api[_-]?key", re.I)


def _hardware_facts() -> dict:
    return {
        "manufacturer": "fixture manufacturer", "model": "fixture model",
        "manual_source": "fixture manual revision", "nameplate_identity": "fixture device identity",
        "protocol_version": "confirmed fixture protocol", "interface_type": "fixture interface",
        "electrical_topology": "fixture topology", "sampling_rate_hz": 10,
        "measurement_range_c": "0..500", "cold_junction_compensation": "fixture CJC method",
        "fault_codes": "fixture fault code table", "vendor_channel_mapping": "fixture mapping for channels 1..64",
    }


def _walk(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield f"{path}.{key}" if path else str(key), item
            yield from _walk(item, f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


class PreSensorConfigurationTemplateTests(unittest.TestCase):
    def _load(self, name: str) -> dict:
        return json.loads((CONFIG_ROOT / name).read_text(encoding="utf-8"))

    def test_two_complete_mutually_exclusive_templates(self) -> None:
        expected_channels = {
            "温度", "压力", "薄膜压力", "ROI平均温度", "张力", "线速度",
            "ABB_X", "ABB_Y", "ABB_Z",
            *{f"温度{index}" for index in range(1, 65)},
            *{f"应变{index}" for index in range(1, 5)},
        }
        required_sections = {
            "schema_version", "release_revision", "dataset_schema", "mode", "channels", "interfaces",
            "storage", "timeouts", "reconnect", "queues", "logging", "capacity",
        }
        local = self._load("local_direct.json")
        remote = self._load("remote_helper.json")

        for name, payload in (("local_direct", local), ("remote_helper", remote)):
            with self.subTest(template=name):
                self.assertTrue(required_sections.issubset(payload))
                self.assertEqual(payload["schema_version"], 1)
                self.assertEqual(payload["dataset_schema"], "new_collection_v13_0")
                self.assertEqual(payload["mode"], name)
                channels = {item["name"]: item for item in payload["channels"]}
                self.assertEqual(set(channels), expected_channels)
                self.assertTrue(all(item["unit"] for item in channels.values()))
                self.assertTrue(all(item["interface_id"] for item in channels.values()))
                self.assertTrue(payload["interfaces"])
                self.assertTrue(all(item["endpoint_ref"] for item in payload["interfaces"]))
                for section in ("storage", "timeouts", "reconnect", "queues", "logging", "capacity"):
                    self.assertIsInstance(payload[section], dict)
                    self.assertTrue(payload[section])

        self.assertNotIn("helper", local)
        self.assertIn("helper", remote)
        self.assertTrue(all(item["location"] == "local" for item in local["interfaces"]))
        self.assertTrue(
            all(item["location"] == "remote_helper" for item in remote["interfaces"])
        )

    def test_templates_and_schema_contain_no_secret_or_machine_path(self) -> None:
        for name in ("local_direct.json", "remote_helper.json"):
            payload = self._load(name)
            rendered = json.dumps(payload, ensure_ascii=False)
            self.assertNotRegex(rendered, r"[A-Za-z]:[\\/]")
            self.assertNotRegex(rendered, r"https?://[^/\s:@]+:[^/\s@]+@")
            for path, value in _walk(payload):
                with self.subTest(template=name, path=path):
                    self.assertIsNone(SECRET_KEY.search(path))
                    if isinstance(value, str):
                        self.assertNotIn("sk-", value.lower())

        schema = self._load("schema.json")
        self.assertEqual(schema["$id"], "afp-pre-sensor-config-v1")
        self.assertIn("oneOf", schema)


class PreSensorConfigurationValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.local = json.loads(
            (CONFIG_ROOT / "local_direct.json").read_text(encoding="utf-8")
        )
        cls.remote = json.loads(
            (CONFIG_ROOT / "remote_helper.json").read_text(encoding="utf-8")
        )
        cls.local["interfaces"][0]["hardware_facts"] = _hardware_facts()
        cls.remote["interfaces"][0]["hardware_facts"] = _hardware_facts()

    def assert_invalid_without_side_effect(self, payload: dict, pattern: str) -> None:
        local_start = Mock()
        remote_start = Mock()
        with self.assertRaisesRegex(PreSensorConfigError, pattern):
            prepare_pre_sensor_start(
                payload,
                references={},
                local_start=local_start,
                remote_start=remote_start,
            )
        local_start.assert_not_called()
        remote_start.assert_not_called()

    def test_missing_or_mixed_mode_fails_before_start(self) -> None:
        missing = deepcopy(self.local)
        missing.pop("mode")
        self.assert_invalid_without_side_effect(missing, "mode")

        local_with_helper = deepcopy(self.local)
        local_with_helper["helper"] = deepcopy(self.remote["helper"])
        self.assert_invalid_without_side_effect(local_with_helper, "helper")

        remote_without_helper = deepcopy(self.remote)
        remote_without_helper.pop("helper")
        self.assert_invalid_without_side_effect(remote_without_helper, "helper")

        wrong_location = deepcopy(self.remote)
        wrong_location["interfaces"][0]["location"] = "local"
        self.assert_invalid_without_side_effect(wrong_location, "location")

    def test_channel_and_unit_contract_is_strict(self) -> None:
        unknown = deepcopy(self.local)
        unknown["channels"][0]["name"] = "不存在通道"
        self.assert_invalid_without_side_effect(unknown, "channel")

        wrong_unit = deepcopy(self.local)
        wrong_unit["channels"][0]["unit"] = "kelvin"
        self.assert_invalid_without_side_effect(wrong_unit, "unit")

        dangling = deepcopy(self.local)
        dangling["channels"][0]["interface_id"] = "missing-interface"
        self.assert_invalid_without_side_effect(dangling, "interface")

    def test_capacity_timeout_queue_and_reconnect_ranges_fail_closed(self) -> None:
        mutations = (
            (("storage", "save_root_ref"), "", "save_root_ref"),
            (("timeouts", "first_sample_seconds"), 0, "first_sample"),
            (("timeouts", "writer_stop_seconds"), 301, "writer_stop"),
            (("reconnect", "maximum_attempts"), 0, "maximum_attempts"),
            (("reconnect", "initial_delay_seconds"), 20, "initial_delay"),
            (("queues", "writer_rows"), 0, "writer_rows"),
            (("queues", "high_water_ratio"), 1.1, "high_water_ratio"),
            (("logging", "max_total_mb"), 0, "max_total_mb"),
            (("capacity", "minimum_free_mb"), 0, "minimum_free_mb"),
            (("capacity", "hard_limit_ratio"), 0.5, "hard_limit_ratio"),
        )
        for (section, field), value, pattern in mutations:
            with self.subTest(section=section, field=field):
                payload = deepcopy(self.local)
                payload[section][field] = value
                self.assert_invalid_without_side_effect(payload, pattern)

    def test_valid_templates_map_to_existing_acquisition_config(self) -> None:
        payload = deepcopy(self.local)
        payload["interfaces"][0]["hardware_facts"] = _hardware_facts()
        local = validate_pre_sensor_config(
            payload,
            references={
                "AFP_SAVE_ROOT": "capture-output",
                "AFP_LOG_ROOT": "capture-logs",
                "AFP_INTERFACE_THERMOCOUPLE64": "fixture-device",
                "AFP_INTERFACE_PLC": "plc-host:502",
                "AFP_INTERFACE_UVC": "uvc-device",
                "AFP_INTERFACE_ABB": "abb-host",
                "AFP_INTERFACE_M3232": "COM9",
                "AFP_INTERFACE_ESR1001": "COM10",
            },
        )
        self.assertEqual(local.real_acquisition_mode, "local_direct")
        self.assertEqual(local.sample_rate_hz, 10.0)
        self.assertEqual(set(local.selected_sensors), {item["name"] for item in self.local["channels"]})

    def test_missing_thermocouple_hardware_facts_block_real_config(self) -> None:
        for field in _hardware_facts():
            payload = deepcopy(self.local)
            payload["interfaces"][0]["hardware_facts"] = _hardware_facts()
            payload["interfaces"][0]["hardware_facts"][field] = None
            with self.subTest(field=field), self.assertRaisesRegex(PreSensorConfigError, field):
                validate_pre_sensor_config(
                    payload,
                    references={
                        "AFP_SAVE_ROOT": "capture-output", "AFP_LOG_ROOT": "capture-logs",
                        "AFP_INTERFACE_THERMOCOUPLE64": "fixture-device",
                        "AFP_INTERFACE_PLC": "plc-host:502", "AFP_INTERFACE_UVC": "uvc-device",
                        "AFP_INTERFACE_ABB": "abb-host", "AFP_INTERFACE_M3232": "COM9",
                        "AFP_INTERFACE_ESR1001": "COM10",
                    },
                )

    def test_remote_helper_contract_is_checked_before_dispatch(self) -> None:
        remote = deepcopy(self.remote)
        contract = json.loads(
            (ROOT / "pre_sensor" / "release-contract.json").read_text(encoding="utf-8")
        )
        identity = contract["identity"]
        contracts = contract["contracts"]
        capabilities = {
            "protocol_version": contracts["helper_protocol_version"],
            "helper_version": identity["helper_version"],
            "build_id": identity["release_revision"],
            "unified_frame_contract": contracts["unified_frame_contract"],
            "configuration_schema_version": contracts["configuration_schema_version"],
            "readiness_schema_version": contracts["readiness_schema_version"],
            "device_readiness_contract": "plc_abb_uvc_readiness_v1",
            "unified_device_readiness_v1": True,
            "unified_frame_v2": True,
            "thermocouple64_hardware_gate": True,
            "real_capture": True,
            "local_csv_save": True,
            "esr1001_modbus_rtu": True,
        }
        references = {
            "AFP_HELPER_SAVE_ROOT": "helper-output",
            "AFP_HELPER_LOG_ROOT": "helper-logs",
            "AFP_HELPER_SERVER_URL": "http://helper.invalid:8765",
            "AFP_HELPER_PAIRING_PROFILE": {
                "session_id": "paired-test-session",
                "capabilities": capabilities,
            },
            "AFP_INTERFACE_THERMOCOUPLE64": "fixture-device",
            "AFP_INTERFACE_PLC": "plc-host:502",
            "AFP_INTERFACE_UVC": "uvc-device",
            "AFP_INTERFACE_ABB": "abb-host",
            "AFP_INTERFACE_M3232": "COM9",
            "AFP_INTERFACE_ESR1001": "COM10",
        }
        local_start = Mock()
        remote_start = Mock(return_value="started")
        self.assertEqual(
            prepare_pre_sensor_start(
                remote,
                references=references,
                local_start=local_start,
                remote_start=remote_start,
            ),
            "started",
        )
        local_start.assert_not_called()
        remote_start.assert_called_once()

        capabilities["helper_version"] = "incompatible"
        remote_start.reset_mock()
        with self.assertRaisesRegex(PreSensorConfigError, "release_contract_mismatch"):
            prepare_pre_sensor_start(
                remote,
                references=references,
                local_start=local_start,
                remote_start=remote_start,
            )
        remote_start.assert_not_called()



if __name__ == "__main__":
    unittest.main()
