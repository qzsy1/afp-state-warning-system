from __future__ import annotations

import csv
import json
import threading
import time
import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from local_direct_acquisition import LocalDirectAdapter  # noqa: E402
import acquisition  # noqa: E402
from real_acquisition import (  # noqa: E402
    ChannelQuality,
    ChannelSample,
    ChannelSampleCache,
    FrameAssembler,
)


DEVICE_CASES = (
    ("plc_process", "modbus_tcp", "温度"),
    ("abb_motion", "abb_robot", "ABB_X"),
    ("uvc_temperature", "uvc_thermal", "ROI平均温度"),
)


def wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class RecoveringDriver:
    def __init__(self, channel: str, *, fail_open: bool = False, fail_read: bool = False):
        self.channel = channel
        self.fail_open = fail_open
        self.fail_read = fail_read
        self.open_calls = 0
        self.read_calls = 0
        self.close_calls = 0
        self.open_times: list[float] = []

    def open(self) -> None:
        self.open_calls += 1
        self.open_times.append(time.monotonic())
        if self.fail_open and self.open_calls == 1:
            raise TimeoutError("injected open timeout")

    def read_sample(self):
        self.read_calls += 1
        if self.fail_read and self.read_calls == 1:
            raise ConnectionError("injected read disconnect")
        return {self.channel: float(self.read_calls)}

    def close(self) -> None:
        self.close_calls += 1


class BlockingDriver:
    def __init__(self, channel: str):
        self.channel = channel
        self.entered = threading.Event()
        self.release = threading.Event()
        self.close_calls = 0

    def open(self) -> None:
        pass

    def read_sample(self):
        self.entered.set()
        self.release.wait(2.0)
        return None

    def close(self) -> None:
        self.close_calls += 1
        self.release.set()


class ContinuousDriver:
    def __init__(self, values: dict[str, float]):
        self.values = dict(values)
        self.read_calls = 0
        self.close_calls = 0
        self.protocol_verified = True
        self.quality_metadata = {"source": "parallel_fixture"}
        self.source_timestamp = None

    def open(self) -> None:
        pass

    def read_sample(self):
        self.read_calls += 1
        self.source_timestamp = time.time()
        return {
            name: value + self.read_calls * 0.001
            for name, value in self.values.items()
        }

    def close(self) -> None:
        self.close_calls += 1


class PlcAbbUvcFaultIsolationTests(unittest.TestCase):
    def test_blocking_close_is_bounded_and_cannot_be_finalized_as_success(self) -> None:
        release = threading.Event()
        closing = threading.Event()

        class Driver(RecoveringDriver):
            def close(self):
                closing.set()
                release.wait(1.0)

        driver = Driver("ROI平均温度")
        adapter, _cache = self._adapter("uvc_temperature", "uvc_thermal", "ROI平均温度", driver)
        adapter.start()
        self.assertTrue(adapter.opened.wait(0.2))
        multi = acquisition.MultiInterfaceDriver([], ["ROI平均温度"])
        multi.workers = [adapter]
        manager = acquisition.AcquisitionManager(recovery_scan=False)
        manager.driver = multi
        manager.config = acquisition.AcquisitionConfig()
        try:
            started = time.monotonic()
            stopped = manager.stop()
            self.assertLess(time.monotonic() - started, 0.7)
            self.assertTrue(closing.is_set())
            self.assertFalse(stopped["finalization_complete"])
            self.assertEqual(stopped["capture_phase"], "stop_timed_out")
            self.assertEqual(multi.worker_status()[0]["state"], "stop_timed_out")
            with patch.object(manager, "_start_new") as start_new:
                with self.assertRaisesRegex(RuntimeError, "previous_capture_stop_timed_out"):
                    manager.start(acquisition.AcquisitionConfig())
                start_new.assert_not_called()
        finally:
            release.set()
            adapter.stop(timeout_seconds=0.5)

    def _adapter(self, interface_id: str, driver_name: str, channel: str, driver):
        cache = ChannelSampleCache()
        adapter = LocalDirectAdapter(
            interface_id,
            driver,
            [channel],
            cache,
            poll_interval_seconds=0.001,
            minimum_poll_seconds=0.001,
            reconnect_backoff_seconds=0.02,
            max_reconnect_backoff_seconds=0.02,
            max_reconnect_attempts=3,
        )
        return adapter, cache

    def test_each_device_retries_open_with_backoff_and_recovers(self) -> None:
        for interface_id, driver_name, channel in DEVICE_CASES:
            with self.subTest(driver=driver_name):
                driver = RecoveringDriver(channel, fail_open=True)
                adapter, cache = self._adapter(
                    interface_id, driver_name, channel, driver
                )
                adapter.start()
                try:
                    self.assertTrue(
                        wait_until(lambda: cache.latest(channel) is not None),
                        adapter.status(),
                    )
                    self.assertGreaterEqual(driver.open_calls, 2)
                    self.assertGreaterEqual(driver.close_calls, 1)
                    self.assertGreaterEqual(
                        driver.open_times[1] - driver.open_times[0], 0.014
                    )
                    self.assertEqual(adapter.status()["reconnect_attempts"], 1)
                finally:
                    self.assertTrue(adapter.stop(timeout_seconds=0.2))

    def test_each_device_reconnects_after_read_disconnect_and_closes(self) -> None:
        for interface_id, driver_name, channel in DEVICE_CASES:
            with self.subTest(driver=driver_name):
                driver = RecoveringDriver(channel, fail_read=True)
                adapter, cache = self._adapter(
                    interface_id, driver_name, channel, driver
                )
                adapter.start()
                try:
                    self.assertTrue(wait_until(lambda: cache.latest(channel) is not None))
                    self.assertGreaterEqual(driver.open_calls, 2)
                    self.assertEqual(adapter.status()["state"], "running")
                finally:
                    self.assertTrue(adapter.stop(timeout_seconds=0.2))
                self.assertGreaterEqual(driver.close_calls, 2)

    def test_one_blocked_device_does_not_stop_other_samples_or_status(self) -> None:
        for blocked_id, blocked_name, blocked_channel in DEVICE_CASES:
            with self.subTest(blocked=blocked_name):
                adapters = []
                caches = {}
                blocking = BlockingDriver(blocked_channel)
                for interface_id, driver_name, channel in DEVICE_CASES:
                    driver = (
                        blocking
                        if interface_id == blocked_id
                        else RecoveringDriver(channel)
                    )
                    adapter, cache = self._adapter(
                        interface_id, driver_name, channel, driver
                    )
                    adapters.append(adapter)
                    caches[interface_id] = (channel, cache)
                    adapter.start()
                try:
                    self.assertTrue(blocking.entered.wait(0.5))
                    for interface_id, _driver_name, channel in DEVICE_CASES:
                        if interface_id == blocked_id:
                            continue
                        cache = caches[interface_id][1]
                        self.assertTrue(
                            wait_until(lambda c=cache, n=channel: c.latest(n) is not None)
                        )
                    started = time.monotonic()
                    status = [adapter.status() for adapter in adapters]
                    self.assertLess(time.monotonic() - started, 0.05)
                    self.assertEqual(len(status), 3)
                finally:
                    stopped_at = time.monotonic()
                    stopped = [
                        adapter.stop(timeout_seconds=0.2) for adapter in adapters
                    ]
                self.assertTrue(all(stopped))
                self.assertLess(time.monotonic() - stopped_at, 0.6)
                self.assertGreaterEqual(blocking.close_calls, 1)

    def test_virtual_disconnect_timeline_never_reuses_stale_values_as_valid_data(self) -> None:
        channels = [item[2] for item in DEVICE_CASES]
        cache = ChannelSampleCache()
        assembler = FrameAssembler(
            channels,
            10.0,
            {name: 0.2 for name in channels},
            clock=lambda: 1.0,
            wall_clock=lambda: 1000.0,
            start_monotonic=0.0,
            start_wall_time=1000.0,
        )
        for index, (interface_id, _driver_name, channel) in enumerate(DEVICE_CASES):
            cache.publish(
                ChannelSample(
                    interface_id=interface_id,
                    channel_name=channel,
                    value=10.0 + index,
                    received_wall_time=1000.05,
                    received_monotonic=0.05,
                    source_sequence=1,
                )
            )

        fresh = assembler.assemble(cache, 0.1)
        held = assembler.assemble(cache, 0.15)
        disconnected = assembler.assemble(cache, 0.31)
        cache.publish(
            ChannelSample(
                interface_id="uvc_temperature",
                channel_name="ROI平均温度",
                value=25.0,
                received_wall_time=1000.32,
                received_monotonic=0.32,
                source_sequence=2,
            )
        )
        recovered = assembler.assemble(cache, 0.33)

        self.assertTrue(
            all(sample.quality == ChannelQuality.MEASURED_NEW for sample in fresh.channels.values())
        )
        self.assertTrue(
            all(sample.quality == ChannelQuality.HELD_WITHIN_FRESHNESS for sample in held.channels.values())
        )
        for sample in disconnected.channels.values():
            self.assertEqual(sample.quality, ChannelQuality.STALE)
            self.assertIsNone(sample.value)
            self.assertEqual(sample.source_sequence, 1)
        self.assertEqual(
            recovered.channels["ROI平均温度"].quality,
            ChannelQuality.MEASURED_NEW,
        )
        self.assertEqual(recovered.channels["ROI平均温度"].value, 25.0)
        self.assertIsNone(recovered.channels["温度"].value)
        self.assertIsNone(recovered.channels["ABB_X"].value)

    def test_three_device_ten_hz_frames_and_saved_rows_reconcile(self) -> None:
        selected = [
            "温度", "压力", "张力",
            "ABB_X", "ABB_Y", "ABB_Z", "线速度",
            "ROI平均温度",
        ]
        drivers = {
            "plc": ContinuousDriver({"温度": 102.0, "压力": 35.0, "张力": 2.0}),
            "abb": ContinuousDriver(
                {"ABB_X": 412.0, "ABB_Y": 2205.0, "ABB_Z": 629.0, "线速度": 1.0}
            ),
            "uvc": ContinuousDriver({"ROI平均温度": 36.5}),
        }
        staging = Path(r"D:\AFP_Runtime\.afp-staging")
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="plc-abb-uvc-parallel-", dir=staging
        ) as temporary:
            save_root = Path(temporary) / "capture"
            save_root.mkdir()
            config = acquisition.AcquisitionConfig(
                processing_mode="capture_only",
                acquisition_mode="real",
                real_acquisition_mode="local_direct",
                dataset_schema="new_collection_v11_3",
                selected_sensors=selected,
                interfaces=[
                    {
                        "id": "plc_process", "role": "plc",
                        "driver": "modbus_tcp", "endpoint": "192.168.125.5:502",
                        "physical_interface_id": "ethernet:industrial",
                        "physical_interface_kind": "ethernet", "physical_verified": True,
                    },
                    {
                        "id": "abb_motion", "role": "robot",
                        "driver": "abb_robot", "endpoint": "192.168.125.1",
                        "physical_interface_id": "ethernet:industrial",
                        "physical_interface_kind": "ethernet", "physical_verified": True,
                    },
                    {
                        "id": "uvc_temperature", "role": "thermal_uvc",
                        "driver": "uvc_thermal", "endpoint": "bsv-uvc-2bdf-0102-v1",
                        "physical_interface_id": "uvc:bsv:fixture",
                        "physical_port_id": "usbport:fixture:1",
                        "physical_interface_kind": "usb_uvc", "physical_verified": True,
                    },
                ],
                interface_channel_assignments={
                    "plc_process": ["温度", "压力", "张力"],
                    "abb_motion": ["ABB_X", "ABB_Y", "ABB_Z", "线速度"],
                    "uvc_temperature": ["ROI平均温度"],
                },
                sample_rate_hz=10.0,
                save_root=str(save_root),
                persistence_policy="required",
                first_sample_timeout_seconds=2.0,
                stop_sampling_timeout_seconds=1.0,
                stop_force_timeout_seconds=1.0,
                writer_stop_timeout_seconds=1.0,
                condition_id="PARALLEL_FIXTURE",
                specimen_id="PARALLEL_FIXTURE",
                run_id="PARALLEL_FIXTURE",
                layer=0,
            )
            route = {
                "ok": True,
                "status": "network_path_ready",
                "actual_source_address": "192.168.125.209",
                "actual_interface_id": "ethernet:industrial",
            }
            manager = acquisition.AcquisitionManager(save_root)
            with patch(
                "acquisition.require_network_path", return_value=route
            ), patch(
                "acquisition.validate_network_path", return_value=route
            ), patch(
                "acquisition.device_profile_readiness",
                return_value={"ready": True, "missing_device_facts": []},
            ), patch(
                "acquisition.ModbusTcpDriver", return_value=drivers["plc"]
            ), patch(
                "acquisition.AbbRobotDriver", return_value=drivers["abb"]
            ), patch(
                "acquisition.UvcThermalDriver", return_value=drivers["uvc"]
            ):
                checked = manager.test_connection(config)
                self.assertTrue(checked["ok"], checked)
                manager.start(config)
                deadline = time.monotonic() + 5.0
                while manager.status()["sample_count"] < 25 and time.monotonic() < deadline:
                    time.sleep(0.02)
                result = manager.stop()

            self.assertGreaterEqual(result["sample_count"], 25, result)
            self.assertTrue(result["capture_saved"], result)
            self.assertTrue(result["finalization_complete"], result)
            self.assertFalse(result["running"])
            self.assertEqual(result["last_error"], "")
            frame_quality = result["data_quality"]["unified_frame_quality"]
            self.assertEqual(frame_quality["frame_count"], result["sample_count"])
            self.assertGreaterEqual(frame_quality["actual_frame_rate_hz"], 8.0)
            self.assertLessEqual(frame_quality["actual_frame_rate_hz"], 12.0)
            self.assertGreaterEqual(frame_quality["complete_frame_rate"], 0.95)
            sensor_status = {item["name"]: item for item in result["sensors"]}
            interface_status = {item["id"]: item for item in result["interfaces"]}
            for interface_id, expected in config.interface_channel_assignments.items():
                self.assertEqual(interface_status[interface_id]["state"], "stopped")
                self.assertNotIn("正在采集", interface_status[interface_id]["message"])
                self.assertEqual(
                    set(interface_status[interface_id]["detected_channels"]),
                    set(expected),
                )
            for channel in selected:
                self.assertEqual(
                    sensor_status[channel]["received_samples"], result["sample_count"]
                )
                self.assertEqual(
                    frame_quality["channels"][channel]["missing_rate"], 0.0
                )
                self.assertEqual(
                    frame_quality["channels"][channel]["stale_rate"], 0.0
                )

            layer_file = Path(result["layer_file"])
            timestamp_file = Path(result["timestamp_file"])
            with layer_file.open("r", encoding="gb18030", newline="") as handle:
                saved_rows = list(csv.DictReader(handle))
            with timestamp_file.open("r", encoding="utf-8-sig", newline="") as handle:
                timestamp_rows = list(csv.DictReader(handle))
            self.assertEqual(len(saved_rows), result["sample_count"])
            self.assertEqual(len(timestamp_rows), result["sample_count"])
            self.assertEqual(len(manager.frame_envelopes), result["sample_count"])
            for row in timestamp_rows:
                quality = json.loads(row["channel_quality_json"])
                self.assertEqual(set(quality), set(selected))
                self.assertTrue(
                    all(value in {"measured_new", "held_within_freshness"} for value in quality.values())
                )
            self.assertTrue(all(driver.close_calls >= 1 for driver in drivers.values()))


if __name__ == "__main__":
    unittest.main()
