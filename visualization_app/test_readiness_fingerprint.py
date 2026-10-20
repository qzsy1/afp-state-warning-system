from __future__ import annotations

import copy
import unittest

try:
    from visualization_app.real_acquisition import readiness_config_fingerprint
except ModuleNotFoundError:
    from real_acquisition import readiness_config_fingerprint


class ReadinessFingerprintTests(unittest.TestCase):
    def test_tc64_parameters_and_missing_profile_migration_revoke_readiness(self):
        config = self._config()
        config["interfaces"][0].update(role="thermocouple64", driver="huipu_tc64_modbus_rtu")
        baseline = readiness_config_fingerprint(config)
        for key, value in {
            "read_profile": "channel_temperature", "timeout": .28,
            "channel_settings": {"confirmed": True, "channels": {"温度64": {"unit": 2}}},
            "baudrate": 9600, "slave_id": 2, "enabled": False,
            "freshness_seconds": 3, "scan_timeout_seconds": 60,
            "poll_interval_seconds": .5, "reconnect_window_seconds": 30,
        }.items():
            changed = copy.deepcopy(config)
            changed["interfaces"][0][key] = value
            self.assertNotEqual(readiness_config_fingerprint(changed), baseline, key)

    def _config(self) -> dict:
        return {
            "application_version": "2.0.0",
            "release_revision": "build-a",
            "acquisition_mode": "real",
            "real_acquisition_mode": "local_direct",
            "dataset_schema": "new_collection_v11_3",
            "selected_sensors": ["ABB_X"],
            "interfaces": [
                {
                    "id": "abb_motion",
                    "enabled": True,
                    "role": "robot",
                    "driver": "abb_robot",
                    "endpoint": "192.168.125.1",
                    "username": "operator",
                    "password": "credential-a",
                    "physical_interface_id": "ethernet:industrial",
                    "physical_port_id": "pcie:1",
                    "physical_interface_kind": "ethernet",
                    "physical_transport_family": "ethernet",
                    "device_identity": {
                        "controller_id": "abb-controller-a",
                        "mechanical_unit": "ROB_1",
                    },
                    "driver_state": {
                        "available": True,
                        "provider": "ABB",
                        "version": "1.0",
                        "status": "ready",
                    },
                    "device_profile": {
                        "id": "abb-rws-rob1-base-v1",
                        "version": 1,
                        "approval_status": "approved",
                    },
                }
            ],
        }

    def test_each_readiness_identity_field_invalidates_the_snapshot(self) -> None:
        baseline_config = self._config()
        baseline = readiness_config_fingerprint(baseline_config)
        mutations = {
            "protocol endpoint": ("endpoint", "192.168.125.2"),
            "physical binding": ("physical_port_id", "pcie:2"),
            "credential identifier": ("password", "credential-b"),
            "device identity": (
                "device_identity",
                {"controller_id": "abb-controller-b", "mechanical_unit": "ROB_1"},
            ),
            "driver state": (
                "driver_state",
                {"available": True, "provider": "ABB", "version": "1.1", "status": "ready"},
            ),
            "device profile version": (
                "device_profile",
                {"id": "abb-rws-rob1-base-v1", "version": 2, "approval_status": "approved"},
            ),
        }
        for label, (field, value) in mutations.items():
            with self.subTest(field=label):
                changed = copy.deepcopy(baseline_config)
                changed["interfaces"][0][field] = value
                self.assertNotEqual(readiness_config_fingerprint(changed), baseline)

        changed_revision = copy.deepcopy(baseline_config)
        changed_revision["release_revision"] = "build-b"
        self.assertNotEqual(readiness_config_fingerprint(changed_revision), baseline)

    def test_fingerprint_never_contains_plaintext_credentials(self) -> None:
        fingerprint = readiness_config_fingerprint(self._config())

        self.assertNotIn("operator", fingerprint)
        self.assertNotIn("credential-a", fingerprint)
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")


if __name__ == "__main__":
    unittest.main()
