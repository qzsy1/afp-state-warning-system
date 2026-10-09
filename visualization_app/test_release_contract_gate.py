from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from acquisition import AcquisitionManager, acquisition_config_from_payload
from helper_relay import HelperRegistry
from release_contract import ReleaseContractError


def _real_payload() -> dict:
    return {
        "acquisition_mode": "real",
        "real_acquisition_mode": "local_direct",
        "dataset_schema": "new_collection_v11_3",
        "persistence_policy": "engineering_preview",
        "driver": "serial_json",
        "interfaces": [
            {
                "id": "temperature-device",
                "enabled": True,
                "driver": "serial_json",
                "endpoint": "COM9",
                "role": "custom",
                "physical_interface_id": "serial:COM9",
                "physical_port_id": "COM9",
                "physical_interface_kind": "serial",
                "physical_verified": True,
                "channel_map": {"temperature": "温度"},
            }
        ],
        "interface_channel_assignments": {"temperature-device": ["温度"]},
        "selected_sensors": ["温度"],
        "execution_host": "server",
    }


class ReleaseContractStartGateTests(unittest.TestCase):
    def test_local_contract_mismatch_stops_before_driver_and_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            capture_root = Path(temporary) / "capture"
            manager = AcquisitionManager(capture_root)
            config = acquisition_config_from_payload(_real_payload())
            config.release_revision = "wrong-revision"
            with patch("acquisition.build_driver") as build_driver:
                build_driver.side_effect = AssertionError("device construction reached")
                with self.assertRaises(ReleaseContractError):
                    manager.start(config)
            build_driver.assert_not_called()
            self.assertIsNone(manager.session_dir)

    def test_remote_contract_mismatch_stops_before_command_send(self) -> None:
        registry = HelperRegistry()
        pairing = registry.start_pairing("session-1")
        completed = registry.complete_pairing(
            pairing["challenge"],
            "helper-1",
            capabilities={
                "protocol_version": 2,
                "helper_version": "wrong-version",
                "build_id": "wrong-revision",
                "unified_frame_v1": True,
                "unified_frame_contract": "unified_frame_v1",
                "command_lifecycle": "isolated_hardware_check",
            },
        )
        sender = Mock()
        self.assertTrue(
            registry.attach(
                "session-1",
                "helper-1",
                completed["pairing_token"],
                sender,
                capabilities=registry.status("session-1")["capabilities"],
            )
        )

        result = registry.command("session-1", "start_capture", _real_payload())

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "release_contract_mismatch")
        sender.assert_not_called()


if __name__ == "__main__":
    unittest.main()
