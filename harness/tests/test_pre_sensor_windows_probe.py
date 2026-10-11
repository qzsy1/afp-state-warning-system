from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from visualization_app.pre_sensor_preflight import (
    _adapter_power_disabled_from_registry,
    _windows_time_status_synchronized,
    ExternalEnvironmentBlocked,
    generate_windows_remediation_script,
    probe_windows_readiness,
)


ROOT = Path(__file__).resolve().parents[2]


class WindowsReadinessProbeTests(unittest.TestCase):
    def test_active_adapter_registry_fallback_handles_unsupported_driver_cmdlet(self) -> None:
        registry_key = (
            "HKEY_LOCAL_MACHINE\\SYSTEM\\CurrentControlSet\\Control\\Class\\"
            "{4d36e972-e325-11ce-bfc1-08002be10318}\\0001"
        )

        def disabled(command):
            if "/f" in command:
                return registry_key + "\n    NetCfgInstanceId    REG_SZ    {ABC-123}\n"
            return registry_key + "\n    PnPCapabilities    REG_DWORD    0x20\n"

        def enabled(command):
            if "/f" in command:
                return registry_key + "\n    NetCfgInstanceId    REG_SZ    {ABC-123}\n"
            return registry_key + "\n    PnPCapabilities    REG_DWORD    0x0\n"

        self.assertTrue(_adapter_power_disabled_from_registry("ABC-123", disabled))
        self.assertFalse(_adapter_power_disabled_from_registry("ABC-123", enabled))

    def test_time_status_rejects_leap_alarm_even_with_non_cmos_source(self) -> None:
        self.assertFalse(
            _windows_time_status_synchronized(
                "Leap Indicator: 3(not synchronized)\nSource: 52.231.114.183,0x8\n"
            )
        )
        self.assertTrue(
            _windows_time_status_synchronized(
                "Leap Indicator: 0(no warning)\nSource: 52.231.114.183,0x9\n"
            )
        )

    def _ready_state(self) -> dict:
        return {
            "firewall_query_ok": True,
            "port_available": True,
            "proxy_bypass_local": True,
            "sleep_disabled_ac": True,
            "adapter_power_saving_disabled": True,
            "usb_selective_suspend_disabled": True,
            "time_service_running": True,
            "time_synchronized": True,
            "clock_offset_seconds": 0.05,
        }

    def test_all_windows_states_are_checked(self) -> None:
        outcome = probe_windows_readiness(
            {
                "windows_state_provider": self._ready_state,
                "maximum_clock_offset_seconds": 1.0,
            }
        )
        self.assertEqual(outcome.status, "passed")
        self.assertEqual(set(outcome.details), set(self._ready_state()))

    def test_each_unsafe_state_fails_with_specific_name(self) -> None:
        fields = (
            "firewall_query_ok",
            "port_available",
            "proxy_bypass_local",
            "sleep_disabled_ac",
            "adapter_power_saving_disabled",
            "usb_selective_suspend_disabled",
            "time_service_running",
            "time_synchronized",
        )
        for field in fields:
            with self.subTest(field=field):
                state = self._ready_state()
                state[field] = False
                with self.assertRaisesRegex(ValueError, field):
                    probe_windows_readiness(
                        {"windows_state_provider": lambda state=state: state}
                    )
        state = self._ready_state()
        state["clock_offset_seconds"] = 5.0
        with self.assertRaisesRegex(ValueError, "clock_offset_seconds"):
            probe_windows_readiness(
                {
                    "windows_state_provider": lambda: state,
                    "maximum_clock_offset_seconds": 1.0,
                }
            )

    def test_access_denied_is_external_blocker_not_sensor_pending(self) -> None:
        def denied():
            raise PermissionError("administrator rights required")

        with self.assertRaises(ExternalEnvironmentBlocked) as raised:
            probe_windows_readiness({"windows_state_provider": denied})
        self.assertIn("administrator", raised.exception.remediation.lower())

    def test_remediation_script_is_review_only_and_packaged(self) -> None:
        script = generate_windows_remediation_script(
            ["sleep_disabled_ac", "usb_selective_suspend_disabled"]
        )
        self.assertIn("Review only", script)
        self.assertNotIn("powercfg /change", script)
        self.assertNotIn("Set-Net", script)
        packaged = ROOT / "pre_sensor" / "review-windows-remediation.ps1"
        self.assertTrue(packaged.is_file())
        self.assertEqual(packaged.read_text(encoding="utf-8"), script)


if __name__ == "__main__":
    unittest.main()
