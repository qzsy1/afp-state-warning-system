from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from interface_monitor_demo.interface_catalog import build_interface_catalog
from interface_monitor_demo.monitor import InterfaceMonitor


class InterfaceCatalogTests(unittest.TestCase):
    def test_catalog_has_five_interfaces_and_separate_pressure_channels(self) -> None:
        catalog = build_interface_catalog()
        self.assertEqual(len(catalog), 5)
        by_id = {item["id"]: item for item in catalog}

        self.assertIn("压力", by_id["plc_process"]["channels"])
        self.assertNotIn("薄膜压力", by_id["plc_process"]["channels"])
        self.assertEqual(by_id["m3232_pressure"]["channels"], ["薄膜压力"])
        self.assertEqual(by_id["m3232_pressure"]["endpoint"], "COM8")
        self.assertEqual(by_id["m3232_pressure"]["baudrate"], 115200)

    def test_catalog_channels_have_one_owner(self) -> None:
        owners: dict[str, str] = {}
        for interface in build_interface_catalog():
            for channel in interface["channels"]:
                self.assertNotIn(channel, owners)
                owners[channel] = interface["id"]


class InterfaceMonitorTests(unittest.TestCase):
    def test_initial_snapshot_marks_all_interfaces_healthy(self) -> None:
        snapshot = InterfaceMonitor().snapshot()

        self.assertTrue(snapshot["simulated"])
        self.assertEqual(len(snapshot["interfaces"]), 5)
        self.assertTrue(all(item["state"] == "healthy" for item in snapshot["interfaces"]))
        self.assertIsNone(snapshot["active_event"])

    def test_m3232_parse_error_creates_film_pressure_event(self) -> None:
        event = InterfaceMonitor().apply_scenario("m3232_pressure", "parse_error")

        self.assertEqual(event["interface_id"], "m3232_pressure")
        self.assertEqual(event["sensor_name"], "M3232薄膜压力")
        self.assertEqual(event["channels"], ["薄膜压力"])
        self.assertEqual(event["state"], "invalid_data")
        self.assertNotIn("压力", event["channels"])
        self.assertGreater(event["evidence"]["invalid_samples"], 0)
        self.assertTrue(event["simulated"])

    def test_partial_channel_event_names_only_missing_channels(self) -> None:
        event = InterfaceMonitor().apply_scenario("thermocouple_8ch", "partial_channels")

        self.assertEqual(event["state"], "partial_channels")
        self.assertEqual(event["evidence"]["missing_channels"], ["温度7", "温度8"])
        self.assertEqual(event["channels"], [f"温度{index}" for index in range(1, 9)])

    def test_partial_channel_scenario_rejects_single_channel_interface(self) -> None:
        with self.assertRaisesRegex(ValueError, "多通道"):
            InterfaceMonitor().apply_scenario("m3232_pressure", "partial_channels")

    def test_healthy_scenario_clears_only_that_interface_event(self) -> None:
        monitor = InterfaceMonitor()
        monitor.apply_scenario("plc_process", "timeout")
        monitor.apply_scenario("m3232_pressure", "parse_error")

        state = monitor.apply_scenario("m3232_pressure", "healthy")
        snapshot = monitor.snapshot()

        self.assertEqual(state["state"], "healthy")
        self.assertEqual(len(snapshot["events"]), 1)
        self.assertEqual(snapshot["events"][0]["interface_id"], "plc_process")

    def test_unknown_interface_and_scenario_are_rejected(self) -> None:
        monitor = InterfaceMonitor()
        with self.assertRaisesRegex(ValueError, "未知接口"):
            monitor.apply_scenario("missing", "timeout")
        with self.assertRaisesRegex(ValueError, "未知故障场景"):
            monitor.apply_scenario("plc_process", "made_up")

    def test_reset_restores_all_interfaces_and_clears_events(self) -> None:
        monitor = InterfaceMonitor()
        monitor.apply_scenario("uvc_temperature", "open_failed")

        snapshot = monitor.reset()

        self.assertTrue(all(item["state"] == "healthy" for item in snapshot["interfaces"]))
        self.assertEqual(snapshot["events"], [])
        self.assertIsNone(snapshot["active_event"])

    def test_concurrent_scenario_requests_receive_unique_event_ids(self) -> None:
        monitor = InterfaceMonitor()

        with ThreadPoolExecutor(max_workers=8) as executor:
            events = list(
                executor.map(
                    lambda _: monitor.apply_scenario("plc_process", "timeout"),
                    range(40),
                )
            )

        event_ids = [event["event_id"] for event in events]
        self.assertEqual(len(event_ids), len(set(event_ids)))


if __name__ == "__main__":
    unittest.main()
