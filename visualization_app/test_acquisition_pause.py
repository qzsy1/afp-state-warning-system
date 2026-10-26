from concurrent.futures import ThreadPoolExecutor
import csv
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import acquisition as a
from acquisition_clock import monotonic
from real_acquisition import FrameAssembler, VirtualClock, AcquisitionQualityMetrics, ChannelSampleCache
from simulation_replay import MonotonicReplayScheduler
from local_direct_acquisition import LocalDirectAdapter


class CountingDriver:
    def __init__(self):
        self.count = 0
        self.opens = 0
        self.closes = 0

    def open(self):
        self.opens += 1

    def close(self):
        self.closes += 1

    def read_sample(self):
        self.count += 1
        return {"温度": float(self.count)}


def wait_until(predicate, timeout=3):
    deadline = monotonic() + timeout
    while not predicate():
        if monotonic() > deadline:
            raise AssertionError("condition timed out")
        time.sleep(.005)


class PauseLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = a.AcquisitionManager(self.root, recovery_scan=False)
        self.driver = CountingDriver()
        self.cfg = a.AcquisitionConfig(acquisition_mode="simulation", processing_mode="capture_only",
            dataset_schema="new_collection_v11_3", selected_sensors=["温度"], sample_rate_hz=50,
            interfaces=[{"id": "sim", "role": "custom", "driver": "simulator"}],
            interface_channel_assignments={"sim": ["温度"]}, save_root=str(self.root), specimen_id="PAUSE_TEST")
        self.patch = patch("acquisition.build_driver", return_value=self.driver)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(self.manager.stop)

    def start(self):
        self.manager.start(self.cfg)
        wait_until(lambda: self.manager.total_sample_count >= 3)

    def test_repeated_pause_resume_saves_one_identity_and_all_segments(self):
        self.start()
        identity = self.manager.capture_uuid
        work = self.manager.raw_work_path
        last = 0
        for _ in range(3):
            paused = self.manager.pause()
            self.assertTrue(paused["paused"])
            self.assertTrue(paused["session_active"])
            self.assertFalse(paused["sampling"])
            count = paused["sample_count"]
            calls = self.driver.count
            time.sleep(.08)
            self.assertEqual(self.driver.count, calls)
            self.assertEqual(self.manager.total_sample_count, count)
            self.assertEqual(self.manager.capture_uuid, identity)
            self.assertEqual(self.manager.raw_work_path, work)
            self.assertFalse(self.manager.raw_path.exists())
            self.assertEqual(self.manager.completed_layers, [])
            resumed = self.manager.resume()
            self.assertTrue(resumed["sampling"])
            wait_until(lambda: self.manager.total_sample_count > count + 2)
            self.assertGreater(self.manager.total_sample_count, last)
            last = self.manager.total_sample_count
        result = self.manager.stop()
        self.assertTrue(result["capture_saved"], result.get("last_error"))
        self.assertTrue(result["finalization_complete"])
        self.assertEqual(self.driver.opens, 1)
        with Path(result["layer_file"]).open(encoding="gb18030", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([float(row["温度"]) for row in rows], list(map(float, range(1, len(rows) + 1))))
        with Path(result["timestamp_file"]).open(encoding="utf-8-sig", newline="") as handle:
            stamps = list(csv.DictReader(handle))
        self.assertEqual(len(stamps), result["sample_count"])
        for gap in result["pause_intervals"]:
            index = gap["row_index"]
            self.assertGreater(float(stamps[index]["timestamp_unix"]) - float(stamps[index - 1]["timestamp_unix"]), .07)
        self.assertEqual(result["data_quality"]["gaps_over_twice_expected"], 0)

    def test_stop_while_paused_and_duplicate_controls(self):
        self.start()
        paused = self.manager.pause()
        self.assertEqual(self.manager.pause()["control_revision"], paused["control_revision"])
        result = self.manager.stop()
        self.assertTrue(result["capture_saved"])
        self.assertEqual(result["sample_count"], paused["sample_count"])
        self.assertFalse(result["session_active"])
        self.assertEqual(self.manager.stop()["sample_count"], result["sample_count"])
        with self.assertRaisesRegex(RuntimeError, "capture_not_paused"):
            self.manager.resume()

    def test_paused_session_cannot_be_replaced_or_controlled_with_old_identity(self):
        self.start()
        paused = self.manager.pause()
        with self.assertRaisesRegex(RuntimeError, "会话仍有效"):
            self.manager.start(self.cfg)
        with self.assertRaisesRegex(RuntimeError, "capture_identity_changed"):
            self.manager.resume(capture_uuid="another-capture")
        with self.assertRaisesRegex(RuntimeError, "capture_control_conflict"):
            self.manager.resume(expected_revision=paused["control_revision"] - 1)
        self.assertTrue(self.manager.status()["paused"])

    def test_late_stop_from_previous_layer_cannot_stop_same_specimen_new_generation(self):
        self.start()
        old = self.manager.status()
        self.assertTrue(self.manager.stop()["capture_saved"])
        self.cfg.layer += 1
        self.manager.start(self.cfg)
        self.assertEqual(self.manager.capture_uuid, old["capture_uuid"])
        self.assertNotEqual(self.cfg.capture_generation, old["capture_generation"])
        with self.assertRaisesRegex(RuntimeError, "capture_generation_changed"):
            self.manager.stop(capture_uuid=old["capture_uuid"], capture_generation=old["capture_generation"])
        self.assertTrue(self.manager.status()["sampling"])

    def test_concurrent_pause_is_idempotent_and_resume_stop_are_serialized(self):
        self.start()
        with ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: self.manager.pause(), range(4)))
        self.assertEqual(len({r["sample_count"] for r in replies}), 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            resume = pool.submit(self.manager.resume)
            stop = pool.submit(self.manager.stop)
            try:
                resume.result()
            except RuntimeError as exc:
                self.assertIn("capture_not_paused", str(exc))
            self.assertTrue(stop.result()["finalization_complete"])

    def test_request_timeout_does_not_claim_pause_until_sampling_confirms(self):
        self.start()
        entered = threading.Event()
        release = threading.Event()
        original = self.driver.read_sample
        def blocked_read():
            entered.set()
            release.wait(2)
            return original()
        self.driver.read_sample = blocked_read
        self.assertTrue(entered.wait(1))
        try:
            with self.assertRaisesRegex(TimeoutError, "capture_control_timeout"):
                self.manager.pause(timeout_seconds=.01)
            self.assertFalse(self.manager.status()["paused"])
            self.assertEqual(self.manager.status()["capture_phase"], "pausing")
        finally:
            release.set()
        wait_until(lambda: self.manager.status()["paused"])

    def test_save_failure_keeps_session_locked_and_layer_unfinished(self):
        self.start()
        self.manager.pause()
        with patch.object(self.manager, "_finalize_working_files", side_effect=OSError("isolated disk failure")):
            result = self.manager.stop()
        self.assertFalse(result["capture_saved"])
        self.assertFalse(result["finalization_complete"])
        self.assertTrue(result["save_failed"])
        self.assertTrue(result["session_active"])
        self.assertEqual(result["completed_layers"], [])
        with self.assertRaisesRegex(RuntimeError, "previous_capture"):
            self.manager.start(self.cfg)

    def test_boundary_timeout_keeps_sampling_owner_alive_and_allows_retry(self):
        self.start()
        paused = self.manager.pause()
        original = self.manager._set_sources_paused
        release = threading.Event()

        def blocked_boundary(is_paused):
            if not release.is_set():
                raise TimeoutError("capture_boundary_timeout: isolated busy device")
            return original(is_paused)

        with patch.object(self.manager, "_set_sources_paused", side_effect=blocked_boundary):
            with self.assertRaisesRegex(RuntimeError, "capture_boundary_timeout"):
                self.manager.resume()
            time.sleep(.08)
            failed = self.manager.status()
            self.assertTrue(self.manager.thread.is_alive())
            self.assertTrue(failed["session_active"])
            self.assertFalse(failed["sampling"])
            self.assertFalse(failed["paused"])
            self.assertEqual(failed["sample_count"], paused["sample_count"])
            release.set()
            wait_until(lambda: self.manager.status()["paused"])
        self.assertEqual(len(self.manager.pause_intervals), 1)
        self.manager.resume()
        wait_until(lambda: self.manager.total_sample_count > paused["sample_count"] + 1)
        saved = self.manager.stop()
        self.assertTrue(saved["capture_saved"])
        self.assertEqual(saved["capture_uuid"], paused["capture_uuid"])

    def test_stop_saves_after_resume_and_rollback_boundary_time_out(self):
        self.start()
        paused = self.manager.pause()
        with patch.object(self.manager, "_set_sources_paused", side_effect=TimeoutError("capture_boundary_timeout")):
            with self.assertRaisesRegex(RuntimeError, "capture_boundary_timeout"):
                self.manager.resume()
            result = self.manager.stop()
        self.assertTrue(result["capture_saved"], result.get("last_error"))
        self.assertTrue(result["finalization_complete"])
        self.assertEqual(result["sample_count"], paused["sample_count"])
        self.assertEqual(result["capture_uuid"], paused["capture_uuid"])

    def test_no_samples_pause_stop_preserves_zero_sample_save_rule(self):
        self.driver.read_sample = lambda: None
        self.manager.start(self.cfg)
        self.manager.pause()
        result = self.manager.stop()
        self.assertFalse(result["capture_saved"])
        self.assertEqual(result["sample_count"], 0)
        self.assertEqual(result["completed_layers"], [])


class PauseBoundaryTests(unittest.TestCase):
    def test_disconnect_retains_connection_if_paused_capture_save_fails(self):
        from test_acquisition_start import config
        class Device(CountingDriver):
            quality_metadata = {}
            protocol_verified = True
            def __init__(self, *args): super().__init__()
            def read_sample(self): return {"温度": 10., "压力": 20.}
        with tempfile.TemporaryDirectory() as root, patch.object(a, "SerialJsonDriver", Device):
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            manager.connect_data(cfg)
            manager._connection_thread.join(3)
            try:
                manager.start(cfg)
                manager.pause()
                driver = manager._connection_driver
                with patch.object(manager, "_finalize_working_files", side_effect=OSError("isolated save failure")):
                    with self.assertRaisesRegex(RuntimeError, "保存尚未完成"):
                        manager.disconnect_data()
                self.assertIs(manager._connection_driver, driver)
                self.assertFalse(manager._connection_cancel.is_set())
                self.assertTrue(manager.status()["save_failed"])
                self.assertEqual(manager.completed_layers, [])
            finally:
                manager.stop()
                manager._connection_cancel.set()
                manager._connection_driver.close(timeout_seconds=2)

    def test_matrix_resume_drops_partial_input_without_resetting_session_counters(self):
        from test_m3232_pressure import driver_double, ZERO_FRAME, RAW_CHANNEL
        device, port = driver_double()
        device.open()
        try:
            port.data = ZERO_FRAME * 2
            self.assertEqual(len(device.read_samples()), 2)
            sequence, frames, identity = device.sequence, device.total_frames, device.generation
            worker = LocalDirectAdapter("matrix", device, [RAW_CHANNEL], ChannelSampleCache())
            port.data = ZERO_FRAME[:500]
            worker._discard_paused_input()
            worker.reset_sampling_boundary()
            self.assertEqual(device.sequence, sequence)
            self.assertEqual(device.total_frames, frames)
            self.assertEqual(device.generation, identity)
            port.data = ZERO_FRAME[500:] + ZERO_FRAME
            records = device.read_samples()
            self.assertEqual(len(records), 1)
            self.assertEqual(device.sequence, sequence + 1)
            self.assertEqual(device.total_frames, frames + 1)
        finally:
            device.close()

    def test_quality_metrics_do_not_count_pause_as_missing_samples_or_device_gap(self):
        clock = VirtualClock()
        cache = ChannelSampleCache()
        from real_acquisition import ChannelSample, ChannelQuality
        assembler = FrameAssembler(["温度"], 10, clock=clock.monotonic, wall_clock=clock.time, wait=clock.wait)
        metrics = AcquisitionQualityMetrics(["温度"], 10)
        for segment in range(2):
            if segment:
                clock.advance_processing(60)
                assembler.rebase(2)
                metrics.begin_segment()
            for _ in range(2):
                cache.publish(ChannelSample("fake", "温度", 1., ChannelQuality.MEASURED_NEW,
                    received_monotonic=clock.monotonic(), received_wall_time=clock.time(), source_sequence=metrics.frame_count))
                metrics.observe(assembler.assemble(cache, clock.monotonic()))
                clock.advance_processing(.1)
        result = metrics.summary()
        self.assertAlmostEqual(result["actual_frame_rate_hz"], 10)
        self.assertEqual(result["channels"]["温度"]["missing_rate"], 0)
        self.assertLess(result["channels"]["温度"]["maximum_update_gap_seconds"], .11)

    def test_connected_real_path_reuses_workers_and_filters_resume_sources(self):
        from test_acquisition_start import config
        class Device(CountingDriver):
            quality_metadata = {}
            protocol_verified = True
            def __init__(self, *args):
                super().__init__()
            def read_sample(self):
                self.count += 1
                return {"温度": float(self.count), "压力": float(self.count)}
        with tempfile.TemporaryDirectory() as root, patch.object(a, "SerialJsonDriver", Device):
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = config(root)
            cfg.sample_rate_hz = 50
            manager.connect_data(cfg)
            manager._connection_thread.join(3)
            self.assertTrue(manager.connection_status()["ready"])
            try:
                manager.start(cfg)
                workers = list(manager.driver.workers)
                paused = manager.pause()
                time.sleep(.15)
                self.assertEqual(manager.total_sample_count, paused["sample_count"])
                self.assertTrue(manager.connection_status()["ready"])
                resumed = manager.resume()
                wait_until(lambda: manager.total_sample_count > paused["sample_count"] + 1)
                self.assertEqual(manager.driver.workers, workers)
                self.assertTrue(all(s.received_monotonic > resumed["segment_boundary_monotonic"] for s in manager.driver.cache.events()))
                manager.pause()
                self.assertTrue(manager.stop()["capture_saved"])
                wait_until(lambda: manager.connection_status()["ready"])
            finally:
                manager.stop()
                manager.disconnect_data()

    def test_usb_pause_preserves_raw_sequence_and_excludes_paused_raw_data(self):
        from test_esr1001_usb import StreamingSerial, UsbIntegrationTests
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root, patch("serial.Serial", StreamingSerial), patch(
                "serial.tools.list_ports.comports", return_value=[SimpleNamespace(device="COM91")]):
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = UsbIntegrationTests.config(save_root=root)
            try:
                manager.connect_data(cfg)
                manager._connection_thread.join(4)
                self.assertTrue(manager.connection_status()["ready"], manager.connection_status())
                manager.start(cfg)
                manager.pause()
                device = manager.driver.workers[0].driver
                sequence = device.sequence
                time.sleep(.15)
                recorded = device.recorder.rows
                time.sleep(.15)
                self.assertEqual(device.sequence, sequence)
                self.assertEqual(device.recorder.rows, recorded)
                resumed = manager.resume()
                wait_until(lambda: manager.total_sample_count > resumed["sample_count"] + 2)
                self.assertGreater(device.sequence, sequence)
                manager.pause()
                result = manager.stop()
                self.assertTrue(result["capture_saved"], result["last_error"])
                evidence = manager.driver.interface_quality_metadata["esr"]
                self.assertEqual(evidence["saved_source_rows"], evidence["source_frames"])
            finally:
                manager.stop()
                manager.disconnect_data()

    def test_resume_timeout_leaves_session_paused_until_new_data_arrives(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            manager = a.AcquisitionManager(Path(root), recovery_scan=False)
            cfg = a.AcquisitionConfig(acquisition_mode="simulation", selected_sensors=["温度1"],
                save_root=root, sample_rate_hz=50, first_sample_timeout_seconds=.03)
            driver = CountingDriver()
            driver.read_sample = lambda: {"温度1": 1.0}
            with patch("acquisition.build_driver", return_value=driver):
                manager.start(cfg)
            try:
                manager.pause()
                worker = SimpleNamespace(state="running", thread=threading.current_thread(), receive_lock=threading.RLock(),
                    reset_sampling_boundary=lambda: None, capture_paused=True)
                driver.workers = [worker]
                driver.cache = ChannelSampleCache()
                with self.assertRaisesRegex(RuntimeError, "resume_fresh_sample_timeout"):
                    manager.resume()
                wait_until(lambda: manager.status()["paused"])
                self.assertTrue(manager.status()["paused"])
                self.assertTrue(worker.capture_paused)
            finally:
                driver.workers = []
                manager.stop()

    def test_clock_rebase_preserves_sequence_and_real_pause_gap(self):
        clock = VirtualClock()
        assembler = FrameAssembler(["温度"], 10, clock=clock.monotonic, wall_clock=clock.time, wait=clock.wait)
        scheduler = MonotonicReplayScheduler(10, clock=clock.monotonic, wait=clock.wait)
        clock.advance_processing(30)
        assembler.rebase(20)
        self.assertAlmostEqual(assembler.deadline(20), clock.monotonic())
        self.assertAlmostEqual(assembler.start_wall_time + assembler.deadline(20) - assembler.start_monotonic, clock.time())
        scheduler.rebase()
        scheduler.wait_next()
        self.assertAlmostEqual(clock.monotonic(), 30.1)

    def test_worker_discards_bounded_serial_input_without_reading_or_publishing_samples(self):
        from types import SimpleNamespace
        drained = []
        port = SimpleNamespace(in_waiting=20000, read=lambda size: drained.append(size))
        device = CountingDriver()
        device.serial = port
        worker = LocalDirectAdapter("fake", device, ["温度"], ChannelSampleCache())
        worker._discard_paused_input()
        self.assertEqual(drained, [16384])
        self.assertEqual(device.count, 0)
        self.assertEqual(worker.cache.snapshot(), {})

    def test_paused_worker_releases_receive_lock_during_poll_wait(self):
        device = CountingDriver()
        worker = LocalDirectAdapter("fake", device, ["温度"], ChannelSampleCache())
        waiting = threading.Event()
        release = threading.Event()
        worker.capture_paused = True

        class PausedWait(threading.Event):
            def wait(self, timeout=None):
                if threading.current_thread() is worker.thread:
                    waiting.set()
                    release.wait(2)
                return super().wait(timeout)

        worker.stop_event = PausedWait()
        worker.start()
        try:
            self.assertTrue(waiting.wait(1))
            acquired = worker.receive_lock.acquire(timeout=.05)
            if acquired:
                worker.receive_lock.release()
            self.assertTrue(acquired, "Paused poll wait must allow a resume boundary to take ownership")
            self.assertEqual(worker.cache.snapshot(), {})
        finally:
            worker.stop_event.set()
            release.set()
            worker.thread.join(2)


if __name__ == "__main__":
    unittest.main()
